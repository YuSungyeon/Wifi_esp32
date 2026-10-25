#!/usr/bin/env python3
"""집 상태 서비스 — 로컬(조명)과 클라우드(알림)가 함께 쓰는 시간 규칙.

설계 정본은 `doc/plans/2026-10-23-home-service.md` §3. 요약:

* 모든 시각은 `Asia/Seoul` 이다. EC2 기본 시간대(UTC)를 그대로 쓰면 9시간 어긋난다.
* 수면 시간은 `HH:MM–HH:MM` 이고 자정을 넘을 수 있다. 시작 = 끝이면 수면 시간이 없다.
* 응급 시계는 수면 동안 멈췄다가 **수면이 끝난 시각부터 다시** 센다 — 23:50 에 쓰러져도
  07:00 + N분에 알림이 간다(아침 안부 확인).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Seoul")

#: 웹에서 편집하고 로컬로 내려가는 설정(§3.4). 계정마다 하나.
DEFAULT: dict = {"sleep_start": "00:00", "sleep_end": "07:00",
                 "motion_wake_s": 5, "sleep_off_min": 10,
                 "static_alert_min": 60, "confirm_min": 5,
                 "away": False, "contacts": []}


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_sleep(now: float, cfg: dict) -> bool:
    a, b = _minutes(cfg["sleep_start"]), _minutes(cfg["sleep_end"])
    t = datetime.fromtimestamp(now, TZ)
    cur = t.hour * 60 + t.minute
    if a == b:
        return False
    return a <= cur < b if a < b else (cur >= a or cur < b)


def last_boundary(now: float, hhmm: str) -> float:
    """`now` 이전(같으면 포함)에 시계가 마지막으로 `hhmm` 를 가리킨 시각."""
    t = datetime.fromtimestamp(now, TZ)
    h, m = hhmm.split(":")
    b = t.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    if b > t:
        b -= timedelta(days=1)
    return b.timestamp()


def emergency_held(now: float, since: float, cfg: dict) -> float:
    """응급 판단에 쓰는 유지 시간(초). 수면 중이면 0 — 시계가 멈춰 있다."""
    if cfg["sleep_start"] == cfg["sleep_end"]:
        return now - since
    if in_sleep(now, cfg):
        return 0.0
    return now - max(since, last_boundary(now, cfg["sleep_end"]))
