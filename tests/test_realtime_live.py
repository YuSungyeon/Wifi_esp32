"""현재 보드 실시간: 포트 소유권, 무라벨 분리, 자식 종료와 GUI/CLI 진입점."""
import json
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import meshsense_gui as gui
import meshsense_cli as cli
import realtime_live as live
import realtime_launch as launch
from realtime.state import StateMachine, Reading


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / 'realtime' / 'run'
        self.registry = self.root / 'mac_collector' / 'device_registry.csv'
        self.registry.parent.mkdir()
        self.registry.write_text('device_id,sta_mac\n101,test\n')

    def child(self):
        child = mock.Mock()
        child.poll.return_value = None
        return child

    def test_capture_is_unlabeled_not_training_raw_and_registry_is_snapshot(self):
        with mock.patch.object(live, 'ROOT', self.root), mock.patch.object(live, 'repo_provenance', return_value={}):
            capture = live.create_capture(self.run, '/dev/fake')
        m = json.loads((capture / 'session.json').read_text())
        self.assertIsNone(m['label'])
        self.assertIsNone(m['session_id'])
        self.assertEqual(m['purpose'], 'realtime_monitor')
        self.assertFalse((self.root / 'raw').exists())
        self.assertEqual((capture / 'device_registry_snapshot.csv').read_bytes(), self.registry.read_bytes())
        with self.assertRaises(FileExistsError):
            live.create_capture(self.run, '/dev/fake')

    def test_start_and_stop_owns_both_children_and_finalizes_only_new_capture(self):
        reader, prediction = self.child(), self.child()
        stop = mock.Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = True
        with mock.patch.object(live, 'ensure_port_free'), mock.patch.object(live, 'check_runtime'), \
             mock.patch.object(live, 'ROOT', self.root), mock.patch.object(live, 'repo_provenance', return_value={}), \
             mock.patch.object(live.subprocess, 'Popen', side_effect=[reader, prediction]) as popen:
            self.assertEqual(live.run_live('/dev/fake', self.run, stop=stop), 0)
        reader.send_signal.assert_called_once_with(signal.SIGINT)
        prediction.send_signal.assert_called_once_with(signal.SIGTERM)
        cmd = popen.call_args_list[0].args[0]
        self.assertNotIn('--force-lines', cmd)
        self.assertNotIn('--device-id', cmd)
        self.assertEqual(cmd[cmd.index('--flush-interval') + 1], '0.1')
        self.assertEqual(cmd[cmd.index('--registry') + 1], str(self.run / 'capture' / 'device_registry_snapshot.csv'))
        self.assertIn('--follow', popen.call_args_list[1].args[0])
        self.assertIsNotNone(json.loads((self.run / 'capture' / 'session.json').read_text())['ended_at_unix_us'])
        self.assertFalse(json.loads((self.run / 'prediction' / 'realtime_status.json').read_text())['running'])

    def test_second_child_start_failure_stops_reader_and_records_error(self):
        reader = self.child()
        with mock.patch.object(live, 'ensure_port_free'), mock.patch.object(live, 'check_runtime'), \
             mock.patch.object(live, 'ROOT', self.root), mock.patch.object(live, 'repo_provenance', return_value={}), \
             mock.patch.object(live.subprocess, 'Popen', side_effect=[reader, OSError('test-start-failure')]):
            self.assertEqual(live.run_live('/dev/fake', self.run), 1)
        reader.send_signal.assert_called_once_with(signal.SIGINT)
        status = json.loads((self.run / 'prediction' / 'realtime_status.json').read_text())
        self.assertEqual(status['state'], 'suspended')
        self.assertIn('test-start-failure', status['note'])

    def test_reader_failure_stops_prediction(self):
        reader, prediction = self.child(), self.child()
        reader.poll.return_value = 3
        stop = mock.Mock()
        stop.is_set.return_value = False
        stop.wait.return_value = False
        with mock.patch.object(live, 'ensure_port_free'), mock.patch.object(live, 'check_runtime'), \
             mock.patch.object(live, 'ROOT', self.root), mock.patch.object(live, 'repo_provenance', return_value={}), \
             mock.patch.object(live.subprocess, 'Popen', side_effect=[reader, prediction]):
            self.assertEqual(live.run_live('/dev/fake', self.run, stop=stop), 1)
        prediction.send_signal.assert_called_once_with(signal.SIGTERM)

    def test_busy_port_does_not_create_capture_or_start_children(self):
        with mock.patch.object(live, 'ensure_port_free', side_effect=ValueError('busy')), \
             mock.patch.object(live.subprocess, 'Popen') as popen:
            with self.assertRaisesRegex(ValueError, 'busy'):
                live.run_live('/dev/fake', self.run)
        popen.assert_not_called()
        self.assertFalse(self.run.exists())

    def test_stuck_child_is_reaped_after_timeout(self):
        child = self.child()
        child.wait.side_effect = [subprocess.TimeoutExpired('fake', 2), 0]
        live.stop_child(child)
        child.kill.assert_called_once()
        self.assertEqual(child.wait.call_count, 2)

    def test_builder_does_not_require_any_old_session(self):
        cmd = launch.build_live_command('/dev/fake', output_dir=self.run)
        self.assertIn('--port', cmd)
        self.assertNotIn('--session', cmd)
        self.assertEqual(cmd[cmd.index('--port') + 1], '/dev/fake')

    def test_gui_live_starts_capture_and_prediction_as_one_owned_job(self):
        rt = gui.Job()
        board = {'role': 'sink', 'port': '/dev/fake-new-port'}
        with mock.patch.object(gui, 'RT_JOB', rt), mock.patch.object(gui.JOB, 'proc', None), \
             mock.patch.object(gui, 'check_runtime'), mock.patch.object(gui, 'rescan', return_value={'ok': True}), \
             mock.patch.object(gui.Handler, 'boards_cache', [board]), \
             mock.patch.object(gui, 'new_output_dir', return_value=self.run), \
             mock.patch.object(rt, 'start', return_value=None) as start:
            result = gui.dispatch('/api/realtime/live', {})
        self.assertTrue(result['ok'])
        self.assertEqual(start.call_args.kwargs['mode'], 'live')
        self.assertEqual(start.call_args.kwargs['output_dir'], self.run / 'prediction')
        self.assertIn('/dev/fake-new-port', start.call_args.args[1])

    def test_gui_live_refuses_external_capture_and_duplicate_start(self):
        with mock.patch.object(gui.JOB, 'proc', mock.Mock()), mock.patch.object(gui, 'check_runtime') as probe:
            gui.JOB.proc.poll.return_value = None
            self.assertIn('error', gui.dispatch('/api/realtime/live', {}))
            probe.assert_not_called()
        rt = gui.Job()
        rt.proc = self.child()
        rt.mode = 'live'
        with mock.patch.object(gui, 'RT_JOB', rt):
            self.assertIn('error', gui.dispatch('/api/realtime/live', {}))
            self.assertIn('error', gui.dispatch('/api/collect', {'label': 'empty'}))
            self.assertIn('error', gui.rescan())

    def test_gui_refuses_missing_or_multiple_sinks(self):
        for boards in ([], [{'role': 'sink'}, {'role': 'sink'}]):
            with mock.patch.object(gui, 'RT_JOB', gui.Job()), mock.patch.object(gui.JOB, 'proc', None), \
                 mock.patch.object(gui, 'check_runtime'), mock.patch.object(gui, 'rescan', return_value={'ok': True}), \
                 mock.patch.object(gui.Handler, 'boards_cache', boards):
                self.assertIn('error', gui.dispatch('/api/realtime/live', {}))

    def test_gui_reads_nested_cli_live_status(self):
        p = self.run / 'prediction' / 'realtime_status.json'
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps({'running': True, 'mode': 'live', 'events': [], 't': 30}))
        self.assertEqual(gui.realtime_view(self.root / 'raw')['mode'], 'live')

    def test_cli_live_passes_port_without_old_session(self):
        with mock.patch('sys.argv', ['meshsense_cli', '--live', '--port', '/dev/fake', '--plain']), \
             mock.patch.object(cli, '_run_model', return_value=0) as run:
            self.assertEqual(cli.main(), 0)
        self.assertIsNone(run.call_args.args[0])
        self.assertEqual(run.call_args.kwargs['live_port'], '/dev/fake')

    def test_valid_input_recovers_from_suspended_before_class_is_confirmed(self):
        for probs in ((.2, .6, .2), (.1, .8, .1)):
            sm = StateMachine()
            sm.update(Reading(0, (0, 0, 0), 3, valid=False))
            self.assertEqual(sm.state, 'suspended')
            ev = sm.update(Reading(1, probs, 3, valid=True))
            self.assertEqual(sm.state, 'deciding')
            self.assertEqual(ev.to, 'deciding')
            self.assertEqual(sm.note, '')
            if max(probs) > .7:
                self.assertEqual(sm.pending[0], 'static')
                sm.update(Reading(16, probs, 3, valid=True))
                self.assertEqual(sm.state, 'static')


if __name__ == '__main__':
    unittest.main()
