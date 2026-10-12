#!/usr/bin/env python3
"""Keep per-signal variance fixed while probing temporal pattern sensitivity."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.phase60 import features as F
from model_train.cnn1d.shared_cnn import SharedTemporalCNN


def run(source, output):
    output.mkdir(parents=True, exist_ok=False)
    original = json.loads(source.read_text())
    for name, expected in original['hashes'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == expected, name
    torch.set_num_threads(2)
    b = torch.load(ROOT/'model_train/phase60/model_60sessions.pt', map_location='cpu', weights_only=True)
    models = []
    for state in b['state_dicts']:
        model = SharedTemporalCNN(groups=(156,), n_classes=3)
        model.load_state_dict(state)
        models.append(model.eval())
    live = ROOT/'mac_collector_output/realtime/20261005-235852-cba933f4/capture'
    cache, rows = {}, []
    names = ['original', 'shuffle_time', 'smooth_3', 'smooth_5', 'smooth_11']
    for record in original['records']:
        if not ((record['split']=='live' and record['label'] in ('empty','static'))
                or record['key'] in ('20260920_55', '20260920_56', '20260917_39')):
            continue
        folder = live if record['split']=='live' else ROOT/record['path']
        if folder not in cache:
            cache[folder] = F.session_phase(folder)
        for w in record['windows']:
            x = F.windows(cache[folder], np.array([w['start_frame']]), 6000, 20)[0]
            rng = np.random.default_rng(42)
            variants = [x, x[rng.permutation(300)]]
            for kernel in (3, 5, 11):
                v = np.mean([np.roll(x, j, axis=0) for j in range(-(kernel//2), kernel//2+1)], axis=0)
                v -= v.mean(0)
                v *= x.std(0)/np.maximum(v.std(0), 1e-12)
                variants.append(v)
            batch = np.stack(variants)
            np.testing.assert_allclose(batch.std(1), np.broadcast_to(x.std(0), (5,156)), rtol=1e-5, atol=1e-7)
            with torch.inference_mode():
                scores = torch.stack([m(torch.from_numpy(batch)).softmax(1) for m in models]).mean(0).numpy()
            np.testing.assert_allclose(scores[0], w['scores'], rtol=1e-5, atol=1e-6)
            rows.append({'key': record['key'], 'label': record['label'], 'start': w['start_frame'],
                         'variants': names, 'scores': scores.tolist(),
                         'prediction': [b['class_names'][int(i)] for i in scores.argmax(1)],
                         'max_std_difference': float(np.abs(batch.std(1)-x.std(0)).max())})
    result = {'description': 'synthetic diagnosis; per-signal input standard deviation preserved; no retraining or accuracy claim',
              'rows': rows, 'source': str(source),
              'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
              'script_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    (output/'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    for row in rows:
        print(row['key'], row['start'], row['prediction'], flush=True)
    for name, expected in original['hashes'].items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == expected, name
    print('PASS: all original input/source hashes unchanged; per-signal variation preserved.')


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    run(args.source, args.output)
