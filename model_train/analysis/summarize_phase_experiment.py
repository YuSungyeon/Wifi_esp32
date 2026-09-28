#!/usr/bin/env python3
"""Audit saved predictions and summarize the completed phase comparison; no retraining."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.analysis.prepare_phase_experiment import sha256, write_json
from model_train.analysis.run_phase_experiment import CLASSES, load_metadata, session_results
from model_train.lstm import LSTM as training


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", required=True, type=Path)
    args = parser.parse_args()
    root = args.dataset.resolve()
    manifest = json.loads((root/"manifest.json").read_text())
    rows = json.loads((root/"comparison.json").read_text())
    metadata = load_metadata(root/"test"/"windows.jsonl")
    placement = {s["key"]: s["placement_id"] for s in manifest["sessions"]}
    expected_truth = np.array([r["label_id"] for r in metadata])
    comparisons, run_audit = {}, []
    for row in rows:
        run = root/"runs"/f"{row['mode']}-seed{row['seed']}"
        config = json.loads((run/"config.json").read_text())
        result = json.loads((run/"result.json").read_text())
        history = load_metadata(run/"history.jsonl")
        # Independently verify checkpoint selection and saved prediction alignment.
        best = max(history, key=lambda h: h["validation"]["macro_f1"])
        assert best["epoch"] == result["best_epoch"]
        assert sha256(run/"best-model.pt") == result["checkpoint_sha256"]
        pred = np.load(run/"test.npz")
        np.testing.assert_array_equal(pred["indices"], np.arange(len(metadata)))
        np.testing.assert_array_equal(pred["truth"], expected_truth)
        np.testing.assert_array_equal(pred["predictions"], pred["probabilities"].argmax(axis=1))
        np.testing.assert_allclose(pred["probabilities"].sum(axis=1), 1, atol=1e-6)
        rebuilt = session_results(dict(pred), metadata)
        assert rebuilt == row["session"]
        grouped = {}
        for p in ("P2", "P3"):
            subset = [r for r in rebuilt["sessions"] if placement[r["session_key"]] == p]
            cm = training.confusion_matrix(np.array([r["true_label_id"] for r in subset]),
                                           np.array([r["predicted_label_id"] for r in subset]))
            grouped[p] = training.metrics_from_confusion(cm, CLASSES)
        entry = {"mode": row["mode"], "seed": row["seed"], "best_epoch": result["best_epoch"],
                 "epochs": result["epochs"], "seconds": result["elapsed_seconds"],
                 "train_macro_f1_at_best": best["train"]["macro_f1"],
                 "val_window_macro_f1": best["validation"]["macro_f1"],
                 "val_session_macro_f1": best["validation_session"]["macro_f1"],
                 "placement_metrics": grouped,
                 "initial_model_sha256": config["initial_model_sha256"],
                 "parameter_count": config["parameter_count"],
                 "test_rows_verified": len(metadata)}
        if row["mode"] == "A":
            entry["amplitude_all"] = json.loads((run/"test-amplitude-all.json").read_text())
        run_audit.append(entry)
    for seed in range(3):
        assert len({r["initial_model_sha256"] for r in run_audit if r["seed"] == seed}) == 1
    for mode in ("A", "P", "AP"):
        selected = [r for r in rows if r["mode"] == mode]
        metrics = [r["session"]["metrics"] for r in selected]
        f1 = [s["macro_f1"] for s in metrics]
        comparisons[mode] = {
            "session_f1_mean": float(np.mean(f1)), "session_f1_std": float(np.std(f1, ddof=1)),
            "session_accuracy_mean": float(np.mean([s["accuracy"] for s in metrics])),
            "window_f1_mean": float(np.mean([r["window"]["macro_f1"] for r in selected])),
            "empty_static_errors_mean": float(np.mean([s["confusion_matrix"][0][1]+s["confusion_matrix"][1][0] for s in metrics])),
            "recall_mean": {c: float(np.mean([s["per_class"][c]["recall"] for s in metrics])) for c in CLASSES},
            "precision_mean": {c: float(np.mean([s["per_class"][c]["precision"] for s in metrics])) for c in CLASSES},
            "pooled_confusion_3_seeds": np.sum([s["confusion_matrix"] for s in metrics], axis=0).tolist()}
    keyed = {(r["mode"], r["seed"]): r for r in rows}
    deltas = [keyed[("AP", s)]["session"]["metrics"]["macro_f1"] - keyed[("A", s)]["session"]["metrics"]["macro_f1"] for s in range(3)]
    summary = {"metrics": comparisons, "AP_minus_A_by_seed": deltas,
               "positive_seeds": sum(v > 0 for v in deltas),
               "AP_minus_A_mean": float(np.mean(deltas)),
               "runs": run_audit, "verified_test_rows": sum(r["test_rows_verified"] for r in run_audit),
               "total_training_seconds": sum(r["seconds"] for r in run_audit)}
    write_json(root/"verified-summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
