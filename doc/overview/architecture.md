# 아키텍처

MeshSense는 **수집 파이프라인 2개**와 **공통 후처리·학습 경로**로 구성됩니다.
CLI(`python scripts/meshsense_cli.py`) 첫 화면의 [1]/[2]가 이 두 파이프라인과 1:1 대응합니다.

## 데이터 흐름

```text
[1] USB 수집 파이프라인 (모델 학습 데이터 — 유일한 정본 경로)
    TX (esp32s3_csi_send_poc)
      └─ ESP-NOW broadcast 10ms (tx_seq 카운터 탑재)
    RX (esp32s3_csi_recv_poc) × N대
      └─ CSI 콜백 → ring buffer → USB-Serial-JTAG 바이너리 프레임 v3 (100Hz)
      └─ IDENT 프레임(2초) 로 자기 eFuse MAC 을 알림 → 호스트가 device_id 결정
    Mac (scripts/csi_serial_reader.py × N)
      └─ magic+CRC32 검증 → device_<id>.csi 에 프레임 그대로 저장 (raw I/Q 보존)
      └─ 라벨·품질통계 → session.json

[2] AP 실시간 수집 파이프라인 (SoftAP + UDP) — **deprecated**
    실측 0.18~22.8Hz 로 100Hz 목표에 못 미치고 tx_seq 유효 데이터가 0건이다.
    원인은 AP/STA association + DTIM 게이팅 + 자극/데이터 채널 공유라는 구조적 문제
    ([csi-rate-troubleshooting.md](csi-rate-troubleshooting.md)). 추가로 RX 온디바이스
    z-score 가 시간축 진폭 변동(=움직임 신호 본체)을 지워 USB 데이터와 스케일도 다르다.
    실시간 경로는 **ESP-NOW 업링크 + USB 싱크 보드**로 재설계 예정
    ([sprint/2026-08-collection-hardening.md](../sprint/2026-08-collection-hardening.md)).

공통 하류
    세션 (mac_collector_output/raw/YYYYMMDD/<HHMMSS>_<label>_s<id>/)
      └─ scripts/csi_store.py : 프레임 파싱 → 진폭 → LLTF 유효 톤 52개 선별
      └─ model_train/model/Preprocessing.py : tx_seq 격자 보간 → 윈도잉 (라벨은 session.json)
      └─ model_train/model/build_dataset.py : 여러 세션 → dataset.npz (세션 단위 split)
      └─ X = (N, 300, RX수×52) → model_train/model/LSTM.py 학습
```

상세: [usb-collection.md](../pipeline/usb-collection.md) ·
[sprint/2026-08-collection-hardening.md](../sprint/2026-08-collection-hardening.md)

## 주요 상수 (SSOT — 이 표가 유일한 정본)

| 소스 | 상수 | 값 | 의미 |
|------|------|----|------|
| `esp32s3_csi_send_poc/main/app_main.c` | `CONFIG_SEND_FREQUENCY` | 100 | TX ESP-NOW 송신 Hz (`usleep(10000)`) |
| `esp32s3_csi_send_poc/sdkconfig.defaults` | `CONFIG_FREERTOS_HZ` | 1000 | **필수** — 100이면 usleep 반올림으로 50Hz가 됨 |
| 양 PoC 펌웨어 | `CONFIG_LESS_INTERFERENCE_CHANNEL` | 11 | 고정 채널 |
| 양 PoC 펌웨어 | `CONFIG_WIFI_BANDWIDTH` | HT20 | LLTF 64 SC × I/Q = raw 128B |
| `esp32s3_csi_recv_poc/main/app_main.c` | `CSI_FRAME_VERSION` | 4 | 시리얼 프레임 v4, 헤더 44B + CRC32 |
| `esp32s3_csi_recv_poc/main/app_main.c` | `CSI_TX_SEQ_OFFSET` | 15 | ESP-NOW payload 내 tx_seq 위치 (`payload_len==19` 경계) |
| `esp32s3_csi_recv_poc/main/app_main.c` | `CSI_IDENT_PERIOD_MS` | 2000 | IDENT(보드 자기소개) 주기 |
| `scripts/csi_store.py` | `LLTF_DATA_IDX` | `[1..26] + [38..63]` | **유효 LLTF 데이터 톤 52개.** 0(DC)·27~37(가드)은 상시 0 |
| `scripts/csi_store.py` | `CSI_FRAME_SIZE` | 172 | 저장 프레임 1개 = 헤더 44B + raw 128B |
| `scripts/csi_store.py` | `LABELS` | empty/static/action | 3-class 라벨 SSOT (0/1/2) |
| `model_train/model/Preprocessing.py` | `F_S` | 100 | 샘플링 주파수 (Hz), tx_seq 1스텝 = 10ms |
| `model_train/model/Preprocessing.py` | `WINDOW` / `STRIDE` | 300 / 30 | 3초 윈도, 0.3초 stride |
| `model_train/model/Preprocessing.py` | 텐서 shape | `(N, 300, RX수×52)` | RX 1대=52, 3대=156 feature |
| 세션 경로 | `raw/<YYYYMMDD>/<HHMMSS>_<label>_s<id>/` | — | 시각이 들어가 충돌 불가. `.csi`는 배타적 생성 |
| `scripts/csi_session.py` | `next_session_id()` | 자동 | 기존 세션 순번 최댓값+1. 사람이 관리하지 않음 |

## 설정 SSOT 4종

| 파일 | 담당 | 소비처 |
|------|------|--------|
| `<세션>/session.json` | **수집 세션의 라벨·품질 통계 (라벨 SSOT)** | `Preprocessing.py`, `build_dataset.py`, `measure_csi_hz.py` |
| `mac_collector/device_registry.csv` | RX `device_id` ↔ `sta_mac` (IDENT MAC 조회표) | reader, CLI |
| `mac_collector/session_meta.yaml` | 실험 조건 기록 (방·운영자·메모) + 라벨 기본값 | CLI (수집 시 세션에 스냅샷 복사). 편집은 `scripts/session_form.py` 웹 폼 |
| `mac_collector/tx_registry.csv` | TX `tx_node_id` ↔ `chip_mac` | CLI 플래시 |
| `scripts/meshsense_config.json` | AP 파이프라인 망 설정 (**deprecated 경로 전용**) | `flash_tx.py` / `flash_rx.py` |

- **라벨은 수집 시점에 `session.json`에 박힌다.** `session_meta.yaml`의
  `experiment.label_target`은 CLI 프롬프트의 **기본값**일 뿐 정본이 아니다.
- `session_meta.yaml`은 run 식별 역할에서 물러났다 — 세션 디렉터리 이름(시각+라벨)이 그 역할을 하고,
  `session_id` 순번은 `next_session_id()`가 파일시스템에서 계산한다. YAML 을 손으로 고칠 일이 없도록
  `python scripts/session_form.py` 가 브라우저 폼으로 이 파일을 대신 쓴다.

## Python 환경 (2개 분리)

| 환경 | 용도 | 준비 |
|------|------|------|
| 프로젝트 `.venv` | 수집기·시각화·후처리 (numpy, matplotlib, pyserial) | [quickstart.md](quickstart.md) |
| ESP-IDF venv (`~/.espressif`) | 펌웨어 빌드 (`idf_bootstrap.py`가 관리) | 자동 |

LSTM 학습(`model_train/model/LSTM.py`)은 추가로 **PyTorch**가 필요합니다 (`pip install torch`).
