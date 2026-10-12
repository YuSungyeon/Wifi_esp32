from pathlib import Path
import unittest

import numpy as np
import torch

from model_train.phase60 import features as F
from model_train.phase60.evaluate_raw import aggregate, validate_bundle, validate_identity


class Phase60EvaluationTests(unittest.TestCase):
    def test_session_aggregation_is_probability_mean_and_metrics_use_distinct_denominators(self):
        rows = [dict(label="static", prediction="static", windows=[
            {"scores": [.51, .48, .01]}, {"scores": [.51, .48, .01]},
            {"scores": [.01, .98, .01]}]),
            dict(label="motion", prediction="empty", windows=[{"scores": [.8, .1, .1]}])]
        result = aggregate(rows)
        self.assertEqual(result["session"]["accuracy"], .5)
        self.assertEqual(result["window"]["accuracy"], .25)
        self.assertAlmostEqual(result["mean_session_window_accuracy"], 1/6)

    def test_main_checkpoint_contract_rejects_wrong_stride_class_order_and_seed_count(self):
        path = Path(__file__).resolve().parents[1] / "model_train/phase60/model_60sessions.pt"
        bundle = torch.load(path, map_location="cpu", weights_only=True)
        validate_bundle(bundle)
        for name, value in [("stride_frames", 30), ("class_names", ["static", "empty", "motion"]),
                            ("state_dicts", bundle["state_dicts"][:2])]:
            changed = dict(bundle, **{name: value})
            with self.subTest(name=name), self.assertRaises(ValueError):
                validate_bundle(changed)

    def test_label_mismatch_and_training_session_are_rejected(self):
        path = Path("raw/20260916/100000_static_s33")
        manifest = dict(session_id=33, label="static", devices=[{"device_id": x} for x in F.RX_ORDER])
        self.assertEqual(validate_identity(path, manifest, []), "20260916_33")
        with self.assertRaises(ValueError):
            validate_identity(path, dict(manifest, label="empty"), [])
        with self.assertRaises(ValueError):
            validate_identity(path, manifest, ["20260916_33"])

    def test_projection_is_finite_and_matches_explicit_dot_products(self):
        design = np.vstack([F.FREQUENCIES, np.ones(52)]).T
        inverse = np.linalg.pinv(design)
        independent = np.einsum("ik,kj->ij", design, inverse, optimize=False)
        self.assertTrue(np.isfinite(F._PROJ).all())
        np.testing.assert_allclose(F._PROJ, independent, atol=1e-14)
        phi = .02 * F.FREQUENCIES + .1
        np.testing.assert_allclose(F.phase_residual(np.exp(1j*phi)), 0, atol=1e-12)
        rng = np.random.default_rng(7)
        z = rng.normal(size=(3, 4096, 52)) + 1j*rng.normal(size=(3, 4096, 52))
        phi = np.unwrap(np.angle(z), axis=-1)
        residual = phi - np.einsum("...j,ij->...i", phi, independent, optimize=False)
        actual = F.phase_residual(z)
        self.assertTrue(np.isfinite(actual).all())
        np.testing.assert_allclose(actual, residual, atol=1e-12)

    def test_gaps_reject_windows_and_mean_subtraction_preserves_shape(self):
        phase = np.zeros((6600, 156), np.float32)
        phase[100] = np.nan
        starts = F.valid_starts(phase, 6000, 300)
        self.assertEqual(starts.tolist(), [300, 600])
        phase[300:] = np.arange(6300, dtype=np.float32)[:, None]
        x = F.windows(phase, starts, 6000, 20)
        self.assertEqual(x.shape, (2, 300, 156))
        np.testing.assert_allclose(x.mean(1), 0, atol=1e-5)


if __name__ == "__main__":
    unittest.main()
