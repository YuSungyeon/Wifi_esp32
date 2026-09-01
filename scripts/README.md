# MeshSense 호스트 스크립트

**터미널이 익숙하지 않다면 브라우저 제어판을 쓰세요** — 보드 확인·플래시·수집·세션 관리·
진단을 한 화면에서 합니다.

```bash
python scripts/meshsense_gui.py
```

현재 호스트 흐름은 ESP-NOW TX/RX 펌웨어 플래시와 multi-RX USB 수집만 지원합니다.

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
| `csi_serial_reader.py` | USB reader: 시리얼 프레임 → `device_<id>.csi` (`--identify` 로 식별만) |
| `export_jsonl.py` | `.csi` → JSONL record schema v1 (전처리 입력) |
| `check_separability.py` | 3-class 분리 가능성 진단 (세션 단위 LOSO, torch 불필요) |
| `session_form.py` | `session_meta.yaml` 편집 웹 폼 (제어판 '실험 정보' 탭과 동일) |
| `measure_csi_hz.py` | 세션 품질 요약 (hz·crc_fail·boot_changes·tx_back·cross-RX) |
| `_idf_flash.py` / `_collect_run.py` | 제어판이 백그라운드로 돌리는 플래시·수집 러너 |

- **수집은 포트를 프로브하지 않는다.** 보드가 2초마다 보내는 IDENT 의 eFuse MAC 으로
  `device_id` 를 정한다. esptool 프로브는 DTR/RTS 로 보드를 리셋시켜 TX 의 `tx_seq` 까지
  되감았다 (플래시 경로에서는 esptool 사용이 정상).
- `session_id` 는 기존 세션 최댓값+1 자동 부여.
