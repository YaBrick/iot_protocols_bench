set(requires bench_wifi bench_clock esp_event esp_timer)

# IDF 6.x ships esp-mqtt via the Component Registry (see idf_component.yml),
# the component manager adds it to REQUIRES on its own.
if(IDF_VERSION_MAJOR LESS 6)
    list(APPEND requires mqtt)
endif()

# Both roles are always compiled, main.c starts the one selected in Kconfig
idf_component_register(SRCS "main.c" "bench_common.c" "pub.c" "sub.c"
                       INCLUDE_DIRS "."
                       REQUIRES ${requires})
