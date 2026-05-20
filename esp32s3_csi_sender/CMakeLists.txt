cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

set(CSI_WIFI_SSID "MeshSense_TX_AP" CACHE STRING "Wi-Fi SSID")
set(CSI_WIFI_PASS "mstx1234" CACHE STRING "Wi-Fi password")
set(CSI_COLLECTOR_IP "192.168.4.2" CACHE STRING "Mac collector IPv4")
set(CSI_COLLECTOR_PORT "9999" CACHE STRING "Mac collector UDP port")
set(CSI_DEVICE_ID "101" CACHE STRING "Device ID")

add_compile_definitions(
    WIFI_SSID=\"${CSI_WIFI_SSID}\"
    WIFI_PASS=\"${CSI_WIFI_PASS}\"
    COLLECTOR_IP=\"${CSI_COLLECTOR_IP}\"
    COLLECTOR_PORT=${CSI_COLLECTOR_PORT}
    DEVICE_ID=${CSI_DEVICE_ID}
)

project(esp32s3_csi_sender)
