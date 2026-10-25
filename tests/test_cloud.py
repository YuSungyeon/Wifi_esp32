"""집 상태 서비스 클라우드 — 실제 서버를 띄워 HTTP 로 한 바퀴 돈다.

가입 → 연동 코드 → 기기 연동 → 상태 전송 → 응급(본인 확인 → 미확인 시 지인) → 침입 → 끊김.
설계 정본 `doc/plans/2026-10-23-home-service.md` §3.3·§4·§5.
문자는 `server.send_sms` 를 바꿔 끼워 **실제로 나가지 않는다.**
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
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def tearDown(self):
        S.send_sms = self._real_send
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

    def signup_and_pair(self):
        url, body = self.post("/signup", name="성연", email="me@example.com", pw="password1", phone=ME)
        self.assertTrue(url.endswith("/home"))
        self.assertIn("아직 연결된 집 기기가 없어요", body)
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


if __name__ == "__main__":
    unittest.main()
