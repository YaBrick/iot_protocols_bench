#pragma once

#include "esp_netif.h"

// Initializes NVS, netif and the default event loop, connects in STA mode with
// power save off and blocks until an IP is obtained. Reconnects on its own.
esp_netif_t *bench_wifi_connect(void);
