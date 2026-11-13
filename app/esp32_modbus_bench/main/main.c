// Modbus TCP server ("sensor") for the throughput bench. The RPi client drives
// the test: it polls the holding registers with FC03 at the size, rate and
// in-flight window it chooses, so the firmware has no test logic of its own.
//
// Holding registers 0..BENCH_DATA_REGS-1: payload, reads as "abcd..." on the wire.
// Input registers (FC04), 32-bit values high word first:
//   0-1 uptime, s    2-3 FC03 requests served    4-5 registers returned by FC03
//   6-7 free heap    8-9 min free heap ever      10  BENCH_DATA_REGS

#include <stdint.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_system.h"
#include "mbcontroller.h"
#include "bench_wifi.h"

static const char *TAG = "main";

#define STATS_PERIOD_MS  100
#define STATS_TASK_STACK 3072
#define STATS_TASK_PRIO  5

enum {
    STAT_UPTIME_S  = 0,
    STAT_READ_REQS = 2,
    STAT_READ_REGS = 4,
    STAT_FREE_HEAP = 6,
    STAT_MIN_HEAP  = 8,
    STAT_DATA_REGS = 10,
    STAT_REGS      = 11,
};

static uint16_t s_data[CONFIG_BENCH_DATA_REGS];
static uint16_t s_stats[STAT_REGS];
static void *s_mb;

static void put_u32(uint16_t *regs, int idx, uint32_t v)
{
    regs[idx] = v >> 16;
    regs[idx + 1] = v & 0xFFFF;
}

// esp-modbus posts a notification for every register access and, once its
// queue is full, blocks the Modbus task for up to 10 ticks per request.
// Draining the queue keeps that out of the measurement and doubles as a
// server-side request counter.
static void stats_task(void *arg)
{
    uint32_t read_reqs = 0;
    uint32_t read_regs = 0;
    int64_t last_update_us = 0;
    mb_param_info_t info;

    for (;;) {
        if (mbc_slave_get_param_info(s_mb, &info, STATS_PERIOD_MS) == ESP_OK &&
            info.type == MB_EVENT_HOLDING_REG_RD) {
            read_reqs++;
            read_regs += info.size;
        }

        int64_t now_us = esp_timer_get_time();
        if (now_us - last_update_us < STATS_PERIOD_MS * 1000) {
            continue;
        }
        last_update_us = now_us;

        mbc_slave_lock(s_mb);
        put_u32(s_stats, STAT_UPTIME_S, now_us / 1000000);
        put_u32(s_stats, STAT_READ_REQS, read_reqs);
        put_u32(s_stats, STAT_READ_REGS, read_regs);
        put_u32(s_stats, STAT_FREE_HEAP, esp_get_free_heap_size());
        put_u32(s_stats, STAT_MIN_HEAP, esp_get_minimum_free_heap_size());
        mbc_slave_unlock(s_mb);
    }
}

void app_main(void)
{
    esp_netif_t *netif = bench_wifi_connect();

    // esp-modbus logs a warning for every request that arrives before the
    // previous response is sent, i.e. for every pipelined one (inflight > 1)
    esp_log_level_set("mb_port.tcp.slave", ESP_LOG_ERROR);

    for (int i = 0; i < CONFIG_BENCH_DATA_REGS; i++) {
        s_data[i] = (('a' + (2 * i) % 26) << 8) | ('a' + (2 * i + 1) % 26);
    }
    s_stats[STAT_DATA_REGS] = CONFIG_BENCH_DATA_REGS;

    mb_communication_info_t comm = {
        .tcp_opts = {
            .mode = MB_TCP,
            .port = CONFIG_BENCH_MB_PORT,
            .uid = CONFIG_BENCH_MB_UID,
            .addr_type = MB_IPV4,
            .ip_addr_table = NULL,
            .ip_netif_ptr = netif,
        },
    };
    ESP_ERROR_CHECK(mbc_slave_create_tcp(&comm, &s_mb));

    const mb_register_area_descriptor_t data_area = {
        .type = MB_PARAM_HOLDING,
        .start_offset = 0,
        .access = MB_ACCESS_RO,
        .address = s_data,
        .size = sizeof(s_data),
    };
    ESP_ERROR_CHECK(mbc_slave_set_descriptor(s_mb, data_area));

    const mb_register_area_descriptor_t stats_area = {
        .type = MB_PARAM_INPUT,
        .start_offset = 0,
        .access = MB_ACCESS_RO,
        .address = s_stats,
        .size = sizeof(s_stats),
    };
    ESP_ERROR_CHECK(mbc_slave_set_descriptor(s_mb, stats_area));

    ESP_ERROR_CHECK(mbc_slave_start(s_mb));
    xTaskCreate(stats_task, "mb_stats", STATS_TASK_STACK, NULL, STATS_TASK_PRIO, NULL);

    ESP_LOGI(TAG, "Modbus TCP server on port %d, unit %d, %d data registers",
             CONFIG_BENCH_MB_PORT, CONFIG_BENCH_MB_UID, CONFIG_BENCH_DATA_REGS);
}
