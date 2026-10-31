#!/usr/bin/env python3
"""Fixed, resumable 75-session pilot. Does not replace deployed checkpoints."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time

import numpy as np
import torch
from torch import nn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from model_train.robust.predict import load_raw_amplitude
from model_train.robust.shared_temporal import SharedEncoder, amplitude_input
from model_train.phase60 import features as phase_features
from model_train.cnn1d.shared_cnn import SharedTemporalCNN
from scripts import csi_store

DATES = ('20260916', '20260917', '20260919', '20260920')
NAMES = ('empty', 'static', 'motion')
FLAGGED = '20260919_12'
CANDIDATES = [f'{kind}{sec}' for kind in ('amp', 'phase') for sec in (15, 30, 60)]
CONFIG = dict(version=1, epochs=15, batch=32, lr=.001, weight_decay=.01,
              label_smoothing=.1, seeds=[0, 1, 2], train_stride=1500,
              eval_stride=300, common_window=6000, candidates=CANDIDATES,
              excluded_training=[FLAGGED], phase_downsample=20)
SOURCE_PATHS = ['model_train/pilot75/run.py', 'model_train/robust/predict.py',
                'model_train/robust/shared_temporal.py', 'model_train/phase60/features.py',
                'model_train/cnn1d/shared_cnn.py', 'scripts/csi_store.py',
                'model_train/analysis/prepare_phase_experiment.py',
                'model_train/preprocessing/preprocess_3rx.py',
                'model_train/preprocessing/phase_features.py']


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    tmp.replace(path)


def splits():
    out = {f'without-{d}': ([x for x in DATES if x != d], [d]) for d in DATES}
    out['early-to-late'] = (list(DATES[:2]), list(DATES[2:]))
    out['late-to-early'] = (list(DATES[2:]), list(DATES[:2]))
    return out


def inventory():
    rows = []
    for date in DATES:
        for folder in sorted((ROOT / 'mac_collector_output/raw' / date).iterdir()):
            if not (folder / 'session.json').is_file():
                continue
            meta = json.loads((folder / 'session.json').read_text())
            label = meta['label']
            if label not in NAMES:
                raise ValueError(f'Unexpected label: {folder}: {label}')
            key = f"{date}_{folder.name.rsplit('_s', 1)[1]}"
            files = [folder / 'session.json'] + [folder / f'device_{rx}.csi' for rx in (101, 102, 103)]
            rows.append(dict(key=key, date=date, label=NAMES.index(label),
                             path=str(folder.relative_to(ROOT)),
                             files={str(p.relative_to(ROOT)): digest(p) for p in files}))
    assert len(rows) == 75 and len({r['key'] for r in rows}) == 75
    assert np.bincount([r['label'] for r in rows]).tolist() == [25, 25, 25]
    return rows


def common_ends(amp, phase):
    assert len(amp) == len(phase)
    good = np.isfinite(amp).all(1) & np.isfinite(phase).all(1)
    bad = np.r_[0, np.cumsum(~good)]
    ends = np.arange(6000, len(amp) + 1, CONFIG['eval_stride'])
    return ends[bad[ends] == bad[ends - 6000]]


def prepare(out):
    rows = inventory()
    identity = dict(config=CONFIG, sources={p: digest(ROOT / p) for p in SOURCE_PATHS},
                    sessions=rows, torch_version=str(torch.__version__))
    identity['fingerprint'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    path = out / 'identity.json'
    if path.exists() and json.loads(path.read_text()) != identity:
        raise RuntimeError('Source/config/data changed: use a new output directory.')
    write_json(path, identity)
    cache = out / 'cache'
    cache.mkdir(exist_ok=True)
    if (out / 'data.json').exists():
        return json.loads((out / 'data.json').read_text())
    for row in rows:
        dest = cache / row['key']
        dest.mkdir(exist_ok=True)
        meta_path = dest / 'meta.json'
        if meta_path.exists():
            row.update(json.loads(meta_path.read_text()))
            continue
        print(f"[prepare] {row['key']}", flush=True)
        folder = ROOT / row['path']
        amp, info = load_raw_amplitude(folder)
        phase = phase_features.session_phase(folder)
        frames = csi_store.read_session(folder)
        start = max(int(frames[rx]['hdr']['tx_seq'][0]) for rx in (101, 102, 103))
        assert info['common_start_tx_seq'] == start and len(amp) == len(phase)
        del frames
        ends = common_ends(amp, phase)
        if not len(ends) or not np.any((ends - 6000) % CONFIG['train_stride'] == 0):
            raise ValueError(f"No common train/eval windows: {row['key']}")
        np.save(dest / 'amp.npy', amp)
        np.save(dest / 'phase.npy', phase)
        details = dict(frames=len(amp), start_tx_seq=start, ends=ends.tolist(),
                       possible_ends=len(np.arange(6000, len(amp) + 1, 300)),
                       training_windows=int(np.sum((ends - 6000) % 1500 == 0)))
        write_json(meta_path, details)
        row.update(details)
    write_json(out / 'data.json', rows)
    return rows


def candidate_input(kind, raw, end, window):
    if kind == 'amp':
        # The other two channels are unused by mode A. Keep identical values without storing zeros.
        return amplitude_input(raw[end - window:end])[..., :1]
    return phase_features.windows(raw, np.array([end - window]), window, 20)[0]


def features(out, rows, candidate):
    kind = 'amp' if candidate.startswith('amp') else 'phase'
    sec = int(candidate[len(kind):])
    window = sec * 100
    folder = out / 'inputs'
    folder.mkdir(exist_ok=True)
    path = folder / f'{candidate}.npy'
    marker = folder / f'{candidate}.json'
    index = [(si, end) for si, row in enumerate(rows) for end in row['ends']]
    if not marker.exists():
        shape = ((len(index), window // 10, 51, 1) if kind == 'amp'
                 else (len(index), window // 20, 156))
        tmp = path.with_suffix('.tmp.npy')
        x = np.lib.format.open_memmap(tmp, mode='w+', dtype=np.float32, shape=shape)
        pos = 0
        for row in rows:
            raw = np.load(out / 'cache' / row['key'] / f'{kind}.npy', mmap_mode='r')
            for end in row['ends']:
                x[pos] = candidate_input(kind, raw, end, window)
                pos += 1
        x.flush()
        del x
        tmp.replace(path)
        write_json(marker, dict(shape=shape, sha256=digest(path)))
    meta = json.loads(marker.read_text())
    if digest(path) != meta['sha256']:
        raise RuntimeError(f'Corrupted feature cache: {candidate}')
    x = np.load(path, mmap_mode='r')
    assert len(x) == len(index)
    return x, np.asarray(index, dtype=np.int64)


def indices(rows, index, split):
    train_dates, test_dates = splits()[split]
    assert not set(train_dates) & set(test_dates)
    tr = np.array([i for i, (si, end) in enumerate(index)
                   if rows[si]['date'] in train_dates and rows[si]['key'] != FLAGGED
                   and (end - 6000) % CONFIG['train_stride'] == 0])
    va = np.array([i for i, (si, _) in enumerate(index) if rows[si]['date'] in test_dates])
    assert not set(index[tr, 0]) & set(index[va, 0])
    assert len(set(rows[si]['label'] for si in index[tr, 0])) == 3
    return tr, va


def cm_metrics(cm):
    cm = np.asarray(cm, dtype=float)
    tp = cm.diagonal()
    recall = np.divide(tp, cm.sum(1), out=np.zeros(3), where=cm.sum(1) != 0)
    f1 = np.divide(2 * tp, cm.sum(1) + cm.sum(0), out=np.zeros(3),
                   where=cm.sum(1) + cm.sum(0) != 0)
    return dict(confusion_matrix=cm.tolist(), accuracy=float(tp.sum() / cm.sum()),
                macro_f1=float(f1.mean()), recall=dict(zip(NAMES, recall.tolist())),
                static_to_empty=float(cm[1, 0] / cm[1].sum()) if cm[1].sum() else None,
                empty_to_static=float(cm[0, 1] / cm[0].sum()) if cm[0].sum() else None)


def measure(rows, index, probs, exclude_flagged=False):
    if not np.isfinite(probs).all():
        raise ValueError('Non-finite probabilities')
    np.testing.assert_allclose(probs.sum(1), 1, atol=1e-5)
    raw_cm, weighted_cm, session_cm = np.zeros((3, 3)), np.zeros((3, 3)), np.zeros((3, 3))
    per_session = []
    for si in np.unique(index[:, 0]):
        row = rows[si]
        if exclude_flagged and row['key'] == FLAGGED:
            continue
        p = probs[index[:, 0] == si]
        y = row['label']
        cm = np.zeros((3, 3))
        cm[y] = np.bincount(p.argmax(1), minlength=3)
        raw_cm += cm
        weighted_cm += cm / len(p)
        pred = int(p.mean(0).argmax())
        session_cm[y, pred] += 1
        per_session.append(dict(key=row['key'], date=row['date'], true=NAMES[y],
                                pred=NAMES[pred], windows=len(p),
                                window_accuracy=float(cm[y, y] / len(p)),
                                scores=p.mean(0).tolist(), confusion_matrix=cm.astype(int).tolist()))
    return dict(window=cm_metrics(raw_cm), session_balanced_window=cm_metrics(weighted_cm),
                session=cm_metrics(session_cm), per_session=per_session)


def evaluate(model, x, va, device):
    scores = []
    model.eval()
    with torch.inference_mode():
        for pos in range(0, len(va), CONFIG['batch']):
            xb = torch.from_numpy(np.array(x[va[pos:pos + CONFIG['batch']]])).to(device)
            scores.append(model(xb).softmax(1).cpu().numpy())
    return np.concatenate(scores)


def train_seed(out, rows, x, index, candidate, split, seed, device):
    folder = out / candidate / split
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f'seed{seed}'
    tr, va = indices(rows, index, split)
    if stem.with_suffix('.json').exists():
        saved = np.load(stem.with_suffix('.npz'))
        np.testing.assert_array_equal(saved['indices'], va)
        return saved['scores']
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    model = (SharedEncoder(mode='A') if candidate.startswith('amp') else SharedTemporalCNN()).to(device)
    y = np.array([rows[si]['label'] for si in index[:, 0]])
    t0 = time.monotonic()
    history = []
    if stem.with_suffix('.pt').exists():
        saved = torch.load(stem.with_suffix('.pt'), map_location='cpu', weights_only=True)
        assert saved['config'] == CONFIG and saved['candidate'] == candidate and saved['split'] == split
        model.load_state_dict(saved['state_dict'])
        history = saved['history']
    else:
        opt = torch.optim.AdamW(model.parameters(), lr=CONFIG['lr'], weight_decay=CONFIG['weight_decay'])
        loss_fn = nn.CrossEntropyLoss(label_smoothing=CONFIG['label_smoothing'])
        rng = np.random.default_rng(seed)
        for epoch in range(CONFIG['epochs']):
            model.train()
            order = rng.permutation(tr)
            total, correct = 0., 0
            for pos in range(0, len(order), CONFIG['batch']):
                take = order[pos:pos + CONFIG['batch']]
                xb = torch.from_numpy(np.array(x[take])).to(device)
                yb = torch.from_numpy(y[take]).to(device)
                opt.zero_grad(set_to_none=True)
                logits = model(xb)
                loss = loss_fn(logits, yb)
                if not torch.isfinite(loss):
                    raise ValueError('Non-finite training loss')
                loss.backward()
                opt.step()
                total += float(loss.detach().cpu()) * len(take)
                correct += int((logits.argmax(1) == yb).sum().cpu())
            history.append(dict(epoch=epoch + 1, loss=total / len(tr), accuracy=correct / len(tr)))
            print(f'[{candidate}/{split}/seed{seed}] epoch {epoch+1}/15 '
                  f'loss={total/len(tr):.4f} seconds={time.monotonic()-t0:.1f}', flush=True)
        torch.save(dict(state_dict={k: v.detach().cpu() for k, v in model.state_dict().items()},
                        config=CONFIG, candidate=candidate, split=split, seed=seed, history=history,
                        training_sessions=sorted({rows[si]['key'] for si in index[tr, 0]}),
                        evaluation_sessions=sorted({rows[si]['key'] for si in index[va, 0]}),
                        status='EXPERIMENTAL PILOT; not a deployment bundle'), stem.with_suffix('.pt'))
    scores = evaluate(model, x, va, device)
    np.savez_compressed(stem.with_suffix('.npz'), scores=scores, indices=va)
    write_json(stem.with_suffix('.json'), dict(history=history, elapsed=time.monotonic()-t0,
               all_sessions=measure(rows, index[va], scores),
               primary=measure(rows, index[va], scores, True)))
    return scores


def report(out):
    results = []
    for candidate in CANDIDATES:
        folds = {}
        for split in splits():
            path = out / candidate / split / 'ensemble.json'
            if path.exists():
                folds[split] = json.loads(path.read_text())
        dates = [v['primary']['session_balanced_window'] for k, v in folds.items() if k.startswith('without')]
        if len(dates) == 4:
            results.append(dict(candidate=candidate,
                date_mean_f1=float(np.mean([v['macro_f1'] for v in dates])),
                worst_date_f1=min(v['macro_f1'] for v in dates),
                static_to_empty=float(np.mean([v['static_to_empty'] for v in dates])),
                empty_to_static=float(np.mean([v['empty_to_static'] for v in dates])), folds=folds))
    results.sort(key=lambda r: r['date_mean_f1'], reverse=True)
    done = len(list(out.glob('*/*/seed*.json')))
    write_json(out / 'summary.json', dict(completed_seed_runs=done, expected_seed_runs=108, ranking=results))
    lines = ['# 75세션 진폭·위상 파일럿 결과', '',
             f'> 상태: **EXPERIMENTAL** — 학습 {done}/108 완료. 기존 배포 모델은 변경하지 않았다.', '',
             '[비교 계획](pilot-75-session-plan.md)의 고정 조건으로 진행했다. 이미 사용했던 자료의 재비교이며 독립 시험이 아니다.', '',
             '순위는 s12를 뺀 날짜별·세션 균등 비중의 창 macro F1 평균이다. 4개 날짜 평가가 끝난 후보만 표시한다.', '',
             '| 후보 | 날짜 평균 F1 | 최악 날짜 F1 | static→empty | empty→static |',
             '|---|---:|---:|---:|---:|']
    for r in results:
        lines.append(f"| {r['candidate']} | {r['date_mean_f1']:.4f} | {r['worst_date_f1']:.4f} | "
                     f"{r['static_to_empty']:.1%} | {r['empty_to_static']:.1%} |")
    lines += ['', '원본별 예측·seed별 점수·s12 포함/제외 지표와 체크포인트는 '
              '`model_train/pilot75/runs/20261030/`에 있다. 실행 중 표는 잠정 결과다.', '']
    (ROOT / 'model_train/docs/model-training/pilot-75-session-report.md').write_text('\n'.join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', choices=['prepare', 'run', 'report'])
    ap.add_argument('--output', type=Path, default=ROOT / 'model_train/pilot75/runs/20261030')
    ap.add_argument('--candidate', choices=CANDIDATES, nargs='+', default=CANDIDATES)
    ap.add_argument('--split', choices=list(splits()), nargs='+', default=list(splits()))
    ap.add_argument('--threads', type=int, default=2)
    ap.add_argument('--device', choices=['cpu', 'mps'], default='cpu')
    args = ap.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    if args.command == 'report':
        report(args.output)
        return
    # Prevent concurrent writers, including accidental double launch. OS releases lock on crash.
    import fcntl
    with (args.output / 'run.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        torch.set_num_threads(args.threads)
        torch.set_num_interop_threads(1)
        from threadpoolctl import threadpool_limits
        with threadpool_limits(limits=args.threads):
            rows = prepare(args.output)
            write_json(args.output / 'runtime.json', dict(device=args.device, threads=args.threads,
                       pid=os.getpid(), git_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()))
            if args.command == 'prepare':
                print(f'[prepared] {len(rows)} sessions, {sum(len(r["ends"]) for r in rows)} common windows', flush=True)
                return
            for candidate in args.candidate:
                print(f'[candidate] {candidate}', flush=True)
                x, index = features(args.output, rows, candidate)
                for split in args.split:
                    scores = [train_seed(args.output, rows, x, index, candidate, split, seed,
                                         torch.device(args.device)) for seed in CONFIG['seeds']]
                    _, va = indices(rows, index, split)
                    mean = np.mean(scores, axis=0)
                    write_json(args.output / candidate / split / 'ensemble.json',
                               dict(all_sessions=measure(rows, index[va], mean),
                                    primary=measure(rows, index[va], mean, True)))
                    report(args.output)
                del x
            print('[complete] requested candidate/split runs finished', flush=True)


if __name__ == '__main__':
    main()
