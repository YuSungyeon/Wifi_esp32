# 제어판·CLI 흐름 설계

> 상태: **OFFICIAL DESIGN** (2026-10-12 승인, 구현 후 CURRENT 로 전환)
> 범위: `scripts/meshsense_gui.py`(제어판), `scripts/meshsense_cli.py`(터미널 메뉴), `scripts/csi_session.py`(품질 문장)

## 1. 누가 쓰는가

| 화면 | 1순위 사용자 | 목표 |
|---|---|---|
| 제어판 (GUI) | 비개발자 수집 담당자 | 문서를 보지 않고도 보드 → 수집 → 학습 → 실시간까지 혼자 끝낸다 |
| CLI | 개발자 | 번호 메뉴는 유지한다. 빠진 단계(세션 보기·실시간)만 추가한다 |

둘의 기능을 1:1 로 맞추지 않는다. 터미널로 나가야 끝나는 작업은 비개발자 제어판에서 뺀다.

## 2. 제어판

### 2.1 단계 띠 (모든 탭 위에 항상 보임)

서버가 `/api/state` 에 `steps` 를 넣는다. 다섯 칸, 각 칸은 `key · state · text` 세 값이다.
`state` 는 `ok | todo | current` 이고 `current` 는 **첫 번째 `todo`** 한 칸뿐이다. 칸을 누르면
해당 탭이 열린다. "지금 할 일" 같은 안내 문장은 두지 않는다 — 강조된 칸이 그 역할이다.

| key | ok 조건 | text 예 |
|---|---|---|
| `env` | pyserial import 가능, `.venv` 패키지(§2.3 의 세 개) 모두 있음 | `pyserial · .venv · torch` / `torch 없음` |
| `boards` | 등록된 RX ≥ 1 이고 미등록 RX 가 없음 | `RX 3대` / `미등록 1대` |
| `collect` | `empty`·`static`·`motion` 각각 세션 ≥ 2 | `오늘 7세션 · 다음 s48` |
| `train` | 학습한 모델 ≥ 1 | `모델 3개 · 최근 10/12` |
| `realtime` | 실시간 판단이 실행 중 | `움직임 · 94%` / `대기` |

`env` 의 ESP-IDF 는 띠에 넣지 않는다 — 플래시할 때만 필요하고, 수집 담당자는 대개 플래시된
보드를 받는다. ESP-IDF 는 보드 탭의 환경 점검 카드에서만 본다.

### 2.2 탭 순서와 기본 탭

`실험 정보 · 보드 · 수집 · 세션 · 학습 · 실시간 · 진단`. 기본 탭은 **수집**이다 — 담당자가 하루에
가장 자주 여는 탭이다. 블록 순서 뽑기는 수집 탭 안에 그대로 둔다.

### 2.3 보드 탭

- **환경 점검 카드** 세 줄: pyserial / `.venv` 패키지(numpy·matplotlib·torch) / ESP-IDF
  (`esp-idf/export.sh` 존재). 각 줄은 `통과 | 없음`. ESP-IDF 가 없으면 **준비하기** 버튼이
  `scripts/idf_bootstrap.py -y` 를 백그라운드 작업(`JOB`)으로 돌린다.
- **미등록 RX 행**에 `RX<다음 id> 로 등록` 버튼. 이름은 브라우저 `prompt()` 로 묻고 기본값은
  `RX<id>`. `POST /api/register {sta_mac, name}` → `device_registry.py add --mac … --board-name …`
  → 다시 검색. **포트를 프로브하지 않는다** — MAC 은 IDENT 로 이미 안다(CLAUDE.md 재발 방지).
- TX 행 문구: `TX 또는 미플래시` + 보조문 `TX 라면 정상`. TX 는 USB 로 IDENT 를 보내지 않으므로
  펌웨어를 바꾸지 않는 한 구분할 수 없다. 문구로만 푼다.

### 2.4 세션 탭 — 품질 문장

`csi_session.quality(devices) -> (level, text)` 하나가 정본이다. `level ∈ ok | check | recollect`.
GUI 세션 표와 CLI 수집 직후 판정이 같은 함수를 쓴다. 여러 조건이 겹치면 가장 센 level 하나와
문장들을 ` / ` 로 잇는다.

| 조건 (RX 하나라도) | level | 문장 |
|---|---|---|
| `tx_back > 0` | recollect | `TX 가 수집 중 재부팅했습니다 — 시간 격자가 깨져 같은 라벨로 다시 찍으세요` |
| `boot_changes > 0` | recollect | `RX<id> 가 수집 중 재부팅했습니다 — 전원·케이블 확인 후 같은 라벨로 다시 찍으세요` |
| `crc_fail > 0` | check | `RX<id> CRC 실패 <n>건 — 케이블 확인` |
| `seq_gap > frames × 1%` | check | `RX<id> 손실 <x>% — 학습엔 쓸 수 있으나 무선 경합 의심` |

기준값은 지금 `session_rows()` 가 쓰는 것과 같다(바꾸지 않는다). 표의 태그는 `정상 | 확인 | 재수집`.

### 2.5 실시간 탭 — 수집과 동시 실행

- 백그라운드 작업 슬롯을 둘로 나눈다: `JOB`(플래시·수집·학습·진단·IDF 준비) 과 `RT_JOB`(실시간).
  실시간 시작·중지는 `RT_JOB` 만 보고, 다른 탭의 `busy` 는 `JOB` 만 본다.
- **따라가기**는 세션을 고르지 않는다. `mac_collector_output/raw/*/*` 중 `session.json` 이 있는
  가장 최근 디렉터리를 잡는다. 수집 시작 지연 중이라 디렉터리가 아직 없으면
  `수집 세션이 아직 없습니다 — 지연이 끝난 뒤 다시 누르세요` 로 거절한다.
- 실시간 탭 상단에 `JOB`·`RT_JOB` 상태 태그 두 개와 **실시간 중지** 버튼을 둔다.
- 재생(녹화·합성)은 지금처럼 세션을 고른다.

### 2.6 진단 탭

`진단·데이터셋` → `진단`. 분리 가능성 진단 카드만 남긴다. **JSONL 내보내기 카드는 뺀다** —
제어판 학습 경로(`amp60`·`phase60`)는 `.csi` 를 직접 읽으므로 비개발자 흐름에 필요 없고,
공식 3-RX 전처리는 개발자 작업이다(`export_jsonl.py` 와 `scripts/README.md`).

## 3. CLI (개발자)

번호 메뉴를 유지하고 두 항목을 넣는다.

```text
[1] 보드 플래시            [5] 모델 학습 (세션 선택)
[2] 수집                   [6] 실시간 (모델·세션 고르기)   ← 추가
[3] 블록 순서 뽑기         [7] 세션 메타 편집
[4] 세션 보기              [8] 보드 관리
    (날짜별 · 품질 원인)   [9] 돌아가기
    ← 추가
```

- **세션 보기**: `train_sessions.list_sessions()` 를 날짜별로 출력하고 §2.4 의 문장을 붙인다.
- **실시간**: `realtime.engine.available_models()` 와 최근 세션 12개에서 고른 뒤
  `realtime/ui.py` 를 `--follow` 또는 `--speed 2` 로 실행한다.
- 수집 직후 판정 출력에 §2.4 의 문장을 쓴다.
- `scripts/README.md` 러너 표에 `_train_run.py`·`realtime/ui.py`·`export_jsonl.py` 를
  "제어판 없이 직접 부를 수 있는 명령" 으로 적는다. 서브커맨드 CLI 는 만들지 않는다.

## 4. API 변경 (제어판 내부)

| 경로 | 변경 |
|---|---|
| `GET /api/state` | `steps`, `env`, `rt_job` 추가. `sessions[*].warn` → `quality: {level, text}` |
| `POST /api/register` | 추가. `{sta_mac, name}` |
| `POST /api/idf` | 추가. `idf_bootstrap.py -y` 를 `JOB` 으로 |
| `POST /api/realtime/start` | `session` 생략 가능(따라가기). `RT_JOB` 사용 |
| `POST /api/realtime/stop` | 추가 |
| `POST /api/export` | 삭제 |

## 5. 검증

- `tests/test_control_panel.py`: `steps()` 의 current 규칙과 `quality()` 의 표를 unittest 로 고정.
- 화면은 구현 후 브라우저로 직접 눌러 확인한다: 보드 등록 → 수집 시작 → 수집 중 따라가기 →
  세션 표 문장 → 진단 탭에 JSONL 카드 없음.
- 문서: `quickstart.md` §4, `scripts/README.md`, `doc/README.md` 인덱스. 작업 기록은
  `doc/sprint/2026-10-control-panel-flow.md`.

## 6. 하지 않는 것

- 수집 경로(`csi_serial_reader.py`·펌웨어·`.csi`)와 registry·실험 데이터는 수정하지 않는다.
- 위저드("다음" 버튼 선형 흐름), TX IDENT 펌웨어, 모델 "배포" 버튼, CLI 서브커맨드 — 기각.
  근거는 이 설계를 승인한 2026-10-12 논의: 담당자는 같은 수집 탭을 반복하므로 위저드는
  2회차부터 방해이고, 나머지는 지금 방식이 이미 동작한다.
- 시각 디자인(색·간격·서체)은 이번 범위가 아니다. 점진적으로 다듬는다.

## 7. 관련 문서

- [빠른 시작](quickstart.md) §4 — 제어판·터미널 사용 절차 (이 설계 구현 시 갱신)
- [실시간 상태 판단](realtime-inference.md) §8 — 실시간 탭 화면 구성
- [수집 프로토콜](collection-protocol.md) — 블록 순서·라벨 정의
- [호스트 스크립트](../scripts/README.md) — 러너와 CLI 책임
