#!/usr/bin/env python3
"""여러 체크포인트의 **세션 평균 확률을 평균**해 합쳐지면 나아지는지 본다 (학습 불필요).

    python model_train/analysis/ensemble_test.py \
        --models model_train/robust/model.pt model_train/phase60/model_60sessions.pt \
        --dates 20260916 20260917

특징을 합치는 것(진폭+위상을 한 입력으로)은 이미 나빠진다고 측정됐다 — 진폭이 배치 지문을
다시 들여온다(파일럿 사이클 8). 여기서 보는 것은 **결정 수준 결합**이다: 서로 다른 입력으로
따로 학습한 모델이 **서로 다른 세션에서 틀리는가.** 겹쳐서 틀리면 앙상블은 무의미하다.

창 길이가 모델마다 달라 창 단위로는 정렬할 수 없다. 그래서 세션 평균 확률에서 합친다 —
세션 정답률을 묻는 질문에는 맞는 단위지만, **실시간 창 단위 결합의 이득은 아니다.**

**어느 모델이든 학습 날짜가 평가 날짜에 섞이면 거부한다.**
"""
from __future__ import annotations

import argparse
import itertools
import json
import sys
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from model_train.phase60 import features as F  # noqa: E402
from model_train.phase60.train import CLASS_NAMES, session_key  # noqa: E402

RAW = ROOT / "mac_collector_output" / "raw"
PHASE_CACHE = ROOT / "model_train" / "phase60" / "cache"


def load_model(path: Path, device: str):
    """(bundle, models, kind). realtime/engine.load_any 과 같은 분기지만 평가용이라 따로 둔다."""
    head = torch.load(path, map_location="cpu", weights_only=False)
    if head.get("mode") == "P":
        from model_train.phase60.predict import load_bundle
        bundle, models = load_bundle(path, device)
        return bundle, models, "phase"
    if head.get("mode") == "A":
        from model_train.robust.predict import load_bundle
        bundle, models = load_bundle(path)
        return bundle, models, "amp"      # predict_amplitude 는 CPU 텐서를 만든다 — 올리지 않는다
    if head.get("mode") == "A-ours":      # 우리 학습기로 만든 진폭 모델 (창 길이가 자유롭다)
        from model_train.amp60.train import build

        models = []
        for state in head["state_dicts"]:
            m = build(len(head["class_names"]))
            m.load_state_dict(state)
            models.append(m.eval())
        return head, models, "amp"
    raise ValueError(f"3-class 진폭/위상 체크포인트가 아니다: {path} (mode={head.get('mode')})")


def session_scores(d: Path, key: str, bundle, models, kind, device) -> np.ndarray | None:
    from model_train.phase60.predict import predict_phase
    from model_train.robust.predict import load_raw_amplitude, predict_amplitude
    try:
        if kind == "phase":
            npz = PHASE_CACHE / f"{key}.npz"
            phase = np.load(npz)["phase"] if npz.exists() else F.session_phase(d)
            r = predict_phase(phase, bundle, models, device)
        else:
            r = predict_amplitude(load_raw_amplitude(d)[0], bundle, models)
    except (ValueError, KeyError) as exc:
        print(f"  제외 {key}: {exc}", flush=True)
        return None
    return np.array([r["mean_scores"][c] for c in CLASS_NAMES], dtype=np.float64)


def report(name: str, scores: dict, truth: dict) -> dict:
    wrong = [k for k, s in scores.items()
             if CLASS_NAMES[int(np.argmax(s))] != CLASS_NAMES[truth[k]]]
    ok = len(scores) - len(wrong)
    print(f"  {name:38s} {ok:>2}/{len(scores)}" + (f"   틀림 {' '.join(wrong)}" if wrong else ""))
    return {"name": name, "sessions": f"{ok}/{len(scores)}", "wrong": wrong}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", nargs="+", type=Path, required=True)
    ap.add_argument("--dates", nargs="+", required=True)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()
    torch.set_num_threads(2)
    device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

    loaded = []
    for path in args.models:
        bundle, models, kind = load_model(path, device)
        trained = bundle.get("training_dates") or sorted(
            {s[:8] for s in bundle.get("training_sessions", [])})
        if overlap := set(trained) & set(args.dates):
            print(f"[중단] {path.name} 의 학습 날짜가 평가 대상에 있다: {sorted(overlap)}")
            return 1
        loaded.append((path.stem.replace("model_", "") or path.parent.name, bundle, models, kind))
        print(f"[모델] {loaded[-1][0]:24s} {kind:5s} 창 {bundle['window_frames'] / 100:>2.0f}초 "
              f"· 학습 {trained}", flush=True)

    truth, per_model = {}, {n: {} for n, *_ in loaded}
    for date in args.dates:
        for d in sorted((RAW / date).iterdir()):
            if not (d / "session.json").is_file() or d.name.split("_")[1] not in CLASS_NAMES:
                continue
            key = session_key(date, d.name)
            got = {n: session_scores(d, key, b, m, k, device) for n, b, m, k in loaded}
            if any(v is None for v in got.values()):
                continue
            truth[key] = CLASS_NAMES.index(d.name.split("_")[1])
            for n, v in got.items():
                per_model[n][key] = v
    print(f"[평가] {args.dates} · {len(truth)}세션\n")

    rows = [report(n, per_model[n], truth) for n, *_ in loaded]
    names = [n for n, *_ in loaded]
    for r in range(2, len(names) + 1):
        for combo in itertools.combinations(names, r):
            avg = {k: np.mean([per_model[n][k] for n in combo], axis=0) for k in truth}
            rows.append(report("+ " + " + ".join(combo), avg, truth))

    # 겹쳐 틀리는가 — 이것이 앙상블이 통할지 가르는 지표다
    print()
    for a, b in itertools.combinations(names, 2):
        wa = set(next(r for r in rows if r["name"] == a)["wrong"])
        wb = set(next(r for r in rows if r["name"] == b)["wrong"])
        print(f"  오류 겹침 {a} ∩ {b}: {sorted(wa & wb) or '없음'} "
              f"(각 {len(wa)}개 / {len(wb)}개)")

    if args.out:
        args.out.write_text(json.dumps(
            {"dates": args.dates, "n_sessions": len(truth), "results": rows,
             "per_session": {k: {n: [round(float(x), 4) for x in per_model[n][k]]
                                 for n in names} for k in truth},
             "truth": {k: CLASS_NAMES[v] for k, v in truth.items()}},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[기록] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
