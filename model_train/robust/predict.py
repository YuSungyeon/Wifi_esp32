#!/usr/bin/env python3
"""Offline inference from aligned amplitude/phase51 arrays or a raw CSI session."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.robust.shared_temporal import SharedEncoder, amplitude_input, CONFIG
from model_train.robust.run_experiment import DEFAULT_OUTPUT, save_json


def load_raw_amplitude(folder):
    # Reuse the audited CRC, reboot/corruption handling and tx_seq alignment.
    # Only receiver identities are consumed from session.json, never its label.
    from model_train.analysis.prepare_phase_experiment import checked_frames, select_common, CFG
    from model_train.preprocessing import preprocess_3rx as pre, phase_features as phase
    from scripts import csi_store as cs
    manifest = json.loads((folder/"session.json").read_text())
    ids = {int(d["device_id"]): int(d["rx_id"]) for d in manifest["devices"]}
    frames = {rx: checked_frames(folder/f"device_{rx}.csi", ids[rx]) for rx in (101, 102, 103)}
    for f in frames.values():
        h = f["hdr"]
        if not ((h["channel"] == 11) & (h["rate"] == 11) & (h["sig_len"] == 47)).all():
            raise ValueError("RF profile differs from training channel/rate/packet length")
    chosen, quality, _ = select_common(frames)
    start, length = chosen["common_start"], chosen["common_length"]
    amp = np.full((3, length, 51), np.nan, np.float32)
    present = np.zeros((3, length), bool)
    for r, rx in enumerate((101, 102, 103)):
        records, seen = [], set()
        for rec in chosen["segment_records"][rx]:
            if start <= rec.tx_seq <= chosen["common_end"] and rec.tx_seq not in seen:
                records.append(rec)
                seen.add(rec.tx_seq)
        indices = np.asarray([rec.line_no-1 for rec in records])
        f = frames[rx][indices]
        grid = f["hdr"]["tx_seq"].astype(np.int64)-start
        amp[r, grid] = np.abs(cs.complex_csi(f, valid_only=False)[:, phase.RAW_INDICES])
        present[r, grid] = True
    pre.interpolate_short_gaps(amp, present, CFG)
    return amp.transpose(1, 0, 2).reshape(length, 153), {
        "common_start_tx_seq": int(start), "frames": length,
        "crc_checked_frames": sum(q["crc_checked_frames"] for q in quality.values()),
        "input": "raw .csi, manifest receiver identities only"}


def load_bundle(path):
    bundle = torch.load(path, map_location="cpu", weights_only=True)
    if (bundle["feature_version"] != CONFIG["version"] or bundle["mode"] != "A"
            or bundle["rx_order"] != [101, 102, 103] or bundle["window_frames"] != 3000):
        raise ValueError("unsupported model/input contract")
    models = []
    for state in bundle["state_dicts"]:
        model = SharedEncoder(mode="A")
        model.load_state_dict(state)
        model.eval()
        models.append(model)
    return bundle, models


def predict_amplitude(amp, bundle, models):
    amp = np.asarray(amp)
    if amp.ndim != 2 or amp.shape[1] != 153:
        raise ValueError("expected (T,153) in documented RX/tone order")
    w, stride = bundle["window_frames"], bundle["stride_frames"]
    starts = [i for i in range(0, len(amp)-w+1, stride) if np.isfinite(amp[i:i+w]).all()]
    if not starts:
        raise ValueError("no valid 30-second window; no prediction")
    predictions = []
    with torch.no_grad():
        for pos in range(0, len(starts), 32):
            sub = starts[pos:pos+32]
            x = torch.from_numpy(np.stack([amplitude_input(amp[i:i+w]) for i in sub]))
            p = torch.stack([m(x).softmax(1) for m in models]).mean(0).numpy()
            predictions.extend(p)
    scores = np.asarray(predictions)
    if not np.isfinite(scores).all():
        raise ValueError("non-finite inference")
    np.testing.assert_allclose(scores.sum(1), 1, atol=1e-6)
    avg = scores.mean(0)
    return {"status": bundle["status"], "prediction": bundle["class_names"][int(avg.argmax())],
            "mean_scores": dict(zip(bundle["class_names"], avg.tolist())),
            "window_seconds": w/100, "stride_seconds": stride/100, "valid_windows": len(starts),
            "candidate_windows": max(0, (len(amp)-w)//stride+1), "scores_are_calibrated": False,
            "windows": [{"start_frame": i, "end_frame_exclusive": i+w,
                         "prediction": bundle["class_names"][int(p.argmax())], "scores": p.tolist()}
                        for i,p in zip(starts, scores)]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    inp = p.add_mutually_exclusive_group(required=True)
    inp.add_argument("--features", type=Path, help="(T,459) phase51 or (T,153) amplitude .npy")
    inp.add_argument("--session-dir", type=Path, help="complete raw collection session with manifest and 3 .csi files")
    p.add_argument("--model", type=Path, default=Path(__file__).with_name("model.pt"),
                   help="model checkpoint (default: bundled robust/model.pt)")
    p.add_argument("--output", type=Path)
    p.add_argument("--details", action="store_true")
    args = p.parse_args()
    torch.set_num_threads(2)
    if args.features:
        x = np.load(args.features, mmap_mode="r", allow_pickle=False)
        if x.ndim != 2 or x.shape[1] not in (153, 459):
            p.error("features shape must be (T,153) or (T,459)")
        amp, quality = x[:, :153], {"input": "aligned amplitude; caller guarantees documented RX/tone order"}
    else:
        amp, quality = load_raw_amplitude(args.session_dir)
    bundle, models = load_bundle(args.model)
    result = predict_amplitude(amp, bundle, models)
    result["quality"] = quality
    if args.output:
        save_json(args.output, result)
    print(json.dumps(result if args.details else {k:v for k,v in result.items() if k != "windows"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
