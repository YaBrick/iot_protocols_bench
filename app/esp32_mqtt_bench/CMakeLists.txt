cmake_minimum_required(VERSION 3.16)

# One project, two firmwares: the board role (publisher or subscriber) comes
# from Kconfig, the CMake presets in CMakePresets.json select it. See README.md.

# Wi-Fi setup shared with the other benches, so all run under identical conditions;
# reference clock input shared by both roles
set(EXTRA_COMPONENT_DIRS
    ${CMAKE_CURRENT_LIST_DIR}/../common/bench_wifi
    ${CMAKE_CURRENT_LIST_DIR}/../common/bench_clock)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32_mqtt_bench)
