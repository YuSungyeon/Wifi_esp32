#!/usr/bin/env python3
"""1단계(움직임 여부) 모델의 **창 단위 동작**을 잰다 — 오경보율과 최소 감지 시간.

    python model_train/amp60/stage1_eval.py --model model_train/amp60/model_motion3s.pt \
        --dates 20260920 20260910

`transition_test.py` 의 "끼운 구간 최대값" 방식은 **짧은 창에 쓸 수 없다.** 창이 3초면 한
세션에 창이 1000개 나오고, 최대값은 행동이 아니라 꼬리를 잰다 — 실제로 정지 세션에서도
p_motion 최대가 0.896 이 나온다(창 정답률 0.966 의 뒷면). 그래서 여기서는 분포로 본다.

- **오경보**: rest 세션의 창 중 기준을 넘는 비율 → 시간당 몇 번인가
- **최소 감지 시간**: 연속 k 창이 기준을 넘어야 알린다고 할 때, k 가 덮는 시간
  (창 3초 + 간격 0.3초 × (k-1))

연속 창을 요구하는 이유는 단발 꼬리를 거르기 위해서다. 기준값 자체는 여기서 고르지 않는다 —
**전환 세션을 수집한 뒤** 골라야 한다. 이 표는 선택지를 보여줄 뿐이다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from model_train.amp60.train import TASKS, batch_input, build, load_sessions  # noqa: E402


def runs_above(flags: np.ndarray, k: int) -> int:
    """길이 k 이상으로 연속해 참인 구간의 개수 — 알림이 몇 번 울리는가."""
    if k <= 0 or not len(flags):
        return 0
    padded = np.concatenate(([False], flags, [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return int(sum((edges[i + 1] - edges[i]) >= k for i in range(0, len(edges), 2)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--dates", nargs="+", required=True)
    ap.add_argument("--stride", type=int, default=30, help="판단 간격 (frame) — 실시간과 같게")
    ap.add_argument("--thresholds", type=float, nargs="+", default=[0.5, 0.7, 0.9])
    ap.add_argument("--consecutive", type=int, nargs="+", default=[1, 2, 3, 5, 10])
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    torch.set_num_threads(2)

    head = torch.load(args.model, map_location="cpu", weights_only=False)
    if head.get("mode") != "A-motion":
        print(f"[중단] 1단계 체크포인트가 아니다 (mode={head.get('mode')})")
        return 1
    if overlap := set(head["training_dates"]) & set(args.dates):
        print(f"[중단] 학습에 쓴 날짜가 평가 대상에 있다: {sorted(overlap)}")
        return 1

    names, remap = TASKS[head["task"]]
    window = head["window_frames"]
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    models = []
    for state in head["state_dicts"]:
        m = build(len(names))
        m.load_state_dict(state)
        models.append(m.to(device).eval())

    sessions = load_sessions(tuple(args.dates), window, args.stride, None, remap)
    print(f"[평가] {args.model.name} · 창 {window / 100:.0f}초 · 간격 {args.stride / 100:.1f}초 · "
          f"{args.dates} {len(sessions)}세션", flush=True)

    by_class = {n: [] for n in names}
    per_session = {}
    with torch.no_grad():
        for key, v in sessions.items():
            probs = []
            for b in range(0, len(v["starts"]), 64):
                x = torch.from_numpy(batch_input(v["amp"], v["starts"][b:b + 64], window)).to(device)
                probs.append(torch.stack([m(x).softmax(1) for m in models]).mean(0).cpu().numpy())
            p = np.concatenate(probs)[:, names.index("motion")]
            by_class[names[v["label"]]].append(p)
            per_session[key] = {"true": names[v["label"]], "windows": len(p),
                                "mean": round(float(p.mean()), 4),
                                "p95": round(float(np.percentile(p, 95)), 4)}
    merged = {n: np.concatenate(v) for n, v in by_class.items() if v}
    hours = {n: len(v) * args.stride / 100 / 3600 for n, v in merged.items()}
    for n, v in merged.items():
        print(f"  {n:6s} 창 {len(v):>6}개 ({hours[n]:.2f}시간) · p_motion 평균 {v.mean():.3f} "
              f"· 중앙값 {np.median(v):.3f} · 95%ile {np.percentile(v, 95):.3f}")

    rest, motion = merged.get("rest"), merged.get("motion")
    rows = []
    print(f"\n  {'기준':>5} {'연속':>4} {'덮는 시간':>9} {'움직임 창 감지율':>14} "
          f"{'rest 오경보':>12} {'시간당':>8}")
    for thr in args.thresholds:
        for k in args.consecutive:
            covers = (window + args.stride * (k - 1)) / 100
            tpr = float((motion >= thr).mean()) if motion is not None else float("nan")
            fp = runs_above(rest >= thr, k) if rest is not None else 0
            # rest 창을 한 줄로 이었을 때의 비율이다. 세션 경계가 섞이므로 상한에 가깝다.
            per_h = fp / hours["rest"] if rest is not None and hours["rest"] else 0.0
            rows.append({"threshold": thr, "k": k, "covers_s": round(covers, 1),
                         "motion_window_rate": round(tpr, 4),
                         "rest_false_events": fp, "rest_false_per_hour": round(per_h, 2)})
            print(f"  {thr:>5.2f} {k:>4} {covers:>8.1f}초 {tpr:>14.3f} {fp:>12} {per_h:>8.1f}")

    worst = sorted(per_session.items(), key=lambda kv: -kv[1]["p95"] if kv[1]["true"] == "rest"
                   else kv[1]["p95"])[:5]
    print("\n  가장 시끄러운 rest 세션 / 가장 조용한 motion 세션")
    for key, r in worst:
        print(f"    {key:14s} {r['true']:6s} 평균 {r['mean']:.3f} · 95%ile {r['p95']:.3f}")

    if args.out:
        args.out.write_text(json.dumps(
            {"model": args.model.name, "dates": args.dates, "window_frames": window,
             "stride_frames": args.stride, "operating_points": rows,
             "per_session": per_session}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[기록] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
