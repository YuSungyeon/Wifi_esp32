import unittest
import numpy as np
import torch
from model_train.architecture_pilot.models import NAMES,make,statistics
from model_train.architecture_pilot.run import split
from model_train.robust.shared_temporal import SharedEncoder,amplitude_input
from model_train.pilot75.run import DATES,FLAGGED

class ArchitectureTest(unittest.TestCase):
    def test_shared_matches_deployed_eval_and_has_no_signal_dropout(self):
        torch.set_num_threads(2)
        a=SharedEncoder(mode='A').eval();b=make('shared').eval();b.load_state_dict(a.state_dict())
        x=torch.randn(2,150,51)
        with torch.no_grad(): torch.testing.assert_close(a(x[...,None]),b(x))
        observed=[]
        h=b.encoder.register_forward_pre_hook(lambda m,args:observed.append(args[0].shape))
        b.train();b(x);h.remove();self.assertEqual(observed[0],(102,1,150))
    def test_shapes_gradients_and_rf_features(self):
        torch.set_num_threads(2);x=torch.randn(2,150,51)
        for name in NAMES[:-1]:
            m=make(name);out=m(x);self.assertEqual(out.shape,(2,3));out.sum().backward()
            self.assertTrue(all(torch.isfinite(p.grad).all() for p in m.parameters() if p.grad is not None))
        self.assertEqual(statistics(x.numpy()).shape,(2,255))
    def test_session_split_and_training_exclusion(self):
        rows=[dict(date=d,key=f'{d}_{i}',label=i%3) for d in DATES for i in range(15)]
        rows.append(dict(date='20260919',key=FLAGGED,label=1))
        index=np.array([(i,e) for i in range(len(rows)) for e in (1500,1800,3000)])
        for date in [*DATES,'full']:
            tr,it,iv,te=split(rows,index,date)
            self.assertFalse(set(index[it,0])&set(index[iv,0]))
            self.assertFalse(set(index[tr,0])&set(index[te,0]))
            self.assertNotIn(len(rows)-1,index[tr,0])
            self.assertTrue(all(e%1500==0 for e in index[tr,1]))
            self.assertTrue(all(rows[i]['date']==date for i in index[te,0]))
    def test_preprocessing_window_shape(self):
        x=amplitude_input(np.ones((1500,153),np.float32))[...,0]
        self.assertEqual(x.shape,(150,51));np.testing.assert_allclose(x,0,atol=1e-5)
