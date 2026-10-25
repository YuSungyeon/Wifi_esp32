"""문자 알림 규칙 — 언제 보내고 언제 **안** 보내는가.

잘못 울리면 돈이 나가고 실제 전화로 간다. 여기서 고정하는 것은 설계 정본
`doc/realtime-inference.md` §10 의 네 가지다: 올라가는 모서리에서 한 번만,
`suspended` 는 `absent` 시계를 되돌린다, 하루 상한, 보내지 않고 기록만.
"""
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime import sms  # noqa: E402

M = 60.0
T0 = time.time()


def cfg(rules, **kw):
    return {**sms.DEFAULT, "enabled": True, "to": ["01012341234"], "from": "0507",
            "api_key": "k", "api_secret": "s", "rules": rules, **kw}


def rule(kind, state, minutes, **kw):
    return {"id": f"{kind}-{state}", "on": True, "kind": kind,
            "state": state, "minutes": minutes, "repeat_min": 0, **kw}


class WatcherCase(unittest.TestCase):
    """발송을 가짜로 바꾸고 기록은 임시 파일로 — 실제 번호로 나가지 않는다."""

    def setUp(self):
        self.t = T0
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "sms_log.jsonl"
        self.addCleanup(self.tmp.cleanup)
        patch = mock.patch.object(sms, "send", lambda c, t: (True, "sent 1/1"))
        self.sent = patch.start()
        self.addCleanup(patch.stop)

    def watcher(self, conf):
        return sms.Watcher(conf, session="live", now=self.t, log_path=self.log)

    def run_states(self, w, script, step=0.3):
        """`[(상태, 초), …]` 를 step 간격으로 흘리고 보낸 문장 목록을 돌려준다.

        시계는 테스트가 들고 있다 — 이어서 부르면 그 뒤부터 흐른다.
        """
        fired = []
        for state, dur in script:
            end = self.t + dur
            while self.t < end:
                fired += [r["rule"] for r in w.check(state, self.t)]
                self.t += step
        return fired


class HoldTest(WatcherCase):
    def test_fires_at_the_minute_not_before(self):
        w = self.watcher(cfg([rule("hold", "suspended", 10)]))
        self.assertEqual(self.run_states(w, [("suspended", 9 * M)]), [])
        self.assertEqual(self.run_states(w, [("suspended", 2 * M)]), ["판단 정지 10분"])

    def test_stays_quiet_while_the_condition_holds(self):
        w = self.watcher(cfg([rule("hold", "suspended", 10)]))
        self.assertEqual(len(self.run_states(w, [("suspended", 60 * M)])), 1)

    def test_rearms_after_the_state_leaves(self):
        w = self.watcher(cfg([rule("hold", "suspended", 10)]))
        fired = self.run_states(w, [("suspended", 11 * M), ("empty", 1 * M),
                                    ("suspended", 11 * M)])
        self.assertEqual(len(fired), 2)

    def test_zero_minutes_is_an_immediate_alert(self):
        w = self.watcher(cfg([rule("hold", "motion", 0)]))
        self.assertEqual(self.run_states(w, [("empty", M), ("motion", 1.0)]),
                         ["움직임 발생"])

    def test_a_brief_other_state_resets_the_clock(self):
        w = self.watcher(cfg([rule("hold", "static", 30)]))
        self.assertEqual(self.run_states(w, [("static", 29 * M), ("empty", 1.0),
                                             ("static", 29 * M)]), [])


class AbsentTest(WatcherCase):
    def test_fires_after_the_state_is_unseen(self):
        w = self.watcher(cfg([rule("absent", "motion", 30)]))
        self.assertEqual(self.run_states(w, [("static", 20 * M), ("empty", 9 * M)]), [])
        self.assertEqual(self.run_states(w, [("empty", 2 * M)]), ["움직임 없음 30분"])

    def test_survives_churn_between_other_states(self):
        """`hold` 와 다른 점 — 정지·빈 방을 오가도 '움직임 없음' 은 계속 쌓인다."""
        w = self.watcher(cfg([rule("absent", "motion", 30)]))
        script = [("static", 5 * M), ("empty", 5 * M)] * 4
        self.assertEqual(self.run_states(w, script), ["움직임 없음 30분"])

    def test_the_state_appearing_resets_the_clock(self):
        w = self.watcher(cfg([rule("absent", "motion", 30)]))
        self.assertEqual(self.run_states(w, [("static", 29 * M), ("motion", 1.0),
                                             ("static", 29 * M)]), [])

    def test_suspended_rewinds_the_clock(self):
        """RX 가 끊긴 구간을 '움직임이 없었다' 로 세면 보드가 죽은 날 사람 걱정을 시킨다(§10.3)."""
        w = self.watcher(cfg([rule("absent", "motion", 30)]))
        self.assertEqual(self.run_states(w, [("static", 25 * M), ("suspended", 20 * M),
                                             ("static", 25 * M)]), [])
        self.assertEqual(self.run_states(w, [("static", 6 * M)]), ["움직임 없음 30분"])


class RepeatTest(WatcherCase):
    def test_repeat_resends_on_the_interval(self):
        w = self.watcher(cfg([rule("hold", "suspended", 10, repeat_min=20)]))
        self.assertEqual(len(self.run_states(w, [("suspended", 71 * M)])), 4)

    def test_without_repeat_it_sends_once(self):
        w = self.watcher(cfg([rule("hold", "suspended", 10)]))
        self.assertEqual(len(self.run_states(w, [("suspended", 71 * M)])), 1)


class GuardTest(WatcherCase):
    def test_disabled_sends_nothing(self):
        w = self.watcher(cfg([rule("hold", "motion", 0)], enabled=False))
        self.assertEqual(self.run_states(w, [("motion", M)]), [])
        self.assertFalse(self.log.exists())

    def test_a_rule_turned_off_sends_nothing(self):
        r = rule("hold", "motion", 0)
        self.assertEqual(self.run_states(self.watcher(cfg([{**r, "on": False}])),
                                         [("motion", M)]), [])

    def test_daily_cap_stops_sending_and_says_so(self):
        w = self.watcher(cfg([rule("hold", "motion", 0, repeat_min=1)], daily_cap=2))
        self.run_states(w, [("motion", 5 * M)])
        rows = sms.log_rows(self.log, limit=99)
        self.assertEqual([r["status"] for r in rows][::-1][:3], ["sent", "sent", "capped"])
        self.assertEqual(sms.sent_today(self.log, T0), 2)

    def test_dry_run_records_but_never_sends(self):
        with mock.patch.object(sms, "send", side_effect=AssertionError("보내면 안 된다")):
            w = self.watcher(cfg([rule("hold", "motion", 0)], dry_run=True))
            self.assertEqual(self.run_states(w, [("motion", M)]), ["움직임 발생"])
        self.assertEqual([r["status"] for r in sms.log_rows(self.log)], ["dry"])

    def test_a_failed_send_is_recorded_not_swallowed(self):
        with mock.patch.object(sms, "send", lambda c, t: (False, "error: 발신번호 미등록")):
            w = self.watcher(cfg([rule("hold", "motion", 0)]))
            self.run_states(w, [("motion", M)])
        row = sms.log_rows(self.log)[0]
        self.assertFalse(row["ok"])
        self.assertIn("발신번호 미등록", row["status"])
        self.assertEqual(sms.sent_today(self.log, T0), 0)   # 실패는 상한을 먹지 않는다


class ReloadTest(WatcherCase):
    """측정 중에 `알림 끄기` 를 눌렀는데 계속 가면 끌 방법이 없다."""

    def test_turning_it_off_mid_run_stops_the_next_alert(self):
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d) / "solapi.json"
            sms.save(cfg([rule("hold", "motion", 0, repeat_min=1)]), conf)
            w = sms.Watcher(session="live", now=self.t, log_path=self.log, cfg_path=conf)
            self.assertEqual(self.run_states(w, [("motion", 2 * M)]), ["움직임 발생"] * 2)
            sms.save({**sms.load(conf), "enabled": False}, conf)
            self.assertEqual(self.run_states(w, [("motion", 5 * M)]), [])

    def test_an_explicit_config_is_not_reloaded(self):
        w = self.watcher(cfg([rule("hold", "motion", 0)]))
        self.assertFalse(w._reload)


class WordingTest(unittest.TestCase):
    def test_rule_text_reads_as_a_sentence(self):
        for r, want in ((rule("absent", "motion", 30), "움직임 없음 30분"),
                        (rule("hold", "suspended", 10), "판단 정지 10분"),
                        (rule("hold", "motion", 0), "움직임 발생")):
            with self.subTest(want=want):
                self.assertEqual(sms.rule_text(r), want)

    def test_message_fits_a_short_sms(self):
        for state in sms.STATES:
            for kind in sms.KINDS:
                text = sms.message(rule(kind, state, 120), T0)
                with self.subTest(state=state, kind=kind):
                    self.assertLessEqual(len(text.encode("euc-kr")), 90)

    def test_digits_strips_hyphens(self):
        self.assertEqual(sms.digits("010-1234-5678"), "01012345678")

    def test_ready_names_what_is_missing(self):
        self.assertIn("API 키", sms.ready(sms.DEFAULT))
        self.assertIn("발신", sms.ready({**sms.DEFAULT, "api_key": "k", "api_secret": "s"}))
        self.assertIn("받는", sms.ready({**sms.DEFAULT, "api_key": "k", "api_secret": "s",
                                        "from": "0507"}))


class IsolationTest(unittest.TestCase):
    """테스트가 실제 설정·기록 파일을 건드리면 안 된다 — 운영자 번호와 키가 거기 있다."""

    def test_paths_are_not_bound_at_import(self):
        with tempfile.TemporaryDirectory() as d:
            store, log = Path(d) / "c.json", Path(d) / "l.jsonl"
            with mock.patch.object(sms, "STORE", store), mock.patch.object(sms, "LOG", log):
                sms.save({**sms.DEFAULT, "api_key": "x"})
                sms.append_log({"wall": T0, "status": "sent"})
                self.assertEqual(sms.load()["api_key"], "x")
                self.assertEqual(sms.sent_today(now=T0), 1)
            self.assertTrue(store.is_file() and log.is_file())

    def test_a_broken_file_falls_back_to_off(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text("{ not json", encoding="utf-8")
            self.assertFalse(sms.load(p)["enabled"])

    def test_missing_keys_are_filled_from_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.json"
            p.write_text(json.dumps({"enabled": True}), encoding="utf-8")
            self.assertEqual(sms.load(p)["daily_cap"], 20)


class ReplayTest(unittest.TestCase):
    """재생은 지난 상태를 다시 흘린다 — 여기서 문자가 나가면 과거를 지금 일처럼 알린다.

    `step(now=...)` 가 주어지면 재생이다(`evaluate_transitions`·`ui.py --speed`).
    실시간만 `now=None` 으로 부른다.
    """

    def engine(self, d):
        from realtime.engine import Engine
        return Engine(Path(d), model_path=None, stage1=None)

    def test_replay_never_alerts(self):
        with tempfile.TemporaryDirectory() as d:
            eng = self.engine(d)
            with mock.patch.object(sms, "Watcher", side_effect=AssertionError("재생이 알림을 켰다")):
                eng.step(now=0.0)
            self.assertIsNone(eng._sms)

    def test_live_checks_every_step(self):
        with tempfile.TemporaryDirectory() as d:
            eng = self.engine(d)
            with mock.patch.object(sms, "Watcher") as W:
                eng.step()
                eng.step()
            W.assert_called_once()                       # 감시자는 하나, 호출은 매번
            self.assertEqual(W.return_value.check.call_count, 2)


if __name__ == "__main__":
    unittest.main()
