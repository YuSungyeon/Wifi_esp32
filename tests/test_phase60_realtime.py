"""Selectable phase60 realtime contracts, without opening any serial port."""
import hashlib
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))

import csi_store as cs
import meshsense_gui as gui
import meshsense_cli as cli
import realtime_live as live
import realtime_launch as launch
from model_train.phase60 import features as F, predict as P
from realtime.engine import Engine, PREDICTIONS_NAME
from realtime.models import load_model_bundle
from realtime.stream import CsiStream
from realtime import ui


def frames(seq, wire, *, boot=1, channel=11, phase_offset=0):
    seq = np.asarray(seq)
    data = np.zeros(len(seq), cs.CSI_FRAME_DTYPE)
    h = data['hdr']
    h['magic'], h['version'] = cs.FRAME_MAGIC, cs.FRAME_VERSION
    h['frame_type'], h['total_len'], h['raw_len'] = cs.FRAME_TYPE_CSI, cs.CSI_FRAME_SIZE, 128
    h['seq'], h['tx_seq'] = np.arange(len(seq))+1, seq
    h['channel'], h['rssi'], h['rate'], h['sig_len'] = channel, -40, 11, 47
    h['boot_id'], h['rx_id'], h['gain_comp'] = boot, wire, 1
    t, f = seq[:, None], np.arange(64)[None, :]
    phi = (.03*f + .3*np.sin(.007*t)*np.sin(.18*f)
           + .08*np.cos(.09*t+.1*f) + .12*wire*np.sin(.005*t+.2*f) + phase_offset)
    amp = 12 + 3*np.sin(.03*t+.2*f)
    data['raw'][:, 0::2] = np.rint(amp*np.sin(phi)).astype(np.int8)
    data['raw'][:, 1::2] = np.rint(amp*np.cos(phi)).astype(np.int8)
    h['crc32'] = [cs.crc32_of(row.tobytes()) for row in data]
    return data.tobytes()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.session = self.root / 'raw/20261010/fixture'
        self.session.mkdir(parents=True)
        (self.session/'session.json').write_text(json.dumps({
            'label': 'not-a-class',
            'devices': [{'device_id': rx, 'rx_id': rx-100} for rx in F.RX_ORDER],
        }))

    def write(self, seq, *, rx=None, append=False, **kwargs):
        for receiver in (rx,) if rx else F.RX_ORDER:
            with (self.session/f'device_{receiver}.csi').open('ab' if append else 'wb') as fp:
                fp.write(frames(seq, receiver-100, **kwargs))

    def engine(self):
        return Engine(self.session, model_kind='phase60', output_dir=self.root/'prediction')


class PhaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(2)
        cls.bundle = torch.load(launch.PHASE_MODEL, map_location='cpu', weights_only=True)

    def test_phase_loader_accepts_original_checkpoint_and_cpu_eval(self):
        b, models = load_model_bundle(launch.PHASE_MODEL, 'phase60')
        self.assertEqual(b['window_frames'], 6000)
        self.assertEqual(len(models), 3)
        self.assertTrue(all(not m.training for m in models))

    def test_every_phase_contract_field_is_checked(self):
        changes = {'format_version': 2, 'feature_version': 'wrong', 'mode': 'A',
                   'window_frames': 3000, 'downsample': 10, 'sample_rate_hz': 50,
                   'class_names': ['motion', 'static', 'empty'],
                   'state_dicts': self.bundle['state_dicts'][:2], 'config': {'seeds': [2, 1, 0]},
                   'stride_frames': 0, 'rx_order': [103, 102, 101], 'raw_tone_order': list(range(52))}
        for key, value in changes.items():
            with self.subTest(key=key), mock.patch.object(P.torch, 'load', return_value=dict(self.bundle, **{key:value})):
                with self.assertRaisesRegex(ValueError, 'contract'):
                    P.load_bundle(launch.PHASE_MODEL)

    def test_wrong_architecture_has_a_clear_error(self):
        b = dict(self.bundle, state_dicts=[{}]*3)
        with mock.patch.object(P.torch, 'load', return_value=b), self.assertRaisesRegex(ValueError, 'model state'):
            load_model_bundle(launch.PHASE_MODEL, 'phase60')

    def test_amplitude_and_phase_checkpoints_cannot_be_interchanged(self):
        for kind, path in [('amplitude', launch.PHASE_MODEL), ('phase60', launch.MODEL)]:
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, 'contract'):
                load_model_bundle(path, kind)

    def test_one_window_requires_correct_shape_finite_input_and_three_seeds(self):
        for a, models in [(np.zeros((3000,156),np.float32), [None]*3),
                          (np.zeros((6000,153),np.float32), [None]*3),
                          (np.full((6000,156),np.nan,np.float32), [None]*3),
                          (np.zeros((6000,156),np.float32), [None]*2)]:
            with self.assertRaises(ValueError):
                P.predict_window(a, self.bundle, models)


class PhaseStreamTests(Fixture):
    def test_input_and_scores_match_offline_with_short_gap_and_duplicate(self):
        seq = np.arange(27020)
        for rx, begin, end in [(101,0,27020),(102,2,27018),(103,1,27019)]:
            used = seq[begin:end]
            if rx == 102:
                used = used[(used < 21015) | (used > 21019)]  # five-frame gap near window start
            blob = frames(used, rx-100)
            # A conflicting duplicate at the final sequence must never replace the first frame.
            blob += frames([used[-1]], rx-100, phase_offset=.7)
            (self.session/f'device_{rx}.csi').write_bytes(blob)
        original = {p.name:p.read_bytes() for p in self.session.iterdir()}
        expected = F.session_phase(self.session)
        stream = CsiStream(self.session, capacity=30000, model_kind='phase60')
        stream.poll()
        w = stream.latest(6000)
        self.assertIsNone(w.amp)
        self.assertEqual(w.phase.shape, (6000,156))
        self.assertEqual(w.tx_seq0, 21018)
        np.testing.assert_allclose(w.phase, expected[-6000:], rtol=0, atol=1e-6)
        b, models = load_model_bundle(launch.PHASE_MODEL, 'phase60')
        weights = [{k:v.clone() for k,v in m.state_dict().items()} for m in models]
        x = torch.from_numpy(F.windows(expected, np.array([len(expected)-6000]), 6000, 20))
        with torch.inference_mode():
            scores = torch.stack([m(x).softmax(1) for m in models]).mean(0)[0].numpy()
        np.testing.assert_allclose(P.predict_window(w.phase,b,models), scores, rtol=0, atol=1e-6)
        for m,state in zip(models,weights):
            for key,value in m.state_dict().items():
                torch.testing.assert_close(value,state[key],rtol=0,atol=0)
        self.assertEqual(original, {p.name:p.read_bytes() for p in self.session.iterdir()})

    def test_follow_waits_for_sixty_seconds_and_all_three_rx(self):
        self.write(np.arange(5999))
        stream = CsiStream(self.session,model_kind='phase60')
        stream.poll()
        self.assertIsNone(stream.latest(6000))
        self.write([5999],rx=101,append=True)
        self.write([5999],rx=102,append=True)
        stream.poll()
        self.assertIsNone(stream.latest(6000))
        self.write([5999],rx=103,append=True)
        stream.poll()
        self.assertTrue(np.isfinite(stream.latest(6000).phase).all())

    def test_replay_allows_sixty_seconds_but_offline_gate_stays_27000(self):
        self.write(np.arange(6000))
        with self.assertRaisesRegex(ValueError, '27000'):
            F.session_phase(self.session)
        s=CsiStream(self.session,model_kind='phase60')
        s.load_replay(); s.rewind(6000)
        self.assertEqual(s.latest(6000).tx_seq0,0)
        self.assertFalse(s.advance(30))

    def test_short_replay_and_missing_rx_are_rejected(self):
        self.write(np.arange(5999))
        with self.assertRaisesRegex(ValueError,'60초'):
            CsiStream(self.session,model_kind='phase60').load_replay()
        (self.session/'device_103.csi').unlink()
        with self.assertRaisesRegex(ValueError,'RX 3대'):
            CsiStream(self.session,model_kind='phase60').load_replay()

    def test_long_replay_preserves_first_window_and_limits_receiver_ahead(self):
        for rx,n in [(101,31020),(102,30001),(103,30010)]:
            self.write(np.arange(n)+200,rx=rx)
        s=CsiStream(self.session,model_kind='phase60')
        s.load_replay(); s.rewind(6000)
        self.assertEqual(s.capacity,30001)
        self.assertEqual(s.latest(6000).tx_seq0,200)
        self.assertTrue(s.advance(24001))
        self.assertEqual(s.latest(6000).tx_seq0,24201)
        self.assertFalse(s.advance(30))

    def test_interpolation_does_not_borrow_right_context_from_receiver_ahead(self):
        self.write([0,1,2,4,5],rx=101)
        self.write([0,1,2,3],rx=102)
        self.write([0,1,2,3],rx=103)
        s=CsiStream(self.session,model_kind='phase60');s.poll()
        self.assertTrue(np.isnan(s.latest(4).phase[3,:52]).all())
        self.write([4],rx=102,append=True);self.write([4],rx=103,append=True);s.poll()
        self.assertTrue(np.isfinite(s.latest(4).phase).all())

    def test_five_missing_frames_are_filled_but_six_remain_invalid(self):
        self.write([0,1,7,14])
        s=CsiStream(self.session,model_kind='phase60');s.poll()
        w=s.latest(15)
        self.assertTrue(np.isfinite(w.phase[2:7]).all())
        self.assertTrue(np.isnan(w.phase[8:14]).all())

    def test_partial_frame_and_cross_poll_duplicate_keep_first_iq(self):
        self.write([0,1])
        p=self.session/'device_101.csi';blob=p.read_bytes();p.write_bytes(blob[:-20])
        s=CsiStream(self.session,model_kind='phase60');self.assertEqual(s.poll(),5)
        p.write_bytes(blob);self.assertEqual(s.poll(),1)
        first=s.latest(1).phase.copy()
        self.write([1],append=True,phase_offset=.7);s.poll()
        np.testing.assert_array_equal(s.latest(1).phase,first)

    def test_corrupt_crc_rf_identity_and_regression_are_rejected(self):
        bad=bytearray(frames([1],1));bad[-1]^=1
        for blob,reason in [(bad,'crc'),(frames([1],1,channel=6),'RF'),
                            (frames([1],9),'식별'),(frames([2,1],1),'역행')]:
            with self.subTest(reason=reason):
                (self.session/'device_101.csi').write_bytes(blob)
                with self.assertRaisesRegex(ValueError,reason):
                    CsiStream(self.session,model_kind='phase60').poll()

    def test_boot_change_and_file_shrink_are_rejected(self):
        self.write([0,1])
        s=CsiStream(self.session,model_kind='phase60');s.poll()
        self.write([2],rx=101,append=True,boot=2)
        with self.assertRaisesRegex(ValueError,'재부팅'):s.poll()
        self.write([0,1]);s=CsiStream(self.session,model_kind='phase60');s.poll()
        (self.session/'device_101.csi').write_bytes(b'')
        with self.assertRaisesRegex(ValueError,'축소'):s.poll()

    def test_live_ring_stays_bounded_after_a_large_forward_jump(self):
        self.write(np.arange(6000))
        s=CsiStream(self.session,model_kind='phase60');s.poll()
        allocated=s._amp.nbytes+s._present.nbytes
        self.write(np.arange(40000,46000),append=True);s.poll()
        self.assertEqual(s._amp.nbytes+s._present.nbytes,allocated)
        self.assertEqual(allocated,30024000)
        self.assertEqual(s.latest(6000).tx_seq0,40000)
        self.assertTrue(np.isfinite(s.latest(6000).phase).all())

    def test_replay_rejects_boot_boundary_instead_of_hiding_it(self):
        self.write(np.arange(6000))
        self.write([6000],append=True,boot=2)
        with self.assertRaisesRegex(ValueError,'재부팅'):
            CsiStream(self.session,model_kind='phase60').load_replay()


class PhaseEngineTests(Fixture):
    def test_warmup_stride_gate_stale_suspend_and_recovery_are_logged(self):
        self.write(np.arange(5999));e=self.engine()
        with mock.patch('realtime.stream.time.monotonic',return_value=100):
            e.step(now=59.99)
            self.assertEqual(e.view()['warmup_remaining_s'],.01)
            self.assertEqual(e.sm.state,'deciding')
            self.write([5999],append=True);e.step(now=60)
            self.assertEqual(e.n_predictions,1)
            self.assertEqual(e.sm.state,('empty','static','motion')[np.argmax(e.sm.probs)])
            self.write(np.arange(6000,6015),append=True);e.step(now=60.15)
            self.assertEqual(e.n_predictions,1)
            self.write(np.arange(6015,6030),append=True);e.step(now=60.3)
            self.assertEqual(e.n_predictions,2)
        with mock.patch('realtime.stream.time.monotonic',return_value=104):
            e.step(now=64)
            self.assertEqual(e.sm.state,'suspended')
        with mock.patch('realtime.stream.time.monotonic',return_value=105):
            self.write(np.arange(6030,6060),append=True);e.step(now=65)
            self.assertNotIn(e.sm.state,('deciding','suspended'))
        e.close()
        rows=[json.loads(x) for x in (e.out/PREDICTIONS_NAME).read_text().splitlines()]
        self.assertEqual([r['reason'] for r in rows], ['warmup','inference','waiting_for_new_common_frames',
                         'inference','rx_missing','inference','stopped'])
        inferred=[r for r in rows if r['inferred']]
        self.assertEqual([r['input_end_tx_seq_exclusive'] for r in inferred],[6000,6030,6060])
        for r in inferred:
            self.assertEqual(r['input_end_tx_seq_exclusive']-r['input_start_tx_seq'],6000)
            self.assertAlmostEqual(sum(r['probs']),1,places=6)
            self.assertEqual(r['model_sha256'], hashlib.sha256(launch.PHASE_MODEL.read_bytes()).hexdigest())
            self.assertEqual(r['model_kind'],'phase60')
            self.assertIn('wall',r);self.assertIn('monotonic',r)
        self.assertFalse(json.loads((e.out/'realtime_status.json').read_text())['running'])

    def test_six_frame_gap_suspends_until_it_leaves_sixty_second_window(self):
        self.write(np.r_[0:100,106:6106]);e=self.engine()
        e.prepare_replay();e.step(now=60)
        self.assertEqual(e.sm.state,'suspended')
        self.assertEqual(e.n_predictions,0)
        self.assertTrue(e.stream.advance(106));e.step(now=61.06)
        self.assertEqual(e.n_predictions,1)
        self.assertNotIn(e.sm.state,('deciding','suspended'))

    def test_two_receiver_input_is_not_zero_filled_or_inferred(self):
        self.write(np.arange(6000),rx=101);self.write(np.arange(6000),rx=102)
        e=self.engine();e.step(now=60)
        self.assertEqual(e.n_predictions,0)
        self.assertEqual(e.view()['n_rx'],2)

    def test_error_and_close_write_failure_reason_and_no_class_probability(self):
        e=self.engine();e.record_failure('fixture-error');e.close()
        rows=[json.loads(x) for x in (e.out/PREDICTIONS_NAME).read_text().splitlines()]
        self.assertEqual([r['reason'] for r in rows],['error','stopped'])
        self.assertEqual(rows[0]['probs'],[0,0,0])
        self.assertIn('fixture-error',rows[0]['note'])

    def test_terminal_warmup_and_footer_use_selected_window(self):
        e=self.engine();v=e.view()
        self.assertIn('60초 입력 준비',ui.render_plain(v))
        self.assertIn('60초 입력 준비',ui.render(v))
        self.assertIn('위상 60초',ui.render(v))
        self.assertLessEqual(ui._cells(ui.render(v).splitlines()[-2][1:-1]),60)


class PhaseLaunchTests(Fixture):
    def test_both_builders_pass_kind_path_and_default_thirty_frame_stride(self):
        for kind,path in [('amplitude',launch.MODEL),('phase60',launch.PHASE_MODEL)]:
            for cmd in [launch.build_command(self.session,model_kind=kind),
                        launch.build_live_command('/dev/fake',output_dir=self.root/'live',model_kind=kind)]:
                self.assertEqual(cmd[cmd.index('--model-kind')+1],kind)
                self.assertEqual(cmd[cmd.index('--model')+1],str(path))
                self.assertEqual(cmd[cmd.index('--stride')+1],'30')

    def test_unknown_kinds_rejected_and_runtime_lists_model_choices(self):
        for kind in ('unknown',None,{}):
            with self.assertRaises(ValueError):launch.resolve_model(kind)
        self.assertEqual([(m['kind'],m['window_s']) for m in launch.runtime_info()['models']],
                         [('amplitude',30),('phase60',60),('phase60_room',60)])

    def test_gui_replay_follow_and_live_pass_kind_without_touching_other_job(self):
        rt=gui.Job()
        with mock.patch.object(gui,'RT_JOB',rt), mock.patch.object(gui.JOB,'proc',None), \
             mock.patch.object(gui,'RAW_ROOT',self.root/'raw'),mock.patch.object(gui,'check_runtime'), \
             mock.patch.object(gui,'new_output_dir',return_value=self.root/'out'), \
             mock.patch.object(gui,'rescan',return_value={'ok':True}), \
             mock.patch.object(gui.Handler,'boards_cache',[{'role':'sink','port':'/dev/fake'}]), \
             mock.patch.object(rt,'start',return_value=None) as start, mock.patch.object(gui.JOB,'stop') as stop:
            for endpoint,body in [('/api/realtime/start',{'path':str(self.session),'follow':False}),
                                  ('/api/realtime/start',{'path':str(self.session),'follow':True}),
                                  ('/api/realtime/live',{})]:
                result=gui.dispatch(endpoint,dict(body,model_kind='phase60'))
                self.assertTrue(result['ok'])
                self.assertEqual(start.call_args.kwargs['model_kind'],'phase60')
                cmd=start.call_args.args[1]
                self.assertEqual(cmd[cmd.index('--model-kind')+1],'phase60')
            gui.dispatch('/api/realtime/stop',{})
            stop.assert_not_called()

    def test_gui_wrong_kind_is_rejected_before_port_scan(self):
        with mock.patch.object(gui,'RT_JOB',gui.Job()),mock.patch.object(gui.JOB,'proc',None), \
             mock.patch.object(gui,'rescan') as scan:
            with self.assertRaises(ValueError):gui.dispatch('/api/realtime/live',{'model_kind':'wrong'})
        scan.assert_not_called()

    def test_cli_direct_modes_and_menu_preserve_phase_choice(self):
        for args in [['--realtime',str(self.session),'--plain'],['--live','--port','/dev/fake']]:
            with mock.patch('sys.argv',['cli',*args,'--model-kind','phase60']), \
                 mock.patch.object(cli,'_run_model',return_value=0) as run:
                self.assertEqual(cli.main(),0)
                self.assertEqual(run.call_args.kwargs['model_kind'],'phase60')
        with mock.patch.object(cli,'_choose',side_effect=[1,0]),mock.patch('builtins.input',return_value='/dev/fake'), \
             mock.patch.object(cli,'_run_model') as run:
            cli._model_menu()
            self.assertEqual(run.call_args.kwargs['model_kind'],'phase60')

    def test_cli_phase_replay_really_runs_and_saves_sixty_second_predictions(self):
        self.write(np.arange(6010))
        proc=subprocess.run([str(launch.runtime_python()),str(ROOT/'scripts/meshsense_cli.py'),
                             '--realtime',str(self.session),'--model-kind','phase60','--plain',
                             '--output-dir',str(self.root/'cli')],cwd=ROOT,capture_output=True,text=True,timeout=30)
        self.assertEqual(proc.returncode,0,proc.stdout+proc.stderr)
        self.assertIn('위상 60초',proc.stdout)
        v=json.loads((self.root/'cli/realtime_status.json').read_text())
        self.assertEqual(v['model_kind'],'phase60')
        self.assertEqual(v['mode'],'replay');self.assertEqual(v['window_s'],60)
        self.assertEqual(v['predictions'],1);self.assertFalse(v['running'])

    def test_gui_phase_replay_really_runs_its_selected_child(self):
        self.write(np.arange(6010));rt=gui.Job()
        with mock.patch.object(gui,'RT_JOB',rt),mock.patch.object(gui.JOB,'proc',None), \
             mock.patch.object(gui,'RAW_ROOT',self.root/'raw'), \
             mock.patch.object(gui,'new_output_dir',return_value=self.root/'gui'):
            result=gui.dispatch('/api/realtime/start',{'path':str(self.session),'model_kind':'phase60'})
            self.assertTrue(result['ok']);self.addCleanup(rt.stop)
            self.assertEqual(rt.state()['model_kind'],'phase60')
            self.assertEqual(rt.proc.wait(timeout=30),0)
        v=json.loads((self.root/'gui/realtime_status.json').read_text())
        self.assertEqual(v['model_kind'],'phase60');self.assertEqual(v['predictions'],1)

    def test_live_phase_child_failure_stops_reader_and_records_selected_model(self):
        reader=mock.Mock();reader.poll.return_value=None
        with mock.patch.object(live,'ensure_port_free'),mock.patch.object(live,'check_runtime'), \
             mock.patch.object(live,'repo_provenance',return_value={}), \
             mock.patch.object(live.subprocess,'Popen',side_effect=[reader,OSError('fixture-start-error')]) as popen:
            self.assertEqual(live.run_live('/dev/fake',self.root/'live',model_kind='phase60'),1)
        reader.send_signal.assert_called_once_with(signal.SIGINT)
        self.assertEqual(popen.call_args_list[0].args[0][popen.call_args_list[0].args[0].index('--flush-interval')+1],'0.1')
        self.assertIn('phase60',popen.call_args_list[1].args[0])
        v=json.loads((self.root/'live/prediction/realtime_status.json').read_text())
        self.assertEqual(v['state'],'suspended');self.assertEqual(v['window_s'],60)
        rows=[json.loads(x) for x in (self.root/'live/prediction'/PREDICTIONS_NAME).read_text().splitlines()]
        self.assertEqual(rows[-1]['reason'],'supervisor_error')
        self.assertEqual(rows[-1]['model_kind'],'phase60')

    def test_normal_phase_live_stop_owns_only_started_children(self):
        reader,prediction=mock.Mock(),mock.Mock()
        reader.poll.return_value=prediction.poll.return_value=None
        stop=mock.Mock();stop.is_set.return_value=False;stop.wait.return_value=True
        with mock.patch.object(live,'ensure_port_free'),mock.patch.object(live,'check_runtime'), \
             mock.patch.object(live,'repo_provenance',return_value={}), \
             mock.patch.object(live.subprocess,'Popen',side_effect=[reader,prediction]):
            self.assertEqual(live.run_live('/dev/fake',self.root/'live',model_kind='phase60',stop=stop),0)
        reader.send_signal.assert_called_once_with(signal.SIGINT)
        prediction.send_signal.assert_called_once_with(signal.SIGTERM)
        v=json.loads((self.root/'live/prediction/realtime_status.json').read_text())
        self.assertFalse(v['running']);self.assertEqual(v['model_kind'],'phase60')
        rows=[json.loads(x) for x in (self.root/'live/prediction'/PREDICTIONS_NAME).read_text().splitlines()]
        self.assertEqual(rows[-1]['reason'],'supervisor_stopped')

    @unittest.skipUnless(shutil.which('node'),'Node is needed to execute the GUI renderer')
    def test_gui_javascript_renders_phase_warmup_and_retains_active_choice_after_refresh(self):
        source=gui.JS[gui.JS.index('const RT_LABEL'):gui.JS.index('function tog(')]
        prelude='''const assert=require('node:assert/strict');
          let rtModel='amplitude',rtSelected='';
          const esc=s=>String(s??'');
          let S={realtime_job:{running:true,model_kind:'phase60',mode:'live',log:[]},job:{running:false},
            sessions:[],realtime_runtime:REPLACE_RUNTIME,realtime:null};
        '''.replace('REPLACE_RUNTIME',json.dumps(launch.runtime_info()))
        checks='''const controls=renderRealtimeControls();
          assert.equal(rtModel,'phase60');
          assert.ok(controls.includes('id="rt-model" disabled'));
          assert.ok(controls.includes('value="phase60" selected'));
          assert.ok(controls.includes('처음 60초'));
          const html=renderRealtime({state:'deciding',window_s:60,warmup_remaining_s:30,
            model_label:'위상 60초 (시험 후보)',mode:'live',active:true,confidence:0,held_s:0,
            remaining_s:0,threshold:0,t:0,n_rx:3,hz:100,events:[]});
          assert.ok(html.includes('60초 입력 준비'));
          assert.ok(html.includes('width:50%'));
          assert.ok(html.includes('위상 60초 (시험 후보)'));
          S.realtime_job.running=false;rtModel='amplitude';
          assert.ok(renderRealtimeControls().includes('처음 30초'));
          assert.equal(rtModel,'amplitude');
        '''
        proc=subprocess.run(['node','-e',prelude+source+checks],capture_output=True,text=True,timeout=10)
        self.assertEqual(proc.returncode,0,proc.stderr)


if __name__=='__main__':
    unittest.main()
