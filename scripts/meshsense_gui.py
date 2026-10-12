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
import http.server
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

from csi_session import next_session_id, read_manifest, repo_provenance, set_session_note  # noqa: E402
from csi_store import LABELS  # noqa: E402
from realtime_launch import build_command, build_live_command, check_runtime, new_output_dir, runtime_info, resolve_model  # noqa: E402
from realtime_live import ensure_port_free  # noqa: E402
from session_form import (  # noqa: E402
    DEVICE_REGISTRY, FIELDS, SESSION_META, known_devices, read_meta, write_meta,
)

OUTPUT_DIR = REPO_ROOT / "mac_collector_output"
RAW_ROOT = OUTPUT_DIR / "raw"
SEND_POC = REPO_ROOT / "esp32s3_csi_send_poc"
RECV_POC = REPO_ROOT / "esp32s3_csi_recv_poc"
PY = sys.executable

LABEL_DESC = {"empty": "부재 — 공간에 사람 없음",
              "static": "정지 — 사람이 있으나 움직이지 않음",
              "motion": "움직임 — 사람이 움직이는 중"}


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
        self.session_dir: Optional[Path] = None
        self.output_dir: Optional[Path] = None
        self.mode = ""
        self.model_kind = "amplitude"

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self, name: str, cmd: list[str], on_done=None, *,
              session_dir=None, output_dir=None, mode="", model_kind="amplitude") -> Optional[str]:
        with self.lock:
            if self.running:
                return f"이미 실행 중입니다: {self.name}"
            self.name, self.rc, self.done_note, self.on_done = name, None, "", on_done
            self.log.clear()
            self.log.append(f"$ {' '.join(cmd)}")
            self.session_dir, self.output_dir, self.mode = session_dir, output_dir, mode
            self.model_kind = model_kind
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(REPO_ROOT),
            )
            threading.Thread(target=self._pump, args=(self.proc, on_done), daemon=True).start()
        return None

    def _pump(self, proc, on_done) -> None:
        assert proc.stdout
        with proc.stdout:
            for line in proc.stdout:
                if self.proc is proc:
                    self.log.append(line.rstrip())
        rc = proc.wait()
        if self.proc is not proc:
            return
        self.rc = rc
        if on_done:
            try:
                self.done_note = on_done(self.rc) or ""
            except Exception as exc:                       # 콜백 실패로 서버가 죽지 않게
                self.done_note = f"후처리 실패: {exc}"
        self.log.append(f"— 종료 (코드 {self.rc}) {self.done_note}")

    def stop(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=10 if self.mode == "live" else 3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=3)

    def state(self) -> dict:
        return {"running": self.running, "name": self.name,
                "rc": self.rc, "log": list(self.log), "note": self.done_note,
                "session_path": str(self.session_dir) if self.session_dir else None,
                "mode": self.mode, "model_kind": self.model_kind}


JOB = Job()
RT_JOB = Job()  # 파일을 읽는 판단 프로세스는 USB 수집과 독립적으로 실행한다.
PORT_LOCK = threading.RLock()  # 식별·수집 기동 사이에 같은 포트를 다시 열지 않는다.


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


def readiness() -> list[dict]:
    """수집 전에 걸러야 재수집을 면하는 것들. 논문용 데이터는 출처와 배치가 남아야 한다."""
    out = []
    prov = repo_provenance()
    if prov.get("git_dirty"):
        out.append({"level": "warn", "text":
                    "커밋되지 않은 코드 변경이 있습니다 — 이 상태로 찍은 데이터는 "
                    "어느 코드로 만들었는지 재현할 수 없습니다."})
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
        # seq_gap 이 1% 를 넘으면 무선 경합 등 손실이 있는 세션 — 학습 전 확인 필요
        bad = any(x.get("crc_fail") or x.get("boot_changes") or x.get("tx_back")
                  or (x.get("seq_gap", 0) > 0.01 * max(x.get("frames", 1), 1)) for x in devs)
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
            "warn": bad,
            "png": (d / "csi_waterfall.png").is_file(),
            "note": session_note(d),
        })
    return rows


def session_note(d: Path) -> str:
    p = d / "session_meta_snapshot.yaml"
    for line in (p.read_text(encoding="utf-8").splitlines() if p.is_file() else []):
        if line.startswith("session_note:"):
            return json.loads(line.split(":", 1)[1])
    return ""


def realtime_view(raw_root: Path = RAW_ROOT, *, status_file: Optional[Path] = None) -> Optional[dict]:
    """가장 최근 세션의 realtime_status.json. 실시간 판단이 안 돌면 None."""
    # 폴더 이름이 아니라 **마지막으로 갱신된** 상태 파일을 고른다 — 지금 돌고 있는 것이 그것이다
    if status_file is not None:
        files = [status_file] if status_file.is_file() else []
    else:
        candidates = list(Path(raw_root).glob("*/*/realtime_status.json"))
        candidates += list((Path(raw_root).parent / "realtime").glob("*/realtime_status.json"))
        candidates += list((Path(raw_root).parent / "realtime").glob("*/prediction/realtime_status.json"))
        files = sorted(candidates,
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
    view["status_dir"] = str(p.parent)
    view["active"] = bool(view.get("running")) and view["stale_s"] <= 5
    return view


def current_realtime_view() -> Optional[dict]:
    view = realtime_view(status_file=RT_JOB.output_dir / "realtime_status.json"
                         if RT_JOB.output_dir else None)
    if view is not None and RT_JOB.output_dir:
        view["active"] = RT_JOB.running and bool(view.get("running", True))
    return view


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
.big{font-size:1.7rem;font-weight:700;color:var(--accent);font-variant-numeric:tabular-nums}
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
.tl{display:flex;height:22px;border-radius:5px;overflow:hidden;background:var(--bg)}
.tl i{display:block;min-width:2px}
.tl .empty{background:var(--st-empty)} .tl .static{background:var(--st-static)}
.tl .motion{background:var(--st-motion)}
.tl .deciding,.tl .suspended{background:repeating-linear-gradient(45deg,var(--stbg),var(--stbg) 3px,
 var(--st-deciding) 3px,var(--st-deciding) 6px);opacity:.6}
.axis{display:flex;justify-content:space-between;color:var(--muted);font-size:.75rem;margin-top:.3rem}
.key{display:flex;gap:.9rem;flex-wrap:wrap;margin-top:.6rem;color:var(--muted);font-size:.8rem}
.key span{display:flex;align-items:center;gap:.35rem}
.key i{width:9px;height:9px;border-radius:2px;display:block}
.chg td{padding:.45rem .3rem;font-size:.92rem}
.chg .t{width:7rem;color:var(--muted);font-variant-numeric:tabular-nums}
.chg .c{width:4rem;text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:.35rem}
.arrow{color:var(--muted);margin:0 .4rem}
.statusbar{display:flex;align-items:center;gap:.6rem;flex-wrap:wrap}
.statusbar .meta{margin-left:auto;display:flex;gap:1rem;flex-wrap:wrap;color:var(--muted);
 font-size:.8rem;font-variant-numeric:tabular-nums}
.ready{border-radius:8px;padding:.55rem .75rem;margin-bottom:.6rem;font-size:.88rem;
 background:var(--warnbg);color:var(--warn);border:1px solid var(--warn)}
"""

PAGE = """<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshSense 제어판</title><style>__CSS__</style></head><body>
<header>
  <h1>MeshSense 제어판</h1>
  <nav id="nav">
    <button data-t="realtime" class="on">실시간</button>
    <button data-t="boards">보드</button>
    <button data-t="collect">수집</button>
    <button data-t="sessions">세션</button>
    <button data-t="analyze">진단·데이터셋</button>
    <button data-t="meta">실험 정보</button>
  </nav>
  <span id="jobbadge" class="hint" style="margin-left:auto"></span>
</header>
<main>
  <section id="realtime" class="on"><div id="rt-controls"></div><div id="rt-view"></div></section>
  <section id="boards"></section>
  <section id="collect"></section>
  <section id="sessions"></section>
  <section id="analyze"></section>
  <section id="meta"></section>
  <div class="card" id="logcard" style="display:none">
    <div class="bar"><h2 id="logtitle" style="margin:0">작업 로그</h2>
      <button class="act danger" id="stopbtn" onclick="post('/api/stop')">중지</button></div>
    <pre class="log" id="log"></pre>
  </div>
</main>
<script>__JS__</script></body></html>"""

JS = r"""
let S = null, tab = 'realtime', shownPng = null, rtSelected = '', rtModel = 'amplitude';
let order = null;  // 이번 블록의 무작위 클래스 순서
try { order = JSON.parse(localStorage.getItem('blockOrder')); } catch (e) {}
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
let realtimeBusy = false;
async function refreshRealtime() {
  if (!S || tab !== 'realtime' || realtimeBusy) return;
  realtimeBusy = true;
  try {
    const result = await (await fetch('/api/realtime/state', {cache:'no-store'})).json();
    S.realtime = result.realtime;
    renderRealtimeView();
  } catch (e) { /* 서버 재시작 시 다음 폴링에서 복구 */ }
  finally { realtimeBusy = false; }
}
function renderRealtimeView() {
  if (S && tab === 'realtime' && changed('rt-view', S.realtime)) {
    document.getElementById('rt-view').innerHTML = renderRealtime(S.realtime);
  }
}
function renderJob() {
  const j = S.job, card = document.getElementById('logcard');
  document.getElementById('jobbadge').innerHTML = j.running
    ? `<span class="spin"></span> ${esc(j.name)} 실행 중` : '';
  if (j.name) {
    card.style.display = '';
    document.getElementById('logtitle').textContent = j.name + (j.running ? '' : ` — 완료 (코드 ${j.rc})`);
    document.getElementById('stopbtn').disabled = !j.running;
    const pre = document.getElementById('log');
    const stick = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 30;
    pre.textContent = j.log.join('\n');
    if (stick) pre.scrollTop = pre.scrollHeight;
  }
}
function render() {
  if (!S) return;
  const busy = S.job.running;
  if (tab === 'realtime' && changed('realtime', [S.realtime_job, S.sessions, S.realtime_runtime, S.job.running])) {
    document.getElementById('rt-controls').innerHTML = renderRealtimeControls();
  }
  renderRealtimeView();
  if (tab === 'boards' && changed('boards', [S.boards, busy])) {
    const rows = S.boards.map(b => `<tr>
      <td><code>${esc(b.port)}</code></td>
      <td>${b.role === 'sink' ? `<span class="tag ok">SINK</span> ` + (b.via||[]).map(r => r.registered
          ? `<span class="tag ok">${esc(r.board_name || ('RX'+r.device_id))}</span>` : `<span class="tag warn">미등록 ${esc(r.sta_mac)}</span>`).join(' ')
        : b.registered ? `<span class="tag ok">${esc(b.board_name || ('RX'+b.device_id))}</span>`
        : b.sta_mac ? `<span class="tag warn">미등록</span>` : `<span class="tag">RX 아님 / 미플래시</span>`}</td>
      <td class="hint">${esc(b.sta_mac || '—')}</td>
      <td>
        <button class="act" ${busy?'disabled':''} onclick="post('/api/flash',{port:'${b.port}',kind:'rx'})">RX 플래시</button>
        <button class="act" ${busy?'disabled':''} onclick="post('/api/flash',{port:'${b.port}',kind:'tx'})">TX 플래시</button>
      </td></tr>`).join('');
    document.getElementById('boards').innerHTML = `<div class="card">
      <h2>연결된 보드</h2>
      <p class="sub">보드는 2초마다 자기 MAC 을 알립니다 — 확인만으로는 보드가 리셋되지 않습니다.
      TX 는 아무것도 보내지 않으므로 'RX 아님'으로 표시되는 것이 정상입니다.
      SINK 포트는 경유하는 RX 목록으로 표시됩니다.</p>
      <div class="bar"><button class="act" ${busy?'disabled':''} onclick="post('/api/rescan')">다시 검색</button></div>
      ${S.boards.length ? `<table><tr><th>포트</th><th>식별</th><th>MAC</th><th>플래시</th></tr>${rows}</table>`
        : `<p class="sub">USB 시리얼 포트를 찾지 못했습니다. 보드 연결을 확인하세요.</p>`}
      <p class="sub" style="margin-top:1rem">등록되지 않은 보드는 먼저 registry 에 추가해야 수집됩니다:
      <code>python scripts/device_registry.py add --port &lt;포트&gt; --board-name RXn</code></p></div>`;
  }
  if (tab === 'collect' && changed('collect', [S.next_session_id, S.rx_count, busy, S.meta.label_target, S.readiness, order])) {
    const opts = S.labels.map(l => `<option value="${l}" ${l===S.meta.label_target?'selected':''}>${l} — ${esc(S.label_desc[l])}</option>`).join('');
    document.getElementById('collect').innerHTML = `<div class="card">
      <h2>수집</h2>
      <p class="sub">라벨은 지금 고른 값이 그대로 데이터에 기록됩니다. 순번은 <code>session.json</code>이 있는 세션만 세어 자동으로 정합니다. 매니페스트가 없는 예전 <code>session_N</code> 폴더는 제외됩니다.</p>
      <div class="bar"><span>다음 순번</span><span class="big">s${S.next_session_id}</span>
        <span class="hint">RX ${S.rx_count}대 인식됨</span></div>
      ${S.readiness.map(r => `<div class="ready ${r.level}">${esc(r.text)}</div>`).join('')}
      <label for="lab">라벨</label><select id="lab">${opts}</select>
      <label>블록 순서 <span class="hint">— 블록마다 새로 뽑습니다. 누르면 라벨이 선택됩니다</span></label>
      <div class="bar">${(order||[]).map((l,i)=>`<button class="act" onclick="document.getElementById('lab').value='${l}'">${i+1}. ${l}</button>`).join('')}
        <button class="act" onclick="drawOrder()">블록 순서 뽑기</button></div>
      <div class="row"><div><label for="dur">수집 시간 (초)</label><input id="dur" type="number" value="300" min="5" step="5"></div>
        <div><label for="delay">시작 지연 (초) <span class="hint">— 자리 잡기·퇴장, 세션에 안 들어감</span></label>
        <input id="delay" type="number" value="10" min="0" step="1"></div></div>
      <div class="bar" style="margin-top:1rem">
        <button class="act primary" ${busy||!S.rx_count?'disabled':''} onclick="startCollect()">수집 시작</button>
        ${!S.rx_count?'<span class="hint">등록된 RX 보드가 없습니다</span>':''}</div></div>`;
  }
  if (tab === 'sessions' && changed('sessions', [S.sessions, shownPng])) {
    const rows = S.sessions.map(s => `<tr>
      <td>${esc(s.date)}<br><span class="hint">${esc(s.name)}</span></td>
      <td><span class="tag">${esc(s.label)}</span></td>
      <td>${s.span_s}s</td>
      <td>${s.devices.map(d=>`RX${d.id} ${d.hz}Hz`).join('<br>')||'—'}</td>
      <td>${s.warn?'<span class="tag warn">품질 확인</span>':'<span class="tag ok">정상</span>'}</td>
      <td>${s.png?`<button class="act" onclick="showPng('${esc(s.path)}')">파형</button>`:''}
          <button class="act" onclick="noteSession('${esc(s.path)}')" title="${esc(s.note)}">메모${s.note?' ✓':''}</button>
          <button class="act danger" onclick="delSession('${esc(s.path)}')">삭제</button></td></tr>`).join('');
    const cnt = Object.entries(S.label_counts).map(([k,v])=>`${k} ${v}`).join(' · ');
    document.getElementById('sessions').innerHTML = `<div class="card">
      <h2>수집한 세션 (${S.sessions.length})</h2>
      <p class="sub">라벨별 세션 수: ${cnt} — 라벨마다 2세션 이상이어야 학습·진단이 가능합니다.</p>
      ${S.sessions.length?`<table><tr><th>날짜</th><th>라벨</th><th>길이</th><th>RX</th><th>품질</th><th></th></tr>${rows}</table>`
        :`<p class="sub">아직 수집한 세션이 없습니다.</p>`}
      ${shownPng ? `<img class="wf" src="/api/img?path=${encodeURIComponent(shownPng)}">` : ''}</div>`;
  }
  if (tab === 'analyze' && changed('analyze', [busy, S.diag_png])) {
    document.getElementById('analyze').innerHTML = `<div class="card">
      <h2>클래스 분리 가능성 진단</h2>
      <p class="sub">지금 배치로 empty / static / action 이 실제로 갈리는지 확인합니다.
      세션 단위 교차검증이라 결과가 부풀려지지 않습니다. 데이터를 많이 모으기 전에 먼저 돌려보세요.</p>
      <div class="bar"><button class="act primary" ${busy?'disabled':''} onclick="post('/api/diagnose')">진단 실행</button></div>
      ${S.diag_png ? `<img class="wf" src="/api/img?path=${encodeURIComponent(S.diag_png)}&v=${encodeURIComponent(S.diag_png_mtime)}">` : ''}</div>
      <div class="card"><h2>전처리용 JSONL 내보내기</h2>
      <p class="sub">수집은 <code>.csi</code>(raw I/Q)로 하고, 학습 전처리는 JSONL 을 읽습니다.
      모든 세션을 <code>mac_collector_output/jsonl/</code> 로 내보내고,
      전처리의 <code>LABEL_SESSION_RANGES</code> 에 넣을 라벨 배정도 함께 출력합니다.</p>
      <div class="bar"><button class="act" ${busy?'disabled':''} onclick="post('/api/export')">JSONL 내보내기</button></div>
      <p class="sub" style="margin-top:.8rem">이후 터미널에서:
      <code>python model_train/preprocessing/preprocess_3rx.py --raw-dir mac_collector_output/jsonl/raw/&lt;날짜&gt;</code></p></div>`;
  }
  if (tab === 'meta' && changed('meta', ['once'])) {
    const f = S.meta_fields.map(([k,t,kind,hint]) => {
      const v = esc(S.meta[k] ?? '');
      const lab = `<label for="${k}">${esc(t)}${hint?` <span class="hint">— ${esc(hint)}</span>`:''}</label>`;
      if (kind==='label') return lab + `<select id="${k}">${S.labels.map(l=>`<option ${l===S.meta[k]?'selected':''}>${l}</option>`).join('')}</select>`;
      if (kind==='textarea') return lab + `<textarea id="${k}">${v}</textarea>`;
      return lab + `<input id="${k}" type="${kind==='number'?'number':'text'}" ${kind==='number'?'step="0.1"':''} value="${v}">`;
    }).join('');
    const chips = S.known_devices.map(([id,n])=>`<button type="button" onclick="tog(${id})">${id} · ${esc(n)}</button>`).join('');
    document.getElementById('meta').innerHTML = `<div class="card">
      <h2>실험 정보</h2>
      <p class="sub">수집할 때마다 세션 폴더에 그대로 복사됩니다. 나중에 조건을 되짚을 때 쓰입니다.</p>
      ${f}<div class="chips">${chips}</div>
      <div class="bar" style="margin-top:1rem"><button class="act primary" onclick="saveMeta()">저장</button>
        <span id="metamsg" class="hint"></span></div></div>`;
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
function renderRealtimeControls(){
  const j = S.realtime_job, rt = S.realtime_runtime;
  const models = rt.models || [{kind:'amplitude', label:'진폭 30초', ...rt}];
  if (j.running) rtModel = j.model_kind || (S.realtime && S.realtime.model_kind) || rtModel;
  const selected = models.find(m => m.kind === rtModel) || models[0];
  rtModel = selected.kind;
  const modelOpts = models.map(m => `<option value="${esc(m.kind)}" ${m.kind===rtModel?'selected':''}>${esc(m.label)}</option>`).join('');
  if (!rtSelected && S.sessions.length) rtSelected = S.sessions[0].path;
  const opts = S.sessions.map(s => `<option value="${esc(s.path)}" ${s.path===rtSelected?'selected':''}>${esc(s.date + ' / ' + s.name)}</option>`).join('');
  const external = S.realtime && S.realtime.active && !j.running;
  const ended = external ? '외부 CLI 판단 실행 중 (종료는 해당 터미널에서 Ctrl+C)' : j.name && !j.running ? (j.rc===0 ? '완료/중지' : '중지/오류 · 로그 확인') : '';
  return `<div class="card"><h2>현재 보드로 실시간 판단</h2>
    <label for="rt-model">사용할 모델</label>
    <select id="rt-model" ${j.running?'disabled':''} onchange="rtModel=this.value;delete sig.realtime;render()">${modelOpts}</select>
    <p class="sub">${esc(selected.model)} · 모델 3개의 예측 평균 · ${selected.window_s}초 입력 · 0.3초 판단 간격</p>
    ${rtModel==='phase60'?'<p class="note">위상은 시험 후보입니다. 기존 앉기 시험에서 정지 오판이 있어 성능 확인이 필요합니다.</p>':''}
    ${rtModel==='phase60_room'?'<p class="note">10/12 내 방 녹화로 추가 학습한 모델입니다. 새 실시간 측정에서 성능을 확인하세요.</p>':''}
    <p class="sub">매번 최대 확률 클래스를 즉시 표시합니다. 확신 기준·유지 시간 대기 없음.</p>
    ${selected.model_exists ? '' : '<p class="note">선택한 모델 파일이 없습니다.</p>'}
    <div class="bar" style="margin-top:.8rem">
      <button class="act primary" ${j.running||S.job.running||!selected.model_exists?'disabled':''} onclick="post('/api/realtime/live',{model_kind:rtModel})">실시간 시작</button>
      <button class="act danger" ${j.running?'':'disabled'} onclick="post('/api/realtime/stop')">판단 정지</button>
      <span class="hint">${j.running ? (j.mode==='live'?'현재 보드 수집·판단 실행 중':j.mode==='follow'?'수집 파일 따라 읽는 중':'녹화 재생 중') : ended}</span></div>
    <p class="sub">세션 선택 없이 현재 SINK 신호를 수집하고 판단합니다. 처음 ${selected.window_s}초는 준비 시간입니다. 모델 변경은 판단 정지 후 새 실행으로 시작하세요. 판단 정지는 이 버튼으로 시작한 수집도 함께 종료합니다. 학습용 데이터에는 넣지 않습니다.</p>
    ${S.job.running?'<p class="note">다른 수집/작업이 실행 중입니다. 해당 작업을 끝내거나 아래 수집 파일 따라 읽기를 사용하세요.</p>':''}
    <details><summary>저장 데이터 재생 / 수집 파일 따라 읽기</summary>
      <label for="rt-session">저장 세션 (현재 보드 신호와 별개)</label>
      <select id="rt-session" ${j.running?'disabled':''} onchange="rtSelected=this.value">${opts}</select>
      <div class="bar" style="margin-top:.8rem">
        <button class="act" ${j.running||!opts||!selected.model_exists?'disabled':''} onclick="startRealtime(false)">녹화 재생</button>
        <button class="act" ${j.running||!opts||!selected.model_exists?'disabled':''} onclick="startRealtime(true)">수집 파일 따라 읽기</button></div>
      <p class="sub">따라 읽기는 다른 수집 작업이 현재 저장 중인 동일 세션에만 사용하세요. 이 모드의 판단 정지는 외부 수집을 종료하지 않습니다.</p>
    </details>
    ${j.log.length ? `<details ${j.rc && !j.running?'open':''}><summary>판단 로그</summary><pre class="log">${esc(j.log.join('\n'))}</pre></details>` : ''}
  </div>`;
}
function startRealtime(follow){
  rtSelected = document.getElementById('rt-session').value;
  post('/api/realtime/start', {path:rtSelected, follow, model_kind:rtModel});
}
function renderRealtime(r){
  if (!r) return `<div class="card"><h2>실시간 판단이 실행 중이 아닙니다</h2>
    <p class="sub">현재 보드는 실시간 시작, 저장 자료는 녹화 재생을 누르세요.</p></div>`;
  const deciding = r.state === 'deciding';
  const warming = r.warmup_remaining_s > 0;
  const pct = deciding
    ? Math.round(100 * (1 - Math.min(1, warming ? r.warmup_remaining_s / (r.window_s || 30) : r.remaining_s / 60)))
    : Math.round(r.confidence * 100);
  const sub = deciding
    ? (r.candidate ? `${esc(RT_LABEL[r.candidate] || r.candidate)} 확인 중`
                   : r.warmup_remaining_s > 0 ? `${r.window_s || 30}초 입력 준비 · ${r.warmup_remaining_s.toFixed(1)}초 남음`
                   : `확신이 기준(${Math.round(r.threshold*100)}%)에 못 미칩니다`)
    : `모델 확률 ${Math.round(r.confidence*100)}%`;
  return `<div class="card hero s-${esc(r.state)}">
      <p class="sub">${esc(r.model_label || '진폭 30초')} · ${r.mode==='replay'?'녹화 재생':r.mode==='live'?'현재 보드 실시간':'수집 파일 따라 읽기'}${r.active?'':' · 종료된 기록'}</p>
      <p class="state">${esc(RT_LABEL[r.state] || r.state)}</p>
      <p class="sub-state"><b>${deciding ? sub : held(r.held_s) + '째'}</b>
        <span>${deciding ? '' : '· ' + esc(r.session)}</span></p>
      <div class="meter"><span class="lab">${deciding ? '남음' : '확률'}</span>
        <span class="track"><span class="fill" style="width:${pct}%"></span>
          ${deciding || !r.threshold ? '' : `<span class="mark" style="left:${Math.round(r.threshold*100)}%"></span>`}</span>
        <span class="num">${deciding ? (warming ? r.warmup_remaining_s.toFixed(1) : Math.round(r.remaining_s)) + '초' : pct + '%'}</span></div>
      ${r.note ? `<p class="note">${esc(r.note)}</p>` : ''}
    </div>
    <div class="card"><h2>최근 기록</h2>
      <div class="tl">${(r.spans||[]).map(s =>
        `<i class="${esc(s.state)}" style="width:${s.pct}%"></i>`).join('')}</div>
      <div class="axis"><span>${spanLabel(r.spans)} 전</span><span>지금</span></div>
      <div class="key">
        <span><i style="background:var(--st-empty)"></i>빈 방</span>
        <span><i style="background:var(--st-static)"></i>정지</span>
        <span><i style="background:var(--st-motion)"></i>움직임</span>
        <span><i style="background:var(--st-deciding)"></i>판단 중</span></div></div>
    <div class="card"><h2>상태 변화 (${(r.events||[]).length})</h2>
      ${(r.events||[]).length ? `<table class="chg">${r.events.slice().reverse().map(e =>
        `<tr><td class="t">${esc(ago(e.t, r.t))}</td>
         <td><span class="dot" style="background:var(--st-${esc(e.from)})"></span>${esc(RT_LABEL[e.from]||e.from)}
         <span class="arrow">→</span><b><span class="dot" style="background:var(--st-${esc(e.to)})"></span>${esc(RT_LABEL[e.to]||e.to)}</b></td>
         <td class="c">${Math.round(e.confidence*100)}%</td></tr>`).join('')}</table>`
        : `<p class="sub">아직 상태 변화가 없습니다.</p>`}</div>
    <div class="card"><div class="statusbar">
      <button class="act" ${r.active?'':'disabled'} onclick="post('/api/mark')">전환 표시</button>
      <span class="meta">
        <span class="tag ${r.n_rx >= 3 ? 'ok' : 'warn'}">RX ${r.n_rx}대</span>
        <span>${r.hz}Hz</span><span>창 ${r.window_s}초</span>
        <span>${r.mode==='live'?'현재 보드 신호':esc(r.session)}</span>
        <span>${r.active ? '갱신 중' : '완료/중지된 기록'}</span>
      </span></div></div>`;
}
function tog(id){const f=document.getElementById('devices.expected_device_ids');
  const s=f.value.split(/[,\s]+/).filter(Boolean);const i=s.indexOf(String(id));
  if(i<0)s.push(String(id));else s.splice(i,1);f.value=s.sort((a,b)=>a-b).join(', ');}
async function saveMeta(){
  const body={}; S.meta_fields.forEach(([k])=>{const e=document.getElementById(k); if(e) body[k]=e.value;});
  await post('/api/meta', body);
  document.getElementById('metamsg').textContent = '저장했습니다 · ' + new Date().toLocaleTimeString();
}
function startCollect(){
  post('/api/collect',{label:document.getElementById('lab').value,
                       duration:parseFloat(document.getElementById('dur').value),
                       delay:parseFloat(document.getElementById('delay').value)});
}
function drawOrder(){
  const prev = JSON.stringify(order); let o;
  do { o = [...S.labels]; for (let i = o.length-1; i > 0; i--) { const j = Math.floor(Math.random()*(i+1)); [o[i],o[j]] = [o[j],o[i]]; } }
  while (JSON.stringify(o) === prev);          // 직전 블록과 같은 순서는 다시 뽑는다
  order = o;
  try { localStorage.setItem('blockOrder', JSON.stringify(o)); } catch (e) {}
  render();
}
function noteSession(p){
  const s = S.sessions.find(x => x.path === p);
  const t = prompt(p + '\n\n세션 메모 (예: 피험자가 RX102 시야를 가림). 비우면 삭제', s ? s.note : '');
  if (t !== null) post('/api/note', {path: p, text: t});
}
function delSession(p){ if(confirm(p+'\n\n이 세션을 삭제할까요?')) post('/api/delete',{path:p}); }
function showPng(p){ shownPng = (shownPng === p+'/csi_waterfall.png') ? null : p+'/csi_waterfall.png'; render(); }
refresh(); setInterval(refresh, 1200);
setInterval(refreshRealtime, 300);
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
        elif u.path == "/api/realtime/state":
            self._json({"realtime": current_realtime_view()})
        elif u.path == "/api/state":
            rows = session_rows()
            meta = read_meta(SESSION_META)
            self._json({
                "boards": Handler.boards_cache,
                # SINK 포트는 뒤에 붙은 RX 들(via)로 센다
                "rx_count": sum(1 for b in Handler.boards_cache
                                for r in (b.get("via") or [b]) if r.get("registered")),
                "sessions": rows, "label_counts": label_counts(rows),
                "next_session_id": next_session_id(OUTPUT_DIR),
                "labels": list(LABELS), "label_desc": LABEL_DESC,
                "meta": meta, "meta_fields": FIELDS, "known_devices": known_devices(),
                "diag_png": DIAG_PNG_REL if (REPO_ROOT / DIAG_PNG_REL).is_file() else None,
                "diag_png_mtime": int((REPO_ROOT / DIAG_PNG_REL).stat().st_mtime)
                if (REPO_ROOT / DIAG_PNG_REL).is_file() else 0,
                "readiness": readiness(),
                "realtime": current_realtime_view(),
                "realtime_job": RT_JOB.state(), "realtime_runtime": runtime_info(),
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
    with PORT_LOCK:
        if JOB.running or (RT_JOB.running and RT_JOB.mode == "live"):
            return {"error": "수집 중에는 포트를 다시 검색하지 않습니다. 먼저 수집을 정지하세요."}
        boards = []
        for port in list_ports():
            try:
                ensure_port_free(port)
                boards.append(identify(port))
            except (OSError, ValueError, subprocess.SubprocessError):
                continue
        Handler.boards_cache = boards
        return {"ok": True}


def dispatch(path: str, body: dict) -> dict:
    with PORT_LOCK:
        return _dispatch(path, body)


def _dispatch(path: str, body: dict) -> dict:
    if path == "/api/realtime/live":
        with PORT_LOCK:
            if RT_JOB.running:
                return {"error": "판단이 이미 실행 중입니다. 먼저 판단 정지를 누르세요."}
            if JOB.running:
                return {"error": "다른 수집/작업이 실행 중입니다. 먼저 종료하세요."}
            model_kind = body.get('model_kind', 'amplitude')
            model = resolve_model(model_kind)
            check_runtime(model, model_kind=model_kind)
            # 포트 번호는 재연결 때 바뀐다. 캐시 번호를 그대로 사용하지 않는다.
            result = rescan()
            if "error" in result:
                return result
            sinks = [b for b in Handler.boards_cache if b.get("role") == "sink"]
            if len(sinks) != 1:
                return {"error": "SINK 1대를 USB로 연결하세요. 자동 플래시나 리셋은 하지 않습니다."}
            output = new_output_dir()
            cmd = build_live_command(sinks[0]["port"], output_dir=output, model=model, model_kind=model_kind)
            err = RT_JOB.start("현재 보드 실시간 판단", cmd, session_dir=output / "capture",
                               output_dir=output / "prediction", mode="live", model_kind=model_kind)
            return {"error": err} if err else {"ok": True, "output_dir": str(output)}
    if path == "/api/realtime/start":
        if RT_JOB.running:
            return {"error": "판단이 이미 실행 중입니다. 먼저 판단 정지를 누르세요."}
        if JOB.running and "플래시" in JOB.name:
            return {"error": "보드 플래시가 끝난 뒤 판단을 시작하세요"}
        target = (REPO_ROOT / str(body.get("path", ""))).resolve()
        if not target.is_relative_to(RAW_ROOT.resolve()) or not (target / "session.json").is_file():
            return {"error": "수집 폴더 아래의 세션을 선택하세요"}
        follow = body.get("follow", False)
        if not isinstance(follow, bool):
            return {"error": "follow는 true/false여야 합니다"}
        model_kind = body.get('model_kind', 'amplitude')
        model = resolve_model(model_kind)
        check_runtime(model, model_kind=model_kind)
        output = new_output_dir()
        cmd = build_command(target, follow=follow, output_dir=output, speed=1 if not follow else 0,
                            model=model, model_kind=model_kind)
        err = RT_JOB.start("실시간 판단" if follow else "녹화 재생", cmd,
                           session_dir=target, output_dir=output, mode="follow" if follow else "replay",
                           model_kind=model_kind)
        return {"error": err} if err else {"ok": True, "output_dir": str(output)}

    if path == "/api/realtime/stop":
        RT_JOB.stop()
        return {"ok": True}

    if path == "/api/rescan":
        return rescan()

    if path == "/api/stop":
        JOB.stop()
        return {"ok": True}

    if path == "/api/flash":
        if RT_JOB.running:
            return {"error": "보드 플래시 전에 판단 정지를 누르세요"}
        port, kind = body.get("port"), body.get("kind")
        proj = SEND_POC if kind == "tx" else RECV_POC
        def after_flash(rc: int) -> str:
            rescan()                       # 펌웨어가 바뀌었으니 보드 식별을 다시 한다
            return "" if rc == 0 else "플래시 실패 — 포트를 쓰는 다른 프로그램이 있는지 확인하세요"

        err = JOB.start(f"{kind.upper()} 플래시 · {Path(port).name}",
                        [PY, str(SCRIPT_DIR / "_idf_flash.py"), str(proj), port],
                        on_done=after_flash)
        return {"error": err} if err else {"ok": True}

    if path == "/api/collect":
        with PORT_LOCK:
            if RT_JOB.running and RT_JOB.mode == "live":
                return {"error": "현재 보드 실시간 수집 중입니다. 먼저 판단 정지를 누르세요."}
            label, dur = body.get("label"), float(body.get("duration") or 300)
            delay = max(0.0, float(body.get("delay") or 0))
            if label not in LABELS:
                return {"error": f"알 수 없는 라벨: {label}"}
            err = JOB.start(f"수집 · {label} · {dur:.0f}초",
                            [PY, str(SCRIPT_DIR / "_collect_run.py"), label, str(dur), str(delay)])
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

    if path == "/api/mark":
        view = current_realtime_view()
        if not view or not view.get("active"):
            return {"error": "실시간 판단이 실행 중이 아닙니다"}
        target = Path(view["status_dir"])
        if target is None:
            return {"error": "세션을 찾지 못했습니다"}
        with (target / "realtime_marks.jsonl").open("a", encoding="utf-8") as fp:
            fp.write(json.dumps({"wall": time.time(), "note": str(body.get("note", "")),
                                 "via": "gui", "t": view.get("t"), "tx_seq": view.get("tx_seq"),
                                 "state": view.get("state")}, ensure_ascii=False) + "\n")
        return {"ok": True}

    if path == "/api/delete":
        target = (REPO_ROOT / body.get("path", "")).resolve()
        if RT_JOB.running and target == RT_JOB.session_dir:
            return {"error": "판단 중인 세션은 삭제할 수 없습니다. 먼저 판단 정지를 누르세요."}
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
        RT_JOB.stop()
        JOB.stop()
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
