#!/usr/bin/env python3
"""실시간 판단 CLI 화면.

    python realtime/ui.py --session <세션>            # 녹화 파일 재생
    python realtime/ui.py --session <세션> --follow   # 실시간
    python realtime/ui.py --session <세션> --plain    # 한 줄 갱신 (원격·로그용)

터미널에는 글자 크기가 없으므로 **공간 배분**으로 서열을 만든다. 상태 블록이 화면의 40%를
차지하고 그 안에서 상태 한 단어만 왼쪽 위에 놓인다. 나머지는 비운다 — 여백이 강조 장치다.
화면이 좁아지면 이력부터 버리고 여백은 마지막까지 남긴다.
"""
from __future__ import annotations

import argparse
import select
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime.engine import MODEL, Engine, available_models  # noqa: E402

LABEL = {"empty": "빈 방", "static": "정지", "motion": "움직임",
         "deciding": "판단 중", "suspended": "판단 정지"}
COLOR = {"empty": "\x1b[90m", "static": "\x1b[94m", "motion": "\x1b[33m",
         "deciding": "\x1b[2m", "suspended": "\x1b[91m"}
RESET, BOLD = "\x1b[0m", "\x1b[1m"
BLOCK_ROWS = 9


def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}분 {s % 60:02d}초" if s >= 60 else f"{s}초"


SPARK = " ▁▂▃▄▅▆▇█"


def spark(values: list, step_s: float, cells: int = 24, span_s: float = 60.0) -> str:
    """최근 span_s 초를 cells 칸으로. 칸마다 **최대값**을 쓴다 — 짧은 움직임이 평균에 묻히면
    띠를 넣은 뜻이 없다. 값이 낮으면 공백이라 아무 일 없을 때 화면이 조용하다."""
    if not values or not step_s:
        return ""
    per = max(1, round(span_s / step_s / cells))
    need = per * cells
    tail = ([None] * max(0, need - len(values)) + list(values))[-need:]
    out = []
    for i in range(cells):
        chunk = [v for v in tail[i * per:(i + 1) * per] if v is not None]
        out.append(SPARK[round(min(1.0, max(0.0, max(chunk))) * 8)] if chunk else " ")
    return "".join(out)


def bar(fraction: float, width: int = 24) -> str:
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "█" * filled + "░" * (width - filled)


def _pad(text: str, width: int, visible: int | None = None) -> str:
    """색 코드를 세지 않고 폭을 맞춘다. 한글은 두 칸으로 본다."""
    visible = visible if visible is not None else _cells(text)
    return text + " " * max(0, width - visible)


def _cells(text: str) -> int:
    """터미널 표시 폭. 한글은 두 칸, **괘선·블록 문자(U+2500~U+259F)는 한 칸**이다 —
    이 둘을 같이 묶으면 막대가 들어간 줄만 테두리가 어긋난다."""
    return sum(1 if ord(ch) <= 0x259F else 2 for ch in text)


def state_block(view: dict, color: bool) -> list[str]:
    state = view["state"]
    name = LABEL.get(state, state)
    painted = f"{COLOR.get(state, '')}{BOLD}{name}{RESET}" if color else name
    rows = ["", f"     {painted}" if not color else f"     {painted}", ""]
    visible = [0, _cells(name) + 5, 0]

    if state == "suspended":
        rows.append(f"     {view['note']}")
        visible.append(_cells(view["note"]) + 5)
    elif state == "deciding":
        cand = view.get("candidate")
        if cand:
            line = f"     {LABEL.get(cand, cand)} 확인 중 — {int(view['remaining_s'])}초 남음"
        else:
            line = f"     확신 {view['confidence']:.0%} · 기준 {view['threshold']:.0%} 미만"
        rows.append(line)
        visible.append(_cells(line))
    else:
        line = f"     {hms(view['held_s'])}째"
        rows.append(line)
        visible.append(_cells(line))
        conf = f"     확신  {bar(view['confidence'])}  {view['confidence']:.0%}"
        rows.append(conf)
        visible.append(_cells(conf))
        # 활동량 — 1단계(창 3초)가 본 움직임 세기. 상태와 시간 축이 달라 따로 보인다.
        act = spark(view.get("activity") or [], view.get("activity_step_s") or 0)
        if act:
            line = f"     활동  {act}  최근 60초"
            rows.append(line)
            visible.append(_cells(line))

    if view.get("note") and state != "suspended":
        rows += ["", f"     {view['note']}"]
        visible += [0, _cells(view["note"]) + 5]
    while len(rows) < BLOCK_ROWS:
        rows.append("")
        visible.append(0)
    return [(r, v) for r, v in zip(rows[:BLOCK_ROWS], visible[:BLOCK_ROWS])]


def render(view: dict, width: int = 62, height: int = 24, color: bool = False) -> str:
    inner = width - 2
    clock = time.strftime("%H:%M:%S")
    title = f"─ MeshSense 실시간 "
    rows = ["┌" + title + "─" * max(0, inner - _cells(title) - len(clock) - 2) + f" {clock} ┐"]
    rows += [f"│{_pad(text, inner, vis)}│" for text, vis in state_block(view, color)]

    history = max(0, height - (BLOCK_ROWS + 5))
    if history:
        rows.append("├" + "─" * inner + "┤")
        for e in view["events"][::-1][:history]:
            line = (f"  {hms(view['t'] - e['t']):>9} 전   "
                    f"{LABEL.get(e['from'], e['from'])} → {LABEL.get(e['to'], e['to'])}"
                    f"   {e['confidence']:.0%}")
            rows.append(f"│{_pad(line, inner)}│")
        for _ in range(history - len(view["events"])):
            rows.append(f"│{' ' * inner}│")

    rows.append("├" + "─" * inner + "┤")
    quality = "정상" if view["n_rx"] >= 3 else ("저하" if view["n_rx"] == 2 else "이상")
    foot = (f"  {view['hz']:.1f}Hz  RX{view['n_rx']}  {quality}  "
            f"{view.get('model', '')[:14]}  {view['session'][:16]}   [m]전환 [q]종료")
    rows.append(f"│{_pad(foot, inner)}│")
    rows.append("└" + "─" * inner + "┘")
    return "\n".join(rows[:height])


def render_plain(view: dict) -> str:
    extra = ""
    if view["state"] == "deciding" and view.get("candidate"):
        extra = f" ({LABEL.get(view['candidate'])} 확인 중 {int(view['remaining_s'])}초)"
    act = (view.get("activity") or [None])[-1]
    now = f" 활동 {act:.0%}" if act is not None else ""
    return (f"[{time.strftime('%H:%M:%S')}] {LABEL.get(view['state'], view['state']):5s} "
            f"{view['confidence']:>4.0%}  {hms(view['held_s']):>8}  "
            f"{view['hz']:.1f}Hz RX{view['n_rx']}{now}{extra}")


def read_key() -> str | None:
    """대기 없이 키 하나. 터미널이 아니면(파이프) None."""
    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.read(1) if ready else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, default=None)
    ap.add_argument("--follow", action="store_true", help="실시간(파일 tail). 없으면 재생")
    ap.add_argument("--plain", action="store_true", help="한 줄 갱신 모드")
    ap.add_argument("--stride", type=int, default=30, help="판단 간격 (frame, 100Hz)")
    ap.add_argument("--conf", type=float, default=0.6)
    ap.add_argument("--model", type=Path, default=MODEL,
                    help="쓸 체크포인트 (기본: 배포 진폭 모델). --list-models 로 목록을 본다")
    ap.add_argument("--list-models", action="store_true", help="쓸 수 있는 체크포인트 목록")
    ap.add_argument("--speed", type=float, default=0.0,
                    help="재생 속도 (0=최대한 빨리, 1=실제 속도)")
    args = ap.parse_args()

    if args.list_models:
        for m in available_models():
            mark = " (배포)" if m["is_deployed"] else ""
            print(f"  {m['mode']:7s} 창 {m['window_s']:>4.0f}초  {m['name']:22s} "
                  f"{m['score'] or '—':>7}{mark}\n          {m['path']}")
        return 0

    if args.session is None:
        ap.error("--session 또는 --list-models 가 필요하다")
    if not (args.session / "session.json").is_file():
        print(f"세션 디렉터리가 아닙니다: {args.session}")
        return 1
    eng = Engine(args.session, model_path=args.model, stride=args.stride, conf=args.conf)
    print(f"모델: {eng.model_name} ({eng.kind}, 창 {eng.window / 100:.0f}초)", flush=True)
    size = None
    try:
        import shutil
        size = shutil.get_terminal_size((62, 24))
    except OSError:
        pass

    if not args.follow:
        eng.start_replay()          # 링버퍼(240초)보다 긴 세션도 처음부터
    t = eng.window / 100.0
    try:
        while True:
            key = read_key()
            if key == "q":
                break
            if key == "m":
                eng.mark()
            eng.step(now=None if args.follow else t)
            view = eng.view()
            if args.plain:
                print(render_plain(view), flush=True)
            else:
                print("\x1b[H\x1b[J" + render(view, width=min(size.columns, 80) if size else 62,
                                              height=size.lines if size else 24, color=True),
                      flush=True)
            if args.follow:
                time.sleep(args.stride / 100.0)
            else:
                if not eng.stream.advance(args.stride):
                    break
                t += args.stride / 100.0
                if args.speed:
                    time.sleep(args.stride / 100.0 / args.speed)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
