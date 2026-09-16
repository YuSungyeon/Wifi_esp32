#!/usr/bin/env python3
"""Run the fixed three-seed boundary-trim experiment without changing baseline files."""

import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import shutil
import subprocess
import sys

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "model_train" / "lstm"))
import LSTM as lstm  # noqa: E402


def comparison_metrics(result):
    metrics = result["window_level"]
    matrix = np.asarray(metrics["confusion_matrix"])
    quiet_count = int(matrix[:2].sum())
    return {
        "accuracy": metrics["accuracy"],
        "macro_f1": metrics["macro_f1"],
        "empty_recall": metrics["per_class"]["empty"]["recall"],
        "static_recall": metrics["per_class"]["static"]["recall"],
        "motion_recall": metrics["per_class"]["motion"]["recall"],
        "empty_static_mean_f1": float(np.mean([
            metrics["per_class"][name]["f1"] for name in ("empty", "static")
        ])),
        "empty_static_accuracy": float((matrix[0, 0] + matrix[1, 1]) / quiet_count),
        "empty_static_cross_error": float((matrix[0, 1] + matrix[1, 0]) / quiet_count),
        "session_accuracy": result["session_level"]["metrics"]["accuracy"],
    }


def evaluate_baseline(run, contract, device, output):
    """Use original checkpoint normalization on the new raw-amplitude windows."""
    checkpoint = lstm._load_checkpoint(run / "best-model.pt", device)
    config = lstm._read_json(run / "config.json")
    for filename, key in (("dataset-manifest.json", "dataset_manifest_sha256"),
                          ("normalization.npz", "normalization_sha256")):
        actual_hash = lstm._sha256(run / filename)
        if actual_hash != checkpoint[key] or actual_hash != config[key]:
            raise ValueError(f"Baseline provenance mismatch: {run / filename}")
    original_manifest = lstm._read_json(run / "dataset-manifest.json")
    if original_manifest["splits"] != contract.manifest["splits"]:
        raise ValueError("Baseline and trimmed session splits differ")
    with np.load(run / "normalization.npz") as stats:
        original_contract = replace(
            contract,
            mean=stats["mean"].astype(np.float32),
            std_safe=stats["std_safe"].astype(np.float32),
            normalization_path=run / "normalization.npz",
        )
    train_config = lstm.TrainConfig(**checkpoint["train_config"])
    model = lstm.LSTMClassifier(**checkpoint["model_config"]).to(device)
    model.load_state_dict(checkpoint["model_state_dict"])
    loader = lstm.make_dataloader(original_contract, "test", train_config, False)
    names = lstm._ordered_class_names(contract.manifest["label_map"])
    evaluation = lstm.evaluate(model, loader, torch.nn.CrossEntropyLoss(), device, names)
    metadata = lstm._load_windows_metadata(contract.split_metadata_paths["test"])
    result = {
        "seed": train_config.seed,
        "baseline_run": str(run),
        "checkpoint_sha256": lstm._sha256(run / "best-model.pt"),
        "normalization_sha256": checkpoint["normalization_sha256"],
        "evaluation_manifest_sha256": lstm._sha256(contract.dataset_dir / "manifest.json"),
        "window_level": evaluation["metrics"],
        "session_level": lstm.session_level_results(evaluation, metadata, names),
    }
    output.mkdir()
    lstm._write_json(output / "metrics.json", result)
    lstm._write_test_predictions(output / "predictions.jsonl", evaluation, metadata, names)
    return result


def run_command(command, log_path):
    print("RUN", " ".join(map(str, command)), flush=True)
    with log_path.open("w") as log:
        process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        for line in process.stdout:
            print(line, end="", flush=True)
            log.write(line)
            log.flush()
        if process.wait():
            raise RuntimeError(f"Command failed; see {log_path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--baseline-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device", default="mps", choices=("mps", "cpu", "cuda"))
    args = parser.parse_args()
    output = args.output_dir.resolve()
    if output.exists():
        raise FileExistsError(f"Use a new experiment output directory: {output}")
    contract = lstm.validate_dataset_contract(args.dataset_dir)
    if contract.manifest["config"].get("trim_frames") != 500:
        raise ValueError("This experiment requires 500 frames trimmed at each end")
    device = lstm.select_device(args.device)
    baselines = {}
    for path in sorted(args.baseline_root.glob("*/config.json")):
        config = lstm._read_json(path)
        training = config["training"]
        seed = training["seed"]
        if training["class_weight"] != "balanced" or seed not in (0, 1, 2):
            continue
        expected = asdict(lstm.TrainConfig(seed=seed, class_weight="balanced"))
        if training != expected or seed in baselines:
            raise ValueError(f"Ambiguous or incompatible baseline: {path}")
        baselines[seed] = path.parent.resolve()
    if set(baselines) != {0, 1, 2}:
        raise ValueError("Three balanced baseline runs are required")
    output.mkdir(parents=True)
    sources = {}
    for relative in (
        "model_train/lstm/LSTM.py", "model_train/preprocessing/preprocess_3rx.py",
        "model_train/analysis/trim_boundary_experiment.py",
        "model_train/docs/model-training/trim-boundary-experiment.md",
    ):
        target = output / "source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
        sources[relative] = lstm._sha256(target)
    summary = {
        "status": "running", "interpretation": "exploratory; previously inspected test sessions",
        "dataset_dir": str(contract.dataset_dir),
        "dataset_manifest_sha256": lstm._sha256(contract.dataset_dir / "manifest.json"),
        "source_sha256": sources, "seeds": [],
    }
    lstm._write_json(output / "comparison.json", summary)
    # Complete all training before examining any new test results.
    for seed in (0, 1, 2):
        run = output / f"seed{seed}-balanced"
        command = [sys.executable, "-u", str(ROOT / "model_train/lstm/LSTM.py"),
                   "train", "--dataset-dir", str(contract.dataset_dir),
                   "--run-dir", str(run), "--seed", str(seed),
                   "--class-weight", "balanced", "--device", args.device]
        run_command(command, output / f"seed{seed}-train.log")
    for seed in (0, 1, 2):
        run = output / f"seed{seed}-balanced"
        original = lstm._read_json(baselines[seed] / "test-metrics.json")
        central = evaluate_baseline(baselines[seed], contract, device,
                                    output / f"baseline-seed{seed}-central")
        command = [sys.executable, "-u", str(ROOT / "model_train/lstm/LSTM.py"),
                   "test", "--dataset-dir", str(contract.dataset_dir),
                   "--run-dir", str(run), "--device", args.device]
        run_command(command, output / f"seed{seed}-test.log")
        trimmed = lstm._read_json(run / "test-metrics.json")
        summary["seeds"].append({
            "seed": seed, "baseline_run": str(baselines[seed]), "trimmed_run": str(run),
            "training": lstm._read_json(run / "run-summary.json"),
            "baseline_full": comparison_metrics(original),
            "baseline_central": comparison_metrics(central),
            "retrained_central": comparison_metrics(trimmed),
            "baseline_central_sessions": central["session_level"]["sessions"],
            "retrained_central_sessions": trimmed["session_level"]["sessions"],
        })
        lstm._write_json(output / "comparison.json", summary)
    summary["aggregate"] = {}
    for group in ("baseline_full", "baseline_central", "retrained_central"):
        summary["aggregate"][group] = {
            metric: {"mean": float(np.mean([row[group][metric] for row in summary["seeds"]])),
                     "std": float(np.std([row[group][metric] for row in summary["seeds"]]))}
            for metric in summary["seeds"][0][group]
        }
    summary["status"] = "complete"
    lstm._write_json(output / "comparison.json", summary)
    print(json.dumps(summary["aggregate"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
