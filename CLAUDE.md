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
       └─ model_train/lstm/Preprocessing.py → LSTM.py (experimental)
```

SoftAP/UDP production firmware, UDP collector, `flash_rx.py`, `flash_tx.py`, `meshsense_config.py`, `add/main.py`는 제거되었다. 다시 참조하거나 문서에 복원하지 않는다. 결정 근거는 `doc/adr-poc-only.md`다.

## Authoritative Modules

| 경로 | 책임 |
|---|---|
| `esp32s3_csi_send_poc/` | ESP-NOW 100Hz TX firmware |
| `esp32s3_csi_recv_poc/` | CSI capture와 USB binary streaming RX firmware |
| `scripts/meshsense_gui.py` | 브라우저 제어판 — 보드·수집·세션·진단 (비개발자용 권장 경로) |
| `scripts/meshsense_cli.py` | registry, firmware flash, multi-RX USB collection |
| `scripts/csi_store.py` | **frame 규격·검증·진폭·유효 서브캐리어의 Python 단일 소스** |
| `scripts/csi_session.py` | 세션 디렉터리·manifest(라벨 SSOT)·`session_id` 자동 순번 |
| `scripts/csi_serial_reader.py` | binary v4 검증(CRC32), IDENT 식별, `.csi` 저장 |
| `scripts/export_jsonl.py` | `.csi` → JSONL record schema v1 (전처리 입력) |
| `scripts/check_separability.py` | 3-class 분리 가능성 진단 (세션 단위 LOSO) |
| `mac_collector/device_registry.csv` | RX USB MAC ↔ device ID |
| `mac_collector/tx_registry.csv` | TX USB MAC ↔ TX node ID |
| `mac_collector/session_meta.yaml` | 실험 조건과 라벨 기본값 (run ID 는 자동 순번) |
| `<세션>/session.json` | **세션 라벨 SSOT** + RX별 수집 품질 통계 |
| `scripts/visualize_csi.py` | RX별 waterfall PNG |
| `model_train/preprocessing/preprocess_3rx.py` | 공식 3-RX 전처리 구현 (`model_train/docs/[전처리]-설계.md` 기준) |
| `model_train/<model-name>/` | 모델별 실험 단계 전처리·학습 코드 |
| `model_train/docs/` | 전처리·모델 비교·설계·학습 문서 |

`mac_collector/` 디렉터리 이름은 registry/session 파일 호환을 위해 남아 있으며 UDP collector를 의미하지 않는다.

## Commands

```bash
python3 scripts/meshsense_gui.py          # 브라우저 제어판
python3 scripts/meshsense_cli.py
python3 scripts/idf_bootstrap.py -y

python3 scripts/device_registry.py verify
python3 scripts/tx_registry.py verify

python3 scripts/measure_csi_hz.py \
  mac_collector_output/raw/YYYYMMDD/<HHMMSS>_<label>_s<id>
python3 scripts/visualize_csi.py \
  --session-dir mac_collector_output/raw/YYYYMMDD/<HHMMSS>_<label>_s<id>
python3 scripts/check_separability.py

# 학습 전처리: .csi 를 JSONL 로 내보낸 뒤 공식 전처리에 넣는다
python3 scripts/export_jsonl.py --print-labels
python3 model_train/preprocessing/preprocess_3rx.py \
  --raw-dir mac_collector_output/jsonl/raw/YYYYMMDD
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
| USB frame | little-endian **v4, 44-byte header, CRC-32** |
| `tx_seq` | cross-RX synchronization key |
| reader output | **raw I/Q 그대로 저장(`.csi`)**, on-device normalization 없음 |
| 유효 LLTF 톤 | `[1..26] + [38..63]` 52개 — `0`(DC), `27~37`(가드)은 상시 0 |
| 라벨 어휘 | `empty` / `static` / `motion` (`csi_store.LABELS` = 전처리 `LABEL_MAP`) |

## Data Contract

- Binary frame: `doc/data-schema.md`
- 수집 출력: `mac_collector_output/raw/YYYYMMDD/<HHMMSS>_<label>_s<id>/device_<id>.csi`
- 전처리 입력: `export_jsonl.py` 가 만드는 `mac_collector_output/jsonl/raw/YYYYMMDD/session_<id>/device_<id>.jsonl`
  (`record_schema_version=1`, `transport=usb_serial_jtag`, `csi_representation=raw_iq_amplitude`)
- **`.csi` 는 배타적 생성이라 append 되지 않는다.** 구 레이아웃의 append 모드는 실제로
  여러 run 을 한 파일에 섞어 데이터를 오염시켰다.
- RX별 `seq`와 `timestamp_us`는 공유 clock이 아니다. 다중 RX 정렬에는 `tx_seq`를 사용한다.

## 손대면 안 되는 것 (재발 방지)

- CSI 콜백 안에 동기 I/O 금지 — WiFi driver task 가 막혀 ~50Hz 로 붕괴
- 보드에서 진폭 계산·정규화 금지 — raw I/Q 를 그대로 보내고 host 가 처리
- `esp32s3_csi_send_poc` 의 `CONFIG_FREERTOS_HZ=1000` 삭제 금지 — 100이면 실효 50Hz
- 수집 시작 시 esptool 로 포트 프로브 금지 — 보드(TX 포함)가 리셋되어 `tx_seq` 가 끊긴다
- 시리얼 포트의 DTR/RTS 를 건드리지 말 것 — USB-Serial-JTAG 는 그 자체로 리셋된다
- 포트를 열자마자 기록하지 말 것 — 보드 링버퍼의 묵은 프레임을 먼저 비워야 한다
- `tx_seq` 를 정렬로 정리하지 말 것 — 역행은 TX 재부팅이고, 정렬하면 시간이 뒤집힌다

## Development Rules

- 현재 동작과 목표 설계를 한 문단에서 섞지 않는다. `CURRENT`, `PLANNED`, `HISTORICAL` 상태를 명시한다.
- data field, frame layout, sampling, firmware topology 변경은 관련 문서를 먼저 수정한다.
- 코드와 문서가 함께 검증되지 않으면 완료로 간주하지 않는다.
- 작업을 진행하면 `doc/sprint/` 의 현재 스프린트 문서에 시도·막힌 지점·결과를 실측 수치와 함께 남긴다.
- 사용자 실험 데이터와 registry/session 값은 명시적 요청 없이 변경하지 않는다.
