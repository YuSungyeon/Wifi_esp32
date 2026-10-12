"""Fine-tune the existing three-class phase model with an annotated room capture.

No validation split is invented from overlapping windows. Epoch count is fixed;
reported before/after scores are explicitly training resubstitution scores.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.analysis.prepare_phase_experiment import checked_frames
from model_train.phase60 import features as F
from model_train.phase60.predict import load_bundle
from model_train.preprocessing import preprocess_3rx as pre

METHOD = "head_only_source_replay_v1"
CLASSES = ("empty", "static", "motion")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def checked_session(folder, hashes):
    folder = Path(folder)
    meta_path = folder / "session.json"
    hashes[str(meta_path)] = digest(meta_path)
    meta = json.loads(meta_path.read_text())
    if sorted(d['device_id'] for d in meta['devices']) != list(F.RX_ORDER):
        raise ValueError("expected exactly three receivers")
    if {p.name for p in folder.glob('device_*.csi')} != {f'device_{rx}.csi' for rx in F.RX_ORDER}:
        raise ValueError("unexpected receiver files")
    frames = {}
    for d in meta['devices']:
        path = folder / f"device_{d['device_id']}.csi"
        hashes[str(path)] = digest(path)
        f = checked_frames(path, d['rx_id'])
        h = f['hdr']
        if (not len(f) or np.any(np.diff(h['tx_seq'].astype(np.int64)) < 0)
                or not ((h['channel'] == 11) & (h['rate'] == 11) & (h['sig_len'] == 47)).all()):
            raise ValueError(f"invalid sequence or RF: {path}")
        frames[d['device_id']] = f
    return meta, frames


def residual_range(frames, start, end):
    """Canonical I/Q interpolation and residual formula, within one boot per RX."""
    if end <= start:
        raise ValueError("empty interval")
    real = np.full((3, end-start, F.TONES), np.nan, np.float32)
    imag = real.copy()
    present = np.zeros((3, end-start), bool)
    for ri, rx in enumerate(F.RX_ORDER):
        f = frames[rx]
        seq = f['hdr']['tx_seq'].astype(np.int64)
        keep = np.flatnonzero((seq >= start) & (seq < end))
        if not len(keep) or len(np.unique(f['hdr']['boot_id'][keep])) != 1:
            raise ValueError(f"RX{rx}: missing input or boot boundary")
        idx, first = np.unique(seq[keep]-start, return_index=True)
        z = F.cs.complex_csi(f[keep[first]], valid_only=False)[:, F.RAW_TONE_ORDER]
        real[ri, idx], imag[ri, idx] = z.real, z.imag
        present[ri, idx] = True
    pre.interpolate_short_gaps(real, present, pre.DEFAULT_CONFIG)
    pre.interpolate_short_gaps(imag, present, pre.DEFAULT_CONFIG)
    return F.phase_residual(real + 1j*imag).transpose(1, 0, 2).reshape(end-start, F.N_COLS).astype(np.float32)


def capture_windows(summary, hashes):
    capture = (ROOT / summary['capture']).resolve()
    for name, expected in summary['hashes'].items():
        path = Path(name) if Path(name).is_absolute() else ROOT/name
        if digest(path) != expected:
            raise ValueError(f"annotation source changed: {path}")
        hashes[str(path)] = expected
    _, frames = checked_session(capture/'capture', hashes)
    segments = summary['segments']
    if Counter(s['label'] for s in segments) != Counter(CLASSES):
        raise ValueError("expected one annotated segment per class")
    previous_end = None
    for s in segments:
        start, end = s['candidate_start_tx_seq_approx'], s['candidate_end_tx_seq_exclusive_approx']
        if end-start < 6000 or (previous_end is not None and start < previous_end):
            raise ValueError("overlapping or short annotation")
        previous_end = end
        starts = s['valid_start_tx_seq_approx']
        if (not starts or len(set(starts)) != len(starts)
                or any(q < start or q+6000 > end for q in starts)):
            raise ValueError("window outside its annotation")
    start = min(s['candidate_start_tx_seq_approx'] for s in segments)-5
    end = max(s['candidate_end_tx_seq_exclusive_approx'] for s in segments)+5
    phase = residual_range(frames, start, end)
    xs, ys, records = [], [], []
    for s in segments:
        starts = np.asarray(s['valid_start_tx_seq_approx'], np.int64)
        if not all(np.isfinite(phase[q-start:q-start+6000]).all() for q in starts):
            raise ValueError("non-finite annotated input")
        x = F.windows(phase, starts-start, 6000, 20)
        if not np.isfinite(x).all():
            raise ValueError("non-finite transformed input")
        xs.append(x); ys.extend([CLASSES.index(s['label'])]*len(x))
        records.append({'label': s['label'], 'windows': len(x),
                        'starts_tx_seq': starts.tolist(), 'label_status': s['label_status']})
    return np.concatenate(xs), np.asarray(ys, np.int64), records


def source_windows(parent, hashes):
    xs, ys, records = [], [], []
    expected = set(parent['training_sessions'])
    for date in parent['training_dates']:
        for path in sorted((ROOT/'mac_collector_output/raw'/date).glob('*/session.json')):
            meta = json.loads(path.read_text())
            key = f"{date}_{meta['session_id']}"
            if key not in expected:
                continue
            meta, frames = checked_session(path.parent, hashes)
            label = meta['label']
            if label not in CLASSES or any(len(np.unique(f['hdr']['boot_id'])) != 1 for f in frames.values()):
                raise ValueError(f"invalid source label or boot: {key}")
            start = max(int(f['hdr']['tx_seq'][0]) for f in frames.values())
            end = min(int(f['hdr']['tx_seq'][-1])+1 for f in frames.values())
            if end-start < F.MIN_COMMON:
                raise ValueError(f"short source: {key}")
            phase = residual_range(frames, start, end)
            starts = F.valid_starts(phase, 6000, 6000)
            if not len(starts):
                raise ValueError(f"no source windows: {key}")
            x = F.windows(phase, starts, 6000, 20)
            if not np.isfinite(x).all():
                raise ValueError(f"non-finite source: {key}")
            xs.append(x); ys.extend([CLASSES.index(label)]*len(x))
            records.append({'session': key, 'label': label, 'windows': len(x)})
            print(f"source {len(records)}/{len(expected)} {key}: {len(x)} windows", flush=True)
    if {r['session'] for r in records} != expected or len(records) != len(expected):
        raise ValueError("source sessions missing or duplicated")
    return np.concatenate(xs), np.asarray(ys, np.int64), records


def head_features(model, x):
    pooled = []
    for enc, part in zip(model.encoders, torch.split(x, model.groups, dim=2)):
        b, t, f = part.shape
        h = enc(part.permute(0, 2, 1).reshape(b*f, 1, t))
        per_series = torch.cat([h.mean(2), h.std(2)], 1).view(b, f, -1)
        pooled += [per_series.mean(1), per_series.std(1)]
    return torch.cat(pooled, 1)


def frozen_features(model, x, batch=8):
    model.eval()
    for p in model.encoders.parameters():
        p.requires_grad_(False)
    parts = []
    with torch.inference_mode():
        for start in range(0, len(x), batch):
            xb = torch.from_numpy(np.ascontiguousarray(x[start:start+batch]))
            features = head_features(model, xb)
            if start == 0:
                torch.testing.assert_close(model.head(features), model(xb), rtol=0, atol=1e-6)
            parts.append(features)
    # Ordinary tensors are needed for backward through head weights.
    features = torch.cat(parts).clone()
    if not torch.isfinite(features).all():
        raise ValueError("non-finite frozen CNN features")
    return features


def sampling_weights(source_y, target_y):
    parts = []
    for y in (source_y, target_y):
        counts = np.bincount(y, minlength=3)
        if (counts == 0).any():
            raise ValueError("all three classes are required in both domains")
        parts.append(.5/(3*counts[y]))
    return torch.from_numpy(np.concatenate(parts))


def fit_head(model, source, source_y, target, target_y, seed, epochs=20, lr=1e-4, batch=32):
    if epochs <= 0 or batch <= 0 or not np.isfinite(lr) or lr <= 0:
        raise ValueError("positive epochs, learning rate and batch required")
    torch.manual_seed(seed)
    model.eval()
    for p in model.encoders.parameters():
        p.requires_grad_(False)
    frozen = {k: v.clone() for k, v in model.encoders.state_dict().items()}
    x = torch.cat([source, target]); y = torch.from_numpy(np.concatenate([source_y, target_y]))
    weights = sampling_weights(source_y, target_y)
    opt = torch.optim.Adam(model.head.parameters(), lr=lr)
    generator = torch.Generator().manual_seed(seed)
    history = []
    for epoch in range(epochs):
        model.head.train()
        order = torch.multinomial(weights, 2*max(len(source), len(target)), replacement=True, generator=generator)
        total = 0.
        for idx in order.split(batch):
            opt.zero_grad()
            loss = torch.nn.functional.cross_entropy(model.head(x[idx]), y[idx])
            if not torch.isfinite(loss):
                raise ValueError("non-finite training loss")
            loss.backward(); opt.step()
            total += float(loss.detach())*len(idx)
        history.append({'epoch': epoch+1, 'weighted_sample_loss': total/len(order)})
        print(f"seed {seed} epoch {epoch+1}/{epochs}: {history[-1]['weighted_sample_loss']:.5f}", flush=True)
    model.eval()
    for k, v in model.encoders.state_dict().items():
        if not torch.equal(v, frozen[k]):
            raise ValueError(f"frozen CNN or BatchNorm changed: {k}")
    return history


def metrics(probs, labels):
    pred = probs.argmax(1)
    cm = np.zeros((3, 3), int)
    np.add.at(cm, (labels, pred), 1)
    counts = cm.sum(1)
    recalls = np.divide(cm.diagonal(), counts, out=np.zeros(3), where=counts>0)
    return {'scope': 'training_resubstitution_not_independent_test', 'windows': len(labels),
            'accuracy': float((pred == labels).mean()), 'confusion_matrix': cm.tolist(),
            'recall': dict(zip(CLASSES, recalls.tolist())),
            'balanced_accuracy': float(recalls.mean()),
            'empty_static_mean_recall': float(recalls[:2].mean())}


def run(args):
    output = args.output.resolve()
    report_path = output.with_suffix('.metrics.json')
    if output.exists() or report_path.exists() or output == args.parent.resolve():
        raise ValueError("output already exists; use a new path")
    torch.set_num_threads(2)
    parent, models = load_bundle(args.parent)
    if parent.get('adaptation'):
        raise ValueError("first room adaptation must start from the original model")
    hashes = {str(args.parent.resolve()): digest(args.parent), str(args.annotations.resolve()): digest(args.annotations)}
    summary = json.loads(args.annotations.read_text())
    target_x, target_y, target_rows = capture_windows(summary, hashes)
    source_x, source_y, source_rows = source_windows(parent, hashes)
    before = {'source': [], 'target': []}; after = {'source': [], 'target': []}; histories = []
    for seed, model in zip(parent['config']['seeds'], models):
        source = frozen_features(model, source_x); target = frozen_features(model, target_x)
        with torch.inference_mode():
            before['source'].append(model.head(source).softmax(1).numpy())
            before['target'].append(model.head(target).softmax(1).numpy())
        histories.append({'seed': seed, 'history': fit_head(model, source, source_y, target, target_y,
                                                           seed, args.epochs, args.lr, args.batch)})
        with torch.inference_mode():
            after['source'].append(model.head(source).softmax(1).numpy())
            after['target'].append(model.head(target).softmax(1).numpy())
    scores = {period: {domain: metrics(np.mean(values[domain], axis=0), labels)
              for domain, labels in [('source', source_y), ('target', target_y)]}
              for period, values in [('before', before), ('after', after)]}
    for path, expected in hashes.items():
        if digest(path) != expected:
            raise ValueError(f"input changed during training: {path}")
    key = 'realtime:'+Path(summary['capture']).name
    adaptation = {'method': METHOD, 'parent_sha256': digest(args.parent),
                  'annotations_sha256': digest(args.annotations), 'capture': summary['capture'],
                  'clock_assumptions': summary['clock'], 'target_segments': target_rows,
                  'independent_validation': False, 'independent_test': False,
                  'source_windows': len(source_y), 'target_windows': len(target_y),
                  'epochs': args.epochs, 'lr': args.lr, 'batch': args.batch,
                  'cnn_and_batchnorm_frozen': True, 'fixed_epochs_no_model_selection': True}
    bundle = dict(parent)
    bundle.pop('metrics', None)
    bundle.update(state_dicts=[m.state_dict() for m in models], adaptation=adaptation,
                  training_sessions=list(parent['training_sessions'])+[key],
                  training_dates=sorted(set(parent['training_dates']+[summary['recorded_date'].replace('-','')])),
                  evaluation_dates=[], training_resubstitution_metrics=scores,
                  config={**parent['config'], 'epochs': args.epochs, 'lr': args.lr, 'batch': args.batch,
                          'train_stride': 300, 'source_train_stride': 6000},
                  trained_at=datetime.now().astimezone().isoformat(),
                  status='10/12 room capture fine-tuned; no independent validation/test; live trial candidate')
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as fp:
        torch.save(bundle, fp)
    _, restored = load_bundle(output)
    with torch.inference_mode():
        probe = torch.from_numpy(np.ascontiguousarray(target_x[[0, len(target_x)//2, -1]]))
        expected = torch.stack([m(probe).softmax(1) for m in models]).mean(0)
        got = torch.stack([m(probe).softmax(1) for m in restored]).mean(0)
        torch.testing.assert_close(got, expected, rtol=0, atol=1e-6)
    report = {'adaptation': adaptation, 'scores': scores, 'history': histories,
              'source_sessions': source_rows, 'input_hashes': hashes, 'inputs_unchanged': True,
              'output_sha256': digest(output), 'roundtrip_max_error': float((got-expected).abs().max())}
    with report_path.open('x', encoding='utf-8') as fp:
        json.dump(report, fp, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({'output': str(output), 'sha256': report['output_sha256'], 'scores': scores}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', type=Path, default=Path(__file__).parent/'model_60sessions.pt')
    parser.add_argument('--annotations', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--lr', type=float, default=1e-3)
    parser.add_argument('--batch', type=int, default=32)
    args = parser.parse_args()
    if args.epochs <= 0 or args.batch <= 0 or not np.isfinite(args.lr) or args.lr <= 0:
        parser.error('positive epochs, learning rate and batch required')
    run(args)


if __name__ == '__main__':
    main()
