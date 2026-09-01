# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

MeshSense — WiFi CSI 기반 실내 행동 인식 시스템 (3-class: empty / static / action).
ESP32-S3가 CSI를 모으고, Mac이 raw I/Q 프레임으로 저장하며, `model_train/`이 LSTM 학습 텐서를 만든다.

**학습 데이터 수집은 USB 파이프라인이 유일한 정본 경로다.**
AP(SoftAP+UDP) 파이프라인은 deprecated — 실측 0.18~22.8Hz로 100Hz 목표에 못 미치고
온디바이스 z-score가 시간축 진폭 변동을 지운다. 신규 작업을 얹지 말 것.

**아키텍처·상수·데이터 흐름은 `doc/overview/architecture.md`가 유일한 정본이다** — 이 파일에 복사하지 말 것.
문서 인덱스·저장소 레이아웃: `doc/README.md`. 셋업 명령: `doc/overview/quickstart.md`.
진행 중 작업 로그: `doc/sprint/`.

## 자주 쓰는 명령

```bash
python scripts/meshsense_gui.py                          # 브라우저 제어판 (비개발자용 권장 경로)
python scripts/meshsense_cli.py                          # 메뉴 CLI: [1] USB 수집
python scripts/check_separability.py                     # 3-class 분리 가능성 진단 (torch 불필요)
python scripts/idf_bootstrap.py -y                       # ESP-IDF 준비 (최초 1회)
python scripts/measure_csi_hz.py <세션 디렉터리>          # 수집 품질 진단
python scripts/visualize_csi.py --session-dir <세션>      # CSI 워터폴 PNG
python model_train/model/build_dataset.py                # 여러 세션 → dataset.npz
python model_train/model/LSTM.py --epochs 20             # 학습 (PyTorch 필요)
```

- PoC 펌웨어(`esp32s3_csi_send_poc`/`esp32s3_csi_recv_poc`)는 CMake 파라미터가 없어
  `flash_*.py` 대상이 아님 — CLI `[1] 보드 플래시` 또는 `idf.py` 직접 사용.
  **RX 펌웨어는 보드마다 동일한 바이너리** (`device_id`는 IDENT MAC으로 호스트가 결정)
- ESP-IDF는 프로젝트 로컬 submodule `esp-idf/`(v5.2.2), 툴체인은 `~/.espressif`.
  트러블슈팅: `doc/overview/esp-idf-troubleshooting.md`
- Python 환경 2개 분리: 프로젝트 `.venv`(수집·후처리) / ESP-IDF venv(빌드)

## SSOT 위치 (수정 시 여기만)

- 상수표: `doc/overview/architecture.md`
- 프레임 규격: C는 `esp32s3_csi_recv_poc/main/app_main.c`의 `csi_frame_header_t`,
  Python은 `scripts/csi_store.py`의 `HEADER_DTYPE` — 한쪽만 고치면 C의 `offsetof` assert가 터진다
- 라벨·유효 서브캐리어: `scripts/csi_store.py` (`LABELS`, `LLTF_DATA_IDX`)
- 세션 라벨: 각 세션의 `session.json` (`session_meta.yaml`은 기본값 제공용일 뿐)
- `session_id`: 자동 순번 (`csi_session.next_session_id`) — YAML 에 두지 않는다
- RX/TX 보드: `mac_collector/device_registry.csv` / `tx_registry.csv`
  (공통 로직: `scripts/registry_core.py`)

## 손대면 안 되는 것 (재발 방지)

- **CSI 콜백 안에 동기 I/O 금지** — WiFi driver task가 막혀 즉시 ~50Hz로 붕괴
- **보드에서 진폭 계산·정규화 금지** — raw I/Q를 그대로 보내고 호스트가 처리
- **`esp32s3_csi_send_poc`의 `CONFIG_FREERTOS_HZ=1000` 삭제 금지** — 100이면 실효 50Hz
- **수집 시작 시 esptool로 포트 프로브 금지** — 보드(TX 포함)가 리셋되어 `tx_seq`가 끊긴다
- **세션 파일을 append 모드로 열지 말 것** — 서로 다른 런이 한 파일에 섞인다
- **시리얼 포트의 DTR/RTS 를 건드리지 말 것** — USB-Serial-JTAG 는 그 자체로 리셋된다
- **포트를 열자마자 기록하지 말 것** — 보드 링버퍼의 묵은 프레임을 먼저 비워야 한다
- **`tx_seq` 를 정렬로 정리하지 말 것** — 역행은 TX 재부팅이고, 정렬하면 시간이 뒤집힌다. 거부해야 한다

## Conventions

- 한국어 커밋 메시지 및 주석 사용
- 문서에 상수·메뉴 번호를 복붙하지 말고 `architecture.md` 표 또는 코드 링크로 위임
- 작업을 진행하면 `doc/sprint/`의 현재 스프린트 문서에 시도·막힌 지점·결과를 실측 수치와 함께 남길 것
