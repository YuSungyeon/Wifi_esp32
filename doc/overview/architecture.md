# 아키텍처

## 데이터 흐름

```text
TX/AP Node (esp32s3_tx_ap_node)
  └─ SoftAP + 10ms 주기 UDP heartbeat 브로드캐스트 (포트 3333)
       │
RX Nodes (esp32s3_csi_sender) × N대
  └─ TX/AP에 STA 접속 → CSI 콜백 수신
  └─ 전처리(이동평균 → z-score → 이상치 클리핑) → UDP 전송 (10ms 간격, 100Hz)
       │
Mac Collector (mac_collector/udp_collector_mvp.py)
  └─ UDP 수신 → 패킷 검증 → JSONL 저장
       │
Post-processing (add/)
  └─ JSONL 로드 → (선택) 64→52 서브캐리어 매핑 → 100Hz 보간 → (N, 3, 52, 200) 텐서
```

**UDP 패킷:** little-endian 40바이트 헤더 (`magic=0x4353`, `version=1`, `payload_type=1`) + `float32 csi_amp[sample_count]`. 상세는 [udp-packet-schema.md](../mac-collector/udp-packet-schema.md).

**데이터 저장 경로:** `mac_collector_output/raw/YYYYMMDD/session_<id>/device_<id>.jsonl` (git 제외)

## 주요 상수 (100Hz 기준)

| 위치 | 상수 | 값 | 의미 |
|------|------|----|------|
| `csi_sender_main.c` | `SEND_INTERVAL_US` | 10000 | RX 전송 주기 (10ms, 100Hz) |
| `esp32s3_tx_ap_node/CMakeLists.txt` | `TX_AP_INTERVAL_MS` | 10 | TX UDP 브로드캐스트 주기 (ms) |
| 펌웨어 UDP | `sample_count` | 최대 64 | RX CSI 진폭 개수 |
| `add/main.py` | `F_S` | 100 | 후처리 샘플링 주파수 (Hz) |
| `add/main.py` | `WINDOW` / `STRIDE` | 200 / 100 | 2초 윈도, 1초 stride |
| `add/main.py` | `N_SUB` | 52 | 모델 입력 서브캐리어 수 |

RX는 최대 64개 `csi_amp`를 보내고, PC 후처리에서 유효 52개로 줄입니다. 매핑 절차는 [pipeline.md](../postprocessing/pipeline.md).

## Python 환경

```bash
python -m venv .venv && source .venv/bin/activate
pip install numpy matplotlib
```

ESP-IDF 빌드는 IDF 전용 Python venv를 사용합니다 (프로젝트 `.venv`와 분리).

## 발표 자료

중간보고 PPT·PDF 등은 `presentations/`에 두며 git에는 포함하지 않습니다 (`.gitignore`).

## 운영 규칙

- 망 설정: `scripts/meshsense_config.json` (`ap`, `collector`) — TX/RX 플래시
- run `session_id`: `mac_collector/session_meta.yaml` — 수집기 SSOT (펌웨어 없음)
- RX: `mac_collector/device_registry.csv` — `scripts/device_registry.py`, `scripts/flash_rx.py`
- TX: `mac_collector/tx_registry.csv` — `scripts/tx_registry.py`, `scripts/flash_tx.py`
- **TODO:** `session_meta.yaml` `network:` ↔ `meshsense_config.json` 자동 동기화 (현재 수동, [collector.md](../mac-collector/collector.md))
- 실험 조건: `mac_collector/session_meta.yaml`
