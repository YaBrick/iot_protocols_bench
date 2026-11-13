// Subscriber side of the MQTT bench: receives bench/data from the broker.
//
// Takes the same bench/ctrl commands as the publisher (see pub.c); size and
// count matter here, the other keys are validated and ignored. A command arms
// the run: the reference clock counter is cleared, so once the button starts
// the RPi clock, both boards count ticks from the same edge.
//
// Every arrival is stamped with this board's tick of that clock and paired with
// the publisher's tick from the payload header, so rx_tick - tx_tick is the
// one-way latency publisher -> broker -> subscriber, resolution 1/CONFIG_BENCH_CLOCK_HZ.
//
// The run ends when all `count` messages have arrived, after CONFIG_BENCH_SUB_IDLE_MS
// without data, on "stop", or when the next command arrives. Output:
//   bench/status   sub armed ...                 command accepted, waiting for data
//                  sub done run=N end=<complete|idle|stopped|rearmed> ...
//                  sub error ...
//   bench/sub/log  every arrival, duplicates included, as CSV, sent after "sub done":
//                    # run=N clock_hz=100000
//                    seq,tx_tick,rx_tick
//                    0,123,456 ...
//                    # end run=N lines=M
//                  many lines per MQTT message, QoS 0

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#include <sys/param.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_heap_caps.h"
#include "bench_clock.h"
#include "bench.h"

static const char *TAG = "sub";

#define CMD_MAX_LEN    128
#define CMD_QUEUE_LEN  4
#define SUB_TASK_STACK 6144
#define SUB_TASK_PRIO  5
#define POLL_MS        100
#define LOG_CHUNK      1024  // CSV bytes per bench/sub/log message

typedef struct {
    char text[CMD_MAX_LEN];
} sub_cmd_t;

typedef struct {
    uint32_t seq;
    uint32_t tx_tick;
    uint32_t rx_tick;
} sub_rec_t;

typedef enum {
    SUB_IDLE,     // bench/data is ignored
    SUB_ARMED,    // waiting for the first message
    SUB_RUNNING,
} sub_state_t;

typedef struct {
    sub_state_t state;
    bench_params_t p;
    uint32_t run_id;       // taken from the first message of the run
    uint8_t *seen;         // one bit per seq
    sub_rec_t *log;
    uint32_t log_cap;
    uint32_t log_len;
    uint32_t log_dropped;
    uint32_t rx;           // arrivals, duplicates included
    uint32_t unique;
    uint32_t dup;
    uint32_t reordered;    // arrived after a higher seq
    uint32_t foreign;      // another run id, or sent before this run's clock reset
    uint32_t bad;          // unparsable header or seq >= count
    uint32_t max_seq;
    uint32_t first_tx;     // earliest tx_tick of the run
    uint32_t last_rx;
    uint32_t lat_min;      // ticks
    uint32_t lat_max;
    uint64_t lat_sum;
    int64_t last_rx_us;    // esp_timer, for the idle timeout
} sub_run_t;

static esp_mqtt_client_handle_t s_client;
static QueueHandle_t s_cmd_queue;
// Shared between the MQTT task (record) and the sub task (arm, finish)
static sub_run_t s_run;
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

// Called with s_lock held
static void record_locked(sub_run_t *r, const bench_hdr_t *h, uint32_t rx_tick, int64_t now_us)
{
    if (r->state == SUB_IDLE) {
        return;
    }
    // tx_tick ahead of our clock: sent before this run's reset, e.g. a late retransmission
    if (h->tx_tick > rx_tick || (r->state == SUB_RUNNING && h->run != r->run_id)) {
        r->foreign++;
        return;
    }
    if (h->seq >= r->p.count) {
        r->bad++;
        return;
    }
    if (r->state == SUB_ARMED) {
        r->state = SUB_RUNNING;
        r->run_id = h->run;
    }

    r->rx++;
    r->last_rx = rx_tick;
    r->last_rx_us = now_us;
    if (r->log_len < r->log_cap) {
        r->log[r->log_len++] = (sub_rec_t){ .seq = h->seq, .tx_tick = h->tx_tick, .rx_tick = rx_tick };
    } else {
        r->log_dropped++;
    }

    uint8_t bit = 1u << (h->seq & 7);
    if (r->seen[h->seq >> 3] & bit) {
        r->dup++;
        return;
    }
    r->seen[h->seq >> 3] |= bit;
    if (r->unique > 0 && h->seq < r->max_seq) {
        r->reordered++;
    }
    r->max_seq = MAX(r->max_seq, h->seq);
    r->unique++;

    uint32_t lat = rx_tick - h->tx_tick;
    r->lat_min = MIN(r->lat_min, lat);
    r->lat_max = MAX(r->lat_max, lat);
    r->lat_sum += lat;
    r->first_tx = MIN(r->first_tx, h->tx_tick);
}

static void count_bad(void)
{
    taskENTER_CRITICAL(&s_lock);
    if (s_run.state != SUB_IDLE) {
        s_run.bad++;
    }
    taskEXIT_CRITICAL(&s_lock);
}

static bool topic_is(esp_mqtt_event_handle_t ev, const char *topic)
{
    return ev->topic_len == (int)strlen(topic) && memcmp(ev->topic, topic, ev->topic_len) == 0;
}

static void submit_command(const char *data, int len)
{
    if (len <= 0 || len >= CMD_MAX_LEN) {
        ESP_LOGW(TAG, "command length %d ignored", len);
        return;
    }
    sub_cmd_t cmd;
    memcpy(cmd.text, data, len);
    cmd.text[len] = '\0';
    if (xQueueSend(s_cmd_queue, &cmd, 0) != pdTRUE) {
        ESP_LOGW(TAG, "command queue full, dropped");
    }
}

// MQTT task context. rx_tick is read at the very start of the event handler.
static void on_data(esp_mqtt_event_handle_t ev, uint32_t rx_tick)
{
    // Payloads longer than the MQTT buffer come in several DATA events: only the
    // first one has the topic and the header, the arrival is stamped on the last one
    static bool s_is_data;
    static bool s_hdr_ok;
    static bench_hdr_t s_hdr;

    if (ev->current_data_offset == 0) {
        s_is_data = topic_is(ev, BENCH_TOPIC_DATA);
        if (!s_is_data) {
            if (topic_is(ev, BENCH_TOPIC_CTRL) && ev->data_len == ev->total_data_len) {
                submit_command(ev->data, ev->data_len);
            }
            return;
        }
        s_hdr_ok = bench_parse_header(ev->data, ev->data_len, &s_hdr);
    }
    if (!s_is_data || ev->current_data_offset + ev->data_len < ev->total_data_len) {
        return;
    }
    if (!s_hdr_ok) {
        count_bad();
        return;
    }

    int64_t now_us = esp_timer_get_time();
    taskENTER_CRITICAL(&s_lock);
    record_locked(&s_run, &s_hdr, rx_tick, now_us);
    taskEXIT_CRITICAL(&s_lock);
}

static bool log_publish(const char *buf, int len)
{
    // QoS 0 is written to the socket right here, so TCP paces the loop
    return esp_mqtt_client_publish(s_client, BENCH_TOPIC_SUB_LOG, buf, len, 0, 0) >= 0;
}

static void dump_log(const sub_run_t *r)
{
    char buf[LOG_CHUNK + 64];
    int len = snprintf(buf, sizeof(buf), "# run=%" PRIu32 " clock_hz=%d\nseq,tx_tick,rx_tick",
                       r->run_id, CONFIG_BENCH_CLOCK_HZ);

    for (uint32_t i = 0; i < r->log_len; i++) {
        if (len >= LOG_CHUNK) {
            if (!log_publish(buf, len)) {
                bench_status("error run=%" PRIu32 " reason=log_publish_failed line=%" PRIu32, r->run_id, i);
                return;
            }
            len = 0;
        }
        const sub_rec_t *rec = &r->log[i];
        // mosquitto_sub ends every message with a newline, so a chunk has none at its end
        len += snprintf(buf + len, sizeof(buf) - len, "%s%" PRIu32 ",%" PRIu32 ",%" PRIu32,
                        len ? "\n" : "", rec->seq, rec->tx_tick, rec->rx_tick);
    }
    if (len && !log_publish(buf, len)) {
        bench_status("error run=%" PRIu32 " reason=log_publish_failed", r->run_id);
        return;
    }
    len = snprintf(buf, sizeof(buf), "# end run=%" PRIu32 " lines=%" PRIu32, r->run_id, r->log_len);
    log_publish(buf, len);
}

static void finish(const char *end)
{
    // From here on the MQTT task ignores bench/data, the snapshot is ours alone
    taskENTER_CRITICAL(&s_lock);
    sub_run_t r = s_run;
    s_run.state = SUB_IDLE;
    s_run.seen = NULL;
    s_run.log = NULL;
    taskEXIT_CRITICAL(&s_lock);

    if (r.unique == 0) {
        bench_status("done end=%s rx=0 lost=%" PRIu32 " foreign=%" PRIu32 " bad=%" PRIu32,
                     end, r.p.count, r.foreign, r.bad);
    } else {
        // End-to-end: first publish on the publisher to last arrival here, on one timebase
        uint64_t span_us = bench_clock_ticks_to_us(r.last_rx - r.first_tx);
        uint64_t payload_bps = (uint64_t)r.unique * r.p.size * 1000000 / MAX(span_us, 1);
        bench_status("done run=%" PRIu32 " end=%s rx=%" PRIu32 " unique=%" PRIu32 " lost=%" PRIu32
                     " dup=%" PRIu32 " reordered=%" PRIu32 " foreign=%" PRIu32 " bad=%" PRIu32
                     " lat_min_us=%" PRIu64 " lat_avg_us=%" PRIu64 " lat_max_us=%" PRIu64
                     " span_us=%" PRIu64 " payload_Bps=%" PRIu64
                     " tick_first_tx=%" PRIu32 " tick_last_rx=%" PRIu32
                     " log=%" PRIu32 " log_dropped=%" PRIu32,
                     r.run_id, end, r.rx, r.unique, r.p.count - r.unique, r.dup, r.reordered,
                     r.foreign, r.bad,
                     bench_clock_ticks_to_us(r.lat_min), bench_clock_ticks_to_us(r.lat_sum / r.unique),
                     bench_clock_ticks_to_us(r.lat_max), span_us, payload_bps,
                     r.first_tx, r.last_rx, r.log_len, r.log_dropped);
        if (r.log_len) {
            dump_log(&r);
        }
    }
    free(r.seen);
    free(r.log);
}

static void arm(char *text)
{
    bench_params_t p;
    char err[64];
    if (!bench_parse_params(text, &p, err, sizeof(err))) {
        bench_status("error reason=%s", err);
        return;
    }
    // Clearing the counter while edges arrive would put this board out of step
    // with the publisher
    if (bench_clock_running()) {
        bench_status("error reason=clock_running");
        return;
    }

    uint8_t *seen = calloc((p.count + 7) / 8, 1);
    if (!seen) {
        bench_status("error reason=no_mem:count=%" PRIu32, p.count);
        return;
    }
    uint32_t log_cap = MIN(p.count, (uint32_t)CONFIG_BENCH_SUB_LOG_MAX);
    sub_rec_t *log = NULL;
    if (log_cap) {
        log = heap_caps_malloc_prefer((size_t)log_cap * sizeof(sub_rec_t), 2,
                                      MALLOC_CAP_SPIRAM, MALLOC_CAP_DEFAULT);
        if (!log) {
            ESP_LOGW(TAG, "no memory for %" PRIu32 " log records, log disabled", log_cap);
            log_cap = 0;
        }
    }

    bench_clock_reset();
    taskENTER_CRITICAL(&s_lock);
    s_run = (sub_run_t){
        .state = SUB_ARMED,
        .p = p,
        .seen = seen,
        .log = log,
        .log_cap = log_cap,
        .first_tx = UINT32_MAX,
        .lat_min = UINT32_MAX,
    };
    taskEXIT_CRITICAL(&s_lock);

    bench_status("armed size=%" PRIu32 " count=%" PRIu32 " sub_qos=%d log_cap=%" PRIu32,
                 p.size, p.count, CONFIG_BENCH_SUB_QOS, log_cap);
}

static void check_end(void)
{
    taskENTER_CRITICAL(&s_lock);
    sub_state_t state = s_run.state;
    bool all = s_run.unique >= s_run.p.count;
    int64_t last_rx_us = s_run.last_rx_us;
    taskEXIT_CRITICAL(&s_lock);

    // An armed run waits for the button as long as it takes
    if (state != SUB_RUNNING) {
        return;
    }
    if (all) {
        finish("complete");
    } else if (esp_timer_get_time() - last_rx_us > (int64_t)CONFIG_BENCH_SUB_IDLE_MS * 1000) {
        finish("idle");
    }
}

static void sub_task(void *arg)
{
    sub_cmd_t cmd;

    for (;;) {
        if (xQueueReceive(s_cmd_queue, &cmd, pdMS_TO_TICKS(POLL_MS)) != pdTRUE) {
            check_end();
            continue;
        }

        bool stop = strncmp(cmd.text, "stop", 4) == 0;
        taskENTER_CRITICAL(&s_lock);
        bool active = s_run.state != SUB_IDLE;
        taskEXIT_CRITICAL(&s_lock);
        if (active) {
            finish(stop ? "stopped" : "rearmed");
        }
        if (!stop) {
            arm(cmd.text);
        }
    }
}

static void mqtt_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_mqtt_event_handle_t ev = data;

    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_DATA:
        // Stamp first: everything else in this handler only adds delay
        on_data(ev, bench_clock_now());
        break;
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT connected");
        esp_mqtt_client_subscribe(ev->client, BENCH_TOPIC_CTRL, 1);
        esp_mqtt_client_subscribe(ev->client, BENCH_TOPIC_DATA, CONFIG_BENCH_SUB_QOS);
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT disconnected");
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "MQTT error, type=%d", ev->error_handle->error_type);
        break;
    default:
        break;
    }
}

void bench_sub_start(esp_mqtt_client_handle_t client)
{
    s_client = client;
    bench_common_init(client, "sub");
    s_cmd_queue = xQueueCreate(CMD_QUEUE_LEN, sizeof(sub_cmd_t));
    configASSERT(s_cmd_queue);

    // Off core 0, where Wi-Fi and lwIP run
    BaseType_t core = portNUM_PROCESSORS > 1 ? 1 : tskNO_AFFINITY;
    xTaskCreatePinnedToCore(sub_task, "sub", SUB_TASK_STACK, NULL, SUB_TASK_PRIO, NULL, core);
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL));
}
