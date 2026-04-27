# `csi_recv` vs `csi_sender_main` 함수 1:1 매핑 비교

비교 대상:
- 예제 수신 코드: `examples/get-started/csi_recv/main/app_main.c`
- 우리 수신/전송 코드: `esp32s3_csi_sender/main/csi_sender_main.c`

목적:
- 두 코드가 어느 부분에서 직접 대응되는지
- 어느 부분이 우리 코드에서 확장되었는지
- 어떤 기능이 한쪽에만 있는지
를 함수 기준으로 빠르게 파악하기 위함

---

## 1) 함수 매핑 표

| 예제 `csi_recv` 함수 | 우리 `csi_sender_main` 함수 | 매핑 수준 | 설명 |
|---|---|---|---|
| `wifi_init()` | `init_wifi_sta()` | 부분 대응 | 둘 다 Wi-Fi STA 초기화/시작을 담당. 예제는 ESP-NOW 채널/대역 설정에 초점, 우리 코드는 AP 접속 후 Mac UDP 경로 확보에 초점. |
| `wifi_esp_now_init(esp_now_peer_info_t)` | (직접 대응 없음) | 없음 | 예제는 ESP-NOW 기반 송수신 링크를 구성. 우리 코드는 ESP-NOW를 쓰지 않고 일반 UDP/IP 전송 사용. |
| `wifi_csi_init()` | `init_csi()` | 직접 대응(기능 확장) | 둘 다 CSI 수집 설정 + 콜백 등록 + 활성화. 우리 코드는 이후 UDP 전송 파이프라인으로 연결되도록 구성. |
| `wifi_csi_rx_cb(void*, wifi_csi_info_t*)` | `wifi_csi_cb(void*, wifi_csi_info_t*)` | 직접 대응(역할 변경) | 둘 다 CSI 콜백 진입점. 예제는 콘솔 CSV 출력, 우리 코드는 `send_csi_packet()` 호출로 네트워크 전송. |
| (없음) | `to_amplitude(int8_t, int8_t)` | 우리 코드만 있음 | raw I/Q를 amplitude로 변환하는 함수. |
| (없음) | `extract_amp_from_csi(...)` | 우리 코드만 있음 | CSI 버퍼에서 amplitude 배열 추출. |
| (없음) | `moving_average_3tap(...)` | 우리 코드만 있음 | 온디바이스 1차 평활화. |
| (없음) | `zscore_inplace(...)` | 우리 코드만 있음 | 프레임 단위 정규화. |
| (없음) | `clip_outlier_inplace(...)` | 우리 코드만 있음 | 이상치 클리핑. |
| (없음) | `send_csi_packet(...)` | 우리 코드만 있음 | 헤더+payload 패킹 후 UDP `sendto()` 수행. |
| (없음) | `resolve_device_id()` | 우리 코드만 있음 | 고정/자동(MAC 기반) 장치 ID 결정. |
| (없음) | `init_udp_sender()` | 우리 코드만 있음 | Mac 수집기 목적지 소켓/주소 초기화. |
| `app_main()` | `app_main()` | 직접 대응(구성 차이 큼) | 둘 다 엔트리포인트. 예제는 NVS->WiFi->ESP-NOW->CSI, 우리 코드는 NVS->ID결정->WiFi->UDP->CSI 순으로 운영 파이프라인 초기화. |

---

## 2) 호출 흐름 매핑

### 예제 `csi_recv` 흐름

1. `app_main()`  
2. `wifi_init()`  
3. `wifi_esp_now_init(...)`  
4. `wifi_csi_init()`  
5. CSI 수신 시 `wifi_csi_rx_cb()` -> 콘솔 출력

### 우리 `csi_sender_main` 흐름

1. `app_main()`  
2. `resolve_device_id()`  
3. `init_wifi_sta()`  
4. `init_udp_sender()`  
5. `init_csi()`  
6. CSI 수신 시 `wifi_csi_cb()` -> `send_csi_packet()`  
7. `send_csi_packet()` 내부에서
   - `extract_amp_from_csi()`  
   - `moving_average_3tap()`  
   - `zscore_inplace()`  
   - `clip_outlier_inplace()`  
   - UDP 전송

---

## 3) 핵심 차이 요약

- 예제는 **CSI 취득 데모** 목적이라 `wifi_csi_rx_cb()`에서 바로 텍스트 출력에 집중
- 우리 코드는 **운영형 수집 노드** 목적이라 콜백 이후
  - 전처리
  - 패킷 스키마화
  - 네트워크 전송
  을 수행
- 따라서 함수 수가 늘어난 이유는 “불필요한 복잡화”가 아니라 **운영 파이프라인 연결을 위한 필수 확장**입니다.
