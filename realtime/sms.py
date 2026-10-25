#!/usr/bin/env python3
"""문자 알림 — 실시간 판단이 도는 동안 조건을 지켜보다 맞으면 SMS 를 보낸다.

설계 정본은 `doc/realtime-inference.md` §10. 요약:

* **판단 엔진 안**에서 돈다(`Engine.step()` → `Watcher.check()`). 제어판이 상태 파일을 보고
  검사하면 브라우저를 닫는 순간 감시가 멈춘다. 그건 알림이 아니다.
* 규칙은 둘뿐 — `hold`(N분 내리 계속) / `absent`(N분 동안 한 번도 안 나옴).
  "나타나면" 은 `hold(X, 0)` 이다.
* `suspended` 동안은 아무것도 관측하지 못하므로 `absent` 시계를 되돌린다(§10.3).
* 올라가는 모서리에서 한 번만 보낸다. 안 그러면 `stride` 마다(0.3초) 한 통씩 나간다.

설정과 기록은 `mac_collector/` 아래 **저장소에 안 들어가는** 파일 둘이다(`.gitignore`).
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from csi_store import LABELS  # noqa: E402

STORE = ROOT / "mac_collector" / "solapi.json"
LOG = ROOT / "mac_collector" / "sms_log.jsonl"

#: 규칙을 걸 수 있는 상태. `deciding` 은 시작 직후 잠깐이라 뺀다.
SUSPENDED = "suspended"
STATES = (*LABELS, SUSPENDED)
LABEL = {"empty": "빈 방", "static": "정지", "motion": "움직임", SUSPENDED: "판단 정지"}
KINDS = ("hold", "absent")

DEFAULT: dict = {"enabled": False, "dry_run": False, "daily_cap": 20,
                 "api_key": "", "api_secret": "", "from": "", "to": [], "rules": []}
#: 알림을 처음 켤 때 넣어 두는 규칙. 이것이 없으면 보드가 죽어도 화면이 조용하고,
#: 조용한 것이 정상으로 보인다(§10.3).
WATCHDOG: dict = {"id": "suspended", "on": True, "kind": "hold",
                  "state": SUSPENDED, "minutes": 10, "repeat_min": 0}


# ── 설정 ────────────────────────────────────────────────────────────────────
def load(path: Path | None = None) -> dict:
    """저장된 설정. 파일이 없거나 깨졌으면 기본값 — 알림은 꺼진 채로 시작한다."""
    try:
        cfg = json.loads(Path(STORE if path is None else path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT)
    return {**DEFAULT, **cfg} if isinstance(cfg, dict) else dict(DEFAULT)


def save(cfg: dict, path: Path | None = None) -> None:
    p = Path(STORE if path is None else path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digits(number: str) -> str:
    """`010-1234-5678` → `01012341234`. SOLAPI 는 하이픈을 받지 않는다."""
    return re.sub(r"\D", "", str(number))


def rule_text(rule: dict) -> str:
    """규칙 한 줄을 사람 말로. 문자 내용과 화면이 같은 문장을 쓴다."""
    who, m = LABEL.get(rule.get("state"), rule.get("state", "?")), int(rule.get("minutes", 0))
    if rule.get("kind") == "absent":
        return f"{who} 없음 {m}분"
    return f"{who} {m}분" if m else f"{who} 발생"


def ready(cfg: dict) -> str | None:
    """못 보내는 이유, 보낼 수 있으면 None. 화면과 기록이 같은 문장을 쓴다."""
    if not cfg.get("api_key") or not cfg.get("api_secret"):
        return "API 키가 없습니다"
    if not digits(cfg.get("from", "")):
        return "발신 번호가 없습니다"
    if not [n for n in cfg.get("to", []) if digits(n)]:
        return "받는 번호가 없습니다"
    try:
        import solapi  # noqa: F401
    except ImportError:
        return "solapi 가 설치되지 않았습니다 — pip install solapi"
    return None


# ── 발송 ────────────────────────────────────────────────────────────────────
def send(cfg: dict, text: str) -> tuple[bool, str]:
    """한 통 보낸다. 돌려주는 값은 (성공, 기록에 남길 문장)."""
    why = ready(cfg)
    if why:
        return False, f"error: {why}"
    try:
        from solapi import SolapiMessageService
        from solapi.model import RequestMessage

        svc = SolapiMessageService(api_key=cfg["api_key"], api_secret=cfg["api_secret"])
        to = [digits(n) for n in cfg["to"] if digits(n)]
        res = svc.send(RequestMessage(from_=digits(cfg["from"]), to=to, text=text))
        c = res.group_info.count
        return c.registered_failed == 0, f"sent {c.registered_success}/{c.total}"
    except Exception as e:                       # 네트워크·인증·발신번호 미등록이 전부 여기로 온다
        return False, f"error: {e}"


# ── 기록 ────────────────────────────────────────────────────────────────────
def log_rows(path: Path | None = None, limit: int = 20) -> list[dict]:
    try:
        lines = Path(LOG if path is None else path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[::-1]                              # 최근이 위


def sent_today(path: Path | None = None, now: float | None = None) -> int:
    """오늘 **실제로 나간** 통수. 상한을 세려면 실행을 넘어 남아야 해서 파일을 센다."""
    day = time.strftime("%Y-%m-%d", time.localtime(now if now is not None else time.time()))
    return sum(1 for r in log_rows(path, limit=10_000)
               if r.get("status") == "sent"
               and time.strftime("%Y-%m-%d", time.localtime(r.get("wall", 0))) == day)


def append_log(row: dict, path: Path | None = None) -> None:
    p = Path(LOG if path is None else path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False) + "\n")


def message(rule: dict, now: float) -> str:
    """단문 90바이트(한글 45자) 안에 들어가야 해서 사람이 쓰지 않는다(§10.6)."""
    return f"[MeshSense] {rule_text(rule)}\n{time.strftime('%m/%d %H:%M', time.localtime(now))}"


# ── 감시 ────────────────────────────────────────────────────────────────────
class Watcher:
    """상태를 받아 조건을 재고, 맞는 순간 한 번 보낸다.

    설정 파일에서 읽은 경우 **판단마다 다시 읽는다.** 측정 중에 `알림 끄기` 를 눌렀는데도
    계속 문자가 가면 끌 방법이 없다. 엔진은 이미 판단마다 상태 파일을 쓰므로 작은 읽기
    하나가 더 느는 것뿐이다. `cfg` 를 직접 넘기면(테스트) 그것을 그대로 쓴다.
    """

    def __init__(self, cfg: dict | None = None, session: str = "",
                 now: float | None = None, log_path: Path | None = None,
                 cfg_path: Path | None = None) -> None:
        self._reload = cfg is None
        self.cfg_path = cfg_path
        self.cfg = load(cfg_path) if cfg is None else cfg
        self.session = session
        self.log_path = log_path
        now = time.time() if now is None else now
        self.state, self.since = "", now
        #: 각 상태를 마지막으로 본 시각. 감시를 시작한 순간부터 센다.
        self.seen = {s: now for s in STATES}
        self._rules: dict[str, dict] = {}

    def _rule_state(self, rule: dict) -> dict:
        return self._rules.setdefault(str(rule.get("id", "")), {"armed": True, "fired": 0.0})

    def _met(self, rule: dict, state: str, now: float) -> bool:
        limit = float(rule.get("minutes", 0)) * 60
        if rule.get("kind") == "absent":
            return now - self.seen.get(rule.get("state"), now) >= limit
        return state == rule.get("state") and now - self.since >= limit

    def check(self, state: str, now: float | None = None) -> list[dict]:
        """판단 한 번마다 부른다. 돌려주는 값은 이번에 남긴 기록 줄들(보통 빈 목록)."""
        now = time.time() if now is None else now
        if self._reload:
            self.cfg = load(self.cfg_path)
        if state != self.state:
            self.state, self.since = state, now
        if state == SUSPENDED:
            # 끊긴 동안은 아무것도 관측하지 못했다. 그 구간을 "없었다" 로 세면 보드가 죽은 날
            # `absent` 가 울려 사람에게 문제가 생겼다고 말하게 된다 (§10.3).
            self.seen = dict.fromkeys(STATES, now)
        elif state in self.seen:
            self.seen[state] = now

        rows = []
        if not self.cfg.get("enabled"):
            return rows
        for rule in self.cfg.get("rules", []):
            if not rule.get("on"):
                continue
            st = self._rule_state(rule)
            if not self._met(rule, state, now):
                st["armed"] = True                # 조건이 깨졌다 → 다시 무장
                continue
            repeat = float(rule.get("repeat_min", 0)) * 60
            if not st["armed"] and not (repeat and now - st["fired"] >= repeat):
                continue
            st["armed"], st["fired"] = False, now
            rows.append(self._fire(rule, now))
        return rows

    def _fire(self, rule: dict, now: float) -> dict:
        text = message(rule, now)
        to = [digits(n) for n in self.cfg.get("to", []) if digits(n)]
        if self.cfg.get("dry_run"):
            ok, status = True, "dry"
        elif sent_today(self.log_path, now) >= int(self.cfg.get("daily_cap", 20)):
            ok, status = False, "capped"
        else:
            ok, why = send(self.cfg, text)
            status = why if why.startswith("error") else "sent"
        row = {"wall": round(now, 2), "rule": rule_text(rule), "text": text,
               "to": to, "ok": ok, "status": status, "session": self.session}
        append_log(row, self.log_path)
        return row


if __name__ == "__main__":        # 셀프체크: 실제 설정 파일로 상태만 본다
    cfg = load()
    why = ready(cfg)
    print(f"알림 {'켜짐' if cfg['enabled'] else '꺼짐'} · 규칙 {len(cfg['rules'])}개 "
          f"· 오늘 {sent_today()}통 / 상한 {cfg['daily_cap']}")
    print(f"발송 가능: {'예' if why is None else why}")
    for r in cfg["rules"]:
        print(f"  [{'켜짐' if r.get('on') else '꺼짐'}] {rule_text(r)}")
