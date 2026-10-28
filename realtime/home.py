#!/usr/bin/env python3
"""집 상태 서비스 — 로컬(조명)과 클라우드(알림)가 함께 쓰는 시간 규칙.

설계 정본은 `doc/plans/2026-10-23-home-service.md` §3. 요약:

* 모든 시각은 `Asia/Seoul` 이다. EC2 기본 시간대(UTC)를 그대로 쓰면 9시간 어긋난다.
* 수면 시간은 `HH:MM–HH:MM` 이고 자정을 넘을 수 있다. 시작 = 끝이면 수면 시간이 없다.
* 응급 시계는 수면 동안 멈췄다가 **수면이 끝난 시각부터 다시** 센다 — 23:50 에 쓰러져도
  07:00 + N분에 알림이 간다(아침 안부 확인).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]

#: 클라우드 주소 — 사용자가 고르는 값이 아니라 배포 때 한 번 정하는 값이라 화면에 없다(§4.3).
#: `MESHSENSE_CLOUD` 는 로컬 시험용으로만 덮는다.
CLOUD_URL = os.environ.get("MESHSENSE_CLOUD", "https://api.meshsense.cloud")   # 계획 §4.4
#: 연동 정보 `{url, token, name, config, paired}` — 토큰이 들어 있다(`.gitignore`).
STORE = ROOT / "mac_collector" / "home_service.json"
HEARTBEAT_S = 60

#: 웹에서 편집하고 로컬로 내려가는 설정(§3.4). 계정마다 하나.
DEFAULT: dict = {"sleep_start": "00:00", "sleep_end": "07:00",
                 "motion_wake_s": 5, "sleep_off_min": 10,
                 "static_alert_min": 60, "confirm_min": 5, "emergency": True, "sleep": True,
                 "away": False, "away_trigger": "motion", "away_repeat_min": 30, "away_notify": "me",
                 "contacts": [], "rules": []}


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_window(now: float, start: str, end: str) -> bool:
    """`start` ≤ 지금 < `end` (자정 넘김 가능). 시작 = 끝이면 빈 창."""
    a, b = _minutes(start), _minutes(end)
    t = datetime.fromtimestamp(now, TZ)
    cur = t.hour * 60 + t.minute
    if a == b:
        return False
    return a <= cur < b if a < b else (cur >= a or cur < b)


def no_sleep(cfg: dict) -> bool:
    """수면 시간이 없다 = 늘 깨어 있다 — 설정에서 껐거나(`sleep`) 시작 = 끝 (§3.1)."""
    return not cfg.get("sleep", True) or cfg["sleep_start"] == cfg["sleep_end"]


def in_sleep(now: float, cfg: dict) -> bool:
    return not no_sleep(cfg) and in_window(now, cfg["sleep_start"], cfg["sleep_end"])


def window_start(now: float, when: str, cfg: dict, start: str = "", end: str = "") -> float | None:
    """지금 들어 있는 시간대가 시작된 시각. 시간대 밖이면 None.

    `when`: `always` · `awake`(수면 끝 → 수면 시작) · `sleep` · `custom`(`start`–`end`).
    """
    if when == "always":
        return float("-inf")
    if when == "custom":
        a, b = start, end
    elif no_sleep(cfg):
        return float("-inf") if when == "awake" else None
    elif when == "sleep":
        a, b = cfg["sleep_start"], cfg["sleep_end"]
    else:
        a, b = cfg["sleep_end"], cfg["sleep_start"]
    return last_boundary(now, a) if in_window(now, a, b) else None


def last_boundary(now: float, hhmm: str) -> float:
    """`now` 이전(같으면 포함)에 시계가 마지막으로 `hhmm` 를 가리킨 시각."""
    t = datetime.fromtimestamp(now, TZ)
    h, m = hhmm.split(":")
    b = t.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    if b > t:
        b -= timedelta(days=1)
    return b.timestamp()


def led(state: str, held_s: float, now: float, cfg: dict, cur: bool | None) -> bool | None:
    """조명을 켤지(§3.2). `cur` 는 지금 조명 — 판단을 못 하면 그대로 둔다. None 이면 아직 모른다.

    수면 중에는 뒤척임(짧은 motion)에 켜지지 않고, 가만히 X분이면 끈다. X분은 수면 시작부터 센다.
    """
    if state == "empty":
        return False
    if state not in ("static", "motion"):          # deciding · suspended — 모르면 건드리지 않는다
        return cur
    ws = window_start(now, "sleep", cfg)
    if ws is None:
        return True
    if state == "motion":
        return True if held_s >= cfg["motion_wake_s"] else cur
    return False if now - max(now - held_s, ws) >= cfg["sleep_off_min"] * 60 else cur


def emergency_held(now: float, since: float, cfg: dict) -> float:
    """응급 판단에 쓰는 유지 시간(초). 수면 중이면 0 — 시계가 멈춰 있다."""
    ws = window_start(now, "awake", cfg)
    return 0.0 if ws is None else now - max(since, ws)


# ── 클라우드 연동 (§4.3) ───────────────────────────────────────────────────
def load(path: Path | None = None) -> dict:
    try:
        d = json.loads(Path(path or STORE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save(d: dict, path: Path | None = None) -> None:
    p = Path(path or STORE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    p.chmod(0o600)


def _call(url: str, path: str, body: dict, token: str | None = None) -> tuple[int, dict]:
    """(HTTP 상태, 응답). 서버에 닿지 않으면 상태 0."""
    req = urllib.request.Request(url.rstrip("/") + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json",
                                  **({"Authorization": f"Bearer {token}"} if token else {})})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}
    except (OSError, ValueError):
        return 0, {"error": "서버에 닿지 않습니다"}


def pair(code: str, name: str, path: Path | None = None) -> str | None:
    """연동 코드로 이 컴퓨터를 계정에 붙인다. 실패하면 사유 문장, 성공하면 None."""
    code, name = re.sub(r"\s", "", str(code)), (str(name).strip() or "집 기기")[:40]
    if not re.fullmatch(r"\d{6}", code):
        return "6자리 숫자를 넣어 주세요"
    st, res = _call(CLOUD_URL, "/api/pair", {"code": code, "name": name})
    if st != 200 or not res.get("token"):
        return res.get("error") or f"연결하지 못했습니다 (HTTP {st})"
    # 주소를 함께 적는다 — 나중에 CLOUD_URL 이 바뀌어도 토큰을 엉뚱한 서버로 보내지 않는다
    save({"url": CLOUD_URL, "token": res["token"], "name": name, "config": res.get("config", {}),
          "paired": time.time()}, path)
    return None


def unpair(path: Path | None = None) -> None:
    Path(path or STORE).unlink(missing_ok=True)


class Sender:
    """판단 엔진 안에서 상태를 클라우드로 보낸다 — 바뀌면 바로, 아니면 60초마다.

    HTTP 는 이 스레드가 맡는다. 엔진은 `offer()` 로 최신 상태만 놓고 간다(0.3초 판단을 막지 않는다).
    보낼 때마다 연동 정보를 다시 읽는다 — 실행 중에 연결·해제해도 따라간다.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self.last: dict = {}                 # 마지막 전송 결과 — 제어판 연동 탭이 보여 준다
        self._want: dict | None = None
        self._dirty = False
        self._cv = threading.Condition()
        threading.Thread(target=self._run, daemon=True).start()

    def offer(self, state: str, since: float, led: bool | None = None, rx: dict | None = None) -> None:
        """`rx` = RX 별 생존 `{"101": True, ...}` — 웹 홈의 보드 그림이 쓴다. 상태처럼 바뀌면 바로 보낸다."""
        with self._cv:
            if self._want is None or self._want["state"] != state or self._want.get("rx") != rx:
                self._dirty = True
                self._cv.notify()
            self._want = {"state": state, "since": round(since, 1), "led": led, "rx": rx}

    def _run(self) -> None:
        while True:
            with self._cv:
                self._cv.wait_for(lambda: self._dirty, timeout=HEARTBEAT_S)
                self._dirty = False
                body = dict(self._want) if self._want else None
            if body:
                self.send(body)

    def send(self, body: dict) -> None:
        st = load(self.path)
        if not st.get("token"):
            self.last = {}
            return
        code, res = _call(st["url"], "/api/state", body, st["token"])
        self.last = {"at": time.time(), "ok": code == 200, "state": body["state"],
                     "error": None if code == 200 else
                     ("홈페이지에서 해제된 기기입니다" if code == 401 else res.get("error") or f"HTTP {code}")}
        if code == 200 and res.get("config") and res["config"] != st.get("config"):
            st["config"] = res["config"]
            save(st, self.path)
