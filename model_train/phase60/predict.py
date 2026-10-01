#!/usr/bin/env python3
"""위상 60초 모델 추론 — 세션 디렉터리를 넣으면 판단을 낸다.

    python model_train/phase60/predict.py --session-dir mac_collector_output/raw/<날짜>/<세션>
    python model_train/phase60/predict.py --session-dir <세션> --model model_train/phase60/model_60sessions.pt
    python model_train/phase60/predict.py --list              # 가진 체크포인트와 그 성능

세션의 라벨(`session.json` 의 `label`)은 **읽지 않는다** — 보드 식별자만 쓴다. 평가에
정답이 새어 들어가지 않게 하려는 것이다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from model_train.cnn1d.shared_cnn import SharedTemporalCNN  # noqa: E402
from model_train.phase60 import features as F  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_MODEL = HERE / "model_60sessions.pt"


def load_bundle(path: Path, device: str = "cpu"):
    """체크포인트를 열고 입력 계약을 검사한다. 어긋나면 거부한다."""
    bundle = torch.load(path, map_location=device, weights_only=False)
    if (bundle.get("feature_version") != F.FEATURE_VERSION
            or bundle.get("mode") != "P"
            or bundle.get("rx_order") != list(F.RX_ORDER)
            or bundle.get("raw_tone_order") != F.RAW_TONE_ORDER.tolist()):
        raise ValueError(f"입력 계약이 다른 체크포인트다: {path}")
    models = []
    for state in bundle["state_dicts"]:
        model = SharedTemporalCNN(groups=(F.N_COLS,), n_classes=len(bundle["class_names"]))
        model.load_state_dict(state)
        models.append(model.to(device).eval())
    return bundle, models


def predict_phase(phase: np.ndarray, bundle: dict, models: list, device: str = "cpu") -> dict:
    window, downsample = bundle["window_frames"], bundle["downsample"]
    starts = F.valid_starts(phase, window, bundle["stride_frames"])
    if len(starts) == 0:
        raise ValueError(f"유효한 {window / 100:.0f}초 창이 없다 — 누락이 너무 많다")
    probs = []
    with torch.no_grad():
        for b in range(0, len(starts), 32):
            x = torch.from_numpy(np.ascontiguousarray(
                F.windows(phase, starts[b:b + 32], window, downsample))).to(device)
            probs.append(torch.stack([m(x).softmax(1) for m in models]).mean(0).cpu().numpy())
    probs = np.concatenate(probs)
    mean = probs.mean(0)
    names = bundle["class_names"]
    return {
        "prediction": names[int(mean.argmax())],
        "mean_scores": {c: round(float(s), 4) for c, s in zip(names, mean)},
        "window_seconds": window / 100, "stride_seconds": bundle["stride_frames"] / 100,
        "valid_windows": int(len(starts)),
        "window_votes": {c: int((probs.argmax(1) == i).sum()) for i, c in enumerate(names)},
        "scores_are_calibrated": False,
        "model": {"status": bundle.get("status", ""),
                  "trained_at": bundle.get("trained_at", ""),
                  "training_dates": bundle.get("training_dates", [])},
    }


def list_models() -> int:
    found = sorted(HERE.glob("model_*.pt"))
    if not found:
        print("체크포인트가 없다. 먼저 train.py 로 학습한다.")
        return 1
    for p in found:
        b = torch.load(p, map_location="cpu", weights_only=False)
        m = b.get("metrics", {})
        print(f"{p.name}")
        print(f"  학습 {len(b.get('training_sessions', []))}세션 "
              f"{b.get('training_dates')} → 평가 {b.get('evaluation_dates')}")
        print(f"  세션 {m.get('sessions')} · 사람 있음/없음 {m.get('presence')} · "
              f"빈방vs정지 {m.get('empty_vs_static')} · 창 정답률 {m.get('window_accuracy')}")
        print(f"  창 {b['window_frames'] / 100:.0f}초 · seed {len(b['state_dicts'])}개 · "
              f"{b.get('trained_at', '')}\n")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session-dir", type=Path)
    ap.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    ap.add_argument("--list", action="store_true", help="체크포인트 목록과 성능")
    ap.add_argument("--details", action="store_true", help="창 단위 득표까지 출력")
    args = ap.parse_args()
    torch.set_num_threads(2)

    if args.list:
        return list_models()
    if not args.session_dir:
        ap.error("--session-dir 또는 --list 가 필요하다")

    bundle, models = load_bundle(args.model)
    result = predict_phase(F.session_phase(args.session_dir), bundle, models)
    result["session"] = args.session_dir.name
    if not args.details:
        result.pop("window_votes")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
