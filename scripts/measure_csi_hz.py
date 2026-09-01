#!/usr/bin/env python3
"""세션의 RX별 수집 품질 요약 — Hz·gap·손실·무결성.

    python scripts/measure_csi_hz.py mac_collector_output/raw/20260825/143000_static_s21

신규 `.csi` 스토어를 읽고, 구 JSONL 세션이면 경고와 함께 읽을 수 있는 지표만 낸다.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import csi_store as cs  # noqa: E402


def analyze_frames(frames: np.ndarray) -> dict:
    h = frames["hdr"]
    n = len(frames)
    if n < 2:
        return {"n": n, "hz": 0.0}

    t = h["timestamp_us"].astype(np.int64) / 1e6      # 보드 시계 — 호스트 스케줄링과 무관
    dur = float(t[-1] - t[0])
    dt_ms = np.diff(t) * 1e3

    seq = h["seq"].astype(np.int64)
    boot = h["boot_id"]
    boot_changes = int((np.diff(boot.astype(np.int64)) != 0).sum())
    dseq = np.diff(seq)
    seq_gap = int(dseq[dseq > 1].sum() - (dseq > 1).sum())   # 점프분에서 정상 1스텝을 뺀 값
    seq_back = int((dseq < 0).sum())

    tx = h["tx_seq"].astype(np.int64)
    tx_back = int((np.diff(tx) < 0).sum())     # TX 재부팅 = 시간 격자 파손
    tx_span = int(tx[-1] - tx[0] + 1)
    tx_cov = len(np.unique(tx)) / tx_span if tx_span > 0 and not tx_back else 0.0

    agc_levels = len(np.unique(h["agc_gain"]))

    return {
        "n": n,
        "dur_s": dur,
        "hz": n / dur if dur > 0 else 0.0,
        "dt_ms_median": float(np.median(dt_ms)),
        "gaps_gt_200ms": int((dt_ms > 200.0).sum()),
        "gap_pct": 100.0 * float((dt_ms > 200.0).sum()) / len(dt_ms),
        "seq_gap": seq_gap,
        "seq_back": seq_back,
        "boot_changes": boot_changes,
        "tx_coverage": tx_cov,
        "tx_back": tx_back,
        "agc_levels": agc_levels,
        "rssi_median": float(np.median(h["rssi"])),
    }


def analyze_legacy_jsonl(path: Path) -> dict:
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
    if len(lines) < 2:
        return {"n": len(lines), "hz": 0.0}
    t = [x["received_at_unix_us"] for x in lines]
    seq = [x["seq"] for x in lines]
    dur = (t[-1] - t[0]) / 1e6
    dt = [(t[i + 1] - t[i]) / 1e3 for i in range(len(t) - 1)]
    big = sum(1 for d in dt if d > 200.0)
    dseq = [b - a for a, b in zip(seq, seq[1:])]
    return {
        "n": len(lines),
        "dur_s": dur,
        "hz": len(lines) / dur if dur > 0 else 0.0,
        "dt_ms_median": statistics.median(dt),
        "gaps_gt_200ms": big,
        "gap_pct": 100.0 * big / len(dt),
        "seq_gap": sum(d - 1 for d in dseq if d > 1),
        "seq_back": sum(1 for d in dseq if d < 0),
        "boot_changes": -1,
        "tx_coverage": float("nan"),
        "tx_back": -1,
        "agc_levels": -1,
        "rssi_median": float("nan"),
    }


def _fmt(name: str, r: dict) -> str:
    if r["n"] < 2:
        return f"  {name}: n={r['n']} (분석 불가)"
    return (
        f"  {name}: n={r['n']} dur={r['dur_s']:.2f}s hz={r['hz']:.2f} "
        f"median_dt={r['dt_ms_median']:.1f}ms gaps>200ms={r['gaps_gt_200ms']} ({r['gap_pct']:.1f}%)\n"
        f"        seq_gap={r['seq_gap']} seq_back={r['seq_back']} boot_changes={r['boot_changes']} "
        f"tx_back={r['tx_back']} tx_cov={r['tx_coverage']:.3f} "
        f"agc_levels={r['agc_levels']} rssi_med={r['rssi_median']:.0f}"
        + ("\n        [경고] TX 재부팅 감지 — 시간 격자가 깨져 학습에 쓸 수 없습니다. 재수집하세요."
           if r["tx_back"] > 0 else "")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="세션 수집 품질 요약")
    parser.add_argument("session_dir", type=Path, help="예: mac_collector_output/raw/.../143000_static_s21")
    args = parser.parse_args()
    d = args.session_dir
    if not d.is_dir():
        print(f"error: not a directory: {d}")
        return 1

    print(f"session: {d}\n")

    manifest_path = d / "session.json"
    if manifest_path.is_file():
        m = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"  label={m['label']}  session_id={m['session_id']}  frame_v{m['frame_version']}")
        for dev in m.get("devices", []):
            print(f"    reader RX{dev['device_id']}: crc_fail={dev['crc_fail']} "
                  f"invalid={dev['invalid']} resync={dev['resync']}")
        print()

    files = sorted(d.glob("device_*.csi"))
    if files:
        per_dev = {}
        for p in files:
            frames = cs.read_device_file(p)
            per_dev[cs.device_id_from_path(p)] = frames
            print(_fmt(p.name, analyze_frames(frames)))
        if len(per_dev) > 1:
            sets = [set(f["hdr"]["tx_seq"].tolist()) for f in per_dev.values()]
            common = set.intersection(*sets)
            smallest = min(len(s) for s in sets)
            print(f"\n  cross-RX 공통 tx_seq: {len(common)} / 최소 RX {smallest} "
                  f"= {100.0 * len(common) / smallest:.1f}%")
        return 0

    legacy = sorted(d.glob("device_*.jsonl"))
    if legacy:
        print("  [경고] 구 JSONL 세션 — 라벨 없음, 여러 런이 섞였을 수 있어 학습에 쓰지 마세요.\n")
        for p in legacy:
            print(_fmt(p.name, analyze_legacy_jsonl(p)))
        return 0

    print(f"error: device_*.csi / device_*.jsonl 없음: {d}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
