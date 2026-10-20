#!/usr/bin/env python3
"""MeshSense 브라우저 제어판 — 터미널 없이 수집·진단을 할 수 있게 한다.

    python scripts/meshsense_gui.py

표준 라이브러리만 쓰고(`http.server`) **127.0.0.1 에만 바인딩**한다. 보드를 플래시하고
데이터를 지우는 화면이라 외부에 열어둘 이유가 없다.

무거운 작업(플래시·수집·진단)은 백그라운드 프로세스로 돌리고 로그를 실시간으로 보여준다.
한 번에 하나만 실행한다 — 플래시 중에 수집을 시작하면 포트가 충돌한다.
"""
from __future__ import annotations

import argparse
import glob
import importlib.util
import http.server
import re
import json
import shutil
import subprocess
import time
import sys
import threading
import urllib.parse
import webbrowser
from collections import deque
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import block_order  # noqa: E402
import model_registry  # noqa: E402
#: 화면에서 지울 수 없는 체크포인트. 지우면 실시간이 뜨지 않거나(기본 모델)
#: 조용히 느려진다(1단계 게이트 — 멈추지 않아 더 찾기 어렵다). 2026-10-20 실측으로 확인했다.
#: 담당자 배포본은 지워도 멈추지 않으므로 막지 않고 팝업이 경고만 한다.
PROTECTED = model_registry.PINNED
from csi_session import (  # noqa: E402
    LIVE_LABEL, SESSION_LABELS, VERDICT, next_session_id, quality, read_manifest,
    set_session_note,
)
from csi_store import LABELS  # noqa: E402
from train_sessions import (  # noqa: E402
    EXCLUDED_SESSIONS, available_eval_dates, check_selection, group_by_date, is_excluded,
    list_sessions,
)
from session_form import (  # noqa: E402
    DEVICE_REGISTRY, FIELDS, SESSION_META, known_devices, read_meta, write_meta,
)

OUTPUT_DIR = REPO_ROOT / "mac_collector_output"
RAW_ROOT = OUTPUT_DIR / "raw"
SEND_POC = REPO_ROOT / "esp32s3_csi_send_poc"
RECV_POC = REPO_ROOT / "esp32s3_csi_recv_poc"
PY = sys.executable

sys.path.insert(0, str(SCRIPT_DIR))
from _train_run import (  # noqa: E402
    PRESETS as _PRESETS, estimate_seconds, predict_windows, preset_shape,
)

PRESET_INFO = {k: {"label": v["label"], "detail": v["detail"], **preset_shape(v)}
               for k, v in _PRESETS.items()}

LABEL_DESC = {"empty": "부재 — 공간에 사람 없음",
              "static": "정지 — 사람이 있으나 움직이지 않음",
              "motion": "움직임 — 사람이 움직이는 중",
              LIVE_LABEL: "실시간 — 여러 상태를 섞어 찍음 (학습에 안 씀)"}


# ── 백그라운드 작업 ────────────────────────────────────────────────────────────
class Job:
    """한 번에 하나만 도는 백그라운드 작업. 로그를 링버퍼에 모아 화면에 흘린다."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.name = ""
        self.proc: Optional[subprocess.Popen] = None
        self.log: deque[str] = deque(maxlen=400)
        self.rc: Optional[int] = None
        self.done_note = ""
        self.on_done = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, name: str, cmd: list[str], on_done=None) -> Optional[str]:
        with self.lock:
            if self.running:
                return f"이미 실행 중입니다: {self.name}"
            self.name, self.rc, self.done_note, self.on_done = name, None, "", on_done
            self.log.clear()
            self.log.append(f"$ {' '.join(cmd)}")
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(REPO_ROOT),
            )
            threading.Thread(target=self._pump, daemon=True).start()
        return None

    def _pump(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.log.append(line.rstrip())
        self.rc = self.proc.wait()
        if self.on_done:
            try:
                self.done_note = self.on_done(self.rc) or ""
            except Exception as exc:                       # 콜백 실패로 서버가 죽지 않게
                self.done_note = f"후처리 실패: {exc}"
        self.log.append(f"— 종료 (코드 {self.rc}) {self.done_note}")

    def stop(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            p.terminate()

    def state(self) -> dict:
        return {"running": self.running, "name": self.name,
                "rc": self.rc, "log": list(self.log), "note": self.done_note}


JOB = Job()       # 플래시·수집·학습·진단 — 포트를 쓰므로 한 번에 하나
RT_JOB = Job()    # 실시간 판단 — 파일만 읽으므로 수집과 같이 돈다
LIVE_JOB = "실시간 측정 · 수집"
#: 운영자가 `판단 정지` 를 눌렀는가. RX 끊김과 구분해 화면에 다른 말을 쓴다.
PAUSED_BY_OPERATOR = [False]
#: 마지막으로 고른 모델. 일시정지에서 돌아올 때 같은 모델로 다시 붙인다.
LIVE_MODEL = [""]
MODEL_DEFAULT = "model_train/amp60/model_amp1500_all74.pt"
#: 엔진(`realtime/engine.py`)이 정본이다. 여기 베껴 둔 이유는 engine import 가 torch 를
#: 끌어와 제어판 시작이 느려지기 때문이고, 두 값이 같은지는 테스트가 묶어 둔다.
MARKS_NAME = "realtime_marks.jsonl"
IDF_EXPORT = REPO_ROOT / "esp-idf" / "export.sh"


#: 학습·파형에만 쓰이는 패키지. 없어도 **수집은 막지 않는다**.
VENV_PACKAGES = {name: importlib.util.find_spec(name) is not None
                 for name in ("numpy", "matplotlib", "torch")}


# ── 상태 조회 ─────────────────────────────────────────────────────────────────
def list_ports() -> list[str]:
    return sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))


def identify(port: str) -> dict:
    """IDENT 로 보드 식별. esptool 과 달리 보드를 리셋하지 않는다."""
    try:
        r = subprocess.run(
            [PY, str(SCRIPT_DIR / "csi_serial_reader.py"), "--port", port,
             "--identify", "--ident-timeout", "4"],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=20,
        )
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"port": port, "registered": False, "device_id": None,
                "sta_mac": None, "firmware": None, "board_name": ""}


def _registered_rx(boards: list) -> list[dict]:
    """등록된 RX 들. SINK 포트는 뒤에 붙은 RX(`via`)로 센다."""
    return [r for b in boards for r in (b.get("via") or [b]) if r.get("registered")]


def last_session_hz() -> dict:
    """보드별 마지막 세션의 Hz. **실측이 아니다** — 포트를 열지 않고 매니페스트에서 읽는다."""
    out: dict = {}
    for d in sorted(RAW_ROOT.glob("*/*"), reverse=True):
        if not (d / "session.json").is_file():
            continue
        try:
            m = read_manifest(d)
        except Exception:
            continue
        for x in m.get("devices", []):
            span = x.get("span_s") or 0
            if span and x["device_id"] not in out:
                out[x["device_id"]] = round(x["frames"] / span, 1)
        if len(out) >= 3:
            break
    return out


def ready_line(boards: list | None = None, job_running: bool | None = None,
               packages: dict | None = None, pyserial: bool | None = None,
               last_hz: dict | None = None, notes: list | None = None,
               idf: bool | None = None) -> dict:
    """수집 화면 맨 위 한 줄. 옛 보드 탭이 여기로 들어온다 (doc/control-panel.md §3.1).

    **수집에 필요한 것만 막는다** — `pyserial` 과 RX 3대. torch·matplotlib 은 학습과 파형에만
    쓰이므로 없어도 수집은 돌아가야 한다.

    **포트를 다시 찾지 않는다.** 보드 목록은 `/api/rescan` 이 채운 캐시이고 Hz 는 마지막 세션
    매니페스트에서 읽는다. 수집 중에 포트를 건드리면 수집기와 다툰다 (CLAUDE.md).
    """
    boards = Handler.boards_cache if boards is None else boards
    job_running = JOB.running if job_running is None else job_running
    if job_running:
        return {"level": "ok", "text": "수집 중 — 보드 점검을 멈춥니다", "rows": []}

    rows: list[dict] = []
    rx = _registered_rx(boards)
    last_hz = last_session_hz() if last_hz is None else last_hz
    for r in sorted(rx, key=lambda x: x.get("device_id") or 0):
        hz = last_hz.get(r.get("device_id"))
        rows.append({"key": f"rx{r.get('device_id')}", "level": "ok", "action": None,
                     "text": f"RX{r.get('device_id')} 연결됨"
                             + (f" · 지난 세션 {hz}Hz" if hz else "")})
    for b in boards:
        for r in (b.get("via") or [b]):
            if not r.get("registered") and r.get("sta_mac"):
                rows.append({"key": f"new-{r['sta_mac']}", "level": "warn", "action": "register",
                             "text": f"등록되지 않은 보드 {r['sta_mac']}"})
        #: 포트마다 플래시. 보드 탭을 지워도 할 수 있는 일은 그대로다.
        rows.append({"key": f"port-{b.get('port')}", "level": "ok", "action": "flash",
                     "text": f"{b.get('port')} — "
                             + ("TX 또는 미플래시 (TX 라면 정상)" if not b.get("sta_mac")
                                and not b.get("via") else "펌웨어 다시 굽기")})

    level = "ok" if len(rx) >= 3 else "block"
    text = f"RX {len(rx)}대" if len(rx) >= 3 else f"RX {len(rx)}대 — 수집에는 3대가 필요합니다"

    if pyserial is None:
        pyserial = importlib.util.find_spec("serial") is not None
    if not pyserial:
        rows.append({"key": "pyserial", "level": "block", "action": None,
                     "text": "pyserial 이 없습니다 — 수집기가 포트를 열 수 없습니다"})
        level, text = "block", "pyserial 이 없습니다"

    packages = VENV_PACKAGES if packages is None else packages
    missing = sorted(k for k, v in packages.items() if not v)
    if missing:
        rows.append({"key": "venv", "level": "warn", "action": None,
                     "text": f".venv 에 {', '.join(missing)} 가 없습니다 — 학습·파형에만 필요합니다"})
        if level == "ok":
            level = "warn"

    #: ESP-IDF 는 **플래시할 때만** 필요하다. 수집 담당자는 대개 플래시된 보드를 받으므로
    #: 수집을 막지 않지만, 없으면 준비하기 버튼을 보여 날것의 실패를 막는다 (설계 §3.1).
    if (IDF_EXPORT.is_file() if idf is None else idf) is False:
        rows.append({"key": "idf", "level": "warn", "action": "idf",
                     "text": "ESP-IDF 가 없습니다 — 펌웨어를 구우려면 먼저 준비해야 합니다"})
        if level == "ok":
            level = "warn"

    for r in (readiness() if notes is None else notes):   # 좌표·커밋 경고는 그대로 쓴다
        rows.append({"key": "note", "level": "warn", "action": None, "text": r["text"]})
        if level == "ok":
            level = "warn"
    if level == "ok":
        text = f"준비됐습니다 · {text}"
    return {"level": level, "text": text, "rows": rows}


def collect_label(state: dict) -> str | None:
    """수집 버튼이 찍을 라벨. 블록이 끝났으면 None — 새로 뽑아야 한다."""
    return block_order.next_label(state)


def settings_summary(meta: dict) -> str:
    """`▸ 이번 실험 설정` 접힌 줄에 미리 적는 값.

    **펼치지 않고도 맞는지 확인되는 것이 접기의 조건이다**(설계 §3.3). 메모는 있다/없다만
    적는다 — 한 줄에 본문을 넣으면 접힌 줄이 아니게 된다.
    """
    place = (meta.get("condition.placement_id") or "").strip() or "배치 없음"
    subj = (meta.get("condition.subject_id") or "").strip() or "피험자 없음"
    note = "메모 있음" if (meta.get("operator.notes") or "").strip() else "메모 없음"
    return f"{place} · {subj} · {note}"


def block_state() -> dict:
    """지금 블록. 조건(날짜·배치·피험자)이 바뀌었으면 새로 뽑아 사유를 함께 돌려준다."""
    meta = read_meta(SESSION_META)
    date = time.strftime("%Y%m%d")
    place = (meta.get("condition.placement_id") or "").strip()
    subj = (meta.get("condition.subject_id") or "").strip()
    state = block_order.load()
    reason = block_order.redraw_reason(state, date, place, subj)
    if reason:
        state = block_order.start(date, place, subj, state.get("order"))
        block_order.save(state)
    return {**state, "next": collect_label(state), "redrawn_reason": reason}


def collect_done(rc: int, before: Optional[Path] = None) -> str:
    """수집 JOB 이 끝난 뒤 — 품질 판정으로 블록을 진행시키고 한 줄을 돌려준다.

    사람이 "어디까지 찍었더라" 를 기억하지 않게 하는 고리다. `recollect` 면 칸을 되돌려
    같은 라벨이 다시 다음 차례가 된다 (설계 §3.2).
    """
    if rc != 0:
        return ""
    d = latest_session_dir()
    #: 시작 지연 중 중지하면 rc=0 인데 세션이 없다. 직전 세션으로 블록을 움직이면
    #: 찍지도 않은 라벨이 끝난 것이 된다.
    if d is None or (before is not None and d == before):
        return ""
    try:
        m = read_manifest(d)
    except Exception:
        return ""
    label, devices = m.get("label"), m.get("devices", [])
    level, text = quality(devices)
    if label in LABELS:                       # live 세션은 블록에 속하지 않는다
        block_order.save(block_order.advance(block_order.load(), label, level))
    return f"판정 {VERDICT[level]}" + (f" — {text}" if text else "")


def readiness() -> list[dict]:
    """수집 전에 걸러야 재수집을 면하는 것. 배치도는 나중에 복원할 수 없다.

    코드 커밋 여부는 더 이상 경고하지 않는다 — 수집 코드가 고정돼 늘 켜져 있었고, 늘 켜진
    경고는 진짜일 때도 무시된다. 출처는 `session.json` 의 `provenance` 에 계속 남는다.
    """
    out = []

    def zero(v: str) -> bool:
        # CSV 값은 문자열이라 "0" 도 참이다. 숫자로 보고 판단한다.
        try:
            return float(v) == 0.0
        except ValueError:
            return True

    missing = [f"RX{d}" for d, name, x, y in known_devices_geometry()
               if zero(x) and zero(y)]
    if missing:
        out.append({"level": "warn", "text":
                    f"보드 좌표가 비어 있습니다 ({', '.join(missing)}) — "
                    "device_registry.csv 의 room_x_m / room_y_m 을 실측값으로 채우세요. "
                    "배치도를 나중에 복원할 수 없습니다."})
    return out


def known_devices_geometry() -> list[tuple]:
    if not DEVICE_REGISTRY.is_file():
        return []
    rows = []
    for line in DEVICE_REGISTRY.read_text(encoding="utf-8").splitlines()[1:]:
        c = line.split(",")
        if len(c) >= 5 and c[0].strip().isdigit():
            rows.append((int(c[0]), c[1].strip(), c[3].strip(), c[4].strip()))
    return rows


def session_rows() -> list[dict]:
    rows = []
    for d in sorted(RAW_ROOT.glob("*/*"), reverse=True):
        if not (d / "session.json").is_file():
            continue
        try:
            m = read_manifest(d)
        except Exception:
            continue
        devs = m.get("devices", [])
        span = max((x.get("span_s") or 0) for x in devs) if devs else 0
        level, text = quality(devs)        # 품질 판정은 csi_session 한 곳에서만 나온다
        rows.append({
            "path": str(d.relative_to(REPO_ROOT)),
            "name": d.name, "date": d.parent.name,
            "label": m.get("label", "?"), "session_id": m.get("session_id"),
            "span_s": round(span, 1),
            "devices": [{"id": x["device_id"], "frames": x["frames"],
                         "hz": round(x["frames"] / span, 1) if span else 0,
                         "crc_fail": x.get("crc_fail", 0),
                         "boot_changes": x.get("boot_changes", 0),
                         "tx_back": x.get("tx_back", 0)} for x in devs],
            "quality": {"level": level, "text": text},
            "png": (d / "csi_waterfall.png").is_file(),
            "note": session_note(d), "placement": meta_field(d, "placement_id"),
        })
    return rows


def session_note(d: Path) -> str:
    return meta_field(d, "session_note")


def meta_field(d: Path, key: str) -> str:
    """세션 폴더에 복사된 실험 정보에서 한 줄. 스냅샷은 평평한 `키: 값` 이다."""
    p = d / "session_meta_snapshot.yaml"
    for line in (p.read_text(encoding="utf-8").splitlines() if p.is_file() else []):
        head, _, rest = line.partition(":")
        if head.strip().split(".")[-1] == key:
            rest = rest.strip()
            try:
                return str(json.loads(rest))
            except ValueError:
                return rest.strip("'\"")
    return ""


def session_groups(rows: list) -> list:
    """세션 목록을 날짜로 접는다 (doc/control-panel.md §4.1).

    접힌 줄에 **라벨 개수와 배치**를 적어 다음에 무엇을 더 찍어야 할지 보이게 한다.
    품질 경고도 접힌 줄까지 올라온다 — 지금은 펼쳐야 찾을 수 있다.
    """
    order = {"ok": 0, "check": 1, "recollect": 2}
    out: dict = {}
    for r in rows:
        g = out.setdefault(r["date"], {"date": r["date"], "count": 0, "placements": set(),
                                       "labels": {l: 0 for l in LABELS}, "live": 0,
                                       "worst": "ok", "excluded": [], "rows": [],
                                       "quality_counts": {"check": 0, "recollect": 0}})
        g["count"] += 1
        g["rows"].append(r)
        if r["placement"]:
            g["placements"].add(r["placement"])
        if r["label"] in g["labels"]:
            g["labels"][r["label"]] += 1
        elif r["label"] == LIVE_LABEL:
            g["live"] += 1
        level = r["quality"]["level"]
        if level != "ok":
            g["quality_counts"][level] += 1
        if order[level] > order[g["worst"]]:
            g["worst"] = level
        key = f"{r['date']}_{r['session_id']}"
        if is_excluded(key):
            g["excluded"].append(key)
    return [{**g, "placements": sorted(g["placements"]), "quality_hint": quality_hint(g["rows"])}
            for _, g in sorted(out.items(), reverse=True)]


#: 한 날짜에서 이만큼 걸리면 "그날 뭔가 달랐다" 는 신호로 본다. 2개 미만이거나 비중이
#: 작으면(1/30 같은) 조언을 띄우지 않는다 — 매번 뜨는 조언은 읽히지 않는다.
#: ponytail: 두 값 모두 눈대중이다. 전환 세션을 모으면 실측으로 다시 잡는다.
ADVICE_MIN, ADVICE_SHARE = 2, 0.2
ADVICE = "같은 날짜에 자주 나오면 보드 위치나 주변 전파를 확인해 보세요."


def quality_hint(rows: list, limit: int = 3) -> str:
    """날짜 줄의 배지에 달 설명. **왜** 걸렸는지 펼치지 않고도 읽게 한다.

    전부 적으면 툴팁이 화면을 덮는다 — 앞의 몇 개만 보이고 나머지는 수로 말한다.
    """
    bad = [r for r in rows if r["quality"]["level"] != "ok"]
    if not bad:
        return ""
    head = [f"{r['name']} — {r['quality']['text']}" for r in bad[:limit]]
    if len(bad) > limit:
        head.append(f"… {len(bad) - limit}개 더")
    if len(bad) >= ADVICE_MIN and len(bad) >= ADVICE_SHARE * len(rows):
        head += ["", ADVICE]
    return "\n".join(head)


def realtime_view(raw_root: Path = RAW_ROOT) -> Optional[dict]:
    """가장 최근 세션의 realtime_status.json. 실시간 판단이 안 돌면 None."""
    # 폴더 이름이 아니라 **마지막으로 갱신된** 상태 파일을 고른다 — 지금 돌고 있는 것이 그것이다.
    files = sorted(Path(raw_root).glob("*/*/realtime_status.json"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return None
    p = files[0]
    try:
        view = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:          # rename 직전의 부분 파일 — 다음 폴링에 다시 읽는다
        return None
    view["spans"] = timeline_spans(view.get("events", []), view.get("t", 0.0))
    view["stale_s"] = round(time.time() - p.stat().st_mtime, 1)
    view["last_mark_s"] = last_mark(p.parent)
    view["paused_by_operator"] = PAUSED_BY_OPERATOR[0]
    return view


def last_mark(session_dir: Path) -> Optional[float]:
    """마지막으로 누른 전환 표시의 시각. 없으면 None.

    이 버튼이 전환 반응 지연의 **유일한 정답 입력**이다 (realtime-inference.md §6).
    눌렀는지 화면이 보여 주지 않으면 10/14 세션처럼 정답 없는 녹화가 또 남는다.
    """
    f = session_dir / MARKS_NAME
    last = None
    for line in (f.read_text(encoding="utf-8").splitlines() if f.is_file() else []):
        try:
            last = json.loads(line).get("t", last)
        except json.JSONDecodeError:
            continue
    return last


def pause_note(paused_by_operator: bool) -> str:
    """멈춘 이유. **운영자가 멈춘 것과 RX 가 끊긴 것을 같은 말로 쓰지 않는다** — 전자는
    정상이고 후자는 이상이다 (설계 §6)."""
    return ("운영자가 판단을 멈췄습니다 — 수집은 계속됩니다"
            if paused_by_operator else "신호가 끊겨 판단을 멈췄습니다")


def timeline_spans(events: list, now: float, minutes: int = 60) -> list:
    """최근 `minutes` 분을 상태 구간으로 자른다. 짧은 구간도 최소 폭으로 남긴다.

    판단을 시작한 지 `minutes` 분이 안 됐으면 **지나온 시간만** 그린다 — 시작 전 구간까지
    '판단 중'으로 칠하면 한 시간 내내 못 정한 것처럼 보인다.
    """
    span_s = min(minutes * 60.0, max(now, 1.0))
    start = now - span_s
    marks = [(max(e["t"], start), e["to"]) for e in events if e["t"] >= start]
    first = next((e["from"] for e in events if e["t"] >= start), "deciding")
    marks.insert(0, (start, first))
    out = []
    for (t0, state), (t1, _) in zip(marks, marks[1:] + [(now, None)]):
        out.append({"state": state, "pct": max(0.5, 100.0 * (t1 - t0) / span_s)})
    total = sum(s["pct"] for s in out) or 1.0
    for s in out:
        s["pct"] = round(100.0 * s["pct"] / total, 3)
    out[0]["covers_s"] = round(span_s, 1)
    return out


def realtime_models() -> list:
    """실시간에 쓸 수 있는 체크포인트 (엔진과 같은 목록을 쓴다)."""
    sys.path.insert(0, str(REPO_ROOT))
    from realtime.engine import available_models

    return available_models()


def latest_session_dir(raw_root: Path = RAW_ROOT) -> Optional[Path]:
    return next((d for d in sorted(Path(raw_root).glob("*/*"), reverse=True)
                 if (d / "session.json").is_file()), None)


def live_session_dir(raw_root: Path = RAW_ROOT, max_age_s: float = 10.0) -> Optional[Path]:
    """지금 수집 중인 세션 — 이름이 가장 최근이고 `.csi` 가 최근 `max_age_s` 초 안에 자란 것.

    옛 녹화를 실시간인 것처럼 따라가지 않도록, 파일이 지금 쓰이고 있는지까지 본다.
    """
    for d in sorted(Path(raw_root).glob("*/*"), reverse=True):
        if (d / "session.json").is_file():
            now = time.time()
            if any(now - f.stat().st_mtime <= max_age_s for f in d.glob("device_*.csi")):
                return d
            return None
    return None


def training_state() -> dict:
    """학습 탭이 쓰는 것: 날짜별 세션과 지금까지 학습한 모델.

    날짜마다 **그 날짜로 학습한 모델이 몇 개인지** 적는다. "봉인" 이라고 쓰지 않는 이유는
    봉인된 날짜가 더 이상 없기 때문이다 — 네 날짜 모두 이미 학습에 쓰였다. 잠금이 아니라
    사실이므로 체크포인트를 세어 만들고, 학습할 때마다 저절로 맞는다 (설계 §5).
    """
    try:
        models = trained_models()
    except Exception:                      # torch 가 없는 기기 — 학습만 못 하고 나머지는 돈다
        models = []
    used: dict = {}
    for m in models:
        for d in m.get("train_dates") or []:
            used[d] = used.get(d, 0) + 1
    sessions = list_sessions()
    groups = [{"date": date, "sessions": items, "trained_models": used.get(date, 0)}
              for date, items in group_by_date(sessions).items()]
    return {"dates": groups, "presets": PRESET_INFO, "models": models, "selected": []}


def realtime_models_safe() -> list:
    """`realtime_models()` 의 안전판. torch 가 없는 수집용 기기에서도 화면이 떠야 한다 —
    준비 줄이 "학습·파형에만 필요합니다" 라고 약속한다 (설계 §3.1)."""
    try:
        return realtime_models()
    except Exception:
        return []


def retrain_seconds(bundle: dict) -> float:
    """다시 만드는 데 걸릴 대략 시간. 팝업이 "그냥 두자" 를 판단하게 하려고 보인다.

    세션 길이는 300초로 본다(수집 프로토콜 기준). `약` 을 붙여 쓴다.
    """
    window = bundle.get("window_frames") or 0
    sessions = len(bundle.get("training_sessions") or [])
    if not window or not sessions:
        return 0.0
    phase = bundle.get("mode") == "P"
    stride = 30 if phase else 150
    steps = window // (bundle.get("downsample") or 10) if phase else window // 10
    windows = predict_windows([{"span_s": 300.0}] * sessions, window, stride)
    seeds = len(bundle.get("state_dicts") or [1])
    epochs = (bundle.get("config") or {}).get("epochs", 10)
    return estimate_seconds(windows, steps, epochs, seeds, "phase60" if phase else "amp60")


def trained_models() -> list:
    """model_train/*/model_*.pt 의 요약 — 체크포인트 기록 + 레지스트리의 이름·설명·노출.

    이름과 설명은 체크포인트 밖(`model_train/models.json`)에 있다. 파일 이름은 `file` 로
    따로 돌려줘 화면이 "어느 파일인지" 를 잃지 않게 한다.
    """
    import torch

    store = model_registry.load()
    out = []
    for d in (REPO_ROOT / "model_train", ):
        for p in sorted(d.glob("*/model*.pt"), key=lambda f: f.stat().st_mtime, reverse=True):
            try:
                b = torch.load(p, map_location="cpu", weights_only=False)
            except Exception:
                continue
            m = b.get("metrics", {})
            rel = str(p.relative_to(REPO_ROOT))
            e = model_registry.entry(rel, store)
            files = sorted(q.name for q in p.parent.glob(f"{p.stem}.*"))
            out.append({
                "path": rel, "file": p.stem.replace("model_", ""), **e,
                "files": files, "deletable": rel not in PROTECTED,
                "retrain_s": retrain_seconds(b),
                "mode": b.get("mode", "?"), "window_s": b.get("window_frames", 0) / 100,
                "sessions": len(b.get("training_sessions", [])),
                "train_dates": b.get("training_dates", []),
                "eval_dates": b.get("evaluation_dates", []),
                "score": m.get("sessions"), "window_acc": m.get("window_accuracy"),
                "trained_at": b.get("trained_at", ""),
                "size_kb": round(p.stat().st_size / 1024),
            })
    return out


def label_counts(rows) -> dict:
    c = {l: 0 for l in LABELS}
    for r in rows:
        if r["label"] in c:
            c[r["label"]] += 1
    return c


# ── HTML ──────────────────────────────────────────────────────────────────────
CSS = """
:root{color-scheme:light dark;--bg:#f5f6f8;--card:#fff;--fg:#1b1d21;--muted:#6b7280;
 --line:#dde1e6;--accent:#2a6df4;--ok:#137a45;--okbg:#e9f7ef;--warn:#b45309;--warnbg:#fdf3e3;
 --danger:#b42318;--code:#0f1115;--codefg:#d8dee9}
@media(prefers-color-scheme:dark){:root{--bg:#15171b;--card:#1f2228;--fg:#e7eaee;--muted:#98a0ab;
 --line:#343a42;--accent:#6b9bff;--ok:#5ed49a;--okbg:#17301f;--warn:#f0b45e;--warnbg:#332714;
 --danger:#ff8b80;--code:#0b0d10;--codefg:#cfd6e2}}
*{box-sizing:border-box}
body{margin:0;padding:0 0 3rem;background:var(--bg);color:var(--fg);
 font:15px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Segoe UI",sans-serif}
header{background:var(--card);border-bottom:1px solid var(--line);padding:.9rem 1.2rem;
 position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:1rem;flex-wrap:wrap}
header h1{font-size:1.05rem;margin:0}
nav{display:flex;gap:.3rem;flex-wrap:wrap}
nav button{font:inherit;font-size:.9rem;padding:.35rem .8rem;border-radius:8px;cursor:pointer;
 border:1px solid transparent;background:transparent;color:var(--muted)}
nav button.on{background:var(--accent);color:#fff}
main{max-width:1000px;margin:1.2rem auto;padding:0 1rem}
section{display:none} section.on{display:block}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.3rem;margin-bottom:1rem}
h2{font-size:1rem;margin:0 0 .2rem} .sub{color:var(--muted);font-size:.87rem;margin:0 0 1rem}
button.act{font:inherit;font-weight:600;padding:.5rem .9rem;border-radius:8px;border:1px solid var(--line);
 background:var(--bg);color:var(--fg);cursor:pointer}
button.act.primary{background:var(--accent);color:#fff;border-color:transparent}
button.act.danger{color:var(--danger)}
button.act:disabled{opacity:.45;cursor:not-allowed}
label{display:block;margin:.9rem 0 .3rem;font-weight:600;font-size:.88rem}
.hint{color:var(--muted);font-weight:400;font-size:.82rem}
input,select,textarea{width:100%;padding:.5rem .65rem;font:inherit;color:var(--fg);
 background:var(--bg);border:1px solid var(--line);border-radius:8px}
textarea{min-height:5rem;resize:vertical}
.row{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
table{width:100%;border-collapse:collapse;font-size:.9rem}
th,td{text-align:left;padding:.45rem .5rem;border-bottom:1px solid var(--line);vertical-align:middle}
th{color:var(--muted);font-weight:600;font-size:.82rem}
.tag{display:inline-block;padding:.1rem .5rem;border-radius:999px;font-size:.78rem;
 background:var(--bg);border:1px solid var(--line)}
.tag.ok{background:var(--okbg);color:var(--ok);border-color:var(--ok)}
.tag.warn{background:var(--warnbg);color:var(--warn);border-color:var(--warn)}
.tag.bad{background:var(--stbg);color:var(--st-suspended);border-color:var(--st-suspended)}
/* 배지의 설명은 직접 그린다 — 브라우저 기본 title 은 1~2초 기다려야 뜨고,
   올릴 수 있다는 표시도 없어서 "아무것도 안 뜬다" 로 읽힌다 */
.tag[data-tip]{position:relative;cursor:help;border-bottom-style:dashed}
.tag[data-tip]:hover::after{content:attr(data-tip);position:absolute;z-index:20;
 top:calc(100% + 6px);right:0;white-space:pre-line;text-align:left;width:max-content;
 max-width:min(34rem,80vw);padding:.5rem .7rem;border-radius:8px;font-size:.78rem;
 line-height:1.6;font-weight:400;color:var(--fg);background:var(--card);
 border:1px solid var(--line);box-shadow:0 10px 30px rgba(0,0,0,.45)}
pre.log{background:var(--code);color:var(--codefg);border-radius:10px;padding:.8rem;
 font:12px/1.5 ui-monospace,Menlo,monospace;max-height:22rem;overflow:auto;white-space:pre-wrap;margin:0}
.bar{display:flex;gap:.5rem;align-items:center;flex-wrap:wrap;margin-bottom:.8rem}
.spin{width:.8rem;height:.8rem;border:2px solid var(--muted);border-top-color:transparent;
 border-radius:50%;display:inline-block;animation:sp .8s linear infinite}
@keyframes sp{to{transform:rotate(360deg)}}
img.wf{width:100%;border:1px solid var(--line);border-radius:10px;margin-top:.6rem}
.chips{display:flex;flex-wrap:wrap;gap:.4rem;margin-top:.45rem}
.chips button{font:inherit;font-size:.85rem;padding:.25rem .6rem;cursor:pointer;background:var(--bg);
 color:var(--fg);border:1px solid var(--line);border-radius:999px}
/* 실시간 탭 — 상태색은 글자와 왼쪽 띠에만. 카드 전체를 칠하면 오래 켜두기 어렵다 */
:root{--st-empty:#64748b;--st-static:#2a6df4;--st-motion:#ea580c;--st-deciding:#b45309;
 --st-suspended:#b42318;--stbg:#eef1f5}
@media(prefers-color-scheme:dark){:root{--st-empty:#94a3b8;--st-static:#7aa7ff;--st-motion:#fb923c;
 --st-deciding:#f0b45e;--st-suspended:#ff8b80;--stbg:#232830}}
.hero{position:relative;padding:1.8rem 1.6rem 1.6rem 2.1rem;overflow:hidden;--c:var(--st-deciding)}
.hero::before{content:"";position:absolute;left:0;top:0;bottom:0;width:6px;background:var(--c)}
.hero.s-empty{--c:var(--st-empty)} .hero.s-static{--c:var(--st-static)}
.hero.s-motion{--c:var(--st-motion)} .hero.s-deciding{--c:var(--st-deciding)}
.hero.s-suspended{--c:var(--st-suspended)}
.state{font-size:3.4rem;line-height:1.1;font-weight:800;letter-spacing:-.03em;margin:0;color:var(--c)}
.sub-state{display:flex;align-items:baseline;gap:.6rem;margin:.3rem 0 1.3rem;color:var(--muted);
 font-size:1.02rem;font-variant-numeric:tabular-nums}
.sub-state b{color:var(--fg);font-weight:600}
.meter{display:flex;align-items:center;gap:.7rem;max-width:30rem}
.meter .lab{color:var(--muted);font-size:.8rem;min-width:2.2rem}
.meter .track{position:relative;flex:1;height:10px;border-radius:999px;background:var(--stbg)}
.meter .fill{position:absolute;inset:0 auto 0 0;border-radius:999px;background:var(--c)}
.meter .mark{position:absolute;top:-3px;bottom:-3px;width:2px;background:var(--fg);opacity:.35}
.meter .num{font-variant-numeric:tabular-nums;font-weight:700;min-width:3rem;text-align:right}
.note{margin:1rem 0 0;font-size:.88rem;color:var(--warn);background:var(--warnbg);
 border:1px solid var(--warn);border-radius:8px;padding:.45rem .7rem;display:inline-block}
.act{display:flex;align-items:flex-end;gap:1px;height:36px;max-width:30rem;
 margin:.9rem 0 .25rem;background:var(--bg);border-radius:5px;padding:2px}
.act i{display:block;flex:1;min-height:2px;border-radius:1px;background:var(--st-motion);opacity:.85}
.act i.gap{background:var(--line);opacity:.5;height:2px}
.act-lab{display:flex;justify-content:space-between;max-width:30rem;color:var(--muted);
 font-size:.75rem}
.tl{display:flex;height:22px;border-radius:5px;overflow:hidden;background:var(--bg)}
.tl i{display:block;min-width:2px}
.tl .empty{background:var(--st-empty)} .tl .static{background:var(--st-static)}
.tl .motion{background:var(--st-motion)}
.tl .deciding,.tl .suspended{background:repeating-linear-gradient(45deg,var(--stbg),var(--stbg) 3px,
 var(--st-deciding) 3px,var(--st-deciding) 6px);opacity:.6}
.axis{display:flex;justify-content:space-between;color:var(--muted);font-size:.75rem;margin-top:.3rem}

.chg td{padding:.45rem .3rem;font-size:.92rem}
.chg .t{width:7rem;color:var(--muted);font-variant-numeric:tabular-nums}
.chg .c{width:4rem;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:.35rem}
.arrow{color:var(--muted);margin:0 .4rem}
/* 상태줄·버튼은 기록 카드 맨 아래 한 줄 (control-panel.md §6) */
.statusbar{display:flex;align-items:center;gap:.7rem;flex-wrap:wrap;margin-top:1rem;
 padding-top:.7rem;border-top:1px solid var(--line)}
.statusbar .meta{display:flex;gap:.9rem;flex-wrap:wrap;align-items:center;color:var(--muted);
 font-size:.8rem;font-variant-numeric:tabular-nums}
.prog{height:12px;border-radius:999px;background:var(--bg);border:1px solid var(--line);overflow:hidden}
.prog i{display:block;height:100%;background:var(--accent)}
.ready{display:flex;align-items:center;gap:.5rem;border-radius:8px;padding:.55rem .75rem;
 margin-bottom:.6rem;font-size:.88rem;cursor:pointer;
 background:var(--warnbg);color:var(--warn);border:1px solid var(--warn)}
.ready.ok{background:var(--okbg);color:var(--ok);border-color:var(--ok)}
.ready.block{background:var(--stbg);color:var(--st-suspended);border-color:var(--st-suspended)}
.ready .sp{margin-left:auto;font-size:.79rem;opacity:.8}
.readyrows{margin:-.3rem 0 .9rem;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.readyrows .r{display:flex;align-items:center;gap:.6rem;padding:.4rem .75rem;font-size:.85rem;
 border-top:1px solid var(--line)}
.readyrows .r:first-child{border-top:0}
/* 수집 화면 — 위에서 아래로 읽으면 그게 작업 순서다 (control-panel.md §3) */
.lbl{font-size:.72rem;letter-spacing:.05em;text-transform:uppercase;color:var(--muted);
 margin:1.1rem 0 .35rem}
.seq{font-size:1.9rem;font-weight:800;letter-spacing:-.02em;line-height:1;margin:0;
 font-variant-numeric:tabular-nums}
.seq em{font-size:.85rem;font-weight:400;font-style:normal;color:var(--muted);margin-left:.5rem}
.steps{display:flex;gap:.4rem;flex-wrap:wrap;align-items:center;margin-bottom:.35rem}
.steps i{font-style:normal;font-size:.85rem;padding:.32rem .7rem;border-radius:8px;cursor:pointer;
 background:var(--bg);border:1px solid var(--line);color:var(--muted)}
.steps i.done{color:var(--ok);border-color:var(--ok)}
.steps i.now{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:700}
.setting{display:flex;align-items:baseline;gap:.5rem;margin:1.1rem 0 .2rem}
.link{color:var(--accent);font-size:.8rem;text-decoration:underline;cursor:pointer}
button.act.go{display:block;width:100%;margin:.9rem 0 .2rem;padding:.75rem;font-size:1rem}
.fold{border-top:1px solid var(--line);margin-top:1.1rem;padding-top:.75rem;cursor:pointer;
 font-size:.88rem;color:var(--muted)}
.fold b{color:var(--fg);font-weight:600}
.fold .vals{margin-left:.5rem}
/* 세션 탭 — 날짜로 접는다 (control-panel.md §4.1) */
.sum{display:flex;gap:.45rem;flex-wrap:wrap;margin-bottom:.9rem}
.sum i{font-style:normal;font-size:.8rem;padding:.28rem .62rem;border-radius:999px;
 background:var(--bg);border:1px solid var(--line);color:var(--fg)}
.sum i.on{background:var(--accent);border-color:var(--accent);color:#fff;font-weight:600}
/* overflow 를 자르지 않는다 — 자르면 배지 설명이 날짜 줄 밖으로 못 나간다.
   대신 둥근 모서리는 .hd 가 직접 갖는다 (접혀 있으면 네 귀, 펼치면 위 두 귀). */
.grp{border:1px solid var(--line);border-radius:9px;margin-bottom:.45rem}
.grp .hd{display:flex;align-items:center;gap:.6rem;padding:.5rem .7rem;background:var(--bg);
 font-size:.87rem;cursor:pointer;border-radius:8px 8px 0 0}
.grp:not(:has(.rows)) .hd{border-radius:8px}
.grp .hd .sp,.grp .r .sp{margin-left:auto;color:var(--muted);font-size:.79rem}
.grp .rows{padding:.15rem 0 .35rem}
.grp .r{display:flex;align-items:center;gap:.5rem;padding:.3rem .8rem;font-size:.84rem}
.grp .r.off{opacity:.5}
/* 모델 표가 관리 화면이다 (control-panel.md §5) */
table.mt td{border-bottom:1px solid #23282f;vertical-align:middle}
table.mt tr.off td{opacity:.45}
table.mt .nm{font-weight:600}
table.mt .nm .file{display:block;font-weight:400;font-size:.72rem;color:var(--muted);
 font-family:ui-monospace,monospace}
table.mt .editrow td{background:var(--bg)}
.sw{width:34px;height:19px;border-radius:999px;background:var(--stbg);border:1px solid var(--line);
 position:relative;display:inline-block;cursor:pointer}
.sw i{position:absolute;top:2px;left:2px;width:13px;height:13px;border-radius:50%;
 background:var(--muted);display:block}
.sw.on{background:var(--accent);border-color:var(--accent)}
.sw.on i{left:17px;background:#fff}
.ff{display:grid;grid-template-columns:4rem 1fr;gap:.4rem .7rem;align-items:center;padding:.3rem 0 .5rem}
.ff label{font-size:.78rem;color:var(--muted);margin:0}
.ff .inp{background:var(--card);border:1px solid var(--line);border-radius:7px;
 padding:.33rem .55rem;font-size:.85rem;color:var(--fg);width:100%}
"""

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshSense 제어판</title><style>__CSS__</style></head><body>
<header>
  <h1>MeshSense 제어판</h1>
  <nav id="nav">
    <button data-t="collect" class="on">수집</button>
    <button data-t="sessions">세션</button>
    <button data-t="train">학습</button>
    <button data-t="realtime">실시간</button>
  </nav>
  <span id="jobbadge" class="hint" style="margin-left:auto"></span>
</header>
<main>
  <section id="collect" class="on"></section>
  <section id="sessions"></section>
  <section id="train"></section>
  <section id="realtime"></section>
  <div class="card" id="logcard" style="display:none">
    <div class="bar"><h2 id="logtitle" style="margin:0">작업 로그</h2>
      <button class="act danger" id="stopbtn" onclick="post('/api/stop')">중지</button></div>
    <div id="progbar" style="display:none;margin-bottom:.6rem"><div class="prog"><i style="width:0%"></i></div></div>
    <p class="sub" style="margin:.3rem 0 .8rem"></p>
    <pre class="log" id="log"></pre>
  </div>
</main>
<script>__JS__</script></body></html>"""

JS = r"""
let S = null, tab = 'collect', shownPng = null;
// 섹션별 데이터 지문. 바뀐 섹션만 다시 그린다 —
// 1.2초 폴링마다 전부 다시 그리면 입력 중인 폼이 통째로 날아간다.
const sig = {};
function changed(k, v) { const s = JSON.stringify(v); if (sig[k] === s) return false; sig[k] = s; return true; }
document.querySelectorAll('#nav button').forEach(b => b.onclick = () => {
  tab = b.dataset.t;
  document.querySelectorAll('#nav button').forEach(x => x.classList.toggle('on', x === b));
  document.querySelectorAll('main section').forEach(s => s.classList.toggle('on', s.id === tab));
  delete sig[tab];
  render();
});
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

async function post(url, body) {
  const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'},
                             body: JSON.stringify(body || {})});
  const j = await r.json();
  if (j.error) alert(j.error);
  await refresh();
  return j;
}
async function refresh() {
  S = await (await fetch('/api/state')).json();
  render(); renderJob();
}
function renderJob() {
  const j = S.job, card = document.getElementById('logcard');
  document.getElementById('jobbadge').innerHTML = j.running
    ? `<span class="spin"></span> ${esc(j.name)} 실행 중` : '';
  if (j.name) {
    card.style.display = '';
    document.getElementById('logtitle').textContent = j.name + (j.running ? '' : ` — 완료 (코드 ${j.rc})`);
    document.getElementById('stopbtn').disabled = !j.running;
    const prog = j.log.slice().reverse().find(l => l.startsWith('[진행]'));
    const bar = document.getElementById('progbar');
    if (bar) {
      const m = prog && prog.match(/\((\d+)%\)/);
      bar.style.display = m ? '' : 'none';
      if (m) {
        bar.querySelector('i').style.width = m[1] + '%';
        bar.nextElementSibling.textContent = prog.replace('[진행] ', '');
      }
    }
    const pre = document.getElementById('log');
    const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
    pre.textContent = j.log.join('\n');
    if (stick) pre.scrollTop = pre.scrollHeight;
  }
}
function render() {
  if (!S) return;
  const busy = S.job.running;
  if (tab === 'collect' && changed('collect', [S.next_session_id, busy, S.ready, S.block, S.meta, readyOpen, timeOpen, setOpen, projOpen])) {
    const b = S.block, next = b.next;
    //: 블록 세 칸이 곧 진행 상태다 — 어디까지 찍었는지 화면이 기억한다 (설계 §3.2)
    const cells = (b.order||[]).map(l => {
      const done = (b.done||[]).includes(l);
      return `<i class="${done?'done':(l===next?'now':'')}" onclick="post('/api/block/goto',{label:'${l}'})"
        title="${esc(S.label_desc[l]||'')}">${l}${done?' ✓':''}</i>`;
    }).join('');
    document.getElementById('collect').innerHTML = `<div class="card">
      ${renderReady(S.ready, busy)}
      <p class="lbl">다음 세션</p>
      <p class="seq">s${S.next_session_id}
        <em>${esc(S.meta.date||'')} · ${esc(settingsSummary(S.meta))}</em></p>

      <p class="lbl">블록 순서</p>
      <div class="steps">${cells}
        <span class="link" onclick="post('/api/block/draw')">다시 뽑기</span></div>
      <p class="hint">${b.redrawn_reason ? esc(b.redrawn_reason) : '블록이 끝나면 새로 뽑습니다.'}</p>

      ${timeOpen
        ? `<div class="row" style="margin-top:.9rem">
             <div><label for="dur">수집 시간 (초)</label><input id="dur" type="number" value="${durS}" min="5" step="5" oninput="durS=+this.value"></div>
             <div><label for="delay">시작 지연 (초) <span class="hint">— 자리 잡기·퇴장, 세션에 안 들어감</span></label>
             <input id="delay" type="number" value="${delayS}" min="0" step="1" oninput="delayS=+this.value"></div></div>`
        : `<div class="setting"><span>${durS}초 녹화 · ${delayS}초 뒤 시작</span>
             <span class="link" onclick="timeOpen=true;delete sig.collect;render()">바꾸기</span></div>`}

      <button class="act primary go" ${busy||S.ready.level==='block'||!next?'disabled':''}
        onclick="startCollect()">${next ? esc(next)+' 수집 시작'
          : '블록을 마쳤습니다 — 다시 뽑으세요'}</button>
      ${S.ready.level==='block'?`<p class="hint">${esc(S.ready.text)}</p>`:''}

      ${renderSettings(S)}</div>`;
  }
  if (tab === 'sessions' && changed('sessions', [S.session_groups, shownPng, openDates, toolOpen, busy, S.diag_png])) {
    const cnt = S.label_counts, total = S.sessions.length;
    const chips = `<div class="sum"><i class="on">전체 ${total}</i>` +
      Object.entries(cnt).filter(([k]) => k !== 'live').map(([k,v]) => `<i>${esc(k)} ${v}</i>`).join('') +
      (cnt.live ? `<i>live ${cnt.live}</i>` : '') + `</div>`;
    const groups = S.session_groups.map(g => {
      const open = openDates.includes(g.date);
      const labs = Object.entries(g.labels).map(([k,v]) => `${esc(k)} ${v}`).join(' · ')
        + (g.live ? ` · live ${g.live}` : '');
      const rows = open ? `<div class="rows">${g.rows.map(r => {
        const ex = S.excluded[`${r.date}_${r.session_id}`];
        return `<div class="r${ex?' off':''}" ${ex?`title="${esc(ex)}"`:''}>
          <span class="tag">${esc(r.label)}</span> s${r.session_id}
          <span class="hint">${r.span_s}초 · RX${r.devices.length}${
            r.devices.length?' · '+r.devices.map(d=>d.hz).join('/')+'Hz':''}</span>
          ${r.quality.level==='ok'?'':`<span class="tag ${r.quality.level==='check'?'warn':'bad'}" title="${esc(r.quality.text)}">${r.quality.level==='check'?'확인':'재수집'}</span>`}
          ${ex?'<span class="hint">학습·평가에서 제외됨</span>':''}
          <span class="sp">${r.png?`<span class="link" onclick="showPng('${esc(r.path)}')">파형</span> · `:''}<span class="link" onclick="noteSession('${esc(r.path)}')">메모${r.note?' ✓':''}</span> · <span class="link" onclick="delSession('${esc(r.path)}')">삭제</span></span></div>`;
      }).join('')}</div>` : '';
      return `<div class="grp"><div class="hd" onclick="toggleDate('${g.date}')">
          ${open?'▾':'▸'} <b>${esc(g.date)}</b>
          <span class="hint">${g.count}세션${g.placements.length?' · 배치 '+g.placements.map(esc).join('·'):''}</span>
          <span class="sp">${labs}${badges(g)}</span>
        </div>${rows}</div>`;
    }).join('');
    document.getElementById('sessions').innerHTML = `<div class="card">
      <h2>수집한 세션 (${total})</h2>
      <p class="sub">라벨마다 2세션 이상이어야 학습·진단이 가능합니다. 날짜 줄을 누르면 펼쳐집니다.</p>
      ${chips}${groups || '<p class="sub">아직 수집한 세션이 없습니다.</p>'}
      ${shownPng ? `<img class="wf" src="/api/img?path=${encodeURIComponent(shownPng)}">` : ''}
      <div class="setting"><span class="hint">이 데이터로</span>
        <span class="link" onclick="toolOpen=toolOpen==='diag'?null:'diag';delete sig.sessions;render()">분리 가능성 진단</span>
        <span class="hint">·</span>
        <span class="link" onclick="toolOpen=toolOpen==='jsonl'?null:'jsonl';delete sig.sessions;render()">JSONL 내보내기</span></div>
      ${toolOpen==='diag' ? `<p class="sub">지금 배치로 empty / static / motion 이 실제로 갈리는지 확인합니다.
        세션 단위 교차검증이라 결과가 부풀려지지 않습니다.</p>
        <div class="bar"><button class="act" ${busy?'disabled':''} onclick="post('/api/diagnose')">진단 실행</button></div>
        ${S.diag_png ? `<img class="wf" src="/api/img?path=${encodeURIComponent(S.diag_png)}&v=${encodeURIComponent(S.diag_png_mtime)}">` : ''}` : ''}
      ${toolOpen==='jsonl' ? `<p class="sub">공식 3-RX 전처리는 JSONL 을 읽습니다. 제어판 학습은
        <code>.csi</code> 를 직접 읽으므로 이 단계가 필요 없습니다.</p>
        <div class="bar"><button class="act" ${busy?'disabled':''} onclick="post('/api/export')">JSONL 내보내기</button></div>
        <p class="sub">이후 터미널에서:
        <code>python model_train/preprocessing/preprocess_3rx.py --raw-dir mac_collector_output/jsonl/raw/&lt;날짜&gt;</code></p>` : ''}
      </div>`;
  }
  if (tab === 'train' && changed('train', [S.training, busy, trainPick.size, trainEval, trainPreset, trainName, trainNote, trainOpen, modelEdit])) {
    document.getElementById('train').innerHTML = renderTrain(S.training, busy);
  }
  if (tab === 'realtime' && changed('realtime', [S.realtime, S.rt_models, S.rt_job, S.job.running, S.job.name, rtModel])) {
    document.getElementById('realtime').innerHTML =
      renderRealtimeStart(S) + renderRealtime(S.realtime);
  }
}
const RT_LABEL = {empty:'빈 방', static:'정지', motion:'움직임',
                  deciding:'판단 중', suspended:'판단 정지'};
function held(s){ s = Math.max(0, Math.round(s));
  return s >= 60 ? `${Math.floor(s/60)}분 ${String(s%60).padStart(2,'0')}초` : `${s}초`; }
function ago(t, now){ return held(now - t) + ' 전'; }
function spanLabel(spans){
  const s = (spans && spans[0] && spans[0].covers_s) || 0;
  return s >= 3600 ? '60분' : held(s);
}
let rtModel = null;
function rtSet(v){ rtModel = v; delete sig.realtime; render(); }
function startLive(){ post('/api/live/start', {model: rtModel}); }
function renderRealtimeStart(S){
  const models = S.rt_models || [], j = S.rt_job || {}, c = S.job || {};
  if (!models.length) return '';
  if (rtModel === null) rtModel = (models.find(m => m.is_deployed) || models[0]).path;
  const collecting = c.running && c.name === S.live_job;
  if (j.running || collecting) {
    //: 멈춘 것과 시작 중인 것을 같은 말로 쓰지 않는다 — 전자는 돌아갈 길이 필요하다
    const paused = collecting && !j.running && S.realtime && S.realtime.paused_by_operator;
    return `<div class="card"><div class="bar" style="margin:0">
      <span class="tag ${paused ? 'warn' : 'ok'}">${
        j.running ? esc(j.name) : paused ? esc(S.pause_note) : '수집 시작 중 — 첫 판단까지 약 20초'}</span>
      ${paused ? `<button class="act primary" onclick="post('/api/rt/resume')">판단 다시 켜기</button>` : ''}
      <button class="act danger" onclick="post('/api/live/stop')">종료</button></div></div>`;
  }
  if (c.running) return `<div class="card"><h2>실시간 측정</h2>
    <p class="sub">${esc(c.name)} 이(가) 실행 중입니다. 끝난 뒤 시작할 수 있습니다.</p></div>`;
  const mopts = models.map(m => `<option value="${esc(m.path)}" ${rtModel===m.path?'selected':''}>
      ${esc(m.name)} · 창 ${m.window_s}초${m.is_deployed ? ' (기본)' : ''}</option>`).join('');
  const chosen = models.find(m => m.path === rtModel) || models[0];
  const crashed = j.name && ![0, null, -15].includes(j.rc)
    ? `<p class="note">지난 실시간이 오류로 끝났습니다: ${esc((j.log || []).slice(-1)[0] || '')}</p>` : '';
  const stalled = c.name === S.live_job && (c.log || []).some(l => l.includes('스트림 정지'))
    ? `<p class="note">보드 신호가 3초 넘게 끊겨 측정이 멈췄습니다. 보드 전원과 USB 를 확인하고 다시 누르세요.</p>` : '';
  return `<div class="card"><h2>실시간 측정</h2>
    <p class="sub">누르면 수집(라벨 live, 학습에 안 씀)과 실시간 판단이 함께 시작되고, 종료를 누를 때까지 계속됩니다.</p>
    <label for="rtm">모델</label>
    <select id="rtm" onchange="rtSet(this.value)">${mopts}</select>
    <p class="hint" style="margin:.45rem 0 0">${chosen && chosen.note ? esc(chosen.note)
      : '설명이 없습니다 — 학습 탭에서 적을 수 있습니다'}</p>
    <div class="bar" style="margin-top:1rem">
      <button class="act primary" onclick="startLive()">실시간 측정</button></div>${stalled}${crashed}</div>`;
}
function actBars(r){
  const raw = r.activity || [], step = r.activity_step_s || 0.3;
  if (!raw.length) return '';
  const cells = 120, per = Math.max(1, Math.round(180 / step / cells));   // 최근 3분
  const need = cells * per;
  const v = Array(Math.max(0, need - raw.length)).fill(null).concat(raw.slice(-need));
  let out = '';
  for (let i = 0; i < cells; i++){
    const c = v.slice(i * per, (i + 1) * per).filter(x => x !== null && x !== undefined);
    out += c.length ? `<i style="height:${Math.max(4, Math.round(Math.max(...c) * 100))}%"></i>`
                    : '<i class="gap"></i>';
  }
  return out;
}
function renderRealtime(r){
  if (!r) return `<div class="card"><h2>실시간 판단이 실행 중이 아닙니다</h2>
    <p class="sub">아래에서 모델을 고르고 실시간 측정을 누르면 시작합니다.</p></div>`;
  const deciding = r.state === 'deciding';
  const pct = deciding ? Math.round(100 * (1 - Math.min(1, r.remaining_s / 60)))
                       : Math.round(r.confidence * 100);
  const sub = deciding
    ? (r.candidate ? `${esc(RT_LABEL[r.candidate] || r.candidate)} 확인 중`
                   : `확신이 기준(${Math.round(r.threshold*100)}%)에 못 미칩니다`)
    : `확신 ${Math.round(r.confidence*100)}%`;
  //: 카드는 둘이다 — 상태 하나, 기록 하나. 넷이면 테두리가 네 번 나와 화면이 끊긴다.
  return `<div class="card hero s-${esc(r.state)}">
      <p class="state">${esc(RT_LABEL[r.state] || r.state)}</p>
      <p class="sub-state"><b>${deciding ? sub : held(r.held_s) + '째'}</b>
        <span>${deciding ? '' : '· ' + esc(r.session)}</span></p>
      <div class="meter"><span class="lab">${deciding ? '남음' : '확신'}</span>
        <span class="track"><span class="fill" style="width:${pct}%"></span>
          ${deciding ? '' : `<span class="mark" style="left:${Math.round(r.threshold*100)}%"></span>`}</span>
        <span class="num">${deciding ? Math.round(r.remaining_s) + '초' : pct + '%'}</span></div>
      ${r.activity && r.activity.length ? `<div class="act">${actBars(r)}</div>
        <div class="act-lab"><span>3분 전</span>
          <span>활동 — 창 ${r.activity_window_s}초 · 상태 판단에는 쓰지 않습니다</span>
          <span>지금</span></div>` : ''}
      ${r.note ? `<p class="note">${esc(r.note)}</p>` : ''}
      ${r.paused_by_operator ? `<p class="note">${esc(S.pause_note)}</p>` : ''}
      ${r.synthetic ? `<p class="note">합성 세션 — 녹화를 이어 붙인 것이라 실제 전환이 아닙니다</p>` : ''}
    </div>
    <div class="card"><h2>최근 기록</h2>
      <div class="tl">${(r.spans||[]).map(s =>
        `<i class="${esc(s.state)}" style="width:${s.pct}%"></i>`).join('')}</div>
      <div class="axis"><span>${spanLabel(r.spans)} 전</span><span>지금</span></div>
      ${r.truth ? `<p class="sub" style="margin:.7rem 0 .2rem">정답 (합성 대본, ${r.truth.hold_s}초마다)</p>
        <div class="tl">${r.truth.segments.map(sg =>
          `<i class="${esc(sg.state)}" style="width:${100 / r.truth.segments.length}%"></i>`).join('')}</div>` : ''}
      ${(r.events||[]).length ? `<table class="chg">${r.events.slice().reverse().map(e =>
        `<tr><td class="t">${esc(ago(e.t, r.t))}</td>
         <td><span class="dot" style="background:var(--st-${esc(e.from)})"></span>${esc(RT_LABEL[e.from]||e.from)}
         <span class="arrow">→</span><b><span class="dot" style="background:var(--st-${esc(e.to)})"></span>${esc(RT_LABEL[e.to]||e.to)}</b></td>
         <td class="c">${Math.round(e.confidence*100)}%</td></tr>`).join('')}</table>`
        : `<p class="sub">아직 상태 변화가 없습니다.</p>`}
      <div class="statusbar">
        <span class="meta">
          <span class="tag ${r.n_rx >= 3 ? 'ok' : 'warn'}">RX ${r.n_rx}대</span>
          <span>${r.hz}Hz</span><span>창 ${r.window_s}초</span>
          <span>${esc(r.model || '')}</span></span>
        <span class="meta" style="margin-left:auto">
          <span class="link" onclick="post('/api/mark')">전환 표시</span>
          <span class="hint">${r.last_mark_s != null ? esc(held(r.t - r.last_mark_s)) + ' 전' : '아직 없음'}</span>
          <span class="link" onclick="post('/api/rt/pause')">판단 정지</span></span></div>
    </div>`;
}
//: 학습 탭이 고른 것 — 서버가 아니라 화면이 들고 있다. 새로고침하면 지워진다.
let trainPick = new Set(), trainEval = null, trainPreset = 'balanced', trainName = '', trainNote = '';
let trainOpen = false, modelEdit = null;
//: 되돌릴 수 없다 — 무엇이 지워지는지 먼저 말한다 (control-panel.md §5)
function delModel(m){
  const files = m.files.map(f => '  ' + f).join('\n');
  const again = m.retrain_s ? `\n\n다시 만들려면 약 ${hms(m.retrain_s)} 학습해야 합니다.` : '';
  const owner = m.path.indexOf('/robust/') >= 0
    ? '\n\n담당자가 만든 공식 체크포인트입니다.' : '';
  if (confirm(`${m.name}\n\n아래 ${m.files.length}개 파일을 지웁니다. 되돌릴 수 없습니다.\n`
              + files + again + owner))
    post('/api/model/delete', {path: m.path});
}
function saveModel(path){
  post('/api/model', {path: path,
    name: document.getElementById('mn').value, note: document.getElementById('md').value});
  modelEdit = null;
}
function trainToggleDate(date, keys){
  const all = keys.every(k => trainPick.has(k));
  keys.forEach(k => all ? trainPick.delete(k) : trainPick.add(k));
  if (trainEval && [...trainPick].some(k => k.startsWith(trainEval))) trainEval = null;
  delete sig.train; render(); }
function trainSetEval(d){ trainEval = d; delete sig.train; render(); }
function trainSetPreset(p){ trainPreset = p; delete sig.train; render(); }
function startTrain(){
  const locked = new Set([...trainPick].map(k => k.split('_')[0]));
  const free = (S.training.dates || []).map(g => g.date).filter(d => !locked.has(d)).sort().reverse();
  const ev = trainEval === 'none' ? null
    : (free.includes(trainEval) ? trainEval : (free[0] || null));
  post('/api/train', {sessions: [...trainPick], eval_dates: ev ? [ev] : [],
                      preset: trainPreset, name: trainName.trim(), note: trainNote.trim()});
}
function renderTrain(t, busy){
  if (!t) return '';
  const counts = {empty:0, static:0, motion:0};
  const spans = [];
  t.dates.forEach(g => g.sessions.forEach(s => {
    if (trainPick.has(s.key)) { counts[s.label]++; spans.push(s.span_s || 0); }
  }));
  const total = trainPick.size;
  const ready = total > 0 && Object.values(counts).every(c => c >= 2);
  const lockedDates = new Set([...trainPick].map(k => k.split('_')[0]));
  //: 평가 날짜 기본값 — 학습에 안 쓴 날짜 중 가장 최근 (설계 §5)
  const free = t.dates.map(g => g.date).filter(d => !lockedDates.has(d)).sort().reverse();
  //: null 은 "아직 안 골랐다", 'none' 은 "평가 없이" 다. 둘을 같은 값으로 두면 고를 수 없다.
  const evalDate = trainEval === 'none' ? null
    : (free.includes(trainEval) ? trainEval : (free[0] || null));
  const preset = t.presets[trainPreset] || Object.values(t.presets)[0];
  const name = trainName || autoName(trainPreset);

  const dates = t.dates.slice().reverse().map(g => {
    const keys = g.sessions.map(s => s.key);
    const picked = keys.filter(k => trainPick.has(k)).length;
    const isEval = evalDate === g.date;
    //: 왜 못 고르는지 적는다. 전에는 그냥 회색이라 이유를 알 수 없었다.
    const why = isEval ? '평가에 쓰입니다'
      : g.trained_models ? `이 날짜로 학습한 모델이 ${g.trained_models}개 있습니다` : '';
    const n = {empty:0, static:0, motion:0};
    g.sessions.forEach(s => { if (n[s.label] !== undefined) n[s.label]++; });
    return `<div class="grp"><div class="hd" ${isEval||busy?'':`onclick='trainToggleDate("${g.date}", ${JSON.stringify(keys)})'`}
        style="${isEval?'opacity:.55;cursor:default':''}">
        ${picked === keys.length ? '☑' : picked ? '◪' : '☐'} <b>${esc(g.date)}</b>
        <span class="hint">${picked ? picked + '/' : ''}${keys.length}세션</span>
        <span class="sp">${why || `empty ${n.empty} · static ${n.static} · motion ${n.motion}`}</span>
      </div></div>`;
  }).join('');

  const windows = predictWindows(spans, preset);
  const secs = windows ? preset.rate * windows * preset.steps * preset.epochs * preset.seeds : 0;
  const eta = secs && secs < 36000 ? ` — 약 ${hms(secs)}` : '';

  const onCount = t.models.filter(m => m.realtime && m.mode !== 'A-motion').length;
  const models = t.models.length ? `<table class="mt">
    <tr><th style="width:3.2rem">실시간</th><th>이름</th><th>설명</th>
        <th style="width:5.5rem">구성</th><th style="width:5rem">평가</th><th style="width:3rem"></th></tr>
    ${t.models.map(m => {
      const editing = modelEdit === m.path;
      const row = `<tr class="${m.realtime ? '' : 'off'}">
        <td><span class="sw ${m.realtime?'on':''}" title="${m.pinned?'코드가 쓰는 모델이라 끌 수 없습니다':'실시간 선택지에 넣기'}"
          ${m.pinned?'':`onclick="post('/api/model',{path:'${esc(m.path)}',realtime:${!m.realtime}})"`}><i></i></span></td>
        <td class="nm">${esc(m.name)}
          ${m.is_deployed?'<span class="tag ok">기본</span>':''}${m.mode==='A-motion'?'<span class="tag warn">1단계</span>':''}
          <span class="file">${esc(m.path)}</span></td>
        <td class="hint">${m.note ? esc(m.note) : '<i>설명 없음</i>'}</td>
        <td class="hint">${esc(m.mode)} · 창 ${m.window_s}초</td>
        <td class="hint">${m.score ? esc(m.score) : '—'}</td>
        <td><span class="link" onclick="modelEdit=${editing?'null':`'${esc(m.path)}'`};delete sig.train;render()">${editing?'닫기':'수정'}</span>
          · ${m.deletable
            ? `<span class="link bad" onclick='delModel(${JSON.stringify(m)})'>삭제</span>`
            : `<span class="link dis" title="코드가 쓰는 모델이라 화면에서는 지울 수 없습니다">삭제</span>`}</td></tr>`;
      return row + (editing ? `<tr class="editrow"><td colspan="6">
        <div class="ff"><label>이름</label><input class="inp" id="mn" value="${esc(m.name)}">
          <label>설명</label><input class="inp" id="md" value="${esc(m.note)}" placeholder="한 줄로 — 어떤 자료로 학습했고 어디에 쓰는지"></div>
        <div class="bar"><button class="act primary" onclick="saveModel('${esc(m.path)}')">저장</button>
          <button class="act" onclick="modelEdit=null;delete sig.train;render()">취소</button>
          <span class="hint">파일 이름은 바뀌지 않습니다 — 보이는 이름만 바뀝니다</span></div></td></tr>` : '');
    }).join('')}</table>`
    : `<p class="sub">아직 학습한 모델이 없습니다.</p>`;

  return `<div class="card">
      <p class="lbl">학습할 세션</p>
      ${dates}
      <p class="hint">선택 <b>${total}세션</b> — 빈 방 ${counts.empty} · 정지 ${counts.static} · 움직임 ${counts.motion}
        ${ready ? '✓' : '<span class="tag warn">클래스마다 2세션 이상 필요</span>'}</p>

      <p class="lbl">설정</p>
      <div class="steps">${Object.entries(t.presets).map(([k,p]) =>
        `<i class="${trainPreset===k?'now':''}" onclick="trainSetPreset('${k}')">${esc(p.label)}</i>`).join('')}</div>
      <p class="hint">${esc(preset.detail)}</p>

      ${trainOpen
        ? `<div class="row" style="margin-top:.9rem">
             <div><label>평가 날짜</label>
               <div class="steps">${free.map(d => `<i class="${evalDate===d?'now':''}" onclick="trainSetEval('${d}')">${esc(d)}</i>`).join('')}
                 <i class="${trainEval==='none'?'now':''}" onclick="trainSetEval('none')">평가 없이</i></div></div>
             <div><label for="trainname">모델 이름</label>
               <input id="trainname" type="text" maxlength="40" value="${esc(trainName)}"
                 placeholder="${esc(autoName(trainPreset))}" oninput="trainName = this.value">
               <label for="trainnote">설명 <span class="hint">— 지금이 가장 잘 아는 시점입니다</span></label>
               <input id="trainnote" type="text" maxlength="120" value="${esc(trainNote)}"
                 placeholder="어떤 자료로 학습했고 어디에 쓰는지" oninput="trainNote = this.value"></div></div>`
        : `<div class="setting"><span>평가는 <b>${evalDate ? esc(evalDate) : '없이'}</b>로 · 이름 <b>${esc(name)}</b></span>
             <span class="link" onclick="trainOpen=true;delete sig.train;render()">바꾸기</span></div>`}

      <button class="act primary go" ${busy||!ready?'disabled':''} onclick="startTrain()">
        ${ready ? `${total}세션으로 학습 시작${eta}` : '세션을 더 고르세요'}</button>
      ${busy?'<p class="hint">다른 작업이 실행 중입니다</p>':''}

      <p class="lbl">학습한 모델 (${t.models.length}) · 실시간 선택지 ${onCount}개</p>
      ${models}</div>`;
}
//: 창 수 어림 — `_train_run.predict_windows()` 와 **같은 식**이어야 한다. 합계 길이로 한 번에
//: 세면 세션마다 버려지는 창 끝이 사라져 +23% 치우친다.
function predictWindows(spans, preset){
  const w = preset.window, stride = preset.train_stride;
  return spans.reduce((n, sec) => {
    const frames = Math.round(sec * 100);
    return n + (frames >= w ? Math.floor((frames - w) / stride) + 1 : 0);
  }, 0);
}
function autoName(preset){
  const d = new Date(), p = n => String(n).padStart(2,'0');
  return `${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}-${preset}`;
}
function hms(s){ s = Math.round(s); return s >= 60 ? `${Math.floor(s/60)}분 ${p2(s%60)}초` : `${s}초`; }
function p2(n){ return String(n).padStart(2,'0'); }
function tog(id){const f=document.getElementById('devices.expected_device_ids');
  const s=f.value.split(/[,\s]+/).filter(Boolean);const i=s.indexOf(String(id));
  if(i<0)s.push(String(id));else s.splice(i,1);f.value=s.sort((a,b)=>a-b).join(', ');}
async function saveMeta(){
  const body={}; S.meta_fields.forEach(([k])=>{const e=document.getElementById(k); if(e) body[k]=e.value;});
  await post('/api/meta', body);
  document.getElementById('metamsg').textContent = '저장했습니다 · ' + new Date().toLocaleTimeString();
}
function startCollect(){ post('/api/collect',{duration:durS, delay:delayS}); }
let readyOpen = false, timeOpen = false, setOpen = false, projOpen = false;
let openDates = [], toolOpen = null;
//: 느낌표 하나로는 "30개 중 2개" 와 "6개 중 3개" 가 같아 보인다. 비중까지 적는다.
//: 정상은 적지 않는다 — 기본이라 매 줄에 배지가 붙으면 눈이 그걸 거른다.
function badges(g){
  const q = g.quality_counts || {}, t = esc(g.quality_hint || '');
  const one = (n, label, cls) => n
    ? ` <span class="tag ${cls}" data-tip="${t}">${label} ${n}/${g.count}</span>` : '';
  return one(q.recollect, '재수집', 'bad') + one(q.check, '확인', 'warn');
}
function toggleDate(d){
  openDates = openDates.includes(d) ? openDates.filter(x => x !== d) : [...openDates, d];
  delete sig.sessions; render();
}
let durS = 300, delayS = 10;
const BLOCK_FIELDS = ['condition.placement_id', 'condition.subject_id', 'operator.notes'];
function settingsSummary(m){
  const v = k => ((m[k]||'')+'').trim();
  return `${v('condition.placement_id')||'배치 없음'} · ${v('condition.subject_id')||'피험자 없음'}`
       + ` · ${v('operator.notes')?'메모 있음':'메모 없음'}`;
}
//: 12칸을 3 + 7 로 나눈다. 블록마다 바뀌는 셋만 한 겹 안, 나머지는 두 겹 안 (설계 §3.3)
function metaField(S, k){
  const f = S.meta_fields.find(x => x[0] === k); if (!f) return '';
  const [, title, kind, hint] = f, v = esc(S.meta[k] ?? '');
  const lab = `<label for="${k}">${esc(title)}${hint?` <span class="hint">— ${esc(hint)}</span>`:''}</label>`;
  if (kind === 'label') return lab + `<select id="${k}">${S.labels.map(l=>`<option ${l===S.meta[k]?'selected':''}>${l}</option>`).join('')}</select>`;
  if (kind === 'textarea') return lab + `<textarea id="${k}">${v}</textarea>`;
  return lab + `<input id="${k}" type="${kind==='number'?'number':'text'}" ${kind==='number'?'step="0.1"':''} value="${v}">`;
}
function renderSettings(S){
  if (!setOpen) return `<div class="fold" onclick="setOpen=true;delete sig.collect;render()">
    ▸ <b>이번 실험 설정</b><span class="vals">${esc(settingsSummary(S.meta))}</span></div>`;
  const rest = S.meta_fields.map(f => f[0]).filter(k => !BLOCK_FIELDS.includes(k));
  const proj = projOpen
    ? rest.map(k => metaField(S, k)).join('')
      + `<div class="chips">${S.known_devices.map(([id,n])=>`<button type="button" onclick="tog(${id})">${id} · ${esc(n)}</button>`).join('')}</div>`
    : '';
  return `<div class="fold" onclick="setOpen=false;delete sig.collect;render()">▾ <b>이번 실험 설정</b></div>
    ${BLOCK_FIELDS.map(k => metaField(S, k)).join('')}
    <div class="fold" style="margin-top:.8rem" onclick="event.stopPropagation();projOpen=!projOpen;delete sig.collect;render()">
      ${projOpen?'▾':'▸'} <b>프로젝트 설정</b>
      <span class="vals">${projOpen?'':'한 번 적으면 거의 안 바뀝니다'}</span></div>${proj}
    <div class="bar" style="margin-top:1rem"><button class="act primary" onclick="saveMeta()">저장</button>
      <span id="metamsg" class="hint"></span></div>`;
}
function toggleReady(){ readyOpen = !readyOpen; delete sig.collect; render(); }
function registerBoard(mac){
  const name = prompt('보드 이름', 'RX');
  if (name !== null) post('/api/register', {sta_mac: mac, name: name});
}
//: 평소엔 한 줄, 막혔을 때만 펼친다 — 문제는 막힌 자리에서 푼다 (control-panel.md §2)
function renderReady(r, busy){
  if (!r) return '';
  const act = {
    register: k => `<button class="act" onclick="registerBoard('${esc(k.replace('new-',''))}')">등록</button>`,
    idf: () => `<button class="act" onclick="post('/api/idf')">ESP-IDF 준비하기</button>`,
    flash: k => { const port = k.replace('port-','');
      return `<button class="act" onclick="post('/api/flash',{port:'${esc(port)}',kind:'rx'})">RX 플래시</button>`
           + `<button class="act" onclick="post('/api/flash',{port:'${esc(port)}',kind:'tx'})">TX 플래시</button>`; }};
  const rows = readyOpen ? `<div class="readyrows">${r.rows.map(x => `<div class="r">
      <span>${esc(x.text)}</span>
      ${x.action && act[x.action] ? act[x.action](x.key) : ''}</div>`).join('')
    || '<div class="r"><span>더 볼 것이 없습니다</span></div>'}
    <div class="r"><button class="act" ${busy?'disabled':''} onclick="post('/api/rescan')">보드 다시 찾기</button></div></div>` : '';
  return `<div class="ready ${esc(r.level)}" onclick="toggleReady()">
      <span>${r.level === 'ok' ? '✓' : '⚠'} ${esc(r.text)}</span>
      <span class="sp">자세히 ${readyOpen ? '▾' : '▸'}</span></div>${rows}`;
}
function noteSession(p){
  const s = S.sessions.find(x => x.path === p);
  const t = prompt(p + '\n\n세션 메모 (예: 피험자가 RX102 시야를 가림). 비우면 삭제', s ? s.note : '');
  if (t !== null) post('/api/note', {path: p, text: t});
}
function delSession(p){ if(confirm(p+'\n\n이 세션을 삭제할까요?')) post('/api/delete',{path:p}); }
function showPng(p){ shownPng = (shownPng === p+'/csi_waterfall.png') ? null : p+'/csi_waterfall.png'; render(); }
refresh(); setInterval(refresh, 1200);
"""


def page() -> bytes:
    return PAGE.replace("__CSS__", CSS).replace("__JS__", JS).encode("utf-8")


# ── 서버 ──────────────────────────────────────────────────────────────────────
class Handler(http.server.BaseHTTPRequestHandler):
    boards_cache: list[dict] = []

    def _json(self, obj, status=200):
        b = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            b = page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        elif u.path == "/api/state":
            # 실시간 측정은 종료를 누를 때까지 돈다. 그 전에 수집이 끝났다면 보드 신호가 끊긴 것이다 —
            # 실시간만 남겨 두면 멈춘 화면을 측정 중으로 착각하므로 같이 내린다.
            if RT_JOB.running and not JOB.running and JOB.name == LIVE_JOB:
                RT_JOB.stop()
            rows = session_rows()
            meta = read_meta(SESSION_META)
            self._json({
                "sessions": rows, "label_counts": label_counts(rows),
                "session_groups": session_groups(rows),
                "excluded": EXCLUDED_SESSIONS,
                "next_session_id": next_session_id(OUTPUT_DIR),
                "labels": list(LABELS), "label_desc": LABEL_DESC,
                "meta": meta, "meta_fields": FIELDS, "known_devices": known_devices(),
                "diag_png": DIAG_PNG_REL if (REPO_ROOT / DIAG_PNG_REL).is_file() else None,
                "diag_png_mtime": int((REPO_ROOT / DIAG_PNG_REL).stat().st_mtime)
                if (REPO_ROOT / DIAG_PNG_REL).is_file() else 0,
                "ready": ready_line(), "block": block_state(),
                "training": training_state(),
                "rt_models": realtime_models_safe(),
                # 실시간이 안 돌면 지난 실행의 화면을 띄우지 않는다 — 옛 결과를 지금으로 착각하게 된다
                #: 운영자가 멈춘 동안에는 마지막 판단을 남겨 둔다 — 화면이 비면
                #: 돌아갈 버튼이 어디 있는지 알 수 없다. 그냥 끊긴 경우는 전처럼 지운다.
                "realtime": view if (view := realtime_view())
                and (RT_JOB.running or PAUSED_BY_OPERATOR[0] or view["stale_s"] < 5) else None,
                "rt_job": RT_JOB.state(), "live_job": LIVE_JOB,
                "pause_note": pause_note(PAUSED_BY_OPERATOR[0]),
                "job": JOB.state(),
            })
        elif u.path == "/api/img":
            rel = (q.get("path") or [""])[0]
            target = (REPO_ROOT / rel).resolve()
            # 저장소 밖 파일을 읽어가지 못하게 막는다
            if not str(target).startswith(str(REPO_ROOT)) or not target.is_file():
                self._json({"error": "not found"}, 404)
                return
            b = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        try:
            self._json(dispatch(path, body))
        except Exception as exc:
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def log_message(self, *a):  # 요청 로그 억제
        pass


DIAG_PNG_REL = "mac_collector_output/separability.png"


def rescan() -> dict:
    Handler.boards_cache = [identify(p) for p in list_ports()]
    return {"ok": True}


def dispatch(path: str, body: dict) -> dict:
    if path == "/api/rescan":
        #: 수집 중 포트를 다시 열면 reader 가 읽던 바이트를 가져간다 (CLAUDE.md 금지)
        if JOB.running:
            return {"error": "수집 중에는 보드를 다시 찾지 않습니다"}
        return rescan()

    if path == "/api/stop":
        JOB.stop()
        return {"ok": True}

    if path == "/api/flash":
        port, kind = body.get("port"), body.get("kind")
        proj = SEND_POC if kind == "tx" else RECV_POC
        def after_flash(rc: int) -> str:
            rescan()                       # 펌웨어가 바뀌었으니 보드 식별을 다시 한다
            return "" if rc == 0 else "플래시 실패 — 포트를 쓰는 다른 프로그램이 있는지 확인하세요"

        err = JOB.start(f"{kind.upper()} 플래시 · {Path(port).name}",
                        [PY, str(SCRIPT_DIR / "_idf_flash.py"), str(proj), port],
                        on_done=after_flash)
        return {"error": err} if err else {"ok": True}

    if path == "/api/block/draw":
        st = block_state()
        meta = read_meta(SESSION_META)
        block_order.save(block_order.start(
            time.strftime("%Y%m%d"), (meta.get("condition.placement_id") or "").strip(),
            (meta.get("condition.subject_id") or "").strip(), st.get("order")))
        return {"ok": True}

    if path == "/api/block/goto":
        #: 순서를 벗어나 찍고 싶으면 칸을 직접 눌러 "지금" 을 옮긴다 (설계 §3.2)
        label = body.get("label")
        st = block_order.load()
        if label not in st.get("order", []):
            return {"error": f"이번 블록에 없는 라벨: {label}"}
        #: 고른 칸만 "남음" 으로 되돌린다. 뒤쪽 칸의 ✓ 를 지우면 이미 찍은 세션을 또 찍게 된다.
        block_order.save({**st, "done": [l for l in st.get("done", []) if l != label]})
        return {"ok": True}

    if path == "/api/collect":
        #: 라벨은 화면이 고르지 않는다 — 블록 순서가 정한다 (설계 §3.2)
        label = collect_label(block_order.load())
        dur = float(body.get("duration") or 300)
        delay = max(0.0, float(body.get("delay") or 0))
        if label not in SESSION_LABELS:
            return {"error": "블록을 마쳤습니다 — 순서를 다시 뽑으세요"}
        before = latest_session_dir()
        err = JOB.start(f"수집 · {label} · {dur:.0f}초",
                        [PY, str(SCRIPT_DIR / "_collect_run.py"), label, str(dur), str(delay)],
                        on_done=lambda rc: collect_done(rc, before))
        return {"error": err} if err else {"ok": True}

    if path == "/api/register":
        if JOB.running:                        # 등록도 끝에서 rescan() 을 부른다
            return {"error": "수집 중에는 보드를 다시 찾지 않습니다"}
        mac, name = (body.get("sta_mac") or "").strip(), (body.get("name") or "").strip()
        if not mac:
            return {"error": "sta_mac 이 필요합니다"}
        r = subprocess.run([PY, str(SCRIPT_DIR / "device_registry.py"), "add",
                            "--mac", mac, "--board-name", name or "RX"],
                           capture_output=True, text=True, cwd=str(REPO_ROOT))
        if r.returncode != 0:                  # 조용히 실패하지 않는다 — stderr 를 그대로 보인다
            return {"error": (r.stderr or r.stdout).strip()[:400]}
        return rescan()

    if path == "/api/model":
        #: 보이는 이름·설명·실시간 노출만 바꾼다. **체크포인트 파일은 건드리지 않는다.**
        rel = str(body.get("path", ""))
        target = (REPO_ROOT / rel).resolve()
        if not str(target).startswith(str(REPO_ROOT / "model_train")) or target.suffix != ".pt":
            return {"error": "체크포인트가 아닙니다"}
        if not target.is_file():
            return {"error": "없는 파일입니다"}
        try:
            return {"ok": True, "entry": model_registry.update(
                rel, {k: body[k] for k in ("name", "note", "realtime") if k in body})}
        except ValueError as exc:
            return {"error": str(exc)}

    if path == "/api/model/delete":
        #: 되돌릴 수 없다. 화면이 먼저 무엇이 지워지는지 보이고 확인을 받는다.
        rel = str(body.get("path", ""))
        if rel in PROTECTED:
            return {"error": "코드가 쓰는 모델이라 화면에서는 지울 수 없습니다"}
        target = (REPO_ROOT / rel).resolve()
        if not str(target).startswith(str(REPO_ROOT / "model_train")) or target.suffix != ".pt":
            return {"error": "체크포인트가 아닙니다"}
        if not target.is_file():
            return {"error": "없는 파일입니다"}
        gone = []
        for q in sorted(target.parent.glob(f"{target.stem}.*")):
            q.unlink()
            gone.append(q.name)
        model_registry.forget(rel)
        return {"ok": True, "deleted": gone}

    if path == "/api/idf":
        err = JOB.start("ESP-IDF 준비", [PY, str(SCRIPT_DIR / "idf_bootstrap.py"), "-y"])
        return {"error": err} if err else {"ok": True}

    if path == "/api/diagnose":
        err = JOB.start("분리 가능성 진단",
                        [PY, str(SCRIPT_DIR / "check_separability.py"),
                         "--out", str(REPO_ROOT / DIAG_PNG_REL)])
        return {"error": err} if err else {"ok": True}

    if path == "/api/export":
        # 학습 전처리(preprocess_3rx.py)는 JSONL record schema v1 을 소비한다.
        err = JOB.start("전처리용 JSONL 내보내기",
                        [PY, str(SCRIPT_DIR / "export_jsonl.py"), "--print-labels"])
        return {"error": err} if err else {"ok": True}

    if path == "/api/meta":
        write_meta(SESSION_META, body)
        return {"ok": True}

    if path == "/api/note":
        target = (REPO_ROOT / body.get("path", "")).resolve()
        if not str(target).startswith(str(RAW_ROOT)) or not (target / "session.json").is_file():
            return {"error": "세션 디렉터리가 아닙니다"}
        set_session_note(target, str(body.get("text", "")))
        return {"ok": True}

    if path == "/api/live/start":
        model = (REPO_ROOT / str(body.get("model", ""))).resolve()
        if not str(model).startswith(str(REPO_ROOT)) or model.suffix != ".pt":
            return {"error": "체크포인트가 아닙니다"}
        if RT_JOB.running:
            return {"error": "실시간 측정이 이미 돌고 있습니다"}
        PAUSED_BY_OPERATOR[0] = False
        LIVE_MODEL[0] = str(model)
        before = latest_session_dir()
        err = JOB.start(LIVE_JOB, [PY, str(SCRIPT_DIR / "_collect_run.py"), LIVE_LABEL, "0", "0"])
        if err:
            return {"error": err}

        def attach() -> None:
            """수집이 세션을 만들고 파일이 자라기 시작하면 실시간 판단을 붙인다."""
            for _ in range(120):                       # 최대 60초
                if not JOB.running:
                    return
                d = live_session_dir()
                if d is not None and d != before and LIVE_LABEL in d.name.split("_"):
                    RT_JOB.start(f"실시간 · {model.stem.replace('model_', '')} · {d.name}",
                                 [PY, str(REPO_ROOT / "realtime" / "ui.py"), "--session", str(d),
                                  "--model", str(model), "--plain", "--follow"])
                    return
                time.sleep(0.5)

        threading.Thread(target=attach, daemon=True).start()
        return {"ok": True}

    if path == "/api/rt/resume":
        #: 멈춘 판단을 **같은 세션에** 다시 붙인다. 수집은 애초에 멈추지 않았다.
        d = live_session_dir()
        if d is None:
            return {"error": "따라갈 수집 세션이 없습니다"}
        if RT_JOB.running:
            return {"error": "판단이 이미 돌고 있습니다"}
        model = Path(LIVE_MODEL[0] or MODEL_DEFAULT)
        err = RT_JOB.start(f"실시간 · {model.stem.replace('model_', '')} · {d.name}",
                           [PY, str(REPO_ROOT / "realtime" / "ui.py"), "--session", str(d),
                            "--model", str(model), "--plain", "--follow"])
        if err:
            return {"error": err}
        PAUSED_BY_OPERATOR[0] = False
        return {"ok": True}

    if path == "/api/rt/pause":
        #: 화면만 끄고 데이터는 계속 받는 쓰임이다 — 수집(JOB)은 건드리지 않는다
        PAUSED_BY_OPERATOR[0] = True
        RT_JOB.stop()
        return {"ok": True}

    if path == "/api/live/stop":
        PAUSED_BY_OPERATOR[0] = False
        RT_JOB.stop()
        if JOB.running and JOB.name == LIVE_JOB:
            JOB.stop()                                  # 러너가 reader 를 정상 마감한다
        return {"ok": True}

    if path == "/api/train":
        keys = list(body.get("sessions") or [])
        eval_dates = list(body.get("eval_dates") or [])
        preset = body.get("preset", "balanced")
        if preset not in PRESET_INFO:
            return {"error": f"알 수 없는 설정: {preset}"}
        name = str(body.get("name") or "").strip()
        if name and not re.fullmatch(r"[\w가-힣.-]{1,40}", name):
            return {"error": "모델 이름은 글자·숫자·밑줄·하이픈만, 40자까지 씁니다"}
        problems = check_selection(keys, eval_dates)
        if problems:
            return {"error": " / ".join(problems)}
        cmd = [PY, str(SCRIPT_DIR / "_train_run.py"), "--preset", preset, "--sessions", *keys]
        if name:
            cmd += ["--name", name]
        if (note := str(body.get("note") or "").strip()):
            cmd += ["--note", note]
        if eval_dates:
            cmd += ["--eval", *eval_dates]
        err = JOB.start(f"학습 · {PRESET_INFO[preset]['label']} · {len(keys)}세션", cmd)
        return {"error": err} if err else {"ok": True}

    if path == "/api/mark":
        view = realtime_view()
        if not view:
            return {"error": "실시간 판단이 실행 중이 아닙니다"}
        target = next((d for d in sorted(RAW_ROOT.glob("*/*"), reverse=True)
                       if d.name == view.get("session")), None)
        if target is None:
            target = latest_session_dir()
        if target is None:
            return {"error": "세션을 찾지 못했습니다"}
        #: `t` 가 정본이다 — 엔진이 쓰는 형식과 같아야 last_mark() 와 지연 계산이 읽는다.
        #: 전에는 `wall` 만 써서 화면이 늘 "아직 없음" 이었다.
        with (target / MARKS_NAME).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps({"t": view.get("t"), "wall": time.time(),
                                 "state": view.get("state"),
                                 "note": str(body.get("note", "")), "via": "gui"},
                                ensure_ascii=False) + "\n")
        return {"ok": True}

    if path == "/api/delete":
        target = (REPO_ROOT / body.get("path", "")).resolve()
        if not str(target).startswith(str(RAW_ROOT)) or not (target / "session.json").is_file():
            return {"error": "세션 디렉터리가 아닙니다"}
        shutil.rmtree(target)
        return {"ok": True}

    return {"error": "unknown endpoint"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=0, help="0이면 빈 포트 자동 선택")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    threading.Thread(target=rescan, daemon=True).start()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"[gui] {url}  (종료: Ctrl+C)")
    if not args.no_open:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[gui] 종료")
    finally:
        JOB.stop()
        RT_JOB.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
