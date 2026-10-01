#!/usr/bin/env python3
"""합성 전환 시퀀스로 **반응 지연과 최소 감지 길이**를 잰다.

    # 배포 진폭 모델 (실시간 경로가 쓰는 것)
    python model_train/phase60/transition_test.py --model model_train/robust/model.pt
    # 위상 모델
    python model_train/phase60/transition_test.py --model model_train/phase60/model_15sessions_v2.pt

두 가지를 잰다.

1. **burst** — 정지 세션 가운데 움직임을 길이를 바꿔가며 끼워 넣고, 출력이 뒤집히는 최소 길이
2. **sequence** — 빈 방 → 움직임 → 정지 → 움직임 → 빈 방 을 이어 붙이고 각 전환의 지연

**한계**: 다른 세션에서 잘라 붙인 것이라 사람이 실제로 움직여 생기는 연속 변화와 다르다.
경계가 불연속이라 진짜 전환보다 쉽게 잡힐 수도, 정규화 때문에 덜 잡힐 수도 있다. 그래도
**창 길이로 인한 희석**은 그대로 드러난다. 실제 지연은 전환 세션을 수집해야 확정된다.
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

CLASSES = ("empty", "static", "motion")
#: 같은 날·같은 배치에서 고른다 — 배치가 다르면 이어 붙인 경계가 배치 변화까지 섞는다
SOURCES = {
    "empty": ROOT / "mac_collector_output/raw/20260919/153454_empty_s1",
    "static": ROOT / "mac_collector_output/raw/20260919/181915_static_s10",
    "motion": ROOT / "mac_collector_output/raw/20260919/163507_motion_s5",
}


def load_model(path: Path):
    """진폭(robust)과 위상(phase60) 체크포인트를 모두 받는다. (판단 함수, 창, 이름) 반환."""
    bundle = torch.load(path, map_location="cpu", weights_only=False)
    if bundle.get("mode") == "A":
        from model_train.robust.predict import load_bundle, load_raw_amplitude
        from model_train.robust.shared_temporal import amplitude_input

        bundle, models = load_bundle(path)
        window = bundle["window_frames"]

        def features(session_dir):
            return load_raw_amplitude(session_dir)[0]

        def judge(w):
            x = torch.from_numpy(amplitude_input(w)).unsqueeze(0)
            with torch.no_grad():
                return torch.stack([m(x).softmax(1) for m in models]).mean(0)[0].numpy()

        return features, judge, window, "진폭"

    from model_train.phase60 import features as F
    from model_train.phase60.predict import load_bundle

    bundle, models = load_bundle(path)
    window, ds = bundle["window_frames"], bundle["downsample"]
    diff = bundle.get("diff_channel", False)

    def features(session_dir):
        return F.session_phase(session_dir)

    def judge(w):
        x = torch.from_numpy(F.windows(w, np.array([0]), window, ds, diff))
        with torch.no_grad():
            return torch.stack([m(x).softmax(1) for m in models]).mean(0)[0].numpy()

    return features, judge, window, "위상"


def sweep(series: dict, judge, window: int, stride: int) -> np.ndarray:
    """(n, 3) 확률. 각 행은 창 끝이 그 위치일 때의 판단이다."""
    out = []
    for s in range(0, len(series) - window + 1, stride):
        w = series[s:s + window]
        out.append(judge(w) if np.isfinite(w).all() else np.full(3, np.nan))
    return np.asarray(out)


def burst_test(feats: dict, judge, window: int, stride: int, lengths: list) -> list:
    """정지 가운데 움직임을 끼워 넣고 뒤집히는 최소 길이를 찾는다."""
    static, motion = feats["static"], feats["motion"]
    at = min(12000, len(static) - window - 1)
    rows = []
    for sec in lengths:
        n = int(sec * 100)
        mixed = static.copy()
        mixed[at:at + n] = motion[5000:5000 + n]
        probs = sweep(mixed, judge, window, stride)
        p_motion = np.nanmax(probs[:, 2])
        rows.append({"burst_seconds": sec, "max_motion_prob": round(float(p_motion), 3),
                     "detected": bool(p_motion >= 0.7)})
        print(f"  움직임 {sec:>4g}초 → 최대 확률 {p_motion:.3f}"
              f"{'  ← 감지' if p_motion >= 0.7 else ''}", flush=True)
    return rows


def sequence_test(feats: dict, judge, window: int, stride: int, hold: float) -> list:
    """빈 방 → 움직임 → 정지 → 움직임 → 빈 방. 각 전환 뒤 몇 초 만에 정답이 1등이 되는가."""
    n = int(hold * 100)
    plan = ["empty", "motion", "static", "motion", "empty"]
    parts, truth = [], []
    for i, label in enumerate(plan):
        start = 3000 + i * n                      # 구간마다 다른 지점에서 떠온다
        parts.append(feats[label][start:start + n])
        truth += [CLASSES.index(label)] * n
    series = np.concatenate(parts)
    truth = np.asarray(truth)
    probs = sweep(series, judge, window, stride)
    ends = np.arange(window, window + len(probs) * stride, stride)[:len(probs)]

    rows = []
    for i in range(1, len(plan)):
        change = i * n                            # 전환이 일어난 frame
        after = ends >= change
        if not after.any():
            continue
        want = CLASSES.index(plan[i])
        correct = after & (np.nan_to_num(probs[:, want]) == np.nanmax(probs, axis=1))
        delay = (ends[correct][0] - change) / 100.0 if correct.any() else None
        rows.append({"from": plan[i - 1], "to": plan[i],
                     "delay_s": round(float(delay), 1) if delay is not None else None})
        print(f"  {plan[i-1]:6s} → {plan[i]:6s}  "
              f"{'지연 ' + format(delay, '.1f') + '초' if delay is not None else '끝까지 못 따라감'}",
              flush=True)
    held = np.nan_to_num(probs).argmax(1)
    truth_at_end = truth[np.clip(ends - 1, 0, len(truth) - 1)]
    print(f"  구간 정답률 {float((held == truth_at_end).mean()):.3f} "
          f"(각 구간 {hold:.0f}초)", flush=True)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--stride", type=int, default=100, help="판단 간격 (frame)")
    ap.add_argument("--burst", type=float, nargs="+",
                    default=[1, 2, 3, 5, 10, 15, 20, 30, 45, 60, 90])
    ap.add_argument("--hold", type=float, default=60.0, help="시퀀스 각 구간 길이 (초)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    torch.set_num_threads(2)

    features, judge, window, kind = load_model(args.model)
    print(f"[모델] {args.model.name} · {kind} · 창 {window / 100:.0f}초")
    feats = {k: features(v) for k, v in SOURCES.items()}

    print("[burst] 정지 가운데 움직임을 끼워 넣는다")
    lengths = [s for s in args.burst if s * 100 <= window * 1.5]
    bursts = burst_test(feats, judge, window, args.stride, lengths)
    smallest = next((r["burst_seconds"] for r in bursts if r["detected"]), None)
    print(f"  → 감지되는 최소 길이: "
          f"{str(smallest) + '초' if smallest else '없음 (시험한 범위에서)'}")

    print(f"[sequence] 빈 방 → 움직임 → 정지 → 움직임 → 빈 방 (구간 {args.hold:.0f}초)")
    transitions = sequence_test(feats, judge, window, args.stride, args.hold)

    result = {"model": args.model.name, "window_s": window / 100, "kind": kind,
              "bursts": bursts, "min_detected_s": smallest, "transitions": transitions,
              "hold_s": args.hold}
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[기록] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
