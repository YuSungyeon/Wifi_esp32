#!/usr/bin/env python3
"""고른 세션으로 **진폭 모델**을 학습한다 — 모델 담당의 구조와 입력 변환을 그대로 쓴다.

    python model_train/amp60/train.py --name 내실험 --sessions 20260916_31 20260916_32 ... --eval 20260919

**공식 산출물이 아니다.** 담당자의 학습 파이프라인(`model_train/robust/run_experiment.py`)은
문서화된 60세션 고정이라(`len(sessions) != 60` 이면 거부) 임의의 세션 선택에 쓸 수 없고,
그 코드는 담당자가 유지한다. 그래서 **구조(`SharedEncoder`)와 입력 변환(`amplitude_input`)만
import** 하고 학습 루프와 체크포인트는 우리 것을 쓴다. 배포 체크포인트와 혼동하지 않도록
`mode="A-ours"` 로 표시한다.

위상 학습기(`model_train/phase60/train.py`)와 세션 선택·날짜 잠금·평가 규칙은 같다.
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
sys.path.insert(0, str(ROOT / "scripts"))

from model_train.phase60.train import session_key  # noqa: E402
from train_sessions import is_excluded  # noqa: E402
from model_train.robust.predict import load_raw_amplitude  # noqa: E402
from model_train.robust.shared_temporal import SharedEncoder, amplitude_input  # noqa: E402

HERE = Path(__file__).resolve().parent
RAW = ROOT / "mac_collector_output" / "raw"
CACHE = HERE / "cache"
CLASS_NAMES = ("empty", "static", "motion")
#: 과제. `cv_pilot.py` 의 TASKS 와 같은 어휘다 — 라벨 매핑이 두 곳에서 어긋나면 안 된다.
#: motion(움직임 여부)은 짧은 창에서 잘 되고, 빈 방/정지는 긴 창이 필요하다. 그래서 과제를 나눈다.
#: 전용 empty/static 모델은 파일럿에서 3-class 보다 나빴으므로(0.852 < 0.877) 두지 않는다.
TASKS = {
    "3class": (("empty", "static", "motion"), {0: 0, 1: 1, 2: 2}),
    "motion": (("rest", "motion"), {0: 0, 1: 0, 2: 1}),
}
FEATURE_VERSION = "shared-window-local-v1"   # 담당자 입력 변환과 같은 규격
N_COLS = 153


def load_sessions(dates: tuple, window: int, stride: int, only: set | None = None,
                  remap: dict | None = None) -> dict:
    """{키: {amp, label, starts}}. 진폭 정렬이 느려 세션별로 캐시한다."""
    CACHE.mkdir(parents=True, exist_ok=True)
    out = {}
    for date in dates:
        for d in sorted((RAW / date).iterdir()):
            if not (d / "session.json").is_file():
                continue
            label = d.name.split("_")[1]
            if label not in CLASS_NAMES:
                continue
            key = session_key(date, d.name)
            if only is not None and key not in only:
                continue
            if is_excluded(key):
                print(f"  제외 {key}: 라벨 정의 위반 (train_sessions.EXCLUDED_SESSIONS)", flush=True)
                continue
            npz = CACHE / f"{key}.npz"
            if npz.exists():
                amp = np.load(npz)["amp"]
            else:
                try:
                    amp = load_raw_amplitude(d)[0].astype(np.float32)
                except (ValueError, KeyError) as exc:
                    print(f"  제외 {key}: {exc}", flush=True)
                    continue
                np.savez(npz, amp=amp)
            starts = valid_starts(amp, window, stride)
            if len(starts) == 0:
                print(f"  제외 {key}: 유효한 창 없음", flush=True)
                continue
            y = CLASS_NAMES.index(label)
            out[key] = {"amp": amp, "label": remap[y] if remap else y, "starts": starts}
    return out


def valid_starts(amp: np.ndarray, window: int, stride: int) -> np.ndarray:
    """누락(NaN)이 낀 창은 제외한다 — `amplitude_input` 이 유한값만 받는다."""
    bad = np.concatenate(([0], np.cumsum(~np.isfinite(amp).all(axis=1))))
    starts = np.arange(0, len(amp) - window + 1, stride)
    return starts[bad[starts + window] == bad[starts]]


def batch_input(amp: np.ndarray, starts: np.ndarray, window: int) -> np.ndarray:
    return np.stack([amplitude_input(amp[s:s + window]) for s in starts])


def build(n_classes: int) -> nn.Module:
    """담당자 구조는 3-class 고정이다. 마지막 층만 바꿔 끼운다 — 나머지는 그대로 쓴다."""
    model = SharedEncoder(mode="A")
    if n_classes != 3:
        model.head[-1] = nn.Linear(model.head[-1].in_features, n_classes)
    return model


def train_one(train: dict, seed: int, args, device) -> nn.Module:
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = build(len(TASKS[args.task][0])).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    loss_fn = nn.CrossEntropyLoss(label_smoothing=0.1)
    index = [(k, int(s)) for k, v in train.items() for s in v["starts"]]
    rng = np.random.default_rng(seed)
    for epoch in range(args.epochs):
        model.train()
        order = rng.permutation(len(index))
        for b in range(0, len(order), args.batch):
            rows = [index[i] for i in order[b:b + args.batch]]
            xb = np.concatenate([batch_input(train[k]["amp"], np.array([s]), args.window)
                                 for k, s in rows])
            yb = np.array([train[k]["label"] for k, _ in rows])
            opt.zero_grad()
            loss_fn(model(torch.from_numpy(xb).to(device)),
                    torch.from_numpy(yb).to(device)).backward()
            opt.step()
        print(f"    seed {seed} epoch {epoch + 1}/{args.epochs}", flush=True)
    return model.eval()


def evaluate(models: list, sessions: dict, window: int, device, names=CLASS_NAMES) -> dict:
    _names, n = names, len(names)
    rows, cm = [], np.zeros((n, n), int)
    with torch.no_grad():
        for key, v in sessions.items():
            probs = []
            for b in range(0, len(v["starts"]), 32):
                x = torch.from_numpy(batch_input(v["amp"], v["starts"][b:b + 32], window)).to(device)
                probs.append(torch.stack([m(x).softmax(1) for m in models]).mean(0).cpu().numpy())
            probs = np.concatenate(probs)
            mean = probs.mean(0)
            pred = int(mean.argmax())
            cm[v["label"], pred] += 1
            rows.append({"session": key, "true": _names[v["label"]], "pred": _names[pred],
                         "scores": {c: round(float(s), 4) for c, s in zip(_names, mean)},
                         "window_acc": round(float((probs.argmax(1) == v["label"]).mean()), 4)})
    ok = sum(r["true"] == r["pred"] for r in rows)
    out = {"sessions": f"{ok}/{len(rows)}",
           "window_accuracy": round(float(np.mean([r["window_acc"] for r in rows])), 4),
           "confusion_matrix": cm.tolist(), "per_session": rows}
    if "static" in _names:                      # 3-class 일 때만 뜻이 있는 지표
        out["presence"] = f"{sum((r['pred'] != 'empty') == (r['true'] != 'empty') for r in rows)}/{len(rows)}"
        es = [r for r in rows if r["true"] in ("empty", "static")]
        out["empty_vs_static"] = (
            f"{sum((r['scores']['empty'] >= r['scores']['static']) == (r['true'] == 'empty') for r in es)}"
            f"/{len(es)}")
    else:
        rec = [cm[i, i] / max(cm[i].sum(), 1) for i in range(n)]
        out["recall"] = {c: round(float(v), 4) for c, v in zip(_names, rec)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--name", required=True)
    ap.add_argument("--train", nargs="+", default=None, help="학습에 쓸 날짜 폴더")
    ap.add_argument("--sessions", nargs="+", default=None, help="학습에 쓸 세션 키")
    ap.add_argument("--eval", nargs="+", default=[], help="평가 날짜 (학습 날짜와 겹치면 거부)")
    ap.add_argument("--task", choices=tuple(TASKS), default="3class",
                    help="motion: 움직임 여부만 (짧은 창용 1단계)")
    ap.add_argument("--window", type=int, default=3000, help="배포 모델과 같은 30초")
    ap.add_argument("--train-stride", type=int, default=150)
    ap.add_argument("--eval-stride", type=int, default=300)
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    if not args.train and not args.sessions:
        print("[중단] --train 또는 --sessions 가 필요하다")
        return 1
    only = set(args.sessions) if args.sessions else None
    train_dates = sorted({k.split("_")[0] for k in only}) if only else list(args.train)
    overlap = set(train_dates) & set(args.eval)
    if overlap:
        print(f"[중단] 학습에 쓴 날짜를 평가에도 쓸 수 없다: {sorted(overlap)}")
        return 1

    names, remap = TASKS[args.task]
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    print(f"[데이터] 과제 {args.task}{list(names)} · 창 {args.window / 100:.0f}초 · "
          f"학습 {train_dates} / 평가 {args.eval or '없음'} · device={device}", flush=True)
    train = load_sessions(tuple(train_dates), args.window, args.train_stride, only, remap)
    if only and (missing := only - set(train)):
        print(f"[중단] 읽지 못한 세션: {sorted(missing)}")
        return 1
    if len({v["label"] for v in train.values()}) < len(names):
        print(f"[중단] {len(names)} 클래스가 모두 필요하다")
        return 1
    test = (load_sessions(tuple(args.eval), args.window, args.eval_stride, None, remap)
            if args.eval else {})
    print(f"  학습 {len(train)}세션 (창 {sum(len(v['starts']) for v in train.values())}개) → "
          f"평가 {len(test)}세션", flush=True)

    models, t0 = [], time.time()
    for seed in args.seeds:
        s0 = time.time()
        models.append(train_one(train, seed, args, device))
        print(f"  seed {seed} 완료 ({time.time() - s0:.0f}s)", flush=True)

    if test:
        metrics = evaluate(models, test, args.window, device, names)
        extra = (f"사람 있음/없음 {metrics['presence']} · 빈방vs정지 {metrics['empty_vs_static']}"
                 if "presence" in metrics else f"재현율 {metrics['recall']}")
        print(f"  세션 {metrics['sessions']} · {extra} · "
              f"창 정답률 {metrics['window_accuracy']}", flush=True)
    else:
        metrics = {"note": "평가 없이 학습만 했다"}

    out = HERE / f"model_{args.name}.pt"
    torch.save({
        "format_version": 1, "feature_version": FEATURE_VERSION,
        # 배포 체크포인트("A")와 구분한다. 이진 모델은 "A-motion" — 3-class 상태 모델 자리에
        # 잘못 끼우면 엔진이 거부한다(realtime/engine.py load_any).
        "mode": "A-ours" if args.task == "3class" else "A-motion",
        "task": args.task,
        "window_frames": args.window, "stride_frames": args.eval_stride,
        "sample_rate_hz": 100, "rx_order": [101, 102, 103],
        "class_names": list(names),
        "state_dicts": [m.cpu().state_dict() for m in models],
        "training_sessions": sorted(train), "training_dates": train_dates,
        "evaluation_dates": list(args.eval), "metrics": metrics,
        "config": {"epochs": args.epochs, "lr": args.lr, "batch": args.batch,
                   "seeds": args.seeds, "train_stride": args.train_stride},
        "status": (f"우리 학습기로 만든 진폭 모델 — 학습 {len(train)}세션 · "
                   + (f"평가 {len(test)}세션 {metrics.get('sessions')}" if test else "평가 없음")
                   + " · 공식 배포 체크포인트가 아니다"),
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, out)
    (HERE / f"model_{args.name}.metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[저장] {out.relative_to(ROOT)}  ({out.stat().st_size / 1024:.0f} KB, "
          f"총 {time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
