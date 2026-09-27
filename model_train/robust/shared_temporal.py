#!/usr/bin/env python3
"""Shared temporal encoder using window-local amplitude and circular phase."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys
import time

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
from threadpoolctl import threadpool_limits

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from model_train.robust.run_experiment import (
    DEFAULT_OUTPUT, save_json, digest, load, development_folds, train_indices, weights,
    evaluate_scores, bootstrap_sessions,
)

CONFIG = {"version": "shared-window-local-v1", "epochs": 15, "lr": .001,
          "weight_decay": .01, "label_smoothing": .1, "batch_size": 32, "dropout": .3,
          "windows": [1000, 3000], "modes": ["A", "P", "AP"], "tones_per_rx": 17}


def temporal_input(x):
    """(T,459) -> (T/10,51,3). Fixed gain factors; no fitted normalization."""
    t = len(x)
    amp = np.asarray(x[:, :153], np.float32)
    if not np.isfinite(amp).all() or t % 10:
        raise ValueError("invalid amplitude window")
    # Keep each receiver equally represented; the same subset at train/inference.
    amp = amp[:, ::3]
    relative = amp / (amp.mean(0, keepdims=True)+.5)
    a = relative.reshape(t//10, 10, 51).mean(1)
    a -= a.mean(0, keepdims=True)
    sine = np.asarray(x[:, 153:306], np.float32)[:, ::3]
    cosine = np.asarray(x[:, 306:], np.float32)[:, ::3]
    valid = np.isfinite(sine) & np.isfinite(cosine)
    n = valid.reshape(t//10, 10, 51).sum(1)
    vals = [np.where(valid, v, 0).reshape(t//10, 10, 51).sum(1)/np.maximum(n, 1)
            for v in (cosine, sine)]
    z = vals[0] + 1j*vals[1]
    observed = n >= 5
    center = np.where(observed, z, 0).sum(0) / np.maximum(observed.sum(0), 1)
    rotated = (z-center[None]) * (np.conj(center)/np.maximum(np.abs(center), 1e-6))[None]
    rotated = np.where(observed, rotated, 0)
    value = np.stack([a*3, rotated.imag*10, rotated.real*10], axis=-1)
    return np.clip(value, -20, 20).astype(np.float32)


def amplitude_input(amp):
    """Amplitude-only inference for the selected A model; phase is unnecessary."""
    amp = np.asarray(amp, dtype=np.float32)
    if amp.ndim != 2 or amp.shape[1] != 153 or len(amp) % 10 or not np.isfinite(amp).all():
        raise ValueError("expected finite (T,153) amplitude, T divisible by 10")
    a = amp[:, ::3]
    relative = a / (a.mean(0, keepdims=True)+.5)
    slow = relative.reshape(len(amp)//10, 10, 51).mean(1)
    slow -= slow.mean(0, keepdims=True)
    value = np.zeros((len(slow), 51, 3), dtype=np.float32)
    value[..., 0] = np.clip(slow*3, -20, 20)
    return value


class SharedEncoder(nn.Module):
    def __init__(self, mode="AP", dropout=.3):
        super().__init__()
        self.mode = mode
        channels = {"A": 1, "P": 2, "AP": 3}[mode]
        self.encoder = nn.Sequential(
            nn.Conv1d(channels, 8, 5, padding=2, bias=False), nn.BatchNorm1d(8), nn.ReLU(), nn.AvgPool1d(2),
            nn.Conv1d(8, 16, 5, padding=2, bias=False), nn.BatchNorm1d(16), nn.ReLU(), nn.AvgPool1d(2),
            nn.Conv1d(16, 16, 3, padding=1, bias=False), nn.BatchNorm1d(16), nn.ReLU())
        self.head = nn.Sequential(nn.Linear(96, 32), nn.ReLU(), nn.Dropout(dropout), nn.Linear(32, 3))

    def forward(self, x):
        # (B,time,51,3), series do not have receiver/tone identities in the network.
        if self.mode == "A":
            x = x[..., :1]
        elif self.mode == "P":
            x = x[..., 1:]
        if self.training:
            # One gain per RX, shared over all its tones/time samples.
            gain = .8 + .4*torch.rand((len(x), 1, 3, 1, 1), device=x.device)
            x = (x.reshape(len(x), x.shape[1], 3, 17, x.shape[-1])*gain).flatten(2, 3)
            keep = torch.randperm(x.shape[2], device=x.device)[:39]
            x = x[:, :, keep]
        b, t, f, d = x.shape
        h = self.encoder(x.permute(0, 2, 3, 1).reshape(b*f, d, t))
        series = torch.cat([h.mean(2), h.std(2, unbiased=False)], 1).reshape(b, f, 32)
        pooled = torch.cat([series.mean(1), series.std(1, unbiased=False), series.amax(1)], 1)
        return self.head(pooled)


def prepare_cache(out):
    m, caches = load(out)
    folder = out/"neural"
    folder.mkdir(exist_ok=True)
    source = Path(m["source"])
    for w in CONFIG["windows"]:
        path = folder/f"input-w{w}.npy"
        if path.exists():
            continue
        data = caches[w]
        values = np.lib.format.open_memmap(path, mode="w+", dtype=np.float32,
                                           shape=(len(data["y"]), w//10, 51, 3))
        for si, s in enumerate(m["sessions"]):
            raw = np.load(source/"sessions"/s["key"]/"features.npy", mmap_mode="r")
            if digest(source/"sessions"/s["key"]/"features.npy") != s["feature_sha256"]:
                raise ValueError("source changed since statistical feature preparation")
            for index in np.flatnonzero(data["session"] == si):
                start = data["start"][index]
                values[index] = temporal_input(raw[start:start+w])
        values.flush()
        save_json(folder/f"input-w{w}.json", {"version": CONFIG["version"], "sha256": digest(path),
                                            "shape": list(values.shape), "statistical_cache_sha256": m["caches"][str(w)]["sha256"]})
        print(f"NEURAL CACHE w={w} shape={values.shape}", flush=True)
    (folder/"shared_temporal.py").write_bytes(Path(__file__).read_bytes())
    save_json(folder/"config.json", CONFIG)


def seed_all(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)


def get_tensor(out, w):
    path = out/"neural"/f"input-w{w}.npy"
    meta = json.loads(path.with_suffix(".json").read_text())
    if digest(path) != meta["sha256"] or meta["version"] != CONFIG["version"]:
        raise ValueError("neural cache hash/version mismatch")
    return np.load(path, mmap_mode="r")


def fit_predict(x, data, train, val, mode, seed, device, stem):
    if stem.with_suffix(".json").exists():
        r = json.loads(stem.with_suffix(".json").read_text())
        z = np.load(stem.with_suffix(".npz"))
        if not np.array_equal(z["indices"], val) or r["mode"] != mode or r["config"] != CONFIG:
            raise ValueError("existing run incompatible")
        return r, z["scores"]
    seed_all(seed)
    model = SharedEncoder(mode=mode).to(device)
    criterion = nn.CrossEntropyLoss(label_smoothing=CONFIG["label_smoothing"], reduction="none")
    optimizer = torch.optim.AdamW(model.parameters(), lr=CONFIG["lr"], weight_decay=CONFIG["weight_decay"])
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, CONFIG["epochs"])
    ds = TensorDataset(torch.from_numpy(np.array(x[train])), torch.from_numpy(data["y"][train]),
                       torch.from_numpy(weights(data, train).astype(np.float32)))
    loader = DataLoader(ds, batch_size=32, shuffle=True, num_workers=0,
                        generator=torch.Generator().manual_seed(seed))
    history = []
    start = time.monotonic()
    for epoch in range(CONFIG["epochs"]):
        model.train()
        loss_sum, correct, count = 0., 0, 0
        for xb, yb, wb in loader:
            xb, yb, wb = xb.to(device), yb.to(device), wb.to(device)
            optimizer.zero_grad(set_to_none=True)
            logits = model(xb)
            loss = (criterion(logits, yb)*wb).mean()
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 2.)
            optimizer.step()
            loss_sum += float(loss.detach().cpu())*len(xb)
            correct += int((logits.argmax(1) == yb).sum().cpu())
            count += len(xb)
        scheduler.step()
        history.append({"epoch": epoch+1, "loss": loss_sum/count, "accuracy": correct/count})
        if epoch in (0, 4, 9, 14):
            print(f"NEURAL {stem.name} epoch={epoch+1}/15 train_acc={correct/count:.3f} elapsed={time.monotonic()-start:.1f}s", flush=True)
    model.eval()
    scores = []
    with torch.no_grad():
        for pos in range(0, len(val), 32):
            xb = torch.from_numpy(np.array(x[val[pos:pos+32]])).to(device)
            scores.append(model(xb).softmax(1).cpu().numpy())
    scores = np.concatenate(scores) if len(val) else np.empty((0, 3), np.float32)
    result = evaluate_scores(data, val, scores) if len(val) else {}
    result.update({"mode": mode, "seed": seed, "config": CONFIG, "epochs": CONFIG["epochs"],
                   "history": history, "seconds": time.monotonic()-start,
                   "parameters": sum(p.numel() for p in model.parameters()),
                   "train_session_ids": sorted(np.unique(data["session"][train]).tolist()),
                   "validation_session_ids": sorted(np.unique(data["session"][val]).tolist())})
    torch.save({"state_dict": {k:v.detach().cpu() for k,v in model.state_dict().items()},
                "mode": mode, "config": CONFIG, "status": "EXPERIMENTAL"}, stem.with_suffix(".pt"))
    np.savez_compressed(stem.with_suffix(".npz"), indices=val, scores=scores, truth=data["y"][val])
    save_json(stem.with_suffix(".json"), result)
    torch.mps.empty_cache() if device.type == "mps" else None
    return result, scores


def search(out, device):
    m, caches = load(out)
    folder = out/"neural"/"development"
    folder.mkdir(exist_ok=True)
    folds = development_folds(m["sessions"])
    candidates = []
    for w in CONFIG["windows"]:
        data, x = caches[w], get_tensor(out, w)
        for mode in CONFIG["modes"]:
            results, ids, scores = [], [], []
            key = f"w{w}-{mode}-shared"
            for f, (tr, va) in enumerate(folds):
                train = train_indices(data, tr)
                val = np.flatnonzero(np.isin(data["session"], va))
                r, p = fit_predict(x, data, train, val, mode, 0, device, folder/f"{key}-fold{f}")
                results.append(r)
                ids.append(val)
                scores.append(p)
            pooled = evaluate_scores(data, np.concatenate(ids), np.concatenate(scores))
            f1 = [r["session"]["macro_f1"] for r in results]
            row = {"id": key, "window": w, "mode": mode, "folds": results, "pooled": pooled,
                   "mean_session_f1": float(np.mean(f1)), "std_session_f1": float(np.std(f1, ddof=1)),
                   "selection_score": float(np.mean(f1)-.25*np.std(f1, ddof=1))}
            candidates.append(row)
            save_json(folder/f"{key}.json", row)
            print(f"NEURAL CANDIDATE {key} session={pooled['session']['macro_f1']:.3f} window={pooled['window']['macro_f1']:.3f}", flush=True)
    candidates.sort(key=lambda r: (r["selection_score"], min(r["pooled"]["session"]["recall"]),
                                  r["pooled"]["window"]["macro_f1"]), reverse=True)
    save_json(out/"neural"/"leaderboard.json", candidates)
    save_json(out/"neural"/"selected.json", {k:v for k,v in candidates[0].items() if k != "folds"})


def stress(out, device):
    m, caches = load(out)
    selected = json.loads((out/"neural"/"selected.json").read_text())
    w, mode = selected["window"], selected["mode"]
    data, x, sessions = caches[w], get_tensor(out, w), m["sessions"]
    defs = [(f"date-{d}", [i for i,s in enumerate(sessions) if s["date"] == d]) for d in ("20260920", "20260919")]
    defs += [(f"placement-{p}", [i for i,s in enumerate(sessions) if s["placement_id"] == p]) for p in ("P1", "P2", "P3")]
    folder = out/"neural"/"evaluation"
    folder.mkdir(exist_ok=True)
    results, gates = [], {}
    for name, held in defs:
        train = train_indices(data, sorted(set(range(60))-set(held)))
        val = np.flatnonzero(np.isin(data["session"], held))
        these = []
        for seed in (0, 1, 2):
            r, _ = fit_predict(x, data, train, val, mode, seed, device, folder/f"{name}-seed{seed}")
            r = dict(r, fold=name)
            for row in r["sessions"]:
                row["session_key"] = sessions[row["session"]]["key"]
            these.append(r)
            results.append(r)
            print(f"NEURAL EVAL {name} seed={seed} session={r['session']['macro_f1']:.3f} window={r['window']['macro_f1']:.3f}", flush=True)
        g = {"session_f1_mean": float(np.mean([r["session"]["macro_f1"] for r in these])),
             "session_f1_std": float(np.std([r["session"]["macro_f1"] for r in these], ddof=1)),
             "min_class_recall": min(min(r["session"]["recall"]) for r in these),
             "window_f1_mean": float(np.mean([r["window"]["macro_f1"] for r in these])),
             "session_bootstrap_95_seed0": bootstrap_sessions(these[0]["sessions"])}
        threshold = .8 if name.startswith("date") else .7
        g["passed"] = (all(r["session"]["macro_f1"] >= threshold for r in these)
                       and g["session_f1_std"] <= .05 and (not name.startswith("date") or
                       (g["min_class_recall"] >= .7 and all(r["window"]["macro_f1"] >= .75 for r in these))))
        gates[name] = g
    save_json(out/"neural"/"stress-summary.json", {"selected": selected, "folds": gates,
              "acceptance_passed": all(g["passed"] for g in gates.values()),
              "fresh_external_validation_available": False, "results": results})


def negative_control(out, device):
    m, caches = load(out)
    selected = json.loads((out/"neural"/"selected.json").read_text())
    w, mode = selected["window"], selected["mode"]
    original, x = caches[w], get_tensor(out, w)
    tr = [i for i,s in enumerate(m["sessions"]) if s["date"] == "20260919"]
    held = [i for i,s in enumerate(m["sessions"]) if s["date"] == "20260920"]
    train = train_indices(original, tr)
    val = np.flatnonzero(np.isin(original["session"], held))
    labels = np.asarray([m["sessions"][i]["label_id"] for i in tr])
    folder = out/"neural"/"negative-controls"
    folder.mkdir(exist_ok=True)
    rng, rows = np.random.default_rng(20260924), []
    for trial in range(49):
        shuffled = rng.permutation(labels)
        data = dict(original, y=original["y"].copy())
        for si, label in zip(tr, shuffled):
            data["y"][data["session"] == si] = label
        r, _ = fit_predict(x, data, train, val, mode, 0, device, folder/f"shuffle-{trial:02d}")
        rows.append({"trial": trial, "train_session_labels": shuffled.tolist(),
                     "session_f1": r["session"]["macro_f1"], "window_f1": r["window"]["macro_f1"]})
        print(f"NEGATIVE CONTROL {trial+1}/49 session_f1={rows[-1]['session_f1']:.3f}", flush=True)
    actual = json.loads((out/"neural/evaluation/date-20260920-seed0.json").read_text())["session"]["macro_f1"]
    summary = {"unit_of_permutation": "whole training session", "held_labels_unchanged": True,
               "actual_session_f1": actual, "mean_shuffled_session_f1": float(np.mean([r["session_f1"] for r in rows])),
               "max_shuffled_session_f1": max(r["session_f1"] for r in rows),
               "monte_carlo_tail_fraction": (1+sum(r["session_f1"] >= actual for r in rows))/50,
               "interpretation": "Leakage diagnostic for a fixed model; not selection-adjusted significance or new external validation.",
               "trials": rows}
    save_json(out/"neural"/"negative-control.json", summary)


def export(out, device):
    m, caches = load(out)
    selected = json.loads((out/"neural"/"selected.json").read_text())
    stress_result = json.loads((out/"neural"/"stress-summary.json").read_text())
    w, mode = selected["window"], selected["mode"]
    data, x = caches[w], get_tensor(out, w)
    train = train_indices(data, range(60))
    folder = out/"neural"/"final"
    folder.mkdir(exist_ok=True)
    states = []
    for seed in (0, 1, 2):
        stem = folder/f"seed{seed}"
        fit_predict(x, data, train, np.array([], dtype=np.int64), mode, seed, device, stem)
        states.append(torch.load(stem.with_suffix(".pt"), map_location="cpu", weights_only=True)["state_dict"])
    bundle = {"format_version": 1, "feature_version": CONFIG["version"], "config": CONFIG,
              "mode": mode, "window_frames": w, "stride_frames": w//2, "sample_rate_hz": 100,
              "rx_order": [101, 102, 103], "raw_tone_order": list(range(38,64))+list(range(2,27)),
              "class_names": ["empty", "static", "motion"], "state_dicts": states,
              "training_sessions": [s["key"] for s in m["sessions"]],
              "acceptance_passed": stress_result["acceptance_passed"],
              "status": "VALIDATED_ON_EXISTING_DATA; new-date deployment validation pending",
              "selected_using": selected["id"], "selection_scope": "selected within neural family by development CV; compared with classical family on reused diagnostics",
              "source_manifest_sha256": digest(out/"manifest.json")}
    torch.save(bundle, out/"neural"/"model.pt")
    save_json(out/"neural"/"model-metadata.json", {k:v for k,v in bundle.items() if k != "state_dicts"})
    print(f"EXPORTED {out/'neural'/'model.pt'}", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=("prepare", "search", "stress", "negative-control", "export"))
    p.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = p.parse_args()
    torch.set_num_threads(2)
    with threadpool_limits(limits=2):
        if args.command == "prepare":
            prepare_cache(args.output)
        else:
            if not torch.backends.mps.is_available():
                raise RuntimeError("MPS unavailable in this process; run with GPU access")
            {"search": search, "stress": stress, "negative-control": negative_control,
             "export": export}[args.command](args.output, torch.device("mps"))


if __name__ == "__main__":
    main()
