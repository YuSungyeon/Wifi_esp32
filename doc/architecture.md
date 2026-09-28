# 현재 아키텍처

> 상태: **CURRENT**
> 기준일: 2026-09-09
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
Mac — 수집 (실시간)
  meshsense_gui.py (비개발자 권장) 또는 meshsense_cli.py
    └─ csi_serial_reader.py × N
         binary v4 검증(CRC-32, [data-schema.md](data-schema.md))
         raw I/Q를 변환 없이 그대로 저장
             │
             ▼
  mac_collector_output/raw/YYYYMMDD/<HHMMSS>_<label>_s<id>/
    device_<id>.csi              raw I/Q, 배타적 생성(append 아님)
    session.json                 라벨 SSOT + 코드 출처(git commit) + RX별 품질 통계
    session_meta_snapshot.yaml   수집 시점 실험 조건
             │
             ├─ visualize_csi.py → 같은 디렉터리에 csi_waterfall.png (`.csi` 직접 읽음)
             │
             ▼
Mac — 전처리 (수집과 분리된 별도 단계)
  export_jsonl.py
    .csi → JSONL record schema v1 (raw_iq_amplitude)
             │
             ▼
  mac_collector_output/jsonl/raw/YYYYMMDD/session_<id>/device_<id>.jsonl
             │
             ├─ model_train/preprocessing/preprocess_3rx.py (공식 3-RX 전처리, CURRENT CONTRACT)
             │    tx_seq 정렬 → 3-RX 공통 구간 → 3초 window → session 단위 split → normalize
             ├─ model_train/lstm/ (LSTM 기준모델, 학습·평가 완료)
             ├─ model_train/cnn1d/ (1D-CNN, LSTM과 공통 runner 사용)
             └─ model_train/docs/ (preprocessing/·model-training/)
```

수집(`.csi` 저장까지)과 전처리(JSONL 내보내기 이후)는 서로 다른 단계다. 수집은 실시간
USB 스트림을 그대로 저장하고, 전처리는 저장된 `.csi`를 나중에 원하는 만큼 다시 내보내고
다시 돌릴 수 있다 — `.csi`가 정본이고 JSONL은 파생물이다.

다음 경로는 지원하지 않는다.

- SoftAP association
- RX→Mac **UDP** CSI 전송, UDP collector
- production/PoC 이중 firmware 선택
- `meshsense_config.json` 기반 network 설정

실시간 분류를 위해 RX를 방 안에 흩어 놓아야 할 때는 USB 대신 ESP-NOW 무선 업링크 +
USB 싱크 경로를 쓴다. 이 경로는 위 수집 도구를 그대로 재사용하며(리더는 싱크 포트를
RX처럼 읽는다) 상세는 [realtime-uplink.md](realtime-uplink.md)를 따른다.

## 2. 모듈 구조

| 모듈 | 책임 | 입력 | 출력 |
|---|---|---|---|
| `esp32s3_csi_send_poc` | 고정 RF 조건에서 CSI 유도 frame 송신 | 없음 | ESP-NOW broadcast |
| `esp32s3_csi_recv_poc` | CSI capture와 binary frame 생성 | ESP-NOW frame | USB-Serial-JTAG bytes |
| `meshsense_gui.py` | 브라우저 제어판 — 보드·수집·세션·진단 (비개발자용 권장 경로) | registry, session meta, USB ports | reader processes, session 폴더 |
| `meshsense_cli.py` | 보드 식별·flash·multi-RX 수집 orchestration (터미널) | registry, session meta, USB ports | reader processes, logs |
| `csi_serial_reader.py` | binary v4 frame 검증(CRC-32)·IDENT 식별·`.csi` 저장 | RX USB stream | `device_<id>.csi`, `session.json` |
| `csi_store.py` | frame 규격·검증·진폭·유효 서브캐리어의 Python 단일 소스 | — | — |
| `csi_session.py` | 세션 디렉터리·manifest(`session.json`)·`session_id` 자동 순번 | — | — |
| `device_registry.csv` | RX 물리 보드와 논리 ID 연결 | USB chip MAC | `device_id` |
| `tx_registry.csv` | TX 물리 보드 식별 | USB chip MAC | `tx_node_id` |
| `session_meta.yaml` | run과 실험 조건의 SSOT | 운영자 입력 | snapshot |
| `export_jsonl.py` | `.csi` → JSONL record schema v1 내보내기 | `device_<id>.csi`, `session.json` | `device_<id>.jsonl`, `labels.json` |
| `visualize_csi.py` | RX별 amplitude 시각화 | JSONL | PNG |
| `model_train/preprocessing/preprocess_3rx.py` | 공식 3-RX 정렬·window·split·normalization | JSONL과 session metadata | split별 배열, metadata, manifest |
| `model_train/lstm/LSTM.py` | 공식 LSTM 기준모델 학습·검증·평가 | 전처리 산출물 | checkpoint, metric, prediction |
| `model_train/cnn1d/CNN1D.py` | 1D-CNN 학습·검증·평가 (LSTM의 공통 runner 사용) | 전처리 산출물 | checkpoint, metric, prediction |
| `model_train/lstm/Preprocessing.py` | 구형 단일 RX 전처리 기록 (역사적 참고용) | JSONL | in-memory `X`, `y` |

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
   - `csi_session.next_session_id`가 `mac_collector_output/raw/`에 이미 있는 세션 디렉터리 이름에서
     순번을 스캔해 최댓값+1을 다음 `session_id`로 정한다 — 사람이 손으로 올릴 값이 아니다.
   - 세션 디렉터리 이름 자체에 수집 시각과 라벨이 들어간다: `<HHMMSS>_<label>_s<session_id>`.
   - `mac_collector/session_meta.yaml`은 라벨을 포함한 실험 조건의 SSOT이며, 수집 시작 시점의
     내용을 `session_meta_snapshot.yaml`로 복사해 데이터와 함께 보존한다.

5. **RX별 reader process 실행**
   - RX마다 `csi_serial_reader.py`를 별도 process로 실행한다.
   - 각 process에는 USB port, `device_id`, session 디렉터리를 전달한다.
   - reader는 RX가 보낸 binary v4 header(CRC-32 포함)를 검증하고, **변환 없이** raw I/Q를
     그대로 `device_<id>.csi`에 이어붙인다 — 진폭 계산이나 정규화는 하지 않는다.
   - `.csi`는 배타적 생성(`open("xb")`)이라 이미 파일이 있으면 append 대신 즉시 에러를 낸다.
   - 수집 종료 시 `csi_session.finalize_session`이 RX별 CRC 실패·resync·seq gap 등의 품질
     통계와 라벨을 `session.json`에 기록한다.
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
  → raw/YYYYMMDD/<HHMMSS>_<label>_s<id>/device_<id>.csi
  → session.json (라벨·품질 통계) + session_meta_snapshot.yaml
```

전처리 입력이 필요할 때만 다음을 실행해 JSONL을 별도로 만든다(§9, [data-schema.md](data-schema.md) §3-4).

```text
export_jsonl.py
  → jsonl/raw/YYYYMMDD/session_<id>/device_<id>.jsonl + labels.json
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
mac_collector_output/raw/YYYYMMDD/<HHMMSS>_<label>_s<session_id>/
├── device_101.csi
├── device_102.csi
├── device_103.csi
├── session.json
├── session_meta_snapshot.yaml
└── csi_waterfall.png            (선택, visualize_csi.py)
```

디렉터리 이름에 수집 시각이 들어가 충돌이 불가능하다. `.csi`는 배타적 생성(`open("xb")`)이라
같은 파일에 다시 쓰려 하면 append가 아니라 즉시 에러가 난다 — 예전 `session_<id>` + JSONL
append 레이아웃은 실제로 여러 run이 한 파일에 섞이는 사고를 냈다.

`visualize_csi.py`는 이 디렉터리의 `.csi`를 직접 읽어 같은 위치에 PNG를 만든다(구 JSONL
세션에는 `.jsonl`로 폴백). JSONL 내보내기와는 별개다.

Frame과 `.csi`/JSONL field는 [serial frame schema](data-schema.md)가 유일한 data contract다.

## 8. 설정 SSOT

| 관심사 | SSOT |
|---|---|
| TX/RX 물리 보드 구분 | `tx_registry.csv`, `device_registry.csv` |
| run ID와 label/환경 | `session_meta.yaml` |
| RF channel/bandwidth/rate | TX/RX firmware source constants |
| binary frame·`.csi`·JSONL field | `data-schema.md` + producer/reader constants |
| 공식 3-RX 전처리(window/split/normalize) | `model_train/docs/preprocessing/design.md` + `preprocess_3rx.py` — CURRENT CONTRACT |
| 모델별 feature/학습 설정 | `model_train/docs/model-training/` 문서와 `model_train/lstm/`·`model_train/cnn1d/` 코드 |

RF 설정은 현재 compile-time constant다. 별도의 network configuration file은 없다.

## 9. 후처리 경계

현재 안정적으로 제공되는 후처리는 waterfall과 수집률 측정이다.

- `visualize_csi.py`: 각 RX를 Mac 수신 시각 기준 100Hz grid로 독립 보간해 PNG 생성
- `measure_csi_hz.py`: 마지막 재부팅 이후 RX `timestamp_us` 기준 수집률·gap·sequence 진단

수집(`.csi`)과 모델 입력 사이는 두 단계로 나뉜다.

1. `export_jsonl.py` — `.csi`를 [JSONL record schema v1](data-schema.md#3-jsonl-record-schema-v1-전처리-입력)로 내보낸다. **CURRENT.**
2. `model_train/preprocessing/preprocess_3rx.py` — JSONL을 입력받아 RX 101·102·103의
   `tx_seq` 정렬, 손상 record 제거, 공통 구간 선택, 5-frame 이하 보간, 3초/300-frame
   window 생성, session 단위 split, train 통계 normalization까지 수행해 split별 배열과
   manifest를 만든다. **CURRENT CONTRACT** — LSTM과 1D-CNN 두 모델이 이미 이 산출물로
   학습·평가를 완료했다. 상세는
   [`model_train/docs/preprocessing/design.md`](../model_train/docs/preprocessing/design.md).

`model_train/lstm/`·`model_train/cnn1d/`의 학습 코드는 이 공식 전처리 산출물을 입력으로
쓴다 — 모델별 구현과 실험 결과는 `model_train/docs/model-training/`에 기록한다. 단일
RX·단일 session·hardcoded path를 쓴 구형 코드(`lstm/Preprocessing.py`)는 역사적 참고
자료로만 유지한다.

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
