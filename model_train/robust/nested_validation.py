#!/usr/bin/env python3
"""Choose neural hyperparameters inside each outer training fold, then evaluate."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import numpy as np
import torch
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.robust.run_experiment import DEFAULT_OUTPUT, load, save_json, train_indices, evaluate_scores, digest
from model_train.robust.shared_temporal import CONFIG, get_tensor, fit_predict


def inner_folds(sessions, allowed):
    vals = [[] for _ in range(3)]
    for c in range(3):
        ids = sorted([i for i in allowed if sessions[i]["label_id"] == c],
                     key=lambda i: (sessions[i]["date"], sessions[i]["session_id"]))
        if len(ids) < 3:
            raise ValueError("not enough distinct training sessions per class")
        for dest, chunk in zip(vals, np.array_split(ids, 3)):
            dest.extend(chunk.tolist())
    return [(sorted(set(allowed)-set(val)), sorted(val)) for val in vals]


def main(out):
    if not torch.backends.mps.is_available():
        raise RuntimeError("MPS GPU access required")
    device = torch.device("mps")
    m, cache = load(out)
    sessions = m["sessions"]
    tensors = {w: get_tensor(out, w) for w in CONFIG["windows"]}
    definitions = [(f"date-{d}", [i for i,s in enumerate(sessions) if s["date"] == d]) for d in ("20260920", "20260919")]
    definitions += [(f"placement-{p}", [i for i,s in enumerate(sessions) if s["placement_id"] == p]) for p in ("P1", "P2", "P3")]
    folder = out/"neural"/"nested"
    folder.mkdir(exist_ok=True)
    selections = []
    for name, held in definitions:
        allowed = sorted(set(range(60))-set(held))
        folds = inner_folds(sessions, allowed)
        run_folder = folder/name
        run_folder.mkdir(exist_ok=True)
        candidates = []
        for w in CONFIG["windows"]:
            data, x = cache[w], tensors[w]
            for mode in CONFIG["modes"]:
                rows, all_val, all_scores = [], [], []
                for f, (tr, va) in enumerate(folds):
                    assert not (set(tr)|set(va)) & set(held)
                    assert not set(tr) & set(va)
                    train = train_indices(data, tr)
                    val = np.flatnonzero(np.isin(data["session"], va))
                    r, p = fit_predict(x, data, train, val, mode, 0, device, run_folder/f"w{w}-{mode}-inner{f}")
                    rows.append(r)
                    all_val.append(val)
                    all_scores.append(p)
                pooled = evaluate_scores(data, np.concatenate(all_val), np.concatenate(all_scores))
                f1s = [r["session"]["macro_f1"] for r in rows]
                candidates.append({"window": w, "mode": mode, "pooled": pooled,
                                   "selection_score": float(np.mean(f1s)-.25*np.std(f1s, ddof=1)),
                                   "inner_session_f1s": f1s})
                print(f"NESTED INNER {name} w={w} {mode} session={pooled['session']['macro_f1']:.3f}", flush=True)
        candidates.sort(key=lambda r: (r["selection_score"], min(r["pooled"]["session"]["recall"]),
                                      r["pooled"]["window"]["macro_f1"]), reverse=True)
        result = {"fold": name, "train": allowed, "held": held,
                  "inner_folds": [{"train": tr, "validation": va} for tr,va in folds],
                  "selected": candidates[0], "candidates": candidates}
        save_json(run_folder/"selection.json", result)
        selections.append(result)
    save_json(folder/"frozen-selections.json", selections)
    # No outer prediction is read or computed until every selection is frozen.
    evaluations, gates = [], {}
    for selection in selections:
        name, selected = selection["fold"], selection["selected"]
        w, mode = selected["window"], selected["mode"]
        data, x = cache[w], tensors[w]
        train = train_indices(data, selection["train"])
        val = np.flatnonzero(np.isin(data["session"], selection["held"]))
        these, probabilities = [], []
        for seed in (0, 1, 2):
            r, p = fit_predict(x, data, train, val, mode, seed, device, folder/name/f"outer-seed{seed}")
            r = dict(r, fold=name, selected_window=w, selected_mode=mode)
            # Independently confirm saved output and all split memberships.
            assert not set(r["train_session_ids"]) & set(r["validation_session_ids"])
            assert set(r["validation_session_ids"]) == set(selection["held"])
            recomputed = evaluate_scores(data, val, p)
            assert recomputed["session"] == r["session"] and recomputed["window"] == r["window"]
            these.append(r)
            probabilities.append(p)
            print(f"NESTED OUTER {name} w={w} {mode} seed={seed} session={r['session']['macro_f1']:.3f} window={r['window']['macro_f1']:.3f}", flush=True)
        ensemble = evaluate_scores(data, val, np.mean(probabilities, axis=0))
        f1s = [r["session"]["macro_f1"] for r in these]
        threshold = .8 if name.startswith("date") else .7
        gates[name] = {"selected_window": w, "selected_mode": mode,
                       "session_f1_mean": float(np.mean(f1s)), "session_f1_std": float(np.std(f1s, ddof=1)),
                       "window_f1_mean": float(np.mean([r["window"]["macro_f1"] for r in these])),
                       "min_class_recall": min(min(r["session"]["recall"]) for r in these),
                       "ensemble": ensemble,
                       "passed": all(v >= threshold for v in f1s) and np.std(f1s, ddof=1) <= .05
                       and (not name.startswith("date") or all(min(r["session"]["recall"]) >= .7 and r["window"]["macro_f1"] >= .75 for r in these))}
        gates[name]["passed"] = bool(gates[name]["passed"])
        evaluations.extend(these)
    save_json(folder/"summary.json", {"config": CONFIG, "selection_sha256": digest(folder/"frozen-selections.json"),
              "folds": gates, "acceptance_passed": all(g["passed"] for g in gates.values()),
              "results": evaluations, "independent_new_collection": False})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    torch.set_num_threads(2)
    with threadpool_limits(limits=2):
        main(args.output)
