// Pieces shared by the publisher and the subscriber: command parsing, payload
// header parsing and status lines.

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "esp_log.h"
#include "bench.h"

static esp_mqtt_client_handle_t s_client;
static const char *s_role = "bench";

void bench_common_init(esp_mqtt_client_handle_t client, const char *role)
{
    s_client = client;
    s_role = role;
}

void bench_status(const char *fmt, ...)
{
    char buf[512];
    int n = snprintf(buf, sizeof(buf), "%s ", s_role);
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf + n, sizeof(buf) - n, fmt, ap);
    va_end(ap);

    ESP_LOGI(s_role, "%s", buf);
    // QoS 0, so status lines add no PUBACKs to the capture
    esp_mqtt_client_publish(s_client, BENCH_TOPIC_STATUS, buf, 0, 0, 0);
}

bool bench_parse_params(char *text, bench_params_t *p, char *err, size_t err_len)
{
    *p = (bench_params_t){ .size = 256, .count = 1000, .qos = 1, .inflight = 8, .rate = 0 };

    char *save = NULL;
    for (char *tok = strtok_r(text, " \t\r\n", &save); tok; tok = strtok_r(NULL, " \t\r\n", &save)) {
        char *eq = strchr(tok, '=');
        if (!eq) {
            snprintf(err, err_len, "bad_token:%s", tok);
            return false;
        }
        *eq = '\0';
        char *end;
        unsigned long v = strtoul(eq + 1, &end, 10);
        if (end == eq + 1 || *end != '\0') {
            snprintf(err, err_len, "bad_value:%s", tok);
            return false;
        }

        if (!strcmp(tok, "size")) {
            p->size = v;
        } else if (!strcmp(tok, "count")) {
            p->count = v;
        } else if (!strcmp(tok, "qos")) {
            p->qos = v;
        } else if (!strcmp(tok, "inflight")) {
            p->inflight = v;
        } else if (!strcmp(tok, "rate")) {
            p->rate = v;
        } else {
            snprintf(err, err_len, "unknown_key:%s", tok);
            return false;
        }
    }

    // The payload must hold the "<run>:<seq>:<tx_tick>|" header
    if (p->size < BENCH_HDR_MAX_LEN || p->size > CONFIG_BENCH_MAX_PAYLOAD) {
        snprintf(err, err_len, "size_out_of_range:%d..%d", BENCH_HDR_MAX_LEN, CONFIG_BENCH_MAX_PAYLOAD);
        return false;
    }
    if (p->count < 1) {
        snprintf(err, err_len, "count_out_of_range");
        return false;
    }
    if (p->qos > 2) {
        snprintf(err, err_len, "qos_out_of_range:0..2");
        return false;
    }
    if (p->inflight < 1 || p->inflight > CONFIG_BENCH_MAX_INFLIGHT) {
        snprintf(err, err_len, "inflight_out_of_range:1..%d", CONFIG_BENCH_MAX_INFLIGHT);
        return false;
    }
    return true;
}

bool bench_parse_header(const char *data, int len, bench_hdr_t *hdr)
{
    char buf[BENCH_HDR_MAX_LEN + 1];
    int n = len < BENCH_HDR_MAX_LEN ? len : BENCH_HDR_MAX_LEN;
    memcpy(buf, data, n);
    buf[n] = '\0';

    static const char seps[3] = { ':', ':', '|' };
    uint32_t v[3];
    char *p = buf;
    for (int i = 0; i < 3; i++) {
        if (*p < '0' || *p > '9') {
            return false;
        }
        char *end;
        v[i] = strtoul(p, &end, 10);
        if (*end != seps[i]) {
            return false;
        }
        p = end + 1;
    }
    *hdr = (bench_hdr_t){ .run = v[0], .seq = v[1], .tx_tick = v[2] };
    return true;
}
