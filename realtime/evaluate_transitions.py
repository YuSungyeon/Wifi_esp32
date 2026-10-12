#!/usr/bin/env python3
"""정답 구간이 있는 세션(합성 `demo_truth.json`)을 재생해 **시간 기준 정확도**와 전환 지연을 잰다.

    python realtime/evaluate_transitions.py --session mac_collector_output/demo/<이름> \
        [--model …] [--conf 0.7] [--rest-hold 15] [--motion-hold 1] [--direct 60]

정확도 = 화면에 보인 상태가 정답과 같은 시간 / 전체 시간. `deciding` 은 틀린 것으로 센다
(사람이 화면에서 보는 기준). 창이 차기 전 구간도 전체에 넣고, 창 이후만 따로 보인다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime.engine import MODEL, STAGE1, Engine  # noqa: E402
from realtime.state import StateMachine  # noqa: E402


def truth_at(segments: list, t: float) -> str:
    for sg in segments:
        if sg["from_s"] <= t < sg["to_s"]:
            return sg["state"]
    return segments[-1]["state"]


def replay(session: Path, model: Path, stride: int, sm: StateMachine, stage1, gate=None) -> list:
    """[(t, state)] — 0.3초마다 화면에 보였을 상태."""
    eng = Engine(session, model_path=model, stride=stride, stage1=stage1,
                 stage1_gate=(gate[0], int(gate[1])) if gate else None)
    eng.sm = sm
    for p in (eng.dir / "realtime_events.jsonl", eng.dir / "realtime_status.json"):
        if p.exists():
            p.unlink()
    t, out = eng.start_replay(), []
    while True:
        eng.step(now=t)
        out.append((t, eng.sm.state))
        if not eng.stream.advance(stride):
            break
        t += stride / 100.0
    return out


def score(trace: list, segments: list, stride_s: float) -> dict:
    total_s = segments[-1]["to_s"]
    warm = trace[0][0] if trace else total_s
    right = sum(stride_s for t, s in trace if s == truth_at(segments, t))
    after = len(trace) * stride_s
    per_state = {}
    for t, s in trace:
        tr = truth_at(segments, t)
        d = per_state.setdefault(tr, [0.0, 0.0])
        d[0] += stride_s
        d[1] += stride_s if s == tr else 0.0
    # 전환 지연: 정답 경계 → 그 상태가 화면에 처음 보인 시각
    delays = []
    for a, b in zip(segments, segments[1:]):
        t0 = b["from_s"]
        hit = next((t for t, s in trace if t >= t0 and s == b["state"]), None)
        wrong = sum(stride_s for t, s in trace if t0 <= t < b["to_s"] and s != b["state"])
        delays.append({"at_s": t0, "from": a["state"], "to": b["state"],
                       "delay_s": None if hit is None else round(hit - t0, 1),
                       "wrong_s": round(wrong, 1)})
    return {"total_s": total_s, "warmup_s": round(warm, 1),
            "acc_all": round(right / total_s, 4), "acc_after_window": round(right / after, 4),
            "per_state": {k: round(v[1] / v[0], 4) for k, v in per_state.items()},
            "transitions": delays}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, required=True, nargs="+")
    ap.add_argument("--model", type=Path, default=MODEL)
    ap.add_argument("--stage1", type=Path, default=STAGE1)
    ap.add_argument("--stride", type=int, default=30)
    ap.add_argument("--conf", type=float, default=0.6)
    ap.add_argument("--rest-hold", type=float, default=5.0)
    ap.add_argument("--motion-hold", type=float, default=1.0)
    ap.add_argument("--direct", type=float, default=10.0)
    ap.add_argument("--gate", type=float, nargs=2, default=(0.9, 3), metavar=("THR", "N"),
                    help="1단계 게이트: p_motion ≥ THR 가 N 번 연속이면 motion 후보. 끄려면 --no-gate")
    ap.add_argument("--no-gate", action="store_true")
    ap.add_argument("--truth-plan", nargs="+", default=None, metavar="STATE:SEC",
                    help="demo_truth.json 이 없는 실제 세션의 정답 — 세션 시작 기준 대본 (예: empty:120 motion:60 …)")
    ap.add_argument("--json", type=Path, default=None)
    args = ap.parse_args()

    results = []
    for session in args.session:
        if args.truth_plan:
            t0, segs = 0.0, []
            for item in args.truth_plan:
                state, _, sec = item.partition(":")
                segs.append({"state": state, "from_s": t0, "to_s": t0 + float(sec)})
                t0 += float(sec)
            truth = {"segments": segs}
        else:
            truth = json.loads((session / "demo_truth.json").read_text(encoding="utf-8"))
        sm = StateMachine(conf=args.conf, rest_hold_s=args.rest_hold,
                          motion_hold_s=args.motion_hold, direct_switch_s=args.direct)
        trace = replay(session, args.model, args.stride, sm, args.stage1,
                       None if args.no_gate else args.gate)
        r = score(trace, truth["segments"], args.stride / 100.0)
        r["session"] = session.name
        results.append(r)
        print(f"{session.name}: 전체 {r['acc_all']:.1%} · 창 이후 {r['acc_after_window']:.1%} · "
              + " ".join(f"{k} {v:.0%}" for k, v in r["per_state"].items()))
        for d in r["transitions"]:
            print(f"    {d['from']:7s}→{d['to']:7s} @{d['at_s']:>5.0f}s  "
                  f"지연 {d['delay_s'] if d['delay_s'] is not None else '없음':>5}  오상태 {d['wrong_s']:.0f}초")
    if len(results) > 1:
        print(f"평균: 전체 {sum(r['acc_all'] for r in results) / len(results):.1%}")
    if args.json:
        args.json.write_text(json.dumps({"args": vars(args) | {"session": [str(s) for s in args.session],
                                                                "model": str(args.model), "stage1": str(args.stage1),
                                                                "json": str(args.json)},
                                         "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
