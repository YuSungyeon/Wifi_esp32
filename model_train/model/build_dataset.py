"""여러 수집 세션 → 학습 데이터셋 `.npz` (3-class).

각 세션의 `session.json` 라벨을 그대로 써서 세션별 윈도를 만들고 이어 붙인다.

**split 은 반드시 세션 단위**다. 윈도가 3초 길이에 0.3초 stride라 이웃 윈도끼리 90%가
겹치므로, 윈도 단위로 무작위 분할하면 같은 3초 구간이 train 과 val 양쪽에 들어가
검증 정확도가 실제보다 높게 나온다.

    python model_train/model/build_dataset.py --out model_train/dataset.npz
    python model_train/model/build_dataset.py --rx-ids 101 103 --val-ratio 0.2
"""

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = PROJECT_ROOT / "mac_collector_output" / "raw"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import csi_store as cs  # noqa: E402
from csi_session import read_manifest  # noqa: E402
from Preprocessing import LABEL_MAP, run_preprocessing  # noqa: E402


def collect_sessions(raw_root: Path, rx_ids):
    """세션별로 (dir, label, X, y) 생성. RX 구성이 안 맞는 세션은 건너뛰고 이유를 알린다."""
    out, skipped = [], []
    for d in cs.find_sessions(raw_root):
        try:
            label = read_manifest(d)["label"]
            X, y = run_preprocessing(d, rx_ids=rx_ids, label_name=None, verbose=False)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            skipped.append((d, str(exc).splitlines()[0]))
            continue
        out.append((d, label, X, y))
    return out, skipped


def split_by_session(sessions, val_ratio: float, seed: int):
    """라벨별로 세션을 섞어 val 로 뗀다 — 클래스가 한쪽에 몰리지 않게."""
    rng = np.random.default_rng(seed)
    by_label = defaultdict(list)
    for i, (_, label, _, _) in enumerate(sessions):
        by_label[label].append(i)

    val_idx = set()
    for label, idxs in sorted(by_label.items()):
        idxs = list(idxs)
        rng.shuffle(idxs)
        n_val = max(1, round(len(idxs) * val_ratio)) if len(idxs) > 1 else 0
        val_idx.update(idxs[:n_val])
    return val_idx


def main() -> int:
    ap = argparse.ArgumentParser(description="여러 세션 → 학습 데이터셋 npz")
    ap.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    ap.add_argument("--out", type=Path, default=PROJECT_ROOT / "model_train" / "dataset.npz")
    ap.add_argument("--rx-ids", type=int, nargs="+", default=None,
                    help="사용할 RX device_id (기본: 각 세션에 있는 전부)")
    ap.add_argument("--val-ratio", type=float, default=0.25, help="검증용으로 뗄 세션 비율")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    sessions, skipped = collect_sessions(args.raw_root, args.rx_ids)
    if skipped:
        print("[건너뜀]")
        for d, why in skipped:
            print(f"  {d.name}: {why}")
    if not sessions:
        print(f"error: 사용할 세션이 없습니다 ({args.raw_root})")
        return 1

    feat = {X.shape[2] for _, _, X, _ in sessions}
    if len(feat) > 1:
        print(f"error: 세션마다 feature 수가 다릅니다 {sorted(feat)} — --rx-ids 로 RX 구성을 고정하세요")
        return 1

    val_idx = split_by_session(sessions, args.val_ratio, args.seed)

    print(f"\n[세션 {len(sessions)}개]")
    parts = {"train": ([], [], []), "val": ([], [], [])}
    for i, (d, label, X, y) in enumerate(sessions):
        split = "val" if i in val_idx else "train"
        parts[split][0].append(X)
        parts[split][1].append(y)
        parts[split][2].append(d.name)
        print(f"  {split:5s} {d.name:32s} label={label:7s} windows={len(X)}")

    data = {}
    for split, (Xs, ys, names) in parts.items():
        if not Xs:
            print(f"\n[경고] {split} 세션이 없습니다 — 세션 수를 늘리거나 --val-ratio 를 조정하세요")
            continue
        X = np.concatenate(Xs).astype(np.float32)
        y = np.concatenate(ys)
        data[f"X_{split}"] = X
        data[f"y_{split}"] = y
        data[f"sessions_{split}"] = np.array(names)
        dist = {k: int(v) for k, v in sorted(Counter(y.tolist()).items())}
        print(f"\n[{split}] X={X.shape} y={y.shape} 클래스분포={dist}")

    inv = {v: k for k, v in LABEL_MAP.items()}
    data["label_names"] = np.array([inv[i] for i in sorted(inv)])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out, **data)
    print(f"\n[완료] {args.out}  ({args.out.stat().st_size / 1e6:.1f} MB)")

    meta = args.out.with_suffix(".json")
    meta.write_text(json.dumps({
        "raw_root": str(args.raw_root),
        "rx_ids": args.rx_ids,
        "val_ratio": args.val_ratio,
        "seed": args.seed,
        "n_sub": cs.N_SUB,
        "window": int(sessions[0][2].shape[1]),
        "sessions": {s: sorted(data.get(f"sessions_{s}", np.array([])).tolist()) for s in ("train", "val")},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"       {meta.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
