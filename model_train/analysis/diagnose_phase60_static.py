#!/usr/bin/env python3
"""Frozen-model diagnosis: historical and live static vs empty, no training."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import csi_store as cs
from scripts.session_form import read_meta
from model_train.phase60 import features as F
from model_train.phase60.evaluate_raw import validate_bundle
from model_train.cnn1d.shared_cnn import SharedTemporalCNN


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def distribution(values):
    a = np.asarray(values, dtype=float)
    return dict(zip(('min', 'p10', 'median', 'p90', 'max'),
                    np.quantile(a, [0, .1, .5, .9, 1]).tolist()))


def predict(x, models):
    with torch.inference_mode():
        scores = torch.stack([m(torch.from_numpy(np.ascontiguousarray(x))).softmax(1)
                              for m in models]).numpy()
    assert np.isfinite(scores).all()
    return scores.mean(0), scores


def window_stats(phase, start, models):
    w = phase[start:start+6000]
    x = F.windows(phase, np.array([start]), 6000, 20)
    assert x.shape == (1, 300, 156) and np.isfinite(x).all()
    raw_std = w.std(0)
    input_std = x[0].std(0)
    scores, seeds = predict(x, models)
    v = x[0].astype(np.float64)
    power = np.abs(np.fft.rfft(v, axis=0))**2
    power[1:-1] *= 2  # one-sided energy: DC and Nyquist occur only once
    np.testing.assert_allclose(power.sum(0)/300, (v*v).sum(0), rtol=1e-12, atol=1e-12)
    frequencies = np.fft.rfftfreq(300, d=.2)
    total = power[1:].sum()
    lag1 = (v[1:]*v[:-1]).sum()/np.sqrt((v[1:]**2).sum()*(v[:-1]**2).sum())
    return {
        'start_frame': int(start), 'end_frame': int(start+6000),
        'residual_std_mean': float(raw_std.mean()),
        'input_std_mean': float(input_std.mean()),
        'input_std_per_rx': input_std.reshape(3, 52).mean(1).tolist(),
        'residual_std_per_rx': raw_std.reshape(3, 52).mean(1).tolist(),
        'downsampling_std_ratio': float(input_std.mean()/raw_std.mean()),
        'temporal_jump_fraction_gt_pi': float((np.abs(np.diff(w, axis=0)) > np.pi).mean()),
        'lag1_correlation': float(lag1),
        'power_fraction_under_0_5hz': float(power[(frequencies>0)&(frequencies<=.5)].sum()/total),
        'scores': scores[0].tolist(), 'seed_scores': seeds[:, 0].tolist(),
        'prediction': ('empty', 'static', 'motion')[int(scores[0].argmax())],
    }, x


def raw_stats(folder, begin=None, end=None):
    result = []
    for rx, f in sorted(cs.read_session(folder).items()):
        if begin is not None:
            seq = f['hdr']['tx_seq']
            f = f[(seq >= begin) & (seq < end)]
        z = cs.complex_csi(f, valid_only=False)[:, F.RAW_TONE_ORDER]
        raw = f['raw'].reshape(-1, 64, 2)[:, F.RAW_TONE_ORDER]
        h = f['hdr']
        result.append({'rx': int(rx), 'frames': len(f),
            'rssi_median': float(np.median(h['rssi'])),
            'agc_median': float(np.median(h['agc_gain'])),
            'fft_median': float(np.median(h['fft_gain'])),
            'amplitude_median': float(np.median(np.abs(z))),
            'zero_complex_fraction': float((np.abs(z) == 0).mean()),
            'saturated_iq_fraction': float(((raw == -128) | (raw == 127)).mean()),
            'rf': {k: np.unique(h[k]).tolist() for k in ('channel', 'rate', 'sig_len')},
        })
    return result


def run(output):
    output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    model_path = ROOT/'model_train/phase60/model_60sessions.pt'
    bundle = torch.load(model_path, map_location='cpu', weights_only=True)
    validate_bundle(bundle)
    models = []
    for state in bundle['state_dicts']:
        model = SharedTemporalCNN(groups=(156,), n_classes=3)
        model.load_state_dict(state)
        models.append(model.eval())
    hashes = {str(model_path.relative_to(ROOT)): sha(model_path)}
    for p in (Path(__file__), ROOT/'model_train/phase60/features.py',
              ROOT/'model_train/cnn1d/shared_cnn.py', ROOT/'scripts/csi_store.py',
              ROOT/'scripts/session_form.py', ROOT/'model_train/phase60/evaluate_raw.py',
              ROOT/'model_train/preprocessing/preprocess_3rx.py'):
        hashes[str(p.relative_to(ROOT))] = sha(p)
    records, interventions = [], []
    folders = [p.parent for date in ('20260916', '20260917', '20260919', '20260920')
               for p in sorted((ROOT/'mac_collector_output/raw'/date).glob('*/session.json'))]
    for index, folder in enumerate(folders):
        meta = json.loads((folder/'session.json').read_text())
        label = meta['label']
        key = f"{folder.parent.name}_{meta['session_id']}"
        split = 'train' if key in bundle['training_sessions'] else 'eval'
        meta_path = folder/'session_meta_snapshot.yaml'
        session_meta = read_meta(meta_path) if meta_path.exists() else None
        extra = [meta_path] if meta_path.exists() else []
        extra += list(folder.glob('*registry_snapshot.csv'))
        for p in [folder/'session.json', *sorted(folder.glob('device_*.csi')), *extra]:
            hashes[str(p.relative_to(ROOT))] = sha(p)
        phase = F.session_phase(folder)
        starts = F.valid_starts(phase, 6000, 6000)
        windows = [window_stats(phase, int(s), models)[0] for s in starts]
        assert windows
        records.append({'key': key, 'label': label, 'split': split,
            'path': str(folder.relative_to(ROOT)), 'provenance': meta.get('provenance'),
            'session_metadata': session_meta,
            'devices': [{k: d.get(k) for k in ('device_id', 'sta_mac', 'rx_id', 'sink_mac')}
                        for d in meta['devices']],
            'raw': raw_stats(folder), 'windows': windows})
        print(f'{index+1}/{len(folders)} {key} {label}', flush=True)
    replay = ROOT/'model_train/analysis/output/20261008-transition-model-comparison'
    protocol = json.loads((replay/'protocol.json').read_text())
    truth = json.loads((replay/'truth.json').read_text())['segments']
    folder = ROOT/protocol['session']
    phase = F.session_phase(folder)
    for p in [folder/'session.json', *sorted(folder.glob('device_*.csi')),
              *folder.glob('*registry_snapshot.csv'), replay/'protocol.json', replay/'truth.json']:
        hashes[str(p.relative_to(ROOT))] = sha(p)
    for j, segment in enumerate(truth):
        # Use only windows entirely contained in the reported state.
        begin = int(np.ceil((segment['start_wall']-protocol['anchor_wall'])*100))
        end = int(np.floor((segment['end_wall']-protocol['anchor_wall'])*100))
        windows = []
        for start in range(begin, end-6000+1, 6000):
            if not np.isfinite(phase[start:start+6000]).all():
                continue
            row, x = window_stats(phase, start, models)
            windows.append(row)
            factors = [.5, 1, 1.5, 2, 3, 4, 6]
            scores, seeds = predict(np.concatenate([x*f for f in factors]), models)
            interventions.append({'segment': j, 'label': segment['label'], 'start': start,
                'factor': factors, 'scores': scores.tolist(), 'seed_scores': seeds.tolist(),
                'predictions': [bundle['class_names'][int(i)] for i in scores.argmax(1)]})
        records.append({'key': f'live_{j}', 'label': segment['label'], 'split': 'live',
            'name': segment['name'], 'windows': windows,
            'raw': raw_stats(folder, protocol['common_start_tx_seq']+begin,
                             protocol['common_start_tx_seq']+end)})
    groups = {}
    for split in ('train', 'eval', 'live'):
        for label in bundle['class_names']:
            rows = [r for r in records if r['split']==split and r['label']==label and r['windows']]
            windows = [w for r in rows for w in r['windows']]
            groups[f'{split}/{label}'] = {'sessions_or_segments': len(rows), 'windows': len(windows),
                'input_std_session_mean': distribution([np.mean([w['input_std_mean'] for w in r['windows']]) for r in rows]),
                'input_std_windows': distribution([w['input_std_mean'] for w in windows]),
                'raw_residual_std_windows': distribution([w['residual_std_mean'] for w in windows]),
                'prediction_counts': {c: sum(w['prediction']==c for w in windows) for c in bundle['class_names']}}
    for name, expected in hashes.items():
        assert sha(ROOT/name)==expected, name
    for m, state in zip(models, bundle['state_dicts']):
        for k, v in m.state_dict().items():
            assert torch.equal(v, state[k]), k
    result = {'method': 'fixed non-overlapping 60s windows; summary of sessions, no retraining; scaled inputs diagnostic only',
              'groups': groups, 'records': records, 'interventions': interventions,
              'hashes': hashes, 'hashes_unchanged': True, 'model_state_unchanged': True}
    (output/'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(groups, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    run(parser.parse_args().output)
