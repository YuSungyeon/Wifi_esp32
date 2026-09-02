cmake_minimum_required(VERSION 3.16)
include($ENV{IDF_PATH}/tools/cmake/project.cmake)

# 수집 경로 선택.
#   0 = USB-Serial-JTAG 로 스트리밍 (학습 데이터 수집 — 정본 경로)
#   1 = ESP-NOW 업링크로 SINK 에 전송 (무선 배치 — USB 출력 없음)
set(CSI_UPLINK_ENABLED "0" CACHE STRING "1: CSI를 ESP-NOW로 sink에 업링크")
# 업링크 모드에서 이 RX 의 MAC 마지막 바이트. 1~254, 보드마다 달라야 한다.
set(CSI_RX_ID "1" CACHE STRING "업링크 RX 식별자 (MAC 마지막 바이트)")
# 업링크 송신 시점 분산. 프레임 캡처 시각 + rx_id × 이 값(ms) 뒤에 보낸다. 0 이면 즉시.
# RX 여러 대가 같은 TX 자극 직후 동시에 쏘면 싱크에서 먼 쪽이 ~7% 를 잃는다 (troubleshooting/08).
set(CSI_UPLINK_OFFSET_MS "0" CACHE STRING "업링크 송신 오프셋 ms (× rx_id)")

add_compile_definitions(
    CSI_UPLINK_ENABLED=${CSI_UPLINK_ENABLED}
    CSI_RX_ID=${CSI_RX_ID}
    CSI_UPLINK_OFFSET_MS=${CSI_UPLINK_OFFSET_MS}
)

project(esp32s3_csi_recv_poc)
