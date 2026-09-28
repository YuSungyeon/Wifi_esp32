#!/usr/bin/env python3
"""세션 메타(`mac_collector/session_meta.yaml`)를 브라우저 폼으로 편집한다.

YAML 을 직접 손으로 고치다 보면 들여쓰기·따옴표·키 이름을 틀리기 쉽고, 틀려도 수집이
그냥 돌아가서 나중에야 알게 된다. 폼이 값을 받아 파일을 대신 쓴다.

    python scripts/session_form.py            # 브라우저 자동 실행
    python scripts/session_form.py --no-open  # URL 만 출력

표준 라이브러리만 쓴다 (`http.server`). **127.0.0.1 에만 바인딩**한다 — 이 폼은 파일을
쓰므로 외부에 열어둘 이유가 없다.
"""
from __future__ import annotations

import argparse
import html
import http.server
import json
import socket
import sys
import threading
import time
import urllib.parse
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csi_session import next_session_id  # noqa: E402
from csi_store import LABELS  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SESSION_META = REPO_ROOT / "mac_collector" / "session_meta.yaml"
OUTPUT_DIR = REPO_ROOT / "mac_collector_output"
DEVICE_REGISTRY = REPO_ROOT / "mac_collector" / "device_registry.csv"

#: 폼 필드 = (키 경로, 라벨, 종류, 도움말)
FIELDS = [
    ("label_target", "다음 수집 라벨", "label", "수집 시작 시 프롬프트의 기본 선택값"),
    ("date", "실험일", "text", "실제 실험 날짜 (수집 폴더 날짜와 별개로 기록)"),
    ("condition.placement_id", "배치 번호", "text", "보드 배치를 바꿀 때마다 새 번호 (P1, P2 …). 좌표는 device_registry.csv 에"),
    ("condition.subject_id", "피험자 코드", "text", "익명 코드 (S1, S2 …). empty 세션은 비움"),
    ("room.width_m", "방 가로 (m)", "number", ""),
    ("room.height_m", "방 세로 (m)", "number", ""),
    ("room.description", "공간 설명", "text", "배치·가구·특이사항"),
    ("experiment.objective", "실험 목표", "text", ""),
    ("experiment.split_strategy", "분할 방식", "text", "윈도가 90% 겹치므로 session-wise 권장"),
    ("devices.expected_device_ids", "참여 RX device_id", "text", "쉼표 구분 (예: 101, 103)"),
    ("operator.name", "운영자", "text", ""),
    ("operator.notes", "메모", "textarea", "피험자 수, 동작 프로토콜, 환경 변화 등"),
]

DEFAULTS = {
    "label_target": "static",
    "date": time.strftime("%Y-%m-%d"),
    "condition.placement_id": "P1",
    "condition.subject_id": "",
    "room.width_m": "3.0",
    "room.height_m": "3.0",
    "room.description": "3m x 3m indoor room",
    "experiment.objective": "3-class activity classification",
    "experiment.split_strategy": "session-wise",
    "devices.expected_device_ids": "",
    "operator.name": "",
    "operator.notes": "",
}


# ── YAML 입출력 (필요한 만큼만; PyYAML 의존성을 만들지 않는다) ──────────────────
def read_meta(path: Path) -> dict:
    """`session_meta.yaml` → 평탄화된 {키경로: 문자열}. 파일이 없으면 기본값."""
    values = dict(DEFAULTS)
    if not path.is_file():
        return values
    section = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        body = line.split("#", 1)[0].rstrip() if not _in_quotes_hash(line) else line.rstrip()
        if ":" not in body:
            continue
        key, _, val = body.partition(":")
        indented = key[0].isspace()
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not indented:
            section = key if not val else None
            if val:
                values[key] = val
        elif section:
            values[f"{section}.{key}"] = val
    # YAML 리스트 표기 `[101, 103]` 는 폼에서 쉼표 목록으로 보여준다
    ids = values.get("devices.expected_device_ids", "")
    values["devices.expected_device_ids"] = ", ".join(
        t.strip() for t in ids.strip("[]").replace(",", " ").split()
    )
    return values


def _in_quotes_hash(line: str) -> bool:
    """값 안의 `#` 를 주석으로 잘라내지 않도록 대충 판별."""
    q = line.find('"')
    return q != -1 and "#" in line[q:]


def _yaml_scalar(value: str) -> str:
    v = value.strip()
    if v == "":
        return '""'
    if v.startswith("[") and v.endswith("]"):
        return v
    try:
        float(v)
        return v
    except ValueError:
        pass
    return json.dumps(v, ensure_ascii=False)


def write_meta(path: Path, values: dict) -> None:
    """폼 값을 기존 파일 위에 **병합**해 쓴다.

    폼이 보내지 않은 키는 기존 값을 유지한다 — 필드가 하나라도 빠진 요청이 나머지를
    통째로 날리지 않게 하기 위해서다.
    """
    merged = read_meta(path)
    merged.update({k: v for k, v in values.items() if k in merged or k in DEFAULTS})
    values = merged
    ids = [t.strip() for t in values.get("devices.expected_device_ids", "").replace(",", " ").split()]
    lines = [
        "# MeshSense 실험 세션 메타",
        "# 편집은 `python scripts/session_form.py` (브라우저 폼)를 권장합니다.",
        "# 수집 시 세션 디렉터리에 session_meta_snapshot.yaml 로 복사됩니다.",
        "#",
        "# session_id 는 여기에 두지 않습니다 — 수집한 세션들의 최댓값+1로 자동 부여됩니다.",
        "",
        "# 수집 시작 프롬프트의 기본 라벨 (empty / static / action)",
        f"label_target: {_yaml_scalar(values.get('label_target', 'static'))}",
        "",
        "# 실험일 (수집 폴더 날짜와 별개로 기록)",
        f"date: {_yaml_scalar(values.get('date', ''))}",
        "",
        "# 조건 식별자 — 조건 단위 분할(날짜·배치·피험자)의 키. doc/collection-protocol.md §6",
        "condition:",
        f"  placement_id: {_yaml_scalar(values.get('condition.placement_id', ''))}",
        f"  subject_id: {_yaml_scalar(values.get('condition.subject_id', ''))}",
        "",
        "# 실험 공간",
        "room:",
        f"  width_m: {_yaml_scalar(values.get('room.width_m', ''))}",
        f"  height_m: {_yaml_scalar(values.get('room.height_m', ''))}",
        f"  description: {_yaml_scalar(values.get('room.description', ''))}",
        "",
        "# 실험 설계",
        "experiment:",
        f"  objective: {_yaml_scalar(values.get('experiment.objective', ''))}",
        f"  split_strategy: {_yaml_scalar(values.get('experiment.split_strategy', ''))}",
        "",
        "# 이번 실험에 켤 RX device_id (device_registry.csv 와 대응)",
        "devices:",
        f"  expected_device_ids: [{', '.join(ids)}]",
        "",
        "# 운영자·현장 메모",
        "operator:",
        f"  name: {_yaml_scalar(values.get('operator.name', ''))}",
        f"  notes: {_yaml_scalar(values.get('operator.notes', ''))}",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def known_devices() -> list[tuple[int, str]]:
    if not DEVICE_REGISTRY.is_file():
        return []
    out = []
    for line in DEVICE_REGISTRY.read_text(encoding="utf-8").splitlines()[1:]:
        cells = line.split(",")
        if len(cells) >= 2 and cells[0].strip().isdigit():
            out.append((int(cells[0]), cells[1].strip()))
    return out


# ── HTML ───────────────────────────────────────────────────────────────────────
PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MeshSense 세션 메타</title>
<style>
  :root {{ color-scheme: light dark;
    --bg:#f6f7f9; --card:#fff; --fg:#1a1c1f; --muted:#666; --line:#dcdfe4;
    --accent:#2a6df4; --ok:#137a45; --okbg:#e8f6ee; }}
  @media (prefers-color-scheme: dark) {{ :root {{
    --bg:#16181c; --card:#212429; --fg:#e8eaed; --muted:#9aa0a6; --line:#3a3f46;
    --accent:#6b9bff; --ok:#5ed49a; --okbg:#1c3227; }} }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:2rem 1rem 4rem; background:var(--bg); color:var(--fg);
    font:15px/1.6 -apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Segoe UI",sans-serif; }}
  main {{ max-width:680px; margin:0 auto; }}
  h1 {{ font-size:1.4rem; margin:0 0 .25rem; }}
  .sub {{ color:var(--muted); font-size:.9rem; margin:0 0 1.5rem; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
    padding:1.25rem 1.4rem; margin-bottom:1rem; }}
  .next {{ display:flex; align-items:baseline; gap:.6rem; }}
  .next b {{ font-size:1.9rem; color:var(--accent); font-variant-numeric:tabular-nums; }}
  label {{ display:block; margin:1rem 0 .3rem; font-weight:600; font-size:.9rem; }}
  label:first-of-type {{ margin-top:0; }}
  .hint {{ color:var(--muted); font-weight:400; font-size:.82rem; }}
  input,textarea,select {{ width:100%; padding:.55rem .7rem; font:inherit; color:var(--fg);
    background:var(--bg); border:1px solid var(--line); border-radius:8px; }}
  textarea {{ min-height:5.5rem; resize:vertical; }}
  .row {{ display:grid; grid-template-columns:1fr 1fr; gap:1rem; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:.4rem; margin-top:.45rem; }}
  .chips button {{ font:inherit; font-size:.85rem; padding:.25rem .6rem; cursor:pointer;
    background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:999px; }}
  .save {{ position:sticky; bottom:1rem; width:100%; padding:.8rem; font:inherit;
    font-weight:700; color:#fff; background:var(--accent); border:0; border-radius:10px; cursor:pointer; }}
  .flash {{ background:var(--okbg); color:var(--ok); border:1px solid var(--ok);
    border-radius:8px; padding:.6rem .8rem; margin-bottom:1rem; font-size:.9rem; }}
  code {{ font-size:.85em; background:var(--bg); padding:.1rem .35rem; border-radius:4px; }}
</style></head><body><main>
<h1>MeshSense 세션 메타</h1>
<p class="sub">{path}</p>
{flash}
<form method="post" action="/save">
  <div class="card next">
    <span>다음 수집 순번</span><b>s{next_id}</b>
    <span class="hint">기존 세션 최댓값+1로 자동 부여됩니다. 직접 적을 필요 없습니다.</span>
  </div>
  <div class="card">{fields}</div>
  <button class="save" type="submit">저장</button>
</form>
<p class="sub" style="margin-top:1.5rem">저장 후 이 창을 닫고
<code>python scripts/meshsense_cli.py</code> → [1] USB 수집 → [2] 수집 으로 진행하세요.</p>
</main></body></html>"""


def render(values: dict, flash: str = "") -> bytes:
    parts = []
    devs = known_devices()
    for key, title, kind, hint in FIELDS:
        v = html.escape(str(values.get(key, "")))
        hint_html = f' <span class="hint">— {html.escape(hint)}</span>' if hint else ""
        parts.append(f'<label for="{key}">{html.escape(title)}{hint_html}</label>')
        if kind == "label":
            opts = "".join(
                f'<option value="{l}"{" selected" if values.get(key) == l else ""}>{l}</option>'
                for l in LABELS
            )
            parts.append(f'<select id="{key}" name="{key}">{opts}</select>')
        elif kind == "textarea":
            parts.append(f'<textarea id="{key}" name="{key}">{v}</textarea>')
        else:
            t = "number" if kind == "number" else "text"
            step = ' step="0.1"' if kind == "number" else ""
            parts.append(f'<input type="{t}"{step} id="{key}" name="{key}" value="{v}">')
        if key == "devices.expected_device_ids" and devs:
            chips = "".join(
                f'<button type="button" onclick="tog({d})">{d} · {html.escape(n)}</button>'
                for d, n in devs
            )
            parts.append(f'<div class="chips">{chips}</div>')
    parts.append("""<script>
function tog(id){const f=document.getElementById('devices.expected_device_ids');
 const s=f.value.split(/[,\\s]+/).filter(Boolean);const i=s.indexOf(String(id));
 if(i<0)s.push(String(id));else s.splice(i,1);
 f.value=s.sort((a,b)=>a-b).join(', ');}
</script>""")
    body = PAGE.format(
        path=html.escape(str(SESSION_META)),
        next_id=next_session_id(OUTPUT_DIR),
        flash=f'<div class="flash">{html.escape(flash)}</div>' if flash else "",
        fields="\n".join(parts),
    )
    return body.encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    flash = ""

    def _send(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if urllib.parse.urlparse(self.path).path != "/":
            self._send(b"not found", 404)
            return
        body = render(read_meta(SESSION_META), Handler.flash)
        Handler.flash = ""
        self._send(body)

    def do_POST(self) -> None:  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8"))
        values = {k: v[0] for k, v in form.items()}
        write_meta(SESSION_META, values)
        print(f"[form] 저장: {SESSION_META}")
        Handler.flash = f"저장했습니다 — {SESSION_META.name}"
        self.send_response(303)
        self.send_header("Location", "/")
        self.end_headers()

    def log_message(self, *args) -> None:  # 요청 로그 억제
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=0, help="0이면 빈 포트 자동 선택")
    ap.add_argument("--no-open", action="store_true", help="브라우저를 자동으로 열지 않음")
    args = ap.parse_args()

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"[form] {url}  (종료: Ctrl+C)")
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[form] 종료")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
