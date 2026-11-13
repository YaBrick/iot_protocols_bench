cmake_minimum_required(VERSION 3.16)

# Wi-Fi setup shared with the other benches, so all run under identical conditions
set(EXTRA_COMPONENT_DIRS ${CMAKE_CURRENT_LIST_DIR}/../common/bench_wifi)

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(esp32_modbus_bench)
