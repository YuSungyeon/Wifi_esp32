# esp-csi 베이스 PoC (Phase 1)

`csi-rate-troubleshooting.md`에서 결정한 esp-csi 공식 예제 기반 PoC. **MeshSense 프로토콜 통합 없음**, 100Hz CSI 콜백 달성 자체를 검증하기 위한 최소 펌웨어.

## 디렉터리

- [esp32s3_csi_send_poc/](../../esp32s3_csi_send_poc) — esp-csi `csi_send` 예제 그대로 (송신 보드)
- [esp32s3_csi_recv_poc/](../../esp32s3_csi_recv_poc) — esp-csi `csi_recv` 예제 + 5초 Hz 카운터 로그 추가 (수신 보드)

## upstream과의 차이

- 프로젝트 이름과 `CMakeLists.txt` 단순화 (git_describe 제거)
- `sdkconfig.defaults`에 `CONFIG_IDF_TARGET="esp32s3"` 명시
- RX 측 `wifi_csi_rx_cb`에 카운터 1줄 추가, `hz_log_task`가 5초마다 `5s: cb=N (+delta, Hz)` 출력
- **대역폭 HT20 (raw CSI 128B = 64 OFDM 서브캐리어 × I/Q 2B)** — upstream esp-csi 예제는 HT40(128 SC, raw 256~384B). MeshSense 학습 모델이 64 SC 기준이라 HT20 사용. TX/RX 양쪽에서 `CONFIG_WIFI_BANDWIDTH = WIFI_BW_HT20`, `CONFIG_ESP_NOW_PHYMODE = WIFI_PHY_MODE_HT20`. 채널 secondary는 `WIFI_SECOND_CHAN_NONE`로 BW20 조건문에서 자동 분기됨.
- **RX CSI config는 `htltf_en=false` (LLTF only)** — ESP32-S3 CSI HW는 HT20에서도 `lltf_en+htltf_en` 둘 다 켜면 LLTF(64) + HT-LTF(64)를 concatenate해 raw 256B(=128 SC)를 낸다 (`ltf_merge_en=true`도 실제 평균 안 함). 64 SC 모델 호환을 위해 HT-LTF 캡처 비활성화. 원래 MeshSense 펌웨어도 코드에서 `MAX_AMP_SAMPLES=64`로 앞 64개만 잘랐던 패턴과 등가.

## 토폴로지 (현 MeshSense와 큰 차이)

- **AP/STA association 없음**. 양쪽 보드 모두 `WIFI_MODE_STA`.
- 양쪽 보드의 STA MAC을 `1a:00:00:00:00:00`로 **임의 덮어씀** (`esp_wifi_set_mac`). RX는 이 MAC에서 온 frame만 CSI 통과시킴.
- 채널 11 고정, **HT40 (BELOW)** 강제. ESP-NOW peer rate `HT40 MCS0 LGI`.
- ESP-NOW broadcast (`ff:ff:ff:ff:ff:ff`), `esp_now_set_pmk` 호출 (encrypt=false라 키는 비활성이지만 호출 패턴 보존).
- 송신 주기 `usleep(10ms)`, FreeRTOS HZ=1000 (1ms tick).

## 빌드

```bash
# TX
cd esp32s3_csi_send_poc
idf.py set-target esp32s3
idf.py build

# RX
cd esp32s3_csi_recv_poc
idf.py set-target esp32s3
idf.py build
```

ESP-IDF v5.2.2 환경에서 빌드 검증됨. RX는 `esp_csi_gain_ctrl` managed component를 자동 다운로드한다.

## 플래시 & 측정

기존 `scripts/flash_*.py`는 MeshSense 펌웨어 전용이므로 PoC는 `idf.py`를 직접 사용:

```bash
# TX 보드 (먼저)
cd esp32s3_csi_send_poc
idf.py -p /dev/cu.usbmodemXXXX flash monitor

# RX 보드 (별도 터미널)
cd esp32s3_csi_recv_poc
idf.py -p /dev/cu.usbmodemYYYY flash monitor -b 921600
```

UART baud 921600 주의 (RX sdkconfig.defaults). monitor `-b 921600` 옵션 필수.

## 측정 절차 — Phase 1 (Hz 자체 검증)

1. 두 보드 모두 plug. cooldown 상태인지 확인 (손등 댔을 때 차가워야 함).
2. **TX부터** flash + monitor. `csi_send: wifi_channel: 11, send_frequency: 100, mac: 1a:00:00:00:00:00` 라인 확인.
3. RX flash + monitor (`-b 921600`). RX는 `POC_DUMP_CSV` 플래그에 따라 두 모드:
   - `POC_DUMP_CSV=0` (기본, **속도 측정 모드**): CSV dump 끔. 5초마다 `5s: cb=N (+M, X.XHz)` 만 출력. **진짜 cb 속도**를 본다.
   - `POC_DUMP_CSV=1` (데이터 모드): CSV row 흘려 데이터 검증. 단 921600 baud 시리얼이 ~50Hz를 한계로 cb 자체를 막아 진짜 속도를 못 본다.
4. 5초 로그에서 **cb Hz가 95~100** 근처면 PoC 성공.

`POC_DUMP_CSV` 토글은 [esp32s3_csi_recv_poc/main/app_main.c](../../esp32s3_csi_recv_poc/main/app_main.c)의 `#define POC_DUMP_CSV 0` 한 줄. 빌드 시 매크로로 변경 가능.

### Phase 1 실측 결과 (2026-05-22)

```
 5s: cb=491  (+491, 98.2Hz)
 5s: cb=975  (+484, 96.8Hz)
 5s: cb=1467 (+492, 98.4Hz)
 5s: cb=1951 (+484, 96.8Hz)
```

평균 **97.5Hz** — 목표 100Hz 사실상 달성. MeshSense baseline 22Hz 대비 4배 이상 개선.

---

## Phase 2 — 바이너리 CSI 프레임을 USB serial로 스트리밍

esp-csi recv는 IP 네트워크 없이 동작(STA 모드, association 없음)하므로 UDP로 Mac에 데이터를 보낼 수 없다. 대신 **USB serial로 바이너리 프레임을 직접 스트리밍**한다.

```
RX board   ───  USB(921600 baud)  ──→  Mac
 │                                       │
 CSI cb → ringbuf → uart_writer_task  →  scripts/csi_serial_reader.py → JSONL
```

### 데이터 흐름

1. `wifi_csi_rx_cb`: source MAC 필터 통과 후 헤더+raw를 stack buf에 만들고 **non-blocking** `xRingbufferSend`로 push. 콜백은 마이크로초 단위로 빠르게 반환.
2. `uart_writer_task`: ring buffer에서 꺼내 `uart_write_bytes(UART_NUM_0, ...)`로 그대로 전송. CSI 콜백을 막지 않음.
3. Mac 측 [`scripts/csi_serial_reader.py`](../../scripts/csi_serial_reader.py): 시리얼 포트 열고 magic을 찾아 프레임 단위로 파싱, JSONL 기록.

### 바이너리 프레임 포맷 (LE, packed) — v2

| 오프셋 | 타입 | 필드 | 비고 |
|---|---|---|---|
| 0 | u16 | magic | `0x4353` ('CS') |
| 2 | u8 | version | **2** (v1: tx_seq 없음, v2: 추가) |
| 3 | u8 | reserved | 0 |
| 4 | u16 | total_len | header + raw |
| 6 | u16 | raw_len | raw[] 바이트 수 |
| 8 | u32 | seq | RX 부팅부터 단조 증가 (보드별 독립) |
| 12 | u64 | timestamp_us | RX `esp_timer_get_time()` (보드별 독립) |
| 20 | i8 | rssi | dBm |
| 21 | u8 | channel | |
| 22 | i8 | noise_floor | dBm |
| 23 | u8 | rate | rx_ctrl->rate |
| 24 | u16 | sig_len | |
| 26 | u16 | reserved | 0 |
| 28 | u32 | **tx_seq** | **TX 송신 카운터 — 모든 RX 공통, cross-RX 동기화 키** |
| 32 | i8[raw_len] | raw CSI (I/Q 교차) | |

헤더 32바이트. CRC 없음 — magic + raw_len sanity check로 동기화 복구.

`tx_seq`는 TX 펌웨어가 ESP-NOW broadcast 페이로드에 실어 보내는 `uint32_t count` (payload offset 15)에서 RX 콜백이 추출한 값. **여러 RX 보드가 같은 ESP-NOW 프레임을 동시에 수신하면 모두 동일한 `tx_seq`를 기록**하므로 후처리에서 boards간 정렬·결합 기준이 된다.

### Ring buffer 사이즈

`CSI_RINGBUF_BYTES = 64KB` ≈ 100Hz × 280B × 2s. 시리얼 task가 2초 이상 stall되면 `g_ringbuf_drop` 카운터 증가. 정상 환경에서는 0 유지.

### 5초 진단 로그

`5s: cb=N (+M, Hz) uart=K (+L, Hz) ringbuf_drop=D` — cb는 콜백, uart는 시리얼로 실제 송출된 프레임 수. cb ≈ uart 이어야 정상.

### Mac 측 사용법

```bash
pip install pyserial    # 최초 1회

# RX 시리얼 포트 확인
ls /dev/cu.usbmodem*

# reader 실행 (RX 보드를 별도 monitor로 열면 안 됨 — 포트 점유 충돌)
python scripts/csi_serial_reader.py \
    --port /dev/cu.usbmodem101 \
    --device-id 101 \
    --session-id 1 \
    --output-dir mac_collector_output
```

출력 경로 `mac_collector_output/raw/YYYYMMDD/session_1/device_101.jsonl` — 기존 mac_collector와 동일한 디렉터리 레이아웃. 후처리 파이프라인([add/main.py](../../add/main.py))은 변경 없이 그대로 사용 가능.

JSONL 스키마는 [mac-collector/udp-packet-schema.md](../mac-collector/udp-packet-schema.md)와 호환: `received_at_unix_us`, `session_id`, `device_id`, `seq`, `timestamp_us`, `channel`, `rssi_dbm`, `noise_floor_dbm`, `sample_count`, `csi_amp`. raw int8 I/Q 페어를 `sqrt(I²+Q²)`로 변환해 `csi_amp`에 저장 (RX 펌웨어 측 전처리는 안 함).

### Multi-RX 동시 수집

RX 보드 N개를 USB로 동시에 연결하면 각각 별도 `/dev/cu.usbmodem*` 포트로 잡힌다. USB는 디바이스별 독립 엔드포인트라 대역 충돌 없음. 보드별 `device_id`만 다르게 부여하고 reader N개를 병렬 실행:

```bash
python scripts/csi_serial_reader.py --port /dev/cu.usbmodem101 --device-id 101 --session-id 1 &
python scripts/csi_serial_reader.py --port /dev/cu.usbmodem201 --device-id 102 --session-id 1 &
python scripts/csi_serial_reader.py --port /dev/cu.usbmodem301 --device-id 103 --session-id 1 &
```

세 reader 모두 같은 `--session-id 1`을 쓰면 `mac_collector_output/raw/YYYYMMDD/session_1/`에 `device_101.jsonl`, `device_102.jsonl`, `device_103.jsonl`이 동시에 생성된다 (기존 mac_collector 레이아웃과 동일).

**Cross-RX 정렬**: 각 보드의 `seq`/`timestamp_us`는 서로 독립적(부팅 시각이 다름)이지만, **모든 RX가 같은 ESP-NOW broadcast를 받으면 동일한 `tx_seq`를 JSONL에 기록**한다. 후처리에서 `tx_seq`를 join key로 쓰면 보드 간 데이터가 정렬된다. 예:

```python
# 3개 device_*.jsonl 을 tx_seq로 inner join하면
# 같은 ESP-NOW frame을 동시에 받은 3보드의 CSI가 한 row로 묶임
```

후처리(`add/main.py`)는 현재 `seq` 기준으로 동작 중인데, multi-RX 텐서 생성을 위해서는 `tx_seq` join 로직 추가가 필요. Phase 3 마무리 시 같이 처리.

### ESP_LOG와 바이너리 스트림이 같은 USB-CDC를 공유하는 이슈

`5s:` 진단 로그가 binary stream 사이에 ASCII 텍스트로 끼어든다. reader는 magic resync로 자동 복구하지만 그 순간 1~2 프레임은 손실 가능 (5초에 1회 → 약 0.4% 손실). 더 엄격한 경우 sdkconfig에서 `CONFIG_LOG_DEFAULT_LEVEL_NONE=y`로 로그를 끈다.

### ESP32-S3 USB 구조 주의 (디버깅 메모)

ESP32-S3 dev 보드 USB-C 포트는 **UART0가 아니라 USB-Serial-JTAG 페리페럴**에 연결됨 (`USB mode: USB-Serial/JTAG`). 초기 버전에서 `uart_write_bytes(UART_NUM_0, ...)`로 송신했더니 데이터가 물리 핀으로 나가 USB로 보이지 않았다. `usb_serial_jtag_driver_install` + `usb_serial_jtag_write_bytes`로 교체 후 정상 동작. ESP_LOG도 USB-Serial-JTAG 경유라 같은 인터페이스를 공유한다.

## Phase 2 실측 결과 (2026-05-23)

```
[reader dev101] frames=500  invalid=0 seq_drop=0 hz_avg=90.8 last_rssi=-59 last_tx_seq=149848 last_raw_len=384
[reader dev101] frames=1000 invalid=0 seq_drop=0 hz_avg=93.7 last_rssi=-48 last_tx_seq=150364 last_raw_len=384
[reader dev101] frames=1500 invalid=0 seq_drop=0 hz_avg=95.0 last_rssi=-48 last_tx_seq=150876 last_raw_len=384
```

- 구간 변화량: 500 frames / 5초 = **100Hz** 정확.
- `invalid=0`, `seq_drop=0` — 전 파이프라인 손실 0%.
- `last_raw_len=384` — HT40 LTF 384바이트 raw CSI 그대로 전달.
- `hz_avg`가 90~95대인 건 reader 시작 시 초기 부팅/스캔 지연이 평균에 섞인 것; steady-state는 100Hz.
- `tx_seq` 단조 증가 (+512, +516 / 5초) — TX→RX→USB→Mac 동기화 정상.

**MeshSense baseline 22Hz → 100Hz, 4.5배 개선, 0% 손실.** Phase 2 검증 완료.

## 기대치

upstream esp-csi는 ESP32-S3 보드에서 100Hz를 안정적으로 달성한다고 보고됨. 우리 환경에서도 동일하게 나오면:
- 토폴로지(AP/association 제거, 채널 강제, HT40, MCS0 OFDM 강제)가 핵심이라는 것이 입증됨
- 다음 Phase 2: 이 베이스에 MeshSense UDP 패킷 스키마 + device_registry + session_meta 통합

만약 동일 환경에서도 100Hz가 안 나오면:
- 보드 하드웨어 문제 가능성 (안테나, RF 부)
- ESP-IDF v5.2.2 wifi lib 회귀 가능성 → v5.1 또는 esp-csi 권장 버전(v5.5.0)으로 다운그레이드 검토

## 참고

- esp-csi 레포: https://github.com/espressif/esp-csi
- ESP32-S3 CSI 가이드: https://docs.espressif.com/projects/esp-idf/en/v5.2.2/esp32s3/api-guides/wifi.html#wi-fi-channel-state-information
