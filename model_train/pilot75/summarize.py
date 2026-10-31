#!/usr/bin/env python3
"""Publish compact, auditable pilot tables after all 108 runs finish."""
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.pilot75.run import CANDIDATES, DATES, measure, splits, write_json


def main():
    out = ROOT / 'model_train/pilot75/runs/20261030'
    rows = json.loads((out / 'data.json').read_text())
    index = np.array([(si, e) for si, r in enumerate(rows) for e in r['ends']])
    records = []
    for candidate in CANDIDATES:
        fold_results, seed_f1s, all_indices, all_scores = {}, [[], [], []], [], []
        for split in splits():
            folder = out / candidate / split
            fold_results[split] = json.loads((folder / 'ensemble.json').read_text())
            seed_probs = []
            for seed in range(3):
                z = np.load(folder / f'seed{seed}.npz')
                seed_probs.append(z['scores'])
                if split.startswith('without-'):
                    metric = json.loads((folder / f'seed{seed}.json').read_text())
                    seed_f1s[seed].append(metric['primary']['session_balanced_window']['macro_f1'])
            if split.startswith('without-'):
                all_indices.append(z['indices'])
                all_scores.append(np.mean(seed_probs, axis=0))
        ids, probs = np.concatenate(all_indices), np.concatenate(all_scores)
        assert len(np.unique(ids)) == len(index) == len(ids)
        date_metrics = [fold_results[f'without-{d}']['primary']['session_balanced_window'] for d in DATES]
        seed_means = np.mean(seed_f1s, axis=1)
        records.append(dict(candidate=candidate,
            date_mean_f1=float(np.mean([m['macro_f1'] for m in date_metrics])),
            all75_date_mean_f1=float(np.mean([fold_results[f'without-{d}']['all_sessions']
                ['session_balanced_window']['macro_f1'] for d in DATES])),
            date_mean_accuracy=float(np.mean([m['accuracy'] for m in date_metrics])),
            worst_date_f1=min(m['macro_f1'] for m in date_metrics),
            seed_mean_f1=seed_means.tolist(), seed_std=float(seed_means.std()),
            date_mean_static_to_empty=float(np.mean([m['static_to_empty'] for m in date_metrics])),
            date_mean_empty_to_static=float(np.mean([m['empty_to_static'] for m in date_metrics])),
            oof_all75=measure(rows, index[ids], probs),
            oof_primary74=measure(rows, index[ids], probs, True), folds=fold_results))
    records.sort(key=lambda r: r['date_mean_f1'], reverse=True)
    artifacts = ROOT / 'model_train/docs/model-training/assets/pilot75'
    artifacts.mkdir(parents=True, exist_ok=True)
    write_json(artifacts / 'results.json', records)
    write_json(artifacts / 'identity.json', json.loads((out / 'identity.json').read_text()))
    write_json(artifacts / 'data-audit.json', rows)
    lines = ['# 75세션 진폭·위상 파일럿 결과', '',
        '> 상태: **SUPPORTING ANALYSIS** — 2026-10-30. 6후보 × 6분할 × 3seed = 108회 학습 완료.', '',
        '기존 모델과 실시간 선택값은 변경하지 않았다. [비교 계획](pilot-75-session-plan.md)에 고정한 조건으로 새로 학습했다.', '',
        '## 1. 무엇을 비교했나', '',
        '9/16·17·19·20의 75세션이다. 알려진 라벨 문제인 9/19 s12는 학습에서 제외하고, 평가 점수는 포함/제외 둘 다 남겼다.',
        '진폭·위상 각각 15·30·60초를 비교했다. 기존 모델 구조와 입력 처리는 서로 다르므로 입력 종류만의 효과로 해석하지 않는다.', '',
        '`amp`는 진폭, `phase`는 위상이고 뒤 숫자는 창 길이(초)다. 예: `amp60`은 60초 진폭 모델이다.', '',
        f"모든 후보가 같은 {sum(len(r['ends']) for r in rows):,}개 시점을 평가했다. 60초 공통 창에 결측이 남아 빠진 시점은 "
        f"{sum(r['possible_ends']-len(r['ends']) for r in rows)}개다. 학습 간격 15초, 평가 간격 3초이며 실시간의 0.3초와 다르다.", '',
        '15 epoch·AdamW·학습률 0.001·batch 32·label smoothing 0.1을 공통 적용했다. 각 분할마다 seed 0·1·2로 새로 학습하고 세 확률을 평균했다.', '',
        '## 2. 날짜를 하나씩 빼고 평가한 순위', '',
        '주 순위는 74세션 기준이다. 각 날짜 안에서 세션마다 같은 비중을 주고, 세 클래스의 F1을 평균한 뒤, 다시 네 날짜를 똑같은 비중으로 평균했다.',
        'F1은 정답 클래스를 놓치는 오류와 다른 클래스를 잘못 부르는 오류를 함께 반영하며 1에 가까울수록 좋다.', '',
        '| 후보 | 날짜 평균 F1 | 날짜 평균 창 정확도 | 최악 날짜 F1 | static→empty | empty→static | 세션 정답 | seed F1 편차 |',
        '|---|---:|---:|---:|---:|---:|---:|---:|']
    for r in records:
        sm = np.array(r['oof_primary74']['session']['confusion_matrix'])
        lines.append(f"| {r['candidate']} | {r['date_mean_f1']:.4f} | {r['date_mean_accuracy']:.1%} | "
                     f"{r['worst_date_f1']:.4f} | {r['date_mean_static_to_empty']:.1%} | "
                     f"{r['date_mean_empty_to_static']:.1%} | {int(sm.trace())}/74 | {r['seed_std']:.4f} |")
    lines += ['', '오류 비율도 날짜·세션 균등 비중의 창 기준이다. 세션 정답은 세션 내 확률 평균으로 판정한 횟수다.',
              'seed 편차는 각 seed의 네 날짜 평균 F1 세 값의 표준편차다. 신뢰구간이나 새 방 성능의 오차 범위는 아니다.', '',
              '## 3. 날짜별 결과', '', '| 후보 | 평가 날짜 | 세션 정답 | 창 F1 | static→empty | empty→static |',
              '|---|---|---:|---:|---:|---:|']
    for r in records:
        for d in DATES:
            m = r['folds'][f'without-{d}']['primary']
            w, sm = m['session_balanced_window'], np.array(m['session']['confusion_matrix'])
            lines.append(f"| {r['candidate']} | {d} | {int(sm.trace())}/{int(sm.sum())} | "
                         f"{w['macro_f1']:.4f} | {w['static_to_empty']:.1%} | {w['empty_to_static']:.1%} |")
    lines += ['', '## 4. 사람이 다른 날짜 묶음으로 옮겼을 때', '',
              '사람뿐 아니라 날짜·배치도 함께 달라진다. 이 표는 네 날짜 제외 순위와 분리해서 읽는다.', '',
              '| 후보 | 학습 → 평가 | 세션 정답 | 창 F1 | static→empty | empty→static |',
              '|---|---|---:|---:|---:|---:|']
    for r in records:
        for split, label in [('early-to-late', '9/16·17 → 9/19·20'), ('late-to-early', '9/19·20 → 9/16·17')]:
            m = r['folds'][split]['primary']
            w, sm = m['session_balanced_window'], np.array(m['session']['confusion_matrix'])
            lines.append(f"| {r['candidate']} | {label} | {int(sm.trace())}/{int(sm.sum())} | "
                         f"{w['macro_f1']:.4f} | {w['static_to_empty']:.1%} | {w['empty_to_static']:.1%} |")
    lines += ['', '## 5. s12를 포함해도 결론이 유지되는가', '',
              '2절과 같은 날짜 균등 비중으로 s12 포함/제외를 비교한다. 마지막 열의 세션 정답은 세션별 확률 평균의 판정이다.', '',
              '| 후보 | s12 제외 날짜 평균 F1 | s12 포함 날짜 평균 F1 | s12 포함 세션 정답 |',
              '|---|---:|---:|---:|']
    for r in records:
        sm = np.array(r['oof_all75']['session']['confusion_matrix'])
        lines.append(f"| {r['candidate']} | {r['date_mean_f1']:.4f} | "
                     f"{r['all75_date_mean_f1']:.4f} | {int(sm.trace())}/75 |")
    lines += ['', '### 날짜 대신 모든 세션에 같은 비중을 주면', '',
              '9/16·17은 15세션, 9/19·20은 s12 제외 59세션이다. 모든 세션을 똑같이 세면 뒤 두 날짜의 영향이 커진다.',
              '이 집계에서는 진폭 60초가 앞선다. 따라서 집계 기준과 무관한 절대적인 우승 모델이 나온 것은 아니다.', '',
              '| 후보 | 74세션 균등 비중 창 정확도 | 74세션 균등 비중 창 F1 |',
              '|---|---:|---:|']
    for r in records:
        w = r['oof_primary74']['session_balanced_window']
        lines.append(f"| {r['candidate']} | {w['accuracy']:.1%} | {w['macro_f1']:.4f} |")
    lines += ['', '## 6. 결과의 범위와 다음 단계', '',
              f"사전 고정한 주 지표의 1위는 **{records[0]['candidate']}**다. 이것은 이번 6개 후보와 고정 학습 조건 안의 순위다.",
              '추천은 60초 위상과 60초 진폭 두 후보를 다음 실시간 전환 시험에 남기는 것이다. '
              '날짜별 안정성을 우선하면 위상 60초, 정지한 사람을 빈 방으로 오판하는 비율을 우선하면 진폭 60초가 유리했다. '
              '주 지표 차이가 작으므로 확실한 우열이나 통계적 유의성을 주장하지 않는다.',
              '이번 조건에서는 두 계열 모두 60초가 가장 높았다. 그러나 60초 창에는 과거 상태가 오래 남을 수 있으므로 '
              '현재의 15초+3초 실시간 구성을 바로 교체할 근거는 충분하지 않다.',
              '기존 파일의 가중치를 그대로 평가한 것이 아니라 각 분할마다 새로 학습한 모델을 평가했다. 전체 자료로 학습한 배포 모델의 독립 정확도가 아니다.',
              '모든 자료가 한 방에서 수집됐고 이미 여러 실험에 사용됐다. 후보 선택에도 이 점수를 사용했으므로 새로운 독립 시험 점수나 다른 방의 보장 성능으로 쓰면 안 된다.',
              'motion 세션에도 멈춘 구간이 있어 창별 라벨은 실제 순간 움직임의 완벽한 정답이 아니다. 겹치는 창은 서로 독립 표본이 아니다.',
              '전체 학습을 끝낸 최종 모델 교체와 3초 움직임 모델·상태 기계 통합 비교는 이번에 실행하지 않았다. 새 출입·착석 전환 기록으로 선택 후보의 지연과 오판을 확인하는 것이 다음 단계다.', '',
              '## 7. 재현과 근거', '',
              '- [상세 지표와 세션별 결과](assets/pilot75/results.json)',
              '- [고정 설정·소스·원본 해시](assets/pilot75/identity.json)',
              '- [세션별 공통 유효 시점](assets/pilot75/data-audit.json)', '',
              '```bash', '.venv/bin/python model_train/pilot75/run.py run --device mps --threads 2',
              '.venv/bin/python model_train/pilot75/summarize.py', '```', '',
              '원본/소스/설정이 달라지면 같은 경로에서 재개를 거부한다. 완료한 seed 결과를 재사용한다. '
              '체크포인트·창별 확률은 로컬 `model_train/pilot75/runs/20261030/`에 있으며 실시간 로더용 모델 파일 형식은 아니다.', '']
    (ROOT / 'model_train/docs/model-training/pilot-75-session-report.md').write_text('\n'.join(lines))
    print(json.dumps([{k: v for k, v in r.items() if k not in ('folds', 'oof_all75', 'oof_primary74')}
                      for r in records], indent=2))


if __name__ == '__main__':
    main()
