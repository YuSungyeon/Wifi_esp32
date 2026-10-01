#!/usr/bin/env python3
"""상태 기계 — 3-class 모델 확률을 화면에 띄울 상태로 바꾼다.

모델은 30초 창마다 독립적으로 판단한다. 그대로 띄우면 경계에서 깜빡이고, 확신 없는 판단도
그대로 나간다. 여기서 두 가지를 건다.

1. **확신 기준** — 최대 확률이 기준 미만이면 상태를 바꾸지 않고 `deciding` 으로 둔다.
   교차 검증에서 틀린 판단은 대부분 동점에 가까웠다. 틀린 답보다 "모르겠다"가 낫다.
2. **유지 시간** — 새 후보가 일정 시간 이어져야 상태를 바꾼다. 움직임은 짧게(빨리 반응),
   빈 방↔정지는 길게(오탐 억제). 사람이 들어오고 나가려면 반드시 움직여야 하므로
   움직임을 거치지 않는 빈 방↔정지 전환은 특히 더 오래 요구한다.
"""
from __future__ import annotations

from dataclasses import dataclass

CLASSES = ("empty", "static", "motion")
DECIDING = "deciding"
SUSPENDED = "suspended"


@dataclass(frozen=True)
class Reading:
    t: float                 # 초 단위 단조 시각
    probs: tuple             # (empty, static, motion)
    n_rx: int                # 이 창에서 살아 있던 RX 수


@dataclass(frozen=True)
class Event:
    t: float
    frm: str
    to: str
    confidence: float


class StateMachine:
    def __init__(self, conf: float = 0.7, degraded_conf: float = 0.85,
                 motion_hold_s: float = 1.0, rest_hold_s: float = 15.0,
                 direct_switch_s: float = 60.0) -> None:
        self.conf, self.degraded_conf = conf, degraded_conf
        self.motion_hold_s, self.rest_hold_s = motion_hold_s, rest_hold_s
        self.direct_switch_s = direct_switch_s
        self.state, self.since, self.confidence, self.note = DECIDING, 0.0, 0.0, ""
        self.probs = (0.0, 0.0, 0.0)
        self._cand: str | None = None      # 지금 쌓고 있는 후보
        self._cand_from = 0.0              # 그 후보가 시작된 시각
        self._t = 0.0
        self._degraded = False
        self._last_decided = ""

    # ── 조회 ────────────────────────────────────────────────────────────────
    @property
    def threshold(self) -> float:
        return self.degraded_conf if self._degraded else self.conf

    @property
    def held_s(self) -> float:
        return max(0.0, self._t - self.since)

    @property
    def pending(self) -> tuple:
        """(후보, 남은 유지 시간). 후보가 없으면 (None, 0)."""
        if self._cand is None:
            return None, 0.0
        need = self._hold_for(self._cand)
        return self._cand, max(0.0, need - (self._t - self._cand_from))

    def _hold_for(self, candidate: str) -> float:
        if candidate == "motion":
            return self.motion_hold_s
        # 움직임을 거치지 않은 빈 방↔정지 전환은 물리적으로 드물다 — 더 오래 본다
        if self.state in ("empty", "static") and candidate in ("empty", "static"):
            return self.direct_switch_s
        return self.rest_hold_s

    # ── 본체 ────────────────────────────────────────────────────────────────
    def update(self, r: Reading) -> Event | None:
        self._t = r.t
        self.probs = tuple(r.probs)
        self._degraded = r.n_rx == 2
        self.note = ""

        if r.n_rx <= 1:
            self._cand = None
            self.note = f"RX {r.n_rx}대 — 판단 정지. 마지막 판단 {self._last_decided or '없음'}"
            return self._to(r.t, SUSPENDED, 0.0) if self.state != SUSPENDED else None
        if self._degraded:
            self.note = f"RX 2대 — 확신 {self.degraded_conf:.0%} 이상일 때만 판단합니다"

        top = int(max(range(3), key=lambda i: r.probs[i]))
        candidate, p = CLASSES[top], float(r.probs[top])
        if p < self.threshold:
            self._cand = None                       # 확신이 없으면 후보를 쌓지 않는다
            if self.state in (DECIDING, SUSPENDED):
                self.confidence = p
            return None
        if candidate == self.state:
            self._cand, self.confidence = None, p
            return None
        if self._cand != candidate:
            self._cand, self._cand_from = candidate, r.t
        if r.t - self._cand_from >= self._hold_for(candidate):
            return self._to(r.t, candidate, p)
        return None

    def _to(self, t: float, new: str, confidence: float) -> Event:
        ev = Event(t=t, frm=self.state, to=new, confidence=confidence)
        self.state, self.since, self.confidence = new, t, confidence
        self._cand = None
        if new in CLASSES:
            self._last_decided = new
        return ev
