# 제어판·CLI 흐름 구현 계획

> 상태: **SUPERSEDED** (2026-10-18) — 이 계획은 탭 7개와 **단계 띠**를 전제로 쓰였다.
> 설계가 탭 4개로 바뀌면서 Task 2(단계 띠)·Task 3(보드 탭)·Task 6(진단 탭)이 무효가 됐다.
> 새 설계는 [doc/control-panel.md](../control-panel.md) 이고, 계획은 거기서 다시 쓴다.
> **아직 유효한 부분**: Task 1(`csi_session.quality()` 단일 소스 — 새 설계 §4.2 에서 "RX
> 3대" 한 줄이 늘었다), Task 7(CLI 메뉴 두 항목 — 새 설계 §7 그대로).
> Task 5(`RT_JOB` 분리)는 그 뒤 구현됐다.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 비개발자가 제어판만 보고 보드 → 수집 → 학습 → 실시간을 혼자 끝내게 하고, 개발자 CLI 메뉴에 빠진 두 단계(세션 보기·실시간)를 넣는다.

**Architecture:** 서버(`meshsense_gui.py`)가 `/api/state` 에 단계 띠 `steps`·환경 점검 `env`·실시간 작업 `rt_job` 을 추가하고, 품질 판정 문장은 `csi_session.quality()` 하나를 GUI·CLI·수집 러너가 공유한다. 백그라운드 작업 슬롯을 `JOB`(플래시·수집·학습·진단·IDF)과 `RT_JOB`(실시간) 둘로 나눠 수집 중 따라가기를 살린다. 화면은 기존 단일 HTML 문자열(`PAGE`·`CSS`·`JS`) 안에서만 바뀐다.

**Tech Stack:** Python 3 표준 라이브러리(`http.server`, `subprocess`), 기존 `.venv`(numpy·torch), `unittest`. 새 의존성 없음.

**Spec:** [doc/control-panel.md](../control-panel.md)

## Global Constraints

- 모든 Python 실행·테스트는 저장소 루트의 `.venv/bin/python` 으로 한다. 테스트: `.venv/bin/python -m pytest tests/<파일> -v`.
- 테스트는 `tests/` 에 `unittest` 스타일. `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))` 로 `scripts/` 모듈을 import 한다 (`tests/test_train_ui.py` 와 같은 방식).
- **수집 경로(`csi_serial_reader.py`·펌웨어·`.csi` 포맷)·registry 값·`mac_collector_output/` 실험 데이터는 수정하지 않는다.** CLAUDE.md "손대면 안 되는 것" 그대로.
- 포트 프로브(esptool) 금지. 보드 식별은 IDENT, 등록은 `device_registry.py add --mac`.
- 품질 기준값은 지금과 같다: `tx_back>0`·`boot_changes>0`·`crc_fail>0`·`seq_gap > frames×1%`.
- 라벨 어휘는 `csi_store.LABELS`, 클래스당 최소 세션은 `train_sessions.MIN_PER_CLASS`(=2).
- 커밋 메시지는 저장소 관례(`feat : …`, `docs : …`, `fix : …`)를 따르고 끝에
  `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` 를 붙인다.
- 화면 문구는 spec §2 의 문장을 그대로 쓴다. 새 색·서체는 만들지 않는다(시각 디자인은 범위 밖).

## Review Focus

1. **옛 manifest 에 `tx_back`·`seq_gap` 키가 없다** — `quality()` 가 `KeyError` 없이 `ok` 를 내야 한다. → Task 1 테스트 `test_missing_keys_are_ok`.
2. **보드가 하나도 없거나 세션·모델이 0개** — `steps()` 가 빈 입력에서 `current` 한 칸과 읽을 수 있는 텍스트를 내야 한다. → Task 2 테스트 `test_empty_everything`.
3. **이미 등록된 MAC 으로 등록 버튼** — `device_registry.py add` 의 stderr 가 그대로 `alert` 로 보여야 한다(조용히 실패 금지). → Task 4 Step 6 브라우저 확인.
4. **따라가기를 눌렀는데 실세션이 없고 합성(demo) 세션만 있다** — `latest_session_dir()` 가 `None` 을 내고 spec 문장으로 거절해야 한다. → Task 5 테스트 `test_no_sessions_returns_none`.
5. **서버 종료 시 실시간 프로세스가 고아가 된다** — `main()` 의 `finally` 가 `JOB`·`RT_JOB` 둘 다 멈춰야 한다. → Task 5 Step 4 코드 + Step 7 확인.

---

### Task 1: `csi_session.quality()` — 품질 문장 단일 소스

**Files:**
- Modify: `scripts/csi_session.py` (`summarize()` 아래, 178행 이후)
- Modify: `scripts/meshsense_gui.py:170-198` (`session_rows()`), `:579-595` (세션 탭 JS)
- Modify: `scripts/meshsense_cli.py:883-897` (수집 직후 판정)
- Modify: `scripts/_collect_run.py:96-108`
- Test: `tests/test_control_panel.py` (신규)

**Interfaces:**
- Produces: `csi_session.quality(devices: Iterable[dict]) -> tuple[str, str]` — `(level, text)`, `level ∈ ("ok","check","recollect")`.
  `csi_session.QUALITY_TAG = {"ok": "정상", "check": "확인", "recollect": "재수집"}`.
- `session_rows()` 의 각 행에 `"quality": {"level": str, "text": str}` (기존 `"warn"` 삭제).

- [ ] **Step 1: 실패하는 테스트**

`tests/test_control_panel.py`:

```python
"""제어판·CLI 흐름 규칙 — 품질 문장, 단계 띠, 따라가기 세션 선택.

화면이 편해질수록 "왜 걸렸는지" 를 사람이 추측하게 된다. 문장과 단계 규칙을 여기서 고정한다.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from csi_session import QUALITY_TAG, quality  # noqa: E402


def dev(**kw):
    base = {"device_id": 101, "frames": 30000, "crc_fail": 0, "boot_changes": 0, "tx_back": 0, "seq_gap": 0}
    base.update(kw)
    return base


class QualityTest(unittest.TestCase):
    def test_clean_session_is_ok_with_empty_text(self):
        self.assertEqual(quality([dev(), dev(device_id=102)]), ("ok", ""))

    def test_tx_reboot_is_recollect_and_sentence_appears_once(self):
        level, text = quality([dev(tx_back=1), dev(device_id=102, tx_back=1)])
        self.assertEqual(level, "recollect")
        self.assertEqual(text.count("TX 가 수집 중 재부팅"), 1)

    def test_rx_reboot_names_the_rx(self):
        level, text = quality([dev(), dev(device_id=102, boot_changes=1)])
        self.assertEqual(level, "recollect")
        self.assertIn("RX102 가 수집 중 재부팅", text)

    def test_crc_is_check(self):
        level, text = quality([dev(crc_fail=3)])
        self.assertEqual((level, text), ("check", "RX101 CRC 실패 3건 — 케이블 확인"))

    def test_seq_gap_over_one_percent_is_check_with_percent(self):
        level, text = quality([dev(device_id=103, seq_gap=420)])     # 1.4%
        self.assertEqual(level, "check")
        self.assertIn("RX103 손실 1.4%", text)

    def test_seq_gap_under_one_percent_is_ok(self):
        self.assertEqual(quality([dev(seq_gap=150)])[0], "ok")       # 0.5%

    def test_strongest_level_wins_and_sentences_join(self):
        level, text = quality([dev(crc_fail=1), dev(device_id=102, boot_changes=2)])
        self.assertEqual(level, "recollect")
        self.assertIn(" / ", text)

    def test_missing_keys_are_ok(self):
        self.assertEqual(quality([{"device_id": 101, "frames": 100}]), ("ok", ""))

    def test_tags_cover_every_level(self):
        self.assertEqual(set(QUALITY_TAG), {"ok", "check", "recollect"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -v`
Expected: FAIL — `ImportError: cannot import name 'QUALITY_TAG'`

- [ ] **Step 3: 구현**

`scripts/csi_session.py` 맨 아래(`summarize()` 뒤)에 추가:

```python
QUALITY_LEVELS = ("ok", "check", "recollect")
QUALITY_TAG = {"ok": "정상", "check": "확인", "recollect": "재수집"}
SEQ_GAP_RATIO = 0.01     # seq_gap 이 frames 의 1% 를 넘으면 손실 세션 — 제어판 세션 표의 기준과 같다


def quality(devices: Iterable[dict]) -> tuple[str, str]:
    """RX 통계 → (level, text). 제어판 세션 표·CLI 수집 직후 판정이 같은 문장을 쓴다.

    level 은 ok|check|recollect 중 가장 센 것 하나. text 는 원인과 조치를 ' / ' 로 잇는다.
    옛 manifest 에는 tx_back·seq_gap 키가 없을 수 있어 전부 .get 으로 읽는다.
    """
    level, parts = "ok", []

    def bump(to: str) -> None:
        nonlocal level
        if QUALITY_LEVELS.index(to) > QUALITY_LEVELS.index(level):
            level = to

    for d in devices:
        rx = f"RX{d.get('device_id', '?')}"
        if d.get("tx_back"):
            bump("recollect")
            parts.append("TX 가 수집 중 재부팅했습니다 — 시간 격자가 깨져 같은 라벨로 다시 찍으세요")
        if d.get("boot_changes"):
            bump("recollect")
            parts.append(f"{rx} 가 수집 중 재부팅했습니다 — 전원·케이블 확인 후 같은 라벨로 다시 찍으세요")
        if d.get("crc_fail"):
            bump("check")
            parts.append(f"{rx} CRC 실패 {d['crc_fail']}건 — 케이블 확인")
        frames = max(d.get("frames", 1), 1)
        if d.get("seq_gap", 0) > SEQ_GAP_RATIO * frames:
            bump("check")
            parts.append(f"{rx} 손실 {100 * d['seq_gap'] / frames:.1f}% — 학습엔 쓸 수 있으나 무선 경합 의심")
    # TX 재부팅은 모든 RX 에 같이 찍힌다 — 같은 문장은 한 번만
    parts = list(dict.fromkeys(parts))
    return level, " / ".join(parts)
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -v`
Expected: 9 passed

- [ ] **Step 5: 제어판 세션 표가 `quality` 를 쓰게**

`scripts/meshsense_gui.py` import 줄(33행)에 `quality` 추가:

```python
from csi_session import next_session_id, quality, read_manifest, repo_provenance, set_session_note  # noqa: E402
```

`session_rows()` 에서 `bad = any(...)` 네 줄(181-183행)을 지우고, `rows.append({...})` 의 `"warn": bad,` 를 다음으로 바꾼다:

```python
            "quality": dict(zip(("level", "text"), quality(devs))),
```

세션 탭 JS(585행 근처)의 품질 셀을 바꾼다:

```js
      <td>${s.quality.level==='ok'?'<span class="tag ok">정상</span>'
          :`<span class="tag warn">${s.quality.level==='recollect'?'재수집':'확인'}</span><br><span class="hint">${esc(s.quality.text)}</span>`}</td>
```

- [ ] **Step 6: CLI 수집 직후와 수집 러너가 같은 문장을 찍게**

`scripts/meshsense_cli.py` import(56행)에 `quality, QUALITY_TAG` 추가:

```python
from csi_session import (  # noqa: E402
    QUALITY_TAG, create_session, finalize_session, next_session_id, quality, set_session_note, summarize,
)
```

`[요약]` 출력 직후(880행 근처, `for line in summarize(manifest)` 루프 다음)에:

```python
    level, text = quality(manifest.get("devices", []))
    if level != "ok":
        print(f"\n[품질] {QUALITY_TAG[level]} — {text}")
```

그리고 버릴지 묻는 줄의 `failed` 를 `recollect` 도 포함하게:

```python
    failed = level == "recollect" or (
        venv_py is not None and _run_python(MEASURE_SCRIPT, [str(session_dir)], python=venv_py) == 5)
```

`scripts/_collect_run.py` import 에 `quality, QUALITY_TAG` 추가하고 `[요약]` 루프 뒤에 같은 세 줄을 넣는다(`flush=True` 포함):

```python
    level, text = quality(manifest.get("devices", []))
    if level != "ok":
        print(f"\n[품질] {QUALITY_TAG[level]} — {text}", flush=True)
```

- [ ] **Step 7: 기존 테스트 전부 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py tests/test_csi_session.py tests/test_train_ui.py -v`
Expected: all passed

- [ ] **Step 8: 커밋**

```bash
git add scripts/csi_session.py scripts/meshsense_gui.py scripts/meshsense_cli.py scripts/_collect_run.py tests/test_control_panel.py
git commit -m "feat : 세션 품질을 원인·조치 문장으로 — csi_session.quality 를 GUI·CLI·러너가 공유

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: `steps()`·`env_checks()` — 단계 띠와 환경 점검 데이터

**Files:**
- Modify: `scripts/meshsense_gui.py` (`readiness()` 근처 134행 이후에 함수 추가, `/api/state` 882-903행, `rescan()` 936행)
- Test: `tests/test_control_panel.py`

**Interfaces:**
- Produces: `meshsense_gui.env_checks() -> list[dict]` — 각 `{"key": "pyserial"|"venv"|"idf", "ok": bool, "text": str, "fix": str}`.
- Produces: `meshsense_gui.steps(env, boards, rows, next_id, models, realtime) -> list[dict]` — 다섯 개, 각 `{"key","state","text"}`, `state ∈ ok|current|todo`, `current` 는 첫 미완 한 칸.
- `/api/state` 에 `"steps"`, `"env"`, `"next_device_id"` 추가. `Handler.env_cache` 는 `rescan()` 이 채운다.

- [ ] **Step 1: 실패하는 테스트**

`tests/test_control_panel.py` 에 추가:

```python
from meshsense_gui import steps  # noqa: E402

ENV_OK = [{"key": "pyserial", "ok": True, "text": "pyserial", "fix": ""},
          {"key": "venv", "ok": True, "text": ".venv (numpy · matplotlib · torch)", "fix": ""},
          {"key": "idf", "ok": False, "text": "ESP-IDF — 플래시할 때만 필요", "fix": "idf"}]
RX = {"port": "/dev/cu.usbmodem101", "role": "rx", "sta_mac": "1a:00:00:00:00:65",
      "registered": True, "device_id": 101, "board_name": "RX101"}
TX = {"port": "/dev/cu.usbmodem103", "role": None, "sta_mac": None, "registered": False}


def row(label, date="20261012"):
    return {"label": label, "date": date}


FULL = [row("empty"), row("empty"), row("static"), row("static"), row("motion"), row("motion")]


class StepsTest(unittest.TestCase):
    def states(self, out):
        return [s["state"] for s in out]

    def test_everything_done_has_no_current(self):
        out = steps(ENV_OK, [RX, TX], FULL, 7, [{"trained_at": "2026-10-12 14:30:00"}],
                    {"state": "motion", "confidence": 0.94})
        self.assertEqual(self.states(out), ["ok"] * 5)
        self.assertEqual(out[4]["text"], "움직임 · 94%")

    def test_idf_does_not_block_env(self):
        out = steps(ENV_OK, [RX], FULL, 7, [{}], None)
        self.assertEqual(out[0]["state"], "ok")

    def test_first_todo_is_current_and_later_are_todo(self):
        env = [dict(ENV_OK[0]), {"key": "venv", "ok": False, "text": ".venv — torch 없음", "fix": ""}, ENV_OK[2]]
        out = steps(env, [], [], 1, [], None)
        self.assertEqual(self.states(out), ["current", "todo", "todo", "todo", "todo"])
        self.assertEqual(out[0]["text"], ".venv — torch 없음")

    def test_unregistered_rx_blocks_boards(self):
        unreg = dict(RX, port="/dev/cu.usbmodem102", sta_mac="1a:00:00:00:00:7c", registered=False, device_id=None)
        out = steps(ENV_OK, [RX, unreg, TX], FULL, 7, [{}], None)
        self.assertEqual(out[1]["state"], "current")
        self.assertEqual(out[1]["text"], "미등록 1대")

    def test_sink_counts_its_via_list(self):
        sink = {"port": "/dev/cu.usbmodem110", "role": "sink", "sta_mac": "1a:00:00:00:00:ff",
                "registered": False, "via": [RX, dict(RX, device_id=102)]}
        out = steps(ENV_OK, [sink], FULL, 7, [{}], None)
        self.assertEqual(out[1]["text"], "RX 2대")

    def test_collect_needs_two_per_class(self):
        out = steps(ENV_OK, [RX], FULL[:5], 6, [], None)
        self.assertEqual(out[2]["state"], "current")
        self.assertTrue(out[2]["text"].endswith("다음 s6"))

    def test_empty_everything(self):
        out = steps([], [], [], 1, [], None)
        self.assertEqual(self.states(out), ["current", "todo", "todo", "todo", "todo"])
        self.assertEqual(out[4]["text"], "대기")
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -k Steps -v`
Expected: FAIL — `ImportError: cannot import name 'steps'`

- [ ] **Step 3: 구현**

`scripts/meshsense_gui.py` 의 `readiness()` 바로 뒤에 추가. import 에 `from train_sessions import ... MIN_PER_CLASS` 를 더한다.

```python
RT_KO = {"empty": "빈 방", "static": "정지", "motion": "움직임",
         "deciding": "판단 중", "suspended": "판단 정지"}
VENV_PY = REPO_ROOT / ".venv" / "bin" / "python"


def env_checks() -> list[dict]:
    """보드 탭 환경 점검 카드. 느리므로(torch import ~1초) rescan() 때만 돌고 캐시된다."""
    def has(py: str, mod: str) -> bool:
        return subprocess.run([py, "-c", f"import {mod}"], capture_output=True).returncode == 0

    pk = {m: VENV_PY.is_file() and has(str(VENV_PY), m) for m in ("numpy", "matplotlib", "torch")}
    missing = [m for m, ok in pk.items() if not ok]
    return [
        {"key": "pyserial", "ok": has(PY, "serial"), "text": "pyserial",
         "fix": "python3 -m pip install pyserial"},
        {"key": "venv", "ok": not missing,
         "text": ".venv (numpy · matplotlib · torch)" if not missing else ".venv — " + " · ".join(missing) + " 없음",
         "fix": "python3 -m venv .venv && .venv/bin/pip install -r requirements-viz.txt torch"},
        {"key": "idf", "ok": (REPO_ROOT / "esp-idf" / "export.sh").is_file(),
         "text": "ESP-IDF — 플래시할 때만 필요", "fix": "idf"},
    ]


def steps(env: list, boards: list, rows: list, next_id: int, models: list,
          realtime: Optional[dict]) -> list[dict]:
    """단계 띠. 첫 번째 미완(todo) 칸이 current — 그 칸이 '지금 할 일' 이다 (spec §2.1)."""
    env_core = [e for e in env if e["key"] != "idf"]          # IDF 는 플래시 때만 필요
    env_ok = bool(env_core) and all(e["ok"] for e in env_core)
    rxs = [r for b in boards for r in (b.get("via") or [b]) if r.get("sta_mac") and b.get("role") in ("rx", "sink")]
    reg = sum(1 for r in rxs if r.get("registered"))
    unreg = len(rxs) - reg
    counts = label_counts(rows)
    today = time.strftime("%Y%m%d")
    n_today = sum(1 for r in rows if r.get("date") == today)
    latest = (models[0].get("trained_at") or "")[:10] if models else ""
    plan = [
        ("env", env_ok, "pyserial · .venv · torch" if env_ok
         else " · ".join(e["text"] for e in env_core if not e["ok"]) or "환경 점검 전"),
        ("boards", reg >= 1 and unreg == 0, f"미등록 {unreg}대" if unreg else f"RX {reg}대"),
        ("collect", all(counts[l] >= MIN_PER_CLASS for l in LABELS), f"오늘 {n_today}세션 · 다음 s{next_id}"),
        ("train", bool(models), f"모델 {len(models)}개" + (f" · 최근 {latest}" if latest else "")),
        ("realtime", bool(realtime),
         f"{RT_KO.get(realtime['state'], realtime['state'])} · {round(realtime.get('confidence', 0) * 100)}%"
         if realtime else "대기"),
    ]
    out, found = [], False
    for key, ok, text in plan:
        out.append({"key": key, "state": "ok" if ok else ("todo" if found else "current"), "text": text})
        found = found or not ok
    return out
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -v`
Expected: 16 passed

- [ ] **Step 5: `/api/state` 와 `rescan()` 에 연결**

`Handler` 클래스 변수에 `env_cache: list[dict] = []` 를 추가하고, `rescan()` 을:

```python
def rescan() -> dict:
    Handler.env_cache = env_checks()
    Handler.boards_cache = [identify(p) for p in list_ports()]
    return {"ok": True}
```

import 에 `from registry import suggest_next_device_id` 를 추가한다. `/api/state` 핸들러에서 `training_state()` 를 한 번만 부르고 `steps` 를 넣는다 — `self._json({...})` 앞에:

```python
            training = training_state()
            realtime = realtime_view()
```

그리고 dict 안에서 `"training": training_state(),` → `"training": training,`, `"realtime": realtime_view(),` → `"realtime": realtime,` 로 바꾸고 다음 세 줄을 더한다:

```python
                "env": Handler.env_cache,
                "next_device_id": suggest_next_device_id(DEVICE_REGISTRY),
                "steps": steps(Handler.env_cache, Handler.boards_cache, rows,
                               next_session_id(OUTPUT_DIR), training["models"], realtime),
```

- [ ] **Step 6: 서버가 뜨고 state 가 나오는지 확인**

Run:
```bash
.venv/bin/python scripts/meshsense_gui.py --port 8765 --no-open & sleep 6; curl -s http://127.0.0.1:8765/api/state | .venv/bin/python -c "import json,sys; s=json.load(sys.stdin); print(s['steps']); print(s['env'])"; kill %1
```
Expected: 다섯 단계 리스트와 환경 세 줄이 출력된다. 오류 없음.

- [ ] **Step 7: 커밋**

```bash
git add scripts/meshsense_gui.py tests/test_control_panel.py
git commit -m "feat : 제어판 단계 띠·환경 점검 데이터 — /api/state 에 steps·env

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: 탭 순서·기본 탭·단계 띠 화면·진단 탭 정리

**Files:**
- Modify: `scripts/meshsense_gui.py` — `PAGE`(443-475행), `CSS`(끝에 추가), `JS`(477행 `tab`, 600-614행 analyze 렌더, `refresh()`), `dispatch()` 976-980행(`/api/export` 삭제)

**Interfaces:**
- Consumes: `S.steps` (Task 2).
- 화면 전용. 서버 API 변화는 `/api/export` 삭제뿐.

- [ ] **Step 1: 탭 순서와 기본 탭**

`PAGE` 의 `<nav id="nav">` 를 다음으로 교체:

```html
  <nav id="nav">
    <button data-t="meta">실험 정보</button>
    <button data-t="boards">보드</button>
    <button data-t="collect" class="on">수집</button>
    <button data-t="sessions">세션</button>
    <button data-t="train">학습</button>
    <button data-t="realtime">실시간</button>
    <button data-t="analyze">진단</button>
  </nav>
```

`<main>` 의 `<section id="realtime" class="on">` 에서 `class="on"` 을 떼고 `<section id="collect" class="on">` 으로 옮긴다. `JS` 첫 줄 `let S = null, tab = 'realtime', ...` 을 `tab = 'collect'` 로.

- [ ] **Step 2: 단계 띠 마크업·CSS·렌더**

`PAGE` 의 `</header>` 바로 뒤, `<main>` 앞에:

```html
<div id="steps"></div>
```

`CSS` 끝에 추가:

```css
/* 단계 띠 — 첫 미완 칸(current)만 강조. 띠가 '지금 할 일' 이다 */
#steps{max-width:1000px;margin:1rem auto 0;padding:0 1rem;display:flex;gap:.4rem;flex-wrap:wrap}
#steps .step{flex:1;min-width:8.5rem;text-align:left;font:inherit;cursor:pointer;padding:.5rem .7rem;
 border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--fg)}
#steps .step b{display:block;font-size:.9rem;font-weight:600}
#steps .step span{color:var(--muted);font-size:.8rem}
#steps .step.ok b{color:var(--ok)}
#steps .step.current{border:2px solid var(--accent)}
#steps .step.todo{opacity:.75}
```

`JS` 의 `refresh()` 를:

```js
async function refresh() {
  S = await (await fetch('/api/state')).json();
  renderSteps(); render(); renderJob();
}
const STEP_KO = {env:'환경', boards:'보드', collect:'수집', train:'학습', realtime:'실시간'};
const STEP_TAB = {env:'boards', boards:'boards', collect:'collect', train:'train', realtime:'realtime'};
function renderSteps() {
  if (!changed('steps', S.steps)) return;
  document.getElementById('steps').innerHTML = (S.steps || []).map(s =>
    `<button class="step ${s.state}" onclick="goTab('${STEP_TAB[s.key]}')">
       <b>${s.state === 'ok' ? '✓ ' : ''}${STEP_KO[s.key]}</b><span>${esc(s.text)}</span></button>`).join('');
}
function goTab(t) { const b = document.querySelector(`#nav button[data-t="${t}"]`); if (b) b.click(); }
```

- [ ] **Step 3: 진단 탭에서 JSONL 카드 제거, `/api/export` 삭제**

`JS` 의 analyze 렌더(600-614행)에서 두 번째 `<div class="card"><h2>전처리용 JSONL 내보내기</h2> … </div>` 블록을 통째로 지운다. 첫 카드만 남는다. `dispatch()` 의 `if path == "/api/export": …` 분기(976-980행)를 지운다.

- [ ] **Step 4: 브라우저 확인**

Run: `.venv/bin/python scripts/meshsense_gui.py --port 8765 --no-open` 을 띄우고 내장 브라우저로 `http://127.0.0.1:8765/` 를 연다.
Expected:
- 첫 화면이 **수집** 탭. 탭 순서 `실험 정보 · 보드 · 수집 · 세션 · 학습 · 실시간 · 진단`.
- 헤더 아래 다섯 칸 띠. 한 칸만 파란 테두리(current). 칸을 누르면 해당 탭으로 이동(`환경` 은 보드 탭).
- 진단 탭에 분리 가능성 카드만 있고 JSONL 카드 없음.
- 콘솔 에러 없음.

- [ ] **Step 5: 커밋**

```bash
git add scripts/meshsense_gui.py
git commit -m "feat : 제어판 단계 띠와 생애주기 순서 탭 — 기본 탭 수집, 진단 탭에서 JSONL 제거

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: 보드 탭 — 환경 점검 카드·RX 등록 버튼·ESP-IDF 준비·TX 문구

**Files:**
- Modify: `scripts/meshsense_gui.py` — boards 렌더(537-559행), `dispatch()`(`/api/register`·`/api/idf` 추가), JS 끝에 `registerRx()`

**Interfaces:**
- Consumes: `S.env`, `S.next_device_id` (Task 2).
- Produces: `POST /api/register {sta_mac, name}` → `{ok}|{error}`; `POST /api/idf` → `JOB` 시작.

- [ ] **Step 1: 서버 엔드포인트**

`dispatch()` 의 `if path == "/api/flash":` 앞에 추가:

```python
    if path == "/api/register":
        mac = str(body.get("sta_mac", "")).strip().lower()
        name = str(body.get("name", "")).strip()
        if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
            return {"error": "MAC 형식이 아닙니다"}
        if not re.fullmatch(r"[\w-]{1,20}", name):
            return {"error": "이름은 글자·숫자·밑줄·하이픈만, 20자까지 씁니다"}
        # 포트를 프로브하지 않는다 — MAC 은 IDENT 로 이미 안다 (CLAUDE.md 재발 방지)
        r = subprocess.run([PY, str(SCRIPT_DIR / "device_registry.py"), "add", "--mac", mac,
                            "--board-name", name], capture_output=True, text=True, cwd=str(REPO_ROOT))
        if r.returncode:
            return {"error": (r.stderr or r.stdout).strip() or "등록 실패"}
        threading.Thread(target=rescan, daemon=True).start()   # 식별 표의 '미등록' 을 지운다
        return {"ok": True}

    if path == "/api/idf":
        err = JOB.start("ESP-IDF 준비", [PY, str(SCRIPT_DIR / "idf_bootstrap.py"), "-y"],
                        on_done=lambda rc: rescan() and "")   # 설치 뒤 환경 점검을 다시 한다
        return {"error": err} if err else {"ok": True}
```

- [ ] **Step 2: 보드 탭 렌더**

boards 렌더의 `rows = S.boards.map(b => ...)` 에서 식별 셀을 다음으로 교체한다 (미등록 RX 에 등록 버튼, TX 문구):

```js
    const regBtn = r => `<button class="act" ${busy?'disabled':''} onclick="registerRx('${esc(r.sta_mac)}')">RX${S.next_device_id} 으로 등록</button>`;
    const ident = r => r.registered ? `<span class="tag ok">${esc(r.board_name || ('RX'+r.device_id))}</span>`
                     : `<span class="tag warn">미등록 RX</span> <span class="hint">${esc(r.sta_mac)}</span> ${regBtn(r)}`;
    const rows = S.boards.map(b => `<tr>
      <td><code>${esc(b.port)}</code></td>
      <td>${b.role === 'sink' ? `<span class="tag ok">SINK</span> ` + (b.via||[]).map(ident).join(' ')
        : b.sta_mac ? ident(b)
        : `<span class="tag">TX 또는 미플래시</span> <span class="hint">TX 라면 정상</span>`}</td>
      <td>
        <button class="act" ${busy?'disabled':''} onclick="post('/api/flash',{port:'${b.port}',kind:'rx'})">RX 플래시</button>
        <button class="act" ${busy?'disabled':''} onclick="post('/api/flash',{port:'${b.port}',kind:'tx'})">TX 플래시</button>
      </td></tr>`).join('');
```

표 헤더에서 `<th>MAC</th>` 열을 지운다(MAC 은 미등록일 때만 식별 셀에 보인다). 카드 아래의 "등록되지 않은 보드는 먼저 registry 에 추가해야 … `device_registry.py add`" 안내 `<p>` 를 지운다. `changed('boards', [...])` 의 지문에 `S.env, S.next_device_id` 를 더한다.

같은 `innerHTML` 맨 앞에 환경 점검 카드를 붙인다:

```js
    const envRows = (S.env || []).map(e => `<tr>
      <td style="width:5rem">${e.ok ? '<span class="tag ok">통과</span>' : '<span class="tag warn">없음</span>'}</td>
      <td>${esc(e.text)}${e.ok ? '' : e.fix === 'idf'
        ? ` <button class="act" ${busy?'disabled':''} onclick="post('/api/idf')">준비하기</button> <span class="hint">(idf_bootstrap, 약 10분)</span>`
        : ` <span class="hint">터미널에서: <code>${esc(e.fix)}</code></span>`}</td></tr>`).join('');
    document.getElementById('boards').innerHTML = `<div class="card">
      <h2>환경 점검</h2>
      <p class="sub">수집에는 pyserial 과 .venv 가, 플래시에는 ESP-IDF 가 필요합니다. '다시 검색' 을 누르면 다시 점검합니다.</p>
      <table>${envRows}</table></div>
    <div class="card">
      <h2>연결된 보드</h2>
      <p class="sub">보드는 2초마다 자기 MAC 을 알립니다 — 확인만으로는 보드가 리셋되지 않습니다.
      TX 는 아무것도 보내지 않으므로 'TX 또는 미플래시' 로 표시되는 것이 정상입니다.
      SINK 포트는 경유하는 RX 목록으로 표시됩니다.</p>
      <div class="bar"><button class="act" ${busy?'disabled':''} onclick="post('/api/rescan')">다시 검색</button></div>
      ${S.boards.length ? `<table><tr><th>포트</th><th>식별</th><th>플래시</th></tr>${rows}</table>`
        : `<p class="sub">USB 시리얼 포트를 찾지 못했습니다. 보드 연결을 확인하세요.</p>`}
    </div>`;
```

JS 끝(`refresh(); setInterval(...)` 앞)에:

```js
function registerRx(mac){
  const name = prompt('보드 이름 (registry 의 board_name)', 'RX' + S.next_device_id);
  if (name !== null) post('/api/register', {sta_mac: mac, name: name.trim()});
}
```

- [ ] **Step 3: 테스트·서버 기동 확인**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -v` → Expected: 16 passed (회귀 없음).
Run: Task 2 Step 6 의 curl 명령 → Expected: `env` 세 줄에 `fix` 키가 있고 서버 오류 없음.

- [ ] **Step 4: 등록 엔드포인트 거절 경로 확인 (보드 없이)**

Run:
```bash
curl -s -X POST http://127.0.0.1:8765/api/register -H 'Content-Type: application/json' -d '{"sta_mac":"zz","name":"RX9"}'
```
Expected: `{"error": "MAC 형식이 아닙니다"}`

Run (이미 registry 에 있는 MAC 하나를 `mac_collector/device_registry.csv` 에서 읽어):
```bash
curl -s -X POST http://127.0.0.1:8765/api/register -H 'Content-Type: application/json' -d '{"sta_mac":"<기존 MAC>","name":"RX9"}'
```
Expected: `{"error": "error: MAC … already registered"}` — registry 는 바뀌지 않는다(`git diff mac_collector/` 비어 있음).

- [ ] **Step 5: 브라우저 확인**

Expected (보드 탭):
- 환경 점검 카드 세 줄, 통과/없음 태그. ESP-IDF 없음이면 '준비하기' 버튼.
- TX 포트 행이 `TX 또는 미플래시 · TX 라면 정상`.
- 미등록 RX 가 꽂혀 있으면 `RX<n> 으로 등록` 버튼 → prompt → 등록 후 몇 초 내 `RX<n>` 태그로 바뀜. 미등록 보드가 없으면 이 줄은 건너뛰고 Step 4 의 curl 결과로 갈음한다.

- [ ] **Step 6: 커밋**

```bash
git add scripts/meshsense_gui.py
git commit -m "feat : 보드 탭에서 환경 점검·RX 등록·ESP-IDF 준비 — 터미널 왕복 제거

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: `RT_JOB` — 수집 중 실시간 따라가기

**Files:**
- Modify: `scripts/meshsense_gui.py` — `JOB = Job()`(112행) 옆, `realtime_sessions()`(264행) 근처에 `latest_session_dir()`, `dispatch()` 의 `/api/realtime/start`(993-1006행), `/api/realtime/stop` 추가, `/api/state` 에 `rt_job`, `main()` finally, JS `renderRealtimeStart`·`renderRealtime`·`render()` 의 realtime 분기
- Test: `tests/test_control_panel.py`

**Interfaces:**
- Produces: `meshsense_gui.latest_session_dir(raw_root: Path) -> Optional[Path]` — `session.json` 이 있는 가장 최근(이름 역순) 실세션 디렉터리. demo 는 제외.
- `POST /api/realtime/start {model, follow, session?, speed?}` — `follow` 면 `session` 생략 가능. `POST /api/realtime/stop`.
- `/api/state["rt_job"]` = `RT_JOB.state()`.

- [ ] **Step 1: 실패하는 테스트**

`tests/test_control_panel.py` 에 추가:

```python
import tempfile  # noqa: E402  (파일 상단 import 묶음에 넣는다)
from meshsense_gui import latest_session_dir  # noqa: E402


class LatestSessionTest(unittest.TestCase):
    def test_no_sessions_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(latest_session_dir(Path(tmp)))

    def test_newest_dir_with_manifest_wins_and_unfinished_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            old = raw / "20261011" / "150000_empty_s45"; old.mkdir(parents=True)
            (old / "session.json").write_text("{}", encoding="utf-8")
            new = raw / "20261012" / "140300_motion_s48"; new.mkdir(parents=True)
            (new / "session.json").write_text("{}", encoding="utf-8")
            stray = raw / "20261012" / "150000_static_s49"; stray.mkdir(parents=True)   # manifest 없음
            self.assertEqual(latest_session_dir(raw), new)
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -k Latest -v`
Expected: FAIL — `ImportError: cannot import name 'latest_session_dir'`

- [ ] **Step 3: 구현 — 서버**

`JOB = Job()` 아래:

```python
JOB = Job()          # 플래시·수집·학습·진단·IDF — 포트를 쓰므로 한 번에 하나
RT_JOB = Job()       # 실시간 판단 — 파일만 읽으므로 수집과 같이 돈다 (spec §2.5)
```

`realtime_sessions()` 앞에:

```python
def latest_session_dir(raw_root: Path = RAW_ROOT) -> Optional[Path]:
    """따라가기가 붙을 세션 — session.json 이 있는 가장 최근 실세션. 수집 지연 중이면 None."""
    for d in sorted(Path(raw_root).glob("*/*"), reverse=True):
        if (d / "session.json").is_file():
            return d
    return None
```

`dispatch()` 의 `/api/realtime/start` 분기를 다음으로 교체:

```python
    if path == "/api/realtime/start":
        follow = bool(body.get("follow"))
        if follow and not body.get("session"):
            session = latest_session_dir()
            if session is None:
                return {"error": "수집 세션이 아직 없습니다 — 지연이 끝난 뒤 다시 누르세요"}
        else:
            session = (REPO_ROOT / str(body.get("session", ""))).resolve()
            if not str(session).startswith(str(REPO_ROOT)) or not (session / "session.json").is_file():
                return {"error": "세션 디렉터리가 아닙니다"}
        model = (REPO_ROOT / str(body.get("model", ""))).resolve()
        if not str(model).startswith(str(REPO_ROOT)) or model.suffix != ".pt":
            return {"error": "체크포인트가 아닙니다"}
        cmd = [PY, str(REPO_ROOT / "realtime" / "ui.py"), "--session", str(session),
               "--model", str(model), "--plain", "--speed", str(float(body.get("speed") or 2))]
        if follow:
            cmd.append("--follow")
        err = RT_JOB.start(f"실시간 · {model.stem.replace('model_', '')} · {session.name}", cmd)
        return {"error": err} if err else {"ok": True}

    if path == "/api/realtime/stop":
        RT_JOB.stop()
        return {"ok": True}
```

`/api/state` dict 에 `"rt_job": RT_JOB.state(),` 추가. `main()` 의 `finally:` 에 `RT_JOB.stop()` 추가:

```python
    finally:
        JOB.stop()
        RT_JOB.stop()
        srv.server_close()
```

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m pytest tests/test_control_panel.py -v`
Expected: 18 passed

- [ ] **Step 5: 구현 — 화면**

`render()` 의 realtime 분기 지문에 `S.rt_job.running, S.job.name` 을 넣고 `busy` 대신 `rtBusy` 를 쓴다:

```js
  const rtBusy = S.rt_job && S.rt_job.running;
  if (tab === 'realtime' && changed('realtime', [S.realtime, S.rt_models, S.rt_sessions, rtBusy,
                                                  S.job.running, S.job.name, rtSession, rtModel])) {
    document.getElementById('realtime').innerHTML =
      renderRtStatus(S) + renderRealtime(S.realtime) + renderRealtimeStart(S, rtBusy);
  }
```

`renderRealtimeStart(S, busy)` 는 시그니처를 그대로 두되 안의 `${busy ? '<span class="hint">다른 작업이 실행 중입니다</span>' : ''}` 를 `${busy ? '<span class="hint">실시간이 이미 돌고 있습니다 — 위에서 중지하세요</span>' : ''}` 로 바꾼다. 따라가기 버튼은 세션을 보내지 않는다:

```js
function startRealtime(follow){
  post('/api/realtime/start', {session: follow ? null : rtSession, model: rtModel, follow: follow, speed: 2});
}
```

그리고 따라가기 버튼 라벨을 `따라가기 (수집 중인 세션)` 로. 상태 태그 카드:

```js
function renderRtStatus(S){
  const j = S.job, r = S.rt_job;
  if (!j.running && !r.running) return '';
  return `<div class="card"><div class="bar" style="margin:0">
    ${j.running ? `<span class="tag ok">${esc(j.name)}</span>` : ''}
    ${r.running ? `<span class="tag ok">${esc(r.name)}</span>
      <button class="act danger" onclick="post('/api/realtime/stop')">실시간 중지</button>` : ''}
  </div></div>`;
}
```

`renderJob()` 은 `JOB` 만 보이므로 그대로 둔다 — 실시간의 출력은 상태 파일로 화면에 이미 나온다.

- [ ] **Step 6: 브라우저 확인 — 재생**

실시간 탭에서 녹화 세션 하나를 고르고 `재생 (2배속)`.
Expected: 상단에 `실시간 · …` 태그와 `실시간 중지` 버튼. 다른 탭(수집)의 `수집 시작` 버튼이 **비활성화되지 않음**. `실시간 중지` 로 멈춤.

- [ ] **Step 7: 브라우저 확인 — 수집 중 따라가기 (보드 있을 때)**

수집 탭에서 `empty` 20초·지연 5초로 시작 → 지연 중 실시간 탭 `따라가기` → `수집 세션이 아직 없습니다 …` alert → 지연 후 다시 누르면 시작. 수집이 끝나도 실시간은 계속 돈다. `Ctrl+C` 로 서버를 끄면 두 프로세스 모두 사라진다(`ps aux | grep realtime/ui.py` 비어 있음). 보드가 없으면 이 단계는 Step 6 과 `ps` 확인으로 갈음한다.

- [ ] **Step 8: 커밋**

```bash
git add scripts/meshsense_gui.py tests/test_control_panel.py
git commit -m "feat : 실시간 작업 슬롯 분리 — 수집 중에도 따라가기, 세션은 자동 선택

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: CLI 메뉴 — 세션 보기·실시간

**Files:**
- Modify: `scripts/meshsense_cli.py` — `_menu_usb_pipeline()`(1030-1058행), 새 함수 `_menu_sessions()`·`_menu_realtime()`
- Modify: `scripts/README.md:115-130` (러너 표)

**Interfaces:**
- Consumes: `csi_session.quality`·`QUALITY_TAG` (Task 1), `train_sessions.list_sessions`·`group_by_date`, `realtime.engine.available_models`.

- [ ] **Step 1: 세션 보기**

`_menu_train()` 앞에 추가:

```python
def _menu_sessions() -> None:
    """날짜별 세션과 품질 원인. 제어판 세션 탭과 같은 문장(csi_session.quality)."""
    from csi_session import read_manifest
    sessions = list_sessions()
    if not sessions:
        print("\n  아직 수집한 세션이 없습니다.")
        return
    for date, items in group_by_date(sessions).items():
        print(f"\n  {date}")
        for s in items:
            devs = read_manifest(REPO_ROOT / s["path"]).get("devices", [])
            level, text = quality(devs)
            print(f"    {s['label']:7s} {s['span_s']:>5.0f}s {s['hz']:>5.1f}Hz  RX{s['rx']}  {s['name']}"
                  f"  [{QUALITY_TAG[level]}]" + (f"  {text}" if text else ""))
```

- [ ] **Step 2: 실시간**

```python
def _menu_realtime() -> None:
    """모델·세션을 골라 realtime/ui.py 를 띄운다. 따라가기는 가장 최근 세션."""
    sys.path.insert(0, str(REPO_ROOT))
    from realtime.engine import available_models
    models = available_models()
    if not models:
        print("\n  쓸 수 있는 체크포인트가 없습니다 (model_train/*/model*.pt).")
        return
    mi = _choose("모델", [f"{m['name']}  {m['mode']} 창 {m['window_s']:.0f}초"
                         + (" (배포)" if m["is_deployed"] else "") for m in models])
    recent = sorted((REPO_ROOT / "mac_collector_output" / "raw").glob("*/*"), reverse=True)
    recent = [d for d in recent if (d / "session.json").is_file()][:12]
    if not recent:
        print("\n  세션이 없습니다.")
        return
    si = _choose("세션", [f"{d.parent.name} {d.name}" for d in recent] + ["따라가기 (가장 최근 세션, 수집 중)"])
    follow = si == len(recent)
    session = recent[0] if follow else recent[si]
    args = ["--session", str(session), "--model", str(REPO_ROOT / models[mi]["path"])]
    args += ["--follow"] if follow else ["--speed", "2"]
    _run_python(REPO_ROOT / "realtime" / "ui.py", args)
```

- [ ] **Step 3: 메뉴에 끼우기**

`_menu_usb_pipeline()` 의 `_choose("선택", [...])` 목록과 분기를 다음으로 교체:

```python
        idx = _choose(
            "선택",
            [
                "보드 플래시 (PoC, MAC 자동 매칭)",
                "수집 (라벨·시간 입력)",
                "블록 순서 뽑기 (클래스 순서 무작위)",
                "세션 보기 (날짜별 · 품질 원인)",
                "모델 학습 (세션 선택)",
                "실시간 (모델·세션 고르기)",
                "세션 메타 편집 (브라우저 폼)",
                "보드 관리 (registry 등록·검증)",
                "파이프라인 선택으로 돌아가기",
            ],
        )
        if idx == 0:
            _flash_poc_board()
            _pause()
        elif idx == 1:
            _collect_poc_interactive()
            _pause()
        elif idx == 2:
            order = _draw_block_order(order)
        elif idx == 3:
            _menu_sessions()
            _pause()
        elif idx == 4:
            _menu_train()
        elif idx == 5:
            _menu_realtime()
            _pause()
        elif idx == 6:
            _open_session_form()
        elif idx == 7:
            _menu_board_management()
        else:
            break
```

- [ ] **Step 4: 터미널 확인**

Run: `.venv/bin/python scripts/meshsense_cli.py --quick` → `4` → 세션이 날짜별로 나오고 `[정상]`/`[확인] …`/`[재수집] …` 꼬리가 붙는다 → Enter → `6` → 모델·세션 선택 후 재생 화면이 뜨고 `q` 로 나온다 → `9`.

- [ ] **Step 5: README 러너 표**

`scripts/README.md` 의 `_idf_flash.py / _collect_run.py` 행을 다음 두 행으로 바꾼다:

```markdown
| `_idf_flash.py` / `_collect_run.py` / `_train_run.py` | 제어판이 백그라운드로 돌리는 플래시·수집·학습 러너. 제어판 없이 직접 부를 수 있다 (`_collect_run.py <label> <sec> [delay]`, `_train_run.py --preset … --sessions …`) |
| `../realtime/ui.py` | 실시간 판단 화면 — `--session <dir> --follow` (수집 중) 또는 `--speed 2` (재생). `--list-models` 로 체크포인트 목록 |
```

- [ ] **Step 6: 커밋**

```bash
git add scripts/meshsense_cli.py scripts/README.md
git commit -m "feat : CLI 메뉴에 세션 보기·실시간 추가 — 러너 스크립트를 README 에 드러냄

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: 문서 갱신·전체 확인·스프린트 기록

**Files:**
- Modify: `doc/control-panel.md` (상태 → CURRENT), `doc/README.md` (7b 상태), `doc/quickstart.md:55-83` (§4)
- Create: `doc/sprint/2026-10-control-panel-flow.md`

- [ ] **Step 1: quickstart §4 제어판 절차**

`### 제어판 (권장)` 아래 번호 목록을 다음으로 교체:

```markdown
1. **단계 띠** — 화면 맨 위 `환경 → 보드 → 수집 → 학습 → 실시간`. 파란 테두리 칸이 지금 할 일이다.
   칸을 누르면 그 탭이 열린다.
2. **보드** — 환경 점검(pyserial·.venv·ESP-IDF)과 연결된 보드. 보드는 2초마다 IDENT 를 보내므로
   **확인만으로는 리셋되지 않는다.** TX 는 아무것도 보내지 않아 `TX 또는 미플래시` 로 보이는 것이
   정상이다. 미등록 RX 는 `RX<n> 으로 등록` 버튼으로 그 자리에서 등록한다.
3. **수집** — 라벨(`empty`/`static`/`motion`)·시간·시작 지연을 고르고 시작. 블록 순서 뽑기도 여기.
4. **세션** — 품질이 `정상 | 확인 | 재수집` 과 원인 문장으로 보인다. `재수집` 이면 같은 라벨로 다시 찍는다.
5. **학습** — 세션을 고르고 평가 날짜·설정을 정해 학습.
6. **실시간** — 수집 중이면 `따라가기 (수집 중인 세션)`, 녹화면 `재생`. 수집과 동시에 돌 수 있다.
7. **진단** — 라벨당 2세션 이상 모이면 분리 가능성 진단.
```

`### 터미널` 의 메뉴 설명 줄을:

```markdown
`[1] 보드 플래시` / `[2] 수집` / `[3] 블록 순서` / `[4] 세션 보기` / `[5] 모델 학습` / `[6] 실시간` /
`[7] 세션 메타 편집` / `[8] 보드 관리`. JSONL 내보내기(공식 3-RX 전처리 입력)는
`python3 scripts/export_jsonl.py --print-labels` 로 직접 실행한다.
```

- [ ] **Step 2: 상태 전환**

`doc/control-panel.md` 2행을 `> 상태: **CURRENT** (2026-10-12 승인, 구현 완료)` 로. `doc/README.md` 7b 행의 `**OFFICIAL DESIGN**` 을 `**CURRENT**` 로.

- [ ] **Step 3: 스프린트 기록**

`doc/sprint/2026-10-control-panel-flow.md`:

```markdown
# 제어판·CLI 흐름 스프린트 (2026-10)

> 상태: **HISTORICAL** (작업 로그)
> 설계: [control-panel.md](../control-panel.md)

## 2026-10-12 — 비개발자 제어판 / 개발자 CLI 역할 분리

### 배경
탭이 `실시간` 부터 시작해 생애주기 순서가 화면에 없었고, 보드 등록은 터미널로, 품질은 `품질 확인`
한 단어였다. 작업 슬롯이 하나라 수집 중 `따라가기` 버튼이 항상 꺼져 있었다 (`busy` 가드).

### 한 일
- `csi_session.quality()` — 품질 문장 단일 소스. GUI 세션 표·CLI 수집 직후·`_collect_run.py` 공용.
- `/api/state` 에 `steps`·`env`·`rt_job`·`next_device_id`. 단계 띠는 첫 미완 칸만 강조.
- 보드 탭: 환경 점검 카드, `RX<n> 으로 등록`(`device_registry.py add --mac`, 프로브 없음), `ESP-IDF 준비`.
- `RT_JOB` 분리 — 수집과 실시간 동시 실행. 따라가기는 최근 세션 자동.
- 진단 탭에서 JSONL 카드 제거(`/api/export` 삭제). CLI 메뉴에 `세션 보기`·`실시간`.

### 실측
- (구현 시 기록) 단계 띠 포함 `/api/state` 응답 시간, 보드 n대 rescan 소요, 수집 중 따라가기 시작까지 지연.

### 막힌 지점
- (구현 시 기록)

### 하지 않은 것
위저드·TX IDENT 펌웨어·배포 버튼·CLI 서브커맨드 — [control-panel.md §6](../control-panel.md).
```

구현 중 실제 수치와 막힌 지점을 이 파일의 `(구현 시 기록)` 자리에 채운다. 빈 자리를 남기지 않는다.

- [ ] **Step 4: 전체 테스트와 흐름 확인**

Run: `.venv/bin/python -m pytest tests/ -q -x --ignore=tests/test_lstm.py --ignore=tests/test_cnn1d.py`
Expected: 통과 (기존 느린 학습 테스트는 제외).

브라우저 전체 순서: 보드 탭(환경·보드) → 수집 탭 기본 → 세션 탭 문장 → 실시간 재생+중지 → 진단 탭. 콘솔 에러 0.

- [ ] **Step 5: 커밋**

```bash
git add doc/control-panel.md doc/README.md doc/quickstart.md doc/sprint/2026-10-control-panel-flow.md
git commit -m "docs : 제어판·CLI 흐름을 CURRENT 로 — quickstart §4, 스프린트 기록

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```
