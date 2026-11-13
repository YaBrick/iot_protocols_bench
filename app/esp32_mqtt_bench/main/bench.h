#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <inttypes.h>
#include "mqtt_client.h"

#define BENCH_TOPIC_CTRL    "bench/ctrl"
#define BENCH_TOPIC_STATUS  "bench/status"
#define BENCH_TOPIC_DATA    "bench/data"
#define BENCH_TOPIC_SUB_LOG "bench/sub/log"

// Every bench/data payload starts with "<run>:<seq>:<tx_tick>|", for example
// "3:00000042:0000123456|", and is padded with 'a'..'z' to the full size.
// tx_tick is the publisher's reference clock tick right before the publish.
#define BENCH_HDR_FMT "%" PRIu32 ":%08" PRIu32 ":%010" PRIu32 "|"
// Longest possible header (10 + 1 + 10 + 1 + 10 + 1), also the smallest allowed size
#define BENCH_HDR_MAX_LEN 33

typedef struct {
    uint32_t size;
    uint32_t count;
    uint32_t qos;
    uint32_t inflight;
    uint32_t rate;
} bench_params_t;

typedef struct {
    uint32_t run;
    uint32_t seq;
    uint32_t tx_tick;
} bench_hdr_t;

// role is the prefix of every status line: "pub" or "sub"
void bench_common_init(esp_mqtt_client_handle_t client, const char *role);

// Publishes "<role> <text>" to bench/status with QoS 0 and logs it
void bench_status(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

// Parses a bench/ctrl command (modified in place). Both roles parse the same
// command with the same rules, so they accept or reject it alike.
bool bench_parse_params(char *text, bench_params_t *p, char *err, size_t err_len);

bool bench_parse_header(const char *data, int len, bench_hdr_t *hdr);

// Create the role's task and register its MQTT event handler.
// Call before esp_mqtt_client_start().
void bench_pub_start(esp_mqtt_client_handle_t client);
void bench_sub_start(esp_mqtt_client_handle_t client);
