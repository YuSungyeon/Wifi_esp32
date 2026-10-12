"""Shared frozen features must equal the two independent inference paths."""
import copy
import unittest

import numpy as np
import torch

from model_train.analysis.evaluate_room_on_oct6 import paired_inference
from model_train.cnn1d.shared_cnn import SharedTemporalCNN
from model_train.phase60 import features as F


class PairedPhaseTests(unittest.TestCase):
    def test_shared_features_match_independent_models_without_mutating_weights(self):
        torch.set_num_threads(2)
        torch.manual_seed(31)
        old = [SharedTemporalCNN().eval() for _ in range(3)]
        new = copy.deepcopy(old)
        for m in new:
            with torch.no_grad():
                m.head[-1].bias += torch.tensor([.1, -.2, .3])
        snapshots = [{k:v.clone() for k,v in m.state_dict().items()} for m in old+new]
        phase = np.random.default_rng(31).normal(size=(6010,156)).astype(np.float32)
        scores, _ = paired_inference(phase, [0, 10], old, new, batch=2)
        x = torch.from_numpy(F.windows(phase, np.array([0,10]), 6000, 20))
        with torch.inference_mode():
            for key, models in [('phase_original',old), ('phase_room',new)]:
                each = torch.stack([m(x).softmax(1) for m in models]).numpy()
                for i,s in enumerate([0,10]):
                    np.testing.assert_allclose(scores[key][s]['scores'],each.mean(0)[i],atol=1e-6,rtol=0)
                    np.testing.assert_allclose(scores[key][s]['seed_scores'],each[:,i],atol=1e-6,rtol=0)
        for m,state in zip(old+new,snapshots):
            self.assertTrue(all(torch.equal(v,state[k]) for k,v in m.state_dict().items()))

    def test_different_encoders_cannot_silently_share_features(self):
        old = [SharedTemporalCNN().eval()]
        new = copy.deepcopy(old)
        with torch.no_grad():
            new[0].encoders[0][0].weight += .01
        with self.assertRaisesRegex(ValueError,'identical encoders'):
            paired_inference(np.zeros((6000,156),np.float32),[0],old,new)


if __name__ == '__main__':
    unittest.main()
