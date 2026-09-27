import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"scripts"))
import csi_store as cs
from model_train.preprocessing import phase_features as pf
from model_train.analysis import prepare_phase_experiment as prep


class PhaseFeaturesTest(unittest.TestCase):
    def test_iq_order_and_amplitude_unchanged(self):
        frames = np.zeros(4, dtype=cs.CSI_FRAME_DTYPE)
        # Known imaginary, real pairs in all four quadrants.
        frames["raw"][:, 0::2] = np.array([4, 4, -4, -4])[:, None]
        frames["raw"][:, 1::2] = np.array([3, -3, -3, 3])[:, None]
        z = cs.complex_csi(frames, valid_only=False)
        np.testing.assert_allclose(z[:, 17], [3+4j, -3+4j, -3-4j, 3-4j])
        np.testing.assert_allclose(np.abs(z), cs.amplitude(frames, valid_only=False))

    def test_bandwise_affine_invariance_keeps_nonlinear_component(self):
        k = pf.FREQUENCIES
        original = .16*np.cos(k/5)
        shifted = original.copy()
        for band, a, b in zip(pf.BANDS, [.12, -.09], [3.0, -2.8]):
            shifted[band] += a*k[band]+b
        baseline, _ = pf.detrend_phase(np.angle(np.exp(1j*original)))
        corrected, _ = pf.detrend_phase(np.angle(np.exp(1j*shifted)))
        np.testing.assert_allclose(corrected, baseline, atol=1e-12)
        self.assertGreater(np.std(corrected), .01)
        for band in pf.BANDS:
            self.assertAlmostEqual(float(corrected[band].mean()), 0, places=12)
            self.assertAlmostEqual(float(corrected[band] @ k[band]), 0, places=10)

    def test_unreliable_first_word_excluded_and_zero_active_tone_invalid(self):
        z = np.ones((2, 64), dtype=np.complex128)
        z[:, :2] = 0
        z[1, 17] = 0
        amp, sine, cosine, valid, _ = pf.extract(z)
        self.assertEqual(amp.shape, (2, 51))
        self.assertEqual(valid.tolist(), [True, False])
        np.testing.assert_allclose(sine[0]**2+cosine[0]**2, 1)
        self.assertTrue(np.isnan(sine[1]).all())
        np.testing.assert_array_equal(pf.RAW_INDICES, np.r_[38:64, 2:27])

    def test_short_gap_crosses_angle_wrap_without_going_through_zero(self):
        sine = np.full((1, 3, 51), np.nan)
        cosine = sine.copy()
        for index, angle in [(0, 179), (2, -179)]:
            sine[0, index] = np.sin(np.deg2rad(angle))
            cosine[0, index] = np.cos(np.deg2rad(angle))
        valid = np.array([[True, False, True]])
        filled, _ = pf.interpolate_phase(sine, cosine, valid)
        self.assertTrue(filled[0, 1])
        self.assertLess(cosine[0, 1, 0], -.999)
        np.testing.assert_array_equal(valid, [[True, False, True]])

    def test_large_angle_long_gap_and_edges_are_not_filled(self):
        sine = np.full((1, 12, 51), np.nan)
        cosine = sine.copy()
        valid = np.zeros((1, 12), bool)
        for index, s, c in [(1, 0, 1), (3, 1, 0), (10, 1, 0)]:
            sine[0, index], cosine[0, index], valid[0, index] = s, c, True
        filled, rejected = pf.interpolate_phase(sine, cosine, valid)
        self.assertFalse(filled.any())
        self.assertEqual(rejected["angle"], 1)
        self.assertEqual(rejected["long"], 1)

    def test_normalization_counts_overlapping_train_windows_only(self):
        amp = np.tile(np.arange(6, dtype=float)[:, None], (1, 153))
        # Windows 0:3 and 2:5 => [0,1,2,2,3,4]; unused final frame ignored.
        amp[-1] = 1e12
        mean, std, safe, count = pf.normalization_from_sessions([(amp, [0, 2])], window=3)
        expected = np.array([0, 1, 2, 2, 3, 4])
        self.assertEqual(count, 6)
        np.testing.assert_allclose(mean[:153], expected.mean())
        np.testing.assert_allclose(std[:153], expected.std())
        np.testing.assert_array_equal(mean[153:], 0)
        np.testing.assert_array_equal(safe[153:], 1)

    def test_mask_is_applied_after_normalization(self):
        x = np.ones((3, 459), dtype=np.float32)
        mean = np.r_[np.full(153, 10.), np.zeros(306)].astype(np.float32)
        safe = np.ones(459, dtype=np.float32)
        a = pf.transform_window(x, mean, safe, "A")
        p = pf.transform_window(x, mean, safe, "P")
        np.testing.assert_array_equal(a[:, 153:], 0)
        np.testing.assert_array_equal(p[:, :153], 0)
        np.testing.assert_array_equal(a[:, :153], -9)
        np.testing.assert_array_equal(p[:, 153:], 1)

    def test_split_includes_date_and_rejects_other_collections(self):
        self.assertEqual(prep.split_for("20260919", 24), "train")
        self.assertEqual(prep.split_for("20260919", 25), "validation")
        self.assertEqual(prep.split_for("20260920", 31), "test")
        with self.assertRaises(ValueError):
            prep.split_for("20260616", 19)

    def test_crc_and_receiver_identity_checked(self):
        frames = np.zeros(1, dtype=cs.CSI_FRAME_DTYPE)
        h = frames["hdr"]
        for field, value in {"magic": cs.FRAME_MAGIC, "version": 4, "frame_type": 0,
                             "total_len": 172, "raw_len": 128, "channel": 11,
                             "rssi": -35, "rx_id": 2}.items():
            h[field] = value
        h["crc32"] = cs.crc32_of(frames.tobytes())
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"device_103.csi"
            path.write_bytes(frames.tobytes())
            self.assertEqual(len(prep.checked_frames(path, 2)), 1)
            with self.assertRaisesRegex(ValueError, "identity"):
                prep.checked_frames(path, 1)
            corrupt = bytearray(frames.tobytes())
            corrupt[-1] ^= 1
            path.write_bytes(corrupt)
            with self.assertRaisesRegex(ValueError, "CRC"):
                prep.checked_frames(path, 2)

    def test_session_aggregation_uses_mean_probabilities_and_date_key(self):
        from model_train.analysis.run_phase_experiment import session_results
        # First session: 2/3 window votes empty, but mean probability is static.
        probs = np.array([[.51, .49, 0], [.51, .49, 0], [0, 1, 0], [0, 0, 1]])
        metadata = [{"session_key": "20260919-s1", "date": "20260919", "session_id": 1}]*3
        metadata += [{"session_key": "20260920-s1", "date": "20260920", "session_id": 1}]
        evaluation = {"indices": np.arange(4), "probabilities": probs,
                      "predictions": probs.argmax(axis=1), "truth": np.array([1, 1, 1, 2])}
        result = session_results(evaluation, metadata)
        self.assertEqual(len(result["sessions"]), 2)
        self.assertEqual(result["sessions"][0]["predicted_label"], "static")
        self.assertEqual(result["metrics"]["accuracy"], 1)

    def test_all_modes_share_window_order_and_model_initialization(self):
        import torch
        from model_train.analysis.run_phase_experiment import PhaseDataset, model_digest
        from model_train.cnn1d.CNN1D import CNN1DClassifier
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"train").mkdir()
            (root/"sessions"/"20260919-s1").mkdir(parents=True)
            (root/"train"/"windows.jsonl").write_text(
                '{"session_key":"20260919-s1","session_id":1,"date":"20260919","start":0,"label_id":0}\n')
            values = np.ones((300, 459), dtype=np.float32)
            np.save(root/"sessions"/"20260919-s1"/"features.npy", values)
            np.savez(root/"normalization.npz", mean=np.zeros(459), std_safe=np.ones(459))
            digests, metadata = [], []
            for mode in ("A", "P", "AP"):
                ds = PhaseDataset(root, "train", mode)
                x, y, index = ds[0]
                self.assertEqual(tuple(x.shape), (300, 459))
                self.assertEqual((y, index), (0, 0))
                metadata.append(ds.metadata)
                torch.manual_seed(0)
                model = CNN1DClassifier(input_size=459)
                self.assertEqual(sum(p.numel() for p in model.parameters()), 96483)
                digests.append(model_digest(model))
            self.assertEqual(metadata[0], metadata[1])
            self.assertEqual(metadata[1], metadata[2])
            self.assertEqual(len(set(digests)), 1)


if __name__ == "__main__":
    unittest.main()
