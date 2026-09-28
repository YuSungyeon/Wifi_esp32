#!/usr/bin/env python3
"""Train/evaluate frozen A/P/AP CNN experiments with shared official model/metrics.

The standard 192-feature training entry points and historical artifacts are unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import sys
import time

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, Dataset

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.analysis.prepare_phase_experiment import sha256, source_snapshot, write_json
from model_train.preprocessing import phase_features as phase
from model_train.cnn1d.CNN1D import CNN1DClassifier
from model_train.lstm import LSTM as training

CLASSES = ["empty", "static", "motion"]
MODES = ("A", "P", "AP")
TRAINING = {"batch_size": 32, "learning_rate": .001, "max_epochs": 50,
            "patience": 5, "min_delta": 0., "class_weight": "none", "dropout": .2,
            "optimizer": "Adam", "betas": [.9, .999], "eps": 1e-8,
            "weight_decay": 0., "channels": [32, 64, 64], "kernel_sizes": [5, 5, 3]}


def load_metadata(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def contract_hash(manifest):
    stable = {k: manifest[k] for k in ("feature_version", "input_size", "window", "stride",
                                      "rx_order", "raw_indices", "normalization_sha256")}
    stable["development_sessions"] = [{k: s[k] for k in ("key", "split", "label_id", "feature_sha256", "starts_sha256")}
                                       for s in manifest["sessions"] if s["split"] != "test"]
    stable["metadata"] = {s: manifest["split_summary"][s]["metadata_sha256"] for s in ("train", "validation")}
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()


def validate_contract(dataset, include_test=False):
    m = json.loads((dataset/"manifest.json").read_text())
    if m["feature_version"] != phase.FEATURE_VERSION or m["input_size"] != 459 or m["window"] != 300:
        raise ValueError("phase input contract mismatch")
    if m["rx_order"] != [101, 102, 103] or m["raw_indices"] != phase.RAW_INDICES.tolist():
        raise ValueError("receiver/subcarrier order mismatch")
    if sha256(dataset/"normalization.npz") != m["normalization_sha256"]:
        raise ValueError("normalization hash mismatch")
    norm = np.load(dataset/"normalization.npz")
    if norm["mean"].shape != (459,) or norm["std_safe"].shape != (459,):
        raise ValueError("normalization shape mismatch")
    if not (norm["std_safe"] > 0).all() or not np.isfinite(norm["mean"]).all():
        raise ValueError("invalid normalization")
    np.testing.assert_array_equal(norm["mean"][153:], 0)
    np.testing.assert_array_equal(norm["std_safe"][153:], 1)
    seen = set()
    for split in (["train", "validation", "test"] if include_test else ["train", "validation"]):
        summary = m["split_summary"][split]
        if not summary["all_classes_present"]:
            raise ValueError(f"{split} lost a class: {summary['class_counts']}")
        metadata_path = dataset/split/"windows.jsonl"
        if sha256(metadata_path) != summary["metadata_sha256"]:
            raise ValueError(f"{split} window metadata hash mismatch")
        rows = load_metadata(metadata_path)
        if len(rows) != summary["windows"]:
            raise ValueError("window count mismatch")
        for s in [v for v in m["sessions"] if v["split"] == split]:
            if s["key"] in seen:
                raise ValueError("session leakage across splits")
            seen.add(s["key"])
            path = dataset/"sessions"/s["key"]/"features.npy"
            if sha256(path) != s["feature_sha256"]:
                raise ValueError(f"feature file changed: {s['key']}")
            x = np.load(path, mmap_mode="r")
            if x.shape != (s["frames"], 459) or x.dtype != np.float32:
                raise ValueError(f"feature shape/dtype mismatch: {path}")
    return m


class PhaseDataset(Dataset):
    def __init__(self, dataset, split, mode, amplitude_all=False):
        self.root, self.mode = Path(dataset), mode
        filename = "amplitude-windows.jsonl" if amplitude_all else "windows.jsonl"
        self.metadata = load_metadata(self.root/split/filename)
        self.arrays = {key: np.load(self.root/"sessions"/key/"features.npy", mmap_mode="r")
                       for key in {r["session_key"] for r in self.metadata}}
        norm = np.load(self.root/"normalization.npz")
        self.mean, self.std = norm["mean"].astype(np.float32), norm["std_safe"].astype(np.float32)
        if amplitude_all and mode != "A":
            raise ValueError("full amplitude windows are only valid for A")

    def __len__(self):
        return len(self.metadata)

    def __getitem__(self, index):
        row = self.metadata[index]
        x = self.arrays[row["session_key"]][row["start"]:row["start"]+300]
        x = phase.transform_window(x, self.mean, self.std, self.mode)
        return torch.from_numpy(x), row["label_id"], index


def session_results(evaluation, metadata):
    # Official helper groups by integer ID; remap date-qualified keys explicitly.
    keys = sorted({r["session_key"] for r in metadata})
    mapping = {key: i for i, key in enumerate(keys)}
    adapted = [dict(row, session_id=mapping[row["session_key"]]) for row in metadata]
    result = training.session_level_results(evaluation, adapted, CLASSES)
    originals = {r["session_key"]: r for r in metadata}
    for row in result["sessions"]:
        key = keys[row["session_id"]]
        row["session_key"] = key
        row["session_id"] = originals[key]["session_id"]
        row["date"] = originals[key]["date"]
    return result


def loader(dataset, seed, shuffle=False):
    return DataLoader(dataset, batch_size=32, shuffle=shuffle, num_workers=0,
                      generator=torch.Generator().manual_seed(seed), drop_last=False)


def model_digest(model):
    digest = hashlib.sha256()
    for name, tensor in model.state_dict().items():
        digest.update(name.encode())
        digest.update(tensor.detach().cpu().numpy().tobytes())
    return digest.hexdigest()


def save_evaluation(path, evaluation, dataset):
    sessions = session_results(evaluation, dataset.metadata)
    write_json(path.with_suffix(".json"), {"window": evaluation["metrics"], "session": sessions})
    np.savez(path.with_suffix(".npz"), truth=evaluation["truth"],
             predictions=evaluation["predictions"], probabilities=evaluation["probabilities"],
             indices=evaluation["indices"])
    return {"window": evaluation["metrics"], "session": sessions}


def train_run(dataset, manifest, mode, seed, device):
    run = dataset/"runs"/f"{mode}-seed{seed}"
    config_path = run/"config.json"
    fingerprint = contract_hash(manifest)
    if (run/"result.json").exists():
        existing = json.loads(config_path.read_text())
        if existing["dataset_contract_hash"] != fingerprint or existing["training"] != TRAINING:
            raise ValueError(f"existing run has different contract: {run}")
        print(f"Already completed {mode} seed={seed}", flush=True)
        return
    if run.exists():
        raise ValueError(f"incomplete run exists; inspect it before retry: {run}")
    run.mkdir(parents=True)
    training.set_random_seed(seed)
    model = CNN1DClassifier(input_size=459, channels=(32, 64, 64), kernel_sizes=(5, 5, 3), dropout=.2)
    initial_digest = model_digest(model)
    model.to(device)
    train_data, val_data = PhaseDataset(dataset, "train", mode), PhaseDataset(dataset, "validation", mode)
    train_loader, val_loader = loader(train_data, seed, True), loader(val_data, seed)
    optimizer = torch.optim.Adam(model.parameters(), lr=.001)
    criterion = nn.CrossEntropyLoss()
    cfg = {"mode": mode, "seed": seed, "training": TRAINING,
           "parameter_count": sum(p.numel() for p in model.parameters()),
           "actual_features": {"A": 153, "P": 306, "AP": 459}[mode],
           "initial_model_sha256": initial_digest, "dataset_contract_hash": fingerprint,
           "normalization_sha256": manifest["normalization_sha256"],
           "train_metadata_sha256": manifest["split_summary"]["train"]["metadata_sha256"],
           "runtime": {"torch": torch.__version__, "numpy": np.__version__,
                       "python": sys.version, "platform": platform.platform(), "device": str(device)},
           "source": source_snapshot(run)}
    write_json(config_path, cfg)
    best, best_epoch, stale = -1., 0, 0
    started = time.perf_counter()
    with (run/"history.jsonl").open("w") as history:
        for epoch in range(1, 51):
            tick = time.perf_counter()
            train_metrics = training.train_one_epoch(model, train_loader, criterion, optimizer, device, CLASSES)
            val = training.evaluate(model, val_loader, criterion, device, CLASSES)
            session = session_results(val, val_data.metadata)
            score = val["metrics"]["macro_f1"]
            record = {"epoch": epoch, "train": train_metrics, "validation": val["metrics"],
                      "validation_session": session["metrics"], "seconds": time.perf_counter()-tick}
            history.write(json.dumps(record)+"\n")
            history.flush()
            if score > best:
                best, best_epoch, stale = score, epoch, 0
                checkpoint = {"model_state_dict": {k: v.detach().cpu() for k, v in model.state_dict().items()},
                              "epoch": epoch, "mode": mode, "seed": seed,
                              "dataset_contract_hash": fingerprint, "normalization_sha256": cfg["normalization_sha256"],
                              "model_config": {"input_size": 459, "channels": [32, 64, 64],
                                               "kernel_sizes": [5, 5, 3], "dropout": .2}}
                torch.save(checkpoint, run/"best-model.pt")
                save_evaluation(run/"validation", val, val_data)
            else:
                stale += 1
            print(f"{mode} seed={seed} epoch={epoch:02d} train_f1={train_metrics['macro_f1']:.4f} "
                  f"val_window={score:.4f} val_session={session['metrics']['macro_f1']:.4f} "
                  f"best={best:.4f}@{best_epoch} seconds={record['seconds']:.1f}", flush=True)
            if stale >= 5:
                break
    result = {"mode": mode, "seed": seed, "epochs": epoch, "best_epoch": best_epoch,
              "best_validation_window_macro_f1": best, "elapsed_seconds": time.perf_counter()-started,
              "checkpoint_sha256": sha256(run/"best-model.pt")}
    write_json(run/"result.json", result)
    if device.type == "mps":
        torch.mps.empty_cache()


def evaluate_runs(dataset, manifest, device):
    # All nine model selections must be finished before opening test predictions.
    runs = [(mode, seed, dataset/"runs"/f"{mode}-seed{seed}") for seed in range(3) for mode in MODES]
    if not all((p/"result.json").exists() for _, _, p in runs):
        raise ValueError("all nine runs must finish before final test")
    fingerprint = contract_hash(manifest)
    output = []
    for mode, seed, run in runs:
        checkpoint = torch.load(run/"best-model.pt", map_location="cpu", weights_only=True)
        if checkpoint["dataset_contract_hash"] != fingerprint:
            raise ValueError("checkpoint dataset contract mismatch")
        model = CNN1DClassifier(**checkpoint["model_config"])
        model.load_state_dict(checkpoint["model_state_dict"])
        model.to(device)
        test_data = PhaseDataset(dataset, "test", mode)
        evaluation = training.evaluate(model, loader(test_data, seed), nn.CrossEntropyLoss(), device, CLASSES)
        metrics = save_evaluation(run/"test", evaluation, test_data)
        if mode == "A":
            full_data = PhaseDataset(dataset, "test", "A", amplitude_all=True)
            full_eval = training.evaluate(model, loader(full_data, seed), nn.CrossEntropyLoss(), device, CLASSES)
            save_evaluation(run/"test-amplitude-all", full_eval, full_data)
        output.append({"mode": mode, "seed": seed, **metrics})
        print(f"TEST {mode} seed={seed} window={metrics['window']['macro_f1']:.4f} "
              f"session={metrics['session']['metrics']['macro_f1']:.4f} "
              f"accuracy={metrics['session']['metrics']['accuracy']:.4f}", flush=True)
    write_json(dataset/"comparison.json", output)
    return output


def report(dataset):
    results = json.loads((dataset/"comparison.json").read_text())
    m = json.loads((dataset/"manifest.json").read_text())
    lines = ["# 진폭·위상 비교 실험 결과", "", "> 상태: EXPERIMENTAL — 고정 분할 3 seed 실험", "",
             "## 사용 데이터", "", "| Split | 클래스별 공통 윈도우 (empty/static/motion) | 총 공통 | 진폭 전체 |",
             "|---|---|---:|---:|"]
    for split in ("train", "validation", "test"):
        s = m["split_summary"][split]
        lines.append(f"| {split} | {s['class_counts']} | {s['windows']} | {s['amplitude_windows']} |")
    lines += ["", "## Test 결과", "", "| 입력 | seed | session macro F1 | session accuracy | window macro F1 | empty→static | static→empty | motion recall |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in results:
        s = r["session"]["metrics"]
        cm = s["confusion_matrix"]
        lines.append(f"| {r['mode']} | {r['seed']} | {s['macro_f1']:.4f} | {s['accuracy']:.4f} | "
                     f"{r['window']['macro_f1']:.4f} | {cm[0][1]} | {cm[1][0]} | {s['per_class']['motion']['recall']:.4f} |")
    lines += ["", "| 입력 | session macro F1 평균 ± 표본 표준편차 |", "|---|---:|"]
    for mode in MODES:
        values = [r["session"]["metrics"]["macro_f1"] for r in results if r["mode"] == mode]
        lines.append(f"| {mode} | {np.mean(values):.4f} ± {np.std(values, ddof=1):.4f} |")
    by = {(r["mode"], r["seed"]): r for r in results}
    delta = [by[("AP", seed)]["session"]["metrics"]["macro_f1"] - by[("A", seed)]["session"]["metrics"]["macro_f1"] for seed in range(3)]
    lines += ["", f"AP−A session macro F1 (seed 0/1/2): {delta}; 평균 {np.mean(delta):.4f}.", "",
              "A의 test-amplitude-all은 같은 공통 train 윈도우에서 학습한 모델을 진폭 전체 test 윈도우에 평가한 결과다.",
              "별도 전체 train 윈도우로 재학습한 결과는 아니다.", "", "## 세션별 윈도우 유지율", "",
              "| 세션 | 클래스 | 공통 / 진폭 | 유지율 | 환경 사진 수 |", "|---|---|---:|---:|---:|"]
    for s in m["sessions"]:
        lines.append(f"| {s['key']} | {s['label']} | {s['common_windows']} / {s['amplitude_windows']} | {s['window_retention']:.1%} | {s['image_count']} |")
    lines += ["", "사진은 RX 배치·환경 설정 기록이며 모델에는 사용하지 않았다.",
              "두 날짜·제한된 validation 세션의 결과이며, 새로운 날짜/환경 재검증이 필요하다.", ""]
    (dataset/"report.md").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "train", "test", "report", "benchmark"))
    parser.add_argument("--dataset", required=True, type=Path)
    parser.add_argument("--device", default="auto")
    parser.add_argument("--mode", choices=MODES)
    parser.add_argument("--seed", type=int, choices=(0, 1, 2))
    args = parser.parse_args()
    dataset = args.dataset.resolve()
    if args.command == "report":
        report(dataset)
        return
    m = validate_contract(dataset, include_test=args.command == "test")
    if args.command == "validate":
        print("Dataset contract passed", contract_hash(m))
        return
    device = training.select_device(args.device)
    torch.set_num_threads(5)
    if args.command == "train":
        seeds = [args.seed] if args.seed is not None else range(3)
        modes = [args.mode] if args.mode else MODES
        for seed in seeds:
            for mode in modes:
                train_run(dataset, m, mode, seed, device)
    elif args.command == "benchmark":
        training.set_random_seed(0)
        model = CNN1DClassifier(input_size=459).to(device)
        optimizer = torch.optim.Adam(model.parameters(), lr=.001)
        data = PhaseDataset(dataset, "train", "AP")
        tick = time.perf_counter()
        for index, (x, y, _) in enumerate(loader(data, 0, True)):
            optimizer.zero_grad(set_to_none=True)
            loss = nn.functional.cross_entropy(model(x.to(device)), y.to(device))
            loss.backward()
            optimizer.step()
            if index == 29:
                break
        if device.type == "mps":
            torch.mps.synchronize()
        print("benchmark", str(device), "seconds", time.perf_counter()-tick, "batches", index+1, flush=True)
    else:
        evaluate_runs(dataset, m, device)
        report(dataset)


if __name__ == "__main__":
    main()
