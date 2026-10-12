# MeshSense 호스트 스크립트

**터미널이 익숙하지 않다면 브라우저 제어판을 쓰세요** — 보드 확인·플래시·수집·세션 관리·
진단을 한 화면에서 합니다.

```bash
python scripts/meshsense_gui.py
```

현재 호스트 흐름은 ESP-NOW TX/RX 펌웨어 플래시와 multi-RX USB 수집만 지원합니다.

## 학습 모델로 상태 판단

`.venv/bin/python -m pip install -r requirements-robust.txt`로 판단 의존성을 준비한다.
GUI **실시간** 탭에서 **사용할 모델**을 고르고 **실시간 시작**을 누르면 현재 SINK 수집과 모델 판단을 함께 켠다.
세션 선택은 필요 없다. **판단 정지**는 그 실행의 수집·판단을 함께 끈다.
과거 자료는 세션을 고르고 **녹화 재생**한다. **수집 파일 따라 읽기**는 외부에서
저장 중인 세션을 읽는 별도 모드다. TX/RX/SINK 펌웨어를 자동으로 바꾸지 않는다.

```bash
# 현재 보드 신호를 수집하면서 판단 (Ctrl+C로 함께 종료)
.venv/bin/python scripts/meshsense_cli.py --live --port /dev/cu.usbmodem101 --plain
# 보드 없이 저장 데이터 확인
.venv/bin/python scripts/meshsense_cli.py --realtime mac_collector_output/raw/<날짜>/<세션> --plain
# 다른 터미널/GUI에서 수집 중인 동일 세션을 따라 읽기
.venv/bin/python scripts/meshsense_cli.py --realtime mac_collector_output/raw/<날짜>/<세션> --follow
# 위상 60초 시험 후보 선택 (실제 SINK 포트로 바꾼다)
.venv/bin/python scripts/meshsense_cli.py --live --port <SINK포트> --model-kind phase60 --plain
```

기본 모델은 `model_train/robust/model.pt`(진폭 30초)다. `--model-kind phase60`은
`model_train/phase60/model_60sessions.pt`(위상 60초 시험 후보)를 선택한다. 두 경로의 기본 이동은 0.3초다.
각 창의 최대 확률 클래스를 바로 표시한다. 확신 70%·클래스 유지 시간은 쓰지 않는다.
최초 30초/60초 준비와 신호 누락 때의 판단 정지는 유지한다. GUI 판단 화면 갱신은 300ms다.
원본 대신 `mac_collector_output/realtime/`에 실행 기록을 남긴다. 현재 보드 모드의
raw I/Q는 실행 폴더의 `capture/`, 판단은 `prediction/`에 저장된다. 정답 라벨 없이
학습 데이터와 분리하므로 기존 세션·라벨·registry를 수정하지 않는다.
입력 품질·수신 중단 처리와 검증 한계는 [실시간 상태 판단](../doc/realtime-inference.md)을 따른다.
매 판단 점수·입력 순번·모델 해시·정지 사유는 `realtime_predictions.jsonl`에도 저장된다.
모델 변경은 판단 정지 후 새 실행으로 시작한다. 이미 실행 중인 GUI는 작업 종료 후 서버를 재실행해야 새 선택 화면이 적용된다.

## 권장 진입점

```bash
python3 scripts/meshsense_cli.py
```

메뉴:

1. 전체 가이드: 환경 → TX → RX → USB 수집
2. 보드 플래시: USB MAC으로 TX/RX 자동 판별
3. 보드 관리: registry 목록·등록·삭제·검증
4. USB 시리얼 수집: 연결된 RX reader 병렬 실행
5. 사전 점검: ESP-IDF, registry, session metadata, pyserial 확인

## 최초 환경

```bash
git submodule update --init esp-idf
python3 scripts/idf_bootstrap.py -y
python3 -m pip install pyserial
```

시각화가 필요하면:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-viz.txt
```

## Registry와 session

| 파일 | 역할 |
|---|---|
| `mac_collector/tx_registry.csv` | TX USB chip MAC과 `tx_node_id` |
| `mac_collector/device_registry.csv` | RX USB MAC과 `device_id`, 설치 정보 |
| `mac_collector/session_meta.yaml` | run `session_id`, label, 환경, 수집 방식 |
| `mac_collector/flash_state.json` | CLI의 로컬 플래시 상태 표시 |

Registry CLI:

```bash
python3 scripts/tx_registry.py verify
python3 scripts/device_registry.py verify
python3 scripts/tx_registry.py add --port /dev/cu.usbmodem101 --board-name TX1
python3 scripts/device_registry.py add --port /dev/cu.usbmodem102 --board-name RX101
```

## 파일별 책임

| 파일 | 책임 |
|---|---|
| `meshsense_cli.py` | 공식 interactive workflow |
| `csi_serial_reader.py` | RX binary stream → JSONL |
| `device_registry.py`, `registry.py` | RX registry CRUD/조회 |
| `tx_registry.py` | TX registry CRUD/조회 |
| `esptool_mac.py` | USB 연결 보드 MAC 확인 |
| `idf_bootstrap.py` | ESP-IDF submodule/toolchain 준비 |
| `idf_env.py`, `idf_paths.py`, `idf_util.py` | ESP-IDF 실행 환경 |
| `flash_state.py` | CLI 플래시 상태 |
| `measure_csi_hz.py` | JSONL 수집률·gap 측정 |
| `visualize_csi.py` | RX별 waterfall PNG |
| `visualize_tx_seq_overlap.py` | RX별 `tx_seq` 전체 범위·존재·누락 PNG |

## RX별 `tx_seq` 범위 시각화

한 session의 RX 101·102·103에 대해 각 JSONL의 최소~최대 `tx_seq` 범위와
범위 안의 실제 존재·누락을 한 PNG와 터미널 표로 보여 준다. 공통 범위를
계산하거나 누락을 보간하지 않는 진단 전용 도구다.

```bash
.venv/bin/python scripts/visualize_tx_seq_overlap.py \
  --session-dir mac_collector_output/raw/20260616/session_1
```

상세 동작은 [후처리 문서의 RX별 tx_seq 범위 시각화](../doc/postprocessing.md#4-current-rx별-tx_seq-범위-시각화)를 참조한다.

## 수집률 측정

RX callback 시각 기준 수집률을 확인하려면 다음처럼 실행합니다.

```bash
python3 scripts/measure_csi_hz.py \
  --gap-ms 200 \
  mac_collector_output/raw/YYYYMMDD/session_<id>
```

`--gap-ms`는 긴 수신 공백으로 셀 기준값이며 기본값은 200ms입니다.

RX가 재부팅돼 `seq`와 `timestamp_us`가 함께 감소하면 그전 데이터는 버립니다.
마지막 재부팅 이후 데이터만 계산하고 결과는 장치별 한 행의 표로 출력합니다.
표에는 재부팅 횟수, 남은 record 수, RX 기준 Hz, sequence 기반 추정 Hz, 수신
간격, 큰 gap, sequence 누락·중복·순서 이상을 표시합니다.

## 수집 시 주의

- TX는 ESP-NOW를 계속 보내도록 전원을 유지합니다.
- RX는 모두 USB로 Mac에 연결합니다.
- RX 포트를 `idf.py monitor`와 reader가 동시에 열 수 없습니다.
- `session_meta.yaml`의 `session_id`를 바꾼 뒤 수집합니다.
- 같은 날짜/session ID의 JSONL은 append되므로 ID를 재사용하지 않습니다.

상세 절차는 [빠른 시작](../doc/quickstart.md), data contract는 [serial frame schema](../doc/data-schema.md)를 참조하세요.

## 수집 계층 도구

> 상태: **CURRENT**

| 파일 | 설명 |
|------|------|
| `meshsense_gui.py` | 브라우저 제어판 (stdlib only, 127.0.0.1 바인딩) |
| `csi_store.py` | **프레임 규격·검증·진폭·유효 서브캐리어의 Python 단일 소스** |
| `csi_session.py` | 세션 디렉터리·manifest(`session.json`, 라벨 SSOT)·`session_id` 자동 순번 |
| `csi_serial_reader.py` | reader: 시리얼 프레임 → `device_<id>.csi`. 싱크 경유 시 `rx_id` 로 여러 RX 를 갈라 씀 (`--identify` 로 식별만) |
| `export_jsonl.py` | `.csi` → JSONL record schema v1 (전처리 입력) |
| `check_separability.py` | 3-class 분리 가능성 진단 (세션 단위 LOSO, torch 불필요) |
| `ablation.py` | 베이스라인·ablation 하네스 — RX 대수·윈도·서브캐리어·위상을 같은 베이스라인으로 비교, 조건 단위 분할 지원 |
| `session_form.py` | `session_meta.yaml` 편집 웹 폼 (제어판 '실험 정보' 탭과 동일) |
| `measure_csi_hz.py` | 세션 품질 요약 (hz·crc_fail·boot_changes·tx_back·cross-RX) |
| `_idf_flash.py` / `_collect_run.py` | 제어판이 백그라운드로 돌리는 플래시·수집 러너 |

- **수집은 포트를 프로브하지 않는다.** 보드가 2초마다 보내는 IDENT 의 eFuse MAC 으로
  `device_id` 를 정한다. esptool 프로브는 DTR/RTS 로 보드를 리셋시켜 TX 의 `tx_seq` 까지
  되감았다 (플래시 경로에서는 esptool 사용이 정상).
- `session_id` 는 기존 세션 최댓값+1 자동 부여.
