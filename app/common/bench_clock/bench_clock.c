// Reference clock input: the PCNT peripheral counts rising edges in hardware,
// so 100 kHz costs no CPU time and no interrupts except one per overflow.

#include "soc/soc_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_check.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "driver/pulse_cnt.h"
#include "bench_clock.h"

#if !SOC_PCNT_SUPPORTED
#error "bench_clock needs the PCNT peripheral, which this target lacks (e.g. ESP32-C3)"
#endif

static const char *TAG = "bench_clock";

// The hardware counter is 16 bits. With accum_count the driver adds HIGH_LIMIT
// to a software total every time the counter wraps there, and compensates a
// wrap whose interrupt is still pending when the count is read.
#define HIGH_LIMIT 30000

#define RUNNING_WINDOW_MS 5

static pcnt_unit_handle_t s_unit;

esp_err_t bench_clock_init(void)
{
    const pcnt_unit_config_t unit_cfg = {
        .low_limit = -1,  // never reached: the channel only counts up
        .high_limit = HIGH_LIMIT,
        .flags.accum_count = 1,
    };
    ESP_RETURN_ON_ERROR(pcnt_new_unit(&unit_cfg, &s_unit), TAG, "pcnt unit");

    const pcnt_chan_config_t chan_cfg = {
        .edge_gpio_num = CONFIG_BENCH_CLOCK_GPIO,
        .level_gpio_num = -1,
    };
    pcnt_channel_handle_t chan;
    ESP_RETURN_ON_ERROR(pcnt_new_channel(s_unit, &chan_cfg, &chan), TAG, "pcnt channel");
    // One tick per period: count rising edges, ignore falling ones
    ESP_RETURN_ON_ERROR(pcnt_channel_set_edge_action(chan, PCNT_CHANNEL_EDGE_ACTION_INCREASE,
                                                     PCNT_CHANNEL_EDGE_ACTION_HOLD), TAG, "edge action");
    ESP_RETURN_ON_ERROR(pcnt_channel_set_level_action(chan, PCNT_CHANNEL_LEVEL_ACTION_KEEP,
                                                      PCNT_CHANNEL_LEVEL_ACTION_KEEP), TAG, "level action");

    // Holds the line low while the RPi pin is not driving it (before the PWM is
    // configured, or with the wire unplugged), so the counter does not pick up noise
    ESP_RETURN_ON_ERROR(gpio_set_pull_mode(CONFIG_BENCH_CLOCK_GPIO, GPIO_PULLDOWN_ONLY), TAG, "pull-down");

#if CONFIG_BENCH_CLOCK_GLITCH_NS > 0
    const pcnt_glitch_filter_config_t filter_cfg = { .max_glitch_ns = CONFIG_BENCH_CLOCK_GLITCH_NS };
    ESP_RETURN_ON_ERROR(pcnt_unit_set_glitch_filter(s_unit, &filter_cfg), TAG, "glitch filter");
#endif

    // accum_count only works with the limit registered as a watch point
    ESP_RETURN_ON_ERROR(pcnt_unit_add_watch_point(s_unit, HIGH_LIMIT), TAG, "watch point");
    ESP_RETURN_ON_ERROR(pcnt_unit_enable(s_unit), TAG, "enable");
    ESP_RETURN_ON_ERROR(pcnt_unit_clear_count(s_unit), TAG, "clear");
    ESP_RETURN_ON_ERROR(pcnt_unit_start(s_unit), TAG, "start");

    ESP_LOGI(TAG, "counting rising edges on GPIO%d, %d Hz expected", CONFIG_BENCH_CLOCK_GPIO,
             CONFIG_BENCH_CLOCK_HZ);
    return ESP_OK;
}

uint32_t bench_clock_now(void)
{
    int value = 0;
    pcnt_unit_get_count(s_unit, &value);
    return (uint32_t)value;
}

void bench_clock_reset(void)
{
    pcnt_unit_clear_count(s_unit);
}

bool bench_clock_running(void)
{
    uint32_t before = bench_clock_now();
    TickType_t window = pdMS_TO_TICKS(RUNNING_WINDOW_MS);
    vTaskDelay(window > 0 ? window : 1);
    return bench_clock_now() != before;
}
