# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MeshSense — WiFi CSI(Channel State Information) 기반 실내 행동 인식 시스템. ESP32-S3 보드들이 CSI를 수집하고, Mac 수집기가 UDP로 데이터를 받아 JSONL로 저장하며, 후처리 파이프라인이 ML 모델 학습 텐서를 생성한다.

사용자 문서: `doc/README.md` (계층: `doc/overview/`, `doc/firmware/`, `doc/mac-collector/`, `doc/postprocessing/`).

## Architecture

```
TX/AP Node (esp32s3_tx_ap_node)
 └─ SoftAP + 10ms 주기 UDP heartbeat 브로드캐스트
 │
RX Nodes (esp32s3_csi_sender) × N대
 └─ TX/AP에 STA 접속 → CSI 콜백 수신
 └─ 전처리(이동평균 → z-score → 이상치 클리핑) → UDP 전송 (10ms 간격, 100Hz)
 │
Mac Collector (mac_collector/udp_collector_mvp.py)
 └─ UDP 수신 → 패킷 검증 → JSONL 저장
 │
Post-processing (add/)
 └─ JSONL 로드 → 64→52 매핑(선택) → 100Hz 보간 → 슬라이딩 윈도우 → (N, 3, 52, 200) 텐서
```

**UDP 패킷 프로토콜:** little-endian 40바이트 헤더 (`magic=0x4353`, `version=1`, `payload_type=1`) + `float32 csi_amp[sample_count]`. 상세: `doc/mac-collector/udp-packet-schema.md`.

**데이터 저장 경로:** `mac_collector_output/raw/YYYYMMDD/session_<id>/device_<id>.jsonl` (git 제외)

## Build & Run Commands

### ESP-IDF 펌웨어 (ESP32-S3)

프로젝트 로컬: `esp-idf/` (git submodule, **v5.2.2**) · 툴체인 `~/.espressif` · 마커 `프로젝트/.espressif/` (gitignore). 트러블슈팅: `doc/overview/esp-idf-troubleshooting.md`.

```bash
python scripts/meshsense_cli.py             # [1] 전체 가이드 (TX→수집기→RX)
python scripts/meshsense_cli.py --guide     # 가이드 바로 시작

git clone --recursive <repo>
cp scripts/meshsense_config.example.json scripts/meshsense_config.json
python scripts/idf_bootstrap.py -y          # 최초 1회 (submodule + install.sh esp32s3)

python scripts/device_registry.py verify
python scripts/flash_rx.py -p /dev/cu.usbmodemXXXX -y   # bootstrap 자동 포함
python scripts/flash_tx.py -p /dev/cu.usbmodemXXXX -y

# 전역 ~/esp/esp-idf 만 사용: --skip-idf-bootstrap
# 보드 전환 시: python scripts/flash_rx.py ... --clean -y
```

망 설정 SSOT: `scripts/meshsense_config.json` (`ap`, `collector`). RX/TX 플래시.
run `session_id` SSOT: `mac_collector/session_meta.yaml` — 수집기가 파싱해 `session_<id>/` 경로 결정 (펌웨어 session_id 없음, v1 패킷 필드=0).
RX: `device_registry.csv` + `flash_rx.py`. TX: `tx_registry.csv` + `flash_tx.py`.
TODO: `session_meta.yaml` `network:` ↔ `meshsense_config.json` 자동 동기화 (현재 수동).

TX/AP 파라미터는 `esp32s3_tx_ap_node/CMakeLists.txt`에서 CMake cache로 관리: `TX_AP_SSID`, `TX_AP_INTERVAL_MS`(기본 10), `TX_AP_CHANNEL` 등.

### Mac Collector (Python)

```bash
python mac_collector/udp_collector_mvp.py \
 --host 0.0.0.0 --port 9999 \
 --output-dir mac_collector_output \
 --device-registry-csv mac_collector/device_registry.csv \
 --session-meta mac_collector/session_meta.yaml
```

### Post-processing

```bash
# add/main.py: SESSION_DIR·RX_IDS 상단을 mac_collector_output/raw/.../session_<id> 에 맞게 수정
python add/main.py
```

상세: `doc/postprocessing/pipeline.md`

Python 환경: `.venv` (numpy, matplotlib). ESP-IDF는 별도 venv.

## Key Constants (100Hz 기준)

| 위치 | 상수 | 값 | 의미 |
|------|------|----|------|
| `csi_sender_main.c` | `SEND_INTERVAL_US` | 10000 | RX 전송 주기 (10ms) |
| `CMakeLists.txt` (TX) | `TX_AP_INTERVAL_MS` | 10 | TX 브로드캐스트 주기 (ms) |
| 펌웨어 UDP | `sample_count` | 최대 64 | RX CSI 진폭 개수 |
| `add/main.py` | `F_S` | 100 | 샘플링 주파수 (Hz) |
| `add/main.py` | `WINDOW` | 300 | 윈도우 크기 (2초) |
| `add/main.py` | `STRIDE` | 100 | 스트라이드 (1초) |
| `add/main.py` | `N_SUB` | 52 | 후처리 OFDM 서브캐리어 수 |

## Conventions

- 한국어 커밋 메시지 및 주석 사용
- RX `device_id` / `sta_mac`: `mac_collector/device_registry.csv` — `scripts/device_registry.py`, `scripts/flash_rx.py`
- TX `tx_node_id` / `chip_mac`: `mac_collector/tx_registry.csv` — `scripts/tx_registry.py`, `scripts/flash_tx.py`
- run/session: `session_meta.yaml` `session_id` (수집기 SSOT); 장치: `device_registry.csv`
