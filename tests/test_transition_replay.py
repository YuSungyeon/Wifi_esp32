import unittest

import numpy as np

from model_train.analysis.replay_transition import (
    analyze, common_end, fill_ready, make_schedule, segment_metrics, truth_segments,
)


class TransitionReplayTests(unittest.TestCase):
    def test_prefix_does_not_use_future_right_endpoint_for_interpolation(self):
        present = np.ones((3, 100), bool)
        present[1, 58:60] = False
        values = np.ones((3, 100, 1), np.float32)
        values[1, 58:60] = np.nan
        filled, ready, mask = fill_ready(values, present)
        self.assertTrue(np.isfinite(filled[:, :60]).all())
        self.assertEqual(ready[1, 58], 60)
        rows = make_schedule(present, ready, mask, 30, True)
        at60 = next(row for row in rows if row['frame_cutoff_exclusive'] == 60)
        # The last common frame is 57: infer a past-only window ending at 58.
        self.assertEqual(at60['input_end_frame_exclusive'], 58)
        self.assertTrue(at60['state'] == 'hold' or at60['infer'])
        prefix = present.copy(); prefix[2, 59] = False
        observations=[np.flatnonzero(x) for x in prefix]
        self.assertEqual(common_end(observations, 60), 58)

    def test_prefix_common_end_does_not_borrow_from_receiver_ahead(self):
        present=np.ones((3, 100),bool)
        present[0,59]=False;present[1,58]=False;present[1,59:]=False
        values=np.ones((3,100,1),np.float32);values[~present]=np.nan
        _,ready,mask=fill_ready(values,present)
        # Common end is 58; a hole at 57 needs right endpoint 58 (unavailable).
        present[0,57]=False;values[0,57]=np.nan
        _,ready,mask=fill_ready(values,present)
        row=next(r for r in make_schedule(present,ready,mask,45,True) if r['frame_cutoff_exclusive']==60)
        self.assertEqual(row['state'],'suspended')
        self.assertEqual(row['reason'],'boundary_interpolation_not_observed')

    def test_existing_display_is_held_until_thirty_new_common_frames(self):
        present=np.ones((3,100),bool);present[1,58:60]=False
        values=np.ones((3,100,1),np.float32);values[~present]=np.nan
        _,ready,mask=fill_ready(values,present)
        row=next(r for r in make_schedule(present,ready,mask,30,True) if r['frame_cutoff_exclusive']==60)
        self.assertEqual(row['state'],'hold')
        self.assertEqual(row['reason'],'waiting_for_30_new_common_frames')

    def test_six_missing_frames_remain_invalid_for_window_length(self):
        present=np.ones((3,150),bool);present[2,50:56]=False
        values=np.ones((3,150,1),np.float32);values[~present]=np.nan
        filled,ready,mask=fill_ready(values,present)
        self.assertTrue(np.isnan(filled[2,50:56]).all())
        rows=make_schedule(present,ready,mask,60,False)
        self.assertEqual([r['frame_cutoff_exclusive'] for r in rows if r['state']=='suspended'],[60,90])

    def test_duration_metrics_clip_hold_intervals_and_suspension_breaks_stability(self):
        intervals=[{'wall':0,'state':'static'},{'wall':2,'state':'empty'},
                   {'wall':7,'state':'suspended'},{'wall':9,'state':'empty'}]
        result=segment_metrics(intervals,1,25,'empty',25)
        self.assertEqual(result['correct_s'],21)
        self.assertEqual(result['incorrect_s'],1)
        self.assertEqual(result['unavailable_s'],2)
        self.assertEqual(result['first_correct_delay_s'],1)
        self.assertEqual(result['first_10s_stable_start_delay_s'],8)

    def test_truth_keeps_reported_times_and_620_second_range(self):
        segments=truth_segments()
        self.assertEqual([r['end_wall']-r['start_wall'] for r in segments],[124,64,181,68,183])
        self.assertEqual(sum(r['end_wall']-r['start_wall'] for r in segments),620)
        rows=[{'t':0,'state':'empty'}]
        metrics=analyze(rows,segments[0]['start_wall']-100,1000)
        self.assertEqual(metrics['overall']['duration_s'],620)
        self.assertEqual(metrics['overall']['correct_s'],307)


if __name__=='__main__':
    unittest.main()
