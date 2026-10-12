#!/usr/bin/env python3
"""10/6 원본의 진폭·위상 순차 재생과 전체 파일 재생 비교 (학습 없음)."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import csi_store as cs
from model_train.analysis.prepare_phase_experiment import checked_frames
from model_train.preprocessing import preprocess_3rx as pre, phase_features as old_phase
from model_train.phase60 import features as P
from model_train.phase60.evaluate_raw import validate_bundle
from model_train.cnn1d.shared_cnn import SharedTemporalCNN
from model_train.robust import predict as A
from model_train.robust.shared_temporal import amplitude_input
from model_train.robust.run_experiment import digest, save_json

SESSION = ROOT / "mac_collector_output/realtime/20261005-235852-cba933f4/capture"
CLASSES = ("empty", "static", "motion")
STRIDE = 30
ACTION_ROWS = (
    ("00:05:05", "empty", "첫 빈 방"),
    ("00:07:09", "motion", "첫 걷기"),
    ("00:08:13", "static", "앉아서 정지(근사)"),
    ("00:11:14", "motion", "두 번째 걷기"),
    ("00:12:22", "empty", "마지막 빈 방"),
    ("00:15:25", None, "평가 종료"),
)


def truth_segments():
    rows = [(datetime.fromisoformat(f"2026-10-06T{clock}").replace(
        tzinfo=ZoneInfo("Asia/Seoul")).timestamp(), label, name) for clock, label, name in ACTION_ROWS]
    return [{"start_wall": start, "end_wall": rows[i+1][0], "label": label, "name": name}
            for i, (start, label, name) in enumerate(rows[:-1])]


def fill_ready(values, present):
    """5-frame 内部 보간과 값마다 필요한 오른쪽 관측 프레임 위치."""
    filled = values.copy()
    interpolated = pre.interpolate_short_gaps(filled, present, pre.DEFAULT_CONFIG)
    ready = np.where(present, np.arange(present.shape[1])[None], present.shape[1]+1)
    for r in range(len(present)):
        obs = np.flatnonzero(present[r])
        for left, right in zip(obs[:-1], obs[1:]):
            if 0 < right-left-1 <= 5:
                ready[r, left+1:right] = right
    return filled, ready, interpolated


def common_end(observed, cutoff):
    """미래 프레임 없이 각 RX의 마지막 실제 프레임을 찾는다 (끝은 exclusive)."""
    last = []
    for obs in observed:
        idx = np.searchsorted(obs, cutoff, side="left")-1
        if idx < 0:
            return 0
        last.append(int(obs[idx])+1)
    return min(last)


def make_schedule(present, ready, filled_mask, width, causal):
    length = present.shape[1]
    observations = [np.flatnonzero(row) for row in present]
    unfilled = ~(present | filled_mask)
    prefix = np.r_[0, np.cumsum(unfilled.any(0))]
    rows, last_valid = [], None
    for cutoff in range(0, length+1, STRIDE):
        end = common_end(observations, cutoff) if causal else cutoff
        row = {"t": cutoff/100, "frame_cutoff_exclusive": cutoff,
               "input_end_frame_exclusive": end, "input_start_frame": max(0, end-width),
               "state": "pending", "reason": None, "infer": False}
        if end < width:
            row.update(state="deciding", reason="warmup")
        elif causal and last_valid is not None and 0 <= end-last_valid < STRIDE:
            row.update(state="hold", reason="waiting_for_30_new_common_frames")
        else:
            start = end-width
            bad = int(prefix[end]-prefix[start])
            pending = (ready[:, start:end] >= end).any(0) if causal else np.zeros(width, bool)
            # A long unfillable gap is already counted separately.
            waiting = int((pending & ~unfilled[:, start:end].any(0)).sum())
            if bad or waiting:
                row.update(state="suspended", reason="unfilled_gap" if bad else "boundary_interpolation_not_observed",
                           unfilled_frames=bad, pending_boundary_frames=waiting)
            else:
                row["infer"] = True
                last_valid = end
        rows.append(row)
    return rows


def compact(rows, anchor):
    intervals = []
    for row in rows:
        wall = anchor+row["t"]
        if intervals and intervals[-1]["state"] == row["state"]:
            continue
        intervals.append({"wall": wall, "state": row["state"]})
    return intervals


def segment_metrics(intervals, begin, end, label, recording_end):
    durations = {s: 0.0 for s in (*CLASSES, "suspended", "deciding")}
    first = stable = None
    for i, item in enumerate(intervals):
        stop = intervals[i+1]["wall"] if i+1 < len(intervals) else recording_end
        a, b = max(begin, item["wall"]), min(end, stop)
        if b <= a:
            continue
        durations[item["state"]] += b-a
        if item["state"] == label:
            if first is None:
                first = a-begin
            if stable is None and b-a >= 10-1e-9:
                stable = a-begin
    total = end-begin
    np.testing.assert_allclose(sum(durations.values()), total, atol=1e-6)
    valid = sum(durations[s] for s in CLASSES)
    return {"duration_s": total, "correct_s": durations[label], "incorrect_s": valid-durations[label],
            "unavailable_s": durations["suspended"]+durations["deciding"],
            "valid_s": valid, "match_among_valid": durations[label]/valid if valid else None,
            "match_among_all": durations[label]/total if total else None,
            "first_correct_delay_s": first, "first_10s_stable_start_delay_s": stable,
            "durations_by_state_s": durations}


def analyze(rows, anchor, duration, trim=0, shift=0):
    intervals = compact(rows, anchor+shift)
    end_recording = anchor+duration+shift
    segments = []
    for action in truth_segments():
        start = action["start_wall"]+trim
        if start >= action["end_wall"]:
            continue
        segments.append({**action, "evaluation_start_wall": start,
                         **segment_metrics(intervals, start, action["end_wall"], action["label"], end_recording)})
    correct = sum(s["correct_s"] for s in segments)
    valid = sum(s["valid_s"] for s in segments)
    total = sum(s["duration_s"] for s in segments)
    return {"segments": segments, "overall": {"duration_s": total, "correct_s": correct,
            "incorrect_s": valid-correct, "unavailable_s": total-valid, "valid_s": valid,
            "match_among_valid": correct/valid if valid else None, "match_among_all": correct/total},
            "excluded_seconds_after_each_action": trim, "time_axis_shift_s": shift}


def load_grids(session):
    manifest = json.loads((session/"session.json").read_text())
    if manifest.get("label") is not None or manifest.get("purpose") != "realtime_monitor":
        raise ValueError("expected original unlabeled realtime capture")
    devices = {int(d["device_id"]): d for d in manifest["devices"]}
    if sorted(devices) != [101, 102, 103]:
        raise ValueError("expected three receivers")
    frames, quality = {}, []
    for rx in P.RX_ORDER:
        f = checked_frames(session/f"device_{rx}.csi", int(devices[rx]["rx_id"]))
        h = f["hdr"]; tx = h["tx_seq"].astype(np.int64); delta = np.diff(tx)
        if ((delta < 0).any() or len(np.unique(h["boot_id"])) != 1
                or not ((h["channel"] == 11) & (h["rate"] == 11) & (h["sig_len"] == 47)).all()
                or len(h) != devices[rx]["frames"]):
            raise ValueError(f"invalid profile/reset/sequence/frame count RX{rx}")
        frames[rx] = f
        quality.append({"rx": rx, "frames": len(f), "duplicates": int((delta == 0).sum()),
                        "max_gap_frames": int(np.maximum(delta-1, 0).max()),
                        "long_gaps": [{"left_tx_seq": int(tx[i]), "right_tx_seq": int(tx[i+1]),
                                       "missing_frames": int(delta[i]-1)} for i in np.flatnonzero(delta > 6)]})
    start = max(int(f["hdr"]["tx_seq"][0]) for f in frames.values())
    end = min(int(f["hdr"]["tx_seq"][-1]) for f in frames.values())
    length = end-start+1
    present = np.zeros((3, length), bool)
    amp = np.full((3, length, 51), np.nan, np.float32)
    real = np.full((3, length, 52), np.nan, np.float32); imag = real.copy()
    for ri, rx in enumerate(P.RX_ORDER):
        f = frames[rx]; grid = f["hdr"]["tx_seq"].astype(np.int64)-start
        keep = np.flatnonzero((grid >= 0) & (grid < length))
        idx, first = np.unique(grid[keep], return_index=True)
        z = cs.complex_csi(f[keep[first]], valid_only=False)
        amp[ri, idx] = np.abs(z[:, old_phase.RAW_INDICES])
        real[ri, idx] = z[:, P.RAW_TONE_ORDER].real
        imag[ri, idx] = z[:, P.RAW_TONE_ORDER].imag
        present[ri, idx] = True
    amp_full, ready, interpolated = fill_ready(amp, present)
    real_full, phase_ready, phase_interpolated = fill_ready(real, present)
    imag_full, _, _ = fill_ready(imag, present)
    np.testing.assert_array_equal(ready, phase_ready)
    np.testing.assert_array_equal(interpolated, phase_interpolated)
    phase = P.phase_residual(real_full+1j*imag_full).transpose(1, 0, 2).reshape(length, 156).astype(np.float32)
    amp_full = amp_full.transpose(1, 0, 2).reshape(length, 153)
    # Compare complete-file inputs against the already established inference paths.
    reference, q = A.load_raw_amplitude(session, min_common_length=3000)
    if q["common_start_tx_seq"] != start:
        raise ValueError("amplitude common interval differs")
    np.testing.assert_allclose(amp_full, reference, rtol=0, atol=0, equal_nan=True)
    np.testing.assert_allclose(phase, P.session_phase(session), rtol=0, atol=0, equal_nan=True)
    return manifest, start, present, ready, interpolated, amp, real, imag, amp_full, phase, quality


def verify_prefix_inputs(rows, present, amp, real, imag, full, mode, width):
    interesting = [r for r in rows if r["infer"] or r["state"] == "suspended"]
    picks = np.linspace(0, len(interesting)-1, 12, dtype=int)
    verified = 0
    for index in picks:
        row = interesting[index]; end = row["input_end_frame_exclusive"]; start = end-width
        lo = max(0, start-6)
        mask = present[:, lo:end]
        if mode == "amplitude":
            direct = amp[:, lo:end].copy()
            pre.interpolate_short_gaps(direct, mask, pre.DEFAULT_CONFIG)
            direct = direct.transpose(1, 0, 2).reshape(end-lo, 153)[start-lo:]
            reference = full[start:end]
        else:
            a, b = real[:, lo:end].copy(), imag[:, lo:end].copy()
            pre.interpolate_short_gaps(a, mask, pre.DEFAULT_CONFIG)
            pre.interpolate_short_gaps(b, mask, pre.DEFAULT_CONFIG)
            direct = P.phase_residual(a+1j*b).transpose(1, 0, 2).reshape(end-lo, 156).astype(np.float32)[start-lo:]
            reference = full[start:end]
        valid = bool(np.isfinite(direct).all())
        if valid != bool(row["infer"]):
            raise ValueError(f"prefix validity mismatch: {mode}, {end}")
        if valid:
            np.testing.assert_allclose(direct, reference, atol=0, rtol=0)
        verified += 1
    return verified


def infer_inputs(full, starts, models, mode, batch=8):
    scores, timings = {}, []
    for pos in range(0, len(starts), batch):
        sub = starts[pos:pos+batch]; began = time.perf_counter()
        if mode == "amplitude":
            values = np.stack([amplitude_input(full[s:s+3000]) for s in sub])
        else:
            values = P.windows(full, np.asarray(sub), 6000, 20)
        if not np.isfinite(values).all():
            raise ValueError("invalid inference input")
        with torch.inference_mode():
            each = torch.stack([m(torch.from_numpy(np.ascontiguousarray(values))).softmax(1) for m in models]).numpy()
        mean = each.mean(0)
        if not np.isfinite(mean).all():
            raise ValueError("nonfinite scores")
        np.testing.assert_allclose(mean.sum(1), 1, atol=1e-6)
        timings.append((time.perf_counter()-began)/len(sub))
        for i, s in enumerate(sub):
            scores[s] = {"scores": mean[i].tolist(), "seed_scores": each[:, i].tolist()}
        if pos % 400 == 0:
            print(f"{mode}: inferred {min(pos+batch,len(starts))}/{len(starts)} unique windows", flush=True)
    return scores, {"batch_size": batch, "median_batch_amortized_seconds_per_window": float(np.median(timings))}


def finalize_rows(rows, predictions, anchor, tx0, output):
    state, previous_scores = "deciding", None
    with output.open("w", encoding="utf-8") as fp:
        for row in rows:
            if row["infer"]:
                row.update(predictions[row["input_start_frame"]])
                row["state"] = CLASSES[int(np.argmax(row["scores"]))]
                previous_scores = row["scores"]
            elif row["state"] == "hold":
                row["state"] = state
                row["scores"] = previous_scores if state in CLASSES else None
            else:
                row["scores"] = None
                previous_scores = None
            state = row["state"]
            row.update(wall=anchor+row["t"], input_start_tx_seq=tx0+row["input_start_frame"],
                       input_end_tx_seq_exclusive=tx0+row["input_end_frame_exclusive"])
            fp.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n")


def run(output):
    if output.resolve() == SESSION.resolve() or SESSION.resolve() in output.resolve().parents:
        raise ValueError("output must be separate from capture")
    output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    model_paths = {"amplitude": ROOT/"model_train/robust/model.pt",
                   "phase": ROOT/"model_train/phase60/model_60sessions.pt"}
    source_paths = [Path(__file__), ROOT/"model_train/phase60/features.py",
                    ROOT/"model_train/robust/shared_temporal.py", ROOT/"model_train/robust/predict.py",
                    ROOT/"model_train/preprocessing/preprocess_3rx.py", ROOT/"scripts/csi_store.py",
                    ROOT/"model_train/cnn1d/shared_cnn.py", ROOT/"model_train/phase60/evaluate_raw.py",
                    ROOT/"model_train/analysis/prepare_phase_experiment.py"]
    preserved = [*SESSION.glob("*"), *model_paths.values(),
                 *(SESSION.parent/"prediction").glob("*"), ROOT/"mac_collector/session_meta.yaml",
                 *(ROOT/"model_train/robust").glob("*.py")]
    hashes = {str(p.relative_to(ROOT)): digest(p) for p in set(source_paths+preserved) if p.is_file()}
    save_json(output/"input-hashes.json", hashes)
    m, tx0, present, ready, interp, raw_amp, real, imag, amp, phase, quality = load_grids(SESSION)
    anchor = m["started_at_unix_us"]/1e6
    duration = present.shape[1]/100
    save_json(output/"truth.json", {"source": "user supplied action times, original 10/6 report",
        "timezone": "Asia/Seoul", "segments": truth_segments(), "static_onset_is_sitting_start": True})
    bundles, models = {}, {}
    bundles["amplitude"], models["amplitude"] = A.load_bundle(model_paths["amplitude"])
    bundles["phase"] = torch.load(model_paths["phase"], map_location="cpu", weights_only=True)
    validate_bundle(bundles["phase"])
    models["phase"] = []
    for state in bundles["phase"]["state_dicts"]:
        model = SharedTemporalCNN(groups=(156,), n_classes=3)
        model.load_state_dict(state)
        models["phase"].append(model.eval())
    save_json(output/"protocol.json", {"started_at_utc": datetime.now(timezone.utc).isoformat(),
        "session": str(SESSION.relative_to(ROOT)), "anchor_wall": anchor, "common_start_tx_seq": tx0,
        "common_length_frames": present.shape[1], "stride_frames": STRIDE,
        "time_mapping": "manifest start + (exclusive TX cutoff - first common TX) / 100; approximate, no per-frame host arrival clock",
        "causal_policy": "TX-prefix common-end + 30-new-frame gate; interpolation right endpoint must precede common-end",
        "model_hashes": {k:digest(p) for k,p in model_paths.items()}, "quality": quality,
        "runtime": {"python": sys.version, "numpy": np.__version__, "torch": str(torch.__version__), "device": "cpu"},
        "training_performed": False, "confidence_gate": False, "hold_seconds_gate": False,
        "limitations": ["No per-frame host arrival times; wireless, reader, polling and browser latency not reconstructed.",
                        "One person, one room, one transition run; previously inspected development data."]})
    results, checks = {}, {}
    for mode, data, width in (("amplitude", amp, 3000), ("phase", phase, 6000)):
        schedules = {"prefix": make_schedule(present, ready, interp, width, True),
                     "whole_file": make_schedule(present, ready, interp, width, False)}
        checks[mode] = {"prefix_direct_input_checks": verify_prefix_inputs(
            schedules["prefix"], present, raw_amp, real, imag, data, mode, width)}
        starts = sorted({row["input_start_frame"] for rows in schedules.values() for row in rows if row["infer"]})
        predictions, timing = infer_inputs(data, starts, models[mode], mode)
        if mode == "amplitude":
            for start in (starts[0], starts[len(starts)//2], starts[-1]):
                np.testing.assert_allclose(predictions[start]["scores"],
                    A.predict_window(data[start:start+width], bundles[mode], models[mode]), rtol=1e-5, atol=1e-6)
        else:
            for start in (starts[0], starts[len(starts)//2], starts[-1]):
                x=torch.from_numpy(P.windows(data,np.array([start]),6000,20))
                with torch.inference_mode():
                    ref=torch.stack([model(x).softmax(1) for model in models[mode]]).mean(0)[0].numpy()
                np.testing.assert_allclose(predictions[start]["scores"],ref,rtol=1e-5,atol=1e-6)
        results[mode] = {"window_frames": width, "unique_inferred_windows": len(starts), "timing": timing}
        for kind, rows in schedules.items():
            finalize_rows(rows, predictions, anchor, tx0, output/f"{mode}-{kind}.jsonl")
            results[mode][kind] = {"metrics": analyze(rows,anchor,duration),
                "after60s": analyze(rows,anchor,duration,trim=60),
                "time_shift_sensitivity": {str(shift): analyze(rows,anchor,duration,shift=shift) for shift in (-1,1)},
                "inferences": sum(row["infer"] for row in rows),
                "holds": sum(row["reason"] == "waiting_for_30_new_common_frames" for row in rows),
                "suspended_polls": sum(row["state"] == "suspended" for row in rows),
                "reason_counts": {reason: sum(row["reason"]==reason for row in rows) for reason in
                    ("unfilled_gap", "boundary_interpolation_not_observed", "waiting_for_30_new_common_frames", "warmup")}}
            save_json(output/f"{mode}-{kind}-events.json",compact(rows,anchor))
        for model, initial in zip(models[mode], bundles[mode]["state_dicts"]):
            for name,tensor in model.state_dict().items():
                torch.testing.assert_close(tensor,initial[name],rtol=0,atol=0)
        print(mode,json.dumps(results[mode]["prefix"]["metrics"]["overall"],ensure_ascii=False),flush=True)
    live_events = [json.loads(line) for line in (SESSION.parent/"prediction/realtime_events.jsonl").read_text().splitlines()]
    original_rows = [{"t": 0.0, "state": "deciding"}]+[{"t": ev["wall"]-anchor,"state":ev["to"]} for ev in live_events]
    original = analyze(original_rows,anchor,(m["ended_at_unix_us"]/1e6)-anchor)
    for name,sha in hashes.items():
        if digest(ROOT/name) != sha:
            raise ValueError(f"input/source changed: {name}")
    summary = {"completed_at_utc": datetime.now(timezone.utc).isoformat(),"models":results,
               "original_live_amplitude": original,"quality":quality,
               "checks": {"hashes_unchanged": True,"verified_files":len(hashes),
                          "model_state_unchanged":True,"complete_file_inputs_match_official_paths":True,
                          "batch_matches_single_window":True,**checks}}
    save_json(output/"summary.json",summary)
    print("COMPLETED",output,flush=True)
    return summary


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path,required=True)
    run(parser.parse_args().output)
