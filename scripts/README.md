# MeshSense 호스트 스크립트

**터미널이 익숙하지 않다면 브라우저 제어판을 쓰세요** — 보드 확인·플래시·수집·세션 관리·
진단을 한 화면에서 합니다.

```bash
python scripts/meshsense_gui.py
```

호스트 흐름은 ESP-NOW TX/RX 펌웨어 플래시, multi-RX 수집(USB 직결 또는 싱크 경유), 세션 관리,
학습, 실시간 판단을 다룹니다.

## 권장 진입점

```bash
python3 scripts/meshsense_cli.py
```

메뉴:

1. 보드 플래시: USB MAC으로 TX/RX 자동 판별 (RX는 USB 모드)
2. 수집: 라벨·시간 입력, 연결된 포트마다 reader 병렬 실행
3. 블록 순서 뽑기: 세 클래스 순서를 무작위로
4. 세션 보기: 날짜별 · 품질 원인
5. 모델 학습: 세션 선택
6. 실시간: 모델·세션을 골라 `realtime/ui.py` 실행
7. 세션 메타 편집: 브라우저 폼
8. 보드 관리: registry 목록·상세·등록·삭제·검증
9. 종료

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
| `mac_collector/session_meta.yaml` | 실험 조건과 다음 수집의 기본 라벨(`label_target`). `session_id`는 자동 순번 |
| `mac_collector/flash_state.json` | CLI의 로컬 플래시 상태 표시 |
| `mac_collector/block_order.json` | 블록 순서와 진행 상태 (제어판·CLI 공용) |

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
| `csi_serial_reader.py` | RX binary stream → `device_<id>.csi` |
| `device_registry.py`, `registry.py` | RX registry CRUD/조회 |
| `tx_registry.py` | TX registry CRUD/조회 |
| `esptool_mac.py` | USB 연결 보드 MAC 확인 |
| `idf_bootstrap.py` | ESP-IDF submodule/toolchain 준비 |
| `idf_env.py`, `idf_paths.py`, `idf_util.py` | ESP-IDF 실행 환경 |
| `flash_state.py` | CLI 플래시 상태 |
| `measure_csi_hz.py` | `.csi`(구 JSONL 폴백) 수집률·gap 측정과 품질 판정 |
| `visualize_csi.py` | RX별 waterfall PNG |
| `visualize_tx_seq_overlap.py` | RX별 `tx_seq` 전체 범위·존재·누락 PNG (JSONL 입력) |

## RX별 `tx_seq` 범위 시각화

한 session의 RX 101·102·103에 대해 각 JSONL의 최소~최대 `tx_seq` 범위와
범위 안의 실제 존재·누락을 한 PNG와 터미널 표로 보여 준다. 공통 범위를
계산하거나 누락을 보간하지 않는 진단 전용 도구다. `.csi`를 직접 읽지 않으므로
`export_jsonl.py`로 내보낸 디렉터리를 넣는다.

```bash
.venv/bin/python scripts/visualize_tx_seq_overlap.py \
  --session-dir mac_collector_output/jsonl/raw/<YYYYMMDD>/session_<id>
```

상세 동작은 [후처리 문서의 RX별 tx_seq 범위 시각화](../doc/postprocessing.md#4-current-rx별-tx_seq-범위-시각화)를 참조한다.

## 수집률 측정

RX callback 시각 기준 수집률을 확인하려면 다음처럼 실행합니다.

```bash
python3 scripts/measure_csi_hz.py \
  --gap-ms 200 \
  mac_collector_output/raw/YYYYMMDD/<HHMMSS>_<label>_s<id>
```

`--gap-ms`는 긴 수신 공백으로 셀 기준값이며 기본값은 200ms입니다.

RX가 재부팅되면(`.csi`는 `boot_id` 변화, 구 JSONL은 `seq`와 `timestamp_us`의 동시 감소)
그전 데이터는 버립니다. 마지막 재부팅 이후 데이터만 계산하고 결과는 장치별 한 행의 표로
출력합니다. 끝에 `tx_back`·`tx_cov`·cross-RX 공통 `tx_seq`와 품질 판정(통과/불합격)을 붙입니다.
표에는 재부팅 횟수, 남은 record 수, RX 기준 Hz, sequence 기반 추정 Hz, 수신
간격, 큰 gap, sequence 누락·중복·순서 이상을 표시합니다.

## 수집 시 주의

- TX는 ESP-NOW를 계속 보내도록 전원을 유지합니다.
- RX는 모두 USB로 Mac에 연결합니다.
- RX 포트를 `idf.py monitor`와 reader가 동시에 열 수 없습니다.
- `session_id`는 자동 순번이라 손으로 바꾸지 않습니다.
- `.csi`는 배타적 생성이라 같은 파일에 다시 쓰면 append 대신 에러가 납니다.

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
| `session_form.py` | `session_meta.yaml` 편집 웹 폼 (제어판 수집 탭 '이번 실험 설정' 과 동일) |
| `measure_csi_hz.py` | 세션 품질 요약 (hz·crc_fail·boot_changes·tx_back·cross-RX) |
| `_idf_flash.py` / `_collect_run.py` | 제어판이 백그라운드로 돌리는 플래시·수집 러너 |
| `block_order.py` | 블록 순서와 진행 상태 (`mac_collector/block_order.json`) — 제어판·CLI 공용 |
| `train_sessions.py` | 학습용 세션 목록·선택 규칙·제외 세션 |
| `_train_run.py` | 학습 러너 — 프리셋·진행률·남은 시간 |

**제어판 없이 직접 부를 수 있는 명령** (서브커맨드 CLI 는 만들지 않는다):

```bash
python scripts/_train_run.py --preset balanced --sessions 20260916_31 ... --eval 20260919
python realtime/ui.py --session <세션> --follow          # 또는 --speed 2 로 재생
python scripts/export_jsonl.py --print-labels
```

- **수집은 포트를 프로브하지 않는다.** 보드가 2초마다 보내는 IDENT 의 eFuse MAC 으로
  `device_id` 를 정한다. esptool 프로브는 DTR/RTS 로 보드를 리셋시켜 TX 의 `tx_seq` 까지
  되감았다 (플래시 경로에서는 esptool 사용이 정상).
- `session_id` 는 기존 세션 최댓값+1 자동 부여.
