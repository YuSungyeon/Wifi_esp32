"""학습 화면의 규칙 — 세션 선택, 날짜 잠금, 소요 시간 추정.

화면이 편해질수록 "학습에 쓴 데이터로 평가하는" 실수가 쉬워진다. 그 방어선을 여기서 고정한다.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from _train_run import PRESETS, estimate_seconds, hms, predict_windows  # noqa: E402
from train_sessions import (  # noqa: E402
    EXCLUDED_SESSIONS, available_eval_dates, check_selection, group_by_date, is_excluded, session_key,
)

SESSIONS = [
    {"key": "20260916_31", "date": "20260916", "label": "motion", "ok": True},
    {"key": "20260916_32", "date": "20260916", "label": "empty", "ok": True},
    {"key": "20260916_33", "date": "20260916", "label": "static", "ok": True},
    {"key": "20260917_38", "date": "20260917", "label": "motion", "ok": True},
    {"key": "20260917_39", "date": "20260917", "label": "static", "ok": True},
    {"key": "20260917_40", "date": "20260917", "label": "empty", "ok": False},
    {"key": "20260919_1", "date": "20260919", "label": "empty", "ok": True},
]
ALL = [s["key"] for s in SESSIONS if s["date"] != "20260919"]


class SelectionTest(unittest.TestCase):
    def test_key_includes_the_date_because_session_ids_repeat(self):
        self.assertEqual(session_key("20260920", "172519_static_s42"), "20260920_42")
        self.assertNotEqual(session_key("20260917", "213701_static_s42"),
                            session_key("20260920", "172519_static_s42"))

    def test_a_balanced_selection_passes(self):
        self.assertEqual(check_selection(ALL, ["20260919"], SESSIONS),
                         ["품질 미달 세션이 있습니다(학습에서 빼기를 권합니다): 20260917_40"])

    def test_training_date_cannot_be_used_for_evaluation(self):
        problems = check_selection(ALL, ["20260916"], SESSIONS)
        self.assertTrue(any("평가에 쓸 수 없습니다" in p for p in problems))

    def test_missing_class_is_rejected(self):
        only_empty = ["20260916_32", "20260917_40"]
        problems = check_selection(only_empty, [], SESSIONS)
        self.assertTrue(any("클래스마다" in p for p in problems))

    def test_one_session_per_class_is_not_enough(self):
        three = ["20260916_31", "20260916_32", "20260916_33"]
        problems = check_selection(three, [], SESSIONS)
        self.assertTrue(any("클래스마다" in p for p in problems))

    def test_empty_selection_is_rejected(self):
        self.assertTrue(check_selection([], ["20260919"], SESSIONS))

    def test_unknown_session_is_reported(self):
        problems = check_selection(ALL + ["20261231_9"], ["20260919"], SESSIONS)
        self.assertTrue(any("없는 세션" in p for p in problems))

    def test_eval_dates_exclude_everything_used_for_training(self):
        self.assertEqual(available_eval_dates(ALL, SESSIONS), ["20260919"])
        self.assertEqual(available_eval_dates([], SESSIONS),
                         ["20260916", "20260917", "20260919"])

    def test_grouping_keeps_every_session(self):
        groups = group_by_date(SESSIONS)
        self.assertEqual(sum(len(v) for v in groups.values()), len(SESSIONS))
        self.assertEqual(list(groups), ["20260916", "20260917", "20260919"])


class PresetTest(unittest.TestCase):
    def test_every_preset_names_a_real_trainer_and_step_count(self):
        for name, p in PRESETS.items():
            with self.subTest(name):
                self.assertIn(p["trainer"], ("phase60", "amp60"))
                self.assertGreater(p["steps"], 0)
                self.assertTrue(p["label"] and p["detail"])

    def test_presets_only_expose_settings_that_measured_better(self):
        """실험에서 나빠진 것(증강·가중치·차분·스펙트럼)은 화면에 올리지 않는다."""
        banned = ("--augment", "--class-weight", "--diff", "--spectral", "--per-rx")
        for name, p in PRESETS.items():
            with self.subTest(name):
                self.assertFalse(set(p["args"]) & set(banned))


class ExclusionTest(unittest.TestCase):
    def test_protocol_violating_session_is_excluded_with_a_reason(self):
        self.assertTrue(is_excluded("20260919_12"))
        self.assertFalse(is_excluded("20260919_11"))
        for key, why in EXCLUDED_SESSIONS.items():
            self.assertRegex(key, r"^\d{8}_\d+$")
            self.assertTrue(why.strip())


class PresetShapeTest(unittest.TestCase):
    def test_stride_follows_the_trainer_not_one_global_default(self):
        """학습기마다 --train-stride 기본값이 다르다 — phase60 은 30, amp60 은 150.
        하나로 뭉뚱그리면 버튼의 예상 시간이 5배 틀린다."""
        from _train_run import preset_shape
        self.assertEqual(preset_shape(PRESETS["balanced"])["train_stride"], 30)
        self.assertEqual(preset_shape(PRESETS["amplitude"])["train_stride"], 150)

    def test_explicit_argument_wins_over_the_default(self):
        from _train_run import preset_shape
        p = dict(PRESETS["balanced"], args=["--window", "6000", "--train-stride", "60"])
        self.assertEqual(preset_shape(p)["train_stride"], 60)


class PredictWindowsTest(unittest.TestCase):
    """버튼에 예상 시간을 쓰려면 창 수를 **학습기가 읽기 전에** 알아야 한다."""

    def test_matches_the_trainer_within_10_percent(self):
        """실측: 15세션 300초, 창 3000 stride 150 → 학습기가 센 창 2687개."""
        got = predict_windows([{"span_s": 300.0}] * 15, window=3000, stride=150)
        self.assertLess(abs(got - 2687) / 2687, 0.10)

    def test_sessions_shorter_than_the_window_contribute_nothing(self):
        self.assertEqual(predict_windows([{"span_s": 10.0}], window=3000, stride=150), 0)

    def test_no_sessions_is_zero_not_a_crash(self):
        self.assertEqual(predict_windows([], window=3000, stride=150), 0)


class EstimateTest(unittest.TestCase):
    def test_estimate_is_within_25_percent_of_measured_runs(self):
        for windows, steps, real in [(11789, 300, 1631), (11789, 600, 2629), (10189, 900, 3415)]:
            with self.subTest(steps=steps):
                got = estimate_seconds(windows, steps, 10, 3)
                self.assertLess(abs(got - real) / real, 0.25)

    def test_amplitude_is_much_faster_and_needs_its_own_rate(self):
        """실측: 진폭 30초 15세션(2687창 × 300스텝, seed 3, epoch 10) 62초.
        위상 계수를 그대로 쓰면 5배 부풀어 버튼이 거짓말을 한다."""
        got = estimate_seconds(2687, 300, 10, 3, trainer="amp60")
        self.assertLess(abs(got - 62) / 62, 0.25)

    def test_phase_rate_is_the_default(self):
        self.assertEqual(estimate_seconds(1000, 300, 10, 3),
                         estimate_seconds(1000, 300, 10, 3, trainer="phase60"))

    def test_estimate_scales_with_every_factor(self):
        base = estimate_seconds(1000, 300, 10, 3)
        self.assertAlmostEqual(estimate_seconds(2000, 300, 10, 3), base * 2)
        self.assertAlmostEqual(estimate_seconds(1000, 600, 10, 3), base * 2)
        self.assertAlmostEqual(estimate_seconds(1000, 300, 20, 3), base * 2)
        self.assertAlmostEqual(estimate_seconds(1000, 300, 10, 6), base * 2)

    def test_hms_reads_naturally(self):
        self.assertEqual(hms(45), "45초")
        self.assertEqual(hms(1631), "27분 11초")


if __name__ == "__main__":
    unittest.main()
