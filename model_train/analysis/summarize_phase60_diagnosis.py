#!/usr/bin/env python3
"""Publish compact diagnosis numbers and a standard scientific scatter plot."""
import argparse
import hashlib
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np


def run(source, probes, output):
    data = json.loads(source.read_text())
    intervention = json.loads(probes.read_text())
    assert intervention['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    output.mkdir(parents=True, exist_ok=True)
    patterns = {}
    for split in ('train', 'eval', 'live'):
        for label in ('empty', 'static', 'motion'):
            windows = [w for r in data['records'] if r['split']==split and r['label']==label for w in r['windows']]
            patterns[f'{split}/{label}'] = {
                k: dict(zip(('min', 'median', 'max'), np.quantile([w[k] for w in windows], [0,.5,1]).tolist()))
                for k in ('lag1_correlation', 'power_fraction_under_0_5hz')}
    summary = {'groups': data['groups'], 'patterns': patterns, 'temporal_probes': intervention['rows'],
               'preserved_files': len(data['hashes']), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
               'probes_sha256': hashlib.sha256(probes.read_bytes()).hexdigest(),
               'note': 'Diagnostic windows are not independent subjects or a new accuracy benchmark.'}
    (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')
    fig, ax = plt.subplots(figsize=(9,5.3), constrained_layout=True)
    colors = {'empty':'#25845a', 'static':'#2463bc'}
    for label in ('empty','static'):
        windows = [w for r in data['records'] if r['split']=='train' and r['label']==label for w in r['windows']]
        ax.scatter([w['input_std_mean'] for w in windows], [w['lag1_correlation'] for w in windows],
                   color=colors[label], alpha=.4, s=28, label=f'Training {label} ({len(windows)} windows)')
    for label, marker, color in (('empty','D','#137644'), ('static','*','#da4135')):
        windows = [w for r in data['records'] if r['split']=='live' and r['label']==label for w in r['windows']]
        ax.scatter([w['input_std_mean'] for w in windows], [w['lag1_correlation'] for w in windows],
                   color=color, marker=marker, s=130 if label=='static' else 60,
                   edgecolor='black', linewidth=.7, label=f'Oct 6 {label} ({len(windows)} windows)', zorder=5)
    ax.set(xscale='log', xlabel='Input variation (mean temporal standard deviation, radians)',
           ylabel='Temporal continuity (correlation at 0.2-second lag)',
           title='Seated-person input resembles empty-room temporal patterns')
    ax.grid(alpha=.18)
    ax.legend(fontsize=9, loc='lower right')
    fig.savefig(output/'patterns.png', dpi=180)
    plt.close(fig)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--probes', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    run(args.source, args.probes, args.output)
