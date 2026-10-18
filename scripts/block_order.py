#!/usr/bin/env python3
"""블록 순서와 그 **진행 상태** — 제어판과 CLI 가 같은 블록을 본다.

수집 프로토콜의 블록은 `{empty, static, motion}` 을 매번 무작위 순서로 한 번씩 찍는 묶음이다
(`doc/collection-protocol.md` §4). 지금까지 순서는 제어판의 `localStorage` 와 CLI 의 메모리에
따로 있었고 **어디까지 찍었는지는 사람이 기억**했다.

진행 상태를 서버 파일 하나(`mac_collector/block_order.json`)에 둔다. 브라우저를 새로 고쳐도
남고, CLI 로 찍은 세션도 제어판에 반영된다. 이 상태가 수집 버튼의 라벨을 정한다
(`doc/control-panel.md` §3.2).

registry 와 같은 자리에 두지만 **실험 데이터가 아니다** — 지워도 다음에 새로 뽑으면 된다.
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from csi_store import LABELS  # noqa: E402

STORE = ROOT / "mac_collector" / "block_order.json"
EMPTY: dict = {"order": [], "done": [], "drawn_at": "", "date": "", "placement": "", "subject": ""}


def load(path: Path = STORE) -> dict:
    """저장된 상태. 파일이 없거나 깨졌으면 빈 상태 — 수집 화면이 떠야 새로 뽑을 수 있다."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(EMPTY)
    return {**EMPTY, **state} if isinstance(state, dict) else dict(EMPTY)


def save(state: dict, path: Path = STORE) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def draw(prev: list | None, labels=LABELS, rng=None) -> list:
    """블록 안 순서를 무작위로. 직전 블록과 같은 순서는 다시 뽑는다.

    같은 순서가 이어지면 순서 자체가 클래스와 상관되어 시간에 따른 채널 변화가 라벨과 얽힌다
    (`doc/collection-protocol.md` §4).
    """
    rng = rng or random
    order = list(labels)
    if len(order) < 2:
        return order
    while True:
        rng.shuffle(order)
        if order != list(prev or []):
            return order


def next_label(state: dict) -> str | None:
    """아직 안 찍은 첫 라벨. 블록이 끝났으면 None."""
    done = state.get("done", [])
    return next((l for l in state.get("order", []) if l not in done), None)


def advance(state: dict, label: str, level: str) -> dict:
    """수집 한 번의 결과를 반영한다. `level` 은 `csi_session.quality()` 의 판정이다.

    `recollect` 는 칸을 **되돌린다** — 품질 문장이 이미 "같은 라벨로 다시 찍으세요" 라고
    말하므로 화면도 같은 말을 해야 한다. `check` 는 학습에 쓸 수 있으므로 끝난 것으로 센다.
    """
    done = [l for l in state.get("done", []) if l != label]
    if level != "recollect":
        done.append(label)
    return {**state, "done": done}


def redraw_reason(state: dict, date: str, placement: str, subject: str) -> str | None:
    """새 블록을 뽑아야 하면 사유 문장, 아니면 None.

    조건 = (날짜, 보드 배치, 피험자) 다 (`doc/collection-protocol.md` §4). 조건이 바뀌면
    블록도 새로 시작한다 — 한 블록이 두 조건에 걸치면 조건 교차가 깨진다.
    """
    if not state.get("order"):
        return "블록이 없어 새로 뽑았습니다"
    if next_label(state) is None:
        return "블록을 마쳐 새로 뽑았습니다"
    for was, now, what in ((state.get("date"), date, "날짜"),
                           (state.get("placement"), placement, "배치"),
                           (state.get("subject"), subject, "피험자")):
        if was and was != now:
            return f"{what}가 바뀌어 새 블록을 뽑았습니다"
    return None


def start(date: str, placement: str, subject: str, prev: list | None = None,
          labels=LABELS) -> dict:
    """새 블록 하나. 조건을 함께 적어 두어야 `redraw_reason` 이 바뀜을 알아챈다."""
    return {"order": draw(prev, labels), "done": [],
            "drawn_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "date": date, "placement": placement, "subject": subject}


if __name__ == "__main__":  # 셀프체크: 실제 저장 파일로 규칙이 도는지
    s = load()
    print(f"순서 {s['order'] or '없음'} · 끝남 {s['done']} · 다음 {next_label(s) or '—'}")
    print(f"저장 위치: {STORE.relative_to(ROOT)}")
