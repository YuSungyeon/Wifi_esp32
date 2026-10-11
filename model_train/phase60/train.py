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
def session_key(date: str, dir_name: str) -> str:
    """`20260916_31` — 날짜가 다르면 세션 번호가 겹치므로 날짜를 붙인다."""
    return f"{date}_{dir_name.rsplit('_s', 1)[1]}"


def load_dates(dates: tuple, window: int, stride: int, only: set | None = None) -> dict:
    """{키: {phase, label, starts}}. 위상 추출이 느려 세션별로 캐시한다.

    `only` 를 주면 그 세션 키만 읽는다 (화면에서 세션을 골랐을 때).
    """
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
def train_one(train: dict, seed: int, args, device: torch.device) -> nn.Module:
    torch.manual_seed(seed)
    np.random.seed(seed)
    model = SharedTemporalCNN(dropout=args.dropout, groups=model_groups(args),
                              n_classes=3, width=args.width).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    weight = (torch.tensor(args.class_weight, dtype=torch.float32, device=device)
              if args.class_weight else None)
    loss_fn = nn.CrossEntropyLoss(weight=weight, label_smoothing=args.label_smoothing)
    index = [(k, int(s)) for k, v in train.items() for s in v["starts"]]
    rng = np.random.default_rng(seed)
    for epoch in range(args.epochs):
        model.train()
        order = rng.permutation(len(index))
        for b in range(0, len(order), args.batch):
            rows = [index[i] for i in order[b:b + args.batch]]
            xb = np.concatenate([make_input(train[k]["phase"], np.array([s]), args)
                                 for k, s in rows])
            yb = np.array([train[k]["label"] for k, _ in rows])
            if args.augment > 0:
                # 세 클래스 모두 시간 방향과 무관하다 → 뒤집어도 라벨이 보존된다.
                # 서브캐리어별 게인 흔들기는 특정 톤 크기에 기대지 못하게 한다.
                flip = rng.random(len(xb)) < 0.5
                xb[flip] = xb[flip, ::-1]
                xb = (xb * (1 + args.augment * rng.standard_normal(
                    (len(xb), 1, xb.shape[2])))).astype(np.float32)
            opt.zero_grad()
            loss_fn(model(torch.from_numpy(xb).to(device)),
                    torch.from_numpy(yb).to(device)).backward()
            opt.step()
        print(f"    seed {seed} epoch {epoch + 1}/{args.epochs}", flush=True)
    return model.eval()


def model_groups(args) -> tuple:
    """입력 열을 어떤 묶음으로 나눌지. 묶음마다 인코더와 풀링이 따로 돈다."""
    cols = F.n_input_columns(args.diff, args.pool) * (2 if args.spectral == "add" else 1)
    if args.per_rx:                       # RX 마다 따로 요약 — 보드별 신호 차이를 살린다
        return tuple(cols // 3 for _ in range(3))
    if args.spectral == "add" or args.diff or args.pool == "mean+std":
        return tuple(cols // 2 for _ in range(2))
    return (cols,)


class _Cfg:
    """evaluate 가 make_input 에 넘길 최소 설정 (학습 때의 args 와 같은 필드)."""

    def __init__(self, window, downsample, diff, pool, spectral="none"):
        self.window, self.downsample = window, downsample
        self.diff, self.pool, self.spectral = diff, pool, spectral


def make_input(phase, starts, args):
    """창 변환 한 곳 — 학습과 평가가 반드시 같은 함수를 쓴다."""
    w = F.windows(phase, starts, args.window, args.downsample, args.diff, args.pool)
    mode = getattr(args, "spectral", "none")
    if mode == "none":
        return w
    spec = F.spectrum(w, 100.0 / args.downsample)
    if mode == "only":
        return spec
    # 두 묶음을 나란히 넣으려면 길이가 같아야 한다. 시간 쪽을 자르면 정보를 버리게 되므로
    # 주파수 쪽을 선형 보간으로 늘린다 (스펙트럼은 이웃 bin 이 매끄럽다).
    t = w.shape[1]
    src = np.linspace(0, 1, spec.shape[1])
    dst = np.linspace(0, 1, t)
    spec_up = np.stack([np.stack([np.interp(dst, src, spec[i, :, c])
                                  for c in range(spec.shape[2])], axis=1)
                        for i in range(spec.shape[0])]).astype(np.float32)
    return np.concatenate([w, spec_up], axis=2)


def evaluate(models: list, sessions: dict, window: int, downsample: int,
             device: torch.device, diff: bool = False, pool: str = "mean",
             spectral: str = "none") -> dict:
    cfg = _Cfg(window, downsample, diff, pool, spectral)
    """세션별 softmax 평균(seed 앙상블)으로 판정. 창 단위 정확도도 함께 낸다."""
    rows, cm = [], np.zeros((3, 3), int)
    with torch.no_grad():
        for key, v in sessions.items():
            probs = []
            for b in range(0, len(v["starts"]), 32):
                xb = np.ascontiguousarray(
                    make_input(v["phase"], v["starts"][b:b + 32], cfg))
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
    ap.add_argument("--train", nargs="+", default=None, help="학습에 쓸 날짜 폴더")
    ap.add_argument("--sessions", nargs="+", default=None,
                    help="학습에 쓸 세션 키 (예: 20260916_31). --train 대신 쓴다")
    ap.add_argument("--eval", nargs="+", default=[], help="평가에 쓸 날짜 폴더 (학습 날짜와 겹치면 거부)")
    ap.add_argument("--window", type=int, default=6000)
    ap.add_argument("--downsample", type=int, default=20)
    ap.add_argument("--train-stride", type=int, default=30)
    ap.add_argument("--eval-stride", type=int, default=300)
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--dropout", type=float, default=0.2)
    ap.add_argument("--diff", action="store_true", help="위상에 시간 차분 묶음을 더한다")
    ap.add_argument("--augment", type=float, default=0.0,
                    help=">0 이면 시간 뒤집기 + 서브캐리어별 게인 흔들기(표준편차)")
    ap.add_argument("--label-smoothing", type=float, default=0.0)
    ap.add_argument("--class-weight", type=float, nargs=3, default=None,
                    help="empty static motion 손실 가중치")
    ap.add_argument("--pool", choices=("mean", "mean+std"), default="mean",
                    help="다운샘플 구간 요약 방식")
    ap.add_argument("--per-rx", action="store_true", help="RX 별로 묶어 따로 요약한다")
    ap.add_argument("--width", type=int, default=16, help="인코더 첫 채널 수 (기본 16)")
    ap.add_argument("--spectral", choices=("none", "add", "only"), default="none",
                    help="호흡 대역(0.05~1Hz) 로그 스펙트럼: 두 번째 묶음으로 더하거나(add) 그것만(only)")
    args = ap.parse_args()

    if not args.train and not args.sessions:
        print("[중단] --train 또는 --sessions 가 필요하다")
        return 1
    only = set(args.sessions) if args.sessions else None
    train_dates = sorted({k.split("_")[0] for k in only}) if only else list(args.train)

    # 날짜 단위로 막는다. 같은 날·같은 배치가 학습과 평가 양쪽에 들어가면 점수가 부풀고,
    # 화면에서 세션을 고르게 하면 그 실수가 특히 쉬워진다.
    overlap = set(train_dates) & set(args.eval)
    if overlap:
        print(f"[중단] 학습에 쓴 날짜를 평가에도 쓸 수 없다: {sorted(overlap)}")
        return 1

    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
    print(f"[데이터] 학습 {train_dates} / 평가 {args.eval or '없음'} · device={device}", flush=True)
    train = load_dates(tuple(train_dates), args.window, args.train_stride, only)
    if only:
        missing = only - set(train)
        if missing:
            print(f"[중단] 읽지 못한 세션: {sorted(missing)}")
            return 1
    labels = {v["label"] for v in train.values()}
    if len(labels) < 3:
        print(f"[중단] 세 클래스가 모두 필요하다 — 지금 {sorted(CLASS_NAMES[i] for i in labels)}")
        return 1
    test = load_dates(tuple(args.eval), args.window, args.eval_stride) if args.eval else {}
    n_win = sum(len(v["starts"]) for v in train.values())
    print(f"  학습 {len(train)}세션 (창 {n_win}개) → 평가 {len(test)}세션", flush=True)

    models, t0 = [], time.time()
    for seed in args.seeds:
        s0 = time.time()
        models.append(train_one(train, seed, args, device))
        print(f"  seed {seed} 완료 ({time.time() - s0:.0f}s)", flush=True)

    if test:
        print("[평가] 학습에 쓰지 않은 날짜로 판정", flush=True)
        metrics = evaluate(models, test, args.window, args.downsample, device, args.diff,
                           args.pool, args.spectral)
        print(f"  세션 {metrics['sessions']} · 사람 있음/없음 {metrics['presence']} · "
              f"빈방vs정지 {metrics['empty_vs_static']} · 창 정답률 {metrics['window_accuracy']}",
              flush=True)
    else:
        metrics = {"note": "평가 없이 학습만 했다 — evaluate.py 로 따로 재야 한다"}
        print("[평가] 건너뜀 (--eval 없음)", flush=True)

    out = HERE / f"model_{args.name}.pt"
    torch.save({
        "format_version": FORMAT_VERSION,
        "feature_version": F.FEATURE_VERSION,
        "mode": "P",
        "diff_channel": args.diff,
        "pool": args.pool,
        "spectral": args.spectral,
        "per_rx": args.per_rx,
        "width": args.width,
        "window_frames": args.window,
        "stride_frames": args.eval_stride,
        "downsample": args.downsample,
        "sample_rate_hz": 100,
        "rx_order": list(RX_ORDER),
        "raw_tone_order": F.RAW_TONE_ORDER.tolist(),
        "class_names": list(CLASS_NAMES),
        "state_dicts": [m.cpu().state_dict() for m in models],
        "training_sessions": sorted(train),
        "training_dates": train_dates,
        "evaluation_dates": list(args.eval),
        "metrics": metrics,
        "config": {"epochs": args.epochs, "lr": args.lr, "batch": args.batch,
                   "seeds": args.seeds, "train_stride": args.train_stride,
                   "dropout": args.dropout, "diff": args.diff, "augment": args.augment,
                   "label_smoothing": args.label_smoothing, "class_weight": args.class_weight,
                   "pool": args.pool,
        "spectral": args.spectral, "per_rx": args.per_rx, "width": args.width},
        "status": (f"학습 {len(train)}세션({'+'.join(train_dates)}) · "
                   + (f"평가 {len(test)}세션({'+'.join(args.eval)}) 세션 {metrics['sessions']}"
                      if test else "평가 없음")
                   + " · 전환 구간·장시간 운영 미검증"),
        "trained_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }, out)
    (HERE / f"model_{args.name}.metrics.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[저장] {out.relative_to(ROOT)}  ({out.stat().st_size / 1024:.0f} KB, "
          f"총 {time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
