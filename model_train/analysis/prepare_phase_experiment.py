#!/usr/bin/env python3
"""Prepare the frozen September 19/20 phase experiment from original .csi files."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
import csi_store as cs
from model_train.preprocessing import preprocess_3rx as pre
from model_train.preprocessing import phase_features as phase

CFG = pre.DEFAULT_CONFIG
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".webp"}


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for part in iter(lambda: stream.read(1024*1024), b""):
            digest.update(part)
    return digest.hexdigest()


def split_for(date, sid):
    if date == "20260919" and 1 <= sid <= 30:
        return "train" if sid <= 24 else "validation"
    if date == "20260920" and 31 <= sid <= 60:
        return "test"
    raise ValueError(f"session outside frozen split: {date}/{sid}")


def inventory(raw_root):
    rows, keys = [], set()
    for date in ("20260919", "20260920"):
        for path in sorted((raw_root / date).glob("*/session.json")):
            m = json.loads(path.read_text())
            sid = int(m["session_id"])
            key = f"{date}-s{sid}"
            if key in keys:
                raise ValueError(f"duplicate session key: {key}")
            keys.add(key)
            snapshot = path.parent / "session_meta_snapshot.yaml"
            snapshot_text = snapshot.read_text() if snapshot.exists() else ""
            placement = re.search(r"^\s*placement_id:\s*([^\s#]+)", snapshot_text, re.M)
            images = sorted(p for p in (path.parent / "image").glob("*")
                            if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS)
            rows.append({"key": key, "date": date, "session_id": sid,
                         "split": split_for(date, sid), "label": m["label"],
                         "label_id": cs.LABEL_MAP[m["label"]],
                         "path": str(path.parent.relative_to(ROOT)),
                         "manifest_sha256": sha256(path),
                         "snapshot_sha256": sha256(snapshot) if snapshot.exists() else None,
                         "snapshot_text": snapshot_text,
                         "placement_id": placement.group(1).strip("'\"") if placement else "unknown",
                         "collection_provenance": m.get("provenance"),
                         "devices": m["devices"],
                         "files": [{"rx": rx, "sha256": sha256(path.parent/f"device_{rx}.csi"),
                                    "bytes": (path.parent/f"device_{rx}.csi").stat().st_size}
                                   for rx in CFG.rx_order],
                         "images": [{"name": p.name, "sha256": sha256(p)} for p in images],
                         "image_role": "RX placement and experiment environment evidence; not model input"})
    rows.sort(key=lambda r: (r["date"], r["session_id"]))
    if len(rows) != 60:
        raise ValueError(f"expected 60 sessions, found {len(rows)}")
    return rows


def source_snapshot(output):
    destination = output / "source"
    destination.mkdir()
    sources = ["scripts/csi_store.py", "model_train/preprocessing/phase_features.py",
               "model_train/preprocessing/preprocess_3rx.py", "model_train/lstm/LSTM.py",
               "model_train/cnn1d/CNN1D.py", "model_train/analysis/prepare_phase_experiment.py",
               "model_train/analysis/run_phase_experiment.py",
               "model_train/docs/model-training/amplitude-phase-classification-design.md"]
    hashes = {}
    for name in sources:
        src = ROOT / name
        if not src.exists():
            continue
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        hashes[name] = sha256(src)
    diff = subprocess.check_output(["git", "diff", "HEAD", "--binary"], cwd=ROOT)
    (destination / "working-tree.patch").write_bytes(diff)
    return {"commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip(),
            "status": subprocess.check_output(["git", "status", "--short"], cwd=ROOT).decode(),
            "source_hashes": hashes}


def checked_frames(path, expected_rx_id):
    data = path.read_bytes()
    if not data or len(data) % cs.CSI_FRAME_SIZE:
        raise ValueError(f"empty or truncated frame file: {path}")
    frames = cs.read_device_file(path)
    h = frames["hdr"]
    good = ((h["magic"] == cs.FRAME_MAGIC) & (h["version"] == 4)
            & (h["frame_type"] == cs.FRAME_TYPE_CSI) & (h["total_len"] == cs.CSI_FRAME_SIZE)
            & (h["raw_len"] == 128) & (h["channel"] >= 1) & (h["channel"] <= 14)
            & (h["rssi"] >= -100) & (h["rssi"] <= 0) & (h["rx_id"] == expected_rx_id))
    if not good.all():
        raise ValueError(f"invalid header or receiver identity: {path}")
    actual = np.fromiter((cs.crc32_of(data[p:p+cs.CSI_FRAME_SIZE])
                          for p in range(0, len(data), cs.CSI_FRAME_SIZE)), dtype=np.uint32)
    if not np.array_equal(actual, h["crc32"]):
        raise ValueError(f"CRC mismatch: {path}")
    return frames


def select_common(frames_by_rx):
    candidates, diagnostics = {}, {}
    for rx, frames in frames_by_rx.items():
        h = frames["hdr"]
        # Use explicit boot IDs as an additional boundary for the binary format.
        edges = np.r_[0, np.flatnonzero(h["boot_id"][1:] != h["boot_id"][:-1])+1, len(h)]
        segments, removed_all, ambiguous_all = [], [], []
        dropped_count = 0
        for lo, hi in zip(edges[:-1], edges[1:]):
            records = [pre.Record(int(i)+1, int(h["seq"][i]), int(h["tx_seq"][i]), None)
                       for i in range(lo, hi)]
            kept, removed, ambiguous = pre.remove_single_corrupt(records, CFG)
            parts, _, dropped = pre.split_boot_segments(kept, CFG)
            segments.extend(parts)
            removed_all.extend(removed)
            ambiguous_all.extend(ambiguous)
            dropped_count += len(dropped)
        if any(pre.count_tx_seq_decreases(s) for s in segments):
            raise ValueError(f"RX {rx}: sustained tx_seq regression")
        candidates[rx] = pre.candidate_segments(segments)
        diagnostics[str(rx)] = {"raw_frames": len(frames), "crc_checked_frames": len(frames),
                                "boot_id_changes": len(edges)-2,
                                "segments": len(segments), "removed_single_corrupt": removed_all,
                                "ambiguous": ambiguous_all, "dropped_boundary_frames": dropped_count}
    chosen = pre.choose_combination(candidates, CFG)
    if chosen is None or chosen["common_length"] < CFG.min_common_length:
        raise ValueError("no sufficiently long common segment")
    ratios = pre.observed_ratios(chosen, CFG)
    if min(ratios.values()) < CFG.min_observed_ratio:
        raise ValueError(f"observed ratio below threshold: {ratios}")
    return chosen, diagnostics, ratios


def quantiles(values):
    values = np.asarray(values)
    values = values[np.isfinite(values)]
    return np.quantile(values, [0, .01, .5, .99, 1]).tolist() if len(values) else None


def phase_quality(z, amp, sine, cosine, valid, fits, headers):
    theta = np.angle(z[:, phase.RAW_INDICES])
    raw_resultant = np.abs(np.exp(1j*theta[valid]).mean(axis=0)) if valid.any() else np.full(51, np.nan)
    corr_resultant = np.abs((cosine[valid]+1j*sine[valid]).mean(axis=0)) if valid.any() else np.full(51, np.nan)
    adjacent = valid[1:] & valid[:-1] & (np.diff(headers["tx_seq"].astype(np.int64)) == 1)
    raw_delta = np.abs(np.angle(np.exp(1j*np.diff(theta, axis=0))))
    corr_complex = cosine + 1j*sine
    corr_delta = np.abs(np.angle(corr_complex[1:] * corr_complex[:-1].conj()))
    gain_change = ((headers["agc_gain"][1:] != headers["agc_gain"][:-1])
                   | (headers["fft_gain"][1:] != headers["fft_gain"][:-1]))
    return {"unique_frames": len(z), "valid_frames": int(valid.sum()),
            "zero_tone_frames": int((amp == 0).any(axis=1).sum()),
            "amplitude_quantiles": quantiles(amp),
            "amplitude_le_2_ratio": float((amp <= 2).mean()),
            "raw_circular_variance_mean": float(np.nanmean(1-raw_resultant)) if valid.any() else None,
            "corrected_circular_variance_mean": float(np.nanmean(1-corr_resultant)) if valid.any() else None,
            "raw_adjacent_time_abs_delta_quantiles_rad": quantiles(raw_delta[adjacent]),
            "corrected_adjacent_time_abs_delta_quantiles_rad": quantiles(corr_delta[adjacent]),
            "gain_change_adjacent_pairs": int((gain_change & adjacent).sum()),
            "corrected_delta_at_gain_changes": quantiles(corr_delta[adjacent & gain_change]),
            "corrected_delta_without_gain_changes": quantiles(corr_delta[adjacent & ~gain_change]),
            "fit_slope_quantiles": [quantiles(fits[:, i, 0]) for i in range(2)],
            "fit_intercept_quantiles": [quantiles(fits[:, i, 1]) for i in range(2)]}


def diagnostic_plot(path, z, sine, cosine, valid, tx_seqs):
    os.environ.setdefault("MPLCONFIGDIR", str(path.parent / "mplconfig"))
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    observed = np.flatnonzero(valid)
    if not len(observed):
        return
    i = int(observed[len(observed)//2])
    theta = np.angle(z[i, phase.RAW_INDICES])
    residual, fits = phase.detrend_phase(theta)
    fig, axes = plt.subplots(2, 1, figsize=(9, 6))
    for band, coeff in zip(phase.BANDS, fits):
        k = phase.FREQUENCIES[band]
        axes[0].plot(k, np.unwrap(theta[band]), color="gray", label="unwrapped")
        axes[0].plot(k, coeff[0]*k+coeff[1], color="orange", label="linear fit")
        axes[0].plot(k, residual[band], color="blue", label="residual")
    axes[0].set(xlabel="Subcarrier k (one packet)", ylabel="Phase (radian)")
    handles, labels = axes[0].get_legend_handles_labels()
    axes[0].legend(handles[:3], labels[:3], fontsize=8)
    # First 10 s only, explicit gaps; k=-17 is not an average over tones.
    seconds = (tx_seqs.astype(np.int64)-int(tx_seqs[0]))/100
    keep = seconds <= 10
    k_index = int(np.flatnonzero(phase.FREQUENCIES == -17)[0])
    axes[1].plot(seconds[keep], np.angle(z[keep, phase.RAW_INDICES[k_index]]),
                 alpha=.6, label="raw phase, k=-17")
    axes[1].plot(seconds[keep], np.arctan2(sine[keep, k_index], cosine[keep, k_index]),
                 alpha=.8, label="corrected phase, k=-17")
    axes[1].set(xlabel="TX sequence time (s, 100 Hz assumed)", ylabel="Wrapped phase (radian)")
    axes[1].legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)


def prepare_session(row, output):
    directory = ROOT / row["path"]
    folder = output / "sessions" / row["key"]
    folder.mkdir(parents=True, exist_ok=False)
    devices = {int(d["device_id"]): d for d in row["devices"]}
    frames = {}
    for entry in row["files"]:
        path = directory / f"device_{entry['rx']}.csi"
        if sha256(path) != entry["sha256"]:
            raise ValueError(f"raw file changed after inventory: {path}")
        frames[entry["rx"]] = checked_frames(path, int(devices[entry["rx"]].get("rx_id", 0)))
    chosen, quality, ratios = select_common(frames)
    length, start = chosen["common_length"], chosen["common_start"]
    amp_grid = np.full((3, length, 51), np.nan, np.float32)
    sin_grid, cos_grid = np.full_like(amp_grid, np.nan), np.full_like(amp_grid, np.nan)
    present, phase_valid = np.zeros((3, length), bool), np.zeros((3, length), bool)
    for r, rx in enumerate(CFG.rx_order):
        records, seen = [], set()
        for rec in chosen["segment_records"][rx]:
            if start <= rec.tx_seq <= chosen["common_end"] and rec.tx_seq not in seen:
                records.append(rec)
                seen.add(rec.tx_seq)
        indices = np.array([rec.line_no-1 for rec in records])
        selected = frames[rx][indices]
        z = cs.complex_csi(selected, valid_only=False)
        amp, sine, cosine, valid, fits = phase.extract(z)
        grid = selected["hdr"]["tx_seq"].astype(np.int64)-start
        amp_grid[r, grid], sin_grid[r, grid], cos_grid[r, grid] = amp, sine, cosine
        present[r, grid], phase_valid[r, grid] = True, valid
        quality[str(rx)].update(phase_quality(z, amp, sine, cosine, valid, fits, selected["hdr"]))
        quality[str(rx)]["observed_ratio"] = ratios[rx]
        if row["split"] != "test":
            diagnostic_plot(folder/f"rx{rx}-phase.png", z, sine, cosine, valid, selected["hdr"]["tx_seq"])
    amp_filled = pre.interpolate_short_gaps(amp_grid, present, CFG)
    amp_starts, candidates, _ = pre.select_window_starts(present, amp_filled, CFG)
    phase_filled = np.zeros_like(phase_valid)
    for r, rx in enumerate(CFG.rx_order):
        filled, rejected = phase.interpolate_phase(sin_grid[r:r+1], cos_grid[r:r+1], phase_valid[r:r+1])
        phase_filled[r:r+1] = filled
        quality[str(rx)]["phase_gap_rejections"] = rejected
        quality[str(rx)]["phase_interpolated_frames"] = int(filled.sum())
        quality[str(rx)]["amplitude_interpolated_frames"] = int(amp_filled[r].sum())
    phase_starts, _, _ = pre.select_window_starts(phase_valid, phase_filled, CFG)
    common = np.intersect1d(amp_starts, phase_starts)
    def flatten(values):
        return values.transpose(1, 0, 2).reshape(length, 153)
    all_features = np.concatenate([flatten(amp_grid), flatten(sin_grid), flatten(cos_grid)], axis=1)
    for s in common:
        if not np.isfinite(all_features[s:s+CFG.window]).all():
            raise ValueError(f"non-finite retained window {row['key']}:{s}")
    np.save(folder / "features.npy", all_features)
    np.save(folder / "starts.npy", common)
    np.save(folder / "amplitude-starts.npy", np.asarray(amp_starts, dtype=np.int64))
    summary = {k: row[k] for k in ("key", "date", "session_id", "split", "label", "label_id", "placement_id")}
    summary.update({"common_start": start, "common_end": chosen["common_end"],
                    "chosen_segments": chosen["segments"], "frames": length,
                    "candidate_windows": candidates, "amplitude_windows": len(amp_starts),
                    "common_windows": len(common),
                    "window_retention": len(common)/len(amp_starts) if amp_starts else 0,
                    "quality": quality, "image_count": len(row["images"]),
                    "feature_sha256": sha256(folder/"features.npy"),
                    "starts_sha256": sha256(folder/"starts.npy"),
                    "amplitude_starts_sha256": sha256(folder/"amplitude-starts.npy")})
    write_json(folder/"summary.json", summary)
    print(f"{row['key']} {row['split']} {row['label']}: amp={len(amp_starts)} common={len(common)}", flush=True)
    return summary


def build_indexes(output, summaries, split):
    folder = output / split
    folder.mkdir(exist_ok=True)
    common_rows, amp_rows = [], []
    for session in summaries:
        if session["split"] != split:
            continue
        base = output/"sessions"/session["key"]
        for filename, rows in (("starts.npy", common_rows), ("amplitude-starts.npy", amp_rows)):
            for start in np.load(base/filename):
                rows.append({"index": len(rows), "session_key": session["key"],
                             "session_id": session["session_id"], "date": session["date"],
                             "label_id": session["label_id"], "start": int(start),
                             "window_start_tx_seq": session["common_start"]+int(start),
                             "segments": session["chosen_segments"]})
    for filename, rows in (("windows.jsonl", common_rows), ("amplitude-windows.jsonl", amp_rows)):
        with (folder/filename).open("w") as stream:
            for row in rows:
                stream.write(json.dumps(row)+"\n")
    counts = np.bincount([r["label_id"] for r in common_rows], minlength=3)
    return {"windows": len(common_rows), "amplitude_windows": len(amp_rows),
            "class_counts": counts.tolist(), "all_classes_present": bool((counts>0).all()),
            "metadata_sha256": sha256(folder/"windows.jsonl")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=("development", "test"), required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if args.stage == "development":
        output.mkdir(parents=True, exist_ok=False)
        rows = inventory(ROOT/"mac_collector_output/raw")
        write_json(output/"inventory.json", rows)
        manifest = {"feature_version": phase.FEATURE_VERSION, "input_size": 459,
                    "window": CFG.window, "stride": CFG.stride, "rx_order": list(CFG.rx_order),
                    "raw_indices": phase.RAW_INDICES.tolist(), "frequencies": phase.FREQUENCIES.tolist(),
                    "max_gap": 5, "angle_limit_degrees": 90, "class_map": cs.LABEL_MAP,
                    "source": source_snapshot(output), "split_summary": {}, "sessions": []}
    else:
        manifest = json.loads((output/"manifest.json").read_text())
        rows = json.loads((output/"inventory.json").read_text())
        if "test" in manifest["split_summary"]:
            raise ValueError("test already prepared; refusing to overwrite")
    wanted = {"train", "validation"} if args.stage == "development" else {"test"}
    for row in rows:
        if row["split"] in wanted:
            manifest["sessions"].append(prepare_session(row, output))
            write_json(output/"manifest.json", manifest)
    for split in sorted(wanted):
        manifest["split_summary"][split] = build_indexes(output, manifest["sessions"], split)
    if args.stage == "development":
        train_arrays = [(np.load(output/"sessions"/s["key"]/"features.npy", mmap_mode="r")[:, :153],
                         np.load(output/"sessions"/s["key"]/"starts.npy"))
                        for s in manifest["sessions"] if s["split"] == "train"]
        mean, std, safe, count = phase.normalization_from_sessions(train_arrays)
        np.savez(output/"normalization.npz", mean=mean, std=std, std_safe=safe, train_frame_count=count)
        manifest["normalization_sha256"] = sha256(output/"normalization.npz")
        manifest["train_frame_count_with_overlap"] = count
    write_json(output/"manifest.json", manifest)
    write_json(output/"phase-quality.json", manifest["sessions"])
    write_json(output/"split.json", {s: [r["key"] for r in rows if r["split"] == s]
                                    for s in ("train", "validation", "test")})
    print(json.dumps(manifest["split_summary"], indent=2), flush=True)
    if not all(v["all_classes_present"] for v in manifest["split_summary"].values()):
        raise SystemExit("STOP: a split lost at least one class under the frozen quality rules")


if __name__ == "__main__":
    main()
