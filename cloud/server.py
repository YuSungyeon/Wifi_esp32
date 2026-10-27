#!/usr/bin/env python3
"""집 상태 서비스 클라우드 — 상태 기록, 알림 판단, 웹 화면.

설계 정본은 `doc/plans/2026-10-23-home-service.md` §4~5. 요약:

* 집 Mac 은 상태가 바뀔 때와 60초마다 `POST /api/state` 를 보낸다. 그 요청이 곧 생존 신호이고,
  응답에 설정을 실어 돌려준다. 조명은 집에서 판단한다 — 여기서는 보여 주기만 한다.
* 알림(응급·침입·끊김)은 **여기서** 판단한다. 집 시스템이 죽은 것은 집 밖에서만 알 수 있다.
* 표준 라이브러리만 쓴다(`http.server` + `sqlite3`). HTTPS 는 앞단 Caddy 가 맡고 이 서버는
  127.0.0.1 에만 붙는다.

    python3 cloud/server.py --base https://meshsense.cloud     # 문자 링크는 프론트 주소 (계획 §4.4)
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import smtplib
import sqlite3
import sys
import threading
import time
import traceback
from collections import deque
from email.message import EmailMessage
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime import home  # noqa: E402

HERE = Path(__file__).resolve().parent
STATES = ("empty", "static", "motion", "deciding", "suspended")
OFFLINE_S = 180               # 마지막 수신 후 이만큼 지나면 끊김
CODE_TTL_S = 600              # 연동 코드 10분
SESSION_S = 30 * 86400
TICK_S = 30                   # 알림 검사 주기
DAILY_CAP = 40                # 하루 문자 상한 — 서비스 전체, 받는 번호 하나가 1건 (계획 §3.3)
MAIL_DAILY_CAP = 100          # 하루 인증 메일 상한 — 서비스 전체 (계획 §3.6)
VERIFY_TTL_S = 600            # 인증 코드 10분
VERIFY_TRIES = 5              # 이만큼 틀리면 코드 무효
VERIFY_RESEND_S = 60          # 다시 보내기 간격
UNVERIFIED_TTL_S = 86400      # 인증하지 않은 계정은 하루 뒤 지운다

LABEL = {"empty": "빈 방", "static": "정지", "motion": "움직임", "deciding": "판단 중",
         "suspended": "판단 정지", "offline": "끊김"}
DESC = {"empty": "방에 아무도 없어요", "static": "사람이 있고 가만히 있어요",
        "motion": "사람이 움직이고 있어요", "deciding": "상태를 판단하는 중이에요",
        "suspended": "수신기가 모자라 판단을 멈췄어요", "offline": "집 기기 신호가 끊겼어요"}
CLS = {"empty": "e", "static": "s", "motion": "m", "deciding": "d", "suspended": "x", "offline": "x"}
KIND = {"emergency": "움직임 없음", "intrusion": "외출 중 움직임", "offline": "집 기기 연결 끊김",
        "rule": "나만의 상황"}
WHEN = {"always": "항상", "awake": "깨어 있는 시간", "sleep": "수면 시간", "custom": "직접 지정"}
RULE_STATES = {"empty": "빈 방이", "static": "정지가", "motion": "움직임이"}   # 조사까지 붙여 둔다
RULE_KINDS = {"hold": "계속되면", "absent": "한 번도 없으면"}
NOTIFY = {"me": "나에게", "escalate": "나에게, 확인 없으면 지인에게", "all": "나와 지인에게 바로"}
MAX_RULES = 10
#: 예시로 시작 (§3.5) — 버튼 이름, 편집기에 미리 채울 값
PRESETS = {
    "night": ("밤에 집에 안 들어옴", {"when": "sleep", "state": "empty", "kind": "hold", "minutes": 30, "notify": "me"}),
    "away": ("낮에 너무 오래 비어 있음", {"when": "awake", "state": "empty", "kind": "hold", "minutes": 240, "notify": "me"}),
    "wake": ("아침까지 안 일어남", {"when": "custom", "start": "07:00", "end": "10:00", "state": "motion",
                                "kind": "absent", "minutes": 120, "notify": "escalate"}),
}
NEW_RULE = {"when": "awake", "start": "22:00", "end": "06:00", "state": "empty", "kind": "hold",
            "minutes": 60, "notify": "me"}


def span(m: int) -> str:
    return f"{m}분" if m < 60 else f"{m // 60}시간" + (f" {m % 60}분" if m % 60 else "")


def rule_text(r: dict) -> str:
    """상황 한 줄을 사람 말로. 목록·문자·확인 페이지가 같은 문장을 쓴다."""
    when = {"always": "", "custom": f"{r.get('start')}–{r.get('end')}에 "}.get(r["when"], WHEN[r["when"]] + "에 ")
    m = int(r["minutes"])
    if r["kind"] == "hold":
        return f"{when}{RULE_STATES[r['state']]} " + (f"{span(m)} 계속되면" if m else "나타나면")
    return f"{when}{RULE_STATES[r['state']]} {span(m)} 없으면"

BASE = "http://127.0.0.1:8000"   # 문자에 넣는 확인 링크의 앞부분 (--base)
SECRET = b""
CONN: sqlite3.Connection
LOCK = threading.Lock()

SCHEMA = """
create table if not exists users(id integer primary key, email text unique not null, name text not null,
  phone text not null, pw_hash text not null, salt text not null, config text not null, created real not null);
create table if not exists devices(id integer primary key, user_id integer not null, name text not null,
  token_hash text unique not null, state text, since real, led integer, last_seen real, created real not null);
create table if not exists pair_codes(code text primary key, user_id integer not null, expires real not null);
create table if not exists events(id integer primary key, device_id integer not null, state text not null,
  since real not null, received real not null);
create index if not exists events_since on events(device_id, since);
create table if not exists sms_daily(day text primary key, n integer not null);
create table if not exists email_codes(user_id integer primary key, code_hash text not null, expires real not null,
  tries integer not null, sent real not null);
create table if not exists alerts(id integer primary key, device_id integer not null, kind text not null,
  rule text, label text, token text unique, created real not null, confirmed real, escalated real, resolved real, status text);
"""


# ── 저장소 ──────────────────────────────────────────────────────────────────
def init(db: Path, base: str, secret: bytes | None = None) -> None:
    global CONN, BASE, SECRET
    CONN = sqlite3.connect(db, check_same_thread=False)
    CONN.row_factory = sqlite3.Row
    CONN.executescript(SCHEMA)
    if "verified" not in [r["name"] for r in CONN.execute("pragma table_info(users)")]:
        # 메일 인증(§3.6) 이전에 가입한 계정은 인증된 것으로 친다
        CONN.execute("alter table users add column verified real")
        CONN.execute("update users set verified=created")
        CONN.commit()
    BASE = base.rstrip("/")
    SECRET = secret or _secret()


def _secret() -> bytes:
    """세션 서명 키. 환경 변수가 없으면 처음 실행 때 만들어 파일로 둔다(`.gitignore`)."""
    if os.environ.get("MESHSENSE_SECRET"):
        return os.environ["MESHSENSE_SECRET"].encode()
    p = HERE / "secret"
    if not p.exists():
        p.write_text(secrets.token_hex(32))
        p.chmod(0o600)
    return p.read_text().strip().encode()


def q(sql: str, args: tuple = (), one: bool = False):
    # ponytail: 연결 하나 + 전역 잠금. 시연 규모(집 한두 곳)에선 충분하고, 커지면 요청마다 연결.
    with LOCK:
        cur = CONN.execute(sql, args)
        rows = cur.fetchall()
        CONN.commit()
        if sql.lstrip().lower().startswith("insert"):
            return cur.lastrowid
    return (rows[0] if rows else None) if one else rows


def config(user) -> dict:
    return {**home.DEFAULT, **json.loads(user["config"])}


def save_config(uid: int, cfg: dict) -> None:
    q("update users set config=? where id=?", (json.dumps(cfg, ensure_ascii=False), uid))


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def hash_pw(pw: str, salt: str) -> str:
    return hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()


def device_of(uid: int):
    """한 계정 = 한 집. 기기가 여럿이면 가장 최근에 신호를 보낸 것을 집의 상태로 본다."""
    return q("select * from devices where user_id=? order by coalesce(last_seen,0) desc limit 1",
             (uid,), one=True)


def stale(dev, now: float) -> bool:
    return dev is not None and dev["last_seen"] is not None and now - dev["last_seen"] > OFFLINE_S


# ── 문자 ────────────────────────────────────────────────────────────────────
def digits(number: str) -> str:
    """`010-1234-5678` → `01012345678`. SOLAPI 는 하이픈을 받지 않는다."""
    return re.sub(r"\D", "", str(number))


def send_sms(numbers: list[str], text: str) -> str:
    """보낸 결과 문장(`alerts.status` 에 남는다). 테스트가 이 함수를 바꿔 끼운다 — 실제 번호로 나가면 안 된다.

    SOLAPI 키와 발신 번호는 환경 변수다(EC2 systemd `EnvironmentFile`). PLANNED: 카카오톡 알림으로
    바꿀 때 이 함수만 갈아 끼운다.
    """
    to = [digits(n) for n in numbers if digits(n)]
    if not to:
        return "error: 받는 번호가 없습니다"
    key, secret, sender = (os.environ.get(k, "") for k in ("SOLAPI_API_KEY", "SOLAPI_API_SECRET", "SOLAPI_FROM"))
    if not (key and secret and digits(sender)):
        return "error: SOLAPI 환경 변수(SOLAPI_API_KEY·SOLAPI_API_SECRET·SOLAPI_FROM)가 없습니다"
    try:
        from solapi import SolapiMessageService
        from solapi.model import RequestMessage
    except ImportError:
        return "error: solapi 가 설치되지 않았습니다 — pip install -r cloud/requirements.txt"
    try:
        res = SolapiMessageService(api_key=key, api_secret=secret).send(
            RequestMessage(from_=digits(sender), to=to, text=text))
        c = res.group_info.count
        return f"sent {c.registered_success}/{c.total}" if c.registered_failed == 0 \
            else f"error: 일부 실패 {c.registered_success}/{c.total}"
    except Exception as e:                       # 네트워크·인증·발신번호 미등록이 전부 여기로 온다
        return f"error: {e}"


def send_mail(to: str, subject: str, text: str) -> str:
    """보낸 결과 문장. 테스트가 이 함수를 바꿔 끼운다 — 실제 주소로 나가면 안 된다.

    Gmail(`smtp.gmail.com:465`) + 앱 비밀번호. 계정과 비밀번호는 환경 변수 `SMTP_USER`·`SMTP_PASSWORD`.
    """
    user, password = os.environ.get("SMTP_USER", ""), os.environ.get("SMTP_PASSWORD", "")
    if not (user and password):
        return "error: SMTP 환경 변수(SMTP_USER·SMTP_PASSWORD)가 없습니다"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = f"MeshSense <{user}>", to, subject
    msg.set_content(text)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as smtp:
            smtp.ehlo()
            # login() 은 PLAIN 이 535 로 거절되면 LOGIN 으로 다시 시도하는데, Gmail 은 그때 말없이 끊는다 —
            # 진짜 이유(535)가 "Connection unexpectedly closed" 에 가려진다. PLAIN 하나로 고정한다.
            smtp.user, smtp.password = user, password.replace(" ", "")   # 앱 비밀번호는 띄어 써서 보여 준다
            smtp.auth("PLAIN", smtp.auth_plain)
            smtp.send_message(msg)
        return "sent"
    except smtplib.SMTPAuthenticationError as e:
        return f"error: Gmail 로그인 거절({e.smtp_code}) — SMTP_USER 나 앱 비밀번호가 맞지 않습니다"
    except Exception as e:                       # 네트워크·수신 거부가 여기로 온다
        return f"error: {type(e).__name__}: {e}"


def counted(key: str, n: int, cap: int) -> int | None:
    """오늘 `key` 로 이미 센 수. `n` 을 더하면 `cap` 을 넘으면 None."""
    row = q("select n from sms_daily where day=?", (key,), one=True)
    used = row["n"] if row else 0
    return None if used + n > cap else used


def count(key: str, n: int) -> None:
    q("insert into sms_daily(day,n) values(?,?) on conflict(day) do update set n=n+?", (key, n, n))


def today(now: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if now is None else now, home.TZ).strftime("%Y-%m-%d")


def mail_code(user, now: float) -> str:
    """새 인증 코드를 만들어 보낸다(이전 코드는 무효). 돌려주는 값은 화면에 띄울 문장, 잘 갔으면 빈 문장."""
    key = "mail:" + today(now)
    if counted(key, 1, MAIL_DAILY_CAP) is None:
        return "오늘은 인증 메일을 더 보낼 수 없어요. 내일 다시 시도해 주세요"
    code = f"{secrets.randbelow(10 ** 6):06d}"
    q("insert or replace into email_codes(user_id,code_hash,expires,tries,sent) values(?,?,?,0,?)",
      (user["id"], sha(code), now + VERIFY_TTL_S, now))
    status = send_mail(user["email"], f"[MeshSense] 인증 코드 {code}",
                       f"가입을 마치려면 아래 코드를 화면에 넣어 주세요.\n\n    {code}\n\n"
                       f"10분 동안만 쓸 수 있어요. 직접 가입하지 않았다면 이 메일은 무시하세요 — "
                       f"인증하지 않은 계정은 24시간 뒤 지워져요.\n\nMeshSense · {BASE}")
    if not status.startswith("sent"):
        sys.stderr.write(f"인증 메일 실패: {status}\n")
        q("update email_codes set sent=0 where user_id=?", (user["id"],))   # 못 보냈으면 바로 다시 보낼 수 있게
        return "메일을 보내지 못했어요. 잠시 뒤 다시 보내기를 눌러 주세요"
    count(key, 1)
    return ""


def notify(numbers: list[str], text: str, now: float | None = None) -> str:
    """상한을 세고 보낸다. 모든 발송은 여기를 지난다 — 가입이 열려 있어 계정별 상한은 뚫린다."""
    to = [n for n in numbers if digits(n)]
    day = today(now)
    used = counted(day, len(to), DAILY_CAP)
    if to and used is None:
        used = counted(day, 0, DAILY_CAP)
        return f"error: 하루 상한({DAILY_CAP}건)에 걸려 보내지 않았습니다 — 오늘 {used}건"
    status = send_sms(numbers, text)
    if status.startswith("sent"):
        count(day, len(to))
    return status


def hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, home.TZ).strftime("%H:%M")


def raise_alert(dev, user, kind: str, now: float, text: str, numbers: list[str], label: str = "",
                rule: str | None = None, token: str | None = None, resolved: bool = False) -> None:
    status = notify(numbers, text, now)
    q("insert into alerts(device_id,kind,rule,label,token,created,resolved,status) values(?,?,?,?,?,?,?,?)",
      (dev["id"], kind, rule, label or KIND[kind], token, now, now if resolved else None, status))


def last_end(dev, state: str) -> float | None:
    """그 상태가 마지막으로 **끝난** 시각(다음 이벤트의 시작). 본 적 없으면 None."""
    r = q("select min(since) t from events where device_id=? and since > "
          "(select max(since) from events where device_id=? and state=?)",
          (dev["id"], dev["id"], state), one=True)
    return r["t"] if r else None


def rule_due(dev, r: dict, cfg: dict, now: float) -> bool:
    """나만의 상황 하나가 지금 참인가(§3.5). 시간대가 시작된 시각부터 센다."""
    ws = home.window_start(now, r["when"], cfg, r.get("start", ""), r.get("end", ""))
    if ws is None:
        return False
    need = int(r["minutes"]) * 60
    if r["kind"] == "hold":
        return dev["state"] == r["state"] and now - max(dev["since"], ws) >= need
    if dev["state"] == r["state"]:
        return False
    return now - max(last_end(dev, r["state"]) or dev["created"], ws) >= need


def tick(now: float) -> None:
    """알림 검사 한 번(§3.3·§3.5). 조건이 참이 되는 순간 한 번, 거짓이 되면 닫고 다시 무장한다."""
    for u in q("select id from users where verified is null and created < ?", (now - UNVERIFIED_TTL_S,)):
        drop_user(uid=u["id"])                  # 인증하지 않은 계정은 하루 뒤 지운다 (§3.6)
    for dev in q("select d.* from devices d"):
        user = q("select * from users where id=?", (dev["user_id"],), one=True)
        if user is None or dev["last_seen"] is None:
            continue
        cfg = config(user)
        open_ = {a["rule"] or a["kind"]: a
                 for a in q("select * from alerts where device_id=? and resolved is null", (dev["id"],))}
        gone = stale(dev, now)

        off = open_.pop("offline", None)
        if gone and not off:
            raise_alert(dev, user, "offline", now,
                        f"[MeshSense] 집 기기 신호가 {OFFLINE_S // 60}분째 없습니다 ({hm(now)})",
                        [user["phone"]])
        elif off and not gone:
            q("update alerts set resolved=? where id=?", (now, off["id"]))

        # (열쇠, 참인가, 알릴 사람, 문장) — 긴급 알림도 "나에게, 확인 없으면 지인에게" 인 상황 하나다
        minutes = int(cfg["static_alert_min"])
        conds = [("emergency", not gone and dev["state"] == "static"
                  and home.emergency_held(now, dev["since"], cfg) >= minutes * 60,
                  "escalate", f"{minutes}분째 움직임 없음")]
        conds += [(r["id"], not gone and rule_due(dev, r, cfg, now), r["notify"], rule_text(r))
                  for r in cfg["rules"] if r.get("on")]
        contacts = [c["phone"] for c in cfg["contacts"]]
        for key, due, who, label in conds:
            a = open_.pop(key, None)
            if due and not a:
                token = secrets.token_urlsafe(9) if who == "escalate" else None
                link = f"\n괜찮으면 눌러 주세요 {BASE}/c/{token}" if token else ""
                raise_alert(dev, user, "emergency" if key == "emergency" else "rule", now,
                            f"[MeshSense] {label} ({hm(now)}){link}",
                            [user["phone"]] + (contacts if who == "all" else []),
                            label=label, rule=None if key == "emergency" else key, token=token)
            elif a and not due:
                q("update alerts set resolved=? where id=?", (now, a["id"]))
            elif (a and a["token"] and a["confirmed"] is None and a["escalated"] is None
                  and now - a["created"] >= int(cfg["confirm_min"]) * 60):
                status = notify(contacts, f"[MeshSense] {user['name']}님 방: {label}. "
                                            f"본인 확인이 없어요. 연락해 주세요", now)
                q("update alerts set escalated=?, status=? where id=?",
                  (now, f"{a['status']} / 지인 {status}", a["id"]))
        for a in open_.values():                    # 지웠거나 끈 상황 — 열린 채 두지 않는다
            if a["kind"] != "intrusion":
                q("update alerts set resolved=? where id=?", (now, a["id"]))


def ticker() -> None:
    while True:
        try:
            tick(time.time())
        except Exception:
            traceback.print_exc()
        time.sleep(TICK_S)


# ── 하루 막대 ───────────────────────────────────────────────────────────────
def day_range(date: str) -> tuple[float, float]:
    d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=home.TZ)
    return d.timestamp(), (d + timedelta(days=1)).timestamp()


def segments(uid: int, t0: float, t1: float, now: float) -> list[tuple]:
    """[(시작, 끝, 상태)]. 지금 끊겨 있으면 마지막 수신부터 지금까지를 `offline` 으로 채운다."""
    base = "from events e join devices d on d.id=e.device_id where d.user_id=?"
    prev = q(f"select e.state {base} and e.since<? order by e.since desc limit 1", (uid, t0), one=True)
    rows = q(f"select e.state, e.since {base} and e.since>=? and e.since<? order by e.since", (uid, t0, t1))
    pts = ([(t0, prev["state"])] if prev else []) + [(r["since"], r["state"]) for r in rows]
    end = min(t1, now)
    dev = device_of(uid)
    if stale(dev, now) and dev["last_seen"] < end:
        cut = max(dev["last_seen"], t0)
        pts = [p for p in pts if p[0] < cut] + [(cut, "offline")]
    out = []
    for (a, s), (b, _) in zip(pts, pts[1:] + [(end, None)]):
        if b > a:
            out.append((a, b, s))
    return out


def bar(segs: list[tuple], t0: float, now: float, cfg: dict) -> str:
    pct = lambda x: f"{max(0.0, min(100.0, (x - t0) / 864)):.3f}%"   # noqa: E731 — 86400초 = 100%
    a, b = home._minutes(cfg["sleep_start"]) / 14.4, home._minutes(cfg["sleep_end"]) / 14.4
    spans = [] if a == b else ([(a, b)] if a < b else [(0, b), (a, 100)])
    band = "".join(f'<i style="left:{x:.3f}%;width:{y - x:.3f}%"></i>' for x, y in spans)
    body = "".join(f'<i class="{CLS[s]}" style="left:{pct(x)};width:calc({pct(y)} - {pct(x)})"></i>'
                   for x, y, s in segs)
    mark = f'<div class="now" style="left:{pct(now)}"></div>' if t0 <= now < t0 + 86400 else ""
    return (f'<div class="sleepband">{band}</div><div class="day">{body}{mark}</div>'
            '<div class="ticks"><span>0시</span><span>6</span><span>12</span><span>18</span><span>24</span></div>'
            '<div class="legend"><span class="badge c-e">빈 방</span><span class="badge c-s">정지</span>'
            '<span class="badge c-m">움직임</span><span class="badge c-x">끊김</span>'
            '<span class="badge c-z">수면</span></div>')


def dur(s: float) -> str:
    m = int(s // 60)
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def ago(s: float) -> str:
    s = max(0, int(s))
    return f"{s}초 전" if s < 60 else f"{s // 60}분 전" if s < 3600 else f"{s // 3600}시간 전"


# ── 화면 ────────────────────────────────────────────────────────────────────
e = html.escape

ICONS = {
    "home": '<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"/>',
    "history": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1'
                'M2 12h3M19 12h3M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>',
    "devices": '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
}
NAV = (("home", "홈"), ("history", "기록"), ("settings", "설정"), ("devices", "기기"))


def page(title: str, body: str, user=None, active: str = "", refresh: int | None = None,
         flash: str = "", header: str = "") -> str:
    nav, tabs = header, ""
    if user is not None:
        av = f'<div class="avatar" title="{e(user["name"])}">{e(user["name"][:1])}</div>'
        links = "".join(f'<a href="/{k}"{" class=on" if k == active else ""}>{t}</a>' for k, t in NAV)
        nav = (f'<header class="top"><a class="brand" href="/home"><i></i>MeshSense</a>'
               f'<nav class="pills">{links}</nav>'
               f'<form method="post" action="/logout" class="who">{av}<button class="btn ghost sm">로그아웃</button></form></header>'
               f'<div class="mtitle"><a class="brand" href="/home"><i></i>MeshSense</a>{av}</div>')
        tabs = '<nav class="tabs">' + "".join(
            f'<a href="/{k}"{" class=on" if k == active else ""}><svg viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.8">{ICONS[k]}</svg>{t}</a>' for k, t in NAV) + "</nav>"
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    fl = f'<div class="flash">{e(flash)}</div>' if flash else ""
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">{meta}
<title>{e(title)} · MeshSense</title>
<link rel="manifest" href="/manifest.json"><link rel="icon" href="/icon.svg">
<meta name="theme-color" content="#ffffff" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0b0b0c" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes"><meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="MeshSense">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/static/pretendard.min.css">
<style>{CSS}</style></head><body>{nav}<main>{fl}{body}</main>{tabs}</body></html>"""


def head(title: str, sub: str = "", right: str = "") -> str:
    s = f'<p class="hide-m">{e(sub)}</p>' if sub else ""
    return f'<div class="pagehead"><div><h1>{e(title)}</h1>{s}</div>{right}</div>'


def page_login(h, signup: bool, err: str) -> str:
    if signup:
        form = """<h2 class="formtitle">가입</h2>
<form method="post" action="/signup">
<label>이름 <span class="hint">지인에게 가는 문자에 들어가요</span></label><input name="name" required maxlength="20">
<label>이메일</label><input name="email" type="email" required autocomplete="username">
<label>비밀번호 <span class="hint">8자 이상</span></label><input name="pw" type="password" required minlength="8" autocomplete="new-password">
<label>내 휴대폰 번호</label><input name="phone" inputmode="tel" required placeholder="010-0000-0000">
<button class="btn primary wide lg mt">가입하기</button></form>
<p class="note center">이미 계정이 있나요? <a href="/">로그인</a></p>"""
    else:
        form = """<h2 class="formtitle">로그인</h2>
<form method="post" action="/login">
<label>이메일</label><input name="email" type="email" required autocomplete="username">
<label>비밀번호</label><input name="pw" type="password" required autocomplete="current-password">
<button class="btn primary wide lg mt">로그인</button></form>
<p class="note center">처음이신가요? <a href="/?signup=1">가입하기</a></p>"""
    errh = f'<p class="err">{e(err)}</p>' if err else ""
    body = f"""<div class="auth"><div>
<h1 class="display">Wi-Fi 로<br>방을 지켜봅니다.</h1>
<p class="lead">카메라 없이 Wi-Fi 신호만으로 방이 비었는지, 사람이 가만히 있는지, 움직이는지 알고 — 불을 켜고, 이상하면 알려 줍니다.</p>
<div class="preview hide-m"><div class="card hero" style="--c:var(--st-s)"><div class="lbl">지금 방은</div>
<div class="st display">정지</div><div class="desc muted">12분째 · 조명 켜짐</div></div></div></div>
<div class="card authcard">{errh}{form}</div></div>"""
    return page("로그인", body)


def page_home(user, now: float) -> str:
    cfg, dev = config(user), device_of(user["id"])
    t0 = day_range(datetime.fromtimestamp(now, home.TZ).strftime("%Y-%m-%d"))[0]
    urgent = ""
    if dev is not None:
        em = q("select * from alerts where device_id=? and token is not null and resolved is null "
               "and confirmed is null order by created desc limit 1", (dev["id"],), one=True)
        if em is not None:
            left = int(cfg["confirm_min"]) * 60 - (now - em["created"])
            n = len(cfg["contacts"])
            msg = (f'<div class="count display">{int(left // 60)}:{int(left % 60):02d}</div>'
                   f'<p>뒤 지인 {n}명에게 문자가 갑니다.</p>' if left > 0
                   else f'<p class="mt">지인 {n}명에게 문자를 보냈습니다.</p>')
            urgent = (f'<div class="card urgent"><h2>응급 확인을 기다리는 중</h2>'
                      f'<p>{e(em["label"])} — 본인에게 문자를 보냈습니다.</p>{msg}'
                      f'<form method="post" action="/c/{e(em["token"])}"><button class="btn wide lg">괜찮아요</button></form></div>')
    if dev is None:
        hero = ('<div class="card hero" style="--c:var(--line)"><div class="lbl">지금 방은</div>'
                '<div class="st display">—</div><div class="desc">아직 연결된 집 기기가 없어요.</div>'
                '<a class="btn primary mt" href="/devices">기기 연결하기</a></div>')
    else:
        st = "offline" if stale(dev, now) else (dev["state"] or "deciding")
        held = now - (dev["last_seen"] if st == "offline" else dev["since"] or now)
        lamp = ('<i class="lamp"></i>켜짐' if dev["led"] else '<i class="lampoff"></i>꺼짐') \
            if dev["led"] is not None else "—"
        sig = (f'<i class="{"baddot" if st == "offline" else "okdot"}"></i>{ago(now - dev["last_seen"])}'
               if dev["last_seen"] else "—")
        hero = (f'<div class="card hero" style="--c:var(--st-{CLS[st]})"><div class="lbl">지금 방은</div>'
                f'<div class="st display">{LABEL[st]}</div>'
                f'<div class="desc">{DESC[st]} · <b>{dur(held)}째</b></div><div class="facts">'
                f'<div class="fact"><small>조명</small><b>{lamp}</b></div>'
                f'<div class="fact"><small>집 기기</small><b>{sig}</b></div>'
                f'<div class="fact"><small>지금은</small><b>{"수면 시간" if home.in_sleep(now, cfg) else "깨어 있는 시간"}</b></div>'
                f'</div></div>')
    away = (f'<form method="post" action="/away" class="card switch"><div class="t"><h2>외출 모드</h2>'
            f'<div class="muted small">켜 두면 움직임을 침입으로 보고 알려요</div></div>'
            f'<button class="toggle{" on" if cfg["away"] else ""}" aria-label="외출 모드 전환"></button></form>')
    today = (f'<div class="card"><div class="rowhead"><h2>오늘</h2><a class="muted small" href="/history">기록 보기 →</a></div>'
             f'<div class="mt">{bar(segments(user["id"], t0, t0 + 86400, now), t0, now, cfg)}</div></div>')
    body = (head("홈", datetime.fromtimestamp(now, home.TZ).strftime("%m월 %d일 · %H:%M"))
            + f'<div class="grid"><div class="stack">{urgent}{hero}{away}</div>'
            f'<div class="stack">{today}<div class="card"><h2>최근 알림</h2>{alerts_table(user["id"], 0, now + 1, 5, True)}</div></div></div>')
    return page("홈", body, user, "home", refresh=15)


def alerts_table(uid: int, t0: float, t1: float, limit: int, with_day: bool) -> str:
    rows = q("select a.* from alerts a join devices d on d.id=a.device_id where d.user_id=? "
             "and a.created>=? and a.created<? order by a.created desc limit ?", (uid, t0, t1, limit))
    if not rows:
        return '<p class="muted small mt">알림이 없어요.</p>'
    out = []
    for a in rows:
        when = datetime.fromtimestamp(a["created"], home.TZ).strftime("%m/%d %H:%M" if with_day else "%H:%M")
        what = ("확인함" if a["confirmed"] else "지인에게 보냄" if a["escalated"]
                else "복구됨" if a["kind"] == "offline" and a["resolved"] else "본인")
        fail = ' <span class="err">발송 실패</span>' if (a["status"] or "").startswith("error") else ""
        out.append(f'<tr><td class="t">{when}</td><td>{e(a["label"] or KIND[a["kind"]])}{fail}</td>'
                   f'<td class="muted r" title="{e(a["status"] or "")}">{what}</td></tr>')
    return f'<table class="mt">{"".join(out)}</table>'


def page_history(user, date: str, now: float) -> str:
    cfg = config(user)
    t0, t1 = day_range(date)
    segs = segments(user["id"], t0, t1, now)
    tot = {}
    for a, b, s in segs:
        tot[s] = tot.get(s, 0) + b - a
    n_alert = q("select count(*) n from alerts a join devices d on d.id=a.device_id where d.user_id=? "
                "and a.created>=? and a.created<?", (user["id"], t0, t1), one=True)["n"]
    rows = "".join(f'<tr><td class="t">{hm(a)}</td><td><span class="badge c-{CLS[s]}">{LABEL[s]}</span></td>'
                   f'<td class="muted r">{dur(b - a)}</td></tr>' for a, b, s in reversed(segs))
    picker = (f'<form><input type="date" name="date" value="{e(date)}" onchange="this.form.submit()" '
              f'class="date"></form>')
    body = (head("기록", "하루 동안 방이 어떻게 지났는지", picker)
            + f'<div class="card">{bar(segs, t0, now, cfg)}<div class="kpis">'
            f'<div class="kpi"><small>방에 있음</small><b>{dur(tot.get("static", 0) + tot.get("motion", 0))}</b></div>'
            f'<div class="kpi"><small>빈 방</small><b>{dur(tot.get("empty", 0))}</b></div>'
            f'<div class="kpi"><small>알림</small><b>{n_alert}건</b></div></div></div>'
            f'<div class="grid2 mt16"><div class="card"><h2>상태 변화</h2>'
            f'{f"<table class=mt>{rows}</table>" if rows else EMPTY_P.format("기록이 없어요.")}</div>'
            f'<div class="card"><h2>이날 알림</h2>{alerts_table(user["id"], t0, t1, 50, False)}</div></div>')
    return page("기록", body, user, "history")


def rule_editor(r: dict, rid: str) -> str:
    def sel(name, opts, cur):
        return (f'<select name="{name}"' + (' onchange="this.form.classList.toggle(\'custom\',this.value===\'custom\')"'
                                            if name == "when" else "") + ">"
                + "".join(f'<option value="{k}"{" selected" if k == cur else ""}>{v}</option>' for k, v in opts.items())
                + "</select>")
    delete = ('<button class="btn ghost" formaction="/rules/del" formnovalidate>삭제</button>' if rid != "new" else "")
    return f"""<form method="post" action="/rules/save" class="editor{' custom' if r['when'] == 'custom' else ''}">
<input type="hidden" name="id" value="{e(rid)}"><div class="sentence">
{sel("when", WHEN, r["when"])}<span class="tw"><input type="time" name="start" value="{e(r.get('start', '22:00'))}"> –
<input type="time" name="end" value="{e(r.get('end', '06:00'))}"></span> 에
{sel("state", {k: LABEL[k] for k in RULE_STATES}, r["state"])} 이
<input type="number" name="minutes" min="0" max="1440" value="{int(r['minutes'])}" required> 분
{sel("kind", RULE_KINDS, r["kind"])}</div>
<p class="hint">0분 계속되면 = 나타나는 즉시</p>
<label>알릴 사람</label>{sel("notify", NOTIFY, r["notify"])}
<div class="edbar"><button class="btn primary">{'추가' if rid == 'new' else '저장'}</button>
<a class="btn ghost" href="/settings#rules">취소</a>{delete}</div></form>"""


def rules_card(c: dict, edit: str, preset: str) -> str:
    rows = []
    for r in c["rules"]:
        if r["id"] == edit:
            rows.append(rule_editor(r, r["id"]))
            continue
        rows.append(f'<div class="rule{"" if r["on"] else " off"}">'
                    f'<form method="post" action="/rules/toggle"><input type="hidden" name="id" value="{e(r["id"])}">'
                    f'<button class="toggle sm{" on" if r["on"] else ""}" aria-label="켜기/끄기"></button></form>'
                    f'<div class="txt"><b>{e(rule_text(r))}</b><small>{NOTIFY[r["notify"]]}</small></div>'
                    f'<a class="btn ghost" href="/settings?rule={e(r["id"])}#rules">수정</a></div>')
    if edit == "new" or preset in PRESETS:
        tail = rule_editor({**NEW_RULE, **(PRESETS[preset][1] if preset in PRESETS else {})}, "new")
    elif len(c["rules"]) >= MAX_RULES:
        tail = f'<p class="note">상황은 {MAX_RULES}개까지예요.</p>'
    else:
        chips = "".join(f'<a class="chip" href="/settings?preset={k}#rules">+ {t}</a>' for k, (t, _) in PRESETS.items())
        tail = (f'<div class="chips"><span class="muted small">예시로 시작:</span>{chips}</div>'
                '<a class="btn wide mt10" href="/settings?rule=new#rules">+ 상황 직접 만들기</a>')
    return (f'<div class="card" id="rules"><h2>나만의 상황</h2><p class="sub">정해 둔 상황이 되면 알려요. '
            f'조건이 처음 맞는 순간 한 번 보내요.</p>{"".join(rows)}{tail}</div>')


def page_settings(user, flash: str, edit: str = "", preset: str = "") -> str:
    c = config(user)
    num = lambda k, unit: f'<div class="unit"><input name="{k}" type="number" min="1" value="{int(c[k])}" required><span>{unit}</span></div>'  # noqa: E731
    people = "".join(
        f'<div class="person"><span class="av" style="background:{AVC[i % len(AVC)]}">{e(p["name"][:1])}</span>'
        f'<div><b>{e(p["name"])}</b><small>{e(p["phone"])}</small></div>'
        f'<form method="post" action="/contacts/del"><input type="hidden" name="i" value="{i}">'
        f'<button class="btn ghost">삭제</button></form></div>' for i, p in enumerate(c["contacts"]))
    body = (head("설정", "저장하면 집 기기가 1분 안에 새 설정을 받아 갑니다",
                 '<button class="btn primary hide-small" form="cfg">저장</button>')
            + f"""<div class="grid2"><form id="cfg" method="post" action="/settings" class="stack">
<div class="card"><h2>수면 시간</h2><p class="sub">이 시간엔 오래 가만히 있어도 응급 알림을 보내지 않고 조명을 꺼 드려요.</p>
<div class="row"><div><label class="mt0">시작</label><input type="time" name="sleep_start" value="{e(c['sleep_start'])}" required></div>
<div><label class="mt0">끝</label><input type="time" name="sleep_end" value="{e(c['sleep_end'])}" required></div></div>
<label>조명 끄기 <span class="hint">수면 시작부터 셉니다</span></label>{num('sleep_off_min', '분 가만히 있으면')}
<label>조명 켜기 <span class="hint">뒤척임엔 켜지지 않게</span></label>{num('motion_wake_s', '초 넘게 움직이면')}</div>
<div class="card"><h2>긴급 알림</h2><p class="sub">깨어 있는 시간에 너무 오래 움직임이 없으면, 먼저 나에게 묻고 답이 없으면 지인에게 알려요.</p>
<label class="mt0">나에게 확인 문자</label>{num('static_alert_min', '분 움직임이 없으면')}
<label>지인에게 문자</label>{num('confirm_min', '분 안에 확인하지 않으면')}
<label>내 번호</label><input name="phone" inputmode="tel" value="{e(user['phone'])}" required></div>
<div class="savebar"><button class="btn primary">저장</button></div></form>
<div class="stack">{rules_card(c, edit, preset)}<div class="card"><h2>지인</h2><p class="sub">문자 받는 데 동의한 분만 넣어 주세요. 최대 {MAX_CONTACTS}명.</p>
{people or '<p class="muted small">아직 없어요.</p>'}
<form method="post" action="/contacts/add"><div class="row mt16"><input name="name" placeholder="이름" required maxlength="20">
<input name="phone" placeholder="010-0000-0000" inputmode="tel" required></div>
<button class="btn wide mt10">+ 지인 추가</button></form></div></div></div>""")
    return page("설정", body, user, "settings", flash=flash)


EMPTY_P = '<p class="muted small mt">{}</p>'   # f-string 안에 역슬래시를 못 쓰는 Python 3.9(Amazon Linux) 대비
AVC = ("#fbcfe8", "#a7f3d0", "#fde68a", "#c7d2fe", "#fed7aa")
MAX_CONTACTS = 5


def page_devices(user, now: float) -> str:
    code = q("select * from pair_codes where user_id=? and expires>? order by expires desc limit 1",
             (user["id"], now), one=True)
    if code:
        left = int(code["expires"] - now)
        new = (f'<div class="code">{code["code"]}</div><div class="muted small center">{left // 60}분 {left % 60}초 남음</div>'
               '<p class="strong mt16">집 컴퓨터에서</p><ol class="steps">'
               '<li><b>MeshSense 제어판</b>을 열고 <b>연동</b> 탭으로 가요</li>'
               '<li>이 코드를 넣고 <b>연결</b>을 눌러요</li></ol>')
    else:
        new = '<form method="post" action="/devices/code"><button class="btn primary wide lg">연동 코드 발급</button></form>'
    devs = q("select * from devices where user_id=? order by created", (user["id"],))
    rows = "".join(
        f'<div class="person"><span class="av" style="background:var(--card)">💻</span><div><b>{e(d["name"])}</b>'
        f'<small>{"<i class=baddot></i>끊김 · " if stale(d, now) else "<i class=okdot></i>" if d["last_seen"] else ""}'
        f'{ago(now - d["last_seen"]) + " 신호" if d["last_seen"] else "아직 신호 없음"} · '
        f'{datetime.fromtimestamp(d["created"], home.TZ).strftime("%m/%d")} 연결</small></div>'
        f'<form method="post" action="/devices/del"><input type="hidden" name="id" value="{d["id"]}">'
        f'<button class="btn">해제</button></form></div>' for d in devs)
    body = (head("기기", "집에서 판단을 돌리는 컴퓨터를 이 계정에 연결합니다")
            + f'<div class="grid2"><div class="card"><h2>새 기기 연결</h2><p class="sub">코드는 10분 동안 한 번만 쓸 수 있어요.</p>{new}</div>'
            f'<div class="card"><h2>연결된 기기</h2>{rows or EMPTY_P.format("아직 없어요.")}'
            f'<p class="note">해제하면 그 기기는 더 이상 기록을 보내지 못해요. 집 조명은 계속 동작합니다.</p></div></div>')
    return page("기기", body, user, "devices", refresh=30 if code else None)


def page_verify(user, now: float, msg: str = "", err: str = "") -> str:
    """메일 인증(§3.6, 시안 v4). 링크가 아니라 코드 — 메일 보안 검사기가 링크를 미리 연다."""
    c = q("select * from email_codes where user_id=?", (user["id"],), one=True)
    wait = int(VERIFY_RESEND_S - (now - c["sent"])) if c else 0
    resend = (f'<span class="muted">코드 다시 보내기 ({wait}초 뒤 가능)</span>' if wait > 0 else
              '<button class="linkbtn" form="resend">코드 다시 보내기</button>')
    fl = f'<div class="flash">{e(msg)}</div>' if msg else ""
    er = f'<p class="err mt">{e(err)}</p>' if err else ""
    return page("메일 인증", f"""<div class="confirm" style="max-width:440px">{fl}
<div class="brand center"><i></i>MeshSense</div>
<h1 class="display" style="font-size:32px">메일을 확인해 주세요</h1>
<p class="muted"><b style="color:var(--ink)">{e(user["email"])}</b> 으로 6자리 코드를 보냈어요.<br>10분 안에 넣어 주세요.</p>
<form method="post" action="/verify" class="card" style="margin-top:24px;text-align:left">
<label class="mt0">인증 코드</label>
<input class="code-in" name="code" placeholder="000000" inputmode="numeric" maxlength="6" autocomplete="one-time-code" required autofocus>
{er}<button class="btn primary wide lg mt16">인증하기</button></form>
<form id="resend" method="post" action="/verify/resend"></form>
<form id="restart" method="post" action="/verify/restart"></form>
<p class="note" style="text-align:center">메일이 안 왔나요? 스팸함도 봐 주세요.<br>{resend} ·
<button class="linkbtn" form="restart">다른 이메일로 가입</button></p>
<p class="note" style="text-align:center">인증 전에는 기기 연결과 알림 설정을 할 수 없어요.</p></div>""",
                refresh=wait + 1 if wait > 0 else None)


def page_confirm(a, now: float, done: bool = False) -> str:
    if a is None:
        inner = '<h1 class="display">링크가 없어요</h1><p class="muted">만료되었거나 잘못된 링크예요.</p>'
    elif a["confirmed"] or done:
        inner = '<h1 class="display">확인했어요</h1><p class="muted">지인에게 문자를 보내지 않아요.</p>'
        if a["escalated"]:
            inner = '<h1 class="display">확인했어요</h1><p class="muted">다만 지인에게는 이미 문자가 갔어요. 직접 연락해 주세요.</p>'
    else:
        dev = q("select * from devices where id=?", (a["device_id"],), one=True)
        user = q("select * from users where id=?", (dev["user_id"],), one=True)
        cfg = config(user)
        left = int(cfg["confirm_min"]) * 60 - (now - a["created"])
        n = len(cfg["contacts"])
        tail = (f'<div class="count display">{int(left // 60)}:{int(left % 60):02d}</div>'
                f'<p class="muted">안에 누르지 않으면 지인 {n}명에게 문자가 가요.</p>' if left > 0 and not a["escalated"]
                else f'<p class="muted mt">지인 {n}명에게 이미 문자가 갔어요.</p>')
        inner = (f'<h1 class="display">괜찮으세요?</h1><p class="muted">{e(a["label"])}</p>'
                 f'{tail}<form method="post"><button class="btn primary wide xl">괜찮아요</button></form>'
                 f'<p class="note">링크를 여는 것만으로는 확인되지 않아요.</p>')
    return page("확인", f'<div class="confirm"><div class="brand center"><i></i>MeshSense</div>{inner}</div>',
                refresh=15 if a is not None and not a["confirmed"] and not done else None)


# ── 요청 처리 ───────────────────────────────────────────────────────────────
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
LIMITS = {"sleep_off_min": (1, 600), "motion_wake_s": (1, 600),
          "static_alert_min": (1, 1440), "confirm_min": (1, 120)}
PAIR_FAILS: deque = deque()


def phone_ok(p: str) -> bool:
    return re.fullmatch(r"01\d{8,9}", digits(p)) is not None


class H(BaseHTTPRequestHandler):
    server_version = "MeshSense"

    def log_message(self, fmt, *args):     # 기본은 stderr 에 요청마다 한 줄 — 토큰이 섞이지 않게 경로만
        sys.stderr.write(f"{self.command} {urlsplit(self.path).path.split('/c/')[0]} {args[1] if len(args) > 1 else ''}\n")

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str):
        url = urlsplit(self.path)
        self.qs = {k: v[0] for k, v in parse_qs(url.query).items()}
        for m, pat, fn in ROUTES:
            mt = re.fullmatch(pat, url.path)
            if m == method and mt:
                try:
                    return fn(self, *mt.groups())
                except Exception:
                    traceback.print_exc()
                    return self.send(500, "서버 오류", "text/plain; charset=utf-8")
        self.send(404, page("없음", '<div class="confirm"><h1 class="display">없는 페이지예요</h1>'
                                   '<p><a href="/">처음으로</a></p></div>'))

    # 응답
    def send(self, code: int, body, ctype="text/html; charset=utf-8", headers=()):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")   # 확인 링크 토큰이 다른 곳으로 새지 않게
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, loc: str, cookie: str | None = None):
        hs = [("Location", loc)] + ([("Set-Cookie", cookie)] if cookie else [])
        self.send(303, "", headers=hs)

    def json(self, obj, code=200):
        self.send(code, json.dumps(obj, ensure_ascii=False), "application/json")

    # 입력
    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 65536:
            raise ValueError("본문이 너무 큽니다")
        return self.rfile.read(n)

    def form(self) -> dict:
        return {k: v[0].strip() for k, v in parse_qs(self.body().decode()).items()}

    def json_body(self) -> dict:
        try:
            b = json.loads(self.body() or b"{}")
        except ValueError:
            return {}
        return b if isinstance(b, dict) else {}

    # 로그인
    def user(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "s":
                uid, _, rest = v.partition(".")
                exp, _, sig = rest.partition(".")
                good = hmac.new(SECRET, f"{uid}.{exp}".encode(), "sha256").hexdigest()
                if hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > time.time():
                    return q("select * from users where id=?", (int(uid),), one=True)
        return None

    def need_user(self, verified: bool = True):
        """로그인한 사용자. 없으면 로그인으로, 미인증이면 메일 인증(§3.6)으로 보내고 None."""
        u = self.user()
        if u is None:
            self.redirect("/")
            return None
        if verified and u["verified"] is None:
            self.redirect("/verify")
            return None
        return u


def cookie_for(uid: int | None) -> str:
    secure = "; Secure" if BASE.startswith("https") else ""
    if uid is None:
        return f"s=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{secure}"
    exp = int(time.time()) + SESSION_S
    sig = hmac.new(SECRET, f"{uid}.{exp}".encode(), "sha256").hexdigest()
    return f"s={uid}.{exp}.{sig}; Path=/; Max-Age={SESSION_S}; HttpOnly; SameSite=Lax{secure}"


def r_index(h):
    if h.user():
        return h.redirect("/home")
    h.send(200, page_login(h, h.qs.get("signup") == "1", h.qs.get("e", "")))


def r_login(h):
    f = h.form()
    u = q("select * from users where email=?", (f.get("email", "").lower(),), one=True)
    # ponytail: 로그인 시도 제한 없음(§9). 공개 운영 전에는 IP 별 실패 횟수를 센다.
    if u is None or not hmac.compare_digest(hash_pw(f.get("pw", ""), u["salt"]), u["pw_hash"]):
        return h.redirect("/?e=" + quote("이메일이나 비밀번호가 맞지 않아요"))
    h.redirect("/home", cookie_for(u["id"]))


def r_signup(h):
    f = h.form()
    email, name, pw, phone = f.get("email", "").lower(), f.get("name", ""), f.get("pw", ""), f.get("phone", "")
    err = ("이름을 적어 주세요" if not name or len(name) > 20 else
           "이메일 형식이 아니에요" if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) else
           "비밀번호는 8자 이상이에요" if len(pw) < 8 else
           "휴대폰 번호 형식이 아니에요" if not phone_ok(phone) else
           "이미 가입된 이메일이에요" if q("select 1 from users where email=? and verified is not null",
                                           (email,), one=True) else "")
    if err:
        return h.redirect("/?signup=1&e=" + quote(err))
    drop_user(email=email)                      # 그 주소로 남아 있던 미인증 계정 — 선점을 막는다
    salt = secrets.token_hex(16)
    now = time.time()
    uid = q("insert into users(email,name,phone,pw_hash,salt,config,created) values(?,?,?,?,?,?,?)",
            (email, name, phone, hash_pw(pw, salt), salt, json.dumps(home.DEFAULT), now))
    msg = mail_code(q("select * from users where id=?", (uid,), one=True), now)
    h.redirect("/verify" + ("?e=" + quote(msg) if msg else ""), cookie_for(uid))


def drop_user(uid: int | None = None, email: str | None = None) -> None:
    """미인증 계정을 지운다 — 인증된 계정은 건드리지 않는다."""
    for u in q("select id from users where verified is null and (id=? or email=?)", (uid, email)):
        q("delete from email_codes where user_id=?", (u["id"],))
        q("delete from users where id=?", (u["id"],))


def r_verify(h):
    u = h.need_user(verified=False)
    if u is None:
        return
    if u["verified"] is not None:
        return h.redirect("/home")
    h.send(200, page_verify(u, time.time(), msg="새 코드를 보냈어요. 이전 코드는 이제 쓸 수 없어요"
                            if h.qs.get("sent") else "", err=h.qs.get("e", "")))


def r_verify_post(h):
    u = h.need_user(verified=False)
    if u is None:
        return
    now, code = time.time(), re.sub(r"\s", "", h.form().get("code", ""))
    c = q("select * from email_codes where user_id=?", (u["id"],), one=True)
    if c is None or c["expires"] < now or c["tries"] >= VERIFY_TRIES:
        return h.redirect("/verify?e=" + quote("코드가 만료됐어요. 다시 보내기를 눌러 주세요"))
    if not hmac.compare_digest(sha(code), c["code_hash"]):
        q("update email_codes set tries=tries+1 where user_id=?", (u["id"],))
        left = VERIFY_TRIES - c["tries"] - 1
        return h.redirect("/verify?e=" + quote(f"코드가 맞지 않아요 — {left}번 더 시도할 수 있어요" if left
                                               else "5번 틀려 코드가 무효가 됐어요. 다시 보내기를 눌러 주세요"))
    q("update users set verified=? where id=?", (now, u["id"]))
    q("delete from email_codes where user_id=?", (u["id"],))
    h.redirect("/home")


def r_verify_resend(h):
    u = h.need_user(verified=False)
    if u is None:
        return
    now = time.time()
    c = q("select sent from email_codes where user_id=?", (u["id"],), one=True)
    if c and now - c["sent"] < VERIFY_RESEND_S:
        return h.redirect("/verify")
    msg = mail_code(u, now)
    h.redirect("/verify?" + ("e=" + quote(msg) if msg else "sent=1"))


def r_verify_restart(h):
    u = h.need_user(verified=False)
    if u is not None:
        drop_user(uid=u["id"])
    h.redirect("/?signup=1", cookie_for(None))


def r_logout(h):
    h.redirect("/", cookie_for(None))


def r_home(h):
    if (u := h.need_user()):
        h.send(200, page_home(u, time.time()))


def r_away(h):
    if (u := h.need_user()):
        cfg = config(u)
        cfg["away"] = not cfg["away"]
        save_config(u["id"], cfg)
        h.redirect("/home")


def r_history(h):
    if (u := h.need_user()):
        today = datetime.now(home.TZ).strftime("%Y-%m-%d")
        date = h.qs.get("date", today)
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            date = today
        h.send(200, page_history(u, date, time.time()))


def r_settings(h):
    if (u := h.need_user()):
        h.send(200, page_settings(u, "저장했어요" if h.qs.get("ok") else h.qs.get("e", ""),
                                  h.qs.get("rule", ""), h.qs.get("preset", "")))


def r_settings_save(h):
    if not (u := h.need_user()):
        return
    f, cfg = h.form(), config(u)
    for k in ("sleep_start", "sleep_end"):
        if not TIME_RE.fullmatch(f.get(k, "")):
            return h.redirect("/settings?e=" + quote("시간 형식이 아니에요"))
        cfg[k] = f[k]
    for k, (lo, hi) in LIMITS.items():
        v = f.get(k, "")
        if not v.isdigit() or not lo <= int(v) <= hi:
            return h.redirect("/settings?e=" + quote(f"숫자는 {lo}~{hi} 사이로 적어 주세요"))
        cfg[k] = int(v)
    if not phone_ok(f.get("phone", "")):
        return h.redirect("/settings?e=" + quote("휴대폰 번호 형식이 아니에요"))
    q("update users set phone=? where id=?", (f["phone"], u["id"]))
    save_config(u["id"], cfg)
    h.redirect("/settings?ok=1")


def r_contact_add(h):
    if not (u := h.need_user()):
        return
    f, cfg = h.form(), config(u)
    name, phone = f.get("name", ""), f.get("phone", "")
    if not name or len(name) > 20 or not phone_ok(phone):
        return h.redirect("/settings?e=" + quote("이름과 휴대폰 번호를 확인해 주세요"))
    if len(cfg["contacts"]) >= MAX_CONTACTS:
        return h.redirect("/settings?e=" + quote(f"지인은 {MAX_CONTACTS}명까지예요"))
    cfg["contacts"].append({"name": name, "phone": phone})
    save_config(u["id"], cfg)
    h.redirect("/settings")


def r_contact_del(h):
    if not (u := h.need_user()):
        return
    i, cfg = h.form().get("i", ""), config(u)
    if i.isdigit() and int(i) < len(cfg["contacts"]):
        del cfg["contacts"][int(i)]
        save_config(u["id"], cfg)
    h.redirect("/settings")


def r_rule_save(h):
    if not (u := h.need_user()):
        return
    f, cfg = h.form(), config(u)
    bad = lambda m: h.redirect("/settings?e=" + quote(m) + "#rules")  # noqa: E731
    if (f.get("when") not in WHEN or f.get("state") not in RULE_STATES or f.get("kind") not in RULE_KINDS
            or f.get("notify") not in NOTIFY):
        return bad("모르는 값이에요")
    m = f.get("minutes", "")
    if not m.isdigit() or int(m) > 1440:
        return bad("분은 0~1440 사이로 적어 주세요")
    r = {"on": True, "when": f["when"], "state": f["state"], "kind": f["kind"], "minutes": int(m),
         "notify": f["notify"]}
    if r["when"] == "custom":
        if not (TIME_RE.fullmatch(f.get("start", "")) and TIME_RE.fullmatch(f.get("end", ""))) \
                or f["start"] == f["end"]:
            return bad("직접 지정한 시간을 확인해 주세요")
        r.update(start=f["start"], end=f["end"])
    old = next((x for x in cfg["rules"] if x["id"] == f.get("id")), None)
    if old:
        old.update(r, on=old["on"])
    elif len(cfg["rules"]) >= MAX_RULES:
        return bad(f"상황은 {MAX_RULES}개까지예요")
    else:
        cfg["rules"].append({"id": "r" + secrets.token_hex(4), **r})
    save_config(u["id"], cfg)
    h.redirect("/settings?ok=1#rules")


def r_rule_toggle(h):
    if (u := h.need_user()):
        i, cfg = h.form().get("id"), config(u)
        for r in cfg["rules"]:
            if r["id"] == i:
                r["on"] = not r["on"]
        save_config(u["id"], cfg)
        h.redirect("/settings#rules")


def r_rule_del(h):
    if (u := h.need_user()):
        i, cfg = h.form().get("id"), config(u)
        cfg["rules"] = [r for r in cfg["rules"] if r["id"] != i]
        save_config(u["id"], cfg)
        h.redirect("/settings#rules")


def r_devices(h):
    if (u := h.need_user()):
        h.send(200, page_devices(u, time.time()))


def r_code(h):
    if not (u := h.need_user()):
        return
    now = time.time()
    q("delete from pair_codes where user_id=? or expires<?", (u["id"], now))
    while True:
        code = f"{secrets.randbelow(10 ** 6):06d}"
        if not q("select 1 from pair_codes where code=?", (code,), one=True):
            break
    q("insert into pair_codes(code,user_id,expires) values(?,?,?)", (code, u["id"], now + CODE_TTL_S))
    h.redirect("/devices")


def r_device_del(h):
    if (u := h.need_user()):
        i = h.form().get("id", "")
        if i.isdigit():
            q("delete from devices where id=? and user_id=?", (int(i), u["id"]))   # 토큰도 함께 사라진다
        h.redirect("/devices")


def r_confirm(h, token):
    h.send(200, page_confirm(q("select * from alerts where token=?", (token,), one=True),
                             time.time()))


def r_confirm_post(h, token):
    a = q("select * from alerts where token=?", (token,), one=True)
    if a is not None and a["confirmed"] is None:
        q("update alerts set confirmed=? where id=?", (time.time(), a["id"]))
    if h.user():
        return h.redirect("/home")
    h.send(200, page_confirm(a, time.time(), done=a is not None))


def public_config(cfg: dict) -> dict:
    """집 기기에 내려 보내는 설정 — 조명 판단에 필요한 것만. 지인 번호는 내려가지 않는다."""
    return {k: cfg[k] for k in ("sleep_start", "sleep_end", "sleep_off_min", "motion_wake_s", "away")}


def r_api_pair(h):
    now = time.time()
    while PAIR_FAILS and now - PAIR_FAILS[0] > CODE_TTL_S:
        PAIR_FAILS.popleft()
    # ponytail: 전역 실패 한도 — 6자리 코드를 무작위로 맞추는 시도를 막는다. 대신 누가 일부러 틀리면
    # 모두가 잠시 연동을 못 한다. 공개 운영이면 IP 별로.
    if len(PAIR_FAILS) >= 30:
        return h.json({"error": "잠시 뒤에 다시 시도해 주세요"}, 429)
    b = h.json_body()
    code, name = str(b.get("code", "")), str(b.get("name", "집 기기"))[:40] or "집 기기"
    row = q("select * from pair_codes where code=? and expires>?", (code, now), one=True)
    if row is None:
        PAIR_FAILS.append(now)
        return h.json({"error": "코드가 틀렸거나 만료되었습니다"}, 400)
    q("delete from pair_codes where code=?", (code,))
    token = secrets.token_urlsafe(32)
    q("insert into devices(user_id,name,token_hash,created) values(?,?,?,?)",
      (row["user_id"], name, sha(token), now))
    user = q("select * from users where id=?", (row["user_id"],), one=True)
    h.json({"token": token, "config": public_config(config(user))})


def r_api_state(h):
    auth = h.headers.get("Authorization", "")
    dev = q("select * from devices where token_hash=?", (sha(auth[7:]),), one=True) \
        if auth.startswith("Bearer ") else None
    if dev is None:
        return h.json({"error": "토큰이 무효입니다"}, 401)
    b, now = h.json_body(), time.time()
    state, since, led = b.get("state"), b.get("since"), b.get("led")
    if (state not in STATES or isinstance(since, bool) or not isinstance(since, (int, float))
            or not isinstance(led, (bool, type(None)))):
        return h.json({"error": "state·since·led 형식이 아닙니다"}, 400)
    since = min(float(since), now)
    gap = dev["last_seen"] is not None and now - dev["last_seen"] > OFFLINE_S
    if gap:
        q("insert into events(device_id,state,since,received) values(?,?,?,?)",
          (dev["id"], "offline", dev["last_seen"], now))
    if gap or state != dev["state"]:
        q("insert into events(device_id,state,since,received) values(?,?,?,?)",
          (dev["id"], state, max(since, dev["last_seen"] or since) if gap else since, now))
    q("update devices set state=?, since=?, led=?, last_seen=? where id=?",
      (state, since, None if led is None else int(led), now, dev["id"]))
    user = q("select * from users where id=?", (dev["user_id"],), one=True)
    cfg = config(user)
    if state == "motion" and dev["state"] != "motion" and cfg["away"]:
        raise_alert(dev, user, "intrusion", now,
                    f"[MeshSense] 외출 중인데 방에서 움직임이 잡혔습니다 ({hm(now)})",
                    [user["phone"]], resolved=True)
    h.json({"config": public_config(cfg)})


MANIFEST = json.dumps({"name": "MeshSense Home", "short_name": "MeshSense", "start_url": "/home",
                       "display": "standalone", "background_color": "#ffffff", "theme_color": "#111111",
                       "icons": [{"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]})
# ponytail: SVG 아이콘 하나. iOS 홈 화면 아이콘은 PNG(apple-touch-icon)만 받는다 — 필요하면 180px PNG 추가.
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" '
        'fill="#111"/><circle cx="32" cy="32" r="16" fill="#fff"/><circle cx="32" cy="32" r="6" fill="#111"/></svg>')

# ── 관리자 (계획 §5.1b) ─────────────────────────────────────────────────────
ADMIN_FAILS: deque = deque()
ADMIN_SESSION_S = 12 * 3600


def admin_password() -> str:
    """비어 있으면 관리자 페이지가 닫힌다 — 빈 비밀번호로 열리는 일이 없게."""
    return os.environ.get("ADMIN_PASSWORD", "")


def admin_mac(exp: int) -> str:
    # 비밀번호 해시를 서명에 넣는다 — ADMIN_PASSWORD 를 바꾸면 열려 있던 관리자 세션이 모두 끊긴다.
    # "admin:" 로 시작해 사용자 쿠키("<uid>.<exp>")의 서명과 겹치지 않는다.
    return hmac.new(SECRET, f"admin:{exp}:{sha(admin_password())}".encode(), "sha256").hexdigest()


def admin_cookie(on: bool) -> str:
    secure = "; Secure" if BASE.startswith("https") else ""
    if not on:
        return f"a=; Path=/admin; Max-Age=0; HttpOnly; SameSite=Strict{secure}"
    exp = int(time.time()) + ADMIN_SESSION_S
    return f"a={exp}.{admin_mac(exp)}; Path=/admin; Max-Age={ADMIN_SESSION_S}; HttpOnly; SameSite=Strict{secure}"


def is_admin(h) -> bool:
    if not admin_password():
        return False
    for part in (h.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "a":
            exp, _, sig = v.partition(".")
            if exp.isdigit() and int(exp) > time.time() and hmac.compare_digest(sig, admin_mac(int(exp))):
                return True
    return False


def admin_gate(h) -> bool:
    """관리자면 True. 닫혀 있으면 404, 로그인 전이면 로그인 화면으로."""
    if not admin_password():
        h.send(404, page("없음", '<div class="confirm"><h1 class="display">없는 페이지예요</h1></div>'))
        return False
    if not is_admin(h):
        h.redirect("/admin")
        return False
    return True


ADMIN_TOP = ('<header class="adm-top"><a class="brand" href="/admin" style="color:inherit"><i></i>MeshSense</a>'
             '<span class="adm-tag">관리자</span><form method="post" action="/admin/logout">'
             '<button class="btn ghost sm">로그아웃</button></form></header>')


def mask_phone(p: str) -> str:
    d = digits(p)
    return f"{d[:3]}-****-{d[-4:]}" if len(d) >= 10 else "—"


def page_admin_login(err: str = "") -> str:
    er = f'<p class="err mt">{e(err)}</p>' if err else ""
    return page("관리자", f"""<div class="confirm" style="max-width:380px"><div class="brand center"><i></i>MeshSense</div>
<h1 class="display" style="font-size:28px">관리자 로그인</h1><p class="muted">일반 사용자 로그인과 따로예요.</p>
<form method="post" action="/admin/login" class="card" style="text-align:left;margin-top:24px">
<label class="mt0">아이디</label><input name="id" required autocomplete="username">
<label>비밀번호</label><input name="pw" type="password" required autocomplete="current-password">
{er}<button class="btn primary wide lg mt16">로그인</button></form>
<p class="note">10분에 5번 틀리면 잠시 막혀요.</p></div>""")


def page_admin_users(find: str, now: float) -> str:
    t0 = day_range(today(now))[0]
    users = q("select * from users order by created desc")
    if find:
        users = [u for u in users if find.lower() in (u["name"] + " " + u["email"]).lower()]
    devs = q("select * from devices")
    n_wait = q("select count(*) n from users where verified is null", one=True)["n"]
    live = sum(1 for d in devs if d["last_seen"] and not stale(d, now))
    sms_n = (q("select n from sms_daily where day=?", (today(now),), one=True) or {"n": 0})["n"]
    mail_n = (q("select n from sms_daily where day=?", ("mail:" + today(now),), one=True) or {"n": 0})["n"]

    def stat(label, big, sub="", frac=None):
        bar = f'<div class="sbar"><i style="width:{min(100, frac * 100):.0f}%"></i></div>' if frac is not None else ""
        return f'<div class="stat"><small>{label}</small><b>{big}</b> <small>{sub}</small>{bar}</div>'

    rows = []
    for u in users:
        mine = [d for d in devs if d["user_id"] == u["id"]]
        d = max(mine, key=lambda x: x["last_seen"] or 0) if mine else None
        dev = (f'{e(d["name"])}<br><small class="muted">{ago(now - d["last_seen"]) if d["last_seen"] else "신호 없음"}'
               f'{" (끊김)" if stale(d, now) else ""}</small>' if d else '—')
        n_alert = q("select count(*) n from alerts a join devices d on d.id=a.device_id where d.user_id=? "
                    "and a.created>=?", (u["id"], t0), one=True)["n"]
        ok = u["verified"] is not None
        rows.append(f'<tr><td class="uname"><b>{e(u["name"])}</b><small>{e(u["email"])}</small></td>'
                    f'<td class="hide-m">{mask_phone(u["phone"])}</td>'
                    f'<td><span class="pill {"ok" if ok else "wait"}">{"인증" if ok else "인증 대기"}</span></td>'
                    f'<td class="hide-m t">{datetime.fromtimestamp(u["created"], home.TZ).strftime("%m/%d")}</td>'
                    f'<td class="hide-m">{dev}</td><td class="hide-m">{n_alert}건</td>'
                    f'<td class="r"><a class="btn sm" href="/admin/u/{u["id"]}">보기</a></td></tr>')
    table = ('<table class="ut"><tr><th>사용자</th><th class="hide-m">전화</th><th>상태</th><th class="hide-m">가입</th>'
             '<th class="hide-m">집 기기</th><th class="hide-m">오늘 알림</th><th></th></tr>' + "".join(rows) + "</table>"
             if rows else '<p class="muted small">사용자가 없어요.</p>')
    body = (head("사용자", "가입한 계정과 연결된 집 기기")
            + '<div class="stats">'
            + stat("가입", q("select count(*) n from users", one=True)["n"], f"인증 대기 {n_wait}")
            + stat("연결된 기기", len(devs), f"지금 신호 {live}")
            + stat("오늘 문자", f"{sms_n} / {DAILY_CAP}", frac=sms_n / DAILY_CAP)
            + stat("오늘 인증 메일", f"{mail_n} / {MAIL_DAILY_CAP}", frac=mail_n / MAIL_DAILY_CAP) + "</div>"
            + f'<div class="card"><form class="search"><input name="q" value="{e(find)}" placeholder="이름·이메일로 찾기"></form>'
            + table + "</div>")
    return page("관리자", body, header=ADMIN_TOP)


def page_admin_user(u, now: float) -> str:
    """상세. **사용자 설정은 보여 주지 않는다** — 수면 시간·지인·나만의 상황은 생활 패턴이다(§5.1b)."""
    ok = u["verified"] is not None
    devs = q("select * from devices where user_id=? order by created", (u["id"],))
    drows = "".join(
        f'<div class="person"><span class="av" style="background:var(--card)">💻</span><div><b>{e(d["name"])}</b>'
        f'<small>{"<i class=baddot></i>끊김 · " if stale(d, now) else "<i class=okdot></i>" if d["last_seen"] else ""}'
        f'{ago(now - d["last_seen"]) + " 신호" if d["last_seen"] else "아직 신호 없음"}</small></div>'
        f'<form method="post" action="/admin/d/{d["id"]}/delete"><button class="btn">해제</button></form></div>'
        for d in devs)
    body = (f'<div class="pagehead"><div><h1>{e(u["name"])}</h1><p>{e(u["email"])} · {mask_phone(u["phone"])} · '
            f'{datetime.fromtimestamp(u["created"], home.TZ).strftime("%m/%d")} 가입 · '
            f'<span class="pill {"ok" if ok else "wait"}">{"인증" if ok else "인증 대기"}</span></p></div>'
            f'<a class="btn" href="/admin">← 목록</a></div>'
            f'<div class="grid2"><div class="card"><h2>집 기기</h2>{drows or EMPTY_P.format("없어요.")}</div>'
            f'<div class="stack"><div class="card"><h2>최근 알림</h2>{admin_alerts(u["id"])}</div>'
            f'<form method="post" action="/admin/u/{u["id"]}/delete" class="card danger-card" '
            f'onsubmit="return confirm(\'{e(u["email"])} 계정을 지웁니다. 되돌릴 수 없어요.\')">'
            f'<h2>계정 삭제</h2><p class="muted small">계정·집 기기·기록·알림이 모두 지워지고 되돌릴 수 없어요.</p>'
            f'<button class="btn danger-btn mt">이 계정 삭제</button></form></div></div>')
    return page("관리자", body, header=ADMIN_TOP)


def admin_alerts(uid: int) -> str:
    """종류와 발송 결과만. 알림 문장(나만의 상황·응급 기준 분)은 사용자 설정이라 보여 주지 않는다."""
    rows = q("select a.* from alerts a join devices d on d.id=a.device_id where d.user_id=? "
             "order by a.created desc limit 10", (uid,))
    if not rows:
        return '<p class="muted small mt">알림이 없어요.</p>'
    return '<table class="mt">' + "".join(
        f'<tr><td class="t">{datetime.fromtimestamp(a["created"], home.TZ).strftime("%m/%d %H:%M")}</td>'
        f'<td>{KIND[a["kind"]]}</td><td class="muted r">{e((a["status"] or "")[:40])}</td></tr>'
        for a in rows) + "</table>"


def delete_user(uid: int) -> None:
    """계정과 그 계정의 모든 것 — 되돌릴 수 없다."""
    for d in q("select id from devices where user_id=?", (uid,)):
        q("delete from alerts where device_id=?", (d["id"],))
        q("delete from events where device_id=?", (d["id"],))
    for t in ("devices", "pair_codes", "email_codes"):
        q(f"delete from {t} where user_id=?", (uid,))
    q("delete from users where id=?", (uid,))


def r_admin(h):
    if not admin_password():
        return admin_gate(h)
    if not is_admin(h):
        return h.send(200, page_admin_login(h.qs.get("e", "")))
    h.send(200, page_admin_users(h.qs.get("q", "").strip()[:50], time.time()))


def r_admin_login(h):
    if not admin_password():
        return admin_gate(h)
    now = time.time()
    while ADMIN_FAILS and now - ADMIN_FAILS[0] > 600:
        ADMIN_FAILS.popleft()
    if len(ADMIN_FAILS) >= 5:
        return h.redirect("/admin?e=" + quote("너무 많이 틀렸어요. 10분 뒤에 다시 시도해 주세요"))
    f = h.form()
    if f.get("id") == "admin" and hmac.compare_digest(f.get("pw", "").encode(), admin_password().encode()):
        ADMIN_FAILS.clear()
        return h.redirect("/admin", admin_cookie(True))
    ADMIN_FAILS.append(now)
    h.redirect("/admin?e=" + quote("아이디나 비밀번호가 맞지 않아요"))


def r_admin_logout(h):
    h.redirect("/admin", admin_cookie(False))


def r_admin_user(h, uid):
    if admin_gate(h):
        u = q("select * from users where id=?", (int(uid),), one=True)
        if u is None:
            return h.redirect("/admin")
        h.send(200, page_admin_user(u, time.time()))


def r_admin_user_delete(h, uid):
    if admin_gate(h):
        delete_user(int(uid))
        h.redirect("/admin")


def r_admin_device_delete(h, did):
    if admin_gate(h):
        d = q("select * from devices where id=?", (int(did),), one=True)
        if d is not None:
            q("delete from devices where id=?", (d["id"],))     # 토큰도 함께 사라진다
            return h.redirect(f"/admin/u/{d['user_id']}")
        h.redirect("/admin")


ROUTES = [
    ("GET", r"/", r_index), ("POST", r"/login", r_login), ("POST", r"/signup", r_signup),
    ("POST", r"/logout", r_logout), ("GET", r"/verify", r_verify), ("POST", r"/verify", r_verify_post),
    ("POST", r"/verify/resend", r_verify_resend), ("POST", r"/verify/restart", r_verify_restart), ("GET", r"/home", r_home), ("POST", r"/away", r_away),
    ("GET", r"/history", r_history), ("GET", r"/settings", r_settings),
    ("POST", r"/settings", r_settings_save), ("POST", r"/contacts/add", r_contact_add),
    ("POST", r"/contacts/del", r_contact_del), ("POST", r"/rules/save", r_rule_save),
    ("POST", r"/rules/toggle", r_rule_toggle), ("POST", r"/rules/del", r_rule_del), ("GET", r"/devices", r_devices),
    ("POST", r"/devices/code", r_code), ("POST", r"/devices/del", r_device_del),
    ("GET", r"/c/([\w-]+)", r_confirm), ("POST", r"/c/([\w-]+)", r_confirm_post),
    ("POST", r"/api/pair", r_api_pair), ("POST", r"/api/state", r_api_state),
    ("GET", r"/admin", r_admin), ("POST", r"/admin/login", r_admin_login),
    ("POST", r"/admin/logout", r_admin_logout), ("GET", r"/admin/u/(\d+)", r_admin_user),
    ("POST", r"/admin/u/(\d+)/delete", r_admin_user_delete), ("POST", r"/admin/d/(\d+)/delete", r_admin_device_delete),
    ("GET", r"/manifest.json", lambda h: h.send(200, MANIFEST, "application/manifest+json")),
    ("GET", r"/icon.svg", lambda h: h.send(200, ICON, "image/svg+xml")),
]

# 시안 v2 (doc/plans/2026-10-23-home-service.md §5.1) — Cal.com 참고, 흑백 + 상태색은 제품 화면 안에서만
CSS = """
:root{color-scheme:light dark;--canvas:#fff;--soft:#f8f9fa;--card:#f5f5f5;--line:#e5e7eb;--line-soft:#f3f4f6;
 --ink:#111;--body:#374151;--muted:#6b7280;--primary:#111;--on-primary:#fff;--press:#242424;
 --inv:#101010;--on-inv:#fff;--on-inv-soft:#a1a1aa;--st-e:#94a3b8;--st-s:#3b82f6;--st-m:#fb923c;--st-x:#ef4444;
 --st-d:#cbd5e1;--sleep:#8b5cf6;--lamp:#f59e0b;--ok:#10b981;--bad:#ef4444;
 --sh:0 1px 2px rgba(0,0,0,.05);--sh2:0 4px 12px rgba(0,0,0,.08)}
@media(prefers-color-scheme:dark){:root{--canvas:#0b0b0c;--soft:#0b0b0c;--card:#161618;--line:#27272a;
 --line-soft:#1c1c1f;--ink:#f4f4f5;--body:#d4d4d8;--muted:#a1a1aa;--primary:#f4f4f5;--on-primary:#111;
 --press:#d4d4d8;--inv:#f4f4f5;--on-inv:#111;--on-inv-soft:#52525b;--st-d:#3f3f46;--sh:none;--sh2:none}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:var(--soft);color:var(--body);
 font:16px/1.5 Pretendard,-apple-system,BlinkMacSystemFont,"Apple SD Gothic Neo","Segoe UI",sans-serif}
a{color:var(--ink)} h1,h2{color:var(--ink);margin:0;font-weight:600}
.display{font-weight:600;letter-spacing:-.04em;color:var(--ink);line-height:1.05}
.muted{color:var(--muted)} .small{font-size:14px} .center{text-align:center;justify-content:center}
.r{text-align:right} .strong{font-weight:600;color:var(--ink);font-size:14px;margin:0}
.mt{margin-top:12px} .mt0{margin-top:0} .mt10{margin-top:10px} .mt16{margin-top:16px}
.err{color:var(--bad);font-size:13px;font-weight:600}
.top{height:64px;background:var(--canvas);border-bottom:1px solid var(--line);display:flex;align-items:center;
 gap:16px;padding:0 24px;position:sticky;top:0;z-index:10}
.brand{display:flex;align-items:center;gap:8px;font-weight:700;color:var(--ink);letter-spacing:-.02em;
 font-size:17px;text-decoration:none}
.brand i{width:22px;height:22px;border-radius:50%;background:var(--ink);display:grid;place-items:center}
.brand i::after{content:"";width:8px;height:8px;border-radius:50%;background:var(--canvas)}
.pills{display:flex;gap:2px;background:var(--card);border-radius:999px;padding:5px;margin:0 auto}
.pills a{padding:7px 16px;border-radius:999px;font-size:14px;font-weight:500;color:var(--muted);text-decoration:none}
.pills a.on{background:var(--canvas);color:var(--ink);box-shadow:var(--sh2)}
.who{display:flex;align-items:center;gap:4px;margin:0}
.avatar{width:36px;height:36px;border-radius:50%;background:#fde68a;color:#111;display:grid;place-items:center;
 font-size:13px;font-weight:600}
.tabs{display:none;position:fixed;left:0;right:0;bottom:0;z-index:10;background:var(--canvas);
 border-top:1px solid var(--line);padding:6px 8px calc(6px + env(safe-area-inset-bottom))}
.tabs a{flex:1;display:flex;flex-direction:column;align-items:center;gap:2px;font-size:11px;font-weight:500;
 color:var(--muted);padding:6px 0;min-height:48px;text-decoration:none}
.tabs a.on{color:var(--ink)} .tabs svg{width:24px;height:24px}
.mtitle{display:none}
main{max-width:1200px;margin:0 auto;padding:32px 24px 48px}
.flash{background:var(--card);border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:14px;color:var(--ink);font-weight:500}
.pagehead{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;margin-bottom:24px}
.pagehead h1{font-size:32px;letter-spacing:-.03em} .pagehead p{margin:4px 0 0;color:var(--muted);font-size:14px}
.pagehead form{margin:0}
.grid{display:grid;grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:16px;align-items:start}
.grid2{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px;align-items:start}
.stack{display:grid;gap:16px;margin:0}
.card{background:var(--canvas);border:1px solid var(--line);border-radius:12px;padding:24px;box-shadow:var(--sh)}
.card h2{font-size:16px} .card .sub{color:var(--muted);font-size:14px;margin:4px 0 16px}
.card p{margin:6px 0 0}
.rowhead{display:flex;justify-content:space-between;align-items:baseline}
.hero{border-radius:16px;padding:28px;box-shadow:var(--sh2)}
.hero .lbl{font-size:13px;font-weight:500;color:var(--muted)}
.hero .st{font-size:64px;margin:8px 0 4px;display:flex;align-items:center;gap:14px}
.hero .st::before{content:"";width:14px;height:14px;border-radius:50%;background:var(--c);flex:none;
 box-shadow:0 0 0 6px color-mix(in srgb,var(--c) 18%,transparent)}
.hero .desc{font-size:15px} .hero .desc b{color:var(--ink)}
.facts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:24px}
.fact{background:var(--card);border-radius:8px;padding:12px}
.fact small{display:block;font-size:12px;color:var(--muted);font-weight:500}
.fact b{font-size:15px;color:var(--ink);font-weight:600;display:flex;align-items:center;gap:6px}
.lamp{width:9px;height:9px;border-radius:50%;background:var(--lamp);box-shadow:0 0 8px var(--lamp)}
.lampoff{width:9px;height:9px;border-radius:50%;background:var(--line)}
.okdot,.baddot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:4px}
.baddot{background:var(--bad)}
.urgent{background:var(--inv);color:var(--on-inv-soft);border:0;border-radius:16px}
.urgent h2{color:var(--on-inv);font-size:18px} .urgent p{font-size:14px}
.urgent .count{font-size:44px;color:var(--on-inv);font-variant-numeric:tabular-nums;margin:12px 0 2px}
.urgent .btn{background:var(--on-inv);color:var(--inv);border:0;margin-top:16px}
.btn{font:600 14px/1 inherit;font-family:inherit;height:40px;padding:0 20px;border-radius:8px;border:1px solid var(--line);
 background:var(--canvas);color:var(--ink);cursor:pointer;display:inline-flex;align-items:center;justify-content:center;
 gap:6px;text-decoration:none}
.btn.primary{background:var(--primary);color:var(--on-primary);border-color:transparent}
.btn.primary:active{background:var(--press)}
.btn.wide{width:100%} .btn.lg{height:52px;font-size:16px} .btn.xl{height:60px;font-size:18px;margin-top:32px}
.btn.ghost{border-color:transparent;background:transparent;color:var(--muted)} .btn.sm{height:32px;padding:0 10px;font-size:13px}
.switch{display:flex;align-items:center;gap:16px;margin:0} .switch .t{flex:1}
.toggle{width:48px;height:28px;border-radius:14px;background:var(--line);position:relative;cursor:pointer;flex:none;border:0}
.toggle::after{content:"";position:absolute;top:3px;left:3px;width:22px;height:22px;border-radius:50%;background:#fff;
 box-shadow:var(--sh2);transition:.15s}
.toggle.on{background:var(--primary)} .toggle.on::after{left:23px;background:var(--on-primary)}
.day{position:relative;height:44px;border-radius:8px;overflow:hidden;background:var(--card)}
.day i{position:absolute;top:0;bottom:0}
.day .e{background:var(--st-e);opacity:.35} .day .s{background:var(--st-s)} .day .m{background:var(--st-m)}
.day .x{background:var(--st-x)} .day .d{background:var(--st-d)}
.sleepband{position:relative;height:6px;margin-bottom:4px}
.sleepband i{position:absolute;top:0;bottom:0;background:var(--sleep);border-radius:3px}
.day .now{position:absolute;top:0;bottom:0;width:2px;background:var(--ink)}
.ticks{display:flex;justify-content:space-between;font-size:12px;color:var(--muted);margin-top:6px}
.legend{display:flex;flex-wrap:wrap;gap:6px;margin-top:14px}
.badge{display:inline-flex;align-items:center;gap:6px;padding:4px 12px;border-radius:999px;background:var(--card);
 font-size:13px;font-weight:500;color:var(--ink);white-space:nowrap}
.badge::before{content:"";width:8px;height:8px;border-radius:50%;background:var(--c)}
.c-e{--c:var(--st-e)} .c-s{--c:var(--st-s)} .c-m{--c:var(--st-m)} .c-x{--c:var(--st-x)} .c-d{--c:var(--st-d)}
.c-z{--c:var(--sleep)}
.kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:20px}
.kpi{border-top:1px solid var(--line);padding-top:12px}
.kpi small{font-size:12px;color:var(--muted)} .kpi b{display:block;font-size:22px;color:var(--ink);font-weight:600;letter-spacing:-.02em}
table{width:100%;border-collapse:collapse;font-size:14px}
td{text-align:left;padding:12px 4px;border-bottom:1px solid var(--line-soft)}
td.t{font-variant-numeric:tabular-nums;color:var(--muted);width:90px;white-space:nowrap}
tr:last-child td{border-bottom:0}
label{display:block;margin:16px 0 6px;font-weight:600;font-size:14px;color:var(--ink)}
label .hint{font-weight:400;color:var(--muted);font-size:13px;margin-left:4px}
input{width:100%;height:44px;padding:0 14px;font:inherit;color:var(--ink);background:var(--canvas);
 border:1px solid var(--line);border-radius:8px}
input:focus{outline:0;border-color:var(--ink)} input.date{width:auto}
.unit{display:flex;align-items:center;gap:10px} .unit input{width:96px;flex:none;text-align:right}
.unit span{color:var(--muted);font-size:14px}
.row{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:12px}
input[type=time]{min-width:0}
select{height:44px;padding:0 12px;font:inherit;color:var(--ink);background:var(--canvas);border:1px solid var(--line);
 border-radius:8px;max-width:100%}
.rule{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line-soft)}
.rule form{margin:0} .rule .txt{flex:1;line-height:1.35;min-width:0}
.rule .txt b{color:var(--ink);font-weight:600;font-size:15px;display:block} .rule .txt small{color:var(--muted);font-size:13px}
.rule.off .txt{opacity:.45}
.toggle.sm{width:40px;height:24px} .toggle.sm::after{width:18px;height:18px} .toggle.sm.on::after{left:19px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:12px;align-items:center}
.chip{font-size:13px;font-weight:500;padding:6px 12px;border-radius:999px;border:1px dashed var(--line);
 background:var(--canvas);color:var(--ink);text-decoration:none}
.editor{background:var(--card);border-radius:12px;padding:16px;margin-top:12px}
.editor .sentence{display:flex;flex-wrap:wrap;align-items:center;gap:8px;font-size:15px;color:var(--ink)}
.editor .sentence input[type=number]{width:80px;text-align:right} .editor .sentence input[type=time]{width:auto}
.editor .tw{display:none;align-items:center;gap:6px} .editor.custom .tw{display:inline-flex}
.editor .hint{font-size:12px;color:var(--muted);margin:8px 0 0} .editor select[name=notify]{width:100%}
.edbar{display:flex;gap:8px;margin-top:14px}
.steps{margin:8px 0 0;padding-left:20px;font-size:14px} .steps li{margin:6px 0}
.person{display:flex;align-items:center;gap:12px;padding:12px 0;border-bottom:1px solid var(--line-soft)}
.person div{flex:1;line-height:1.3} .person b{color:var(--ink);font-weight:600;font-size:15px}
.person small{display:block;color:var(--muted);font-size:13px} .person form{margin:0}
.av{width:36px;height:36px;border-radius:50%;display:grid;place-items:center;font-size:13px;font-weight:600;color:#111;flex:none}
.savebar{display:none}
.note{font-size:13px;color:var(--muted);margin:12px 0 0}
.code{font:600 44px/1 ui-monospace,Menlo,monospace;letter-spacing:.2em;color:var(--ink);text-align:center;
 padding:24px;background:var(--card);border-radius:12px;margin:8px 0}
pre{background:var(--card);color:var(--ink);border-radius:8px;padding:12px 14px;
 font:13px/1.5 ui-monospace,Menlo,monospace;white-space:pre-wrap;word-break:break-all;margin:8px 0 0}
.auth{display:grid;grid-template-columns:7fr 5fr;gap:48px;align-items:center;max-width:1100px;margin:0 auto;padding:40px 0}
.auth h1{font-size:56px} .auth .lead{font-size:18px;color:var(--muted);margin:16px 0 28px;max-width:30rem}
.authcard{padding:32px} .formtitle{font-size:22px;letter-spacing:-.02em} .authcard .btn{margin-top:24px}
.preview{max-width:360px} .preview .hero{padding:20px} .preview .st{font-size:40px}
.confirm{max-width:420px;margin:0 auto;padding:40px 0;text-align:center}
.confirm h1{font-size:36px;margin:24px 0 8px} .confirm p{margin:0}
.code-in{font:600 32px/1 ui-monospace,Menlo,monospace;letter-spacing:.4em;text-align:center;height:64px}
.linkbtn{font:inherit;font-size:13px;color:var(--ink);background:none;border:0;padding:0;text-decoration:underline;cursor:pointer}
.adm-top{height:56px;background:var(--inv);color:var(--on-inv);display:flex;align-items:center;gap:12px;padding:0 24px}
.adm-top .brand i{background:var(--on-inv)} .adm-top .brand i::after{background:var(--inv)}
.adm-tag{font-size:11px;padding:2px 8px;border-radius:999px;background:#ef4444;color:#fff;font-weight:700}
.adm-top form{margin:0 0 0 auto} .adm-top .btn{color:var(--on-inv-soft)}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin-bottom:16px}
.stat{background:var(--canvas);border:1px solid var(--line);border-radius:12px;padding:16px}
.stat small{font-size:12px;color:var(--muted)} .stat>small:first-child{display:block}
.stat b{font-size:24px;color:var(--ink);letter-spacing:-.02em}
.sbar{height:6px;border-radius:3px;background:var(--card);margin-top:8px;overflow:hidden} .sbar i{display:block;height:100%;background:var(--ink)}
.ut td{padding:12px 8px;vertical-align:middle;border-bottom:1px solid var(--line-soft)}
.ut th{text-align:left;font-size:12px;color:var(--muted);font-weight:500;padding:8px;border-bottom:1px solid var(--line)}
.ut td.uname{line-height:1.35} .ut .uname b{display:block;color:var(--ink);white-space:nowrap}
.ut .uname small{display:block;color:var(--muted);font-size:12px;word-break:break-all}
.ut .btn.sm,.pill{white-space:nowrap}
.pill{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px}
.pill.ok{background:#d1fae5;color:#065f46} .pill.wait{background:#fef3c7;color:#92400e}
.search{margin:0 0 12px} .search input{max-width:320px}
.danger-card{border-color:#fecaca} .danger-card h2{color:#ef4444} .danger-btn{color:#ef4444}
.confirm .count{font-size:64px;color:var(--ink);font-variant-numeric:tabular-nums;margin:24px 0 4px}
@media (max-width:767px){
  .top{display:none} .tabs{display:flex}
  .mtitle{display:flex;align-items:center;justify-content:space-between;padding:calc(12px + env(safe-area-inset-top)) 20px 4px}
  main{padding:8px 16px calc(96px + env(safe-area-inset-bottom))}
  .pagehead{margin:8px 0 16px} .pagehead h1{font-size:28px} .hide-small{display:none}
  .grid,.grid2{grid-template-columns:minmax(0,1fr)}
  .card{padding:20px} .hero{padding:22px} .hero .st{font-size:52px}
  .facts{grid-template-columns:1fr 1fr} .facts .fact:last-child{grid-column:1/-1}
  .auth{grid-template-columns:1fr;padding:24px 0;gap:28px} .auth h1{font-size:36px} .auth .lead{font-size:16px}
  .savebar{display:flex;position:sticky;bottom:calc(76px + env(safe-area-inset-bottom));background:var(--soft);padding:12px 0}
  .savebar .btn{flex:1;height:48px}
  .kpi b{font-size:17px} .hide-m{display:none} .stats{grid-template-columns:1fr 1fr}
}
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="MeshSense 집 상태 서비스 클라우드")
    ap.add_argument("--host", default="127.0.0.1", help="앞단 Caddy 가 붙는 주소 (기본 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", type=Path, default=HERE / "home.db")
    ap.add_argument("--base", default=None, help="문자 링크에 쓰는 공개 주소 (예: https://meshsense.cloud)")
    a = ap.parse_args()
    init(a.db, a.base or f"http://{a.host}:{a.port}")
    threading.Thread(target=ticker, daemon=True).start()
    print(f"MeshSense cloud — http://{a.host}:{a.port}  (링크 주소 {BASE})")
    ThreadingHTTPServer((a.host, a.port), H).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
