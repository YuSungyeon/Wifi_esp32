# 제어판 탭 4개 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 제어판 탭을 7개에서 4개(수집·세션·학습·실시간)로 줄이고, 보드·실험 정보·진단을 각각 쓰이는 화면 안으로 옮긴다.

**Architecture:** 화면은 지금처럼 단일 HTML 문자열(`PAGE`·`CSS`·`JS`) 안에서만 바뀐다. 새로 생기는 서버 상태는 블록 순서 진행뿐이고 `mac_collector/block_order.json` 한 파일에 둔다. 품질 문장은 지금 두 벌(`session_rows()` 와 `train_sessions.list_sessions()`)을 `csi_session.quality()` 하나로 합친다. **탭을 지우는 것은 마지막 작업**이다 — 모든 내용이 새 집을 가진 뒤에 옮겨야 중간 상태에서도 제어판이 쓸 수 있다.

**Tech Stack:** Python 3 표준 라이브러리(`http.server`, `json`, `random`), 기존 `.venv`, `unittest`. 새 의존성 없음.

**Spec:** [doc/control-panel.md](../control-panel.md) (2026-10-18 판)

## Global Constraints

- 모든 Python 실행·테스트는 `.venv/bin/python` 으로 한다. 테스트: `.venv/bin/python -m pytest tests/<파일> -v`.
- 테스트는 `tests/` 에 `unittest` 스타일. `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))` 로 `scripts/` 를 import 한다 (`tests/test_train_ui.py` 와 같은 방식).
- **수집 경로(`csi_serial_reader.py`·펌웨어·`.csi` 포맷)·registry 값·`mac_collector_output/` 실험 데이터는 수정하지 않는다.**
- **포트 프로브(esptool) 금지. `JOB.running` 인 동안 `identify()`·`list_ports()` 를 부르지 않는다** — 수집기와 포트를 다툰다 (CLAUDE.md "손대면 안 되는 것").
- 품질 기준값은 지금과 같다: `tx_back>0`·`boot_changes>0`·`crc_fail>0`·`seq_gap > frames×1%`. 여기에 spec §4.2 의 `RX≠3` 한 줄만 는다.
- 라벨 어휘는 `csi_store.LABELS`, 세션 이름 어휘는 `csi_session.SESSION_LABELS`(= LABELS + `live`), 클래스당 최소 세션은 `train_sessions.MIN_PER_CLASS`(=2).
- 화면 문구는 spec 의 문장을 그대로 쓴다. **새 색·서체는 만들지 않는다** — 기존 CSS 변수와 상태색 체계를 그대로 쓴다(spec §9).
- 커밋 메시지는 저장소 관례(`feat : …`, `docs : …`, `fix : …`)를 따르고 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` 를 붙인다.

## Review Focus

1. **옛 매니페스트에 `seq_gap`·`tx_back` 키가 없다** — `quality()` 가 `KeyError` 없이 `ok` 를 내야 한다. → Task 2 `test_missing_keys_are_ok`.
2. **`block_order.json` 이 없거나 깨졌다** — 첫 실행이거나 파일이 손상돼도 수집 화면이 떠야 하고, 새로 뽑으면 된다. → Task 1 `test_missing_and_corrupt_file_start_fresh`.
3. **세션 0개 · 보드 0대** — 날짜 접기·라벨 칩·준비 줄이 빈 입력에서 읽을 수 있는 화면을 내야 한다. → Task 5 `test_empty_sessions`, Task 3 `test_no_boards_blocks_with_reason`.
4. **수집 중에 준비 줄이 포트를 다시 찾는다** — 금지된 동작이다. `JOB.running` 이면 점검을 멈추고 `수집 중` 을 보여야 한다. → Task 3 `test_does_not_probe_while_job_running`.
5. **`live` 세션이 라벨 칩·학습 목록에 섞인다** — `live` 는 클래스가 아니다. 목록에는 보이고 칩·학습에는 안 보여야 한다. → Task 5 `test_live_session_is_listed_but_not_counted`.

---

### Task 1: `scripts/block_order.py` — 블록 순서 진행 상태

**Files:**
- Create: `scripts/block_order.py`
- Create: `tests/test_block_order.py`

**Interfaces:**
- Consumes: `csi_store.LABELS`
- Produces:
  - `STORE: Path` = `mac_collector/block_order.json`
  - `load(path: Path = STORE) -> dict` — `{"order": list[str], "done": list[str], "drawn_at": str, "date": str, "placement": str, "subject": str}`. 파일이 없거나 JSON 이 깨졌으면 빈 상태 `{"order": [], "done": [], ...}`.
  - `save(state: dict, path: Path = STORE) -> None`
  - `draw(prev: list[str] | None, labels=LABELS, rng=None) -> list[str]` — 직전 순서와 같으면 다시 뽑는다.
  - `advance(state: dict, label: str, level: str) -> dict` — `level ∈ ok|check|recollect`. `recollect` 면 `done` 에서 뺀다, 아니면 넣는다.
  - `next_label(state: dict) -> str | None` — `order` 중 `done` 에 없는 첫 라벨. 없으면 `None`.
  - `redraw_reason(state: dict, date: str, placement: str, subject: str) -> str | None` — 새로 뽑아야 하면 한국어 사유 문장, 아니면 `None`.

- [ ] **Step 1: Write the failing tests**

```python
class DrawTest(unittest.TestCase):
    def test_draw_never_repeats_the_previous_order(self):
        for _ in range(20):
            prev = ["empty", "static", "motion"]
            self.assertNotEqual(bo.draw(prev), prev)

    def test_draw_uses_every_label_once(self):
        self.assertEqual(sorted(bo.draw(None)), sorted(LABELS))

class AdvanceTest(unittest.TestCase):
    def setUp(self):
        self.s = {"order": ["empty", "static", "motion"], "done": [],
                  "date": "20261018", "placement": "P2", "subject": "S1"}

    def test_next_label_follows_the_order(self):
        self.assertEqual(bo.next_label(self.s), "empty")
        self.assertEqual(bo.next_label(bo.advance(self.s, "empty", "ok")), "static")

    def test_check_counts_as_done(self):
        self.assertIn("empty", bo.advance(self.s, "empty", "check")["done"])

    def test_recollect_puts_the_cell_back(self):
        done = bo.advance(self.s, "empty", "ok")
        self.assertEqual(bo.advance(done, "empty", "recollect")["done"], [])

    def test_all_done_means_no_next_label(self):
        s = dict(self.s, done=["empty", "static", "motion"])
        self.assertIsNone(bo.next_label(s))

class RedrawTest(unittest.TestCase):
    BASE = {"order": ["empty", "static", "motion"], "done": ["empty"],
            "date": "20261018", "placement": "P2", "subject": "S1"}

    def test_same_condition_keeps_the_block(self):
        self.assertIsNone(bo.redraw_reason(self.BASE, "20261018", "P2", "S1"))

    def test_new_date_placement_or_subject_redraws_with_a_reason(self):
        for date, place, subj, word in [("20261019", "P2", "S1", "날짜"),
                                        ("20261018", "P3", "S1", "배치"),
                                        ("20261018", "P2", "S2", "피험자")]:
            with self.subTest(word):
                self.assertIn(word, bo.redraw_reason(self.BASE, date, place, subj))

    def test_block_finished_redraws(self):
        s = dict(self.BASE, done=["empty", "static", "motion"])
        self.assertIsNotNone(bo.redraw_reason(s, "20261018", "P2", "S1"))

class StoreTest(unittest.TestCase):
    def test_missing_and_corrupt_file_start_fresh(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "block_order.json"
            self.assertEqual(bo.load(p)["order"], [])
            p.write_text("{ not json")
            self.assertEqual(bo.load(p)["order"], [])

    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "block_order.json"
            bo.save({"order": ["motion", "empty", "static"], "done": ["motion"],
                     "date": "20261018", "placement": "P2", "subject": "S1"}, p)
            self.assertEqual(bo.load(p)["done"], ["motion"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_block_order.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'block_order'`

- [ ] **Step 3: Implement `scripts/block_order.py`**

모듈 docstring에 **왜 서버에 두는지**를 적는다: 제어판 `localStorage` 와 CLI 메모리에 따로 있으면 새로고침에 사라지고 CLI 로 찍은 세션이 반영되지 않는다 (spec §3.2).

`redraw_reason` 의 사유 문장은 spec §3.2 의 예를 따른다 — `배치가 바뀌어 새 블록을 뽑았습니다` 형태로 날짜·배치·피험자·블록 완료 네 가지.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_block_order.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/block_order.py tests/test_block_order.py
git commit -m "feat : 블록 순서 진행 상태를 서버 파일 하나로 — 제어판·CLI 공용"
```

---

### Task 2: `csi_session.quality()` — 품질 문장 단일 소스

**Files:**
- Modify: `scripts/csi_session.py` (`summarize()` 아래, 196행 끝)
- Modify: `scripts/meshsense_gui.py:175-204` (`session_rows()`)
- Modify: `scripts/train_sessions.py:40-52` (`list_sessions()` 의 `broken`·`ok`)
- Modify: `scripts/meshsense_cli.py` (수집 직후 판정 출력)
- Create: `tests/test_control_panel.py`

**Interfaces:**
- Produces: `csi_session.quality(devices: list[dict]) -> tuple[str, str]` — `level ∈ ok|check|recollect`, 문장은 spec §4.2 표 그대로. 여러 조건이 겹치면 가장 센 level 하나와 문장들을 ` / ` 로 잇는다. 입력은 `read_manifest()` 가 주는 device dict 그대로(`seq_gap`·`frames` 포함).
- Consumed by: Task 1 `advance(..., level)`, Task 5 세션 목록, Task 6 학습 목록.

- [ ] **Step 1: Write the failing tests**

```python
OK = [{"device_id": i, "frames": 30000, "span_s": 300.0, "crc_fail": 0,
       "boot_changes": 0, "tx_back": 0, "seq_gap": 0} for i in (101, 102, 103)]

class QualityTest(unittest.TestCase):
    def test_clean_three_rx_session_is_ok(self):
        self.assertEqual(quality(OK)[0], "ok")

    def test_missing_keys_are_ok(self):
        old = [{"device_id": 101, "frames": 30000, "span_s": 300.0}]  # 옛 매니페스트
        self.assertEqual(quality(old + OK[1:])[0], "recollect")       # RX 1대 부족만 걸린다

    def test_tx_reboot_demands_recollection(self):
        level, text = quality([dict(OK[0], tx_back=1), *OK[1:]])
        self.assertEqual(level, "recollect")
        self.assertIn("TX", text)

    def test_crc_failure_is_only_a_check(self):
        self.assertEqual(quality([dict(OK[0], crc_fail=3), *OK[1:]])[0], "check")

    def test_loss_over_one_percent_is_a_check(self):
        self.assertEqual(quality([dict(OK[0], seq_gap=400), *OK[1:]])[0], "check")
        self.assertEqual(quality([dict(OK[0], seq_gap=100), *OK[1:]])[0], "ok")

    def test_fewer_than_three_rx_demands_recollection(self):
        level, text = quality(OK[:2])
        self.assertEqual(level, "recollect")
        self.assertIn("2대", text)

    def test_strongest_level_wins_and_sentences_are_joined(self):
        level, text = quality([dict(OK[0], crc_fail=1), dict(OK[1], boot_changes=1), OK[2]])
        self.assertEqual(level, "recollect")
        self.assertIn(" / ", text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -v`
Expected: FAIL — `ImportError: cannot import name 'quality'`

- [ ] **Step 3: Implement `quality()` in `scripts/csi_session.py`**

없는 키는 `0` 으로 본다(`dict.get`) — 옛 매니페스트가 깨지지 않게.

- [ ] **Step 4: Wire the three callers to it**

- `meshsense_gui.session_rows()`: `"warn": bad` → `"quality": {"level": …, "text": …}`. JS 의 `s.warn` 사용처도 함께 바꾼다(지금은 세션 탭 표 한 곳).
- `train_sessions.list_sessions()`: `broken`/`len(devices)==3` 계산을 지우고 `ok = quality(devices)[0] != "recollect"` 로 바꾼다. `check` 는 학습에 쓸 수 있다는 뜻이므로 `ok=True` 다 — `check_selection()` 의 "품질 미달" 경고 문구는 그대로 둔다.
- `meshsense_cli`: 수집 직후 판정 출력에 `quality()` 문장을 쓴다.

- [ ] **Step 5: Run the whole suite**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS (기존 141 + 새 테스트)

- [ ] **Step 6: Commit**

```bash
git add scripts/csi_session.py scripts/meshsense_gui.py scripts/train_sessions.py scripts/meshsense_cli.py tests/test_control_panel.py
git commit -m "feat : 품질 문장을 csi_session.quality() 하나로 — 두 벌이던 기준을 합친다"
```

---

### Task 3: 수집 탭 준비 줄 — 보드 탭 흡수

**Files:**
- Modify: `scripts/meshsense_gui.py:139-162` (`readiness()`)
- Modify: `scripts/meshsense_gui.py:560-578` (수집 탭 JS)
- Modify: `scripts/meshsense_gui.py:321-441` (`CSS` — 접이식 줄 한 벌)
- Modify: `tests/test_control_panel.py`

**Interfaces:**
- Produces: `meshsense_gui.ready_line() -> dict` — `{"level": "ok|warn|block", "text": str, "rows": [{"key","level","text","action"}]}`. `action ∈ None|"register"|"flash"|"idf"|"rescan"`.
- `/api/state` 의 `readiness` 를 이 모양으로 바꾼다. `/api/register {sta_mac, name}` 를 더한다.

spec §3.1 의 점검 여섯 줄을 그대로 옮긴다. **`pyserial` 과 RX 3대만 `block`**, 나머지(미등록 RX·좌표·커밋·학습 환경)는 `warn`.

- [ ] **Step 1: Write the failing tests**

```python
class ReadyLineTest(unittest.TestCase):
    def test_three_boards_and_pyserial_is_ok(self):
        line = ready_line(boards=THREE, job_running=False)
        self.assertEqual(line["level"], "ok")
        self.assertIn("RX 3대", line["text"])
    def test_no_boards_blocks_with_reason(self):
        line = ready_line(boards=[], job_running=False)
        self.assertEqual(line["level"], "block")
        self.assertIn("3대", line["text"])

    def test_missing_torch_warns_but_does_not_block(self):
        """수집은 pyserial 만 있으면 된다 — torch 없는 수집용 노트북을 막지 않는다."""
        line = ready_line(boards=THREE, job_running=False, packages={"torch": False})
        self.assertEqual(line["level"], "warn")

    def test_does_not_probe_while_job_running(self):
        """수집 중 포트 접근은 금지다 (CLAUDE.md)."""
        with mock.patch.object(gui, "identify", side_effect=AssertionError("probed")):
            line = gui.ready_line(boards=THREE, job_running=True)
        self.assertIn("수집 중", line["text"])

    def test_hz_is_labelled_with_its_source(self):
        """실측이 아니라 지난 세션 값이다 — 출처를 함께 적는다."""
        row = next(r for r in ready_line(boards=THREE, job_running=False)["rows"]
                   if r["key"] == "rx")
        self.assertIn("지난 세션", row["text"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -k ReadyLine -v`
Expected: FAIL — `AttributeError: module 'meshsense_gui' has no attribute 'ready_line'`

- [ ] **Step 3: Implement `ready_line()` and `/api/register`**

Hz 는 그 보드의 마지막 세션 매니페스트에서 읽는다(`session_rows()` 가 이미 모은 값 재사용). 보드가 처음이면 칸을 비운다.
갱신은 서버 시작·`다시 찾기`·수집 JOB 종료 직후만 — `/api/state` 는 캐시(`Handler.boards_cache`)만 읽는다.
`/api/register` 는 `device_registry.py add --mac … --board-name …` 를 부르고 stderr 를 그대로 돌려준다(조용히 실패 금지).

- [ ] **Step 4: Render the line in the collect tab**

접힌 한 줄 + `자세히 ▸`. 펼치면 `rows` 를 RX별 한 줄로, `action` 이 있으면 버튼을 붙인다. `level=="block"` 이면 수집 버튼을 잠근다.

- [ ] **Step 5: Verify in the browser**

`python3 scripts/meshsense_gui.py` → 수집 탭에 초록 한 줄. RX 하나를 빼고 다시 찾으면 빨간 줄과 잠긴 버튼. 펼쳐서 등록 버튼 확인.

- [ ] **Step 6: Commit**

```bash
git add scripts/meshsense_gui.py tests/test_control_panel.py
git commit -m "feat : 수집 화면 준비 줄 — 보드 점검을 막힌 자리에서 푼다"
```

---

### Task 4: 수집 탭 본문 — 블록 진행·버튼 라벨·설정 접기

**Files:**
- Modify: `scripts/meshsense_gui.py` (수집 탭 JS, `/api/state`, `/api/collect`)
- Modify: `scripts/meshsense_gui.py:816-836` (`tog`·`startCollect`·`drawOrder` — `drawOrder` 의 `localStorage` 를 지운다)
- Modify: `tests/test_control_panel.py`

**Interfaces:**
- Consumes: Task 1 `block_order.{load,save,draw,advance,next_label,redraw_reason}`, Task 2 `quality()`
- `/api/state` 에 `block: {"order","done","next","redrawn_reason"}` 를 더한다.
- `/api/block/draw` 를 더한다. `/api/collect` 는 `label` 을 받지 않고 `block.next_label()` 을 쓴다.
- 수집 JOB 이 끝나면 서버가 `advance(state, label, quality(...)[0])` 를 부른다.

화면은 spec §3.2·§3.3·§3.4 그대로. 라벨 선택 상자를 없애고, 버튼은 `<라벨> 수집 시작`. 실험 정보는 `▸ 이번 실험 설정`(배치·피험자·`operator.notes`) / `▸ 프로젝트 설정`(나머지 7칸)으로 두 겹. 접힌 줄에 값을 미리 적는다. `300초 녹화 · 10초 뒤 시작` + `바꾸기`.

- [ ] **Step 1: Write the failing tests**

```python
class CollectPayloadTest(unittest.TestCase):
    def test_collect_button_label_comes_from_the_block(self):
        self.assertEqual(gui.collect_label({"order": ["empty","static","motion"],
                                            "done": ["empty"]}), "static")

    def test_block_finished_asks_for_a_new_draw(self):
        self.assertIsNone(gui.collect_label({"order": ["empty"], "done": ["empty"]}))

    def test_recollect_verdict_puts_the_label_back(self):
        """§4.2 가 '같은 라벨로 다시 찍으세요' 라고 하면 화면도 같은 말을 해야 한다."""
        s = bo.advance({"order": ["empty","static","motion"], "done": []}, "empty", "ok")
        self.assertEqual(gui.collect_label(bo.advance(s, "empty", "recollect")), "empty")

    def test_folded_line_shows_the_values_without_opening(self):
        self.assertEqual(gui.settings_summary({"condition.placement_id": "P2",
                                               "condition.subject_id": "S1",
                                               "operator.notes": ""}),
                         "P2 · S1 · 메모 없음")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -k Collect -v`
Expected: FAIL — `AttributeError: … 'collect_label'`

- [ ] **Step 3: Implement `collect_label()` and `settings_summary()` in `meshsense_gui.py`**

- [ ] **Step 4: Wire the collect tab and `/api/collect`**

`drawOrder()` 의 `localStorage` 를 지우고 `/api/block/draw` 를 부른다. 수집 JOB 종료 훅에서 `advance()` 를 부른다 — 세션 매니페스트를 읽어 `quality()` level 을 넘긴다.

- [ ] **Step 5: Run the suite and verify in the browser**

Run: `.venv/bin/python -m pytest tests/ -q` → PASS
브라우저: 블록 순서 세 칸이 `끝남/지금/남음` 으로 보이고, 수집 버튼이 다음 라벨을 말하는지. 배치 번호를 바꾸면 사유 한 줄과 함께 새로 뽑히는지.

- [ ] **Step 6: Commit**

```bash
git add scripts/meshsense_gui.py tests/test_control_panel.py
git commit -m "feat : 수집 화면이 한 화면으로 — 블록 진행·버튼이 말하는 라벨·설정 접기"
```

---

### Task 5: 세션 탭 — 날짜 접기·`live`·진단 링크

**Files:**
- Modify: `scripts/meshsense_gui.py` (세션 탭 JS, `session_rows()` 호출부)
- Modify: `tests/test_control_panel.py`

**Interfaces:**
- Produces: `meshsense_gui.session_groups(rows) -> list[dict]` — 날짜별 `{"date","count","placements","labels","live","worst","excluded"}`. `labels` 는 `LABELS` 만 센다. `worst` 는 그 날짜에서 가장 센 `quality` level.
- Consumes: Task 2 `quality()`, `train_sessions.EXCLUDED_SESSIONS`

진단 카드와 JSONL 내보내기 카드를 `진단·데이터셋` 탭에서 세션 탭 맨 아래 **글자 링크 둘**로 옮긴다(내용·엔드포인트는 그대로). 누르면 그 자리에서 펼쳐져 실행 버튼과 결과를 보인다.

- [ ] **Step 1: Write the failing tests**

```python
class SessionGroupTest(unittest.TestCase):
    def test_empty_sessions(self):
        self.assertEqual(gui.session_groups([]), [])

    def test_groups_are_newest_first_and_keep_every_session(self):
        g = gui.session_groups(ROWS)
        self.assertEqual([x["date"] for x in g], ["20260920", "20260919"])
        self.assertEqual(sum(x["count"] for x in g), len(ROWS))

    def test_live_session_is_listed_but_not_counted(self):
        """live 는 여러 상태가 섞여 클래스가 아니다 — 전체에는 세고 라벨 칩에는 안 센다."""
        g = gui.session_groups(ROWS + [LIVE_ROW])[0]
        self.assertEqual(g["live"], 1)
        self.assertNotIn("live", g["labels"])
        self.assertEqual(g["count"], sum(g["labels"].values()) + g["live"])

    def test_worst_quality_reaches_the_folded_row(self):
        g = gui.session_groups(ROWS_WITH_ONE_CHECK)[0]
        self.assertEqual(g["worst"], "check")

    def test_excluded_session_is_listed_with_its_reason(self):
        g = gui.session_groups(ROWS_WITH_S12)
        self.assertEqual(g[0]["excluded"], ["20260919_12"])

    def test_placements_are_collected_from_the_snapshot(self):
        self.assertEqual(gui.session_groups(ROWS)[0]["placements"], ["P3"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -k SessionGroup -v`
Expected: FAIL — `AttributeError: … 'session_groups'`

- [ ] **Step 3: Implement `session_groups()` in `meshsense_gui.py`**

배치는 각 세션의 `session_meta_snapshot.yaml` 의 `condition.placement_id` 에서 읽는다 — `session_note()` 와 같은 방식으로 한 줄 파싱한다. 값이 없으면 생략.

- [ ] **Step 4: Render the collapsed session list and move the two links**

접힌 줄에 `30세션 · 배치 P3`, 오른쪽에 `empty 10 · static 10 · motion 10`, `worst != "ok"` 면 `⚠N`. 제외 세션은 펼침에서 회색 + 이유.

- [ ] **Step 5: Run the suite and verify in the browser**

Run: `.venv/bin/python -m pytest tests/ -q` → PASS
브라우저: 날짜가 접혀 있고, 9/19 줄에 `⚠`, 10/14 줄에 `live 2`, 맨 아래 링크 둘.

- [ ] **Step 6: Commit**

```bash
git add scripts/meshsense_gui.py tests/test_control_panel.py
git commit -m "feat : 세션 탭을 날짜로 접고 진단·내보내기를 글자 링크로"
```

---

### Task 6: 학습 탭 — 한 화면

**Files:**
- Modify: `scripts/meshsense_gui.py:745-815` (`renderTrain`)
- Modify: `scripts/meshsense_gui.py` (`training_state()`)
- Modify: `scripts/_train_run.py` (창 수 사전 추정)
- Modify: `tests/test_train_ui.py`

**Interfaces:**
- Produces: `_train_run.predict_windows(sessions: list[dict], window: int, stride: int) -> int` — `Σ(span_s × 100 − window) ÷ stride`, 음수는 0.
- `training_state()` 의 날짜마다 `trained_models: int` 를 더한다 — 그 날짜를 `training_dates` 에 가진 체크포인트 수.
- `training_state()` 에 `selected: []` 를 더한다 — 화면이 처음 열릴 때의 선택. 지금은 선택이 브라우저에만 있어(`trainPick`) 기본값을 테스트할 수 없다.

spec §5 그대로. 단계 번호를 없애고, 평가 날짜·모델 이름은 자동값 + `바꾸기` 한 줄. 날짜 묶음에 왜 못 고르는지 적는다(`평가에 쓰입니다` / `이 날짜로 학습한 모델이 N개 있습니다`). 처음 열면 아무것도 선택돼 있지 않다.

- [ ] **Step 1: Write the failing tests**

```python
class PredictWindowsTest(unittest.TestCase):
    def test_matches_the_trainer_within_10_percent(self):
        """실측 세 건: 15세션 창 3000 stride 150 → 2687창."""
        got = predict_windows([{"span_s": 300.0}] * 15, window=3000, stride=150)
        self.assertLess(abs(got - 2687) / 2687, 0.10)

    def test_sessions_shorter_than_the_window_contribute_nothing(self):
        self.assertEqual(predict_windows([{"span_s": 10.0}], window=3000, stride=150), 0)

class TrainScreenTest(unittest.TestCase):
    def test_nothing_is_selected_when_the_screen_opens(self):
        """미리 고르면 평가 날짜 기본값(학습에 안 쓴 날짜)이 갈 곳이 없어진다."""
        self.assertEqual(gui.training_state()["selected"], [])

    def test_each_date_reports_how_many_models_used_it(self):
        row = next(d for d in gui.training_state()["dates"] if d["date"] == "20260920")
        self.assertGreater(row["trained_models"], 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_train_ui.py -k "PredictWindows or TrainScreen" -v`
Expected: FAIL — `ImportError: cannot import name 'predict_windows'`

- [ ] **Step 3: Implement `predict_windows()` and the `trained_models` count**

체크포인트는 `realtime.engine.available_models()` 가 아니라 `trained_models()` 가 이미 읽는 목록을 쓴다 — `training_dates` 를 세면 된다.

- [ ] **Step 4: Rewrite `renderTrain` as one screen**

시작 버튼 라벨: `<N>세션으로 학습 시작 — 약 <hms>`. 추정이 2배 이상 빗나가면 시간을 빼고 세션 수만 남긴다(§5).

- [ ] **Step 5: Run the suite and verify in the browser**

Run: `.venv/bin/python -m pytest tests/ -q` → PASS
브라우저: 처음엔 아무것도 안 골라져 있고 버튼이 잠겨 있는지. 9/19 를 고르면 평가 날짜가 9/20 으로 바뀌고, 9/20 줄에 모델 수가 보이는지.

- [ ] **Step 6: Commit**

```bash
git add scripts/meshsense_gui.py scripts/_train_run.py tests/test_train_ui.py
git commit -m "feat : 학습 탭을 한 화면으로 — 단계 번호 대신 자동값과 이유 문구"
```

---

### Task 7: 실시간 탭 — 카드 둘과 `판단 정지`

**Files:**
- Modify: `scripts/meshsense_gui.py:681-730` (`renderRealtime`)
- Modify: `scripts/meshsense_gui.py` (`/api/live/stop`, 새 `/api/rt/pause`)
- Modify: `tests/test_realtime.py`

**Interfaces:**
- `/api/rt/pause` 를 더한다 — `RT_JOB.stop()` 만. 수집(`JOB`)은 건드리지 않는다.
- `realtime_view()` 에 `paused_by_operator: bool` 를 더한다.

spec §6 그대로. 카드 4장 → 2장, 색 범례 삭제, 상태줄·버튼은 맨 아래 한 줄. `전환 표시` 옆에 마지막으로 누른 시각.

- [ ] **Step 1: Write the failing tests**

```python
class PauseTest(unittest.TestCase):
    def test_pause_stops_judgement_but_not_collection(self):
        """화면만 끄고 데이터는 계속 받는 쓰임이다."""
        with mock.patch.object(gui.RT_JOB, "stop") as rt, \
             mock.patch.object(gui.JOB, "stop") as job:
            gui.dispatch("/api/rt/pause", {})
        rt.assert_called_once()
        job.assert_not_called()

    def test_operator_pause_and_rx_loss_do_not_share_wording(self):
        """둘 다 멈춤이지만 하나는 정상이고 하나는 이상이다."""
        self.assertNotEqual(gui.pause_note(paused_by_operator=True),
                            gui.pause_note(paused_by_operator=False, n_rx=1))

class MarkTest(unittest.TestCase):
    def test_last_mark_time_is_reported(self):
        """전환 표시는 전환 지연 측정의 유일한 정답 입력이다 — 눌렀는지 보여야 한다."""
        self.assertIn("last_mark_s", gui.realtime_view(RAW_WITH_MARKS))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_realtime.py -k "Pause or Mark" -v`
Expected: FAIL

- [ ] **Step 3: Implement `/api/rt/pause`, `pause_note()`, `last_mark_s`**

- [ ] **Step 4: Merge the four cards into two**

상태 카드(상태·유지·확신·활동량)와 기록 카드(60분 띠·변화 목록). 상태줄과 버튼 셋은 기록 카드 맨 아래 한 줄. 범례 삭제.

- [ ] **Step 5: Run the suite and verify in the browser**

Run: `.venv/bin/python -m pytest tests/ -q` → PASS
브라우저: 합성 세션 재생으로 카드가 둘인지, `판단 정지` 가 수집을 안 끄는지.

- [ ] **Step 6: Commit**

```bash
git add scripts/meshsense_gui.py tests/test_realtime.py
git commit -m "feat : 실시간 탭 카드 넷을 둘로, 판단 정지는 수집을 끄지 않는다"
```

---

### Task 8: 탭 넷으로 — 옛 탭 삭제

**Files:**
- Modify: `scripts/meshsense_gui.py:443-476` (`PAGE` 의 `nav`·`section`)
- Modify: `scripts/meshsense_gui.py:477-633` (`JS` 의 탭 분기, 기본 탭)
- Modify: `tests/test_control_panel.py`

**Interfaces:** 없음 — 지우는 작업이다. Task 3~7 이 모든 내용에 새 집을 준 뒤에만 한다.

- [ ] **Step 1: Write the failing test**

```python
class TabTest(unittest.TestCase):
    def test_exactly_four_tabs_in_work_order(self):
        self.assertEqual(re.findall(r'data-t="(\w+)"', gui.PAGE),
                         ["collect", "sessions", "train", "realtime"])

    def test_collect_is_the_default_tab(self):
        self.assertIn('data-t="collect" class="on"', gui.PAGE)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -k Tab -v`
Expected: FAIL — 7개가 나온다

- [ ] **Step 3: Delete the `boards`·`meta`·`analyze` nav buttons, sections, and their render branches**

`let tab = 'realtime'` 을 `'collect'` 로 바꾼다. 죽은 JS 함수(`tog`, `saveMeta` 중 옮겨지지 않은 것)를 지운다.

- [ ] **Step 4: Run the suite and click through the browser**

Run: `.venv/bin/python -m pytest tests/ -q` → PASS
브라우저: 탭 넷, 기본이 수집. 보드 등록 → 수집 → 세션 → 학습 → 실시간까지 한 바퀴.

- [ ] **Step 5: Commit**

```bash
git add scripts/meshsense_gui.py tests/test_control_panel.py
git commit -m "feat : 제어판 탭을 넷으로 — 보드·실험 정보·진단 탭을 지운다"
```

---

### Task 9: CLI 두 메뉴 — 세션 보기·실시간

**Files:**
- Modify: `scripts/meshsense_cli.py` (`_menu_usb_pipeline`)
- Modify: `tests/test_control_panel.py`

spec §7 그대로. 번호 메뉴를 유지하고 `세션 보기`(날짜별 + §4.2 문장)와 `실시간`(모델·세션 고르기)을 넣는다.

- [ ] **Step 1: Write the failing test**

```python
class CliMenuTest(unittest.TestCase):
    def test_menu_has_session_view_and_realtime(self):
        items = cli._usb_menu_items()
        self.assertIn("세션 보기", " ".join(items))
        self.assertIn("실시간", " ".join(items))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -k CliMenu -v`
Expected: FAIL — `AttributeError: … '_usb_menu_items'`

- [ ] **Step 3: Extract `_usb_menu_items()` and add the two entries**

`세션 보기`: `train_sessions.list_sessions()` 를 날짜별로 출력하고 `quality()` 문장을 붙인다.
`실시간`: `realtime.engine.available_models()` 와 최근 세션 12개에서 고른 뒤 `realtime/ui.py` 를 `--follow` 또는 `--speed 2` 로 실행한다.

- [ ] **Step 4: Run the suite and try the menu**

Run: `.venv/bin/python -m pytest tests/ -q` → PASS
`.venv/bin/python scripts/meshsense_cli.py --quick` 로 두 항목을 눌러 본다.

- [ ] **Step 5: Commit**

```bash
git add scripts/meshsense_cli.py tests/test_control_panel.py
git commit -m "feat : CLI 에 세션 보기와 실시간 메뉴"
```

---

### Task 10: 문서 맞추기

**Files:**
- Modify: `doc/control-panel.md` (상태 `OFFICIAL DESIGN` → `CURRENT`)
- Modify: `doc/quickstart.md` §4
- Modify: `scripts/README.md` (러너 표)
- Modify: `doc/README.md` (인덱스)
- Modify: 현재 스프린트 문서 (작업 기록)

- [ ] **Step 1: Update the five documents**

`quickstart.md` §4 의 탭 이름과 절차를 넷으로. `scripts/README.md` 에 `block_order.py` 를 더하고 `_train_run.py`·`realtime/ui.py`·`export_jsonl.py` 를 "제어판 없이 직접 부를 수 있는 명령" 으로 적는다. 스프린트에는 **시도·막힌 지점·결과를 실측 수치와 함께** 남긴다(CLAUDE.md).

- [ ] **Step 2: Check every relative link resolves**

Run: `.venv/bin/python -c "import re,pathlib; [print(f,m) for f in pathlib.Path('doc').rglob('*.md') for m in re.findall(r'\]\((?!https?:)([^)#]+)', f.read_text()) if not (f.parent/m).resolve().exists()]"`
Expected: 출력 없음

- [ ] **Step 3: Run the whole suite one last time**

Run: `.venv/bin/python -m pytest tests/ -q`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add doc/ scripts/README.md
git commit -m "docs : 제어판 탭 넷을 CURRENT 로 — quickstart·러너 표·스프린트 기록"
```
