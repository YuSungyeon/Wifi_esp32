"""Leakage boundaries, matched endpoints, and aggregation for the fixed pilot."""
import unittest
import numpy as np
import torch

from model_train.pilot75.run import (
    DATES, FLAGGED, common_ends, indices, measure, splits, candidate_input,
)
from model_train.robust.shared_temporal import SharedEncoder, amplitude_input


class Pilot75Test(unittest.TestCase):
    def test_splits_never_mix_dates_or_flagged_training(self):
        rows = [dict(date=d, key=f'{d}_{y}', label=y) for d in DATES for y in range(3)]
        rows.append(dict(date='20260919', key=FLAGGED, label=1))
        index = np.array([(si, end) for si in range(len(rows)) for end in (6000, 6300, 7500)])
        for split, (train_dates, test_dates) in splits().items():
            tr, va = indices(rows, index, split)
            self.assertFalse(set(index[tr, 0]) & set(index[va, 0]))
            self.assertTrue(all(rows[si]['date'] in train_dates and rows[si]['key'] != FLAGGED
                                for si in index[tr, 0]))
            self.assertTrue(all(rows[si]['date'] in test_dates for si in index[va, 0]))
            self.assertTrue(all(end in (6000, 7500) for end in index[tr, 1]))
            if '20260919' in test_dates:
                self.assertIn(len(rows) - 1, index[va, 0])

    def test_common_window_rejects_gap_in_either_modality(self):
        a, p = np.ones((12000, 2)), np.ones((12000, 2))
        p[6100] = np.nan
        a[100] = np.nan
        # Every 60s window touches at least one of the two gaps.
        self.assertEqual(len(common_ends(a, p)), 0)
        a[100] = 1
        np.testing.assert_array_equal(common_ends(a, p), [6000])

    def test_probabilities_are_averaged_before_session_argmax(self):
        rows = [dict(key='a', date='d', label=0), dict(key='b', date='d', label=1),
                dict(key='c', date='d', label=2)]
        index = np.array([[0, 0], [0, 1], [0, 2], [1, 0], [2, 0]])
        probs = np.array([[.51, .49, 0], [.51, .49, 0], [0, 1, 0], [0, 1, 0], [0, 0, 1]])
        m = measure(rows, index, probs)
        self.assertEqual(m['per_session'][0]['pred'], 'static')
        self.assertAlmostEqual(m['session']['accuracy'], 2 / 3)
        self.assertAlmostEqual(m['window']['accuracy'], 4 / 5)
        self.assertAlmostEqual(m['session_balanced_window']['accuracy'], (2 / 3 + 1 + 1) / 3)

    def test_flagged_evaluation_can_be_reported_both_ways(self):
        rows = [dict(key=str(i), date='d', label=i) for i in range(3)]
        rows.append(dict(key=FLAGGED, date='d', label=1))
        index = np.array([[i, 6000] for i in range(4)])
        probs = np.array([[1, 0, 0], [0, 1, 0], [0, 0, 1], [1, 0, 0]])
        self.assertEqual(measure(rows, index, probs)['session']['accuracy'], .75)
        self.assertEqual(measure(rows, index, probs, True)['session']['accuracy'], 1)

    def test_amplitude_zero_channel_elision_preserves_model_output(self):
        raw = np.random.default_rng(0).uniform(1, 30, (1500, 153)).astype(np.float32)
        full = amplitude_input(raw)
        compact = candidate_input('amp', raw, 1500, 1500)
        model = SharedEncoder(mode='A').eval()
        with torch.no_grad():
            torch.testing.assert_close(model(torch.tensor(full[None])), model(torch.tensor(compact[None])))


if __name__ == '__main__':
    unittest.main()
