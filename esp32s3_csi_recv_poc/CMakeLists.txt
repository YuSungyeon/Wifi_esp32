cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# 수집 경로 선택.
#   0 = USB-Serial-JTAG 로 스트리밍 (학습 데이터 수집 — 정본 경로)
#   1 = ESP-NOW 업링크로 SINK 에 전송 (무선 배치 — USB 출력 없음)
set(CSI_UPLINK_ENABLED "0" CACHE STRING "1: CSI를 ESP-NOW로 sink에 업링크")
# 업링크 모드에서 이 RX 의 MAC 마지막 바이트. 1~254, 보드마다 달라야 한다.
set(CSI_RX_ID "1" CACHE STRING "업링크 RX 식별자 (MAC 마지막 바이트)")

add_compile_definitions(
    CSI_UPLINK_ENABLED=${CSI_UPLINK_ENABLED}
    CSI_RX_ID=${CSI_RX_ID}
)

project(esp32s3_csi_recv_poc)
