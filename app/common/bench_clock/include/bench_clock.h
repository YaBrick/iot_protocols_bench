#pragma once

// Shared timebase for several boards: each one counts the rising edges of the
// same external clock (the RPi), so equal tick values mean the same instant on
// every board, with no drift between their crystals.
//
// 1 tick = 1 / CONFIG_BENCH_CLOCK_HZ (10 us at 100 kHz). The count is 32 bits
// wide: about 6 hours at 100 kHz before it wraps.

#include <stdbool.h>
#include <stdint.h>
#include "esp_err.h"
#include "sdkconfig.h"

esp_err_t bench_clock_init(void);

// Ticks since the last bench_clock_reset(). Stays 0 until the clock starts.
uint32_t bench_clock_now(void);

// Zeroes the counter. Only meaningful while the clock is stopped: then every
// board starts from the same edge.
void bench_clock_reset(void);

// Watches the input for ~5 ms and reports whether edges are arriving.
bool bench_clock_running(void);

static inline uint64_t bench_clock_ticks_to_us(uint64_t ticks)
{
    return ticks * 1000000ULL / CONFIG_BENCH_CLOCK_HZ;
}
