"""배포 모델·파일 tail·GUI/CLI 실행 연결 회귀 테스트 (실제 포트는 열지 않는다)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import torch

from scripts import csi_store as cs
from realtime.engine import Engine, MODEL
from realtime.stream import CsiStream
from realtime.state import Reading, StateMachine
from model_train.robust.predict import load_bundle, load_raw_amplitude, predict_amplitude
from scripts import realtime_launch as launch
import meshsense_gui as gui
import meshsense_cli as cli


def frame_bytes(seq, wire, *, value=8, boot=1, channel=11):
    frames = np.zeros(len(seq), dtype=cs.CSI_FRAME_DTYPE)
    h = frames['hdr']
    h['magic'], h['version'] = cs.FRAME_MAGIC, cs.FRAME_VERSION
    h['frame_type'], h['total_len'], h['raw_len'] = cs.FRAME_TYPE_CSI, cs.CSI_FRAME_SIZE, 128
    h['seq'] = np.arange(len(seq)) + 1
    h['tx_seq'] = seq
    h['channel'], h['rssi'], h['rate'], h['sig_len'] = channel, -40, 11, 47
    h['boot_id'], h['rx_id'], h['gain_comp'] = boot, wire, 1.0
    frames['raw'] = value
    h['crc32'] = [cs.crc32_of(f.tobytes()) for f in frames]
    return frames.tobytes()


class SessionFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / 'raw' / '20261002' / 'fixture'
        self.session.mkdir(parents=True)
        (self.session / 'session.json').write_text(json.dumps({
            'label': 'not-used-for-prediction',
            'devices': [{'device_id': rx, 'rx_id': i+1} for i, rx in enumerate((101, 102, 103))],
        }))

    def write_rx(self, rx, seq, **kwargs):
        data = frame_bytes(seq, rx-100, **kwargs)
        (self.session / f'device_{rx}.csi').write_bytes(data)
        return data

    def write_all(self, seq):
        for rx in (101, 102, 103):
            self.write_rx(rx, seq)


class StreamTests(SessionFixture):
    def test_waits_for_all_rx_and_uses_common_endpoint(self):
        self.write_rx(101, np.arange(10))
        stream = CsiStream(self.session)
        stream.poll()
        self.assertIsNone(stream.latest(5))
        self.write_rx(102, np.arange(2, 9))
        self.write_rx(103, np.arange(1, 8))
        stream.poll()
        win = stream.latest(5)
        self.assertEqual(win.tx_seq0, 3)
        self.assertEqual(win.amp.shape, (5, 153))
        self.assertTrue(np.isfinite(win.amp).all())

    def test_partial_frame_is_retried_not_consumed(self):
        data = frame_bytes([1, 2], 1)
        p = self.session / 'device_101.csi'
        p.write_bytes(data[:-20])
        stream = CsiStream(self.session)
        self.assertEqual(stream.poll(), 1)
        self.assertEqual(stream._offset[101], cs.CSI_FRAME_SIZE)
        p.write_bytes(data)
        self.assertEqual(stream.poll(), 1)
        self.assertEqual(stream._offset[101], 2 * cs.CSI_FRAME_SIZE)

    def test_crc_failure_is_not_model_input(self):
        data = bytearray(self.write_rx(101, [1]))
        data[-1] ^= 1
        (self.session / 'device_101.csi').write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'crc'):
            CsiStream(self.session).poll()

    def test_rf_and_receiver_identity_are_checked(self):
        self.write_rx(101, [1], channel=6)
        with self.assertRaisesRegex(ValueError, 'RF'):
            CsiStream(self.session).poll()
        (self.session / 'device_101.csi').write_bytes(frame_bytes([1], 9))
        with self.assertRaisesRegex(ValueError, '식별'):
            CsiStream(self.session).poll()

    def test_tx_regression_and_boot_change_are_not_sorted_away(self):
        self.write_rx(101, [2, 1])
        with self.assertRaisesRegex(ValueError, '역행'):
            CsiStream(self.session).poll()
        p = self.session / 'device_101.csi'
        p.write_bytes(frame_bytes([1], 1))
        stream = CsiStream(self.session)
        stream.poll()
        with p.open('ab') as fp:
            fp.write(frame_bytes([2], 1, boot=2))
        with self.assertRaisesRegex(ValueError, '재부팅'):
            stream.poll()

    def test_file_truncation_is_an_error(self):
        self.write_rx(101, [1, 2])
        stream = CsiStream(self.session)
        stream.poll()
        (self.session / 'device_101.csi').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, '축소'):
            stream.poll()

    def test_large_forward_jump_keeps_ring_indices_in_bounds(self):
        self.write_all([0])
        stream = CsiStream(self.session, capacity=10)
        stream.poll()
        for rx in (101, 102, 103):
            with (self.session / f'device_{rx}.csi').open('ab') as fp:
                fp.write(frame_bytes([100], rx-100))
        stream.poll()
        self.assertEqual(stream.latest(1).tx_seq0, 100)
        self.assertTrue(np.isfinite(stream.latest(1).amp).all())

    def test_duplicate_across_polls_keeps_first_amplitude(self):
        self.write_all([1, 2])
        stream = CsiStream(self.session)
        stream.poll()
        first = stream.latest(1).amp.copy()
        for rx in (101, 102, 103):
            with (self.session / f'device_{rx}.csi').open('ab') as fp:
                fp.write(frame_bytes([2], rx-100, value=40))
        stream.poll()
        np.testing.assert_array_equal(stream.latest(1).amp, first)

    def test_only_short_internal_gaps_are_filled(self):
        self.write_all([0, 1, 7, 14])  # gap 5과 6
        stream = CsiStream(self.session)
        stream.poll()
        win = stream.latest(15)
        self.assertTrue(np.isfinite(win.amp[2:7]).all())
        self.assertTrue(np.isnan(win.amp[8:14]).all())

    def test_replay_does_not_discard_first_minutes(self):
        amp = np.ones((30001, 153), dtype=np.float32)
        present = np.ones((3, len(amp)), dtype=bool)
        stream = CsiStream(self.session)
        with mock.patch('model_train.robust.predict.load_raw_amplitude',
                        return_value=(amp, {'common_start_tx_seq': 200}, present)):
            stream.load_replay()
        stream.rewind(3000)
        self.assertEqual(stream.latest(3000).tx_seq0, 200)
        for _ in range(18):
            self.assertTrue(stream.advance(1500))
        self.assertEqual(stream.latest(3000).tx_seq0, 27200)
        self.assertFalse(stream.advance(1500))

    def test_window_edge_interpolation_uses_full_segment_context(self):
        self.write_all([0, 1, 2, 5, 6, 7])
        stream = CsiStream(self.session)
        stream.poll()
        win = stream.latest(5)  # 3~7: 3,4는 창 밖의 tx_seq=2로 보간해야 함
        self.assertEqual(win.tx_seq0, 3)
        self.assertTrue(np.isfinite(win.amp).all())


class ModelTests(SessionFixture):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(2)

    def test_replay_matches_raw_predictor_and_preserves_weights_and_source(self):
        self.write_all(np.arange(3100))
        amp, _ = load_raw_amplitude(self.session, min_common_length=3000)
        bundle, models = load_bundle(MODEL)
        expected = predict_amplitude(amp, bundle, models)['windows'][0]['scores']
        original = {p.name: p.read_bytes() for p in self.session.iterdir()}
        engine = Engine(self.session, output_dir=self.root / 'results')
        weights = [{k: v.clone() for k, v in m.state_dict().items()} for m in engine.models]
        engine.prepare_replay()
        engine.step(now=30)
        np.testing.assert_allclose(engine.sm.probs, expected, rtol=0, atol=1e-7)
        self.assertEqual(engine.n_predictions, 1)
        self.assertEqual(engine.sm.state, ('empty','static','motion')[int(np.argmax(expected))])
        self.assertAlmostEqual(engine.sm.confidence, max(expected), places=7)
        self.assertEqual(engine.view()['decision_mode'], 'instant')
        self.assertEqual(engine.view()['threshold'], 0)
        self.assertEqual(engine.view()['model_count'], 3)
        self.assertEqual(engine.view()['warmup_remaining_s'], 0)
        for m, state in zip(engine.models, weights):
            self.assertFalse(m.training)
            for key, value in m.state_dict().items():
                torch.testing.assert_close(value, state[key], rtol=0, atol=0)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.session.iterdir()})

    def test_stale_follow_data_suspends_even_when_old_window_is_valid(self):
        self.write_all(np.arange(3000))
        engine = Engine(self.session, output_dir=self.root / 'results')
        with mock.patch('realtime.stream.time.monotonic', return_value=100):
            engine.step(now=30)
            engine.step(now=30.3)
        self.assertEqual(engine.n_predictions, 1)  # 같은 창 반복 추론 금지
        engine.sm._to(30, 'motion', .9)
        with mock.patch('realtime.stream.time.monotonic', return_value=104):
            engine.step(now=34)
        self.assertEqual(engine.sm.state, 'suspended')
        self.assertEqual(engine.view()['n_rx'], 0)
        self.assertEqual(engine.n_predictions, 1)

    def test_wrong_model_contract_is_rejected(self):
        bundle = torch.load(MODEL, weights_only=True, map_location='cpu')
        for field, value in [('class_names', ['motion','static','empty']),
                             ('raw_tone_order', list(range(51))), ('sample_rate_hz', 50),
                             ('state_dicts', bundle['state_dicts'][:2])]:
            changed = dict(bundle, **{field: value})
            with mock.patch('model_train.robust.predict.torch.load', return_value=changed):
                with self.assertRaisesRegex(ValueError, 'contract'):
                    load_bundle(MODEL)

    def test_invalid_full_input_cannot_confirm_a_two_rx_prediction(self):
        sm = StateMachine()
        sm._to(0, 'empty', .9)
        sm.update(Reading(t=2, probs=(.1,.8,.1), n_rx=2, valid=False))
        self.assertEqual(sm.state, 'suspended')


class LauncherTests(SessionFixture):
    def test_command_selects_venv_model_and_separate_output(self):
        cmd = launch.build_command(self.session, follow=True, output_dir=self.root/'results')
        self.assertIn('--follow', cmd)
        self.assertIn('--plain', cmd)
        self.assertEqual(cmd[cmd.index('--model')+1], str(MODEL))
        self.assertEqual(cmd[cmd.index('--stride')+1], '30')
        self.assertEqual(cmd[0], str(launch.runtime_python()))

    def test_invalid_session_and_stride_are_rejected(self):
        with self.assertRaises(ValueError):
            launch.build_command(self.root)
        with self.assertRaises(ValueError):
            launch.build_command(self.session, stride=0)

    def test_gui_has_start_stop_controls(self):
        self.assertIn('/api/realtime/start', gui.JS)
        self.assertIn('/api/realtime/stop', gui.JS)
        self.assertIn('녹화 재생', gui.JS)

    def test_gui_starts_prediction_separately_from_collection(self):
        rt_job = gui.Job()
        with mock.patch.object(gui, 'REPO_ROOT', self.root), mock.patch.object(gui, 'RAW_ROOT', self.root/'raw'), \
             mock.patch.object(gui, 'RT_JOB', rt_job), mock.patch.object(gui, 'check_runtime'), \
             mock.patch.object(gui, 'new_output_dir', return_value=self.root/'results'), \
             mock.patch.object(rt_job, 'start', return_value=None) as start, \
             mock.patch.object(gui.JOB, 'stop') as stop_collection:
            result = gui.dispatch('/api/realtime/start', {'path':str(self.session), 'follow':True})
            self.assertTrue(result['ok'])
            self.assertIn('--follow', start.call_args.args[1])
            gui.dispatch('/api/realtime/stop', {})
            stop_collection.assert_not_called()

    def test_gui_rejects_outside_path_and_non_boolean_mode(self):
        with mock.patch.object(gui, 'REPO_ROOT', self.root), mock.patch.object(gui, 'RAW_ROOT', self.root/'raw'):
            self.assertIn('error', gui.dispatch('/api/realtime/start', {'path':str(self.root)}))
            self.assertIn('error', gui.dispatch('/api/realtime/start', {'path':str(self.session), 'follow':'false'}))

    def test_missing_managed_status_never_falls_back_to_old_session(self):
        (self.session/'realtime_status.json').write_text(json.dumps({'events':[], 't':5}))
        self.assertIsNone(gui.realtime_view(self.root/'raw', status_file=self.root/'missing.json'))

    def test_cli_direct_mode_passes_model_and_follow(self):
        args = ['meshsense_cli', '--realtime', str(self.session), '--follow', '--plain']
        with mock.patch('sys.argv', args), mock.patch.object(cli, '_run_model', return_value=0) as run:
            self.assertEqual(cli.main(), 0)
        self.assertEqual(run.call_args.args[0], self.session)
        self.assertTrue(run.call_args.kwargs['follow'])

    def test_cli_interrupt_terminates_its_prediction_child(self):
        child = mock.Mock()
        child.wait.side_effect = [KeyboardInterrupt, 0]
        child.poll.return_value = None
        with mock.patch.object(cli, 'check_runtime'), mock.patch.object(cli.subprocess, 'Popen', return_value=child), \
             mock.patch.object(cli.signal, 'signal'):
            self.assertEqual(cli._run_model(self.session, follow=True), 130)
        child.terminate.assert_called_once()

    def test_gui_reports_selected_managed_status_not_newer_legacy_file(self):
        managed = self.root/'results'/'realtime_status.json'
        managed.parent.mkdir()
        managed.write_text(json.dumps({'events':[], 't':30, 'mode':'follow'}))
        (self.session/'realtime_status.json').write_text(json.dumps({'events':[], 't':300, 'mode':'old'}))
        self.assertEqual(gui.realtime_view(self.root/'raw', status_file=managed)['mode'], 'follow')

    def test_gui_can_see_cli_run_and_distinguishes_finished_records(self):
        status=self.root/'realtime'/'run'/'realtime_status.json'
        status.parent.mkdir(parents=True)
        status.write_text(json.dumps({'events':[], 't':30, 'mode':'follow', 'running':True}))
        self.assertTrue(gui.realtime_view(self.root/'raw')['active'])
        status.write_text(json.dumps({'events':[], 't':30, 'mode':'follow', 'running':False}))
        self.assertFalse(gui.realtime_view(self.root/'raw')['active'])


if __name__ == '__main__':
    unittest.main()
