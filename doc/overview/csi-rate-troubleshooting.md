# CSI 수집률(Hz) 트러블슈팅 기록 (2026-05-22)

100Hz 목표인데 RX 수집률이 17Hz 수준이라는 보고로 시작한 디버깅 세션 기록.
각 항목은 **문제 → 가설 → 시도 → 기대값 → 실제 결과 → 다음 조치**의 형식.
펌웨어 파일 경로:
- TX: [esp32s3_tx_ap_node/main/tx_ap_main.c](../../esp32s3_tx_ap_node/main/tx_ap_main.c)
- RX: [esp32s3_csi_sender/main/csi_sender_main.c](../../esp32s3_csi_sender/main/csi_sender_main.c)

## 0. 시작 상태 측정값

| 지표 | 값 |
|---|---|
| 평균 Hz | 17.4 |
| 패킷 간격 중앙값 | 12.1ms |
| 200ms+ 공백 비율 | 14.2% |

해석: 중앙값 12.1ms는 활성 구간에서 ~83Hz로 들어오고 있다는 뜻. 14.2%가 200ms+ 공백 → "100Hz로 짧은 burst, 그 후 0.2~0.5초 침묵"이 반복되는 패턴. 평균을 떨어뜨리는 주범은 공백이다.

## 1. AMPDU 비활성화

- **가설**: `CONFIG_ESP_WIFI_AMPDU_RX_ENABLED=y`가 CSI 프레임을 묶거나 누락시킴. CSI 측정용 펌웨어는 보통 AMPDU를 끔.
- **시도**: TX/RX 양쪽 sdkconfig에서 `CONFIG_ESP_WIFI_AMPDU_TX_ENABLED=n`, `CONFIG_ESP_WIFI_AMPDU_RX_ENABLED=n`.
- **기대값**: 200ms+ 공백이 줄고 Hz가 30~50대로 상승.
- **실제 결과**: hz=14.15, median_dt=30.3ms, gaps>200ms=10.9%. 미세하게 개선. 큰 변화는 아님.
- **다음 조치**: AMPDU는 끄는 게 맞지만 단독으로는 부족. 다른 변수 탐색.

## 2. TX UDP broadcast 비활성화 (가설 검증)

- **가설**: TX가 ESP-NOW(100Hz) + AP UDP broadcast(100Hz)를 동시에 발사하며 에어타임 경쟁. UDP broadcast가 DTIM 게이팅되어 burst로 송출되어 200ms 공백을 유발할 것.
- **시도**: TX `tx_broadcast_task`를 `xTaskCreate`에서 제거.
- **기대값**: ESP-NOW만 남아 에어타임 경쟁이 사라지고 Hz가 오를 것.
- **실제 결과**: hz=9.94, median_dt=37.4ms, gaps>200ms=24.1%. **더 나빠짐**.
- **해석**: UDP broadcast는 경쟁이 아니라 추가 CSI 자극원이었다. 끄니까 자극이 절반으로 감소. ESP-NOW broadcast 100Hz가 실제로는 ~10Hz 정도만 CSI 콜백을 트리거함을 시사 (대부분이 beacon 기여).
- **다음 조치**: 복원. 가설은 틀렸음.

## 3. 진단 카운터 추가

- **문제**: TX→RX 사이의 어느 단계에서 패킷이 사라지는지 알 수 없음.
- **시도**: 
  - TX: `g_enow_ok`, `g_enow_fail`, `esp_now_register_send_cb`로 `tx_done` 콜백 카운팅 (`g_enow_cb_ok`, `g_enow_cb_fail`). 500패킷마다 출력.
  - RX: `g_csi_cb_count`(CSI 콜백 호출수), `g_csi_throttle_drop`, `g_csi_sent`. 5초마다 출력.
- **기대값**: 어느 단계에서 손실이 발생하는지 명확화.
- **실제 결과**: TX api ok = +500/5초 = 정확히 100Hz, tx_done fail < 1%. **TX 측은 완벽**. RX cb = 36~69Hz 변동, throttle이 ~50% 추가 drop. **RX 콜백 자체가 절반밖에 안 일어남**.

## 4. HT20 강제 + channel_filter 비활성화 + throttle 제거

- **가설**: RX가 `channel 6, 40U` (HT40 upper)로 협상됨. HT40 secondary channel 일부 frame이 `channel_filter_en=true`에 의해 누락되거나, CSI 콜백 자체에서 빠짐.
- **시도**:
  - TX: `esp_wifi_set_bandwidth(WIFI_IF_AP, WIFI_BW_HT20)` 호출 (start 이전·이후 모두).
  - RX: `esp_wifi_set_bandwidth(WIFI_IF_STA, WIFI_BW_HT20)` 추가, `channel_filter_en = false`로 변경, `SEND_INTERVAL_US`를 1ms로 낮춰 throttle 사실상 비활성화.
- **기대값**: BW20 협상 성공, cb가 100Hz 근처로 상승.
- **실제 결과**: RX log `connected with MeshSense_TX_AP, aid = 2, channel 6, BW20` (HT20 협상 성공). cb 5초당 +900~3800 = **182~763Hz로 폭증**. JSONL hz=566.12, gaps>200ms=0.0%, seq_drop=170.
- **해석**: 폭증한 CSI 대부분이 우리 ESP-NOW 자극이 아니라 **주변 2.4GHz 네트워크의 frame**. `channel_filter_en=false` + promiscuous 조합으로 환경 노이즈가 다 잡힌 것. ML 학습 데이터로는 부적합.
- **다음 조치**: throttle 복원 + BSSID 필터 추가로 우리 AP만 통과시키기.

## 5. BSSID 필터 추가

- **시도**:
  - RX: `WIFI_EVENT_STA_CONNECTED`에서 AP BSSID 저장 (`g_ap_bssid`).
  - CSI 콜백에서 `info->mac`이 우리 AP BSSID와 일치하지 않으면 drop (`g_csi_filter_drop` 카운트).
  - `SEND_INTERVAL_US = 9000μs` (9ms throttle 복원).
- **기대값**: cb는 환경 잡힐지언정 sent는 우리 AP CSI만, 약 100Hz.
- **실제 결과**: cb 5초당 +30~150 = ~24Hz (이전 폭증과 모순). filter_drop이 cb의 ~80%. sent 0.2~6Hz로 폭락. JSONL hz=1.91.
- **해석**: 이전 566Hz는 그 시점의 일시적 환경 노이즈였고, 우리 AP frame은 본래 8~15Hz뿐이라는 진실이 드러남. TX 100Hz 송신 중 90%+가 RX CSI 콜백을 안 일으킴.

## 6. 진단용 `info->mac` 직접 로깅

- **문제**: BSSID 필터가 잘못된 필드를 보고 있을 가능성 vs. 진짜 우리 frame이 9Hz뿐일 가능성을 구분 필요.
- **시도**: CSI 콜백 처음 20개에 대해 `info->mac`, RSSI, channel, len 출력.
- **기대값**: 모두 우리 AP BSSID면 필터 OK + 본질 문제; 다른 MAC들이 섞였으면 필터 로직 자체 오류.
- **실제 결과**:
  - csi#0~19 중 12개가 우리 AP MAC `3c:0f:02:cf:7c:29` (RSSI −1~−5)
  - 4개는 옆 RX 보드 MAC `a2:6b:d4:fa:82:ea` (RSSI −34)
  - 3개는 외부 디바이스 `d2:c1:63:96:9b:cf` (RSSI −34)
- **결론**: `info->mac` = source MAC 확정. BSSID 필터 로직 OK. **진짜 본질 문제**: 우리 AP frame은 cb 트리거 빈도가 본래 ~9Hz뿐.

## 7. ESP-NOW broadcast OFDM rate 강제 (가설: DSSS가 원인)

- **가설**: ESP-NOW broadcast는 default가 1Mbps DSSS인데 ESP32 CSI는 OFDM L-LTF에서 추출됨. DSSS frame에서는 CSI 콜백이 트리거 안 됨.
- **시도**: TX에서 `esp_now_set_peer_rate_config(BROADCAST_MAC, {phymode=HT20, rate=MCS0_LGI})`로 OFDM 강제. 실패 시 `esp_wifi_config_espnow_rate(WIFI_IF_AP, WIFI_PHY_RATE_6M)` fallback.
- **기대값**: cb가 100Hz 근처로 상승.
- **실제 결과**: cb 5초당 +10~80 = 2~16Hz. **여전히 낮음**. 11g 6M OFDM으로 시도해도 동일.
- **해석**: rate가 문제의 본질이 아니었다. broadcast 자체의 CSI 콜백 신뢰성 문제로 보임.

## 8. promiscuous 비활성화 (실패)

- **가설**: STA 모드에서 promiscuous 없이도 `esp_wifi_set_csi(true)`만으로 CSI가 동작할 것. promiscuous는 오버헤드.
- **시도**: RX에서 `esp_wifi_set_promiscuous(true)` 제거.
- **기대값**: STA 자체 RX 경로로 CSI가 더 깨끗이 들어옴.
- **실제 결과**: cb 5초당 +0~1 = **0.2Hz**. 거의 모든 frame이 CSI 콜백 안 일어남.
- **결론**: **STA 모드만으로는 broadcast frame의 CSI 콜백이 거의 발생하지 않음**. promiscuous는 필수.
- **다음 조치**: 즉시 복원.

## 9. ESP-NOW unicast 확인 (이미 적용되어 있었음)

- **가설**: broadcast가 CSI를 안정적으로 트리거 못 한다면 unicast로 전환하면 802.11 ACK + 재전송 매커니즘으로 신뢰성 상승.
- **확인 결과**: 코드 검토 시 `esp_now_tx_task`가 이미 STA 연결되면 각 peer로 unicast 송신하도록 되어 있었음 ([tx_ap_main.c:240-255](../../esp32s3_tx_ap_node/main/tx_ap_main.c#L240)). broadcast는 STA 미연결 시 fallback.
- **해석**: 이미 unicast인데도 RX cb가 5~10Hz뿐. unicast 가설로도 100Hz 달성 안 됨.

## 10. 하드웨어 발열 의심

- **현상**: 디버깅 세션 후반부에서 RX 보드가 TX AP에 association 자체를 못 하는 상태에 도달. `wifi:connected` 로그 없이 cb=10에 stuck. 보드 표면 발열 심함.
- **해석**: 누적 열 스트레스로 WiFi RF 부 또는 모뎀 동작 불안정. **이전 측정들 일부도 thermal-degraded 상태에서 수집된 데이터일 가능성**이 있음. 예를 들어 "cb 500Hz 폭증 → 24Hz 폭락" 변화가 코드 변경의 효과인지 누적 발열의 효과인지 구분이 불가.
- **다음 조치**: 양 보드 30분 이상 cooldown, RSSI saturation 회피를 위해 보드 간 거리 30cm~1m 확보 후 baseline 재측정.

## 현재 펌웨어 상태 (롤백 후)

가장 안정적이었던(hz=22.82, gaps=0.3%) 조합으로 코드는 유지:

| 항목 | 설정 |
|---|---|
| AMPDU | TX/RX 양쪽 off (sdkconfig) |
| AP bandwidth | HT20 강제 (`esp_wifi_set_bandwidth` start 전후 두 번 호출) |
| STA bandwidth | HT20 강제 |
| `channel_filter_en` | false (HT40 secondary 누락 회피 의도) |
| promiscuous | **true 필수** |
| ESP-NOW | unicast (peer당 100Hz), broadcast는 STA 미연결 시 fallback |
| ESP-NOW rate 강제 | 사용 안 함 (default가 cb 발생률+tx 성공률 균형 가장 좋음) |
| BSSID 필터 | 콜백에서 `info->mac != AP BSSID`이면 drop |
| `SEND_INTERVAL_US` | 9000μs (~100Hz 상한) |
| 진단 카운터 | TX `g_enow_ok/fail/cb_ok/cb_fail`, RX `g_csi_cb_count/throttle_drop/filter_drop/sent` 유지 |

## 11. 보드 분리 후 baseline 재측정 (saturation 가설 검증)

- **가설**: 이전 측정에서 RSSI −1~−5 dBm으로 수신기 saturation이 cb 누락의 원인일 수 있음.
- **시도**: 양 보드 30분 cooldown + 보드 간 거리 확보. 양쪽 깨끗한 재플래시.
- **기대값**: RSSI −20 ~ −40 범위로 떨어지면서 cb 100Hz 근처 도달.
- **실제 결과**:
  - RSSI = **−37 dBm** (정상 범위 진입)
  - HT20 협상 성공, BSSID lock 성공, association 안정
  - Checksum mismatch 경고 사라짐 → 최신 빌드 동작 확인
  - cb = 5초당 +28~101 = 평균 **12Hz** (filter_drop 제외 시 우리 AP frame ~4Hz)
  - sent ≈ **2~3Hz**, JSONL 1030초 hz=0.18, seq_drop=**−297**
- **결론**: **RSSI saturation 가설 기각**. 거리/RSSI 정상화돼도 cb 트리거율이 5~10%대로 동일. **ESP-IDF v5.2.2 + 현재 펌웨어 베이스의 소프트웨어 한계 확정**.

### 음수 seq_drop=−297의 원인

collector가 device_id=101로 들어오는 패킷의 seq를 누적 카운팅하는데 −297이 나옴. 가능 원인:
1. **RX 보드가 측정 중 여러 번 재부팅** — `g_seq=0`부터 다시 시작 → collector 입장 "이전보다 작은 seq" → 음수 drop 누적.
2. **두 RX 보드 모두 `CSI_DEVICE_ID=101`로 플래시** — 같은 device_id의 두 송신원이 섞여 seq stream 비일관.

해결: `python scripts/device_registry.py verify`로 보드별 `device_id` 분리 확인. 같으면 한쪽을 102 등 다른 ID로 재플래시. collector 측 seq 검증 로직은 부팅 reset과 multi-source를 구분하도록 보완 필요 (예: device_id + boot_session_id 조합).

## 결론 (확정)

- **ESP-IDF v5.2.2 + ESP32-S3 + 현재 펌웨어 베이스**로는 broadcast/unicast 모두 우리 frame의 CSI 콜백 트리거율이 **5~10% 천장**. RSSI/거리/AMPDU/대역폭/rate/필터 등 코드/설정 옵션은 모두 시도됨.
- 200ms+ 공백 문제 자체는 **14.2% → 0.3%로 거의 해결됨** (BW20 강제 + AMPDU off + channel_filter off + promiscuous + 9ms throttle 조합).
- **다음 단계**: Espressif `esp-csi` 공식 레포의 `csi_recv` 예제 기반으로 펌웨어 베이스 교체 (사용자 결정). 100Hz 검증 사례가 있는 공식 예제에서 출발해 MeshSense 통합.
- **Phase 1 PoC 완료 (2026-05-22)**: [esp32s3_csi_send_poc/](../../esp32s3_csi_send_poc), [esp32s3_csi_recv_poc/](../../esp32s3_csi_recv_poc), [firmware/csi-poc.md](../firmware/csi-poc.md). 둘 다 ESP-IDF v5.2.2 + ESP32-S3로 빌드 검증 완료.
- **실측 결과 (POC_DUMP_CSV=0, 측정 모드)**: 5초당 cb +484~492 = **평균 97.5Hz (96.8~98.4Hz)**. **100Hz 목표 사실상 달성.**
- 결정적 발견: 이전 시도가 22Hz 천장에 막혔던 진짜 원인은 ESP32-S3 한계가 아니라 MeshSense의 **AP/STA association + DTIM gating + 잘못된 bandwidth/rate 조합**. esp-csi 토폴로지(STA만, 채널 11, HT40, custom MAC, ESP-NOW MCS0 OFDM 강제, FreeRTOS HZ=1000)로 전환하니 동일 보드/IDF에서 100Hz 달성.
- 또 한 가지 부수 발견: `POC_DUMP_CSV=1`(CSV 시리얼 출력)에서 cb가 ~46Hz로 떨어졌는데, 이건 921600 baud 시리얼이 ~50Hz의 ets_printf 처리 한계라 WiFi driver task 콜백을 백프레셔로 막은 것. **CSI 콜백 핸들러에서 동기 I/O는 절대 금지**.
- **Phase 2 검증 완료 (2026-05-23)**: 바이너리 ring buffer + USB-Serial-JTAG 스트리밍으로 100Hz × 0% 손실 end-to-end 달성. [doc/firmware/csi-poc.md](../firmware/csi-poc.md) 참조. 디버깅 중 발견: ESP32-S3 dev 보드 USB-C는 UART0가 아니라 USB-Serial-JTAG 페리페럴 — 처음에 `uart_write_bytes`를 썼더니 데이터가 물리 핀으로 나가 USB로 안 보였음.
- 별도 이슈: seq_drop 음수 → device_id 충돌 가능성, 보드별 ID 분리 + collector 측 boot_session 구분 로직 필요.

---

## 별도 이슈: Mac collector IP가 .2가 아니라 .4로 잡히는 현상

### 현상

- 정상 시나리오: Mac이 TX SoftAP에 연결되면 192.168.4.2를 받고 RX는 그 다음 .3, .4를 받음. RX 펌웨어는 `COLLECTOR_IP=192.168.4.2`로 하드코딩되어 있어 패킷이 Mac에 도달.
- 비정상 시나리오: "로깅 명령(시리얼 monitor) → 수집 명령" 순서로 진행하면 Mac IP가 192.168.4.4가 됨. RX는 .2로 송신 → Mac에 패킷 0개 수신.

### 원인

TX SoftAP의 DHCP 서버는 **association 순서대로 192.168.4.2부터 차례로 IP 부여**한다 (DHCP 풀 시작 = .2). 약속이나 reservation이 없다.

로깅 명령(시리얼 모니터)이 먼저 켜지면 그 모니터 대상 RX 보드가 먼저 USB 부팅 → SoftAP에 association → **.2 차지**. 옆 RX 보드가 동시/직후 association → .3. Mac은 마지막에 collector 명령 실행 시 SSID에 연결 → **.4**.

TX 로그 예시 (rx_20260522_181650 세션):
```
station: a2:6b:d4:fa:82:ea join, AID=1, bgn, 20
DHCP server assigned IP to a client, IP is: 192.168.4.2   ← 옆 RX
```

RX 펌웨어 [csi_sender_main.c:29](../../esp32s3_csi_sender/main/csi_sender_main.c#L29)의 `COLLECTOR_IP` 는 컴파일 시 고정.

### 해결 방향

1. **운영적 해결 (즉시)**: Mac이 SoftAP에 먼저 association한 뒤 RX 보드 전원을 켠다. 또는 RX 보드를 모두 떼고 Mac만 연결되어 .2를 받은 것을 확인한 뒤 RX 보드를 켠다.
2. **펌웨어 해결 (권장)**: TX SoftAP DHCP에서 **Mac MAC 주소에 대한 reservation**을 설정하거나, DHCP 풀에서 .2를 제외하고 Mac은 별도 static 처리.
3. **프로토콜 해결**: collector 측이 들어오는 UDP 패킷의 송신 IP를 동적으로 학습하고, RX 펌웨어가 broadcast (`192.168.4.255`)로 송신. 단 broadcast는 DTIM 게이팅 영향을 받아 측정 jitter 가능.
4. **수집기 측 보정**: `mac_collector/session_meta.yaml` 또는 collector CLI 옵션으로 Mac이 받은 실제 IP를 확인하고 RX 펌웨어 빌드 시 `CSI_COLLECTOR_IP` CMake cache 값을 그때그때 바꿔 재플래시. 번거롭지만 즉시 적용 가능.

가장 깔끔한 영구 해결은 2번 (DHCP reservation). 펌웨어 [tx_ap_main.c](../../esp32s3_tx_ap_node/main/tx_ap_main.c)의 `esp_netif_create_default_wifi_ap()` 이후 `dhcps_set_option`으로 처리 가능.
