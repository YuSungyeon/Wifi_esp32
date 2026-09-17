import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_train" / "analysis"))
import session_signal_audit as audit


class SignalAuditTest(unittest.TestCase):
    def test_overlap_does_not_reweight_shared_frames_or_include_unused_tail(self):
        values = np.asarray([1, 2, 100, 100, 3, 4, 1000, 1000])
        mask = audit.coverage_mask(len(values), [0, 2], 4)
        self.assertEqual(int(mask.sum()), 6)
        self.assertEqual(float(values[mask].mean()), 35.0)
        # Averaging the two windows would over-weight the shared values of 100.
        self.assertNotEqual(float(values[mask].mean()), np.concatenate((values[:4], values[2:6])).mean())
        np.testing.assert_array_equal(
            audit.coverage_mask(8, [0, 5], 2),
            [True, True, False, False, False, True, True, False],
        )

    def test_profile_reference_excludes_validation_test_and_constant_features(self):
        def row(sid, split, label, mean):
            return {"session_id": sid, "split": split, "label": label, "normalized_mean": np.asarray(mean)}

        sessions = [
            row(1, "train", "empty", [0, 0, 10000]),
            row(11, "train", "static", [2, 2, -10000]),
            row(21, "train", "motion", [4, 4, 20000]),
            row(7, "validation", "empty", [1.5, 1.5, 0]),
            row(10, "test", "empty", [1.5, 1.5, 0]),
        ]
        result = audit.compare_to_train(sessions, np.asarray([True, True, False]))[0]
        nearest = result["train_distances"][0]
        self.assertEqual(nearest["session_id"], 11)
        self.assertAlmostEqual(nearest["distance"], 0.5)
        self.assertEqual({row["session_id"] for row in result["train_distances"]}, {1, 11, 21})


if __name__ == "__main__":
    unittest.main()
