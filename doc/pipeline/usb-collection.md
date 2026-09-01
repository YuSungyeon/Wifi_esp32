# USB 수집 파이프라인 (모델 학습 데이터 표준 경로)

esp-csi 공식 예제 기반 펌웨어로 CSI를 수집해 **USB 시리얼로 직접 스트리밍**하는 경로입니다.
Wi-Fi association·IP 네트워크 없이 동작하며, 실측 100Hz·손실 0%로 모델 학습 데이터 수집의
표준 경로입니다 (검증 이력은 하단 부록).

```text
TX (esp32s3_csi_send_poc) ── ESP-NOW broadcast 10ms (tx_seq) ──▶ RX (esp32s3_csi_recv_poc) × N
                                                                  │ CSI cb → ringbuf → USB-Serial-JTAG
Mac ◀── USB (바이너리 프레임 v3) ── scripts/csi_serial_reader.py × N → device_<id>.csi
```

실행은 CLI 메뉴 **[1] USB 수집**:
`[1] 보드 플래시 (PoC, MAC 자동 매칭) · [2] 수집 (라벨·시간 입력) · [3] 보드 관리`

## 펌웨어 구성

| 디렉터리 | 역할 |
|----------|------|
| `esp32s3_csi_send_poc` | ESP-NOW 10ms broadcast 송신 (페이로드에 `tx_seq` 카운터) |
| `esp32s3_csi_recv_poc` | CSI 콜백 → ring buffer → USB-Serial-JTAG 바이너리 스트리밍 |

esp-csi upstream 예제와의 차이:

- **토폴로지** — AP/STA association 없음. 양쪽 모두 `WIFI_MODE_STA`, STA MAC을
  `1a:00:00:00:00:00`로 통일(`esp_wifi_set_mac`)하고 RX는 이 MAC의 프레임만 CSI 통과.
  채널 11 고정
- **대역폭 HT20** — raw CSI 128B = 64 서브캐리어 × I/Q 2B. RX CSI config는
  `htltf_en=false`(LLTF only)로 64 SC 유지 (둘 다 켜면 LLTF+HT-LTF concat으로 256B가 됨)
- **USB-Serial-JTAG** — ESP32-S3 보드의 USB-C는 UART0이 아니라 USB-Serial-JTAG 페리페럴.
  `usb_serial_jtag_write_bytes`로 송신 (UART API로는 USB에 안 나감)
- **IDENT 프레임** — RX가 부팅 직후와 2초마다 자기 eFuse MAC을 알린다. 호스트가 수집 전
  esptool로 포트를 프로브할 필요가 없다 ([보드 식별](#보드-식별-ident) 참조)
- RX `hz_log_task`가 5초마다 `5s: cb=N (+M, Hz) uart=K ... ringbuf_drop=D partial=P` 출력.
  `cb ≈ uart`, `ringbuf_drop=0`, `partial=0`이 정상

### 손대면 안 되는 것

- **CSI 콜백 안에 동기 I/O를 넣지 말 것.** `ets_printf` 계열을 넣으면 WiFi driver task가
  백프레셔로 막혀 즉시 ~50Hz로 붕괴한다 (upstream의 CSV 덤프 모드가 그래서 제거됨)
- **진폭 계산·정규화를 보드에서 하지 말 것.** raw I/Q를 그대로 보내고 호스트가 처리한다.
  AP 파이프라인이 온디바이스 z-score로 시간축 진폭 변동(=움직임 신호 본체)을 지워버린 전례가 있다
- **`esp32s3_csi_send_poc/sdkconfig.defaults`의 `CONFIG_FREERTOS_HZ=1000`을 지우지 말 것.**
  송신 루프가 `usleep(10000)`으로 100Hz를 만드는데, tick이 기본값 100Hz면 10ms가 1 tick으로
  반올림돼 실효 주기가 20ms(=50Hz)로 반토막 난다

## 바이너리 시리얼 프레임 (LE, packed) — v4

헤더 44바이트 + payload. 규격의 C 정본은
[`esp32s3_csi_recv_poc/main/app_main.c`](../../esp32s3_csi_recv_poc/main/app_main.c)의
`csi_frame_header_t`, Python 정본은 [`scripts/csi_store.py`](../../scripts/csi_store.py)의
`HEADER_DTYPE`입니다. C 쪽에 `offsetof` static assert가 걸려 있어 한쪽만 고치면 빌드가 깨집니다.

| 오프셋 | 타입 | 필드 | 비고 |
|---|---|---|---|
| 0 | u16 | magic | `0x4353` ('CS') |
| 2 | u8 | version | 4 |
| 3 | u8 | **frame_type** | 0=CSI, 1=IDENT |
| 4 | u16 | total_len | header + payload |
| 6 | u16 | raw_len | payload 바이트 수 (HT20 LLTF = 128) |
| 8 | u32 | seq | RX 부팅부터 단조 증가 (보드별 독립) |
| 12 | u64 | timestamp_us | RX `esp_timer_get_time()` (보드별 독립) |
| 20 | i8 | rssi | dBm |
| 21 | u8 | channel | |
| 22 | i8 | noise_floor | dBm |
| 23 | u8 | rate | rx_ctrl->rate |
| 24 | u16 | sig_len | |
| 26 | u16 | **boot_id** | 부팅마다 새 값. seq 되감김(재부팅) vs 보드 혼입 구분 |
| 28 | u32 | **tx_seq** | TX 송신 카운터 — **모든 RX 공통, cross-RX 동기화 키** |
| 32 | u8 | **agc_gain** | AGC gain (원값) |
| 33 | i8 | **fft_gain** | FFT gain (원값) |
| 34 | u16 | reserved | 0 |
| 36 | f32 | **gain_comp** | `esp_csi_gain_ctrl` 이 계산한 진폭 보정 배율. 0 = baseline 미완성 |
| 40 | u32 | **crc32** | 헤더(이 필드를 0으로 둔 상태) + payload, zlib 호환 |
| 44 | i8[raw_len] | raw CSI (I/Q 교차) 또는 IDENT payload | |

`gain_comp` 를 보드에서 계산하는 이유: `esp_csi_gain_ctrl` 은 **소스 없이 정적
라이브러리(.a)로만** 배포되어 호스트에서 같은 식을 재현할 수 없다.
다만 **기본적으로 적용하지 않는다** — 실측상 이 하드웨어에서는 보정이 분산만 키운다
([`csi_store.gain_compensation`](../../scripts/csi_store.py) docstring에 측정표).

**CRC32가 핵심 방어선입니다.** raw CSI 바이트 안에 우연히 `0x53 0x43`이 나타나 프레임
헤더로 오인되는 일이 실제로 있었고(구 세션에 `channel=159`, `rssi=+11dBm` 레코드가 남아 있음),
magic + 길이 검사만으로는 막을 수 없습니다.

### 보드 식별·진단 (IDENT)

`frame_type=1`, `raw_len=32`, payload = eFuse base MAC 6B + 펌웨어 문자열 10B +
**펌웨어 진단 카운터 4×u32** (`csi_cb`, `uart_sent`, `ringbuf_drop`, `uart_partial`).

카운터를 프레임에 실은 이유: 이 프로젝트의 콘솔 primary 는 GPIO43 UART 이고
(`CONFIG_ESP_CONSOLE_UART_CUSTOM`) USB-Serial-JTAG 드라이버를 직접 설치해 쓰기 때문에
**`ESP_LOG` 가 USB 로 나오지 않는다** (실측: 호스트에서 17초 캡처 중 로그 텍스트 0건).
5초 로그만으로는 `ringbuf_drop`/`partial` 을 볼 방법이 없었다. 이제 이 값들이
reader 출력과 `session.json` 에 그대로 들어온다.
reader가 이 MAC을 [`device_registry.csv`](../../mac_collector/device_registry.csv)에서 찾아
`device_id`를 정합니다. **보드를 어느 USB 포트에 꽂아도 되고, 수집 시작 시 보드가 리셋되지
않습니다** — 예전에는 CLI가 포트마다 `esptool read_mac`을 돌렸는데, esptool이 DTR/RTS로
칩을 부트로더에 넣었다 hard reset 하는 바람에 TX까지 리셋되어 `tx_seq`가 세션 중간에
0으로 되감겼습니다.

## 세션 레이아웃

```text
mac_collector_output/raw/<YYYYMMDD>/<HHMMSS>_<label>_s<session_id>/
    device_101.csi               # 40B 헤더 + raw I/Q 프레임을 그대로 이어붙인 바이너리
    device_103.csi
    session.json                 # 매니페스트 — 라벨 SSOT + RX별 수집 품질 통계
    session_meta_snapshot.yaml   # 수집 시점 session_meta.yaml 스냅샷
    csi_waterfall.png            # (선택) 수집 직후 생성
```

- **디렉터리 이름에 수집 시각이 들어가 충돌이 불가능합니다.** 예전 `session_<id>` 레이아웃은
  `session_meta.yaml`의 `session_id` 갱신을 잊으면 기존 파일에 조용히 append 됐고, 실제로
  여러 세션이 그렇게 오염됐습니다. `.csi` 파일은 배타적 생성(`open("xb")`)이라 충돌 시
  append가 아니라 즉시 에러입니다.
- **라벨은 수집 시점에 박힙니다.** CLI가 `empty / static / action`을 묻고(기본값은
  `session_meta.yaml`의 `label_target`) `session.json`에 기록합니다. 후처리는 이 값을 읽습니다.
- **`session_id`는 자동 순번입니다** — 기존 세션의 최댓값+1. 사람이 관리하지 않습니다.
  실험 조건(방·운영자·메모)은 `python scripts/session_form.py` 브라우저 폼으로 입력합니다.
- **`.csi`는 raw I/Q를 그대로 담습니다** — 저장 단계에 변환이 없어 변환 버그가 낄 자리가 없고,
  위상이 보존되어 나중에 살릴 수 있습니다. 진폭 계산과 유효 서브캐리어 선별은
  [`scripts/csi_store.py`](../../scripts/csi_store.py)가 맡습니다.
  구 JSONL 대비 **레코드당 1343B → 168B (8.0배 축소)**.

`session.json` 예:

```json
{ "schema": 1, "pipeline": "usb", "frame_version": 3,
  "session_id": 21, "label": "static",
  "started_at_unix_us": 1781508202208374, "ended_at_unix_us": 1781508262539708,
  "devices": [ {"device_id": 101, "board_name": "RX101", "sta_mac": "E8:F6:0A:8A:E4:F8",
                "port": "/dev/cu.usbmodem101", "boot_id": 41233, "frames": 6090,
                "crc_fail": 0, "invalid": 0, "resync": 0, "seq_gap": 3,
                "first_tx_seq": 122324, "last_tx_seq": 128999, "exit_code": 0} ] }
```

## 수집 실행

CLI가 권장 경로입니다 — 라벨을 묻고, 세션을 만들고, 연결된 모든 포트에 reader를 붙입니다.
RX가 아닌 포트(TX·미등록 보드)는 IDENT가 오지 않아 자동으로 빠집니다.

수동 실행:

```bash
python -c "import sys; sys.path.insert(0,'scripts'); import csi_session, pathlib; \
print(csi_session.create_session(pathlib.Path('mac_collector_output'), label='static', session_id=21))"

python scripts/csi_serial_reader.py \
    --port /dev/cu.usbmodem101 \
    --session-dir mac_collector_output/raw/20260825/143000_static_s21
```

reader 종료 코드: `0` 정상 / `2` IDENT 미수신(RX 보드가 아님) / `3` 스트림 정지 / `4` 파일 충돌.
**스트림이 3초 이상 끊기면 reader가 에러로 끝납니다** — 예전에는 시리얼 타임아웃을 삼켜서
보드가 죽어도 빈 파일을 만들며 영원히 정상인 척했습니다.

### TX 는 전원만 있으면 된다

TX 펌웨어(`esp32s3_csi_send_poc`)는 **호스트 입출력이 전혀 없다** — `app_main` 이
`esp_now_send` + `usleep(10ms)` 를 무한 반복할 뿐이다. USB 포트가 모자라면 TX 를
휴대폰 충전기·보조배터리에 꽂아두고 노트북에는 RX 만 연결하면 된다.
(진단 로그도 어차피 GPIO43 UART 로만 나가므로 USB 로 연결해도 볼 게 없다.)

TX 가 죽으면 RX 에 CSI 가 아예 안 들어오므로 reader 가 3초 안에 `rc=3` 으로 끝난다 —
따로 감시할 필요는 없다. **주의할 것은 TX 가 수집 도중 재부팅하는 경우**다:

- `tx_seq` 가 0부터 다시 시작해 **RX 간 정렬 키가 깨진다.**
- reader 가 `[경고] TX 재부팅으로 보입니다 (tx_seq N → 0)` 을 출력하고 `tx_back` 을 센다.
  `measure_csi_hz.py` 도 `tx_back` 과 경고를 표시하고, `Preprocessing` 은 그런 세션을
  **거부한다** (`build_dataset` 은 건너뛴다). 그 세션은 재수집해야 한다.
- 보조배터리는 소비 전류가 낮으면 자동으로 꺼지는 제품이 있다. 긴 수집에는 상시 전원
  어댑터를 쓰는 편이 안전하다.

### RX 는 반대로 호스트 연결이 필수다

RX 는 CSI 를 USB 로 흘려보내기만 한다 — SD 카드도, 플래시 저장도, 무선 업링크도 없다.
호스트가 안 읽는 동안에도 보드는 계속 CSI 를 받지만 링버퍼(64KB ≈ 4초)를 넘는 것은
버려진다 (실측: 15초 방치에 콜백 1192회 중 670회가 `ringbuf_drop`).
**RX 를 원격에 배치하려면 무선 업링크가 필요하고, 그게 다음 단계인
ESP-NOW 업링크 + USB 싱크 구성이다.**

> 라이브 스트림을 애드혹 스크립트로 들여다볼 때는 **백로그를 먼저 비워야 한다.**
> 포트를 열자마자 읽으면 최대 4초 과거의 프레임(오래된 IDENT 카운터 포함)을 보게 되어
> 진단 수치가 통째로 틀어진다. `csi_serial_reader.py` 는 이 처리를 하지만 임시 스크립트는
> 잊기 쉽다.

### Multi-RX 동시 수집

RX 보드 N개를 USB로 연결하면 각각 독립 포트(`/dev/cu.usbmodem*`)로 잡히고 대역 충돌이
없습니다. 모든 RX 는 **같은 바이너리**를 쓰고 STA MAC 도 같지만(`1a:00:00:00:00:00`),
RX 는 송신을 하지 않으므로 충돌하지 않습니다. 보드 구분은 IDENT 의 eFuse MAC 이 합니다. 각 보드의 `seq`/`timestamp_us`는 부팅 시각이 달라 독립이지만, 같은 ESP-NOW
broadcast를 받은 보드들은 **동일한 `tx_seq`** 를 기록하므로 후처리에서 `tx_seq`를 join
key로 정렬합니다. 정렬 품질은 `measure_csi_hz.py`의 `cross-RX 공통 tx_seq` 비율로 확인합니다.

### 진단

```bash
python scripts/measure_csi_hz.py mac_collector_output/raw/20260825/143000_static_s21
python scripts/visualize_csi.py --session-dir <위와 동일>
```

`crc_fail`·`invalid`·`resync`가 0이 아니면 스트림이 깨지고 있다는 뜻입니다.
`boot_changes > 0`이면 수집 중 보드가 재부팅됐다는 뜻이라 그 세션은 버리는 편이 낫습니다.

### 하드웨어 특성 (실측으로 확인된 것)

- **DTR/RTS 를 건드리면 보드가 리셋된다.** ESP32-S3 의 USB-C 는 외부 UART 브리지가 아니라
  네이티브 USB-Serial-JTAG 라, 브리지 칩용 관례대로 `dtr=False, rts=False` 를 눌러두면
  **오히려** 리셋이 걸린다 (실측: 포트 open 0.43초 뒤 재부팅). reader 는 이 선들을
  건드리지 않는다. 브리지 칩 보드를 쓸 일이 생기면 `--force-lines`
- **보드는 호스트가 없어도 CSI 를 계속 쌓는다.** ring buffer(64KB ≈ 4초)가 차면 **가장
  오래된 프레임을 버리고** 새 것을 넣는다(keep-newest). 그래도 붙는 순간 몇 초치 묵은
  프레임이 먼저 나오므로, reader 는 open 직후 백로그가 마를 때까지 읽고 버린다
  (`--flush-idle-ms`). 이 처리를 안 하면 수집 앞부분에 tx_seq 격자 구멍이 생긴다
  (실측: 46초 방치 후 4134스텝 구멍)
- ESP_LOG 는 USB 로 나오지 않는다 (위 IDENT 절 참조). 진단은 IDENT 카운터로 본다

## 빌드·플래시 (수동, 고급)

PoC 펌웨어는 CMake cache 파라미터가 없어 `flash_rx/tx.py` 대상이 아닙니다.
**RX 펌웨어는 보드마다 동일한 바이너리입니다** (`device_id`가 펌웨어에 없고 IDENT MAC으로
호스트가 정하므로). CLI `[1] 보드 플래시`가 권장 경로이고, 수동은 `idf.py` 직접:

```bash
cd esp32s3_csi_send_poc   # TX 먼저, 이후 esp32s3_csi_recv_poc
idf.py set-target esp32s3
idf.py build
idf.py -p /dev/cu.usbmodemXXXX flash
```

RX는 `esp_csi_gain_ctrl` managed component를 자동 다운로드합니다 (ESP-IDF v5.2.2 검증).

## 부록 — 검증 이력

배경: AP 파이프라인 초기 구현이 22Hz 천장에 막혀([csi-rate-troubleshooting.md](../overview/csi-rate-troubleshooting.md))
esp-csi 예제 기반으로 재구성한 경로가 이 파이프라인입니다.

- **Hz 검증 (2026-05-22)** — RX 5초 카운터 평균 97.5Hz (목표 100Hz 사실상 달성)
- **USB 스트리밍 검증 (2026-05-23)** — reader 측 구간 500 frames/5s = 100Hz 정확
- **RX 2대 동시 수집 검증 (2026-08-26)** — TX1(외부 전원) + RX101 + RX103, 60초:
  `cross-RX 공통 tx_seq = 99.8%`, 두 RX 모두 `crc_fail=0 seq_gap=0 tx_back=0`,
  `hz=99.91 / 98.99`. 후처리 텐서 `(188, 300, 104)` = RX 2대 × 52 서브캐리어.
  공통 구간의 두 RX 평균진폭 상관 −0.062 로, 서로 다른 경로의 채널을 보고 있음을 확인.
- **프레임 v4 · 세션/라벨 정비 실보드 검증 (2026-08-26)** — TX1 + RX103, 60초:
  `hz=99.83  gaps>200ms=0  crc_fail=0  invalid=0  resync=0  seq_gap=0  boot_changes=0
  tx_cov=0.998`, 펌웨어 `ringbuf_drop` 수집 중 증가 없음, `partial=0`.
  진행·실패 기록은 [sprint/2026-08-collection-hardening.md](../sprint/2026-08-collection-hardening.md)

## 참고

- esp-csi: https://github.com/espressif/esp-csi
- ESP32-S3 CSI 가이드: https://docs.espressif.com/projects/esp-idf/en/v5.2.2/esp32s3/api-guides/wifi.html#wi-fi-channel-state-information
