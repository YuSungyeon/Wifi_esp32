#!/usr/bin/env python3
"""JSONL 세션 폴더의 RX별 실측 Hz·gap 비율 요약."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def analyze_jsonl(path: Path) -> dict:
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if len(lines) < 2:
        return {"path": str(path), "n": len(lines), "hz": 0.0}

    t = [x["received_at_unix_us"] for x in lines]
    seq = [x["seq"] for x in lines]
    dur = (t[-1] - t[0]) / 1e6
    dt = [(t[i + 1] - t[i]) / 1e3 for i in range(len(t) - 1)]
    big_gaps = sum(1 for d in dt if d > 200.0)

    return {
        "path": path.name,
        "n": len(lines),
        "dur_s": dur,
        "hz": len(lines) / dur if dur > 0 else 0.0,
        "dt_ms_median": statistics.median(dt),
        "gaps_gt_200ms": big_gaps,
        "gap_pct": 100.0 * big_gaps / len(dt),
        "seq_drop": (seq[-1] - seq[0] + 1) - len(lines),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure CSI packet rate from device_*.jsonl")
    parser.add_argument("session_dir", type=Path, help="e.g. mac_collector_output/raw/.../session_2")
    args = parser.parse_args()
    session_dir = args.session_dir
    if not session_dir.is_dir():
        print(f"error: not a directory: {session_dir}")
        return 1

    files = sorted(session_dir.glob("device_*.jsonl"))
    if not files:
        print(f"error: no device_*.jsonl in {session_dir}")
        return 1

    print(f"session: {session_dir}\n")
    for p in files:
        r = analyze_jsonl(p)
        print(
            f"  {r['path']}: n={r['n']} dur={r['dur_s']:.2f}s "
            f"hz={r['hz']:.2f} median_dt={r['dt_ms_median']:.1f}ms "
            f"gaps>200ms={r['gaps_gt_200ms']} ({r['gap_pct']:.1f}%) seq_drop={r['seq_drop']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
