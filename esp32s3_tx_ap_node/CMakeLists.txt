cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

set(TX_AP_SSID "MeshSense_TX_AP" CACHE STRING "SoftAP SSID")
set(TX_AP_PASS "meshsensetx123" CACHE STRING "SoftAP password")
set(TX_AP_CHANNEL "6" CACHE STRING "SoftAP channel")
set(TX_AP_MAX_CONN "4" CACHE STRING "SoftAP max stations")
set(TX_AP_BROADCAST_PORT "3333" CACHE STRING "UDP broadcast port")
set(TX_AP_INTERVAL_MS "10" CACHE STRING "UDP broadcast interval")
set(TX_AP_PAYLOAD_BYTES "64" CACHE STRING "UDP payload bytes")
set(TX_AP_SESSION_ID "1" CACHE STRING "Session ID")
set(TX_AP_NODE_ID "1" CACHE STRING "TX/AP node ID")

add_compile_definitions(
    TX_AP_SSID=\"${TX_AP_SSID}\"
    TX_AP_PASS=\"${TX_AP_PASS}\"
    TX_AP_CHANNEL=${TX_AP_CHANNEL}
    TX_AP_MAX_CONN=${TX_AP_MAX_CONN}
    TX_AP_BROADCAST_PORT=${TX_AP_BROADCAST_PORT}
    TX_AP_INTERVAL_MS=${TX_AP_INTERVAL_MS}
    TX_AP_PAYLOAD_BYTES=${TX_AP_PAYLOAD_BYTES}
    TX_AP_SESSION_ID=${TX_AP_SESSION_ID}
    TX_AP_NODE_ID=${TX_AP_NODE_ID}
)

project(esp32s3_tx_ap_node)
