import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from model_train.pilot75.evaluate_live import schedule,combine,segment_metrics
from realtime.engine import Engine
from realtime.state import StateMachine


class LivePilotTest(unittest.TestCase):
    def test_window_boundary_does_not_borrow_future_or_past(self):
        present=np.ones((3,1000),bool)
        present[0,300]=False
        s=schedule(present,300)
        # cutoff 600: window starts exactly on the missing frame; invalid despite full-file interpolation.
        i=list(s['cutoffs']).index(600)
        self.assertFalse(s['valid'][i])
        # A gap strictly inside the window is interpolatable.
        i=list(s['cutoffs']).index(570)
        self.assertTrue(s['valid'][i])
        present[1,599]=False
        self.assertEqual(schedule(present,300)['ends'][20],599)

    def test_combiner_matches_actual_engine_step_without_io(self):
        n=180; times=np.arange(n)*.3
        scores=np.tile([.8,.1,.1],(n,1)); scores[35:100]=[.1,.8,.1]
        scores[10:14]=np.nan; scores[70:73]=np.nan; scores[120:130]=[.34,.33,.33]
        motion=np.zeros(n); motion[50:65]=.95; motion[53]=np.nan
        expected,_=combine(scores,motion,np.full(n,3),times)
        with tempfile.TemporaryDirectory() as tmp:
            engine=object.__new__(Engine)
            engine.dir=Path(tmp);engine.sm=StateMachine();engine._frames=0
            engine.stream=SimpleNamespace(poll=lambda:0)
            engine._activity=[];engine.stage1=True;engine.stage1_gate=(.9,3)
            engine._window_probs=[];engine.events=[];engine.view=lambda:{}
            actual=[]
            with patch('realtime.engine.write_status'):
                for i,t in enumerate(times):
                    engine.activity=lambda:float(motion[i])
                    engine.predict=lambda: (tuple(scores[i]),(101,102,103)) if np.isfinite(scores[i]).all() else None
                    engine.step(now=float(t));actual.append(engine.sm.state)
        np.testing.assert_array_equal(actual,expected)

    def test_time_integration_and_stable_delay(self):
        times=np.arange(0,21,.3)
        states=np.where(times<3,'empty','static')
        m=segment_metrics(times,states,0,20,'static',21)
        self.assertAlmostEqual(m['correct_s'],17)
        self.assertAlmostEqual(m['first_correct_s'],3)
        self.assertAlmostEqual(m['first_10s_stable_start_s'],3)


if __name__=='__main__': unittest.main()
