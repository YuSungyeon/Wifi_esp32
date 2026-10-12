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
RAW = ROOT / "mac_collector_output" / "raw"
#: 같은 날·같은 배치에서 고른다 — 다른 배치를 섞으면 경계가 배치 변화까지 담는다
SOURCES = {
    "empty": RAW / "20260919" / "153454_empty_s1",
    "static": RAW / "20260919" / "181915_static_s10",
    "motion": RAW / "20260919" / "163507_motion_s5",
    #: 대조군 — 같은 클래스의 **다른 세션**. 끼워도 행동은 그대로이고 경계 불연속만 생긴다.
    #: 여기서 반응하면 그 수치는 행동이 아니라 이어 붙인 자국을 잰 것이다.
    "empty2": RAW / "20260919" / "155631_empty_s2",
    "static2": RAW / "20260919" / "182726_static_s11",
    "motion2": RAW / "20260919" / "164241_motion_s6",
}
#: 어떤 상태 안에 무엇을 끼울지. 서로 다른 상태끼리 짝지어야 변화가 생긴다.
BASES = {"motion": "static", "static": "empty", "empty": "static"}


def load_model(path: Path, device: str = "cpu"):
    """진폭(robust)과 위상(phase60) 체크포인트를 모두 받는다. (판단 함수, 창, 이름) 반환."""
    bundle = torch.load(path, map_location="cpu", weights_only=False)
    if bundle.get("mode") == "A":
        from model_train.robust.predict import load_bundle, load_raw_amplitude
        from model_train.robust.shared_temporal import amplitude_input

        bundle, models = load_bundle(path)
        models = [m.to(device) for m in models]
        window = bundle["window_frames"]

        def features(session_dir):
            return load_raw_amplitude(session_dir)[0]

        def judge(w):
            x = torch.from_numpy(amplitude_input(w)).unsqueeze(0).to(device)
            with torch.no_grad():
                return torch.stack([m(x).softmax(1) for m in models]).mean(0)[0].cpu().numpy()

        return features, judge, window, "진폭"

    from model_train.phase60 import features as F
    from model_train.phase60.predict import load_bundle

    bundle, models = load_bundle(path, device)
    window, ds = bundle["window_frames"], bundle["downsample"]
    diff = bundle.get("diff_channel", False)

    def features(session_dir):
        return F.session_phase(session_dir)

    def judge(w):
        x = torch.from_numpy(F.windows(w, np.array([0]), window, ds, diff)).to(device)
        with torch.no_grad():
            return torch.stack([m(x).softmax(1) for m in models]).mean(0)[0].cpu().numpy()

    return features, judge, window, "위상"


def sweep(series: np.ndarray, judge, window: int, stride: int, starts=None):
    """((n, 3) 확률, (n,) 창 시작 위치). starts 를 주면 그 창만 본다 (필요한 구간만 재려고)."""
    if starts is None:
        starts = range(0, len(series) - window + 1, stride)
    out, used = [], []
    for s in starts:
        if s < 0 or s + window > len(series):
            continue
        w = series[s:s + window]
        out.append(judge(w) if np.isfinite(w).all() else np.full(3, np.nan))
        used.append(s)
    return np.asarray(out), np.asarray(used)


def hold_test(feats: dict, judge, window: int, stride: int, lengths: list,
              target: str, base: str) -> list:
    """`base` 상태 한가운데 `target` 을 길이를 바꿔가며 끼우고, 판단이 바뀌는 최소 길이를 찾는다.

    **끼운 구간과 겹치는 창만** 본다. 전체에서 최대를 찾으면 모델이 원래 내던 오답까지 섞인다.
    같은 위치의 '끼우지 않은' 확률과 비교해, 기준을 넘고 **동시에** 뚜렷이 올라야 감지로 센다.
    """
    base_series, donor = feats[base], feats[target]
    ti = CLASSES.index(target)
    at = min(12000, len(base_series) - window - 1)
    rows = []
    for sec in lengths:
        n = int(sec * 100)
        starts = range(max(0, at - window + stride), at + n, stride)   # 끼운 구간과 겹치는 창만
        mixed = base_series.copy()
        mixed[at:at + n] = donor[5000:5000 + n]
        probs, used = sweep(mixed, judge, window, stride, starts)
        base_probs, _ = sweep(base_series, judge, window, stride, starts)
        if not len(probs):
            continue
        here = float(np.nanmax(probs[:, ti]))
        was = float(np.nanmax(base_probs[:, ti]))
        detected = here >= 0.7 and here - was >= 0.2
        rows.append({"seconds": sec, "prob": round(here, 3), "baseline": round(was, 3),
                     "detected": bool(detected)})
        print(f"    {target:7s} {sec:>4g}초 → {here:.3f} (끼우지 않으면 {was:.3f})"
              f"{'  ← 감지' if detected else ''}", flush=True)
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
    probs, starts_idx = sweep(series, judge, window, stride)
    ends = starts_idx + window

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
    ap.add_argument("--stride", type=int, default=300, help="판단 간격 (frame)")
    ap.add_argument("--device", default="mps", help="mps 또는 cpu")
    ap.add_argument("--burst", type=float, nargs="+",
                    default=[5, 10, 15, 20, 30, 45, 60, 90, 120])
    ap.add_argument("--targets", nargs="+", default=["motion", "static", "empty"],
                    choices=("motion", "static", "empty"))
    ap.add_argument("--hold", type=float, default=60.0, help="시퀀스 각 구간 길이 (초)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    torch.set_num_threads(2)

    features, judge, window, kind = load_model(args.model, args.device)
    print(f"[모델] {args.model.name} · {kind} · 창 {window / 100:.0f}초")
    feats = {k: features(v) for k, v in SOURCES.items()}

    lengths = [x for x in args.burst if x * 100 <= window * 1.5]
    print("[유지 시간] 각 상태가 몇 초 이어져야 판단이 바뀌는가 "
          "(괄호는 같은 클래스를 끼운 대조군)")
    holds = {}
    for target in args.targets:
        base = BASES[target]
        rows = hold_test(feats, judge, window, args.stride, lengths, target, base)
        # 같은 바탕에 **바탕과 같은 클래스**를 끼운 대조군 — 경계 불연속만 들어 있다
        ctrl_feats = {**feats, target: feats[f"{base}2"]}
        ctrl = hold_test(ctrl_feats, judge, window, args.stride, lengths, target, base)
        fired = next((r["seconds"] for r in ctrl if r["detected"]), None)
        got = next((r["seconds"] for r in rows if r["detected"]), None)
        holds[target] = {"rows": rows, "control": ctrl, "min_s": got,
                         "control_fired_s": fired, "trustworthy": fired is None}
        note = (f"대조군도 {fired}초에서 반응 — 불연속을 잰 값이라 믿을 수 없다"
                if fired else "대조군 무반응")
        print(f"  → {target}: {str(got) + '초' if got else '시험 범위에서 감지 안 됨'}  ({note})",
              flush=True)
    false_fire = any(h["control_fired_s"] is not None for h in holds.values())

    print(f"[sequence] 빈 방 → 움직임 → 정지 → 움직임 → 빈 방 (구간 {args.hold:.0f}초)")
    transitions = sequence_test(feats, judge, window, args.stride, args.hold)

    result = {"model": args.model.name, "window_s": window / 100, "kind": kind,
              "holds": holds, "any_control_fired": bool(false_fire),
              "transitions": transitions, "hold_s": args.hold}
    if args.out:
        args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[기록] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
