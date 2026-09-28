#!/usr/bin/env python3
"""Reproducible session-isolated search, stress evaluation, and CSI inference."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import platform
from pathlib import Path
import sys
import time
import warnings

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import ExtraTreesClassifier, HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.svm import SVC
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.robust.features import VERSION, LENGTHS, SETS, eligible, extract_batch

DEFAULT_SOURCE = ROOT / "model_train/analysis/output/20260924-phase-ablation"
DEFAULT_OUTPUT = ROOT / "model_train/analysis/output/20260924-robust-three-class"
CLASSES = ["empty", "static", "motion"]
MODEL_SPECS = [
    {"kind": "linear", "C": .1}, {"kind": "linear", "C": 1.},
    {"kind": "rbf", "C": 1.}, {"kind": "rbf", "C": 10.},
    {"kind": "extra", "depth": 6}, {"kind": "hist", "depth": 3},
]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024*1024), b""):
            h.update(chunk)
    return h.hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def snapshot(out):
    folder = out / "source"
    folder.mkdir(exist_ok=True)
    hashes = {}
    for p in [Path(__file__), Path(__file__).with_name("features.py"),
              ROOT/"model_train/docs/model-training/robust-three-class-design.md",
              ROOT/"model_train/docs/preprocessing/robust-window-features.md"]:
        target = folder / p.name
        target.write_bytes(p.read_bytes())
        hashes[str(p.relative_to(ROOT))] = digest(p)
    return hashes


def prepare(source, out):
    out.mkdir(parents=True, exist_ok=False)
    (out / "cache").mkdir()
    manifest = json.loads((source / "manifest.json").read_text())
    if manifest["feature_version"] != "phase51-bandwise-ls-sincos-v1" or manifest["rx_order"] != [101, 102, 103]:
        raise ValueError("unexpected source feature contract")
    sessions = [{k: s[k] for k in ("key", "date", "session_id", "label", "label_id", "placement_id", "feature_sha256")}
                for s in manifest["sessions"]]
    if len({s["key"] for s in sessions}) != 60 or len(sessions) != 60:
        raise ValueError("expected 60 unique documented sessions")
    # Reconfirm label SSOT and the data file hash before computing any features.
    inventory = {s["key"]: s for s in json.loads((source / "inventory.json").read_text())}
    for s in sessions:
        original = ROOT / inventory[s["key"]]["path"] / "session.json"
        label = json.loads(original.read_text())["label"]
        if label != s["label"] or s["label_id"] != CLASSES.index(label):
            raise ValueError("label disagrees with original session manifest")
        if digest(source/"sessions"/s["key"]/"features.npy") != s["feature_sha256"]:
            raise ValueError("source cache changed")
    record = {"feature_version": VERSION, "source": str(source), "source_manifest_sha256": digest(source/"manifest.json"),
              "sessions": sessions, "lengths": list(LENGTHS), "max_train_windows_per_session": 48,
              "class_names": CLASSES, "source_files": snapshot(out),
              "runtime": {"python": sys.version, "platform": platform.platform(), "numpy": np.__version__,
                          "sklearn": sklearn.__version__}, "status": "preparing"}
    save_json(out/"manifest.json", record)
    buckets = {w: {k: [] for k in (*SETS, "session", "start", "y")} for w in LENGTHS}
    quality = []
    for si, s in enumerate(sessions):
        tick = time.monotonic()
        x = np.load(source/"sessions"/s["key"]/"features.npy", mmap_mode="r")
        for w in LENGTHS:
            starts, amp_bad, phase_bad = [], 0, 0
            candidates = list(range(0, len(x)-w+1, w//2))
            for start in candidates:
                a, p = eligible(x[start:start+w])
                if not a:
                    amp_bad += 1
                elif p.min() < .8:
                    phase_bad += 1
                else:
                    starts.append(start)
            if not starts:
                raise ValueError(f"lost entire session {s['key']} at {w}")
            for pos in range(0, len(starts), 8):
                batch_starts = starts[pos:pos+8]
                feats = extract_batch(np.stack([x[a:a+w] for a in batch_starts]))
                for key, value in feats.items():
                    buckets[w][key].append(value)
            buckets[w]["session"].append(np.full(len(starts), si, dtype=np.int64))
            buckets[w]["start"].append(np.asarray(starts, dtype=np.int64))
            buckets[w]["y"].append(np.full(len(starts), s["label_id"], dtype=np.int64))
            quality.append({"session_key": s["key"], "label": s["label"], "window": w,
                            "candidates": len(candidates), "kept": len(starts),
                            "amplitude_rejected": amp_bad, "phase_rejected": phase_bad})
        print(f"PREPARE {si+1}/60 {s['key']} {time.monotonic()-tick:.1f}s", flush=True)
    record["caches"] = {}
    for w, parts in buckets.items():
        path = out/"cache"/f"w{w}.npz"
        arrays = {k: np.concatenate(v) for k, v in parts.items()}
        np.savez_compressed(path, **arrays)
        record["caches"][str(w)] = {"sha256": digest(path), "windows": len(arrays["y"]),
                                    "features": {k: arrays[k].shape[1] for k in SETS}}
    record["status"] = "prepared"
    save_json(out/"quality.json", quality)
    save_json(out/"manifest.json", record)


def load(out):
    m = json.loads((out/"manifest.json").read_text())
    if m["status"] != "prepared" or m["feature_version"] != VERSION:
        raise ValueError("incomplete or incompatible feature preparation")
    cache = {}
    for w in LENGTHS:
        p = out/"cache"/f"w{w}.npz"
        if digest(p) != m["caches"][str(w)]["sha256"]:
            raise ValueError("feature cache hash mismatch")
        with np.load(p) as z:
            cache[w] = {k: z[k] for k in z.files}
    return m, cache


def metrics(y, pred):
    cm = confusion_matrix(y, pred, labels=[0, 1, 2])
    recall = np.diag(cm) / np.maximum(cm.sum(1), 1)
    precision = np.diag(cm) / np.maximum(cm.sum(0), 1)
    f1 = 2 * recall * precision / np.maximum(recall + precision, 1e-12)
    return {"macro_f1": float(f1.mean()), "accuracy": float(np.trace(cm)/max(cm.sum(), 1)),
            "recall": recall.tolist(), "confusion": cm.tolist(), "n": int(cm.sum())}


def evaluate_scores(data, indices, scores):
    if len(indices) != len(scores) or not np.isfinite(scores).all():
        raise ValueError("score alignment or finiteness failed")
    rows = []
    for si in np.unique(data["session"][indices]):
        here = data["session"][indices] == si
        truth = np.unique(data["y"][indices][here])
        if len(truth) != 1:
            raise ValueError("inconsistent within-session truth")
        p = scores[here].mean(0)
        rows.append({"session": int(si), "truth": int(truth[0]), "prediction": int(p.argmax()),
                     "scores": p.tolist(), "windows": int(here.sum())})
    return {"window": metrics(data["y"][indices], scores.argmax(1)),
            "session": metrics([r["truth"] for r in rows], [r["prediction"] for r in rows]),
            "sessions": rows}


def train_indices(data, sessions):
    result = []
    for s in sorted(sessions):
        ids = np.flatnonzero(data["session"] == s)
        take = np.linspace(0, len(ids)-1, min(48, len(ids))).round().astype(int)
        result.extend(ids[take].tolist())
    return np.asarray(result, dtype=np.int64)


def weights(data, indices):
    counts = Counter(data["session"][indices])
    class_sessions = Counter(int(data["y"][np.flatnonzero(data["session"] == s)[0]]) for s in counts)
    return np.asarray([len(indices)/(3*class_sessions[int(y)]*counts[int(s)])
                       for s, y in zip(data["session"][indices], data["y"][indices])])


def make_model(spec, seed=0):
    kind = spec["kind"]
    if kind == "linear":
        clf = LogisticRegression(C=spec["C"], max_iter=1200, solver="lbfgs", random_state=seed)
    elif kind == "rbf":
        clf = SVC(C=spec["C"], gamma="scale", kernel="rbf", probability=False, random_state=seed)
    elif kind == "extra":
        clf = ExtraTreesClassifier(n_estimators=200, max_depth=spec["depth"], min_samples_leaf=8,
                                   max_features=.7, n_jobs=2, random_state=seed)
    elif kind == "hist":
        clf = HistGradientBoostingClassifier(max_iter=120, max_leaf_nodes=7, max_depth=spec["depth"],
                                             learning_rate=.05, min_samples_leaf=20,
                                             l2_regularization=10, early_stopping=False, random_state=seed)
    else:
        raise ValueError(kind)
    return make_pipeline(StandardScaler(), clf)


def fit(model, data, feature_set, ids):
    sample_weight = weights(data, ids)
    x = data[feature_set][ids]
    if not np.isfinite(x).all():
        raise ValueError("non-finite training feature")
    # Apple Accelerate can emit spurious matmul FPE flags. Check independently
    # without BLAS instead of globally disabling numerical warnings.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        model.fit(x, data["y"][ids], standardscaler__sample_weight=sample_weight,
                  **{next(reversed(model.named_steps))+"__sample_weight": sample_weight})
    ref_x = np.asarray(x, np.float64)
    # StandardScaler casts sample weights to its input dtype before accumulation.
    ref_weights = sample_weight.astype(x.dtype).astype(np.float64)
    ref_mean = np.einsum("n,nf->f", ref_weights, ref_x, optimize=False)/ref_weights.sum()
    ref_var = np.einsum("n,nf->f", ref_weights, (ref_x-ref_mean)**2, optimize=False)/ref_weights.sum()
    scaler = model.named_steps["standardscaler"]
    np.testing.assert_allclose(scaler.mean_, ref_mean, rtol=1e-10, atol=1e-10)
    np.testing.assert_allclose(scaler.var_, ref_var, rtol=1e-9, atol=1e-10)
    if not np.isfinite(scaler.transform(x)).all():
        raise ValueError("non-finite normalized feature")
    model.numerical_audit_ = {"mean_reference_max_error": float(np.max(np.abs(scaler.mean_-ref_mean))),
                              "variance_reference_max_error": float(np.max(np.abs(scaler.var_-ref_var))),
                              "warnings": dict(Counter(str(w.message) for w in caught))}
    for warning in caught:
        if "encountered in matmul" not in str(warning.message):
            warnings.warn(str(warning.message), warning.category)
    return model


def score_model(model, x):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        if hasattr(model, "predict_proba"):
            p = model.predict_proba(x)
        else:
            # SVC OVR scores are normalized scores, not calibrated probabilities.
            raw = model.decision_function(x)
            p = np.exp(raw - raw.max(1, keepdims=True))
            p = p / p.sum(1, keepdims=True)
    if not np.isfinite(p).all() or (p < 0).any():
        raise ValueError("invalid model scores")
    np.testing.assert_allclose(p.sum(1), 1, atol=1e-6)
    clf = model.steps[-1][1]
    if isinstance(clf, LogisticRegression):
        z = model.named_steps["standardscaler"].transform(x)
        ref = np.einsum("nf,cf->nc", z, clf.coef_, optimize=False)+clf.intercept_
        ref = np.exp(ref-ref.max(1, keepdims=True))
        ref /= ref.sum(1, keepdims=True)
        np.testing.assert_allclose(p, ref, atol=1e-8, rtol=1e-7)
    for warning in caught:
        if "encountered in matmul" not in str(warning.message):
            warnings.warn(str(warning.message), warning.category)
    return p


def development_folds(sessions):
    dev = [i for i, s in enumerate(sessions) if s["date"] == "20260919"]
    folds = [[] for _ in range(5)]
    for label in range(3):
        members = sorted([i for i in dev if sessions[i]["label_id"] == label],
                         key=lambda i: sessions[i]["session_id"])
        if len(members) != 10:
            raise ValueError("unexpected development class/session inventory")
        for j, i in enumerate(members):
            folds[j//2].append(i)
    return [(sorted(set(dev)-set(test)), sorted(test)) for test in folds]


def candidate_id(w, fs, spec):
    desc = str(spec.get("C", spec.get("depth"))).replace(".", "p")
    return f"w{w}-{fs}-{spec['kind']}-{desc}"


def search(out):
    m, caches = load(out)
    folds = development_folds(m["sessions"])
    folder = out/"development"
    folder.mkdir(exist_ok=True)
    protocol = {"development_date": "20260919", "folds": [
        {"train": [m["sessions"][i]["key"] for i in tr], "validation": [m["sessions"][i]["key"] for i in va]}
        for tr, va in folds], "models": MODEL_SPECS, "lengths": list(LENGTHS), "feature_sets": list(SETS),
        "selection": "mean session F1 minus 0.25 * fold std; then minimum recall; then window F1"}
    save_json(out/"search-protocol.json", protocol)
    candidates = []
    for w in LENGTHS:
        data = caches[w]
        for fs in SETS:
            for spec in MODEL_SPECS:
                key = candidate_id(w, fs, spec)
                path = folder/f"{key}.json"
                if path.exists():
                    candidates.append(json.loads(path.read_text()))
                    continue
                tick = time.monotonic()
                results, indices, predictions = [], [], []
                for tr, va in folds:
                    assert not set(tr)&set(va)
                    train = train_indices(data, tr)
                    val = np.flatnonzero(np.isin(data["session"], va))
                    model = fit(make_model(spec), data, fs, train)
                    scores = score_model(model, data[fs][val])
                    results.append(evaluate_scores(data, val, scores))
                    indices.append(val)
                    predictions.append(scores)
                pooled = evaluate_scores(data, np.concatenate(indices), np.concatenate(predictions))
                f1s = [r["session"]["macro_f1"] for r in results]
                result = {"id": key, "window": w, "feature_set": fs, "model": spec,
                          "folds": results, "pooled": pooled, "mean_session_f1": float(np.mean(f1s)),
                          "std_session_f1": float(np.std(f1s, ddof=1)),
                          "selection_score": float(np.mean(f1s)-.25*np.std(f1s, ddof=1)),
                          "seconds": time.monotonic()-tick}
                save_json(path, result)
                candidates.append(result)
                print(f"SEARCH {len(candidates)}/{len(LENGTHS)*len(SETS)*len(MODEL_SPECS)} {key} "
                      f"session={pooled['session']['macro_f1']:.3f} window={pooled['window']['macro_f1']:.3f} "
                      f"score={result['selection_score']:.3f} {result['seconds']:.1f}s", flush=True)
    candidates.sort(key=lambda r: (r["selection_score"], min(r["pooled"]["session"]["recall"]),
                                   r["pooled"]["window"]["macro_f1"]), reverse=True)
    save_json(out/"leaderboard.json", candidates)
    selected = {k: candidates[0][k] for k in ("id", "window", "feature_set", "model", "selection_score", "pooled")}
    selected["selected_using"] = "20260919 development only; frozen before any new external evaluation"
    selected["leaderboard_sha256"] = digest(out/"leaderboard.json")
    save_json(out/"selected.json", selected)
    print("SELECTED " + selected["id"], flush=True)


def bootstrap_sessions(rows, seed=274, iterations=2000):
    rng = np.random.default_rng(seed)
    groups = [[r for r in rows if r["truth"] == c] for c in range(3)]
    values = []
    for _ in range(iterations):
        draw = [group[i] for group in groups for i in rng.integers(len(group), size=len(group))]
        values.append(metrics([r["truth"] for r in draw], [r["prediction"] for r in draw])["macro_f1"])
    return np.quantile(values, [.025, .975]).tolist()


def stress(out):
    m, caches = load(out)
    selected = json.loads((out/"selected.json").read_text())
    w, fs, spec = selected["window"], selected["feature_set"], selected["model"]
    data, sessions = caches[w], m["sessions"]
    definitions = [(f"date-{date}", [i for i,s in enumerate(sessions) if s["date"] == date])
                   for date in ("20260920", "20260919")]
    definitions += [(f"placement-{p}", [i for i,s in enumerate(sessions) if s["placement_id"] == p])
                    for p in ("P1", "P2", "P3")]
    results = []
    folder = out/"evaluation"
    folder.mkdir(exist_ok=True)
    for name, held in definitions:
        tr = sorted(set(range(len(sessions)))-set(held))
        train, val = train_indices(data, tr), np.flatnonzero(np.isin(data["session"], held))
        for seed in (0, 1, 2):
            stem = folder/f"{name}-seed{seed}"
            if stem.with_suffix(".json").exists():
                results.append(json.loads(stem.with_suffix(".json").read_text()))
                continue
            model = fit(make_model(spec, seed), data, fs, train)
            p = score_model(model, data[fs][val])
            result = evaluate_scores(data, val, p)
            for row in result["sessions"]:
                row["session_key"] = sessions[row["session"]]["key"]
            result.update({"fold": name, "seed": seed, "selected_id": selected["id"],
                           "numerical_audit": model.numerical_audit_,
                           "train_sessions": [sessions[i]["key"] for i in tr],
                           "held_sessions": [sessions[i]["key"] for i in held]})
            np.savez_compressed(stem.with_suffix(".npz"), indices=val, scores=p, truth=data["y"][val])
            joblib.dump(model, stem.with_suffix(".joblib"))
            save_json(stem.with_suffix(".json"), result)
            results.append(result)
            print(f"EVALUATE {name} seed={seed} session={result['session']['macro_f1']:.3f} "
                  f"window={result['window']['macro_f1']:.3f} recall={result['session']['recall']}", flush=True)
    gates = {}
    for name, _ in definitions:
        rs = [r for r in results if r["fold"] == name]
        threshold = .8 if name.startswith("date") else .7
        gates[name] = {"session_f1_mean": float(np.mean([r["session"]["macro_f1"] for r in rs])),
                       "session_f1_std": float(np.std([r["session"]["macro_f1"] for r in rs], ddof=1)),
                       "min_class_recall": float(min(min(r["session"]["recall"]) for r in rs)),
                       "window_f1_mean": float(np.mean([r["window"]["macro_f1"] for r in rs])),
                       "session_bootstrap_95_seed0": bootstrap_sessions(rs[0]["sessions"])}
        g = gates[name]
        g["passed"] = (all(r["session"]["macro_f1"] >= threshold for r in rs)
                         and g["session_f1_std"] <= .05
                         and (not name.startswith("date") or
                              (g["min_class_recall"] >= .7 and all(r["window"]["macro_f1"] >= .75 for r in rs))))
    save_json(out/"stress-summary.json", {"selected": selected, "folds": gates,
              "acceptance_passed": all(g["passed"] for g in gates.values()),
              "fresh_external_validation_available": False,
              "status": "EXPERIMENTAL; no deployment stability claim", "results": results})
    # Refit on all available data for a portable research candidate. Its training
    # score is deliberately not reported as a validation estimate.
    bundle = {"feature_version": VERSION, "window": w, "feature_set": fs, "class_names": CLASSES,
              "selection": selected, "acceptance_passed": all(g["passed"] for g in gates.values()),
              "status": "EXPERIMENTAL", "training_sessions": [s["key"] for s in sessions],
              "models": [fit(make_model(spec, seed), data, fs, train_indices(data, range(60))) for seed in (0, 1, 2)]}
    joblib.dump(bundle, out/"candidate.joblib")
    loaded = joblib.load(out/"candidate.joblib")
    np.testing.assert_allclose(score_model(loaded["models"][0], data[fs][:20]),
                               score_model(bundle["models"][0], data[fs][:20]), rtol=0, atol=1e-12)
    save_json(out/"candidate-metadata.json", {k: v for k, v in bundle.items() if k != "models"})
    print("GATES " + json.dumps(gates), flush=True)


def infer(bundle_path, features_path):
    b = joblib.load(bundle_path)
    if b["feature_version"] != VERSION:
        raise ValueError("incompatible feature version")
    x = np.load(features_path, mmap_mode="r")
    w, kept, probs = b["window"], [], []
    for start in range(0, len(x)-w+1, w//2):
        a, p = eligible(x[start:start+w])
        if not a or p.min() < .8:
            continue
        f = extract_batch(x[None, start:start+w])[b["feature_set"]]
        probs.append(np.mean([score_model(m, f)[0] for m in b["models"]], axis=0))
        kept.append(start)
    if not kept:
        raise ValueError("no valid windows; no prediction")
    p = np.mean(probs, axis=0)
    print(json.dumps({"status": b["status"], "prediction": b["class_names"][int(p.argmax())],
                       "mean_scores": dict(zip(b["class_names"], p.tolist())),
                       "valid_windows": len(kept), "window_seconds": w/100,
                       "scores_are_calibrated": False}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "search", "stress", "infer"))
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--features", type=Path)
    args = parser.parse_args()
    with threadpool_limits(limits=2):
        if args.command == "prepare":
            prepare(args.source, args.output)
        elif args.command == "search":
            search(args.output)
        elif args.command == "stress":
            stress(args.output)
        elif args.command == "infer":
            if args.features is None:
                parser.error("infer needs --features pointing at an aligned phase51 features.npy")
            infer(args.output/"candidate.joblib", args.features)


if __name__ == "__main__":
    main()
