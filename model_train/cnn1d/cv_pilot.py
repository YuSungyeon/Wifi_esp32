#!/usr/bin/env python3
"""파일럿(2026-09-16·17) 15세션으로 1D-CNN을 배치(조건) 단위 교차검증한다.

    python model_train/cnn1d/cv_pilot.py cache
    python model_train/cnn1d/cv_pilot.py cv --name c1-baseline

test fold는 배치 하나(A~D)이고 나머지 전부로 학습한다. E(3주기)는 static이 없어 항상
train. validation이 없으므로 epoch 수는 실행 전에 고정한다. 정규화 통계는 fold마다
train 세션에서만 계산한다. 같은 15세션으로 설정을 반복 비교하면 점수가 낙관적으로
기운다 — 최종 성능은 학습에 쓰지 않은 새 날짜 세션으로 따로 확인해야 한다.
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

from csi_store import LLTF_DATA_IDX  # noqa: E402
from model_train.cnn1d.CNN1D import CNN1DClassifier  # noqa: E402
from model_train.lstm import LSTM as training  # noqa: E402
from model_train.preprocessing import preprocess_3rx as pre  # noqa: E402

# 3-RX × 64 중 상시 0인 DC·가드 톤을 뺀 156개 feature 위치
VALID_FEATURES = np.concatenate([r * 64 + LLTF_DATA_IDX for r in range(3)])


class SharedTemporalCNN(nn.Module):
    """모든 서브캐리어 시계열에 같은 가중치의 시간축 Conv를 적용하고 통계로 풀링한다.

    특정 서브캐리어 조합(=배치별 다중경로 지문)을 외울 수 없게 하는 구조다.
    """

    def __init__(self, dropout: float = 0.2, groups: tuple = (156,)) -> None:
        # groups: 모달리티(진폭·위상)별 시계열 수. 모달리티마다 인코더와 풀링을 따로 둔다.
        super().__init__()

        def block(cin, cout, k):
            return [nn.Conv1d(cin, cout, k, padding=k // 2, bias=False),
                    nn.BatchNorm1d(cout), nn.ReLU()]

        self.groups = list(groups)
        self.encoders = nn.ModuleList(
            nn.Sequential(*block(1, 16, 5), nn.MaxPool1d(2),
                          *block(16, 32, 5), nn.MaxPool1d(2), *block(32, 32, 3))
            for _ in self.groups)
        self.head = nn.Sequential(nn.Linear(128 * len(self.groups), 64), nn.ReLU(),
                                  nn.Dropout(dropout), nn.Linear(64, 3))

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # (B, T, F)
        pooled = []
        for enc, part in zip(self.encoders, torch.split(x, self.groups, dim=2)):
            b, t, f = part.shape
            h = enc(part.permute(0, 2, 1).reshape(b * f, 1, t))
            per_series = torch.cat([h.mean(2), h.std(2)], 1).view(b, f, -1)
            pooled += [per_series.mean(1), per_series.std(1)]
        return self.head(torch.cat(pooled, 1))

JSONL_ROOT = ROOT / "mac_collector_output" / "jsonl" / "raw"
RUNS = Path(__file__).resolve().parent / "runs" / "pilot_cv"
CACHE = RUNS / "cache"
CLASS_NAMES = ["empty", "static", "motion"]

# 배치(조건) → (날짜, 세션). 같은 배치 안에서는 보드 위치가 같다.
BLOCKS = {
    "A": ("20260916", [31, 32, 33]),      # 9/16 배치1 (Y자)
    "B": ("20260916", [34, 35, 36, 37]),  # 9/16 배치2 (RX 재배치, s37은 LOS 정지)
    "C": ("20260917", [38, 39, 40]),      # 9/17 1주기
    "D": ("20260917", [41, 42, 43]),      # 9/17 2주기 (윗면 벽쪽 회전)
    "E": ("20260917", [44, 45]),          # 9/17 3주기 (좌우 대칭, static 없음)
}
TEST_BLOCKS = ["A", "B", "C", "D"]


def cmd_cache(_args):
    CACHE.mkdir(parents=True, exist_ok=True)
    for date, sids in BLOCKS.values():
        labels = json.loads((JSONL_ROOT / date / "labels.json").read_text())
        for sid in sids:
            out = CACHE / f"s{sid}.npz"
            if out.exists():
                continue
            t0 = time.time()
            r = pre.process_session(JSONL_ROOT / date / f"session_{sid}", sid,
                                    labels[str(sid)], None, pre.DEFAULT_CONFIG)
            if not r["used"]:
                print(f"s{sid} 제외: {r['manifest']['exclusion_reasons']}")
                continue
            np.savez(out, combined=r["combined"], starts=np.array(r["valid_starts"]),
                     label=r["label"])
            print(f"s{sid} {labels[str(sid)]:6s} windows={len(r['valid_starts'])} "
                  f"({time.time() - t0:.1f}s)")


def cmd_cache_phase(_args):
    """raw .csi 위상을 공식 전처리와 같은 tx_seq 격자에 정렬해 선형 성분을 뺀 잔차로 저장.

    ESP32 LLTF 버퍼 38~63 = 주파수 −26~−1, 1~26 = +1~+26 (실데이터 경계 위상 점프로 확인).
    패킷마다 요동하는 CFO(상수)·타이밍 오프셋(기울기)을 주파수 축 직선 적합으로 제거한다.
    """
    import csi_store as cs

    order = np.r_[38:64, 1:27]
    freq = np.r_[-26:0, 1:27].astype(np.float64)
    design = np.vstack([freq, np.ones_like(freq)]).T
    proj = design @ np.linalg.pinv(design)  # 직선 성분 사영

    for date, sids in BLOCKS.values():
        labels = json.loads((JSONL_ROOT / date / "labels.json").read_text())
        for sid in sids:
            out = CACHE / f"s{sid}_phase.npz"
            if out.exists():
                continue
            r = pre.process_session(JSONL_ROOT / date / f"session_{sid}", sid,
                                    labels[str(sid)], None, pre.DEFAULT_CONFIG)
            start, T = r["manifest"]["common_start"], r["combined"].shape[0]
            frames = cs.read_session(next((ROOT / "mac_collector_output" / "raw" / date)
                                          .glob(f"*_s{sid}")))
            re = np.full((3, T, 52), np.nan, np.float32)
            im = np.full((3, T, 52), np.nan, np.float32)
            present = np.zeros((3, T), bool)
            for ri, rx in enumerate(pre.DEFAULT_CONFIG.rx_order):
                idx = frames[rx]["hdr"]["tx_seq"].astype(np.int64) - start
                keep = np.flatnonzero((idx >= 0) & (idx < T))
                idx, first = np.unique(idx[keep], return_index=True)  # 중복 tx_seq는 첫 frame
                z = cs.complex_csi(frames[rx][keep[first]], valid_only=False)[:, order]
                re[ri, idx], im[ri, idx], present[ri, idx] = z.real, z.imag, True
            pre.interpolate_short_gaps(re, present, pre.DEFAULT_CONFIG)
            pre.interpolate_short_gaps(im, present, pre.DEFAULT_CONFIG)
            phi = np.unwrap(np.angle(re + 1j * im), axis=2)
            residual = phi - phi @ proj.T
            phase = residual.transpose(1, 0, 2).reshape(T, 156).astype(np.float32)
            np.savez(out, phase=phase)
            print(f"s{sid} phase T={T} NaN frames={np.isnan(phase).any(1).sum()}")


def load_sessions():
    out = {}
    for block, (_, sids) in BLOCKS.items():
        for sid in sids:
            path = CACHE / f"s{sid}.npz"
            if path.exists():
                z = np.load(path)
                out[sid] = {"block": block, "x": z["combined"], "starts": z["starts"],
                            "label": int(z["label"])}
                phase = CACHE / f"s{sid}_phase.npz"
                if phase.exists():
                    out[sid]["phase"] = np.load(phase)["phase"]
    return out


def valid_starts(x, width, stride):
    # 공식 전처리 5.11과 같은 규칙: 보간되지 않은 누락(NaN) frame이 낀 윈도는 제외.
    bad = np.concatenate(([0], np.cumsum(np.isnan(x).any(axis=1))))
    starts = np.arange(0, len(x) - width + 1, stride)
    return starts[bad[starts + width] == bad[starts]]


def windows(x, starts, width, downsample=1, pool="mean"):
    view = np.lib.stride_tricks.sliding_window_view(x, width, axis=0)  # (T-w+1, F, w)
    w = view[starts].transpose(0, 2, 1)  # (n, w, F)
    if downsample > 1:
        n, t, f = w.shape
        chunks = w[:, : t - t % downsample].reshape(n, t // downsample, downsample, f)
        # mean+std: 평균만 남기면 다운샘플 구간 안의 빠른 변화(움직임)가 뭉개진다.
        w = (np.concatenate([chunks.mean(axis=2), chunks.std(axis=2)], axis=2)
             if pool == "mean+std" else chunks.mean(axis=2))
    return w


def window_norm(xb):
    # 윈도 안 서브캐리어별 상대 변동. 배치마다 다른 기저 진폭(다중경로 지문)을 지운다.
    # +1: 진폭 단위가 정수 I/Q 크기라 평균≈0인 가드 톤이 폭주하지 않게 한다.
    m = xb.mean(axis=1, keepdims=True)
    return ((xb - m) / (m + 1.0)).astype(np.float32)


def run_fold(sessions, test_block, args, seed, device):
    train_ids = [s for s, v in sessions.items() if v["block"] != test_block]
    test_ids = [s for s, v in sessions.items() if v["block"] == test_block]

    if args.norm == "global":
        frames = np.concatenate([sessions[s]["x"] for s in train_ids])
        mean = np.nanmean(frames, axis=0)
        std = np.nanstd(frames, axis=0)
        std = np.where(std < 1e-6, 1.0, std)

    def featurize(s, starts):
        if args.model == "shared":
            parts = []
            if "amp" in args.features:
                amp = windows(sessions[s]["amp_valid"], starts, args.window,
                              args.downsample, args.pool)
                parts.append(window_norm(amp))
            if "phase" in args.features:
                wp = windows(sessions[s]["phase"], starts, args.window, args.downsample,
                             args.pool)
                parts.append((wp - wp.mean(axis=1, keepdims=True)).astype(np.float32))
            return np.concatenate(parts, axis=2)
        w = windows(sessions[s]["x"], starts, args.window, args.downsample)
        base = sessions[s].get("baseline")
        if args.norm == "global":
            return ((w - mean) / std).astype(np.float32)
        if args.norm == "window":
            return window_norm(w)
        level = ((w - base) / (base + 1.0)).astype(np.float32)
        if args.norm == "baseline":
            return level
        return np.concatenate([window_norm(w), level], axis=2)  # both: 384채널

    index = np.array([(s, st) for s in train_ids for st in sessions[s]["starts"]])
    y_all = {s: sessions[s]["label"] for s in sessions}

    training.set_random_seed(seed)
    if args.model == "shared":
        if args.norm != "window":
            raise ValueError("shared 모델은 norm window 만 지원 (위상은 윈도 평균 차감)")
        n_groups = len(args.features.split("+")) * (2 if args.pool == "mean+std" else 1)
        groups = tuple(156 for _ in range(n_groups))
        model = SharedTemporalCNN(dropout=args.dropout, groups=groups).to(device)
    else:
        model = CNN1DClassifier(input_size=384 if args.norm == "both" else 192,
                                dropout=args.dropout).to(device)
    opt = torch.optim.Adam(model.parameters(), lr=args.lr)
    loss_fn = nn.CrossEntropyLoss()
    rng = np.random.default_rng(seed)

    for _epoch in range(args.epochs):
        model.train()
        order = rng.permutation(len(index))
        for b in range(0, len(order), args.batch_size):
            rows = index[order[b:b + args.batch_size]]
            xb = np.concatenate([featurize(s, np.array([st])) for s, st in rows])
            yb = np.array([y_all[s] for s, _ in rows])
            opt.zero_grad()
            loss = loss_fn(model(torch.from_numpy(xb).to(device)),
                           torch.from_numpy(yb).to(device))
            loss.backward()
            opt.step()

    model.eval()
    cm = np.zeros((3, 3), dtype=np.int64)
    per_session = {}
    with torch.no_grad():
        for s in test_ids:
            starts = sessions[s]["starts"]
            probs = []
            for b in range(0, len(starts), 256):
                xbt = np.ascontiguousarray(featurize(s, starts[b:b + 256]))
                logits = model(torch.from_numpy(xbt).to(device))
                probs.append(torch.softmax(logits, 1).cpu().numpy())
            probs = np.concatenate(probs)
            pred = probs.argmax(1)
            label = sessions[s]["label"]
            np.add.at(cm, (np.full(len(pred), label), pred), 1)
            per_session[s] = {"label": CLASS_NAMES[label],
                              "pred": CLASS_NAMES[int(probs.mean(0).argmax())],
                              "window_acc": float((pred == label).mean())}
    return cm, per_session


def cmd_cv(args):
    sessions = load_sessions()
    for v in sessions.values():
        x = np.hstack([v["x"], v["phase"]]) if "phase" in args.features else v["x"]
        v["starts"] = valid_starts(x, args.window, args.stride)
        if args.model == "shared" and "amp" in args.features:
            v["amp_valid"] = np.ascontiguousarray(v["x"][:, VALID_FEATURES])
    if args.norm in ("baseline", "both"):
        # 빈 방 캘리브레이션: 배치마다 empty 세션 앞 calib_frames 로 기준 진폭을 잡는다.
        # 그 구간은 기준으로만 쓰고 학습·평가 윈도에서 뺀다.
        for block in BLOCKS:
            empty = [v for v in sessions.values() if v["block"] == block and v["label"] == 0]
            base = np.nanmean(empty[0]["x"][: args.calib_frames], axis=0)
            empty[0]["starts"] = empty[0]["starts"][empty[0]["starts"] >= args.calib_frames]
            for v in sessions.values():
                if v["block"] == block:
                    v["baseline"] = base
    device = training.select_device(args.device)
    out_dir = RUNS / args.name
    out_dir.mkdir(parents=True, exist_ok=False)
    (out_dir / "config.json").write_text(json.dumps(vars(args), indent=2, default=str))

    results = []
    for seed in args.seeds:
        for block in TEST_BLOCKS:
            t0 = time.time()
            cm, per_session = run_fold(sessions, block, args, seed, device)
            m = training.metrics_from_confusion(cm, CLASS_NAMES)
            results.append({"seed": seed, "test_block": block, "metrics": m,
                            "sessions": per_session})
            sess = " ".join(f"s{s}:{v['label'][0]}→{v['pred'][0]}({v['window_acc']:.2f})"
                            for s, v in per_session.items())
            print(f"seed={seed} test={block} macroF1={m['macro_f1']:.3f} "
                  f"({time.time() - t0:.0f}s) {sess}", flush=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))
    summarize(results)


def summarize(results):
    f1 = np.array([r["metrics"]["macro_f1"] for r in results])
    cm = sum(np.array(r["metrics"]["confusion_matrix"]) for r in results)
    sess = [v["label"] == v["pred"] for r in results for v in r["sessions"].values()]
    pooled = training.metrics_from_confusion(cm, CLASS_NAMES)
    print(f"\nwindow macro-F1 (fold×seed 평균) = {f1.mean():.3f} ± {f1.std():.3f}")
    print(f"pooled recall: " + " ".join(
        f"{c}={pooled['per_class'][c]['recall']:.3f}" for c in CLASS_NAMES))
    print(f"session accuracy = {np.mean(sess):.3f} ({sum(sess)}/{len(sess)})")
    print("confusion (행=정답 empty/static/motion, seed 합산):")
    print(cm)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("cache")
    sub.add_parser("cache-phase")
    cv = sub.add_parser("cv")
    cv.add_argument("--name", required=True)
    cv.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    cv.add_argument("--epochs", type=int, default=10)
    cv.add_argument("--batch-size", type=int, default=32)
    cv.add_argument("--lr", type=float, default=1e-3)
    cv.add_argument("--dropout", type=float, default=0.2)
    cv.add_argument("--norm", choices=("global", "window", "baseline", "both"),
                    default="global")
    cv.add_argument("--calib-frames", type=int, default=6000, help="baseline용 빈 방 구간")
    cv.add_argument("--model", choices=("cnn1d", "shared"), default="cnn1d")
    cv.add_argument("--pool", choices=("mean", "mean+std"), default="mean",
                    help="다운샘플 구간 요약 방식")
    cv.add_argument("--features", choices=("amp", "phase", "amp+phase"), default="amp",
                    help="shared 모델 입력 (phase는 cache-phase 필요)")
    cv.add_argument("--window", type=int, default=300, help="frame 수 (100Hz)")
    cv.add_argument("--stride", type=int, default=30)
    cv.add_argument("--downsample", type=int, default=1, help="윈도 안 평균 다운샘플 배수")
    cv.add_argument("--device", default="auto")
    args = p.parse_args()
    {"cache": cmd_cache, "cache-phase": cmd_cache_phase, "cv": cmd_cv}[args.cmd](args)


if __name__ == "__main__":
    main()
