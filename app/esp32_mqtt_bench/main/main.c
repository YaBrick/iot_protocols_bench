#include "esp_err.h"
#include "mqtt_client.h"
#include "bench_wifi.h"
#include "bench_clock.h"
#include "bench.h"

void app_main(void)
{
    ESP_ERROR_CHECK(bench_clock_init());
    bench_wifi_connect();

    const esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = CONFIG_BENCH_BROKER_URI,
        .buffer.size = CONFIG_BENCH_MQTT_BUF_SIZE,
        .buffer.out_size = CONFIG_BENCH_MQTT_BUF_SIZE,
    };
    esp_mqtt_client_handle_t client = esp_mqtt_client_init(&mqtt_cfg);

#if CONFIG_BENCH_ROLE_SUB
    bench_sub_start(client);
#else
    bench_pub_start(client);
#endif
    ESP_ERROR_CHECK(esp_mqtt_client_start(client));
}
