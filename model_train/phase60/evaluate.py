#!/usr/bin/env python3
"""저장된 체크포인트를 **학습하지 않고** 지정한 날짜로 평가한다.

    python model_train/phase60/evaluate.py --model model_train/phase60/model_dev3-10hz.pt \
        --dates 20260920 --tag holdout

봉인해 둔 데이터로 마지막에 한 번 쓰는 용도다. 학습 날짜가 평가 날짜에 섞이면 거부한다.
결과는 `<모델>.<tag>.json` 으로 남기고 체크포인트에도 덧붙인다 — 어떤 숫자가 어디서 나왔는지
파일 하나로 따라갈 수 있어야 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from model_train.phase60.predict import load_bundle  # noqa: E402
from model_train.phase60.train import evaluate, load_dates  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--dates", nargs="+", required=True)
    ap.add_argument("--tag", default="eval", help="결과 파일 이름에 붙일 꼬리표")
    ap.add_argument("--eval-stride", type=int, default=300)
    args = ap.parse_args()
    torch.set_num_threads(2)

    bundle, models = load_bundle(args.model)
    overlap = set(bundle["training_dates"]) & set(args.dates)
    if overlap:
        print(f"[중단] 학습에 쓴 날짜가 평가 대상에 있다: {sorted(overlap)}")
        return 1

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    models = [m.to(device) for m in models]
    sessions = load_dates(tuple(args.dates), bundle["window_frames"], args.eval_stride)
    print(f"[평가] {args.model.name} → {args.dates} {len(sessions)}세션 · device={device}",
          flush=True)
    metrics = evaluate(models, sessions, bundle["window_frames"], bundle["downsample"],
                       device, bundle.get("diff_channel", False))
    print(f"  세션 {metrics['sessions']} · 사람 있음/없음 {metrics['presence']} · "
          f"빈방vs정지 {metrics['empty_vs_static']} · 창 정답률 {metrics['window_accuracy']}")
    for r in metrics["per_session"]:
        if r["true"] != r["pred"]:
            print(f"  틀림 {r['session']} {r['true']} → {r['pred']} (창 정답률 {r['window_acc']})")

    out = args.model.with_suffix(f".{args.tag}.json")
    out.write_text(json.dumps({"dates": args.dates, **metrics}, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    bundle.setdefault("extra_evaluations", {})[args.tag] = {
        "dates": args.dates, "sessions": metrics["sessions"], "presence": metrics["presence"],
        "empty_vs_static": metrics["empty_vs_static"],
        "window_accuracy": metrics["window_accuracy"]}
    bundle["state_dicts"] = [{k: v.cpu() for k, v in m.state_dict().items()} for m in models]
    torch.save(bundle, args.model)
    print(f"[기록] {out.name} · 체크포인트의 extra_evaluations['{args.tag}'] 갱신")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
