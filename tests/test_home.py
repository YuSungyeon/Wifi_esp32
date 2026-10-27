"""집 상태 서비스의 시간 규칙 — 자정을 넘는 수면 시간과 응급 시계 멈춤.

설계 정본 `doc/plans/2026-10-23-home-service.md` §3.1.
"""
import sys
import unittest.mock
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime import home  # noqa: E402


def at(h, m=0, day=24):
    return datetime(2026, 10, day, h, m, tzinfo=home.TZ).timestamp()


def cfg(start, end):
    return {**home.DEFAULT, "sleep_start": start, "sleep_end": end}


class SleepWindow(unittest.TestCase):
    def test_same_day_window(self):
        c = cfg("00:00", "07:00")
        self.assertTrue(home.in_sleep(at(0), c))
        self.assertTrue(home.in_sleep(at(6, 59), c))
        self.assertFalse(home.in_sleep(at(7), c))

    def test_window_crossing_midnight(self):
        c = cfg("23:00", "06:00")
        self.assertTrue(home.in_sleep(at(23, 30), c))
        self.assertTrue(home.in_sleep(at(2), c))
        self.assertFalse(home.in_sleep(at(12), c))

    def test_equal_start_end_means_no_sleep(self):
        self.assertFalse(home.in_sleep(at(3), cfg("03:00", "03:00")))

    def test_last_boundary_is_yesterday_when_not_reached_yet(self):
        self.assertEqual(home.last_boundary(at(6), "07:00"), at(7, day=23))
        self.assertEqual(home.last_boundary(at(8), "07:00"), at(7))


class EmergencyClock(unittest.TestCase):
    def test_clock_stops_during_sleep_and_restarts_at_wake(self):
        c = cfg("00:00", "07:00")
        fell = at(23, 50, day=23)                       # 수면 10분 전에 쓰러짐
        self.assertEqual(home.emergency_held(at(3), fell, c), 0)
        self.assertEqual(home.emergency_held(at(7, 30), fell, c), 30 * 60)

    def test_awake_static_counts_from_its_own_start(self):
        c = cfg("00:00", "07:00")
        self.assertEqual(home.emergency_held(at(15), at(14), c), 3600)


class LedTable(unittest.TestCase):
    """조명 규칙 표(§3.2) 그대로."""

    c = cfg("00:00", "07:00")          # motion_wake_s 5, sleep_off_min 10

    def test_awake(self):
        for st in ("static", "motion"):
            self.assertIs(home.led(st, 1, at(15), self.c, False), True)
        self.assertIs(home.led("empty", 1, at(15), self.c, True), False)

    def test_unknown_states_keep_the_light(self):
        for st in ("deciding", "suspended"):
            self.assertIs(home.led(st, 999, at(15), self.c, True), True)
            self.assertIsNone(home.led(st, 999, at(15), self.c, None))

    def test_sleep_toss_does_not_turn_on(self):
        self.assertIs(home.led("motion", 2, at(3), self.c, False), False)    # 뒤척임 2초
        self.assertIs(home.led("motion", 6, at(3), self.c, False), True)     # 5초 넘게

    def test_sleep_static_turns_off_after_x_minutes_from_sleep_start(self):
        self.assertIs(home.led("static", 9 * 60, at(1), self.c, True), True)
        self.assertIs(home.led("static", 11 * 60, at(1), self.c, True), False)
        # 23시부터 가만히 있었어도 수면 시작(0시)부터 센다 — 0:05 엔 아직 켜 둔다
        self.assertIs(home.led("static", 65 * 60, at(0, 5), self.c, True), True)


class LedByteTest(unittest.TestCase):
    """reader 가 싱크로 쓰는 바이트 (realtime-uplink.md "조명 명령")."""

    def test_status_to_byte(self):
        import json
        import tempfile
        sys.path.insert(0, str(ROOT / "scripts"))
        from csi_serial_reader import led_byte
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "realtime_status.json"
            self.assertIsNone(led_byte(Path(d)))                         # 실시간이 안 돈다
            for v, want in ((True, b"1"), (False, b"0"), (None, None)):
                p.write_text(json.dumps({"led": v}))
                self.assertEqual(led_byte(Path(d)), want)
            p.write_text('{"led": tr')                                  # 쓰는 중인 파일
            self.assertIsNone(led_byte(Path(d)))


class SenderTest(unittest.TestCase):
    """판단마다(0.3초) offer 해도 상태가 바뀔 때만 바로 보낸다. 나머지는 60초 생존 신호가 맡는다."""

    def test_sends_on_change_only(self):
        import time
        sent = []
        with unittest.mock.patch.object(home.Sender, "send", lambda self, body: sent.append(body["state"])):
            snd = home.Sender()
            for state in ("static", "static", "static", "motion", "motion"):
                snd.offer(state, 0.0)
                time.sleep(0.05)
        self.assertEqual(sent, ["static", "motion"])


class ReplayTest(unittest.TestCase):
    """재생은 지난 상태를 다시 흘린다 — 여기서 보내면 과거를 지금 일처럼 알린다.

    `step(now=...)` 가 주어지면 재생이다(`evaluate_transitions`·`ui.py --speed`).
    실시간만 `now=None` 으로 부른다. (옛 로컬 문자 알림의 같은 테스트를 옮겨 왔다.)
    """

    def engine(self, d):
        from realtime.engine import Engine
        return Engine(Path(d), model_path=None, stage1=None)

    def test_replay_never_sends(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            eng = self.engine(d)
            with unittest.mock.patch.object(home, "Sender", side_effect=AssertionError("재생이 전송을 켰다")):
                eng.step(now=0.0)
            self.assertIsNone(eng._cloud)

    def test_live_offers_every_step(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            eng = self.engine(d)
            with unittest.mock.patch.object(home, "Sender") as snd:
                snd.return_value.last = {}
                eng.step()
                eng.step()
            snd.assert_called_once()                     # 전송 스레드는 하나, 건네기는 매번
            self.assertEqual(snd.return_value.offer.call_count, 2)


if __name__ == "__main__":
    unittest.main()
