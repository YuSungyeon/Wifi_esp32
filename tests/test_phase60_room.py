"""Room fine-tuning invariants and deployment routing (no serial connection)."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import numpy as np
import torch

from model_train.cnn1d.shared_cnn import SharedTemporalCNN
from model_train.phase60 import finetune_room as ft
from model_train.phase60 import predict as P
from realtime.models import load_model_bundle
from realtime.engine import Engine
from scripts import realtime_launch as launch
from test_phase60_realtime import frames


class RoomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(2)

    def test_class_domain_sampling_and_missing_class_rejection(self):
        source = np.array([0, 0, 0, 1, 2])
        target = np.array([0, 1, 1, 2])
        weights = ft.sampling_weights(source, target).numpy()
        self.assertAlmostEqual(weights[:len(source)].sum(), .5)
        self.assertAlmostEqual(weights[len(source):].sum(), .5)
        for y, w in [(source, weights[:len(source)]), (target, weights[len(source):])]:
            for c in range(3):
                self.assertAlmostEqual(w[y == c].sum(), 1/6)
        with self.assertRaises(ValueError):
            ft.sampling_weights(source, np.array([0, 1]))

    def test_cached_features_match_and_training_freezes_cnn_and_bn(self):
        torch.manual_seed(20)
        model = SharedTemporalCNN().eval()
        x = np.random.default_rng(20).normal(size=(6, 300, 156)).astype(np.float32)
        features = ft.frozen_features(model, x, batch=3)
        with torch.inference_mode():
            torch.testing.assert_close(model.head(features), model(torch.from_numpy(x)), rtol=0, atol=1e-6)
        before = {k: v.clone() for k, v in model.state_dict().items()}
        y = np.array([0, 1, 2, 0, 1, 2])
        ft.fit_head(model, features, y, features, y, 0, epochs=2, batch=3)
        self.assertTrue(any(not torch.equal(v, before[k]) for k, v in model.state_dict().items() if k.startswith('head.')))
        for k, v in model.state_dict().items():
            if k.startswith('encoders.'):
                torch.testing.assert_close(v, before[k], rtol=0, atol=0)
        self.assertFalse(model.training)
        self.assertTrue(all(not p.requires_grad for p in model.encoders.parameters()))

    def test_room_kind_rejects_original_and_reuses_phase_input(self):
        with self.assertRaisesRegex(ValueError, 'provenance'):
            load_model_bundle(launch.PHASE_MODEL, 'phase60_room')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = torch.load(launch.PHASE_MODEL, weights_only=True, map_location='cpu')
            parent['adaptation'] = {'method': ft.METHOD, 'parent_sha256': ft.digest(launch.PHASE_MODEL),
                                    'capture': 'synthetic-test'}
            checkpoint = root/'room.pt'; torch.save(parent, checkpoint)
            session = root/'capture'; session.mkdir()
            (session/'session.json').write_text(json.dumps({'devices': [
                {'device_id': rx, 'rx_id': rx-100} for rx in (101, 102, 103)]}))
            for rx in (101, 102, 103):
                (session/f'device_{rx}.csi').write_bytes(frames(np.arange(6030), rx-100))
            e = Engine(session, model_path=checkpoint, model_kind='phase60_room', output_dir=root/'pred')
            e.prepare_replay(); e.step(now=60)
            self.assertEqual(e.stream.model_kind, 'phase60')
            self.assertEqual(e.n_predictions, 1)
            self.assertEqual(e.view()['model_kind'], 'phase60_room')
            win = e.stream.latest(6000)
            expected = P.predict_window(win.phase, e.bundle, e.models)
            np.testing.assert_allclose(e.sm.probs, expected, rtol=0, atol=1e-6)
            rows = [json.loads(s) for s in (e.out/'realtime_predictions.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1]['model_kind'], 'phase60_room')
            self.assertEqual(rows[-1]['model_sha256'], ft.digest(checkpoint))
            self.assertEqual(e.window, 6000)
            e.close()

    def test_residual_range_rejects_boot_boundary_and_matches_offline(self):
        from scripts import csi_store as cs
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'session.json').write_text(json.dumps({'devices': [
                {'device_id': rx, 'rx_id': rx-100} for rx in (101, 102, 103)]}))
            for rx in (101, 102, 103):
                (root/f'device_{rx}.csi').write_bytes(frames(np.arange(27000), rx-100))
            _, data = ft.checked_session(root, {})
            actual = ft.residual_range(data, 0, 27000)
            from model_train.phase60 import features as F
            np.testing.assert_allclose(actual, F.session_phase(root), rtol=0, atol=1e-6)
            bad = dict(data); bad[103] = data[103].copy()
            bad[103]['hdr']['boot_id'][12000:] += 1
            with self.assertRaisesRegex(ValueError, 'boot boundary'):
                ft.residual_range(bad, 0, 27000)
            # The same reboot outside the selected interval is allowed.
            self.assertTrue(np.isfinite(ft.residual_range(bad, 0, 6000)).all())

    def test_annotations_out_of_range_overlap_and_changed_sources_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = root/'capture'; folder.mkdir()
            (folder/'session.json').write_text(json.dumps({'devices': [
                {'device_id': rx, 'rx_id': rx-100} for rx in (101, 102, 103)]}))
            for rx in (101, 102, 103):
                (folder/f'device_{rx}.csi').write_bytes(frames(np.arange(18010), rx-100))
            segments = [{'label': c, 'candidate_start_tx_seq_approx': 5+i*6000,
                         'candidate_end_tx_seq_exclusive_approx': 6005+i*6000,
                         'valid_start_tx_seq_approx': [5+i*6000], 'label_status': 'synthetic'}
                        for i, c in enumerate(ft.CLASSES)]
            summary = {'capture': str(root), 'hashes': {}, 'segments': segments}
            x, y, _ = ft.capture_windows(summary, {})
            self.assertEqual(x.shape, (3, 300, 156)); np.testing.assert_array_equal(y, [0, 1, 2])
            bad = copy.deepcopy(summary); bad['segments'][0]['valid_start_tx_seq_approx'] = [6]
            with self.assertRaisesRegex(ValueError, 'outside'):
                ft.capture_windows(bad, {})
            bad = copy.deepcopy(summary); bad['segments'][1]['candidate_start_tx_seq_approx'] = 6004
            with self.assertRaisesRegex(ValueError, 'overlapping'):
                ft.capture_windows(bad, {})
            summary['hashes'] = {str(folder/'session.json'): 'bad'}
            with self.assertRaisesRegex(ValueError, 'changed'):
                ft.capture_windows(summary, {})

    def test_room_cli_choice_and_launch_path(self):
        spec = launch.model_spec('phase60_room')
        self.assertEqual(spec['input_kind'], 'phase60')
        self.assertEqual(launch.resolve_model('phase60_room'), launch.ROOM_MODEL)
        cmd = launch.build_live_command('/dev/fake', output_dir=Path('/tmp/fixture'), model_kind='phase60_room')
        self.assertEqual(cmd[cmd.index('--model-kind')+1], 'phase60_room')
        for script in ('scripts/meshsense_cli.py', 'scripts/realtime_live.py', 'realtime/ui.py', 'realtime/engine.py'):
            proc = subprocess.run([str(launch.runtime_python()), script, '--help'],
                                  capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn('phase60_room', proc.stdout)


if __name__ == '__main__':
    unittest.main()
