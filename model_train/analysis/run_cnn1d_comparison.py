#!/usr/bin/env python3
"""고정된 CNN 6회 학습 → validation 선택 저장 → 선택된 seed 3개 test.

기존 LSTM baseline과 같은 탐색 범위를 사용한다. 결과를 보고 재학습하지 않는다.
출력 디렉터리는 새 디렉터리여야 하며, 모델별 CLI를 별도 프로세스로 실행한다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def launch(command, log_path):
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    with log_path.open("w") as log:
        with subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) as process:
            for line in process.stdout:
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
            status = process.wait()
    if status:
        raise subprocess.CalledProcessError(status, command)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--device", choices=("cpu", "mps", "cuda"), default="mps")
    args = parser.parse_args()
    dataset = args.dataset_dir.resolve()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    source_hashes = {}
    for relative in ("model_train/cnn1d/CNN1D.py", "model_train/lstm/LSTM.py",
                     "model_train/analysis/run_cnn1d_comparison.py"):
        path = ROOT / relative
        shutil.copy2(path, source / path.name)
        source_hashes[relative] = sha256(path)
    files = [dataset / "manifest.json", dataset / "normalization.npz"]
    files += [dataset / split / name for split in ("train", "validation", "test")
              for name in ("X.npy", "y.npy", "windows.jsonl")]
    print("Recording dataset hashes before training...", flush=True)
    protocol = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "dataset_dir": str(dataset), "device": args.device,
        "seeds": [0, 1, 2], "class_weight_candidates": ["none", "balanced"],
        "selection_metric": "mean validation window macro_f1; ties prefer none",
        "test_role": "exploratory comparison on previously observed test sessions",
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "source_sha256": source_hashes,
        "dataset_sha256": {str(p.relative_to(dataset)): sha256(p) for p in files},
        "commands": [],
    }
    save(output / "protocol.json", protocol)
    cli = [sys.executable, "-u", str(ROOT / "model_train/cnn1d/CNN1D.py")]
    common = ["--dataset-dir", str(dataset), "--device", args.device]
    scores = {}
    for weight in protocol["class_weight_candidates"]:
        scores[weight] = []
        for seed in protocol["seeds"]:
            run = output / f"seed{seed}-{weight}"
            command = cli + ["train"] + common + [
                "--run-dir", str(run), "--seed", str(seed), "--class-weight", weight,
                "--batch-size", "32", "--epochs", "50", "--patience", "5",
                "--learning-rate", "0.001", "--dropout", "0.2", "--num-workers", "0",
                "--min-delta", "0", "--channels", "32", "64", "64",
                "--kernel-sizes", "5", "5", "3",
            ]
            protocol["commands"].append(command)
            save(output / "protocol.json", protocol)
            launch(command, output / f"seed{seed}-{weight}-train.log")
            metrics = json.loads((run / "validation-metrics.json").read_text())
            scores[weight].append(metrics["window_level"]["macro_f1"])
    means = {weight: sum(values) / len(values) for weight, values in scores.items()}
    selected = max(means, key=means.get)
    selection = {
        "selected_at": datetime.now(timezone.utc).isoformat(),
        "validation_scores": scores, "validation_means": means,
        "selected_class_weight": selected, "test_started": False,
    }
    # 모든 test 호출 이전에 validation만으로 결정한 선택을 고정하여 보존한다.
    save(output / "selection.json", selection)
    print("Validation selection:", json.dumps(selection), flush=True)
    for seed in protocol["seeds"]:
        run = output / f"seed{seed}-{selected}"
        command = cli + ["test"] + common + ["--run-dir", str(run)]
        protocol["commands"].append(command)
        save(output / "protocol.json", protocol)
        launch(command, output / f"seed{seed}-{selected}-test.log")
    save(output / "completed.json", {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "selected_class_weight": selected, "train_runs": 6, "test_runs": 3,
    })


if __name__ == "__main__":
    main()
