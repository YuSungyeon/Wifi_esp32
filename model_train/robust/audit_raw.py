#!/usr/bin/env python3
"""Audit one or more raw collection dates for the frozen amplitude model."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.analysis.prepare_phase_experiment import checked_frames, select_common, CFG
from model_train.preprocessing import preprocess_3rx as pre, phase_features as phase
from model_train.robust.predict import load_bundle
from model_train.robust.run_experiment import CLASSES, digest, save_json
from model_train.robust.shared_temporal import amplitude_input
from scripts import csi_store as cs


def longest_gap(mask):
    edges = np.diff(np.r_[False, mask, False].astype(int))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    return int((ends - starts).max()) if len(starts) else 0


def audit_session(path, bundle, training_hashes):
    manifest = json.loads(path.read_text())
    folder = path.parent
    date = folder.parent.name
    sid = manifest["session_id"]
    label = manifest["label"]
    row = {"date": date, "key": f"{date}-s{sid}", "session_id": sid, "label": label,
           "path": str(folder.relative_to(ROOT)), "manifest_sha256": digest(path),
           "wall_duration_s": (manifest["ended_at_unix_us"] - manifest["started_at_unix_us"]) / 1e6,
           "label_matches_folder": f"_{label}_s{sid}" in folder.name,
           "recorded_pipeline": manifest.get("pipeline"), "devices": [], "errors": []}
    snapshot_path = folder / "session_meta_snapshot.yaml"
    if snapshot_path.exists():
        snapshot = snapshot_path.read_text()
        row["snapshot_sha256"] = digest(snapshot_path)
        for name in ("placement_id", "label_target", "description"):
            match = re.search(rf"^\s*{name}:\s*([^\n#]+)", snapshot, re.M)
            row[name] = match.group(1).strip().strip("\"'") if match else None
    row["image_files"] = [str(p.relative_to(folder)) for p in folder.rglob("*")
                          if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".heic", ".webp")]
    if label not in CLASSES or {int(d["device_id"]) for d in manifest["devices"]} != {101, 102, 103}:
        row["errors"].append("unsupported label or receiver set")
        return row
    frames = {}
    for device in manifest["devices"]:
        rx = int(device["device_id"])
        file = folder / f"device_{rx}.csi"
        info = {"device_id": rx, "rx_id": int(device["rx_id"]), "bytes": file.stat().st_size}
        try:
            info["sha256"] = digest(file)
            info["matches_training_raw_hash"] = info["sha256"] in training_hashes
            frames[rx] = checked_frames(file, info["rx_id"])
            h = frames[rx]["hdr"]
            delta = np.diff(h["tx_seq"].astype(np.int64))
            elapsed = (int(h["timestamp_us"][-1]) - int(h["timestamp_us"][0])) / 1e6
            profile_matches = bool(((h["channel"] == 11) & (h["rate"] == 11) & (h["sig_len"] == 47)).all())
            count_matches = len(h) == device.get("frames")
            info.update(frames=len(h), crc_checked=True, manifest_frames_match=count_matches,
                        profile={k: np.unique(h[k]).tolist() for k in ("version", "channel", "rate", "sig_len", "raw_len")},
                        profile_matches_model=profile_matches, timestamp_span_s=elapsed,
                        received_hz=(len(h) - 1) / elapsed if elapsed > 0 else None,
                        tx_decreases=int((delta < 0).sum()), tx_duplicates=int((delta == 0).sum()),
                        tx_missing_frames=int(np.maximum(delta - 1, 0).sum()),
                        longest_raw_tx_gap_frames=int(np.maximum(delta - 1, 0).max()) if len(delta) else 0,
                        boot_changes=int((h["boot_id"][1:] != h["boot_id"][:-1]).sum()),
                        rssi_min_median_max=np.quantile(h["rssi"], [0, .5, 1]).tolist())
            if not profile_matches or not count_matches:
                row["errors"].append(f"RX {rx}: RF profile or manifest frame count differs")
        except (ValueError, OSError) as error:
            row["errors"].append(f"RX {rx}: {error}")
        row["devices"].append(info)
    if set(frames) == {101, 102, 103} and not row["errors"]:
        try:
            chosen, quality, ratios = select_common(frames)
            start, length = chosen["common_start"], chosen["common_length"]
            amp = np.full((3, length, 51), np.nan, np.float32)
            present = np.zeros((3, length), bool)
            for r, rx in enumerate((101, 102, 103)):
                records, seen = [], set()
                for rec in chosen["segment_records"][rx]:
                    if start <= rec.tx_seq <= chosen["common_end"] and rec.tx_seq not in seen:
                        records.append(rec)
                        seen.add(rec.tx_seq)
                f = frames[rx][np.asarray([rec.line_no - 1 for rec in records])]
                grid = f["hdr"]["tx_seq"].astype(np.int64) - start
                amp[r, grid] = np.abs(cs.complex_csi(f, valid_only=False)[:, phase.RAW_INDICES])
                present[r, grid] = True
            interp = pre.interpolate_short_gaps(amp, present, CFG)
            aligned = amp.transpose(1, 0, 2).reshape(length, 153)
            candidates = list(range(0, length - bundle["window_frames"] + 1, bundle["stride_frames"]))
            valid = [s for s in candidates if np.isfinite(aligned[s:s + bundle["window_frames"]]).all()]
            for s in valid:
                x = amplitude_input(aligned[s:s + bundle["window_frames"]])
                if x.shape != (300, 51, 3) or not np.isfinite(x).all():
                    raise ValueError("unexpected amplitude model input")
            row.update(common_length_frames=int(length), common_seconds=length / 100,
                       observed_ratio={str(k): float(v) for k, v in ratios.items()},
                       segment_diagnostics=quality, interpolated_frames_per_rx=interp.sum(1).tolist(),
                       unfilled_frames_per_rx=(~(present | interp)).sum(1).tolist(),
                       longest_unfilled_gap_frames_per_rx=[longest_gap(x) for x in ~(present | interp)],
                       window_candidates=len(candidates), valid_windows=len(valid),
                       rejected_window_start_frames=sorted(set(candidates) - set(valid)),
                       valid_window_start_frames=valid, input_contract_verified=True)
            if not valid:
                row["errors"].append("no valid model window")
        except ValueError as error:
            row["errors"].append(f"alignment/input: {error}")
    return row


def audit(args):
    bundle, _ = load_bundle(args.model)
    if bundle["stride_frames"] != 1500 or bundle["window_frames"] != 3000:
        raise ValueError("audit expects the frozen 30-second / 15-second contract")
    inventory = json.loads(args.training_inventory.read_text())
    if {s["key"] for s in inventory} != set(bundle["training_sessions"]):
        raise ValueError("training inventory does not match the frozen model")
    training_hashes = {f["sha256"] for s in inventory for f in s["files"]}
    paths = sorted({p.resolve() for raw_dir in args.raw_dir for p in raw_dir.glob("*/session.json")})
    if not paths:
        raise ValueError("no session manifests found")
    args.output.mkdir(parents=True, exist_ok=False)
    rows = []
    for path in paths:
        row = audit_session(path, bundle, training_hashes)
        rows.append(row)
        print(json.dumps({k: row.get(k) for k in ("key", "label", "common_seconds", "valid_windows", "window_candidates", "errors")}, ensure_ascii=False), flush=True)
    previous = {}
    for audit_path in args.previous_audit:
        data = json.loads(audit_path.read_text())
        previous.update({r["path"]: r for r in data["rows"]})
    hashes = defaultdict(list)
    for row in rows:
        prior = previous.get(row["path"])
        if prior:
            old = {d["device_id"]: d.get("sha256") for d in prior["devices"]}
            row["matches_previous_audit"] = (row["manifest_sha256"] == prior["manifest_sha256"]
                and {d["device_id"]: d.get("sha256") for d in row["devices"]} == old)
        else:
            row["matches_previous_audit"] = None
        for device in row["devices"]:
            if "sha256" in device:
                hashes[device["sha256"]].append({"key": row["key"], "device_id": device["device_id"]})
    by_date = {}
    for date in sorted({r["date"] for r in rows}):
        subset = [r for r in rows if r["date"] == date]
        by_date[date] = {"sessions": len(subset), "labels": dict(Counter(r["label"] for r in subset)),
                         "candidate_windows": sum(r.get("window_candidates", 0) for r in subset),
                         "valid_windows": sum(r.get("valid_windows", 0) for r in subset)}
    result = {"completed_at_utc": datetime.now(timezone.utc).isoformat(),
              "scope": "raw data quality and frozen-model input compatibility; no predictions or training",
              "dates": sorted(by_date), "sessions": len(rows), "labels": dict(Counter(r["label"] for r in rows)),
              "files": sum(len(r["devices"]) for r in rows),
              "bytes": sum(d.get("bytes", 0) for r in rows for d in r["devices"]),
              "raw_frames": sum(d.get("frames", 0) for r in rows for d in r["devices"]),
              "crc_checked_files": sum(bool(d.get("crc_checked")) for r in rows for d in r["devices"]),
              "errors": [{"key": r["key"], "errors": r["errors"]} for r in rows if r["errors"]],
              "candidate_windows": sum(r.get("window_candidates", 0) for r in rows),
              "valid_windows": sum(r.get("valid_windows", 0) for r in rows), "by_date": by_date,
              "duplicate_raw_hashes": {sha: locations for sha, locations in hashes.items() if len(locations) > 1},
              "overlapping_training_session_keys": sorted(set(bundle["training_sessions"]) & {r["key"] for r in rows}),
              "overlapping_training_raw_files": [{"key": r["key"], "device_id": d["device_id"]}
                                                  for r in rows for d in r["devices"] if d.get("matches_training_raw_hash")],
              "model_sha256": digest(args.model), "source_sha256": digest(Path(__file__)), "rows": rows}
    save_json(args.output / "audit.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--raw-dir", action="append", required=True, type=Path)
    p.add_argument("--model", required=True, type=Path)
    p.add_argument("--training-inventory", required=True, type=Path)
    p.add_argument("--previous-audit", action="append", default=[], type=Path)
    p.add_argument("--output", required=True, type=Path)
    audit(p.parse_args())


if __name__ == "__main__":
    main()
