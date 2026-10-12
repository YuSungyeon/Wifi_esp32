"""Frozen original vs room-finetuned phase60 models on the Oct 6 capture."""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.analysis import replay_transition as R
from model_train.phase60 import finetune_room as FT
from model_train.phase60 import features as F
from model_train.phase60.predict import predict_window
from realtime.models import load_model_bundle
from scripts.realtime_launch import PHASE_MODEL, ROOM_MODEL


def paired_inference(phase, starts, original, room, batch=8):
    """Identical frozen encoders permit exact shared features for both heads."""
    for old, new in zip(original, room):
        if any(not torch.equal(v, new.encoders.state_dict()[k])
               for k, v in old.encoders.state_dict().items()):
            raise ValueError('shared feature calculation requires identical encoders')
    out = {'phase_original': {}, 'phase_room': {}}
    began = time.perf_counter()
    for pos in range(0, len(starts), batch):
        sub = starts[pos:pos+batch]
        values = F.windows(phase, np.asarray(sub), 6000, 20)
        x = torch.from_numpy(np.ascontiguousarray(values))
        if not torch.isfinite(x).all():
            raise ValueError('invalid input')
        with torch.inference_mode():
            before, after = [], []
            for old, new in zip(original, room):
                features = FT.head_features(old, x)
                before.append(old.head(features).softmax(1))
                after.append(new.head(features).softmax(1))
            each = {'phase_original': torch.stack(before).numpy(),
                    'phase_room': torch.stack(after).numpy()}
        for name, scores in each.items():
            mean = scores.mean(0)
            if not np.isfinite(mean).all():
                raise ValueError('non-finite model scores')
            np.testing.assert_allclose(mean.sum(1), 1, atol=1e-6)
            for i, start in enumerate(sub):
                out[name][start] = {'scores': mean[i].tolist(), 'seed_scores': scores[:, i].tolist()}
        if pos % 400 == 0:
            print(f'paired phase: {min(pos+batch,len(starts))}/{len(starts)} windows', flush=True)
    return out, time.perf_counter()-began


def fully_contained_metrics(phase, anchor, predictions):
    """3s windows entirely inside each reported state, with 10s edge margins."""
    starts, labels, segments = [], [], []
    for truth in R.truth_segments():
        lo = int(np.ceil((truth['start_wall']+10-anchor)*100))
        hi = int(np.floor((truth['end_wall']-10-anchor)*100))
        candidates = list(range(lo, hi-6000+1, 300))
        valid = [s for s in candidates if np.isfinite(phase[s:s+6000]).all()]
        starts.extend(valid); labels.extend([R.CLASSES.index(truth['label'])]*len(valid))
        segments.append({'name': truth['name'], 'label': truth['label'],
                         'candidates': len(candidates), 'valid_windows': len(valid),
                         'starts_frame': valid})
    result = {}
    for model, scores in predictions.items():
        p = np.asarray([scores[s]['scores'] for s in starts])
        result[model] = FT.metrics(p, np.asarray(labels))
        result[model]['scope'] = 'untrained_oct6_recording_previously_inspected; fully-contained_60s_windows'
    return starts, segments, result


def run(output):
    output = output.resolve()
    if output == R.SESSION.resolve() or R.SESSION.resolve() in output.parents:
        raise ValueError('output must be outside original capture')
    output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    original_bundle, original = load_model_bundle(PHASE_MODEL, 'phase60')
    room_bundle, room = load_model_bundle(ROOM_MODEL, 'phase60_room')
    if room_bundle['adaptation']['parent_sha256'] != R.digest(PHASE_MODEL):
        raise ValueError('room model parent mismatch')
    adaptation_report = ROOM_MODEL.with_suffix('.metrics.json')
    training = json.loads(adaptation_report.read_text())
    raw_hashes = [R.digest(p) for p in R.SESSION.glob('device_*.csi')]
    if (any(h in training['input_hashes'].values() for h in raw_hashes)
            or room_bundle['adaptation']['capture'] == str(R.SESSION.parent.relative_to(ROOT))):
        raise ValueError('Oct 6 raw recording was used for fine-tuning')
    inputs = [*R.SESSION.glob('*'), *(R.SESSION.parent/'prediction').glob('*'),
              PHASE_MODEL, ROOM_MODEL, adaptation_report, Path(__file__), Path(R.__file__),
              Path(FT.__file__), Path(F.__file__), ROOT/'model_train/cnn1d/shared_cnn.py',
              ROOT/'model_train/preprocessing/preprocess_3rx.py', ROOT/'scripts/csi_store.py',
              ROOT/'model_train/analysis/verify_transition_replay.py']
    hashes = {str(p.relative_to(ROOT)): R.digest(p) for p in inputs if p.is_file()}
    R.save_json(output/'input-hashes.json', hashes)
    manifest, tx0, present, ready, interp, raw_amp, real, imag, amp, phase, quality = R.load_grids(R.SESSION)
    anchor = manifest['started_at_unix_us']/1e6
    duration = present.shape[1]/100
    schedules = {'prefix': R.make_schedule(present, ready, interp, 6000, True),
                 'whole_file': R.make_schedule(present, ready, interp, 6000, False)}
    checks = R.verify_prefix_inputs(schedules['prefix'], present, raw_amp, real, imag, phase, 'phase', 6000)
    # Extra starts only for state-internal diagnostics, never used to train/tune.
    diagnostic_starts = []
    for truth in R.truth_segments():
        lo = int(np.ceil((truth['start_wall']+10-anchor)*100))
        hi = int(np.floor((truth['end_wall']-10-anchor)*100))
        diagnostic_starts.extend(s for s in range(lo,hi-6000+1,300) if np.isfinite(phase[s:s+6000]).all())
    starts = sorted(set(diagnostic_starts) | {r['input_start_frame'] for rows in schedules.values() for r in rows if r['infer']})
    predictions, seconds = paired_inference(phase, starts, original, room)
    max_single_error = 0.
    for name, bundle, models in [('phase_original', original_bundle, original), ('phase_room', room_bundle, room)]:
        for start in (starts[0], starts[len(starts)//2], starts[-1]):
            ref = np.asarray(predict_window(phase[start:start+6000], bundle, models))
            got = np.asarray(predictions[name][start]['scores'])
            np.testing.assert_allclose(ref, got, rtol=1e-5, atol=1e-6)
            max_single_error = max(max_single_error, float(np.max(np.abs(ref-got))))
    results = {}
    for model in predictions:
        results[model] = {}
        for kind, template in schedules.items():
            rows = copy.deepcopy(template)
            R.finalize_rows(rows, predictions[model], anchor, tx0, output/f'{model}-{kind}.jsonl')
            R.save_json(output/f'{model}-{kind}-events.json', R.compact(rows, anchor))
            results[model][kind] = {'metrics': R.analyze(rows, anchor, duration),
                                  'after60s': R.analyze(rows, anchor, duration, trim=60),
                                  'time_shift_checks': [R.analyze(rows, anchor, duration, shift=s) for s in (-1,1)]}
    _, diagnostic_segments, diagnostics = fully_contained_metrics(phase, anchor, predictions)
    # Reproduce the established original-model result, rather than trust only new output.
    old = json.loads((ROOT/'model_train/analysis/output/20261008-transition-model-comparison/summary.json').read_text())
    for kind in schedules:
        for field in ('metrics', 'after60s'):
            reference = old['models']['phase'][kind][field]
            got = results['phase_original'][kind][field]
            np.testing.assert_allclose([got['overall'][k] for k in ('correct_s','unavailable_s')],
                                      [reference['overall'][k] for k in ('correct_s','unavailable_s')], atol=1e-6)
            for a,b in zip(got['segments'],reference['segments']):
                np.testing.assert_allclose(a['correct_s'],b['correct_s'],atol=1e-6)
    for name, expected in hashes.items():
        if R.digest(ROOT/name) != expected:
            raise ValueError(f'input changed: {name}')
    for bundle, models in [(original_bundle, original), (room_bundle, room)]:
        for model, state in zip(models,bundle['state_dicts']):
            assert all(torch.equal(v,state[k]) for k,v in model.state_dict().items())
    protocol = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
                'session': str(R.SESSION.relative_to(ROOT)), 'anchor_wall': anchor,
                'common_start_tx_seq': tx0, 'common_length_frames': present.shape[1],
                'stride_frames': 30, 'window_frames': 6000,
                'model_hashes': {'phase_original': R.digest(PHASE_MODEL), 'phase_room': R.digest(ROOM_MODEL)},
                'time_mapping': 'same approximate manifest/TX mapping as the Oct 8 replay',
                'training_performed': False, 'oct6_raw_used_in_finetuning': False,
                'previously_inspected_diagnostic_data': True,
                'shared_identical_frozen_encoder_seconds': seconds,
                'unique_windows_including_diagnostics': len(starts), 'quality': quality}
    R.save_json(output/'protocol.json', protocol)
    R.save_json(output/'truth.json', {'segments': R.truth_segments(), 'static_onset_is_sitting_start': True})
    summary = {'models': results, 'state_internal_windows': diagnostics,
               'diagnostic_segments': diagnostic_segments, 'quality': quality,
               'checks': {'hashes_unchanged': True, 'verified_files': len(hashes),
                          'model_state_unchanged': True, 'oct6_not_used_for_finetuning': True,
                          'original_result_reproduced': True, 'direct_prefix_checks': checks,
                          'single_window_max_score_error': max_single_error}}
    R.save_json(output/'summary.json', summary)
    print(json.dumps({name: result['prefix']['metrics'] for name,result in results.items()},ensure_ascii=False),flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output)
