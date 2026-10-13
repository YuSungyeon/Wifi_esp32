import tempfile
import unittest
from pathlib import Path

from model_train.analysis.evaluate_room_live import read_truth, rebuild_rows, analyze, verify_time_metrics


class LoggedEvaluationTests(unittest.TestCase):
    def test_action_parser_excludes_transitions_and_labels_doorway_static(self):
        text = '측정 날짜: 2026-10-13\n실시간 데이터 폴더명: 20261013-223405-12476623\n'
        clocks = ['22:34:05','22:34:32','22:37:41','22:37:49','22:37:57','22:41:00',
                  '22:41:03','22:44:04','22:44:17','22:47:19','22:47:29','22:50:37',
                  '22:50:44','22:50:52']
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)/'info.txt'
            p.write_text(text+'\n'.join(f'{i}. 행동: {clock}' for i, clock in enumerate(clocks, 1)))
            _, truth = read_truth(p)
            self.assertEqual([s['end_wall']-s['start_wall'] for s in truth], [189,183,181,182,188])
            self.assertEqual(truth[3]['label'], 'static')
            p.write_text(p.read_text()+'\n14. 중복: 22:50:53')
            with self.assertRaises(ValueError): read_truth(p)

    def test_counterfactual_holds_its_own_class_and_counts_unavailable(self):
        log = [dict(wall=1, inferred=True, reason='inference', input_start_tx_seq=100,
                    input_end_tx_seq_exclusive=6100, state='static'),
               dict(wall=3, inferred=False, reason='waiting_for_new_common_frames', state='static'),
               dict(wall=4, inferred=False, reason='unfilled_gap', state='suspended'),
               dict(wall=6, inferred=True, reason='inference', input_start_tx_seq=130,
                    input_end_tx_seq_exclusive=6130, state='static'),
               dict(wall=7, inferred=False, reason='stopped', state='static')]
        scores = {0:{'scores':[.9,.1,0]},30:{'scores':[.8,.2,0]}}
        rows = rebuild_rows(log, scores, 100, 0, 8)
        self.assertEqual([r['state'] for r in rows], ['deciding','empty','empty','suspended','empty','empty'])
        truth = [dict(start_wall=1, end_wall=8, label='empty', name='test')]
        result = analyze(rows, truth, 8)
        self.assertEqual(result['overall']['correct_s'], 5)
        self.assertEqual(result['overall']['unavailable_s'], 2)
        verify_time_metrics(rows, truth, 8, result, 0)


if __name__ == '__main__':
    unittest.main()
