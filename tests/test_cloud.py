"""집 상태 서비스 클라우드 — 실제 서버를 띄워 HTTP 로 한 바퀴 돈다.

가입 → 메일 인증 → 연동 코드 → 기기 연동 → 상태 전송 → 응급(본인 확인 → 미확인 시 지인) → 침입 → 끊김.
설계 정본 `doc/plans/2026-10-23-home-service.md` §3.3·§4·§5.
문자·메일은 `server.send_sms`·`server.send_mail` 을 바꿔 끼워 **실제로 나가지 않는다.**
"""
import http.cookiejar
import json
import re
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cloud import server as S  # noqa: E402
from realtime import home  # noqa: E402

ME, MOM = "010-1111-2222", "010-3333-4444"


class CloudFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), S.H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        S.init(Path(self.tmp.name) / "t.db", self.url, secret=b"test")
        S.PAIR_FAILS.clear()
        self.sent = []
        self._real_send, S.send_sms = S.send_sms, lambda nums, text: self.sent.append((nums, text)) or "sent 1/1"
        self.mails = []
        self._real_mail, S.send_mail = S.send_mail, lambda to, subj, text: self.mails.append((to, subj, text)) or "sent"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        S.send_sms, S.send_mail = self._real_send, self._real_mail
        self.httpd.shutdown()
        self.httpd.server_close()
        S.CONN.close()
        self.tmp.cleanup()

    # 도우미
    def post(self, path, **form):
        r = self.web.open(self.url + path, urllib.parse.urlencode(form).encode())
        return r.geturl(), r.read().decode()

    def get(self, path):
        return self.web.open(self.url + path).read().decode()

    def api(self, path, body, token=None):
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(),
                                     {"Content-Type": "application/json",
                                      **({"Authorization": f"Bearer {token}"} if token else {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def last_code(self):
        return re.search(r"인증 코드 (\d{6})", self.mails[-1][1]).group(1)

    def signup(self, email="me@example.com"):
        """가입 → 메일로 온 코드로 인증까지."""
        url, body = self.post("/signup", name="성연", email=email, pw="password1", phone=ME)
        self.assertTrue(url.endswith("/verify"))
        self.assertIn("메일을 확인해 주세요", body)
        url, body = self.post("/verify", code=self.last_code())
        self.assertTrue(url.endswith("/home"))
        return body

    def signup_and_pair(self):
        self.assertIn("아직 연결된 집 기기가 없어요", self.signup())
        self.post("/devices/code")
        code = re.search(r'class="code">(\d{6})<', self.get("/devices")).group(1)
        st, res = self.api("/api/pair", {"code": code, "name": "MacBook"})
        self.assertEqual(st, 200)
        self.assertEqual(self.api("/api/pair", {"code": code})[0], 400)      # 1회용
        self.assertNotIn("contacts", res["config"])                           # 지인 번호는 내려가지 않는다
        return res["token"]

    def awake_now(self):
        """지금이 깨어 있는 시간이 되도록 수면 시간을 2~3시간 뒤로 옮긴다."""
        h = datetime.now(home.TZ).hour
        url, body = self.post("/settings", sleep_start=f"{(h + 2) % 24:02d}:00",
                              sleep_end=f"{(h + 3) % 24:02d}:00", sleep_off_min="10", motion_wake_s="5",
                              static_alert_min="60", confirm_min="1", phone=ME)
        self.assertIn("저장했어요", body)

    def emergency(self):
        return S.q("select * from alerts where kind='emergency' order by id desc limit 1", one=True)

    # ── 시나리오 ───────────────────────────────────────────────────────────
    def test_login_required(self):
        self.assertTrue(self.web.open(self.url + "/home").geturl().endswith("/"))

    def test_bad_token_and_bad_body_are_rejected(self):
        token = self.signup_and_pair()
        self.assertEqual(self.api("/api/state", {"state": "static", "since": 1}, "nope")[0], 401)
        self.assertEqual(self.api("/api/state", {"state": "sleeping", "since": 1}, token)[0], 400)
        self.assertEqual(self.api("/api/state", {"state": "static", "since": "x"}, token)[0], 400)

    def test_emergency_confirm_then_escalate(self):
        token = self.signup_and_pair()
        self.awake_now()
        self.post("/contacts/add", name="엄마", phone=MOM)
        now = time.time()
        st, res = self.api("/api/state", {"state": "static", "since": now - 7200, "led": True}, token)
        self.assertEqual(st, 200)
        home_page = self.get("/home")
        self.assertIn("정지", home_page)
        self.assertIn("켜짐", home_page)

        # 본인에게 한 번 — 다음 검사에서는 다시 보내지 않는다
        S.tick(time.time())
        S.tick(time.time())
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], [ME])
        token_c = re.search(r"/c/([\w-]+)", self.sent[0][1]).group(1)
        self.assertIn("응급 확인을 기다리는 중", self.get("/home"))

        # 링크를 여는 것(GET)만으로는 확인되지 않는다 — 문자 앱 미리보기 대비
        self.assertIn("괜찮으세요?", self.get(f"/c/{token_c}"))
        self.assertIsNone(self.emergency()["confirmed"])
        self.post(f"/c/{token_c}")
        self.assertIsNotNone(self.emergency()["confirmed"])
        S.tick(time.time() + 61)
        self.assertEqual(len(self.sent), 1)                  # 확인했으니 지인에게 안 간다

        # 움직였다가 다시 오래 정지 → 새 건. 이번엔 확인하지 않는다 → 지인에게
        self.api("/api/state", {"state": "motion", "since": time.time()}, token)
        S.tick(time.time())
        self.assertIsNotNone(S.q("select resolved from alerts where kind='emergency' order by id limit 1",
                                 one=True)["resolved"])
        self.api("/api/state", {"state": "static", "since": time.time() - 7200}, token)
        S.tick(time.time())
        S.tick(time.time() + 61)
        self.assertEqual([n for n, _ in self.sent], [[ME], [ME], [MOM]])
        self.assertIn("성연님", self.sent[-1][1])
        S.tick(time.time() + 120)
        self.assertEqual(len(self.sent), 3)                  # 지인에게도 한 번만

        self.assertIn("상태 변화", self.get("/history"))

    def test_no_emergency_during_sleep(self):
        token = self.signup_and_pair()
        h = datetime.now(home.TZ).hour
        self.post("/settings", sleep_start=f"{(h - 1) % 24:02d}:00", sleep_end=f"{(h + 1) % 24:02d}:00",
                  sleep_off_min="10", motion_wake_s="5", static_alert_min="60", confirm_min="1", phone=ME)
        self.api("/api/state", {"state": "static", "since": time.time() - 7200}, token)
        S.tick(time.time())
        self.assertEqual(self.sent, [])

    def test_intrusion_when_away(self):
        token = self.signup_and_pair()
        self.api("/api/state", {"state": "empty", "since": time.time()}, token)
        self.post("/away")
        self.api("/api/state", {"state": "motion", "since": time.time()}, token)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("외출 중", self.sent[0][1])

    def test_offline_once_then_recovers(self):
        token = self.signup_and_pair()
        self.api("/api/state", {"state": "empty", "since": time.time()}, token)
        S.tick(time.time() + 200)
        S.tick(time.time() + 260)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("신호가", self.sent[0][1])
        S.tick(time.time())                                   # 아직 신호가 살아 있으면 닫힌다
        self.assertIsNotNone(S.q("select resolved from alerts where kind='offline'", one=True)["resolved"])


    def test_real_sender_needs_env_and_says_so(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIn("SOLAPI 환경 변수", self._real_send([ME], "x"))
        self.assertEqual(self._real_send(["없음"], "x"), "error: 받는 번호가 없습니다")
        self.assertEqual(S.digits("010-1234-5678"), "01012345678")

    def test_daily_cap_counts_recipients_service_wide(self):
        # 받는 번호 하나가 1건. 상한을 넘기는 발송은 통째로 막고 이유를 남긴다 (§3.3)
        now = time.time()
        for _ in range(13):
            self.assertTrue(S.notify([ME, MOM, ME], "x", now).startswith("sent"))   # 39건
        self.assertIn("하루 상한(40건)", S.notify([ME, MOM], "x", now))              # 41건째 → 막힘
        self.assertTrue(S.notify([ME], "x", now).startswith("sent"))                  # 40건째는 간다
        self.assertIn("하루 상한", S.notify([ME], "x", now))
        self.assertEqual(len(self.sent), 14)
        self.assertTrue(S.notify([ME], "x", now + 86400).startswith("sent"))          # 다음 날 다시

    # ── 메일 인증 (§3.6) ────────────────────────────────────────────────────
    def test_unverified_is_held_at_verify(self):
        url, _ = self.post("/signup", name="성연", email="me@example.com", pw="password1", phone=ME)
        self.assertEqual(self.mails[0][0], "me@example.com")
        for path in ("/home", "/devices", "/settings"):
            self.assertTrue(self.web.open(self.url + path).geturl().endswith("/verify"), path)
        url, _ = self.post("/devices/code")                                  # 연동 코드도 못 만든다
        self.assertTrue(url.endswith("/verify"))
        self.assertIsNone(S.q("select * from pair_codes", one=True))

    def test_wrong_code_counts_down_then_dies(self):
        self.post("/signup", name="성연", email="me@example.com", pw="password1", phone=ME)
        good = self.last_code()
        bad = "000000" if good != "000000" else "111111"
        url, body = self.post("/verify", code=bad)
        self.assertIn("4번 더", body)
        for _ in range(4):
            url, body = self.post("/verify", code=bad)
        self.assertIn("무효", body)
        url, body = self.post("/verify", code=good)                          # 맞는 코드도 이제 안 된다
        self.assertIn("/verify", url)
        self.assertIn("만료", body)

    def test_resend_waits_and_replaces_the_code(self):
        self.post("/signup", name="성연", email="me@example.com", pw="password1", phone=ME)
        old = self.last_code()
        self.post("/verify/resend")
        self.assertEqual(len(self.mails), 1)                                 # 60초 안에는 안 보낸다
        S.q("update email_codes set sent=sent-61")
        url, body = self.post("/verify/resend")
        self.assertEqual(len(self.mails), 2)
        self.assertIn("새 코드를 보냈어요", body)
        if self.last_code() != old:
            url, _ = self.post("/verify", code=old)                          # 이전 코드는 무효
            self.assertIn("/verify", url)
        url, _ = self.post("/verify", code=self.last_code())
        self.assertTrue(url.endswith("/home"))

    def test_unverified_signup_can_be_taken_over_and_expires(self):
        # 남의 주소로 가입해 두고 인증 안 해도 그 주소를 막지 못한다
        self.post("/signup", name="남", email="me@example.com", pw="password1", phone=MOM)
        self.web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.signup()
        self.assertEqual(S.q("select count(*) n from users", one=True)["n"], 1)
        # 인증한 계정 주소로는 다시 가입할 수 없다
        self.post("/logout")
        url, body = self.post("/signup", name="또", email="me@example.com", pw="password1", phone=ME)
        self.assertIn("이미 가입된", body)
        # 인증하지 않은 계정은 하루 뒤 지워진다
        self.post("/signup", name="새", email="new@example.com", pw="password1", phone=ME)
        S.tick(time.time() + 86401)
        self.assertIsNone(S.q("select * from users where email='new@example.com'", one=True))
        self.assertIsNotNone(S.q("select * from users where email='me@example.com'", one=True))

    def test_mail_failure_and_daily_cap_say_so(self):
        S.send_mail = lambda to, subj, text: "error: 인증 실패"
        url, body = self.post("/signup", name="성연", email="me@example.com", pw="password1", phone=ME)
        self.assertIn("메일을 보내지 못했어요", body)
        self.assertNotIn("초 뒤 가능", body)                                   # 못 보냈으면 바로 다시 보낼 수 있다
        S.send_mail = lambda to, subj, text: self.mails.append((to, subj, text)) or "sent"
        S.count("mail:" + S.today(), S.MAIL_DAILY_CAP)
        url, body = self.post("/verify/resend")
        self.assertIn("더 보낼 수 없어요", body)
        self.assertEqual(self.mails, [])

    def test_real_mail_sender_names_bad_login(self):
        # Gmail 은 PLAIN 535 뒤 LOGIN 재시도에서 말없이 끊는다 — 진짜 이유(535)가 보여야 한다
        import smtplib
        from unittest import mock

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def ehlo(self): pass
            def auth_plain(self): return ""
            def auth(self, mech, obj):
                assert mech == "PLAIN"
                raise smtplib.SMTPAuthenticationError(535, b"BadCredentials")
        with mock.patch.dict("os.environ", {"SMTP_USER": "a@gmail.com", "SMTP_PASSWORD": "x"}), \
                mock.patch.object(smtplib, "SMTP_SSL", FakeSMTP):
            self.assertIn("로그인 거절(535)", self._real_mail("b@example.com", "s", "t"))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIn("SMTP 환경 변수", self._real_mail("b@example.com", "s", "t"))

    def test_existing_accounts_count_as_verified_after_upgrade(self):
        import sqlite3
        db = Path(self.tmp.name) / "old.db"
        c = sqlite3.connect(db)
        c.execute("create table users(id integer primary key, email text unique not null, name text not null, "
                  "phone text not null, pw_hash text not null, salt text not null, config text not null, "
                  "created real not null)")
        c.execute("insert into users values(1,'old@example.com','옛',?,'h','s','{}',123.0)", (ME,))
        c.commit(); c.close()
        S.CONN.close()
        S.init(db, self.url, secret=b"test")
        self.assertEqual(S.q("select verified from users", one=True)["verified"], 123.0)

    # ── 관리자 (§5.1b) ──────────────────────────────────────────────────────
    def admin_env(self, pw="1234"):
        from unittest import mock
        S.ADMIN_FAILS.clear()
        return mock.patch.dict("os.environ", {"ADMIN_PASSWORD": pw})

    def test_admin_closed_without_password(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get("/admin")
            self.assertEqual(cm.exception.code, 404)

    def test_admin_login_lockout_and_separation(self):
        token = self.signup_and_pair()                                       # 사용자로 로그인한 상태
        with self.admin_env():
            self.assertIn("관리자 로그인", self.get("/admin"))                 # 사용자 쿠키로는 못 들어간다
            for _ in range(5):
                url, body = self.post("/admin/login", id="admin", pw="wrong")
            url, body = self.post("/admin/login", id="admin", pw="1234")     # 잠긴 동안은 맞아도 안 된다
            self.assertIn("너무 많이 틀렸어요", body)
            S.ADMIN_FAILS.clear()
            url, body = self.post("/admin/login", id="admin", pw="1234")
            self.assertIn("오늘 문자", body)
            self.assertIn("me@example.com", body)
            self.assertIn("010-****-2222", body)                             # 전화는 가운데를 가린다
            self.assertNotIn(ME, body)
        with self.admin_env("changed"):                                      # 비밀번호를 바꾸면 세션이 끊긴다
            self.assertIn("관리자 로그인", self.get("/admin"))

    def test_admin_detail_hides_settings_and_deletes_everything(self):
        token = self.signup_and_pair()
        self.post("/contacts/add", name="엄마", phone=MOM)
        self.add_rule(state="empty", kind="hold", minutes="30")
        self.api("/api/state", {"state": "empty", "since": time.time() - 3600}, token)
        S.tick(time.time())                                                  # 나만의 상황 알림이 실제로 생긴다
        self.assertIsNotNone(S.q("select * from alerts where kind='rule'", one=True))
        uid = S.q("select id from users", one=True)["id"]
        with self.admin_env():
            self.post("/admin/login", id="admin", pw="1234")
            body = self.get(f"/admin/u/{uid}")
            self.assertIn("MacBook", body)
            self.assertIn("나만의 상황", body)                                # 알림이 있었다는 것과 종류만
            for private in ("엄마", MOM, "수면", "빈 방이 30분"):
                self.assertNotIn(private, body)                              # 생활 패턴은 관리자도 안 본다
            self.post(f"/admin/u/{uid}/delete")
        for t in ("users", "devices", "events", "alerts"):
            self.assertEqual(S.q(f"select count(*) n from {t}", one=True)["n"], 0, t)
        self.assertEqual(self.api("/api/state", {"state": "empty", "since": time.time()}, token)[0], 401)

    # ── 나만의 상황 (§3.5) ──────────────────────────────────────────────────
    def add_rule(self, **kw):
        form = {"id": "new", "when": "always", "state": "empty", "kind": "hold", "minutes": "30",
                "notify": "me", "start": "22:00", "end": "06:00", **kw}
        url, body = self.post("/rules/save", **form)
        self.assertIn("저장했어요", body)
        return S.config(S.q("select * from users limit 1", one=True))["rules"][-1]

    def test_rule_text_reads_as_a_sentence(self):
        r = {"when": "custom", "start": "07:00", "end": "10:00", "state": "motion", "kind": "absent", "minutes": 120}
        self.assertEqual(S.rule_text(r), "07:00–10:00에 움직임이 2시간 없으면")
        self.assertEqual(S.rule_text({"when": "always", "state": "motion", "kind": "hold", "minutes": 0}),
                         "움직임이 나타나면")

    def test_hold_rule_fires_once_and_rearms(self):
        token = self.signup_and_pair()
        r = self.add_rule(state="empty", kind="hold", minutes="30")
        self.api("/api/state", {"state": "empty", "since": time.time() - 3600}, token)
        S.tick(time.time())
        S.tick(time.time())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("빈 방이 30분 계속되면", self.sent[0][1])
        self.api("/api/state", {"state": "motion", "since": time.time()}, token)
        S.tick(time.time())                                  # 거짓 → 닫힘
        self.api("/api/state", {"state": "empty", "since": time.time() - 3600}, token)
        S.tick(time.time())
        self.assertEqual(len(self.sent), 2)                  # 다시 무장했다
        # 끄면 열린 건이 닫히고 더 보내지 않는다
        self.post("/rules/toggle", id=r["id"])
        S.tick(time.time())
        self.assertIsNone(S.q("select * from alerts where rule=? and resolved is null", (r["id"],), one=True))

    def test_custom_window_outside_is_false(self):
        token = self.signup_and_pair()
        h = datetime.now(home.TZ).hour
        self.add_rule(when="custom", start=f"{(h + 2) % 24:02d}:00", end=f"{(h + 3) % 24:02d}:00", minutes="0")
        self.api("/api/state", {"state": "empty", "since": time.time() - 3600}, token)
        S.tick(time.time())
        self.assertEqual(self.sent, [])

    def test_absent_rule_escalates_to_contacts(self):
        token = self.signup_and_pair()
        self.awake_now()                                     # confirm_min = 1
        self.post("/contacts/add", name="엄마", phone=MOM)
        self.add_rule(state="motion", kind="absent", minutes="60", notify="escalate")
        S.q("update devices set created=? ", (time.time() - 7200,))   # 연동 2시간 전, 움직임 본 적 없음
        self.api("/api/state", {"state": "static", "since": time.time() - 10}, token)
        S.tick(time.time())
        self.assertEqual([n for n, _ in self.sent], [[ME]])
        self.assertIn("/c/", self.sent[0][1])
        self.assertIn("움직임이 1시간 없으면", self.get("/home"))     # 확인 대기 카드
        S.tick(time.time() + 61)
        self.assertEqual([n for n, _ in self.sent], [[ME], [MOM]])

    def test_bad_rule_is_rejected(self):
        self.signup_and_pair()
        url, body = self.post("/rules/save", id="new", when="custom", start="10:00", end="10:00",
                              state="empty", kind="hold", minutes="5", notify="me")
        self.assertIn("직접 지정한 시간", body)
        url, body = self.post("/rules/save", id="new", when="always", state="sleeping",
                              kind="hold", minutes="5", notify="me")
        self.assertIn("모르는 값", body)

    # ── 집 쪽: 제어판 연동 + 엔진 전송 (§4.3) ─────────────────────────────
    def test_local_pair_and_sender(self):
        self.signup()
        self.post("/devices/code")
        code = re.search(r'class="code">(\d{6})<', self.get("/devices")).group(1)
        store = Path(self.tmp.name) / "home_service.json"
        old, home.CLOUD_URL = home.CLOUD_URL, self.url
        try:
            self.assertEqual(home.pair("12", "Mac", store), "6자리 숫자를 넣어 주세요")
            self.assertIsNone(home.pair(code, "Mac", store))
            self.assertIn("만료", home.pair(code, "Mac", store))          # 1회용
        finally:
            home.CLOUD_URL = old
        saved = home.load(store)
        self.assertEqual(saved["url"], self.url)                         # 주소를 함께 적는다
        snd = home.Sender.__new__(home.Sender)                           # 스레드 없이 send 만
        snd.path, snd.last = store, {}
        snd.send({"state": "static", "since": time.time() - 60, "led": True})
        self.assertTrue(snd.last["ok"])
        self.assertIn("켜짐", self.get("/home"))
        S.q("delete from devices")                                       # 홈페이지에서 해제
        snd.send({"state": "static", "since": time.time() - 60, "led": True})
        self.assertEqual(snd.last["error"], "홈페이지에서 해제된 기기입니다")
        home.unpair(store)
        snd.send({"state": "empty", "since": time.time(), "led": None})
        self.assertEqual(snd.last, {})                                   # 연동 없으면 보내지 않는다


if __name__ == "__main__":
    unittest.main()
