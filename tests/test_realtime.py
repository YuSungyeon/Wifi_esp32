"""실시간 판단 — 상태 기계와 화면 보조 함수.

모델·스트림은 실제 세션으로 확인하고(재생), 여기서는 순수 로직만 고정한다.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import meshsense_gui as gui  # noqa: E402
from realtime.engine import available_models, load_any  # noqa: E402
from realtime.state import Reading, StateMachine  # noqa: E402
from realtime.ui import _cells, hms, render, render_plain, spark  # noqa: E402


def feed(sm, t0, n, probs, n_rx=3, step=0.3):
    last = None
    for i in range(n):
        ev = sm.update(Reading(t=t0 + i * step, probs=probs, n_rx=n_rx))
        last = ev or last
    return last


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.sm = StateMachine(conf=0.7, rest_hold_s=15.0, direct_switch_s=60.0)

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
            "activity": [0.05] * 180 + [0.95] * 20, "activity_step_s": 0.3,
            "activity_window_s": 3.0, "activity_model": "motion3s",
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

    def test_every_row_is_the_same_width_on_screen(self):
        """막대·띠가 들어간 줄만 테두리가 어긋나던 적이 있다 — 표시 폭으로 고정한다."""
        for view in (self.VIEW, dict(self.VIEW, state="deciding", candidate="static",
                                     remaining_s=9.0)):
            widths = {_cells(line) for line in render(view, width=62, height=24).splitlines()}
            self.assertEqual(widths, {62})

    def test_activity_row_appears_and_shows_the_burst(self):
        out = render(self.VIEW, height=24)
        self.assertIn("활동", out)
        self.assertIn("█", out.split("활동")[1].splitlines()[0])

    def test_screen_without_activity_still_renders(self):
        view = {k: v for k, v in self.VIEW.items() if not k.startswith("activity")}
        self.assertEqual(len(render(view, height=24).splitlines()), 24)


class SparkTest(unittest.TestCase):
    def test_quiet_input_stays_low(self):
        out = spark([0.04] * 200, 0.3)
        self.assertTrue(set(out) <= {" ", "▁"}, out)

    def test_a_short_burst_survives_downsampling(self):
        """칸 하나가 여러 창을 덮는다. 평균을 쓰면 3초 움직임이 사라진다 — 최대를 써야 한다."""
        values = [0.04] * 200
        values[100] = 0.95                       # 0.3초짜리 한 창
        self.assertIn("█", spark(values, 0.3))

    def test_missing_values_leave_a_gap(self):
        self.assertEqual(spark([None] * 200, 0.3).strip(), "")

    def test_requested_cell_count_is_exact(self):
        self.assertEqual(len(spark([0.5] * 500, 0.3, cells=24)), 24)

    def test_no_data_is_empty_not_a_crash(self):
        self.assertEqual(spark([], 0.3), "")
        self.assertEqual(spark([0.5], 0.0), "")


class StreamInputTest(unittest.TestCase):
    """실시간이 모델에 넣는 진폭은 **학습 때와 같은 값**이어야 한다.

    한 번 어긋난 적이 있다: 스트림이 공백을 I/Q 성분별로 보간해서, CFO 로 회전하는 성분이
    상쇄돼 진폭이 0 으로 꺼졌다. 30초 창에서는 묻혔지만 3초 창에서는 그 한 줄이 움직임으로
    보였다. 공식 진폭 경로는 **크기**를 보간한다.
    """

    SESSION = ROOT / "mac_collector_output" / "raw" / "20260919" / "181915_static_s10"

    @classmethod
    def setUpClass(cls):
        if not (cls.SESSION / "session.json").is_file():
            raise unittest.SkipTest("수집 세션이 없다")
        import numpy as np

        from model_train.robust.predict import load_raw_amplitude
        from realtime.stream import CsiStream

        cls.np = np
        cls.amp, cls.meta = load_raw_amplitude(cls.SESSION)
        cls.stream = CsiStream(cls.SESSION)
        cls.stream.poll()
        cls.stream.rewind(300)
        for _ in range(200):                      # 세션 가운데 — 공백이 섞인 구간
            cls.stream.advance(30)
        cls.win = cls.stream.latest(300)

    def test_stream_amplitude_matches_the_training_path(self):
        np = self.np
        off = self.win.tx_seq0 - self.meta["common_start_tx_seq"]
        ref = self.amp[off:off + 300]
        both = np.isfinite(ref) & np.isfinite(self.win.amp)
        self.assertGreater(both.sum(), 0.9 * ref.size)
        self.assertLess(float(np.abs(ref[both] - self.win.amp[both]).max()), 1e-4)

    def test_interpolated_gaps_do_not_collapse_the_amplitude(self):
        """I/Q 를 따로 보간하면 여기서 0 근처 값이 나온다 — 진폭에는 물리적 바닥이 있다."""
        np = self.np
        floor = float(np.nanmin(self.amp))
        self.assertGreater(float(np.nanmin(self.win.amp)), 0.5 * floor)


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


class ModelChoiceTest(unittest.TestCase):
    """실시간이 고를 수 있는 체크포인트 — 진폭·위상 어느 쪽이든 같은 방식으로 연다."""

    def setUp(self):
        self.models = available_models()

    def test_deployed_amplitude_model_is_listed_and_marked(self):
        deployed = [m for m in self.models if m["is_deployed"]]
        self.assertEqual(len(deployed), 1)
        self.assertIn(deployed[0]["mode"], ("A", "A-ours"))     # 진폭 모델이어야 실시간 기본이 된다
        self.assertEqual(deployed[0]["window_s"], 15.0)

    def test_phase_checkpoints_are_listed_with_their_window(self):
        phase = [m for m in self.models if m["mode"] == "P"]
        self.assertTrue(phase)
        for m in phase:
            with self.subTest(m["name"]):
                self.assertGreater(m["window_s"], 0)        # 창 길이가 화면에 나와야 고를 수 있다

    def test_every_listed_model_opens_and_reports_its_kind(self):
        for m in self.models:
            with self.subTest(m["name"]):
                bundle, nets, kind = load_any(ROOT / m["path"])
                self.assertIn(kind, ("amp", "phase"))
                self.assertTrue(nets)
                self.assertEqual(bundle["window_frames"], int(m["window_s"] * 100))

    def test_stage1_checkpoint_is_binary_and_short(self):
        from realtime.engine import STAGE1, load_stage1
        if not STAGE1.is_file():
            self.skipTest("1단계 체크포인트가 없다")
        s1 = load_stage1(STAGE1)
        self.assertEqual(s1["window"], 300)              # 3초 — 상태 모델보다 훨씬 짧다
        self.assertEqual(s1["i_motion"], 1)
        self.assertEqual(len(s1["models"]), 3)

    def test_listed_models_carry_their_name_and_note(self):
        """목록에 이름만 뜨면 어느 게 기본이고 어느 게 대조군인지 알 수 없다."""
        for m in self.models:
            with self.subTest(m["path"]):
                self.assertIn("name", m)
                self.assertIn("note", m)

    def test_models_turned_off_do_not_appear(self):
        import model_registry as mr
        from realtime.engine import available_models
        off = next(m["path"] for m in self.models if m["path"] not in mr.PINNED)
        store = {off: {"realtime": False}}
        self.assertNotIn(off, [m["path"] for m in available_models(store=store)])
        self.assertIn(off, [m["path"] for m in available_models(store=store, only_realtime=False)])

    def test_pinned_models_stay_even_if_turned_off(self):
        import model_registry as mr
        from realtime.engine import available_models
        store = {p: {"realtime": False} for p in mr.PINNED}
        listed = [m["path"] for m in available_models(store=store, only_realtime=False)]
        self.assertIn(mr.PINNED[0], listed)

    def test_stage1_is_not_offered_as_a_state_model(self):
        """이진 모델을 상태 모델 자리에 끼우면 화면이 조용히 틀린다 — 목록에서 뺀다."""
        self.assertNotIn("motion3s", [m["name"] for m in self.models])

    def test_unknown_checkpoint_is_refused(self):
        import tempfile

        import torch

        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "weird.pt"
            torch.save({"mode": "X", "state_dicts": []}, p)
            with self.assertRaises(ValueError):
                load_any(p)


class LiveWindowTest(unittest.TestCase):
    """실시간(따라가기)에서 RX 파일은 서로 조금씩 늦는다. 창은 **모든 RX 가 도달한 지점**에서 끝나야
    한다 — 가장 새 프레임까지 잡으면 늦은 RX 의 꼬리가 NaN 이라 판단이 한 번도 나지 않는다."""

    SESSION = ROOT / "mac_collector_output" / "raw" / "20260919" / "181915_static_s10"

    def test_window_ends_where_the_slowest_rx_is(self):
        if not (self.SESSION / "session.json").is_file():
            raise unittest.SkipTest("수집 세션이 없다")
        import numpy as np

        import csi_store as cs
        from realtime.stream import CsiStream

        frames = cs.read_session(self.SESSION)
        s = CsiStream(self.SESSION)
        lag = {101: 0, 102: 7, 103: 40}                 # RX103 이 0.4초 늦게 flush 됐다
        for ri, rx in enumerate((101, 102, 103)):
            s._ingest(ri, frames[rx][3000:3000 + 2000 - lag[rx]])
        win = s.latest(300)
        self.assertIsNotNone(win)
        self.assertTrue(np.isfinite(win.amp).all())      # 꼬리에 NaN 이 없어야 모델이 돈다
        # 프레임 수가 아니라 tx_seq 기준으로 가장 뒤처진 RX 가 창 끝을 정한다
        slowest_end = min(int(frames[rx]["hdr"]["tx_seq"][3000 + 2000 - lag[rx] - 1]) + 1
                          for rx in lag)
        self.assertEqual(win.tx_seq0 + 300, slowest_end)


class LiveSessionTest(unittest.TestCase):
    """실시간 시작은 **지금 쓰이고 있는** 세션만 따라간다 — 옛 녹화를 실시간처럼 보여 준 적이 있다."""

    def test_only_a_growing_newest_session_counts(self):
        import os
        import tempfile
        import time as _t
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp)
            old = raw / "20260920" / "203812_empty_s60"; old.mkdir(parents=True)
            (old / "session.json").write_text("{}"); (old / "device_101.csi").write_bytes(b"x")
            past = _t.time() - 3600
            os.utime(old / "device_101.csi", (past, past))
            self.assertIsNone(gui.live_session_dir(raw))          # 수집 전: 옛 녹화는 안 잡는다
            new = raw / "20261014" / "210500_motion_s61"; new.mkdir(parents=True)
            (new / "session.json").write_text("{}")
            self.assertIsNone(gui.live_session_dir(raw))          # 시작 지연 중: 파일이 아직 없다
            (new / "device_101.csi").write_bytes(b"x")
            self.assertEqual(gui.live_session_dir(raw), new)      # 수집 중
