// Publisher side of the MQTT bench: sends bench/data to the broker.
//
// Command on bench/ctrl, space-separated key=value, missing keys take defaults:
//   size=256 count=1000 qos=1 inflight=8 rate=0
//   stop
// size     - exact MQTT payload length, bytes (>= 33, the header below must fit)
// count    - messages per run
// qos      - 1 or 2
// inflight - unacknowledged messages allowed at once (1 = stop-and-wait)
// rate     - messages per second, 0 = as fast as the window allows
//
// A command only arms the run: the reference clock counter is cleared and the
// first message goes out once the RPi clock starts (button press). Each payload
// starts with "<run>:<seq>:<tx_tick>|", tx_tick being the clock tick right
// before esp_mqtt_client_publish(); the subscriber pairs it with its arrival tick.
//
// Status lines go to bench/status (QoS 0, so they add no PUBACKs to the capture):
//   pub armed run=N ...   command accepted, waiting for the clock
//   pub start run=N ...   clock started, before the first data message
//   pub done run=N ...    after the last PUBACK/PUBCOMP
//   pub error ...         invalid command or aborted run

#include <stdatomic.h>
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
#include "esp_system.h"
#include "esp_heap_caps.h"
#include "bench_clock.h"
#include "bench.h"

static const char *TAG = "pub";

#define CMD_MAX_LEN      128
#define CMD_QUEUE_LEN    4
#define ACK_QUEUE_LEN    (CONFIG_BENCH_MAX_INFLIGHT + 16)
#define ACK_TIMEOUT_US   (5 * 1000 * 1000)
#define HEAP_MARGIN      (16 * 1024)
#define BENCH_TASK_STACK 6144
#define BENCH_TASK_PRIO  5

typedef struct {
    char text[CMD_MAX_LEN];
} bench_cmd_t;

typedef struct {
    int msg_id;
    int64_t t_us;
} bench_ack_t;

typedef struct {
    int msg_id;  // 0 = free; esp-mqtt never hands out 0 for QoS > 0
    int64_t t_send_us;
} bench_slot_t;

typedef struct {
    bench_slot_t slots[CONFIG_BENCH_MAX_INFLIGHT];
    uint32_t n_slots;
    uint32_t outstanding;
    uint32_t acked;
    int64_t lat_min_us;
    int64_t lat_max_us;
    int64_t lat_sum_us;
} bench_run_t;

static esp_mqtt_client_handle_t s_client;
static QueueHandle_t s_cmd_queue;
static QueueHandle_t s_ack_queue;
static atomic_bool s_stop;
static atomic_bool s_connected;
static uint32_t s_run_id;
static bench_run_t s_run;

static bool parse_params(char *text, bench_params_t *p, char *err, size_t err_len)
{
    if (!bench_parse_params(text, p, err, err_len)) {
        return false;
    }

    // Our payload buffer plus the copy esp-mqtt keeps in its outbox for every unacked message
    size_t need = (size_t)p->size * (p->inflight + 1) + HEAP_MARGIN;
    size_t free_heap = esp_get_free_heap_size();
    if (free_heap < need || heap_caps_get_largest_free_block(MALLOC_CAP_8BIT) < p->size + 1024) {
        snprintf(err, err_len, "no_heap:need=%u,free=%u", (unsigned)need, (unsigned)free_heap);
        return false;
    }
    return true;
}

static void handle_ack(bench_run_t *run, const bench_ack_t *ack)
{
    for (uint32_t i = 0; i < run->n_slots; i++) {
        bench_slot_t *slot = &run->slots[i];
        if (slot->msg_id != ack->msg_id) {
            continue;
        }
        int64_t lat = ack->t_us - slot->t_send_us;
        run->lat_min_us = MIN(run->lat_min_us, lat);
        run->lat_max_us = MAX(run->lat_max_us, lat);
        run->lat_sum_us += lat;
        run->acked++;
        run->outstanding--;
        slot->msg_id = 0;
        return;
    }
    // Not ours: a late ack from an aborted run
}

static void track_sent(bench_run_t *run, int msg_id, int64_t t_send_us)
{
    for (uint32_t i = 0; i < run->n_slots; i++) {
        if (run->slots[i].msg_id == 0) {
            run->slots[i] = (bench_slot_t){ .msg_id = msg_id, .t_send_us = t_send_us };
            run->outstanding++;
            return;
        }
    }
    // Unreachable: the caller only publishes while outstanding < n_slots
}

static void drain_acks(bench_run_t *run)
{
    bench_ack_t ack;
    while (xQueueReceive(s_ack_queue, &ack, 0) == pdTRUE) {
        handle_ack(run, &ack);
    }
}

// Blocks until one ack arrives. Returns NULL on success or the abort reason.
static const char *wait_ack(bench_run_t *run)
{
    int64_t deadline = esp_timer_get_time() + ACK_TIMEOUT_US;
    bench_ack_t ack;

    for (;;) {
        if (atomic_load(&s_stop)) {
            return "stopped";
        }
        if (!atomic_load(&s_connected)) {
            return "disconnected";
        }
        if (xQueueReceive(s_ack_queue, &ack, pdMS_TO_TICKS(100)) == pdTRUE) {
            handle_ack(run, &ack);
            return NULL;
        }
        if (esp_timer_get_time() > deadline) {
            return "ack_timeout";
        }
    }
}

// Blocks until the first clock edge. Returns NULL on success or the abort reason.
static const char *wait_clock_start(void)
{
    // Polled every FreeRTOS tick (1 ms): every message carries its own tx_tick,
    // so the polling delay does not enter the measurements
    while (bench_clock_now() == 0) {
        if (atomic_load(&s_stop)) {
            return "stopped";
        }
        if (!atomic_load(&s_connected)) {
            return "disconnected";
        }
        vTaskDelay(1);
    }
    return NULL;
}

static void run_test(const bench_params_t *p)
{
    // Clearing the counter while edges arrive would put this board out of step
    // with the subscriber
    if (bench_clock_running()) {
        bench_status("error reason=clock_running");
        return;
    }

    uint8_t *payload = malloc(p->size);
    if (!payload) {
        bench_status("error reason=no_mem");
        return;
    }
    for (uint32_t i = 0; i < p->size; i++) {
        payload[i] = 'a' + i % 26;
    }

    atomic_store(&s_stop, false);
    uint32_t run_id = ++s_run_id;

    bench_clock_reset();
    bench_status("armed run=%" PRIu32 " size=%" PRIu32 " count=%" PRIu32 " qos=%" PRIu32
                 " inflight=%" PRIu32 " rate=%" PRIu32,
                 run_id, p->size, p->count, p->qos, p->inflight, p->rate);

    const char *reason = wait_clock_start();
    if (reason) {
        free(payload);
        bench_status("error run=%" PRIu32 " reason=%s sent=0 acked=0", run_id, reason);
        return;
    }

    // Acks of an aborted run may have arrived while waiting
    s_run = (bench_run_t){ .n_slots = p->inflight, .lat_min_us = INT64_MAX };
    xQueueReset(s_ack_queue);

    uint32_t tick_start = bench_clock_now();
    bench_status("start run=%" PRIu32 " tick=%" PRIu32, run_id, tick_start);

    const int64_t tick_us = (int64_t)portTICK_PERIOD_MS * 1000;
    uint32_t sent = 0;
    char hdr[BENCH_HDR_MAX_LEN + 1];
    int64_t t_start = esp_timer_get_time();

    while (sent < p->count) {
        if (atomic_load(&s_stop)) {
            reason = "stopped";
            break;
        }
        if (!atomic_load(&s_connected)) {
            reason = "disconnected";
            break;
        }

        drain_acks(&s_run);
        if (s_run.outstanding >= p->inflight) {
            reason = wait_ack(&s_run);
            if (reason) {
                break;
            }
            continue;
        }

        if (p->rate) {
            // Absolute schedule: sub-tick remainders even out over the run
            int64_t due = t_start + (int64_t)sent * 1000000 / p->rate;
            int64_t ahead = due - esp_timer_get_time();
            if (ahead >= tick_us) {
                vTaskDelay(ahead / tick_us);
            }
        }

        // "<run>:<seq>:<tx_tick>|" at the start of the payload, readable in Wireshark's bytes pane
        uint32_t tx_tick = bench_clock_now();
        int n = snprintf(hdr, sizeof(hdr), BENCH_HDR_FMT, run_id, sent, tx_tick);
        memcpy(payload, hdr, MIN((uint32_t)n, p->size));

        int64_t t_send = esp_timer_get_time();
        int msg_id = esp_mqtt_client_publish(s_client, BENCH_TOPIC_DATA, (const char *)payload,
                                             p->size, p->qos, 0);
        if (msg_id <= 0) {
            reason = "publish_failed";
            break;
        }
        track_sent(&s_run, msg_id, t_send);
        sent++;
    }

    while (!reason && s_run.outstanding > 0) {
        reason = wait_ack(&s_run);
    }
    int64_t elapsed_us = esp_timer_get_time() - t_start;
    uint32_t tick_end = bench_clock_now();
    free(payload);

    if (reason) {
        // Unacked messages stay in the esp-mqtt outbox and may still be retransmitted
        bench_status("error run=%" PRIu32 " reason=%s sent=%" PRIu32 " acked=%" PRIu32,
                     run_id, reason, sent, s_run.acked);
        return;
    }

    uint64_t payload_bps = (uint64_t)s_run.acked * p->size * 1000000 / MAX(elapsed_us, 1);
    bench_status("done run=%" PRIu32 " sent=%" PRIu32 " acked=%" PRIu32 " elapsed_us=%" PRId64
                 " payload_Bps=%" PRIu64 " ack_min_us=%" PRId64 " ack_avg_us=%" PRId64
                 " ack_max_us=%" PRId64 " tick_start=%" PRIu32 " tick_end=%" PRIu32
                 " heap_min_ever=%" PRIu32,
                 run_id, sent, s_run.acked, elapsed_us, payload_bps, s_run.lat_min_us,
                 s_run.lat_sum_us / s_run.acked, s_run.lat_max_us, tick_start, tick_end,
                 esp_get_minimum_free_heap_size());
}

static void bench_task(void *arg)
{
    bench_cmd_t cmd;
    bench_params_t params;
    char err[64];

    for (;;) {
        xQueueReceive(s_cmd_queue, &cmd, portMAX_DELAY);
        if (!parse_params(cmd.text, &params, err, sizeof(err))) {
            bench_status("error reason=%s", err);
            continue;
        }
        run_test(&params);
    }
}

static void submit_command(const char *data, int len)
{
    if (len <= 0 || len >= CMD_MAX_LEN) {
        ESP_LOGW(TAG, "command length %d ignored", len);
        return;
    }

    bench_cmd_t cmd;
    memcpy(cmd.text, data, len);
    cmd.text[len] = '\0';

    // A running or armed test only polls flags, so "stop" bypasses the command queue
    if (strncmp(cmd.text, "stop", 4) == 0) {
        atomic_store(&s_stop, true);
        return;
    }
    if (xQueueSend(s_cmd_queue, &cmd, 0) != pdTRUE) {
        ESP_LOGW(TAG, "command queue full, dropped");
    }
}

static void on_ack(int msg_id)
{
    // Matched to sent messages in the bench task: an ack may arrive before
    // the task has recorded the msg_id returned by esp_mqtt_client_publish()
    bench_ack_t ack = { .msg_id = msg_id, .t_us = esp_timer_get_time() };
    if (xQueueSend(s_ack_queue, &ack, 0) != pdTRUE) {
        ESP_LOGW(TAG, "ack queue full, msg_id=%d lost", msg_id);
    }
}

static void mqtt_event_handler(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    esp_mqtt_event_handle_t ev = data;

    switch ((esp_mqtt_event_id_t)id) {
    case MQTT_EVENT_CONNECTED:
        ESP_LOGI(TAG, "MQTT connected");
        esp_mqtt_client_subscribe(ev->client, BENCH_TOPIC_CTRL, 1);
        atomic_store(&s_connected, true);
        break;
    case MQTT_EVENT_DISCONNECTED:
        ESP_LOGW(TAG, "MQTT disconnected");
        atomic_store(&s_connected, false);
        break;
    case MQTT_EVENT_PUBLISHED:
        // PUBACK for QoS 1, PUBCOMP for QoS 2
        on_ack(ev->msg_id);
        break;
    case MQTT_EVENT_DATA:
        // Commands are short; ignore fragmented deliveries (topic is only set on the first one)
        if (ev->current_data_offset == 0 && ev->data_len == ev->total_data_len &&
            ev->topic_len == (int)strlen(BENCH_TOPIC_CTRL) &&
            memcmp(ev->topic, BENCH_TOPIC_CTRL, ev->topic_len) == 0) {
            submit_command(ev->data, ev->data_len);
        }
        break;
    case MQTT_EVENT_ERROR:
        ESP_LOGE(TAG, "MQTT error, type=%d", ev->error_handle->error_type);
        break;
    default:
        break;
    }
}

void bench_pub_start(esp_mqtt_client_handle_t client)
{
    s_client = client;
    bench_common_init(client, "pub");
    s_cmd_queue = xQueueCreate(CMD_QUEUE_LEN, sizeof(bench_cmd_t));
    s_ack_queue = xQueueCreate(ACK_QUEUE_LEN, sizeof(bench_ack_t));
    configASSERT(s_cmd_queue && s_ack_queue);

    // Keep the publishing loop off core 0, where Wi-Fi and lwIP run
    BaseType_t core = portNUM_PROCESSORS > 1 ? 1 : tskNO_AFFINITY;
    xTaskCreatePinnedToCore(bench_task, "pub", BENCH_TASK_STACK, NULL, BENCH_TASK_PRIO, NULL, core);
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL));
}
