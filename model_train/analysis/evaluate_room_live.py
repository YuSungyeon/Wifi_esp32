"""Compare frozen phase models on the inputs and wall times of a logged live run."""
import argparse
import copy
from datetime import datetime
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.analysis import replay_transition as R
from model_train.analysis.evaluate_room_on_oct6 import paired_inference
from model_train.phase60 import features as F
from model_train.phase60.predict import predict_window
from realtime.models import load_model_bundle
from scripts.realtime_launch import PHASE_MODEL, ROOM_MODEL


def read_truth(info):
    text = Path(info).read_text()
    date = re.search(r'측정 날짜:\s*(\d{4}-\d{2}-\d{2})', text).group(1)
    run = re.search(r'실시간 데이터 폴더명:\s*([\w-]+)', text).group(1)
    times = {}
    for number, clock in re.findall(r'^\s*(\d+)\.[^\n]*?:\s*(\d{2}:\d{2}:\d{2})', text, re.M):
        if int(number) in times:
            raise ValueError('duplicate action number')
        times[int(number)] = datetime.fromisoformat(date+'T'+clock).replace(tzinfo=ZoneInfo('Asia/Seoul')).timestamp()
    if sorted(times) != list(range(1, 15)) or any(times[i] > times[i+1] for i in range(1, 14)):
        raise ValueError('expected 14 ordered same-day action times')
    pairs = [(2, 3, 'empty', '집 밖 대기 1'), (5, 6, 'motion', '방 안 걷기'),
             (7, 8, 'static', '침대 앉기'), (9, 10, 'static', '중문 밖 대기'),
             (11, 12, 'empty', '집 밖 대기 2')]
    segments = [{'start_wall': times[a], 'end_wall': times[b], 'label': label, 'name': name}
                for a, b, label, name in pairs]
    if any(s['end_wall']-s['start_wall'] <= 60 for s in segments):
        raise ValueError('each labeled segment must exceed 60 seconds')
    return run, segments


def compact_rows(rows):
    events = []
    for row in rows:
        if not events or events[-1]['state'] != row['state']:
            events.append({'wall': row['wall'], 'state': row['state']})
    return events


def analyze(rows, truth, end, trim=0, shift=0):
    intervals = [{'wall': r['wall']+shift, 'state': r['state']} for r in compact_rows(rows)]
    segments = [{**s, 'evaluation_start_wall': s['start_wall']+trim,
                 **R.segment_metrics(intervals, s['start_wall']+trim, s['end_wall'], s['label'], end+shift)}
                for s in truth]
    def aggregate(group):
        total = sum(s['duration_s'] for s in group)
        valid = sum(s['valid_s'] for s in group)
        correct = sum(s['correct_s'] for s in group)
        return {'duration_s': total, 'valid_s': valid, 'correct_s': correct, 'unavailable_s': total-valid,
                'match_among_valid': correct/valid if valid else None,
                'match_among_all': correct/total if total else None}
    by_class = {c: aggregate([s for s in segments if s['label'] == c]) for c in R.CLASSES}
    return {'segments': segments, 'overall': aggregate(segments), 'by_class': by_class,
            'macro_class_match_among_valid': float(np.mean([v['match_among_valid'] for v in by_class.values()]))
                if all(v['match_among_valid'] is not None for v in by_class.values()) else None,
            'trim_seconds': trim, 'prediction_wall_shift_s': shift}


def rebuild_rows(log, scores, tx0, begin, end):
    out = [{'wall': begin, 'state': 'deciding', 'infer': False, 'reason': 'before_first_log'}]
    state = 'deciding'
    for row in log:
        if row['wall'] < begin or row['wall'] > end:
            continue
        item = {'wall': row['wall'], 'infer': row['inferred'], 'reason': row['reason']}
        if row['inferred']:
            start = row['input_start_tx_seq']-tx0
            item.update(scores[start])
            item.update(input_start_tx_seq=row['input_start_tx_seq'],
                        input_end_tx_seq_exclusive=row['input_end_tx_seq_exclusive'])
            state = R.CLASSES[int(np.argmax(item['scores']))]
        elif row['reason'] not in ('waiting_for_new_common_frames', 'stopped', 'supervisor_stopped'):
            if row['state'] not in ('deciding', 'suspended'):
                raise ValueError('unexpected non-inference state')
            state = row['state']
        item['state'] = state
        out.append(item)
    return out


def verify_time_metrics(rows, truth, end, saved, trim):
    """Independently sum overlaps of every logged row, without compacting events."""
    times = np.array([r['wall'] for r in rows]); stops = np.r_[times[1:], end]
    states = np.array([r['state'] for r in rows])
    if not (np.diff(times) >= 0).all():
        raise ValueError('unordered prediction times')
    for action, result in zip(truth, saved['segments']):
        duration = np.maximum(0, np.minimum(stops, action['end_wall'])-np.maximum(times, action['start_wall']+trim))
        np.testing.assert_allclose([duration.sum(), duration[states == action['label']].sum(),
                                    duration[np.isin(states, R.CLASSES)].sum()],
                                   [result['duration_s'], result['correct_s'], result['valid_s']], atol=1e-6)


def run(info, output):
    info = info.resolve(); output = output.resolve()
    run_name, truth = read_truth(info)
    session = ROOT/'mac_collector_output/realtime'/run_name/'capture'
    if (ROOT/'mac_collector_output').resolve() in output.parents:
        raise ValueError('evaluation output must be outside original captures')
    output.mkdir(parents=True, exist_ok=False)
    log_path = session.parent/'prediction/realtime_predictions.jsonl'
    log = [json.loads(line) for line in log_path.read_text().splitlines()]
    if any(a['wall'] > b['wall'] for a, b in zip(log, log[1:])):
        raise ValueError('nonmonotonic logged wall times')
    torch.set_num_threads(2)
    parent, original = load_model_bundle(PHASE_MODEL, 'phase60')
    adapted, room = load_model_bundle(ROOM_MODEL, 'phase60_room')
    room_hash = R.digest(ROOM_MODEL)
    if adapted['adaptation']['parent_sha256'] != R.digest(PHASE_MODEL):
        raise ValueError('parent checkpoint mismatch')
    if any(r['model_kind'] != 'phase60_room' or r['model_sha256'] != room_hash for r in log):
        raise ValueError('live checkpoint differs from comparison checkpoint')
    training_path = ROOM_MODEL.with_suffix('.metrics.json')
    training = json.loads(training_path.read_text())
    if any(R.digest(p) in training['input_hashes'].values() for p in session.glob('device_*.csi')):
        raise ValueError('test capture was used for adaptation')
    inputs = [*session.glob('*'), *(session.parent/'prediction').glob('*'), info,
              *info.parent.glob('*.jpeg'), PHASE_MODEL, ROOM_MODEL, training_path,
              Path(__file__), Path(R.__file__), Path(F.__file__),
              ROOT/'model_train/analysis/evaluate_room_on_oct6.py',
              ROOT/'model_train/phase60/finetune_room.py', ROOT/'model_train/cnn1d/shared_cnn.py',
              ROOT/'model_train/preprocessing/preprocess_3rx.py', ROOT/'scripts/csi_store.py']
    hashes = {str(p): R.digest(p) for p in inputs if p.is_file()}
    R.save_json(output/'input-hashes.json', hashes)
    manifest, tx0, present, ready, interp, amp_raw, real, imag, amp, phase, quality = R.load_grids(session)
    begin, end = manifest['started_at_unix_us']/1e6, manifest['ended_at_unix_us']/1e6
    if not all(begin <= s['start_wall'] < s['end_wall'] <= end for s in truth):
        raise ValueError('truth outside capture')
    inferred = [r for r in log if r['inferred']]
    starts = sorted({r['input_start_tx_seq']-tx0 for r in inferred})
    for row in inferred:
        s, e = row['input_start_tx_seq']-tx0, row['input_end_tx_seq_exclusive']-tx0
        if (s < 0 or e > len(phase) or e-s != 6000 or not np.isfinite(phase[s:e]).all()
                or (ready[:, s:e] >= e).any()):
            raise ValueError('logged inference cannot be reconstructed causally')
    template = [{'infer': True, 'state': 'pending', 'input_start_frame': s,
                 'input_end_frame_exclusive': s+6000} for s in starts]
    prefix_checks = R.verify_prefix_inputs(template, present, amp_raw, real, imag, phase, 'phase', 6000)
    predictions, seconds = paired_inference(phase, starts, original, room)
    actual = np.asarray([r['probs'] for r in inferred])
    reconstructed = np.asarray([predictions['phase_room'][r['input_start_tx_seq']-tx0]['scores'] for r in inferred])
    error = float(np.max(np.abs(actual-reconstructed)))
    np.testing.assert_allclose(actual, reconstructed, rtol=1e-5, atol=1e-6)
    if any(R.CLASSES[int(np.argmax(p))] != r['state'] for p, r in zip(reconstructed, inferred)):
        raise ValueError('reconstructed live classes differ')
    single_error = 0.
    for name, bundle, models in [('phase_original', parent, original), ('phase_room', adapted, room)]:
        for s in (starts[0], starts[len(starts)//2], starts[-1]):
            ref = np.asarray(predict_window(phase[s:s+6000], bundle, models))
            got = np.asarray(predictions[name][s]['scores'])
            np.testing.assert_allclose(ref, got, rtol=1e-5, atol=1e-6)
            single_error = max(single_error, float(np.max(np.abs(ref-got))))
    models = {}
    for name, scores in predictions.items():
        rows = rebuild_rows(log, scores, tx0, begin, end)
        with (output/f'{name}-logged.jsonl').open('x') as fp:
            for row in rows:
                fp.write(json.dumps(row, allow_nan=False)+'\n')
        R.save_json(output/f'{name}-events.json', compact_rows(rows))
        models[name] = {'metrics': analyze(rows, truth, end), 'after60s': analyze(rows, truth, end, trim=60),
                        'time_shift_checks': [analyze(rows, truth, end, shift=s) for s in (-1, 1)]}
        for field, trim in [('metrics', 0), ('after60s', 60)]:
            verify_time_metrics(rows, truth, end, models[name][field], trim)
        if name == 'phase_room':
            actual_rows = [{'wall': begin, 'state': 'deciding'}]+[
                {'wall': r['wall'], 'state': r['state']} for r in log if begin <= r['wall'] <= end]
            assert [r['state'] for r in rows] == [r['state'] for r in actual_rows]
    for bundle, networks in [(parent, original), (adapted, room)]:
        for network, weights in zip(networks, bundle['state_dicts']):
            assert all(torch.equal(v, weights[k]) for k, v in network.state_dict().items())
    assert all(R.digest(Path(p)) == expected for p, expected in hashes.items())
    summary = {'session': str(session.relative_to(ROOT)), 'truth': truth, 'capture_start_wall': begin,
               'capture_end_wall': end, 'models': models, 'quality': quality,
               'protocol': {'training_performed': False, 'test_raw_used_in_adaptation': False,
                            'timing': 'recorded live decision wall times and exact logged TX windows',
                            'phone_mac_offset_s': 0, 'clock_offset_source': 'user info.txt: clocks equal',
                            'new_inferences': len(inferred), 'unique_inputs': len(starts),
                            'paired_compute_seconds': seconds, 'video_reviewed': False,
                            'model_hashes': {'phase_original': R.digest(PHASE_MODEL), 'phase_room': room_hash}},
               'checks': {'input_files_unchanged': len(hashes), 'model_weights_unchanged': True,
                          'direct_prefix_checks': prefix_checks, 'logged_class_agreement': True,
                          'live_probability_max_error': error, 'single_window_max_error': single_error,
                          'independent_segment_time_checks': 20}}
    R.save_json(output/'summary.json', summary)
    print(json.dumps({'models': models, 'checks': summary['checks']}, ensure_ascii=False), flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--info', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.info, args.output)
