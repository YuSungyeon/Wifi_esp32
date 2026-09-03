# CLAUDE.md

## Project

MeshSense는 ESP32-S3 기반 Wi-Fi CSI 실내 행동 인식 프로젝트다. 현재 지원하는 수집 방식은 ESP-NOW/USB 한 가지뿐이다.

사용자 문서의 시작점은 `doc/README.md`다. 코드 변경 전 `doc/documentation-policy.md`를 따르고, 동작·계약 변경은 문서를 먼저 수정한다.

## Current Architecture

```text
esp32s3_csi_send_poc (TX)
  ESP-NOW broadcast, channel 11, HT20, 100Hz
       │
       ▼
esp32s3_csi_recv_poc (RX × N)
  promiscuous CSI, LLTF only
  callback → 64KiB ring buffer → USB-Serial-JTAG
       │
       ▼
scripts/csi_serial_reader.py (RX별 process)
  binary v2 → I/Q amplitude → JSONL
       │
       ├─ scripts/visualize_csi.py
       ├─ model_train/preprocessing/preprocess_3rx.py (official 3-RX windows)
       ├─ model_train/lstm/LSTM.py (official baseline training/evaluation)
       └─ model_train/lstm/Preprocessing.py (historical single-RX code)
```

SoftAP/UDP production firmware, UDP collector, `flash_rx.py`, `flash_tx.py`, `meshsense_config.py`, `add/main.py`는 제거되었다. 다시 참조하거나 문서에 복원하지 않는다. 결정 근거는 `doc/adr-poc-only.md`다.

## Authoritative Modules

| 경로 | 책임 |
|---|---|
| `esp32s3_csi_send_poc/` | ESP-NOW 100Hz TX firmware |
| `esp32s3_csi_recv_poc/` | CSI capture와 USB binary streaming RX firmware |
| `scripts/meshsense_cli.py` | registry, firmware flash, multi-RX USB collection |
| `scripts/csi_serial_reader.py` | binary v2 validation, amplitude 변환, JSONL append |
| `mac_collector/device_registry.csv` | RX USB MAC ↔ device ID |
| `mac_collector/tx_registry.csv` | TX USB MAC ↔ TX node ID |
| `mac_collector/session_meta.yaml` | run ID와 실험·수집 조건 |
| `scripts/visualize_csi.py` | RX별 waterfall PNG |
| `model_train/preprocessing/preprocess_3rx.py` | 공식 3-RX 전처리 구현 (`model_train/docs/preprocessing/design.md` 기준) |
| `model_train/<model-name>/` | 모델별 학습·평가 코드(현재 상태는 모델 문서 기준) |
| `model_train/docs/preprocessing/` | 전처리 설계·분석·manifest 문서 |
| `model_train/docs/model-training/` | 모델 비교·설계·학습 결과 문서 |

`mac_collector/` 디렉터리 이름은 registry/session 파일 호환을 위해 남아 있으며 UDP collector를 의미하지 않는다.

## Commands

```bash
python3 scripts/meshsense_cli.py
python3 scripts/meshsense_cli.py --guide
python3 scripts/idf_bootstrap.py -y

python3 scripts/device_registry.py verify
python3 scripts/tx_registry.py verify

python3 scripts/measure_csi_hz.py \
  mac_collector_output/raw/YYYYMMDD/session_<id>
python3 scripts/visualize_csi.py \
  --session-dir mac_collector_output/raw/YYYYMMDD/session_<id>
python3 model_train/preprocessing/preprocess_3rx.py \
  --raw-dir mac_collector_output/raw/YYYYMMDD
```

Manual firmware build:

```bash
cd esp32s3_csi_send_poc && idf.py build
cd esp32s3_csi_recv_poc && idf.py build
```

## Current Constants

| 항목 | 값 |
|---|---|
| Wi-Fi topology | STA-only, association 없음 |
| channel / bandwidth | 11 / HT20 |
| ESP-NOW rate | MCS0 LGI |
| TX frequency | 100Hz (`usleep(10ms)`) |
| CSI capture | LLTF only |
| expected raw CSI | 128 bytes = 64 I/Q pairs |
| USB frame | little-endian v2, 32-byte header |
| `tx_seq` | cross-RX synchronization key |
| reader output | raw I/Q amplitude, on-device normalization 없음 |

## Data Contract

- Binary frame: `doc/data-schema.md`
- Output: `mac_collector_output/raw/YYYYMMDD/session_<id>/device_<id>.jsonl`
- Reader writes `record_schema_version=1`, `transport=usb_serial_jtag`, `csi_representation=raw_iq_amplitude`.
- Files use append mode. 같은 날짜와 session ID를 재사용하면 기존 데이터 뒤에 추가된다.
- RX별 `seq`와 `timestamp_us`는 공유 clock이 아니다. 다중 RX 정렬에는 `tx_seq`를 사용한다.

## Development Rules

- 현재 동작과 목표 설계를 한 문단에서 섞지 않는다. `CURRENT`, `PLANNED`, `HISTORICAL` 상태를 명시한다.
- data field, frame layout, sampling, firmware topology 변경은 관련 문서를 먼저 수정한다.
- 코드와 문서가 함께 검증되지 않으면 완료로 간주하지 않는다.
- 사용자 실험 데이터와 registry/session 값은 명시적 요청 없이 변경하지 않는다.
