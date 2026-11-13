# The PCNT driver became its own component in IDF 5.3
if(IDF_VERSION_MAJOR EQUAL 5 AND IDF_VERSION_MINOR LESS 3)
    set(priv_requires driver)
else()
    set(priv_requires esp_driver_pcnt esp_driver_gpio)
endif()

idf_component_register(SRCS "bench_clock.c"
                       INCLUDE_DIRS "include"
                       PRIV_REQUIRES ${priv_requires})
