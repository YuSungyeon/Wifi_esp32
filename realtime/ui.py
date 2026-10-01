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

from realtime.engine import Engine  # noqa: E402

LABEL = {"empty": "빈 방", "static": "정지", "motion": "움직임",
         "deciding": "판단 중", "suspended": "판단 정지"}
COLOR = {"empty": "\x1b[90m", "static": "\x1b[94m", "motion": "\x1b[33m",
         "deciding": "\x1b[2m", "suspended": "\x1b[91m"}
RESET, BOLD = "\x1b[0m", "\x1b[1m"
BLOCK_ROWS = 9


def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}분 {s % 60:02d}초" if s >= 60 else f"{s}초"


def bar(fraction: float, width: int = 24) -> str:
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "█" * filled + "░" * (width - filled)


def _pad(text: str, width: int, visible: int | None = None) -> str:
    """색 코드를 세지 않고 폭을 맞춘다. 한글은 두 칸으로 본다."""
    visible = visible if visible is not None else _cells(text)
    return text + " " * max(0, width - visible)


def _cells(text: str) -> int:
    return sum(2 if ord(ch) > 0x2500 else 1 for ch in text)


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
    foot = (f"  {view['hz']:.1f}Hz  RX{view['n_rx']}  {quality}  {view['session'][:18]}"
            f"        [m]전환 [q]종료")
    rows.append(f"│{_pad(foot, inner)}│")
    rows.append("└" + "─" * inner + "┘")
    return "\n".join(rows[:height])


def render_plain(view: dict) -> str:
    extra = ""
    if view["state"] == "deciding" and view.get("candidate"):
        extra = f" ({LABEL.get(view['candidate'])} 확인 중 {int(view['remaining_s'])}초)"
    return (f"[{time.strftime('%H:%M:%S')}] {LABEL.get(view['state'], view['state']):5s} "
            f"{view['confidence']:>4.0%}  {hms(view['held_s']):>8}  "
            f"{view['hz']:.1f}Hz RX{view['n_rx']}{extra}")


def read_key() -> str | None:
    """대기 없이 키 하나. 터미널이 아니면(파이프) None."""
    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.read(1) if ready else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--follow", action="store_true", help="실시간(파일 tail). 없으면 재생")
    ap.add_argument("--plain", action="store_true", help="한 줄 갱신 모드")
    ap.add_argument("--stride", type=int, default=30, help="판단 간격 (frame, 100Hz)")
    ap.add_argument("--conf", type=float, default=0.7)
    ap.add_argument("--speed", type=float, default=0.0,
                    help="재생 속도 (0=최대한 빨리, 1=실제 속도)")
    args = ap.parse_args()

    if not (args.session / "session.json").is_file():
        print(f"세션 디렉터리가 아닙니다: {args.session}")
        return 1
    eng = Engine(args.session, stride=args.stride, conf=args.conf)
    size = None
    try:
        import shutil
        size = shutil.get_terminal_size((62, 24))
    except OSError:
        pass

    if not args.follow:
        eng.stream.poll()
        eng.stream.rewind(eng.window)
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
