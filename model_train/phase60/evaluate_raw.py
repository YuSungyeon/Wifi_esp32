#!/usr/bin/env python3
"""고정된 main 위상 모델을 원본 9/16·17 자료에서 재평가한다 (학습 없음)."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import subprocess
import sys

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.analysis.prepare_phase_experiment import checked_frames
from model_train.cnn1d.shared_cnn import SharedTemporalCNN
from model_train.phase60 import features as F
from model_train.phase60.predict import predict_phase
from model_train.robust.run_experiment import digest, save_json, metrics

SOURCE_COMMIT = "01c9f1929b170d21fe9a570bd37c821127521aeb"
CLASSES = ["empty", "static", "motion"]
DATES = ("20260916", "20260917")


def validate_bundle(bundle):
    expected = dict(format_version=1, feature_version=F.FEATURE_VERSION, mode="P",
                    window_frames=6000, stride_frames=300, downsample=20,
                    sample_rate_hz=100, rx_order=list(F.RX_ORDER),
                    raw_tone_order=F.RAW_TONE_ORDER.tolist(), class_names=CLASSES,
                    training_dates=["20260919", "20260920"], evaluation_dates=list(DATES))
    for key, value in expected.items():
        if bundle.get(key) != value:
            raise ValueError(f"unexpected checkpoint contract: {key}")
    if len(bundle.get("state_dicts", [])) != 3 or bundle.get("config", {}).get("seeds") != [0, 1, 2]:
        raise ValueError("expected three seeds in order 0, 1, 2")
    keys = bundle.get("training_sessions", [])
    if len(keys) != 60 or len(set(keys)) != 60:
        raise ValueError("expected 60 distinct training sessions")


def validate_identity(folder, manifest, training_keys):
    date, sid, label = folder.parent.name, manifest["session_id"], manifest["label"]
    key = f"{date}_{sid}"
    if date not in DATES or key in training_keys:
        raise ValueError(f"unexpected or training session: {key}")
    if label not in CLASSES or not folder.name.endswith(f"_{label}_s{sid}"):
        raise ValueError(f"manifest/folder label or identity mismatch: {folder}")
    ids = [int(d["device_id"]) for d in manifest["devices"]]
    if sorted(ids) != list(F.RX_ORDER):
        raise ValueError(f"expected exactly three receivers: {key}")
    return key


def aggregate(rows):
    if not rows:
        raise ValueError("no evaluated sessions")
    ys, ps, yw, pw, first, fractions = [], [], [], [], [], []
    for row in rows:
        scores = np.asarray([w["scores"] for w in row["windows"]], dtype=np.float32)
        if scores.ndim != 2 or scores.shape[1] != 3 or not np.isfinite(scores).all():
            raise ValueError("invalid window scores")
        np.testing.assert_allclose(scores.sum(1), 1, atol=1e-6)
        y = CLASSES.index(row["label"])
        pred = int(scores.mean(0).argmax())
        if CLASSES[pred] != row["prediction"]:
            raise ValueError("session prediction must use mean probabilities")
        ys.append(y); ps.append(pred); first.append(int(scores[0].argmax()))
        yw.extend([y] * len(scores)); pw.extend(scores.argmax(1).tolist())
        fractions.append(float((scores.argmax(1) == y).mean()))
    return {"session": metrics(ys, ps), "window": metrics(yw, pw),
            "first_valid_window": metrics(ys, first),
            "mean_session_window_accuracy": float(np.mean(fractions)),
            "main_rounded_mean_session_window_accuracy": round(float(np.mean([
                round(x, 4) for x in fractions])), 4)}


def verify_hashes(hashes):
    for name, expected in hashes.items():
        if digest(ROOT / name) != expected:
            raise ValueError(f"file changed: {name}")


def audit_frames(folder, manifest):
    devices, frames = [], {}
    for device in manifest["devices"]:
        rx = int(device["device_id"])
        frames[rx] = checked_frames(folder / f"device_{rx}.csi", int(device["rx_id"]))
        h = frames[rx]["hdr"]
        delta = np.diff(h["tx_seq"].astype(np.int64))
        if ((delta < 0).any() or len(np.unique(h["boot_id"])) != 1
                or not ((h["channel"] == 11) & (h["rate"] == 11) & (h["sig_len"] == 47)).all()
                or len(h) != device["frames"]):
            raise ValueError(f"RF, reset, sequence or frame-count error: {folder} RX{rx}")
        devices.append({"rx": rx, "frames": len(h), "crc_header_rf_identity_valid": True,
                        "boot_changes": 0, "tx_decreases": 0,
                        "duplicates": int((delta == 0).sum()),
                        "missing_frames": int(np.maximum(delta - 1, 0).sum()),
                        "max_gap_frames": int(np.maximum(delta - 1, 0).max())})
    if {p.name for p in folder.glob('device_*.csi')} != {f'device_{rx}.csi' for rx in F.RX_ORDER}:
        raise ValueError("unexpected raw receiver file")
    start = max(int(f["hdr"]["tx_seq"][0]) for f in frames.values())
    end = min(int(f["hdr"]["tx_seq"][-1]) for f in frames.values())
    length = end - start + 1
    if length < F.MIN_COMMON:
        raise ValueError("insufficient common length")
    for d in devices:
        tx = frames[d["rx"]]["hdr"]["tx_seq"].astype(np.int64)
        d["observed_ratio"] = len(np.unique(tx[(tx >= start) & (tx <= end)])) / length
    return {"common_start_tx_seq": start, "common_length_frames": length, "devices": devices}


def evaluate(output):
    output.mkdir(parents=True, exist_ok=False)
    torch.set_num_threads(2)
    model_path = ROOT / "model_train/phase60/model_60sessions.pt"
    bundle = torch.load(model_path, map_location="cpu", weights_only=True)
    validate_bundle(bundle)
    models = []
    for state in bundle["state_dicts"]:
        model = SharedTemporalCNN(groups=(156,), n_classes=3)
        model.load_state_dict(state)
        models.append(model.eval())

    hashes = {}
    def track(path):
        name = str(path.relative_to(ROOT))
        sha = digest(path)
        hashes[name] = sha
        return sha

    # Preserve every pre-existing amplitude source and its trained checkpoint.
    for p in (ROOT / "model_train/robust").iterdir():
        if p.is_file():
            track(p)
    track(ROOT / "mac_collector/session_meta.yaml")
    imported = subprocess.check_output([
        "git", "ls-tree", "-r", "--name-only", SOURCE_COMMIT,
        "model_train/phase60", "model_train/cnn1d/shared_cnn.py"], cwd=ROOT, text=True).splitlines()
    for name in imported:
        p = ROOT / name
        # Local README links/status can be updated; numerical code and weights stay exact.
        if p.suffix != ".md" and p.read_bytes() != subprocess.check_output(["git", "show", f"{SOURCE_COMMIT}:{name}"], cwd=ROOT):
            raise ValueError(f"import differs from main: {name}")
        track(p)
    for name in ("model_train/phase60/evaluate_raw.py", "scripts/csi_store.py",
                 "model_train/preprocessing/preprocess_3rx.py",
                 "model_train/analysis/prepare_phase_experiment.py"):
        track(ROOT / name)

    inventory_path = ROOT / "model_train/analysis/output/20260924-phase-ablation/inventory.json"
    track(inventory_path)
    inventory = json.loads(inventory_path.read_text())
    if {f"{r['date']}_{r['session_id']}" for r in inventory} != set(bundle["training_sessions"]):
        raise ValueError("historical training inventory keys differ")
    training_hashes = set()
    for row in inventory:
        for f in row["files"]:
            p = ROOT / row["path"] / f"device_{f['rx']}.csi"
            sha = track(p)
            if sha != f["sha256"]:
                raise ValueError(f"training raw differs from historical inventory: {p}")
            training_hashes.add(sha)

    amplitude_path = ROOT / "model_train/docs/model-training/assets/20260916-17-evaluation/results.json"
    old_audit_path = amplitude_path.with_name("audit.json")
    track(amplitude_path); track(old_audit_path)
    amplitude = json.loads(amplitude_path.read_text())
    old_audit = {r["path"]: r for r in json.loads(old_audit_path.read_text())["rows"]}
    if amplitude["model_sha256"] != hashes["model_train/robust/model.pt"]:
        raise ValueError("amplitude baseline checkpoint mismatch")
    amplitude_rows = {f"{r['date']}_{r['session_id']}": r for r in amplitude["sessions"]}
    paths = sorted(p for date in DATES for p in (ROOT / "mac_collector_output/raw" / date).glob("*/session.json"))
    if Counter(p.parent.parent.name for p in paths) != {"20260916": 7, "20260917": 8}:
        raise ValueError("expected seven plus eight sessions")
    entries, keys = [], set()
    for p in paths:
        manifest = json.loads(p.read_text())
        key = validate_identity(p.parent, manifest, bundle["training_sessions"])
        if key in keys:
            raise ValueError("duplicate evaluation key")
        keys.add(key)
        prior = old_audit[str(p.parent.relative_to(ROOT))]
        if track(p) != prior["manifest_sha256"]:
            raise ValueError("evaluation manifest changed since amplitude audit")
        for d in prior["devices"]:
            sha = track(p.parent / f"device_{d['device_id']}.csi")
            if sha in training_hashes or sha != d["sha256"]:
                raise ValueError("evaluation raw overlaps training or changed since amplitude audit")
        entries.append((p.parent, manifest, key))
    if keys != set(amplitude_rows):
        raise ValueError("amplitude baseline session set differs")

    protocol = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
                "source_commit": SOURCE_COMMIT, "imported_code_and_weights_match_source": True,
                "model_sha256": digest(model_path), "model_metadata": {k: v for k, v in bundle.items() if k != "state_dicts"},
                "input_hashes": hashes, "training_key_overlap": [], "training_raw_hash_overlap": [],
                "training_hash_basis": "Current 180 raw files match historical September 24 inventory; phase checkpoint itself contains no training raw hashes.",
                "training_performed": False, "cache_used": False,
                "runtime": {"python": sys.version, "platform": platform.platform(),
                            "numpy": np.__version__, "torch": str(torch.__version__), "device": "cpu"}}
    save_json(output / "protocol.json", protocol)
    saved = {r["session"]: r for r in bundle["metrics"]["per_session"]}
    rows = []
    for folder, manifest, key in entries:
        quality = audit_frames(folder, manifest)
        phase = F.session_phase(folder)
        starts = F.valid_starts(phase, 6000, 300)
        # Independent window rejection check also rejects infinity, not only NaN.
        candidates = list(range(0, len(phase) - 6000 + 1, 300))
        finite = [s for s in candidates if np.isfinite(phase[s:s+6000]).all()]
        if starts.tolist() != finite or not finite or len(phase) != quality["common_length_frames"]:
            raise ValueError(f"invalid phase window enumeration: {key}")
        scores, individual = [], []
        with torch.inference_mode():
            for offset in range(0, len(starts), 32):
                inputs = F.windows(phase, starts[offset:offset+32], 6000, 20)
                if inputs.shape[1:] != (300, 156) or not np.isfinite(inputs).all():
                    raise ValueError("unexpected model input")
                x = torch.from_numpy(np.ascontiguousarray(inputs))
                each = torch.stack([m(x).softmax(1) for m in models])
                scores.append(each.mean(0).numpy())
                individual.append(each.numpy())
        probs = np.concatenate(scores)
        seed_probs = np.concatenate(individual, axis=1)
        np.testing.assert_allclose(seed_probs.mean(0), probs, rtol=1e-6, atol=1e-7)
        prediction = CLASSES[int(probs.mean(0).argmax())]
        reference = predict_phase(phase, bundle, models)
        if reference["prediction"] != prediction or reference["valid_windows"] != len(starts):
            raise ValueError("evaluation differs from imported predictor")
        np.testing.assert_allclose(list(reference["mean_scores"].values()), probs.mean(0), atol=5.1e-5, rtol=0)
        truth = manifest["label"]
        baseline = amplitude_rows[key]
        if baseline["label"] != truth:
            raise ValueError("baseline label mismatch")
        row = {"key": key, "date": folder.parent.name, "session_id": manifest["session_id"],
               "label": truth, "prediction": prediction, "scores": probs.mean(0).tolist(),
               "correct_windows": int((probs.argmax(1) == CLASSES.index(truth)).sum()),
               "valid_windows": len(starts), "candidate_windows": len(candidates),
               "rejected_window_start_frames": sorted(set(candidates)-set(finite)),
               "quality": quality, "main_record": saved[key],
               "main_prediction_matches": saved[key]["pred"] == prediction,
               "main_max_score_difference": float(np.max(np.abs(probs.mean(0)-np.array(list(saved[key]["scores"].values()))))),
               "amplitude_prediction": baseline["ensemble"]["prediction"],
               "windows": [{"start_frame": int(s), "end_frame_exclusive": int(s+6000),
                            "start_tx_seq": int(s+quality["common_start_tx_seq"]),
                            "end_tx_seq_exclusive": int(s+6000+quality["common_start_tx_seq"]),
                            "prediction": CLASSES[int(p.argmax())], "scores": p.tolist(),
                            "seed_scores": seed_probs[:, i].tolist()}
                           for i, (s, p) in enumerate(zip(starts, probs))]}
        rows.append(row)
        save_json(output / f"{key}.json", row)
        print(f"{key}: {truth} -> {prediction}; windows {row['correct_windows']}/{len(starts)}; rejected {len(candidates)-len(starts)}", flush=True)

    verify_hashes(hashes)
    for model, state in zip(models, bundle["state_dicts"]):
        for name, tensor in model.state_dict().items():
            torch.testing.assert_close(tensor, state[name], rtol=0, atol=0)
    result = {"completed_at_utc": datetime.now(timezone.utc).isoformat(),
              "source_commit": SOURCE_COMMIT, "model_sha256": protocol["model_sha256"],
              "metrics": aggregate(rows),
              "metrics_by_date": {d: aggregate([r for r in rows if r["date"] == d]) for d in DATES},
              "valid_windows": sum(r["valid_windows"] for r in rows),
              "candidate_windows": sum(r["candidate_windows"] for r in rows),
              "checks": {"input_hashes_unchanged": True, "verified_file_count": len(hashes),
                         "weights_and_batchnorm_unchanged": True, "ensemble_matches_seed_mean": True,
                         "matches_imported_predictor": True, "main_session_predictions_match": all(r["main_prediction_matches"] for r in rows)},
              "sessions": rows}
    save_json(output / "results.json", result)
    print(json.dumps({k: v for k, v in result.items() if k != "sessions"}, ensure_ascii=False, indent=2), flush=True)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new result directory")
    evaluate(parser.parse_args().output)
