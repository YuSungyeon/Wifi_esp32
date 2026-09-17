#!/usr/bin/env python3
"""저장한 CNN 실험과 LSTM baseline snapshot으로 비교 수치·그림을 재생성한다."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

LABELS = ('empty', 'static', 'motion')
COLORS = {'LSTM': '#5470a7', '1D-CNN': '#079b86'}


def read(path):
    return json.loads(path.read_text())


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def stat(values):
    x = np.asarray(values, dtype=float)
    return {'mean': float(x.mean()), 'std': float(x.std(ddof=0)), 'values': x.tolist()}


def fmt(s, factor=1):
    return f"{s['mean']*factor:.4f} ± {s['std']*factor:.4f}"


def aggregate(runs, params):
    metrics = {
        'validation_macro_f1': stat([r['validation']['window_level']['macro_f1'] for r in runs]),
        'validation_accuracy': stat([r['validation']['window_level']['accuracy'] for r in runs]),
        'validation_session_accuracy': stat([r['validation']['session_level']['metrics']['accuracy'] for r in runs]),
        'test_accuracy': stat([r['test']['window_level']['accuracy'] for r in runs]),
        'test_macro_f1': stat([r['test']['window_level']['macro_f1'] for r in runs]),
        'test_session_accuracy': stat([r['test']['session_level']['metrics']['accuracy'] for r in runs]),
        'test_session_macro_f1': stat([r['test']['session_level']['metrics']['macro_f1'] for r in runs]),
        'validation_test_gap': stat([r['validation']['window_level']['macro_f1'] - r['test']['window_level']['macro_f1'] for r in runs]),
        'seconds_per_epoch': stat([np.mean([h['elapsed_seconds'] for h in r['history']]) for r in runs]),
        'run_training_seconds': stat([sum(h['elapsed_seconds'] for h in r['history']) for r in runs]),
        'parameter_count': params,
        'per_class': {label: {m: stat([r['test']['window_level']['per_class'][label][m] for r in runs])
                             for m in ('precision', 'recall', 'f1')} for label in LABELS},
        'confusion_matrix_mean': np.mean([r['test']['window_level']['confusion_matrix'] for r in runs], axis=0).tolist(),
        'sessions': [],
    }
    for session_id in (9, 10, 19, 20, 29, 30):
        rows = [next(s for s in r['test']['session_level']['sessions'] if s['session_id'] == session_id) for r in runs]
        metrics['sessions'].append({
            'session_id': session_id, 'truth': rows[0]['true_label'],
            'window_count': rows[0]['window_count'],
            'window_accuracy': stat([r['window_accuracy'] for r in rows]),
            'predictions': [r['predicted_label'] for r in rows],
            'mean_probabilities': {c: stat([r['mean_probabilities'][c] for r in rows]) for c in LABELS},
        })
    return metrics


def figures(models, runs, output):
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False,
                         'figure.dpi': 140, 'savefig.dpi': 180})
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.3))
    keys = ('validation_macro_f1', 'test_macro_f1', 'test_session_accuracy')
    titles = ('Validation\nmacro-F1', 'Exploratory test\nmacro-F1', 'Test session\naccuracy')
    x = np.arange(3)
    for i, (name, data) in enumerate(models.items()):
        means = [data[k]['mean'] for k in keys]
        err = [data[k]['std'] for k in keys]
        bars = axes[0].bar(x + (i-.5)*.35, means, .35, yerr=err, capsize=3, label=name, color=COLORS[name])
        axes[0].bar_label(bars, fmt='%.3f', padding=4, fontsize=9)
        bars = axes[1].bar(x + (i-.5)*.35, [data['per_class'][c]['recall']['mean'] for c in LABELS], .35,
                          yerr=[data['per_class'][c]['recall']['std'] for c in LABELS], capsize=3,
                          label=name, color=COLORS[name])
        axes[1].bar_label(bars, fmt='%.3f', padding=4, fontsize=9)
    axes[0].set_xticks(x, titles)
    axes[1].set_xticks(x, LABELS)
    axes[0].set_title('Overall scores (mean ± population SD, 3 seeds)')
    axes[1].set_title('Exploratory test: recall by class')
    for axis in axes:
        axis.set_ylim(0, 1.16)
        axis.legend(loc='upper right', bbox_to_anchor=(1, -.17), ncols=2)
    fig.tight_layout()
    fig.savefig(output/'scores.png', bbox_inches='tight')
    plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10, 4.3))
    for axis, (name, data) in zip(axes, models.items()):
        matrix = np.asarray(data['confusion_matrix_mean'])
        proportions = matrix / matrix.sum(axis=1, keepdims=True)
        axis.imshow(proportions, vmin=0, vmax=1, cmap='Blues')
        for row in range(3):
            for col in range(3):
                axis.text(col, row, f'{matrix[row,col]:.1f}\n({proportions[row,col]*100:.1f}%)',
                          ha='center', va='center', color='white' if proportions[row,col] > .55 else '#222222')
        axis.set(xticks=range(3), yticks=range(3), xticklabels=LABELS, yticklabels=LABELS,
                 xlabel='Predicted class', ylabel='True class', title=name+' — mean counts per seed')
    fig.suptitle('Exploratory test confusion matrices (5,924 windows per seed)')
    fig.tight_layout()
    fig.savefig(output/'confusion-matrices.png', bbox_inches='tight')
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(11, 2.8))
    values = np.array([[s['window_accuracy']['mean'] for s in d['sessions']] for d in models.values()])
    ax.imshow(values, vmin=0, vmax=1, cmap='YlGnBu', aspect='auto')
    for row in range(2):
        for col in range(6):
            ax.text(col, row, f'{values[row,col]*100:.1f}%', ha='center', va='center',
                    color='white' if values[row,col] > .6 else '#222222')
    ax.set(xticks=range(6), xticklabels=['S9\nempty','S10\nempty','S19\nstatic','S20\nstatic','S29\nmotion','S30\nmotion'],
           yticks=range(2), yticklabels=list(models), title='Mean window accuracy within each test session (3 seeds)')
    fig.tight_layout()
    fig.savefig(output/'session-accuracy.png', bbox_inches='tight')
    plt.close(fig)

    fig, axes = plt.subplots(2, 2, figsize=(12, 7), sharex='col')
    for col, (name, records) in enumerate(runs.items()):
        for r in records:
            seed = r['config']['training']['seed']
            h = r['history']
            epochs = [v['epoch'] for v in h]
            axes[0,col].plot(epochs, [v['validation']['macro_f1'] for v in h], '.-', label=f'seed {seed}')
            best = r['summary']['best_epoch']
            axes[0,col].scatter([best], [h[best-1]['validation']['macro_f1']], marker='*', s=120, color=f'C{seed}')
            axes[1,col].plot(epochs, [v['validation']['loss'] for v in h], '.-', label=f'seed {seed}')
        axes[0,col].set(title=f'{name}: selected class weight', ylabel='Validation macro-F1', ylim=(.75,1.02))
        axes[1,col].set(xlabel='Epoch', ylabel='Validation loss')
        axes[0,col].legend()
        axes[1,col].set_yscale('log')
    fig.suptitle('Learning curves — stars mark selected checkpoints')
    fig.tight_layout()
    fig.savefig(output/'learning-curves.png', bbox_inches='tight')
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cnn-experiment', type=Path, required=True)
    parser.add_argument('--lstm-snapshot', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    experiment = args.cnn_experiment.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    assert (experiment/'completed.json').is_file(), 'Experiment must complete before report generation'
    protocol = read(experiment/'protocol.json')
    selection = read(experiment/'selection.json')
    lstm_all = read(args.lstm_snapshot)
    cnn_all = []
    for weight in ('none', 'balanced'):
        for seed in (0, 1, 2):
            path = experiment/f'seed{seed}-{weight}'
            r = {'run_dir': str(path), 'model_type': 'cnn1d'}
            for key, filename in [('config','config.json'),('summary','run-summary.json'),('validation','validation-metrics.json'),('test','test-metrics.json')]:
                if (path/filename).is_file(): r[key] = read(path/filename)
            r['history'] = [json.loads(l) for l in (path/'history.jsonl').read_text().splitlines()]
            cnn_all.append(r)
    save(output/'cnn-runs-snapshot.json', cnn_all)
    assert len(lstm_all) == len(cnn_all) == 6
    for r in lstm_all + cnn_all:
        cfg = r['config']
        assert cfg['dataset_manifest_sha256'] == protocol['dataset_sha256']['manifest.json']
        assert cfg['normalization_sha256'] == protocol['dataset_sha256']['normalization.npz']
        for key, expected in {'batch_size':32, 'learning_rate':.001, 'max_epochs':50, 'patience':5,
                              'dropout':.2, 'min_delta':0, 'num_workers':0}.items():
            assert cfg['training'][key] == expected, (r['run_dir'], key)
        assert r['validation']['window_level']['sample_count'] == 5933
        if 'test' in r: assert r['test']['window_level']['sample_count'] == 5924
    selected = {'LSTM': [r for r in lstm_all if r['config']['training']['class_weight']=='balanced'],
                '1D-CNN': [r for r in cnn_all if r['config']['training']['class_weight']==selection['selected_class_weight']]}
    for records in selected.values():
        records.sort(key=lambda r:r['config']['training']['seed'])
        assert [r['config']['training']['seed'] for r in records] == [0,1,2]
    models = {'LSTM': aggregate(selected['LSTM'], 297347), '1D-CNN': aggregate(selected['1D-CNN'], 53763)}
    scores = {name: {weight: stat([r['validation']['window_level']['macro_f1'] for r in records
                                 if r['config']['training']['class_weight']==weight])
                     for weight in ('none','balanced')}
              for name,records in [('LSTM',lstm_all),('1D-CNN',cnn_all)]}
    summary = {'protocol':protocol, 'selection':selection, 'class_weight_validation':scores, 'models':models,
               'cnn_runs':[{'seed':r['config']['training']['seed'], 'class_weight':r['config']['training']['class_weight'],
                           **r['summary'], 'training_seconds':sum(h['elapsed_seconds'] for h in r['history'])} for r in cnn_all]}
    save(output/'summary.json', summary)
    figures(models, selected, output)
    lines = ['| 지표 | LSTM | 1D-CNN | CNN − LSTM |', '|---|---:|---:|---:|']
    for key in ('validation_macro_f1','test_accuracy','test_macro_f1','test_session_accuracy','test_session_macro_f1','validation_test_gap','seconds_per_epoch','run_training_seconds'):
        a,b = models['LSTM'][key],models['1D-CNN'][key]
        lines.append(f'| {key} | {fmt(a)} | {fmt(b)} | {b["mean"]-a["mean"]:+.4f} |')
    lines += ['', '| Class | 지표 | LSTM | 1D-CNN |', '|---|---|---:|---:|']
    for label in LABELS:
        for metric in ('precision','recall','f1'):
            lines.append(f'| {label} | {metric} | {fmt(models["LSTM"]["per_class"][label][metric])} | {fmt(models["1D-CNN"]["per_class"][label][metric])} |')
    (output/'tables.md').write_text('\n'.join(lines)+'\n')
    print('\n'.join(lines))
    print('SELECTION',selection)
    for name,data in models.items():
        print(name, 'sessions:', json.dumps(data['sessions'],ensure_ascii=False))


if __name__ == '__main__':
    main()
