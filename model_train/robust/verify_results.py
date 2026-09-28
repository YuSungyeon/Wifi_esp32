#!/usr/bin/env python3
"""Independently reproduce held-session metrics and CPU checkpoint predictions."""
from __future__ import annotations

from collections import Counter
import argparse
import json
from pathlib import Path
import shutil
import sys
import time

import joblib
import numpy as np
import torch
from scipy.stats import norm
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.robust.run_experiment import DEFAULT_OUTPUT, digest, load, evaluate_scores, score_model, save_json
from model_train.robust.shared_temporal import SharedEncoder, get_tensor, temporal_input, amplitude_input
from model_train.robust.predict import load_raw_amplitude, load_bundle, predict_amplitude


def wilson(successes, total):
    z = float(norm.ppf(.975))
    p = successes/total
    center = (p+z*z/(2*total))/(1+z*z/total)
    half = z*np.sqrt(p*(1-p)/total+z*z/(4*total*total))/(1+z*z/total)
    return [float(center-half), float(center+half)]


def main(out):
    m, cache = load(out)
    source = Path(m["source"])
    inventory = json.loads((source/"inventory.json").read_text())
    all_hashes = []
    for s in inventory:
        for f in s["files"]:
            actual = digest(ROOT/s["path"]/f"device_{f['rx']}.csi")
            if actual != f["sha256"]:
                raise ValueError("original raw data changed")
            all_hashes.append(actual)
    if len(set(all_hashes)) != len(all_hashes):
        raise ValueError("duplicate raw file across sessions/receivers")
    x = get_tensor(out, 3000)
    neural_inputs = {3000: x, 1000: get_tensor(out, 1000)}
    checked_windows = 0
    for si, s in enumerate(m["sessions"]):
        raw = np.load(source/"sessions"/s["key"]/"features.npy", mmap_mode="r")
        for i in np.flatnonzero(cache[3000]["session"] == si):
            start = cache[3000]["start"][i]
            reference = temporal_input(raw[start:start+3000])
            np.testing.assert_array_equal(x[i], reference)
            np.testing.assert_array_equal(reference[..., 0], amplitude_input(raw[start:start+3000, :153])[..., 0])
            checked_windows += 1
    rows = []
    for family in ("classical", "neural", "neural_nested"):
        folder = out if family == "classical" else out/"neural"
        selected = json.loads((folder/"selected.json").read_text())
        summary_path = folder/"nested"/"summary.json" if family == "neural_nested" else folder/"stress-summary.json"
        summary = json.loads(summary_path.read_text())
        for reported in summary["results"]:
            window = reported["selected_window"] if family == "neural_nested" else selected["window"]
            data = cache[window]
            stem = (folder/"nested"/reported["fold"]/f"outer-seed{reported['seed']}" if family == "neural_nested"
                    else folder/"evaluation"/f"{reported['fold']}-seed{reported['seed']}")
            saved = np.load(stem.with_suffix(".npz"))
            indices, probabilities = saved["indices"], saved["scores"]
            np.testing.assert_array_equal(saved["truth"], data["y"][indices])
            held = set(data["session"][indices].tolist())
            if family == "classical":
                train = {i for i,s in enumerate(m["sessions"]) if s["key"] in reported["train_sessions"]}
                model = joblib.load(stem.with_suffix(".joblib"))
                reproduction = score_model(model, data[selected["feature_set"]][indices])
            else:
                train = set(reported["train_session_ids"])
                ck = torch.load(stem.with_suffix(".pt"), map_location="cpu", weights_only=True)
                model = SharedEncoder(ck["mode"])
                model.load_state_dict(ck["state_dict"])
                model.eval()
                pieces = []
                with torch.no_grad():
                    for pos in range(0, len(indices), 32):
                        xb = torch.from_numpy(np.array(neural_inputs[window][indices[pos:pos+32]]))
                        pieces.append(model(xb).softmax(1).numpy())
                reproduction = np.concatenate(pieces)
            if train & held:
                raise ValueError("train/held session leakage")
            if reported["fold"].startswith("date"):
                field, wanted = "date", reported["fold"].split("-", 1)[1]
            else:
                field, wanted = "placement_id", reported["fold"].split("-", 1)[1]
            assert all(m["sessions"][s][field] == wanted for s in held)
            assert all(m["sessions"][s][field] != wanted for s in train)
            np.testing.assert_allclose(reproduction, probabilities, atol=5e-5, rtol=1e-4)
            np.testing.assert_array_equal(reproduction.argmax(1), probabilities.argmax(1))
            measured = evaluate_scores(data, indices, probabilities)
            if measured["session"] != reported["session"] or measured["window"] != reported["window"]:
                raise ValueError("reported metric mismatch")
            correct = sum(s["truth"] == s["prediction"] for s in measured["sessions"])
            rows.append({"family": family, "fold": reported["fold"], "seed": reported["seed"],
                         "windows_verified": len(indices), "sessions_verified": len(held),
                         "max_cpu_score_difference": float(np.max(np.abs(reproduction-probabilities))),
                         "session_accuracy_wilson_95": wilson(correct, len(held)),
                         "session": measured["session"], "window": measured["window"]})
            print(f"VERIFIED {family} {reported['fold']} seed={reported['seed']} windows={len(indices)}", flush=True)
    ensembles = []
    for fold in ("date-20260920", "date-20260919", "placement-P1", "placement-P2", "placement-P3"):
        parts = [np.load(out/"neural"/"evaluation"/f"{fold}-seed{s}.npz") for s in (0, 1, 2)]
        indices = parts[0]["indices"]
        for p in parts[1:]:
            np.testing.assert_array_equal(p["indices"], indices)
        p = np.mean([v["scores"] for v in parts], axis=0)
        r = evaluate_scores(cache[3000], indices, p)
        first_positions = [np.flatnonzero(cache[3000]["session"][indices] == s)[0]
                           for s in np.unique(cache[3000]["session"][indices])]
        r["first_valid_window"] = evaluate_scores(cache[3000], indices[first_positions], p[first_positions])["window"]
        for s in r["sessions"]:
            here = cache[3000]["session"][indices] == s["session"]
            s["window_accuracy"] = float(np.mean(p[here].argmax(1) == s["truth"]))
            s["session_key"] = m["sessions"][s["session"]]["key"]
        r["fold"] = fold
        ensembles.append(r)
    save_json(out/"neural"/"ensemble-evaluation.json", ensembles)
    deployment_checks = []
    bundle, models = load_bundle(out/"neural"/"model.pt")
    for key in ("20260920-s45", "20260920-s41", "20260920-s31"):
        original = next(s for s in inventory if s["key"] == key)
        amp, quality = load_raw_amplitude(ROOT/original["path"])
        cached = np.load(source/"sessions"/key/"features.npy", mmap_mode="r")[:, :153]
        np.testing.assert_array_equal(amp, cached)
        tick = time.monotonic()
        a = predict_amplitude(amp, bundle, models)
        elapsed = time.monotonic()-tick
        b = predict_amplitude(cached, bundle, models)
        if a != b:
            raise ValueError("raw-session and cached inference differ")
        deployment_checks.append({"session_key": key, "prediction": a["prediction"],
                                  "recorded_label": original["label"], "windows": a["valid_windows"],
                                  "elapsed_inference_seconds": elapsed, "quality": quality,
                                  "purpose": "serialization/preprocessing smoke check on final training data, not held-out accuracy"})
        print(f"RAW INFERENCE {key} prediction={a['prediction']} {elapsed:.3f}s", flush=True)
    frozen = out/"source-final"
    frozen.mkdir(exist_ok=True)
    source_hashes = {}
    files = list(Path(__file__).parent.glob("*.py")) + [
        ROOT/"requirements-robust.txt", ROOT/"tests/test_robust_three_class.py",
        ROOT/"model_train/docs/model-training/robust-three-class-design.md",
        ROOT/"model_train/docs/model-training/robust-three-class-report.md",
        ROOT/"model_train/docs/preprocessing/robust-window-features.md"]
    for file in files:
        shutil.copyfile(file, frozen/file.name)
        source_hashes[str(file.relative_to(ROOT))] = digest(file)
    save_json(out/"verification.json", {"raw_files_verified": len(all_hashes), "raw_hashes_all_unique": True,
              "neural_windows_rebuilt_exactly": checked_windows, "evaluations": rows,
              "verified": True, "neural_source_sha256": digest(Path(__file__).with_name("shared_temporal.py")),
              "deployment_checks": deployment_checks, "model_sha256": digest(out/"neural"/"model.pt"),
              "source_final_hashes": source_hashes, "torch_version": str(torch.__version__),
              "note": "Wilson intervals describe observed session accuracy under binomial assumptions; two dates do not establish new-domain performance."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    torch.set_num_threads(2)
    with threadpool_limits(limits=2):
        main(args.output)
