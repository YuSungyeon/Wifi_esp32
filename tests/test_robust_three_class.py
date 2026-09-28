import unittest

import numpy as np
import torch

from model_train.robust.features import eligible, extract_batch
from model_train.robust.run_experiment import (
    development_folds, evaluate_scores, fit, make_model, train_indices, weights,
)
from model_train.robust.shared_temporal import SharedEncoder, amplitude_input, temporal_input
from model_train.robust.predict import predict_amplitude
from model_train.robust.nested_validation import inner_folds


class RobustWindowTests(unittest.TestCase):
    def sample(self, batch=2, length=300):
        rng = np.random.default_rng(10)
        amp = rng.uniform(2, 20, (batch, length, 153)).astype(np.float32)
        angle = rng.normal(0, .15, amp.shape).astype(np.float32)
        return np.concatenate([amp, np.sin(angle), np.cos(angle)], axis=2)

    def test_batch_and_individual_window_agree(self):
        x = self.sample()
        together = extract_batch(x)
        alone = extract_batch(x[:1])
        for key in together:
            np.testing.assert_allclose(together[key][:1], alone[key], atol=2e-5)

    def test_invariant_features_ignore_tone_receiver_permutation(self):
        x = self.sample()
        # Preserve RX groups for amplitude frame normalization, but permute their
        # order and all tones within each RX.
        order = np.concatenate([r*51 + np.arange(50, -1, -1) for r in (2, 0, 1)])
        other = np.concatenate([x[:, :, j*153+order] for j in range(3)], axis=2)
        a, b = extract_batch(x), extract_batch(other)
        for key in ("A_global", "P_global", "AP_global"):
            np.testing.assert_allclose(a[key], b[key], atol=1e-4)

    def test_sparse_phase_gaps_allowed_but_low_coverage_rejected(self):
        x = self.sample(1)
        x[:, 5::30, 153:] = np.nan
        self.assertTrue(eligible(x[0])[0])
        self.assertTrue(all(np.isfinite(v).all() for v in extract_batch(x).values()))
        x[:, :100, 153:] = np.nan
        with self.assertRaisesRegex(ValueError, "coverage"):
            extract_batch(x)

    def test_unfilled_amplitude_gap_rejected(self):
        x = self.sample(1)
        x[0, 50, 1] = np.nan
        self.assertFalse(eligible(x[0])[0])
        with self.assertRaisesRegex(ValueError, "amplitude gaps"):
            extract_batch(x)

    def test_constant_signal_and_all_lengths_are_finite(self):
        for w in (300, 500, 1000, 2000, 3000):
            x = np.concatenate([np.ones((1, w, 153))*10,
                                np.zeros((1, w, 153)), np.ones((1, w, 153))], axis=2)
            self.assertTrue(all(np.isfinite(v).all() for v in extract_batch(x).values()))


class GroupedEvaluationTests(unittest.TestCase):
    def test_nested_model_selection_never_reads_outer_sessions(self):
        sessions = [{"date": date, "label_id": c, "session_id": d*30+c*10+i}
                    for d,date in enumerate(("20260919", "20260920"))
                    for c in range(3) for i in range(10)]
        allowed = list(range(30, 60))
        folds = inner_folds(sessions, allowed)
        self.assertEqual(sorted(i for _, val in folds for i in val), allowed)
        for tr,val in folds:
            self.assertFalse(set(tr)&set(val))
            self.assertEqual(set(tr)|set(val), set(allowed))
            self.assertEqual({sessions[i]["label_id"] for i in tr}, {0,1,2})
            self.assertEqual({sessions[i]["label_id"] for i in val}, {0,1,2})

    def test_all_windows_from_a_session_stay_in_one_fold(self):
        sessions = [{"date": "20260919", "label_id": label, "session_id": label*10+i}
                    for label in range(3) for i in range(10)]
        sessions += [{"date": "20260920", "label_id": 0, "session_id": 31}]
        folds = development_folds(sessions)
        self.assertEqual(sorted(i for _, val in folds for i in val), list(range(30)))
        for tr, val in folds:
            self.assertFalse(set(tr) & set(val))
            self.assertNotIn(30, tr+val)
            self.assertEqual([sum(sessions[i]["label_id"] == c for i in val) for c in range(3)], [2, 2, 2])

    def test_normalizer_is_fitted_only_on_training_sessions(self):
        data = {"session": np.repeat(np.arange(4), 3), "y": np.repeat([0, 1, 2, 0], 3),
                "x": np.arange(24, dtype=float).reshape(12, 2)}
        data["x"][-3:] = 1e8
        ids = train_indices(data, [0, 1, 2])
        model = fit(make_model({"kind": "linear", "C": 1.}), data, "x", ids)
        np.testing.assert_allclose(model.named_steps["standardscaler"].mean_, data["x"][:9].mean(0))

    def test_balancing_assigns_equal_mass_to_classes_and_sessions(self):
        data = {"session": np.array([0]*100+[1]*10+[2]*20+[3]*30),
                "y": np.array([0]*100+[0]*10+[1]*20+[2]*30)}
        ids = train_indices(data, range(4))
        w = weights(data, ids)
        totals = [w[data["y"][ids] == c].sum() for c in range(3)]
        np.testing.assert_allclose(totals, np.mean(totals))
        self.assertEqual(sum(data["session"][ids] == 0), 48)

    def test_session_aggregation_and_truth_order(self):
        data = {"session": np.array([1, 1, 0, 0]), "y": np.array([2, 2, 0, 0])}
        p = np.array([[.1, .1, .8], [.1, .2, .7], [.9, .05, .05], [.5, .3, .2]])
        r = evaluate_scores(data, np.arange(4), p)
        self.assertEqual(r["session"]["accuracy"], 1)
        self.assertEqual([s["session"] for s in r["sessions"]], [0, 1])
        self.assertEqual(r["session"]["confusion"], [[1, 0, 0], [0, 0, 0], [0, 0, 1]])


class SharedEncoderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(2)

    def test_amplitude_only_inference_matches_training(self):
        x = RobustWindowTests().sample(1, 3000)[0]
        training = temporal_input(x)
        inference = amplitude_input(x[:, :153])
        np.testing.assert_array_equal(training[..., 0], inference[..., 0])
        torch.manual_seed(0)
        model = SharedEncoder("A").eval()
        with torch.no_grad():
            a = model(torch.from_numpy(training[None]))
            b = model(torch.from_numpy(inference[None]))
        torch.testing.assert_close(a, b, rtol=0, atol=0)

    def test_eval_encoder_has_no_series_identity(self):
        torch.manual_seed(0)
        model = SharedEncoder("AP").eval()
        x = torch.randn(2, 100, 51, 3)
        with torch.no_grad():
            a, b = model(x), model(x[:, :, torch.randperm(51)])
        torch.testing.assert_close(a, b, atol=1e-6, rtol=1e-5)

    def test_inference_skips_unfilled_gaps_and_uses_exact_window(self):
        model = SharedEncoder("A").eval()
        amp = np.ones((6500, 153), dtype=np.float32)*10
        amp[3200:3240] = np.nan
        bundle = {"window_frames": 3000, "stride_frames": 1500,
                  "class_names": ["empty", "static", "motion"], "status": "test"}
        result = predict_amplitude(amp, bundle, [model])
        self.assertEqual(result["candidate_windows"], 3)
        self.assertEqual(result["valid_windows"], 1)
        self.assertEqual(result["windows"][0]["start_frame"], 0)
        self.assertEqual(result["windows"][0]["end_frame_exclusive"], 3000)
        with self.assertRaisesRegex(ValueError, "no valid"):
            predict_amplitude(amp[:2999], bundle, [model])


if __name__ == "__main__":
    unittest.main()
