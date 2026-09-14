#!/usr/bin/env python3
"""Reproduce 20260616 windows and audit sessions 10/19 without environment snapshots.

This reads original data, existing preprocessing products, and saved predictions.
It never trains, relabels, or writes into the input directories.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model_train" / "preprocessing"))
import preprocess_3rx as prep

FOCUS = (9, 10, 19, 20)
CLASS_NAMES = ("empty", "static", "motion")


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path):
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def relative(path):
    return str(path.resolve().relative_to(ROOT))


def coverage_mask(length, starts, window):
    """Count a frame once even when many overlapping windows use it."""
    changes = np.zeros(length + 1, dtype=np.int64)
    for start in starts:
        if start < 0 or start + window > length:
            raise ValueError("window outside reconstructed session")
        changes[start] += 1
        changes[start + window] -= 1
    return np.cumsum(changes[:-1]) > 0


def statistics(values):
    values = np.asarray(values, dtype=np.float64)
    return {
        "count": int(values.size), "mean": float(values.mean()),
        "std": float(values.std()), "min": float(values.min()),
        "max": float(values.max()),
    }


def json_default(value):
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, np.generic):
        return value.item()
    raise TypeError(type(value).__name__)


def raw_audit(raw_dir, entry, rx, active, logs_dir):
    """Inspect transport evidence, never read session_meta_snapshot.yaml.

    The selected common range is checked against the preprocessor's observed count.
    seq/timestamp resets are reported separately from host-clock decreases.
    """
    sid = entry["session_id"]
    path = raw_dir / f"session_{sid}" / f"device_{rx}.jsonl"
    selected, seen = [], set()
    ids, device_ids, firmware_ids = set(), set(), set()
    previous = None
    decreases = {key: [] for key in ("seq", "tx_seq", "timestamp_us", "received_at_unix_us")}
    channels, rates, sample_counts, sig_lengths = set(), set(), set(), set()
    rows = 0
    first_time = last_time = None
    amplitude_sum = np.zeros(64, dtype=np.float64)
    amplitude_sumsq = np.zeros(64, dtype=np.float64)
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            record = json.loads(line)
            rows += 1
            ids.add(record["session_id"])
            device_ids.add(record["device_id"])
            firmware_ids.add(record["firmware_session_id"])
            first_time = record["received_at_unix_us"] if first_time is None else first_time
            last_time = record["received_at_unix_us"]
            if previous is not None:
                for key in decreases:
                    if record[key] < previous[key]:
                        decreases[key].append({"line": line_no, "before": previous[key], "after": record[key]})
            previous = record
            tx = record["tx_seq"]
            if entry["common_start"] <= tx <= entry["common_end"] and tx not in seen:
                seen.add(tx)
                amplitude = np.asarray(record["csi_amp"], dtype=np.float64)
                amplitude_sum += amplitude
                amplitude_sumsq += amplitude ** 2
                selected.append((tx, record["received_at_unix_us"], record["rssi_dbm"],
                                 record["noise_floor_dbm"], np.mean(amplitude[active])))
                channels.add(record["channel"])
                rates.add(record["rate"])
                sample_counts.add(record["sample_count"])
                sig_lengths.add(record["sig_len"])
    if ids != {sid} or device_ids != {rx}:
        raise ValueError(f"record/path ID mismatch: {path}")
    data = np.asarray(selected, dtype=np.float64)
    expected_count = round(entry["observed_ratio"][str(rx)] * entry["common_length"])
    if len(data) != expected_count:
        raise ValueError(f"selected raw count mismatch: {path}")
    time_bins = (data[:, 0].astype(np.int64) - entry["common_start"]) // 100
    series = []
    for index in np.unique(time_bins):
        part = data[time_bins == index]
        series.append({"seconds": float(index + 0.5), "rssi_dbm": float(part[:, 2].mean()),
                       "observed_records": int(len(part)), "raw_amplitude_mean": float(part[:, 4].mean())})
    bins_30s = []
    for start in range(0, 300, 30):
        part = data[(time_bins >= start) & (time_bins < start + 30)]
        if len(part):
            bins_30s.append({"start_seconds": start, "observed_records": int(len(part)),
                             "rssi_dbm": float(part[:, 2].mean()), "raw_amplitude_mean": float(part[:, 4].mean())})
    log_rows = []
    for log_path in sorted(logs_dir.glob(f"reader_session{sid}_dev{rx}_20260616_*.log")):
        content = log_path.read_text(encoding="utf-8")
        endings = re.findall(r"done\. total frames=(\d+) invalid=(\d+) seq_drop=(\d+) elapsed=([\d.]+)s hz_avg=([\d.]+)", content)
        log_rows.append({"path": relative(log_path), "sha256": sha256(log_path),
                         "completed_runs": [{"frames": int(a), "invalid": int(b), "seq_drop": int(c),
                                             "elapsed_seconds": float(d), "hz_avg": float(e)}
                                            for a, b, c, d, e in endings]})
    logged_frames = sum(run["frames"] for row in log_rows for run in row["completed_runs"])
    if logged_frames != rows:
        raise ValueError(f"reader log / JSONL frame count mismatch: {path}")
    amplitude_mean = amplitude_sum / len(data)
    amplitude_std = np.sqrt(np.maximum(amplitude_sumsq / len(data) - amplitude_mean ** 2, 0))
    def local_time(micros):
        return datetime.fromtimestamp(micros / 1e6, ZoneInfo("Asia/Seoul")).isoformat(timespec="milliseconds")
    return {
        "path": relative(path), "sha256": sha256(path), "record_count": rows,
        "record_session_ids": sorted(ids), "device_ids": sorted(device_ids),
        "firmware_session_ids": sorted(firmware_ids),
        "first_host_time_kst": local_time(first_time), "last_host_time_kst": local_time(last_time),
        "selected_first_host_time_kst": local_time(data[0, 1]),
        "selected_last_host_time_kst": local_time(data[-1, 1]),
        "selected_host_duration_seconds": float((data[-1, 1] - data[0, 1]) / 1e6),
        "decreases": decreases, "selected_record_count": len(selected),
        "max_internal_tx_gap": int(np.max(np.diff(data[:, 0]) - 1)),
        "channel_values": sorted(channels), "rate_values": sorted(rates),
        "sample_count_values": sorted(sample_counts), "sig_len_values": sorted(sig_lengths),
        "rssi_dbm": statistics(data[:, 2]), "noise_floor_dbm": statistics(data[:, 3]),
        "first_60s_rssi_mean": float(data[time_bins < 60, 2].mean()),
        "last_60s_rssi_mean": float(data[time_bins >= time_bins.max() - 59, 2].mean()),
        "observed_feature_mean": amplitude_mean,
        "observed_mean_temporal_std": float(amplitude_std[active].mean()),
        "time_series": series, "bins_30s": bins_30s, "reader_logs": log_rows,
        "reader_frame_count_matches_jsonl": True,
    }


def prediction_audit(runs_dir, dataset_dir, entries):
    windows = read_jsonl(dataset_dir / "test" / "windows.jsonl")
    runs = []
    for path in sorted(runs_dir.glob("*-balanced/test-predictions.jsonl")):
        config = read_json(path.parent / "config.json")
        if config["dataset_manifest_sha256"] != sha256(dataset_dir / "manifest.json"):
            raise ValueError(f"manifest hash mismatch: {path}")
        if config["normalization_sha256"] != sha256(dataset_dir / "normalization.npz"):
            raise ValueError(f"normalization hash mismatch: {path}")
        rows = read_jsonl(path)
        if len(rows) != len(windows):
            raise ValueError(f"prediction count mismatch: {path}")
        for record, window in zip(rows, windows):
            for key in ("index", "session_id", "window_start_tx_seq"):
                if record[key] != window[key]:
                    raise ValueError(f"prediction/window {key} mismatch")
            if record["true_label_id"] != window["label_id"]:
                raise ValueError("prediction label mismatch")
            probs = np.asarray([record["probabilities"][name] for name in CLASS_NAMES])
            if not np.isfinite(probs).all() or (probs < 0).any() or not np.isclose(probs.sum(), 1, atol=1e-5):
                raise ValueError("invalid saved probabilities")
            if int(probs.argmax()) != record["predicted_label_id"]:
                raise ValueError("saved probability/argmax mismatch")
        saved = read_json(path.parent / "test-metrics.json")
        session_rows = []
        for sid in FOCUS:
            subset = [row for row in rows if row["session_id"] == sid]
            probs = np.asarray([[row["probabilities"][name] for name in CLASS_NAMES] for row in subset])
            accuracy = float(np.mean([row["predicted_label_id"] == row["true_label_id"] for row in subset]))
            session = next(row for row in saved["session_level"]["sessions"] if row["session_id"] == sid)
            # Training stored float32 softmax values; accumulate the same way for this check.
            reproduced_mean = probs.astype(np.float32).mean(axis=0)
            if not np.allclose(reproduced_mean, [session["mean_probabilities"][name] for name in CLASS_NAMES], atol=1e-6):
                raise ValueError("session probability aggregation mismatch")
            if not np.isclose(accuracy, session["window_accuracy"]):
                raise ValueError("session window accuracy mismatch")
            times = np.asarray([(row["window_start_tx_seq"] - entries[sid]["common_start"]) / 100 for row in subset])
            bins = []
            for start in range(0, 300, 30):
                mask = (times >= start) & (times < start + 30)
                if mask.any():
                    pred = probs[mask].argmax(axis=1)
                    bins.append({"start_seconds": start, "window_count": int(mask.sum()),
                                 "accuracy": float(np.mean(pred == entries[sid]["label_id"])),
                                 "mean_probabilities": probs[mask].mean(axis=0)})
            session_rows.append({"session_id": sid, "window_count": len(subset), "accuracy": accuracy,
                                 "session_prediction": CLASS_NAMES[int(reproduced_mean.argmax())],
                                 "mean_probabilities": reproduced_mean,
                                 "seconds": times, "probabilities": probs, "bins_30s": bins})
        runs.append({"seed": config["training"]["seed"], "path": relative(path),
                     "sha256": sha256(path), "sessions": session_rows})
    if sorted(row["seed"] for row in runs) != [0, 1, 2]:
        raise ValueError("expected exactly the three balanced baseline runs")
    return runs


def compare_to_train(sessions, active):
    training = [row for row in sessions if row["split"] == "train"]
    results = []
    for target in sessions:
        if target["session_id"] not in FOCUS:
            continue
        distances = []
        for reference in training:
            delta = target["normalized_mean"][active] - reference["normalized_mean"][active]
            distances.append({"session_id": reference["session_id"], "label": reference["label"],
                              "distance": float(np.sqrt(np.mean(delta ** 2)))})
        results.append({"session_id": target["session_id"],
                        "train_distances": sorted(distances, key=lambda row: row["distance"]),
                        "nearest_per_class": {name: min((row for row in distances if row["label"] == name),
                                                        key=lambda row: row["distance"]) for name in CLASS_NAMES}})
    return results


def figures(sessions, raw_records, predictions, active, directory):
    # Use an isolated writable cache and a headless backend; no GUI or global config writes.
    with tempfile.TemporaryDirectory(prefix="csi-audit-mpl-") as cache:
        os.environ["MPLCONFIGDIR"] = cache
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.rcParams.update({"font.size": 10, "axes.spines.top": False, "axes.spines.right": False})
        directory.mkdir(parents=True, exist_ok=True)
        lookup = {row["session_id"]: row for row in sessions}
        fig, axes = plt.subplots(2, 3, figsize=(14, 7), constrained_layout=True)
        for row_index, (label, good, bad) in enumerate((("empty", 9, 10), ("static", 20, 19))):
            for r, rx in enumerate((101, 102, 103)):
                ax = axes[row_index, r]
                idx = np.flatnonzero(active[r * 64:(r + 1) * 64])
                feature_idx = r * 64 + idx
                train = np.stack([row["normalized_mean"][feature_idx] for row in sessions
                                  if row["split"] == "train" and row["label"] == label])
                ax.fill_between(idx, train.min(axis=0), train.max(axis=0), color="#b9bec8", alpha=.35,
                                label=f"{label} train min-max")
                ax.plot(idx, train.mean(axis=0), color="#666b75", linestyle="--", label="train session mean")
                ax.plot(idx, lookup[good]["normalized_mean"][feature_idx], color="#087e8b", label=f"S{good}: correct")
                ax.plot(idx, lookup[bad]["normalized_mean"][feature_idx], color="#cc4c29", label=f"S{bad}: incorrect")
                ax.set(title=f"{label} / RX{rx}", xlabel="CSI feature index (active train features)", ylabel="Mean standardized CSI (train statistics)")
                ax.grid(alpha=.18)
                if r == 0:
                    ax.legend(fontsize=8, loc="best")
        fig.suptitle("Session mean CSI profiles — descriptive comparison, not LSTM explanations", fontsize=14)
        fig.savefig(directory / "feature-profiles.png", dpi=160)
        plt.close(fig)

        fig, axes = plt.subplots(4, 2, figsize=(14, 12), sharex="col", constrained_layout=True)
        for col, sid in enumerate((10, 19)):
            for r, rx in enumerate((101, 102, 103)):
                ax = axes[r, col]
                series = lookup[sid]["time_series"]
                ax.plot([row["seconds"] for row in series], [row["rx_mean"][r] for row in series],
                        color="#087e8b", label="CSI amplitude (aligned, 1s mean)")
                ax.set_ylabel(f"RX{rx} amplitude", color="#087e8b")
                ax.grid(alpha=.15)
                twin = ax.twinx()
                rf = raw_records[str(sid)][str(rx)]["time_series"]
                twin.plot([row["seconds"] for row in rf], [row["rssi_dbm"] for row in rf],
                          color="#cc4c29", alpha=.7, label="RSSI (observed, 1s mean)")
                twin.set_ylabel("RSSI (dBm)", color="#cc4c29")
                if r == 0:
                    ax.set_title(f"Session {sid}, assigned label: {lookup[sid]['label']}")
            ax = axes[3, col]
            for run in predictions:
                pred = next(row for row in run["sessions"] if row["session_id"] == sid)
                ax.plot(pred["seconds"], pred["probabilities"][:, 1], linewidth=1,
                        label=f"Seed {run['seed']}")
            ax.set(ylim=(-.03, 1.03), xlabel="Seconds since selected common start (tx_seq / 100 Hz)",
                   ylabel="Saved P(static), per window")
            ax.legend(loc="best")
            ax.grid(alpha=.2)
        fig.suptitle("Observed signal and saved predictions — environment snapshots excluded", fontsize=14)
        fig.savefig(directory / "signal-and-predictions.png", dpi=150)
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--figure-dir", type=Path)
    args = parser.parse_args()
    raw_dir = ROOT / "mac_collector_output/raw/20260616"
    dataset_dir = ROOT / "model_train/preprocessing/output/20260616"
    runs_dir = ROOT / "model_train/lstm/runs"
    output_dir = args.output_dir.resolve()
    figure_dir = (args.figure_dir or output_dir / "figures").resolve()
    for directory in (output_dir, figure_dir):
        if any(directory.is_relative_to(source) or source.is_relative_to(directory)
               for source in (raw_dir, dataset_dir, runs_dir)):
            raise ValueError("output must be separate from input directories")
    manifest = read_json(dataset_dir / "manifest.json")
    cfg = prep.PreprocessConfig(**manifest["config"])
    entries = {row["session_id"]: row for row in manifest["sessions"] if row["used"]}
    with np.load(dataset_dir / "normalization.npz") as norm:
        mean, std_safe = norm["mean"].copy(), norm["std_safe"].copy()
        active = norm["std"] >= cfg.zero_std_epsilon
    arrays = {split: np.load(dataset_dir / split / "X.npy", mmap_mode="r") for split in prep.SPLIT_ORDER}
    labels = {split: np.load(dataset_dir / split / "y.npy", mmap_mode="r") for split in prep.SPLIT_ORDER}
    metadata = {split: read_jsonl(dataset_dir / split / "windows.jsonl") for split in prep.SPLIT_ORDER}
    for split in prep.SPLIT_ORDER:
        if len(arrays[split]) != len(labels[split]) or len(arrays[split]) != len(metadata[split]):
            raise ValueError(f"split lengths disagree: {split}")
    sessions, raw_records, provenance = [], {}, []
    for sid, entry in sorted(entries.items()):
        split = entry["split"]
        reconstructed = prep.process_session(raw_dir / f"session_{sid}", sid, entry["label"], split, cfg)
        if reconstructed["manifest"] != entry:
            raise ValueError(f"reconstructed manifest differs: session {sid}")
        combined = reconstructed["combined"]
        starts = reconstructed["valid_starts"]
        window_rows = [row for row in metadata[split] if row["session_id"] == sid]
        if len(window_rows) != len(starts):
            raise ValueError(f"window count mismatch: session {sid}")
        for row, start in zip(window_rows, starts):
            expected = combined[start:start + cfg.window]
            if row["window_start_tx_seq"] != entry["common_start"] + start:
                raise ValueError("window start mismatch")
            if labels[split][row["index"]] != entry["label_id"] or row["label_id"] != entry["label_id"]:
                raise ValueError("saved label mismatch")
            if row["rx_order"] != list(cfg.rx_order):
                raise ValueError("RX order mismatch")
            if not np.array_equal(arrays[split][row["index"]], expected):
                raise ValueError(f"raw-to-saved X mismatch: session {sid}, window {row['index']}")
        covered = coverage_mask(len(combined), starts, cfg.window)
        values = combined[covered].astype(np.float64)
        if not np.isfinite(values).all():
            raise ValueError("nonfinite covered features")
        feature_mean, feature_std = values.mean(axis=0), values.std(axis=0)
        series = []
        for start in range(0, len(combined), 100):
            part = combined[start:start + 100][covered[start:start + 100]]
            if len(part):
                avg = part.mean(axis=0, dtype=np.float64)
                series.append({"seconds": (start + len(part) / 2) / 100,
                               "rx_mean": [float(avg[r * 64:(r + 1) * 64][active[r * 64:(r + 1) * 64]].mean()) for r in range(3)]})
        rx_summary = {}
        for r, rx in enumerate(cfg.rx_order):
            idx = np.arange(r * 64, (r + 1) * 64)[active[r * 64:(r + 1) * 64]]
            rx_summary[str(rx)] = {
                "active_features": int(len(idx)), "amplitude_mean": float(feature_mean[idx].mean()),
                "mean_temporal_std": float(feature_std[idx].mean()),
                "normalized_mean": float(((feature_mean[idx] - mean[idx]) / std_safe[idx]).mean()),
                "normalized_temporal_std": float((feature_std[idx] / std_safe[idx]).mean()),
                "first_60s_mean": float(combined[:6000, idx][covered[:6000]].mean()),
                "last_60s_mean": float(combined[-6000:, idx][covered[-6000:]].mean()),
            }
        sessions.append({"session_id": sid, "label": entry["label"], "split": split,
                         "unique_covered_frames": int(covered.sum()), "window_count": len(starts),
                         "raw_mean": feature_mean, "raw_temporal_std": feature_std,
                         "normalized_mean": (feature_mean - mean) / std_safe,
                         "normalized_temporal_std": feature_std / std_safe,
                         "rx_summary": rx_summary, "time_series": series, "quality": entry})
        for rx in cfg.rx_order:
            source = raw_dir / f"session_{sid}" / f"device_{rx}.jsonl"
            provenance.append({"path": relative(source), "sha256": sha256(source)})
        if sid in FOCUS:
            raw_records[str(sid)] = {
                str(rx): raw_audit(raw_dir, entry, rx, active[r * 64:(r + 1) * 64], ROOT / "log")
                for r, rx in enumerate(cfg.rx_order)
            }
        print(f"session {sid:2d}: {len(starts)} windows reproduced exactly; {covered.sum()} unique frames", flush=True)
    predictions = prediction_audit(runs_dir, dataset_dir, entries)
    comparisons = compare_to_train(sessions, active)
    report = {
        "generated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        "environment_snapshots_used": False, "ground_truth_status": "dataset-assigned, not independently verified",
        "methods": {"frame_weighting": "unique covered frames; includes official short-gap interpolation",
                    "profile_distance": "RMS difference of session means after saved train standardization, active features only",
                    "active_features": int(active.sum()), "constant_train_features": int((~active).sum()),
                    "time_axis": "tx_seq offset / assumed 100 Hz", "training_performed": False},
        "checks": {"reproduced_sessions": len(sessions), "exactly_reproduced_windows": sum(row["window_count"] for row in sessions),
                   "prediction_windows_checked": len(predictions) * len(metadata["test"]),
                   "raw_to_X_exact": True, "manifest_reproduction_exact": True,
                   "labels_and_prediction_alignment": True},
        "provenance": {"script_sha256": sha256(Path(__file__)), "numpy_version": np.__version__,
                       "preprocessor_sha256": sha256(Path(prep.__file__)),
                       "manifest_sha256": sha256(dataset_dir / "manifest.json"),
                       "normalization_sha256": sha256(dataset_dir / "normalization.npz"), "raw_files": provenance},
        "sessions": sessions, "raw_record_audits": raw_records,
        "train_profile_comparisons": comparisons, "saved_predictions": predictions,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "audit.json").write_text(json.dumps(report, indent=2, default=json_default, allow_nan=False) + "\n", encoding="utf-8")
    figures(sessions, raw_records, predictions, active, figure_dir)
    # Keep a small reviewable evidence file beside the figures; full traces stay local.
    compact = {key: report[key] for key in ("generated_at", "environment_snapshots_used", "ground_truth_status", "methods", "checks", "provenance", "train_profile_comparisons")}
    compact["sessions"] = [{key: row[key] for key in ("session_id", "label", "split", "unique_covered_frames", "window_count", "rx_summary")} for row in sessions]
    compact["raw_record_audits"] = {
        sid: {rx: {key: value for key, value in row.items() if key != "time_series"} for rx, row in receivers.items()}
        for sid, receivers in raw_records.items()
    }
    compact["saved_predictions"] = [
        {**{key: run[key] for key in ("seed", "path", "sha256")},
         "sessions": [{key: value for key, value in row.items() if key not in ("seconds", "probabilities")} for row in run["sessions"]]}
        for run in predictions
    ]
    (figure_dir / "summary.json").write_text(json.dumps(compact, indent=2, default=json_default, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report["checks"], indent=2), flush=True)
    for row in comparisons:
        print(f"session {row['session_id']} nearest train profiles: {row['train_distances'][:3]}", flush=True)


if __name__ == "__main__":
    main()
