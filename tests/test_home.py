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


if __name__ == "__main__":
    unittest.main()
