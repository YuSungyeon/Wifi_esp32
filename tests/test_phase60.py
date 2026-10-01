"""위상 60초 모델 — 입력 계약과 특징 추출.

학습이 오래 걸리므로 여기서는 계약과 변환만 고정한다. 성능 숫자는 체크포인트 안에 있다.
"""
import sys
import unittest
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model_train.cnn1d.shared_cnn import SharedTemporalCNN  # noqa: E402
from model_train.phase60 import features as F  # noqa: E402
from model_train.phase60.predict import load_bundle, predict_phase  # noqa: E402

CHECKPOINTS = sorted((ROOT / "model_train" / "phase60").glob("model_*.pt"))


class FeatureTest(unittest.TestCase):
    def test_tone_order_is_frequency_ascending_and_excludes_dc_and_guards(self):
        self.assertEqual(len(F.RAW_TONE_ORDER), 52)
        self.assertNotIn(0, F.RAW_TONE_ORDER)                   # DC
        self.assertFalse(set(range(27, 38)) & set(F.RAW_TONE_ORDER.tolist()))  # 가드
        self.assertTrue((np.diff(F.FREQUENCIES) > 0).all())

    def test_phase_residual_removes_cfo_and_timing_slope(self):
        z = np.exp(1j * (0.9 + 0.04 * F.FREQUENCIES))          # 상수 + 기울기만 있는 신호
        np.testing.assert_allclose(F.phase_residual(z), np.zeros(52), atol=1e-9)

    def test_phase_residual_keeps_a_non_linear_component(self):
        bump = np.zeros(52)
        bump[10] = 0.3
        z = np.exp(1j * (0.9 + 0.04 * F.FREQUENCIES + bump))
        self.assertGreater(np.abs(F.phase_residual(z)).max(), 0.1)

    def test_windows_are_centred_per_series(self):
        rng = np.random.default_rng(0)
        phase = rng.normal(3.0, 0.1, (1200, F.N_COLS)).astype(np.float32)
        w = F.windows(phase, np.array([0, 300]), 600, 20)
        self.assertEqual(w.shape, (2, 30, F.N_COLS))
        np.testing.assert_allclose(w.mean(axis=1), 0, atol=1e-5)

    def test_valid_starts_skip_windows_containing_missing_frames(self):
        phase = np.zeros((1000, F.N_COLS), np.float32)
        phase[500] = np.nan
        starts = F.valid_starts(phase, 300, 100)
        self.assertTrue(all(not (s <= 500 < s + 300) for s in starts))


@unittest.skipUnless(CHECKPOINTS, "체크포인트가 아직 없다")
class CheckpointTest(unittest.TestCase):
    def test_every_checkpoint_declares_the_current_input_contract(self):
        for path in CHECKPOINTS:
            with self.subTest(path.name):
                bundle, models = load_bundle(path)
                self.assertEqual(bundle["feature_version"], F.FEATURE_VERSION)
                self.assertEqual(bundle["mode"], "P")
                self.assertEqual(bundle["window_frames"], 6000)
                self.assertEqual(bundle["rx_order"], list(F.RX_ORDER))
                self.assertEqual(len(models), 3)
                self.assertEqual(bundle["class_names"], ["empty", "static", "motion"])

    def test_checkpoint_records_what_it_was_trained_and_scored_on(self):
        for path in CHECKPOINTS:
            with self.subTest(path.name):
                bundle = torch.load(path, map_location="cpu", weights_only=False)
                self.assertTrue(bundle["training_sessions"])
                self.assertFalse(set(bundle["training_dates"]) & set(bundle["evaluation_dates"]))
                self.assertRegex(bundle["metrics"]["sessions"], r"^\d+/\d+$")

    def test_prediction_runs_and_returns_normalised_scores(self):
        bundle, models = load_bundle(CHECKPOINTS[0])
        rng = np.random.default_rng(0)
        phase = rng.normal(0, 0.05, (12000, F.N_COLS)).astype(np.float32)
        out = predict_phase(phase, bundle, models)
        self.assertIn(out["prediction"], ("empty", "static", "motion"))
        self.assertAlmostEqual(sum(out["mean_scores"].values()), 1.0, places=3)
        self.assertGreater(out["valid_windows"], 0)

    def test_loading_rejects_a_bundle_with_a_different_contract(self):
        import tempfile

        bundle = torch.load(CHECKPOINTS[0], map_location="cpu", weights_only=False)
        bundle["feature_version"] = "something-else"
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "bad.pt"
            torch.save(bundle, p)
            with self.assertRaises(ValueError):
                load_bundle(p)


class ModelShapeTest(unittest.TestCase):
    def test_model_takes_156_series_and_returns_three_classes(self):
        model = SharedTemporalCNN(groups=(F.N_COLS,), n_classes=3).eval()
        with torch.no_grad():
            out = model(torch.zeros(2, 300, F.N_COLS))
        self.assertEqual(tuple(out.shape), (2, 3))


if __name__ == "__main__":
    unittest.main()
