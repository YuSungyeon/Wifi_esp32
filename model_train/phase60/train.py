#!/usr/bin/env python3
"""위상 60초 모델 학습 — 체크포인트에 입력 계약과 평가 결과를 함께 저장한다.

    python model_train/phase60/train.py --name 15sessions --train 20260916 20260917 --eval 20260919 20260920
    python model_train/phase60/train.py --name 60sessions --train 20260919 20260920 --eval 20260916 20260917

학습이 끝나면 **학습에 쓰지 않은 날짜**로 바로 평가하고 그 숫자를 체크포인트에 넣는다.
파일만 열어도 무엇으로 학습했고 어디서 몇 점이었는지 알 수 있어야 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from model_train.cnn1d.shared_cnn import RX_ORDER, SharedTemporalCNN  # noqa: E402
from model_train.phase60 import features as F  # noqa: E402

HERE = Path(__file__).resolve().parent
RAW = ROOT / "mac_collector_output" / "raw"
CACHE = HERE / "cache"
CLASS_NAMES = ("empty", "static", "motion")
FORMAT_VERSION = 1


# ── 데이터 ────────────────────────────────────────────────────────────────────
def load_dates(dates: tuple, window: int, stride: int) -> dict:
    """{키: {phase, label, starts}}. 위상 추출이 느려 세션별로 캐시한다."""
    CACHE.mkdir(parents=True, exist_ok=True)
    out = {}
    for date in dates:
        for d in sorted((RAW / date).iterdir()):
            if not (d / "session.json").is_file():
                continue
            label = d.name.split("_")[1]
            if label not in CLASS_NAMES:
                continue
            key = f"{date}_{d.name.rsplit('_s', 1)[1]}"
            npz = CACHE / f"{key}.npz"
            if npz.exists():
                phase = np.load(npz)["phase"]
            else:
                try:
                    phase = F.session_phase(d)
                except ValueError as exc:
                    print(f"  제외 {key}: {exc}", flush=True)
                    continue
                np.savez(npz, phase=phase)
            out[key] = {"phase": phase, "label": CLASS_NAMES.index(label),
                        "starts": F.valid_starts(phase, window, stride), "dir": str(d)}
    return out


# ── 학습 ──────────────────────────────────────────────────────────────────────
def train_one(train: dict, seed: int, window: int, downsample: int, epochs: int,
              lr: float, batch: int, device: torch.device) -> nn.Module:
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = SharedTemporalCNN(dropout=0.2, groups=(F.N_COLS,), n_classes=3).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=lr)
    loss_fn = nn.CrossEntropyLoss()
    index = [(k, int(s)) for k, v in train.items() for s in v["starts"]]
    rng = np.random.default_rng(seed)
    for epoch in range(epochs):
        model.train()
        order = rng.permutation(len(index))
        for b in range(0, len(order), batch):
            rows = [index[i] for i in order[b:b + batch]]
            xb = np.concatenate([F.windows(train[k]["phase"], np.array([s]), window, downsample)
                                 for k, s in rows])
            yb = np.array([train[k]["label"] for k, _ in rows])
            opt.zero_grad()
            loss_fn(model(torch.from_numpy(xb).to(device)),
                    torch.from_numpy(yb).to(device)).backward()
            opt.step()
        print(f"    seed {seed} epoch {epoch + 1}/{epochs}", flush=True)
    return model.eval()


def evaluate(models: list, sessions: dict, window: int, downsample: int,
             device: torch.device) -> dict:
    """세션별 softmax 평균(seed 앙상블)으로 판정. 창 단위 정확도도 함께 낸다."""
    rows, cm = [], np.zeros((3, 3), int)
    with torch.no_grad():
        for key, v in sessions.items():
            probs = []
            for b in range(0, len(v["starts"]), 32):
                xb = np.ascontiguousarray(
                    F.windows(v["phase"], v["starts"][b:b + 32], window, downsample))
                x = torch.from_numpy(xb).to(device)
                probs.append(torch.stack([m(x).softmax(1) for m in models]).mean(0).cpu().numpy())
            probs = np.concatenate(probs)
            mean = probs.mean(0)
            pred = int(mean.argmax())
            cm[v["label"], pred] += 1
            rows.append({"session": key, "true": CLASS_NAMES[v["label"]],
                         "pred": CLASS_NAMES[pred],
                         "scores": {c: round(float(s), 4) for c, s in zip(CLASS_NAMES, mean)},
                         "window_acc": round(float((probs.argmax(1) == v["label"]).mean()), 4)})
    ok = sum(r["true"] == r["pred"] for r in rows)
    presence = sum((r["pred"] != "empty") == (r["true"] != "empty") for r in rows)
    es = [r for r in rows if r["true"] in ("empty", "static")]
    es_ok = sum((r["scores"]["empty"] >= r["scores"]["static"]) == (r["true"] == "empty")
                for r in es)
    return {"sessions": f"{ok}/{len(rows)}", "presence": f"{presence}/{len(rows)}",
            "empty_vs_static": f"{es_ok}/{len(es)}",
            "window_accuracy": round(float(np.mean([r["window_acc"] for r in rows])), 4),
            "confusion_matrix": cm.tolist(), "per_session": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True, help="체크포인트 이름 (model_<name>.pt)")
    ap.add_argument("--train", nargs="+", required=True, help="학습에 쓸 날짜 폴더")
    ap.add_argument("--eval", nargs="+", required=True, help="평가에 쓸 날짜 폴더 (학습과 겹치면 안 됨)")
    ap.add_argument("--window", type=int, default=6000)
    ap.add_argument("--downsample", type=int, default=20)
    ap.add_argument("--train-stride", type=int, default=30)
    ap.add_argument("--eval-stride", type=int, default=300)
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    overlap = set(args.train) & set(args.eval)
    if overlap:
        print(f"[중단] 학습과 평가 날짜가 겹친다: {sorted(overlap)}")
        return 1

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    print(f"[데이터] 학습 {args.train} / 평가 {args.eval} · device={device}", flush=True)
    train = load_dates(tuple(args.train), args.window, args.train_stride)
    test = load_dates(tuple(args.eval), args.window, args.eval_stride)
    n_win = sum(len(v["starts"]) for v in train.values())
    print(f"  학습 {len(train)}세션 (창 {n_win}개) → 평가 {len(test)}세션", flush=True)

    models, t0 = [], time.time()
    for seed in args.seeds:
        s0 = time.time()
        models.append(train_one(train, seed, args.window, args.downsample,
                                args.epochs, args.lr, args.batch, device))
        print(f"  seed {seed} 완료 ({time.time() - s0:.0f}s)", flush=True)

    print("[평가] 학습에 쓰지 않은 날짜로 판정", flush=True)
    metrics = evaluate(models, test, args.window, args.downsample, device)
    print(f"  세션 {metrics['sessions']} · 사람 있음/없음 {metrics['presence']} · "
          f"빈방vs정지 {metrics['empty_vs_static']} · 창 정답률 {metrics['window_accuracy']}",
          flush=True)

    out = HERE / f"model_{args.name}.pt"
    torch.save({
        "format_version": FORMAT_VERSION,
        "feature_version": F.FEATURE_VERSION,
        "mode": "P",
        "window_frames": args.window,
        "stride_frames": args.eval_stride,
        "downsample": args.downsample,
        "sample_rate_hz": 100,
        "rx_order": list(RX_ORDER),
        "raw_tone_order": F.RAW_TONE_ORDER.tolist(),
        "class_names": list(CLASS_NAMES),
        "state_dicts": [m.cpu().state_dict() for m in models],
        "training_sessions": sorted(train),
        "training_dates": list(args.train),
        "evaluation_dates": list(args.eval),
        "metrics": metrics,
        "config": {"epochs": args.epochs, "lr": args.lr, "batch": args.batch,
                   "seeds": args.seeds, "train_stride": args.train_stride, "dropout": 0.2},
        "status": (f"학습 {len(train)}세션({'+'.join(args.train)}) · "
                   f"평가 {len(test)}세션({'+'.join(args.eval)}) 세션 {metrics['sessions']} · "
                   "전환 구간·장시간 운영 미검증"),
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, out)
    (HERE / f"model_{args.name}.metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[저장] {out.relative_to(ROOT)}  ({out.stat().st_size / 1024:.0f} KB, "
          f"총 {time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
