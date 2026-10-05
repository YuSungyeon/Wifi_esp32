"""실시간 판단 — 상태 기계와 화면 보조 함수.

모델·스트림은 실제 세션으로 확인하고(재생), 여기서는 순수 로직만 고정한다.
"""
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import meshsense_gui as gui  # noqa: E402
from realtime.state import Reading, StateMachine  # noqa: E402
from realtime.ui import hms, render, render_plain  # noqa: E402


def feed(sm, t0, n, probs, n_rx=3, step=0.3):
    last = None
    for i in range(n):
        ev = sm.update(Reading(t=t0 + i * step, probs=probs, n_rx=n_rx))
        last = ev or last
    return last


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.sm = StateMachine()

    def test_starts_undecided(self):
        self.assertEqual(self.sm.state, "deciding")

    def test_low_confidence_never_changes_state(self):
        feed(self.sm, 0.0, 200, (0.4, 0.35, 0.25))
        self.assertEqual(self.sm.state, "deciding")

    def test_motion_is_confirmed_in_about_a_second(self):
        ev = feed(self.sm, 0.0, 5, (0.02, 0.03, 0.95))
        self.assertEqual(self.sm.state, "motion")
        self.assertLessEqual(ev.t, 1.5)

    def test_rest_states_need_a_longer_hold_than_motion(self):
        feed(self.sm, 0.0, 30, (0.9, 0.05, 0.05))       # 9초 — 15초 미만
        self.assertEqual(self.sm.state, "deciding")
        feed(self.sm, 9.0, 30, (0.9, 0.05, 0.05))       # 누적 18초
        self.assertEqual(self.sm.state, "empty")

    def test_a_brief_confident_blip_does_not_flip_a_rest_state(self):
        feed(self.sm, 0.0, 60, (0.9, 0.05, 0.05))
        self.assertEqual(self.sm.state, "empty")
        feed(self.sm, 20.0, 10, (0.05, 0.9, 0.05))      # 3초짜리 반대 판단
        self.assertEqual(self.sm.state, "empty")

    def test_empty_to_static_without_motion_needs_a_long_agreement(self):
        feed(self.sm, 0.0, 60, (0.9, 0.05, 0.05))
        feed(self.sm, 20.0, 100, (0.05, 0.9, 0.05))     # 30초 — 60초 기준 미만
        self.assertEqual(self.sm.state, "empty")
        feed(self.sm, 50.0, 120, (0.05, 0.9, 0.05))     # 누적 66초
        self.assertEqual(self.sm.state, "static")

    def test_motion_interrupts_a_rest_state_quickly(self):
        feed(self.sm, 0.0, 60, (0.9, 0.05, 0.05))
        feed(self.sm, 20.0, 6, (0.02, 0.03, 0.95))
        self.assertEqual(self.sm.state, "motion")

    def test_two_rx_raises_the_threshold(self):
        sm = StateMachine(conf=0.7, degraded_conf=0.85)
        feed(sm, 0.0, 100, (0.8, 0.15, 0.05), n_rx=2)   # 0.8 < 0.85
        self.assertEqual(sm.state, "deciding")
        self.assertIn("RX 2대", sm.note)
        feed(sm, 30.0, 100, (0.9, 0.05, 0.05), n_rx=2)
        self.assertEqual(sm.state, "empty")

    def test_one_rx_suspends_and_reports_the_last_decision(self):
        feed(self.sm, 0.0, 60, (0.9, 0.05, 0.05))
        feed(self.sm, 20.0, 2, (0.9, 0.05, 0.05), n_rx=1)
        self.assertEqual(self.sm.state, "suspended")
        self.assertIn("빈 방" if "빈 방" in self.sm.note else "empty", self.sm.note)

    def test_pending_candidate_counts_down(self):
        feed(self.sm, 0.0, 10, (0.9, 0.05, 0.05))       # 3초 경과, 15초 필요
        cand, remaining = self.sm.pending
        self.assertEqual(cand, "empty")
        self.assertAlmostEqual(remaining, 15.0 - 2.7, places=1)


class RenderTest(unittest.TestCase):
    VIEW = {"state": "motion", "held_s": 134.0, "confidence": 0.87, "note": "",
            "candidate": None, "remaining_s": 0.0, "threshold": 0.7, "t": 200.0,
            "probs": {"empty": .05, "static": .08, "motion": .87}, "n_rx": 3,
            "hz": 99.8, "session": "213039_motion_s41", "window_s": 30.0,
            "events": [{"t": 66.0, "from": "deciding", "to": "motion", "confidence": .9}]}

    def test_screen_has_the_requested_height_and_leads_with_the_state(self):
        out = render(self.VIEW, width=62, height=24).splitlines()
        self.assertEqual(len(out), 24)
        self.assertIn("움직임", "\n".join(out[1:10]))

    def test_state_block_keeps_its_whitespace(self):
        block = render(self.VIEW, height=24).splitlines()[1:10]
        blank = sum(1 for line in block if not line.strip("│ "))
        self.assertGreaterEqual(blank, 4)

    def test_short_terminal_drops_history_not_the_state_block(self):
        out = render(self.VIEW, height=13).splitlines()
        self.assertEqual(len(out), 13)
        self.assertIn("움직임", "\n".join(out))
        self.assertNotIn("판단 중 →", "\n".join(out))

    def test_deciding_shows_the_candidate_and_remaining_time(self):
        view = dict(self.VIEW, state="deciding", candidate="static", remaining_s=9.0)
        self.assertIn("정지", render(view))
        self.assertIn("9초", render(view))

    def test_plain_mode_is_a_single_line(self):
        line = render_plain(self.VIEW)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("움직임", line)
        self.assertIn("99.8", line)

    def test_hms_switches_to_minutes(self):
        self.assertEqual(hms(45), "45초")
        self.assertEqual(hms(134), "2분 14초")


class ImmediateClassificationTest(unittest.TestCase):
    def test_low_probability_class_is_shown_on_first_prediction(self):
        sm = StateMachine(immediate=True)
        ev = sm.update(Reading(30, (.4, .35, .25), 3))
        self.assertEqual(sm.state, 'empty')
        self.assertEqual(ev.to, 'empty')
        self.assertEqual(sm.confidence, .4)
        self.assertEqual(sm.threshold, 0)
        self.assertEqual(sm.pending, (None, 0))

    def test_class_changes_every_03_seconds_without_hold(self):
        sm = StateMachine(immediate=True)
        for t, probs, expected in [(30, (.4,.3,.3), 'empty'),
                                   (30.3, (.3,.4,.3), 'static'),
                                   (30.6, (.3,.3,.4), 'motion')]:
            self.assertEqual(sm.update(Reading(t, probs, 3)).to, expected)
            self.assertEqual(sm.state, expected)
            self.assertEqual(sm.pending, (None, 0))

    def test_same_class_updates_probability_without_extra_transition(self):
        sm = StateMachine(immediate=True)
        sm.update(Reading(30, (.5,.3,.2), 3))
        self.assertIsNone(sm.update(Reading(30.3, (.6,.2,.2), 3)))
        self.assertEqual(sm.confidence, .6)
        self.assertAlmostEqual(sm.held_s, .3)

    def test_invalid_input_suspends_and_valid_input_recovers_immediately(self):
        sm = StateMachine(immediate=True)
        sm.update(Reading(30, (.5,.3,.2), 3))
        sm.update(Reading(30.3, (0,0,0), 3, valid=False))
        self.assertEqual(sm.state, 'suspended')
        sm.update(Reading(30.6, (.3,.4,.3), 3))
        self.assertEqual(sm.state, 'static')

    def test_two_receivers_cannot_produce_an_instant_class(self):
        sm = StateMachine(immediate=True)
        sm.update(Reading(30, (.99,.005,.005), 2))
        self.assertEqual(sm.state, 'suspended')

    def test_gui_fast_status_does_not_scan_sessions_or_ports(self):
        handler = mock.Mock()
        handler.path = '/api/realtime/state'
        view = {'state':'empty', 'confidence':.4}
        with mock.patch.object(gui, 'current_realtime_view', return_value=view), \
             mock.patch.object(gui, 'session_rows') as sessions, mock.patch.object(gui, 'rescan') as rescan:
            gui.Handler.do_GET(handler)
        handler._json.assert_called_once_with({'realtime': view})
        sessions.assert_not_called()
        rescan.assert_not_called()
        self.assertIn('setInterval(refreshRealtime, 300)', gui.JS)


class TimelineTest(unittest.TestCase):
    def test_spans_fill_the_bar(self):
        events = [{"t": 100.0, "from": "deciding", "to": "empty"},
                  {"t": 2000.0, "from": "empty", "to": "motion"}]
        spans = gui.timeline_spans(events, now=3600.0)
        self.assertAlmostEqual(sum(s["pct"] for s in spans), 100.0, places=2)
        self.assertEqual(spans[-1]["state"], "motion")

    def test_short_run_only_covers_elapsed_time(self):
        spans = gui.timeline_spans([{"t": 60.0, "from": "deciding", "to": "static"}], now=90.0)
        self.assertEqual(spans[0]["covers_s"], 90.0)

    def test_brief_state_keeps_a_visible_minimum_width(self):
        events = [{"t": 10.0, "from": "deciding", "to": "empty"},
                  {"t": 3595.0, "from": "empty", "to": "motion"},
                  {"t": 3597.0, "from": "motion", "to": "empty"}]
        spans = gui.timeline_spans(events, now=3600.0)
        motion = [s for s in spans if s["state"] == "motion"]
        self.assertEqual(len(motion), 1)
        self.assertGreaterEqual(motion[0]["pct"], 0.4)


if __name__ == "__main__":
    unittest.main()
