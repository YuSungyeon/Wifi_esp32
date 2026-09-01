# 현재 아키텍처

> 상태: **CURRENT**
> 기준일: 2026-07-22
> 결정: [ADR-0001 — ESP-NOW/USB 단일 경로](adr-poc-only.md)

## 1. 시스템 경계

MeshSense의 공식 데이터 경로는 하나다.

```text
ESP32-S3 TX
  esp32s3_csi_send_poc
  STA-only · channel 11 · HT20
  ESP-NOW broadcast 100Hz
             │
             ▼
ESP32-S3 RX × N
  esp32s3_csi_recv_poc
  promiscuous CSI · LLTF only ([상세 설명](firmware.md#41-lltf의-의미와-csi-계산))
  callback → 64KiB ring buffer → USB-Serial-JTAG
             │ RX별 USB 연결
             ▼
Mac
  meshsense_cli.py
    └─ csi_serial_reader.py × N
         binary v2 validation
         raw I/Q → sqrt(I² + Q²)
         device별 JSONL append
             │
             ├─ session_meta_snapshot.yaml
             ├─ visualize_csi.py → csi_waterfall.png
             ├─ model_train/<model-name>/ (experimental code)
             └─ model_train/docs/ (preprocessing·training documents)
```

다음 경로는 지원하지 않는다.

- SoftAP association
- RX→Mac UDP CSI 전송
- UDP collector
- production/PoC 이중 firmware 선택
- `meshsense_config.json` 기반 network 설정

## 2. 모듈 구조

| 모듈 | 책임 | 입력 | 출력 |
|---|---|---|---|
| `esp32s3_csi_send_poc` | 고정 RF 조건에서 CSI 유도 frame 송신 | 없음 | ESP-NOW broadcast |
| `esp32s3_csi_recv_poc` | CSI capture와 binary frame 생성 | ESP-NOW frame | USB-Serial-JTAG bytes |
| `meshsense_cli.py` | 보드 식별·flash·multi-RX 수집 orchestration | registry, session meta, USB ports | reader processes, logs |
| `csi_serial_reader.py` | binary frame 검증·변환·저장 | RX USB stream | JSONL |
| `device_registry.csv` | RX 물리 보드와 논리 ID 연결 | USB chip MAC | `device_id` |
| `tx_registry.csv` | TX 물리 보드 식별 | USB chip MAC | `tx_node_id` |
| `session_meta.yaml` | run과 실험 조건의 SSOT | 운영자 입력 | snapshot |
| `visualize_csi.py` | RX별 amplitude 시각화 | JSONL | PNG |
| `model_train/lstm/Preprocessing.py` | `tx_seq` 기반 window 실험 | JSONL | in-memory `X`, `y` |
| `model_train/lstm/LSTM.py` | 단일 session 분류 실험 | `X`, `y` | 학습된 in-memory model |

## 3. 제어 흐름

### 보드 식별

1. CLI가 `/dev/cu.usbmodem*` 포트를 찾는다.
2. `esptool`로 보드의 실제 chip MAC을 읽는다.
3. TX registry와 RX registry를 조회한다.
4. TX이면 sender firmware, RX이면 receiver firmware를 선택한다.
5. flash 성공 후 `flash_state.json`에 UI 상태를 기록한다.

Firmware 내부에서는 무선 실험을 위해 STA MAC을 `1a:00:00:00:00:00`으로 설정한다. Registry에 저장되는 MAC은 USB로 읽은 chip MAC이며 용도가 다르다.

### 수집 시작

수집은 firmware flash가 끝난 뒤 실행하는 별도 단계다. TX는 전원을 유지해 무선을 계속 송신하고, RX의 `idf.py monitor`는 reader와 USB 포트를 충돌시키므로 닫아 둔다.

1. **TX 송신 유지**
   - TX firmware가 channel 11, HT20 조건에서 100Hz ESP-NOW broadcast를 계속 송신한다.
   - TX를 끄면 RX callback이 처리할 CSI frame이 더 이상 들어오지 않는다.

2. **RX USB 연결**
   - 수집할 RX 보드를 모두 Mac에 USB로 연결한다.
   - RX 하나당 USB 포트 하나와 reader process 하나가 필요하다.

3. **USB 포트와 RX registry 매칭**
   - CLI가 `/dev/cu.usbmodem*` 포트를 모두 검색한다.
   - 각 포트에서 물리 chip MAC을 읽고 `device_registry.csv`와 비교한다.
   - RX registry와 일치한 포트만 `(port, device_id)` 수집 대상으로 선택한다.
   - TX 보드나 미등록 보드는 수집 대상에서 제외한다.
   - `devices.expected_device_ids`는 현재 실험 기록용이며 CLI의 자동 filter로 사용하지 않는다.

4. **세션 ID와 메타데이터 고정**
   - CLI가 `mac_collector/session_meta.yaml`의 `session_id`를 읽는다.
   - 이 ID가 출력 경로와 JSONL의 `session_id`가 된다.
   - 수집 시작 시점의 `session_meta.yaml`을 `session_meta_snapshot.yaml`로 복사해 당시 라벨·환경·acquisition 조건을 데이터와 함께 보존한다.

5. **RX별 reader process 실행**
   - CLI가 RX마다 `csi_serial_reader.py`를 별도 process로 실행한다.
   - 각 process에는 USB port, `device_id`, `session_id`, output directory를 전달한다.
   - reader는 RX가 보낸 binary v2 header와 raw I/Q를 검증하고, I/Q를 amplitude로 변환한 뒤 해당 device JSONL에 append한다.
   - reader의 stdout/stderr는 터미널과 `log/reader_session<session>_dev<id>_<timestamp>.log`에 동시에 기록된다.

6. **수집 대기와 종료**
   - 기본 수집 시간은 60초이며, CLI에서 다른 시간을 입력하거나 0을 입력해 수동 종료할 수 있다.
   - 시간 제한 수집은 deadline까지 대기하고, 수동 수집은 Ctrl+C로 종료한다.
   - 시간 제한 모드에서 reader가 비정상 종료하면 CLI가 이를 경고하고, 남은 reader에 SIGINT를 보낸다.
   - 최대 10초 동안 정상 종료를 기다린 뒤 응답하지 않으면 SIGTERM, 그 다음 SIGKILL로 정리한다.

7. **수집 후 시각화**
   - 모든 reader process와 로그 파일을 정리한 뒤 `visualize_csi.py` 실행을 시도한다.
   - 시각화용 `.venv`와 numpy/matplotlib가 없으면 설치 여부를 안내하고 PNG 생성을 건너뛸 수 있다.
   - 성공하면 같은 session 폴더에 `csi_waterfall.png`가 생성된다.

수집 중의 최종 데이터 경로는 다음과 같다.

```text
RX USB binary stream
  → csi_serial_reader.py × RX 수
  → raw/YYYYMMDD/session_<id>/device_<id>.jsonl
  → session_meta_snapshot.yaml
  → (선택) csi_waterfall.png
```

## 4. TX firmware

구현: [`esp32s3_csi_send_poc/main/app_main.c`](../esp32s3_csi_send_poc/main/app_main.c)

| 항목 | 현재 값 |
|---|---|
| Wi-Fi mode | STA, association 없음 |
| synthetic STA MAC | `1a:00:00:00:00:00` |
| channel | 11 |
| bandwidth | HT20 |
| ESP-NOW peer | `ff:ff:ff:ff:ff:ff`, encryption off |
| PHY | HT20, MCS0 LGI |
| payload | 증가하는 little-endian `uint32_t count` |
| interval | 10ms (`CONFIG_SEND_FREQUENCY=100`) |

TX sequence는 RX에서 `tx_seq`로 추출되어 여러 RX의 공통 동기화 키가 된다.

## 5. RX firmware

구현: [`esp32s3_csi_recv_poc/main/app_main.c`](../esp32s3_csi_recv_poc/main/app_main.c)

수신 처리:

```text
promiscuous CSI callback
  → source MAC filter
  → RX seq / RX timestamp / RF metadata / tx_seq header 생성
  → raw CSI와 함께 no-split ring buffer에 non-blocking push
  → writer task가 USB-Serial-JTAG로 전송
```

현재 설정:

- LLTF 활성, HT-LTF와 STBC HT-LTF 비활성
- HT20에서 기대 raw CSI는 128 bytes, 즉 signed int8 I/Q pair 64개
- `CSI_MAX_RAW_BYTES=384`는 안전 상한
- ring buffer 64KiB, USB TX buffer 16KiB
- callback과 USB writer를 분리해 serial backpressure가 callback을 직접 막지 않도록 함
- `POC_DUMP_CSV=0`; binary stream만 공식 수집 형식
- 5초마다 callback, USB frame, ring buffer drop 통계를 출력

ESP_LOG와 binary frame이 같은 USB interface를 공유할 수 있다. Reader는 magic 재동기화와 strict header validation으로 로그가 끼어든 구간을 건너뛴다.

## 6. 동기화 모델

| 값 | 범위 | 용도 |
|---|---|---|
| `seq` | RX별, 부팅마다 0부터 | RX 내부 drop 추정 |
| `timestamp_us` | RX별 `esp_timer`, 공유 clock 아님 | RX 내부 시간 분석 |
| `received_at_unix_us` | Mac process별 수신 시각 | 파일 기록·시각화 |
| `tx_seq` | TX가 생성, 모든 RX가 공유 | cross-RX 정렬 기준 |

다중 RX feature 결합은 `tx_seq`를 기준으로 해야 한다. RX별 `seq`나 `timestamp_us`를 서로 직접 비교하면 안 된다.

## 7. 데이터 저장

```text
mac_collector_output/raw/YYYYMMDD/session_<session_id>/
├── device_101.jsonl
├── device_102.jsonl
├── device_103.jsonl
├── session_meta_snapshot.yaml
└── csi_waterfall.png
```

Reader는 JSONL을 append 모드로 연다. 같은 날짜와 같은 `session_id`를 다시 사용하면 기존 파일 뒤에 이어 쓰므로 session ID는 run마다 새로 지정한다.

Frame과 JSONL field는 [serial frame schema](data-schema.md)가 유일한 data contract다.

## 8. 설정 SSOT

| 관심사 | SSOT |
|---|---|
| TX/RX 물리 보드 구분 | `tx_registry.csv`, `device_registry.csv` |
| run ID와 label/환경 | `session_meta.yaml` |
| RF channel/bandwidth/rate | TX/RX firmware source constants |
| binary frame | `serial-frame-schema.md` + producer/reader constants |
| 모델 window/feature | `model_train/docs/` 문서와 `model_train/<model-name>/` 코드 — experimental |

RF 설정은 현재 compile-time constant다. 별도의 network configuration file은 없다.

## 9. 후처리 경계

현재 안정적으로 제공되는 후처리는 waterfall과 수집률 측정이다.

- `visualize_csi.py`: 각 RX를 Mac 수신 시각 기준 100Hz grid로 독립 보간해 PNG 생성
- `measure_csi_hz.py`: 마지막 재부팅 이후 RX `timestamp_us` 기준 수집률·gap·sequence 진단

`model_train/<model-name>/`의 코드는 실험 단계다. 단일 RX·단일 session·hardcoded
path/label이며 CLI pipeline에 연결되지 않았다. 모델별 현재 상태와 목표는
`model_train/docs/`의 전처리·모델 문서에 기록한다.

## 10. 아키텍처 변경으로 취급하는 항목

여기서 말하는 “불변 조건”은 절대로 수정할 수 없다는 뜻이 아니다. 현재 TX·RX·reader·후처리가 함께 기대하는 **공통 약속**이므로, 아래 항목을 바꾸면 단순한 코드 한 줄 수정이 아니라 architecture/data contract 변경으로 취급한다.

예를 들어 TX 송신 주파수를 100Hz에서 50Hz로 바꾸면 수집률, 보간, window 길이, 모델 입력의 시간 의미가 달라진다. 따라서 관련 문서와 producer/consumer를 함께 검토해야 한다.

| 변경 항목 | 함께 영향을 받는 부분 |
|---|---|
| ESP-NOW channel, bandwidth, rate, interval | TX/RX 무선 설정, CSI shape, 수집률 문서 |
| LLTF/HT-LTF capture | raw CSI 길이, I/Q pair 수, binary·모델 입력 |
| binary header field·크기·version | RX producer, `csi_serial_reader.py`, schema |
| `tx_seq` 추출 방식 | multi-RX 동기화와 preprocessing |
| amplitude 표현 또는 subcarrier 선택 | JSONL field 의미, visualization, 모델 feature |
| output directory/JSONL field | reader, measure/viz, 후처리와 기존 데이터 호환 |

이런 변경은 [문서 주도 개발 규칙](documentation-policy.md)에 따라 문서를 먼저 수정하고, 코드 producer와 consumer를 함께 변경한 뒤 검증한다.
