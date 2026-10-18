cmake_minimum_required(VERSION 3.16)

# Shared code (mic, radio, packet format, serial link) lives in ../components.
set(EXTRA_COMPONENT_DIRS "${CMAKE_CURRENT_LIST_DIR}/../components")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(aircraft_noise_bridge)
