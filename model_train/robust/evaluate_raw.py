#!/usr/bin/env python3
"""Evaluate the frozen three-model bundle on a previously audited raw dataset."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import shutil
import sys

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.robust.predict import load_bundle, load_raw_amplitude, predict_amplitude
from model_train.robust.run_experiment import CLASSES, digest, metrics, save_json


def summarize(rows, class_names):
    """Each session contributes once, regardless of its valid-window count."""
    if not rows or class_names != CLASSES:
        raise ValueError("expected nonempty results with the documented class order")
    truth, predicted, session_truth, session_predicted, first = [], [], [], [], []
    for row in rows:
        y = class_names.index(row["label"])
        result = row["ensemble"]
        scores = np.asarray([w["scores"] for w in result["windows"]], dtype=np.float32)
        if scores.ndim != 2 or scores.shape[1] != 3 or not np.isfinite(scores).all():
            raise ValueError("invalid saved window probabilities")
        np.testing.assert_allclose(scores.sum(1), 1, atol=1e-6)
        p = scores.argmax(1)
        session_p = int(scores.mean(0).argmax())
        if class_names[session_p] != result["prediction"]:
            raise ValueError("saved session prediction differs from probability mean")
        truth.extend([y] * len(p))
        predicted.extend(p.tolist())
        session_truth.append(y)
        session_predicted.append(session_p)
        first.append(int(p[0]))
    return {"window": metrics(truth, predicted),
            "session": metrics(session_truth, session_predicted),
            "first_valid_window": metrics(session_truth, first)}


def verify_hashes(expected):
    for name, sha in expected.items():
        if digest(Path(name)) != sha:
            raise ValueError(f"input changed: {name}")


def session_date(audit, row):
    """Support legacy single-date audits and multi-date rows without ID collisions."""
    date = row.get("date", audit.get("date"))
    if not isinstance(date, str) or len(date) != 8 or not date.isdigit():
        raise ValueError("audit row needs an eight-digit collection date")
    if Path(row["path"]).parent.name != date:
        raise ValueError("audit date differs from its collection directory")
    return date


def summarize_dates(rows, class_names):
    return {date: summarize([r for r in rows if r["date"] == date], class_names)
            for date in sorted({r["date"] for r in rows})}


def evaluate(args):
    torch.set_num_threads(2)
    audit = json.loads(args.audit.read_text())
    bundle, models = load_bundle(args.model)
    if (len(models) != 3 or bundle["class_names"] != CLASSES
            or bundle["stride_frames"] != 1500 or bundle["sample_rate_hz"] != 100
            or bundle["raw_tone_order"] != list(range(38, 64)) + list(range(2, 27))):
        raise ValueError("not the documented three-model 30-second bundle")
    if audit["errors"] or len(audit["rows"]) != audit["sessions"]:
        raise ValueError("audit has errors or an inconsistent session count")
    inventory = json.loads(args.training_inventory.read_text())
    if {s["key"] for s in inventory} != set(bundle["training_sessions"]):
        raise ValueError("training inventory does not match the model's training keys")
    training_hashes = {f["sha256"] for s in inventory for f in s["files"]}
    expected = {str(p.resolve()): digest(p) for p in
                (args.model, args.audit, args.training_inventory)}
    keys = set()
    for row in audit["rows"]:
        key = f"{session_date(audit, row)}-s{row['session_id']}"
        if key in keys or key in bundle["training_sessions"]:
            raise ValueError(f"duplicate or training session: {key}")
        keys.add(key)
        folder = ROOT / row["path"]
        expected[str((folder / "session.json").resolve())] = row["manifest_sha256"]
        manifest = json.loads((folder / "session.json").read_text())
        if manifest["label"] != row["label"] or manifest["session_id"] != row["session_id"]:
            raise ValueError(f"label/identity differs from audit: {key}")
        for device in row["devices"]:
            if device["sha256"] in training_hashes:
                raise ValueError(f"raw file overlaps training: {key}")
            expected[str((folder / f"device_{device['device_id']}.csi").resolve())] = device["sha256"]
    verify_hashes(expected)
    args.output.mkdir(parents=True, exist_ok=False)
    source = args.output / "source"
    source.mkdir()
    source_hashes = {}
    for relative in ("model_train/robust/evaluate_raw.py", "model_train/robust/audit_raw.py", "model_train/robust/predict.py",
                     "model_train/robust/shared_temporal.py", "model_train/robust/run_experiment.py",
                     "model_train/robust/features.py", "model_train/analysis/prepare_phase_experiment.py",
                     "model_train/preprocessing/preprocess_3rx.py",
                     "model_train/preprocessing/phase_features.py", "scripts/csi_store.py",
                     str(args.report_document.relative_to(ROOT))):
        path = ROOT / relative
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        source_hashes[relative] = digest(path)
    protocol = {"started_at_utc": datetime.now(timezone.utc).isoformat(),
                "model": str(args.model.resolve()), "model_sha256": digest(args.model),
                "model_metadata": {k: v for k, v in bundle.items() if k != "state_dicts"},
                "audit": str(args.audit.resolve()), "input_hashes": expected,
                "source_hashes": source_hashes, "evaluation_keys": sorted(keys),
                "window_frames": 3000, "stride_frames": 1500,
                "aggregation": "mean model probabilities, then mean window probabilities per session",
                "training_performed": False, "training_key_overlap": [], "training_hash_overlap": [],
                "runtime": {"python": sys.version, "platform": platform.platform(),
                            "numpy": np.__version__, "torch": str(torch.__version__), "device": "cpu"}}
    save_json(args.output / "protocol.json", protocol)
    rows = []
    for audited in audit["rows"]:
        folder = ROOT / audited["path"]
        amp, quality = load_raw_amplitude(folder)
        ensemble = predict_amplitude(amp, bundle, models)
        starts = [w["start_frame"] for w in ensemble["windows"]]
        if (starts != audited["valid_window_start_frames"]
                or ensemble["candidate_windows"] != audited["window_candidates"]
                or len(amp) != audited["common_length_frames"]):
            raise ValueError(f"window reconstruction differs from audit: {folder}")
        per_model = [predict_amplitude(amp, bundle, [m]) for m in models]
        individual_scores = np.asarray([[w["scores"] for w in p["windows"]]
                                        for p in per_model], dtype=np.float32)
        actual_scores = np.asarray([w["scores"] for w in ensemble["windows"]], dtype=np.float32)
        np.testing.assert_allclose(individual_scores.mean(0), actual_scores, rtol=1e-6, atol=1e-7)
        date = session_date(audit, audited)
        row = {"key": f"{date}-s{audited['session_id']}", "date": date,
               "session_id": audited["session_id"], "label": audited["label"],
               "path": audited["path"], "quality": quality,
               "ensemble": ensemble, "models_in_bundle_order": per_model}
        rows.append(row)
        save_json(args.output / f"{row['key']}.json", row)
        correct = sum(w["prediction"] == row["label"] for w in ensemble["windows"])
        print(f"{row['key']} truth={row['label']} prediction={ensemble['prediction']} "
              f"windows={correct}/{ensemble['valid_windows']}", flush=True)
    if (sum(r["ensemble"]["valid_windows"] for r in rows) != audit["valid_windows"]
            or sum(r["ensemble"]["candidate_windows"] for r in rows) != audit["candidate_windows"]):
        raise ValueError("dataset window totals differ from audit")
    verify_hashes(expected)
    # The three models must remain exactly equal to the loaded state, including BN buffers.
    for model, initial in zip(models, bundle["state_dicts"]):
        for name, value in model.state_dict().items():
            torch.testing.assert_close(value, initial[name], rtol=0, atol=0)
    result = {"completed_at_utc": datetime.now(timezone.utc).isoformat(),
              "model_sha256": protocol["model_sha256"], "class_names": CLASSES,
              "metrics": summarize(rows, CLASSES),
              "metrics_by_date": summarize_dates(rows, CLASSES),
              "per_model_metrics": [summarize([dict(r, ensemble=r["models_in_bundle_order"][i])
                                               for r in rows], CLASSES) for i in range(3)],
              "candidate_windows": audit["candidate_windows"], "valid_windows": audit["valid_windows"],
              "checks": {"input_hashes_unchanged": True, "model_state_unchanged": True,
                         "window_starts_match_audit": True, "ensemble_matches_individual_mean": True},
              "limitations": ["Same date/session IDs appear in prior pilot selection records; not a pristine blind test.",
                              f"Overlapping windows are not independent samples; {len(rows)} collection sessions.",
                              "Whole-session aggregation does not measure transition latency or live operation."],
              "sessions": [{k: v for k, v in r.items() if k != "models_in_bundle_order"} for r in rows]}
    save_json(args.output / "results.json", result)
    print(json.dumps(result["metrics"], ensure_ascii=False, indent=2), flush=True)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", required=True, type=Path)
    p.add_argument("--audit", required=True, type=Path)
    p.add_argument("--training-inventory", required=True, type=Path)
    p.add_argument("--report-document", type=lambda p: Path(p).resolve(),
                   default=ROOT / "model_train/docs/model-training/20260917-model-evaluation.md")
    p.add_argument("--output", required=True, type=Path, help="new output directory; existing paths are refused")
    evaluate(p.parse_args())


if __name__ == "__main__":
    main()
