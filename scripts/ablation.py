#!/usr/bin/env python3
"""베이스라인·ablation 하네스 — 논문 결과 섹션의 뼈대.

수집 세션(`.csi`)을 직접 읽어 설계 변수를 바꿔가며 **같은 베이스라인 분류기**로 평가한다.
학습 모델의 성능이 이 베이스라인을 못 넘으면 복잡도가 정당화되지 않는다.

    python scripts/ablation.py                          # 기본 grid, 세션 단위 LOSO
    python scripts/ablation.py --split-by date          # 조건 단위 (날짜) 분할
    python scripts/ablation.py --split-by subject --out mac_collector_output/ablation.md

ablation 축:
  rx        : RX 1대씩 / 전부          → "RX 를 늘리면 나아지는가"
  window    : 1s / 3s / 5s             → 윈도 길이
  features  : amp52 / amp64 / amp52+phase
              amp64 는 DC·가드 12개(상시 0)를 포함한 옛 방식. amp52+phase 는 raw I/Q 를
              보존한 덕분에 재수집 없이 가능한 위상 ablation이다.

분할(`--split-by`)은 doc/collection-protocol.md §6 을 따른다. `session` 은 세션 단위
LOSO, `date`/`placement`/`subject` 는 **조건 단위** — 학습에 한 번도 안 쓴 조건으로
평가하므로 일반화 주장의 근거가 된다. 조건은 세션의 session_meta_snapshot.yaml 에서 읽는다.

분류기는 check_separability.py 와 같다: 세션지문(클래스보다 세션을 더 잘 구분하는
특징)을 걸러낸 최근접 중심. 의도적으로 단순하다 — 결과가 분류기 성능이 아니라
**데이터·설계 변수**의 차이를 반영해야 하기 때문이다.
"""
from __future__ import annotations

import argparse
import itertools
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import csi_store as cs  # noqa: E402
from check_separability import (  # noqa: E402
    FEATURES, loso_nearest_centroid, session_stability, window_features,
)
from csi_session import read_manifest  # noqa: E402
from session_form import read_meta  # noqa: E402

F_S = 100
STRIDE_S = 0.3
PHASE_FEATURES = [
    ("ph_diff_std", "인접SC 위상차 시간std", "인접 서브캐리어 위상차(CFO 상쇄)의 시간축 변동"),
    ("ph_diff_jitter", "인접SC 위상차 변화율", "위상차의 프레임간 |Δ| 평균"),
]


# ── 세션 → 윈도 ──────────────────────────────────────────────────────────────
def _load_aligned(session_dir: Path, rx_ids, *, need_complex: bool):
    """RX 들의 raw 를 tx_seq 공통 격자에 보간. (RX, T, 64) 진폭과 (선택) 복소."""
    bufs = {}
    for dev, frames in cs.read_session(session_dir).items():
        if len(frames) == 0:
            continue
        tx = frames["hdr"]["tx_seq"].astype(np.int64)
        if (np.diff(tx) < 0).any():
            raise ValueError(f"RX{dev}: TX 재부팅(tx_seq 역행) — 세션 제외")
        order = np.argsort(tx, kind="stable")
        keep = np.concatenate(([True], np.diff(tx[order]) > 0))
        idx = order[keep]
        amp = cs.amplitude(frames, valid_only=False)[idx]
        z = cs.complex_csi(frames, valid_only=False)[idx] if need_complex else None
        bufs[dev] = (tx[idx].astype(np.float64), amp, z)

    ids = tuple(rx_ids) if rx_ids else tuple(sorted(bufs))
    missing = [d for d in ids if d not in bufs]
    if missing:
        raise ValueError(f"RX {missing} 없음 (있는 것: {sorted(bufs)})")
    start = int(max(bufs[d][0][0] for d in ids))
    end = int(min(bufs[d][0][-1] for d in ids))
    grid = np.arange(start, end + 1, dtype=np.float64)

    def interp(x, cols):
        return np.column_stack([np.interp(grid, x, cols[:, k]) for k in range(cols.shape[1])])

    amp = np.stack([interp(bufs[d][0], bufs[d][1]) for d in ids])            # (RX, T, 64)
    z = None
    if need_complex:
        # 복소는 실·허수를 따로 보간한다 (위상을 직접 보간하면 2π 경계에서 깨진다)
        z = np.stack([interp(bufs[d][0], bufs[d][2].real) + 1j * interp(bufs[d][0], bufs[d][2].imag)
                      for d in ids])
    return amp, z


def _windows(arr: np.ndarray, window: int, stride: int) -> np.ndarray:
    """(RX, T, F) → (N, window, RX*F)"""
    rx, T, F = arr.shape
    if T < window:
        raise ValueError(f"세션이 너무 짧다: {T} < {window}")
    return np.stack([
        arr[:, i:i + window, :].transpose(1, 0, 2).reshape(window, rx * F)
        for i in range(0, T - window + 1, stride)
    ])


def _phase_features(zw: np.ndarray) -> np.ndarray:
    """(N, W, RX*64) 복소 윈도 → 위상 특징 (N, 2). 유효 톤만 쓴다.

    개별 서브캐리어 위상은 CFO/SFO 로 프레임마다 통째로 돌아가 쓸 수 없다.
    인접 서브캐리어끼리의 위상**차**는 그 공통 회전이 상쇄되어 채널 정보만 남는다.
    조악한 베이스라인용이다 — 정식 위상 sanitization 은 모델 쪽 과제다.
    """
    N, W, RF = zw.shape
    rx = RF // 64
    z = zw.reshape(N, W, rx, 64)[:, :, :, cs.LLTF_DATA_IDX]          # (N, W, rx, 52)
    d = np.angle(z[:, :, :, 1:] * np.conj(z[:, :, :, :-1]))          # 인접 위상차 ∈ (-π, π]
    d = np.unwrap(d, axis=1)                                         # 시간축 unwrap
    std = d.std(axis=1).mean(axis=(1, 2))
    jit = np.abs(np.diff(d, axis=1)).mean(axis=(1, 2, 3))
    return np.column_stack([std, jit])


def session_feature_matrix(session_dir: Path, rx_ids, window: int, stride: int, mode: str):
    """세션 하나 → (N, n_features)."""
    need_z = mode.endswith("+phase")
    amp, z = _load_aligned(session_dir, rx_ids, need_complex=need_z)
    if mode.startswith("amp52"):
        amp = amp[:, :, cs.LLTF_DATA_IDX]
    aw = _windows(amp, window, stride)
    F = window_features(aw)
    if need_z:
        F = np.column_stack([F, _phase_features(_windows(z, window, stride))])
    return F


# ── 조건(그룹) 읽기 ───────────────────────────────────────────────────────────
def session_group(session_dir: Path, split_by: str) -> str:
    if split_by == "session":
        return session_dir.name
    snap = session_dir / "session_meta_snapshot.yaml"
    meta = read_meta(snap) if snap.is_file() else {}
    if split_by == "date":
        return meta.get("date") or session_dir.parent.name
    key = {"placement": "condition.placement_id", "subject": "condition.subject_id"}[split_by]
    v = meta.get(key, "")
    if not v:
        raise ValueError(f"{session_dir.name}: session_meta_snapshot.yaml 에 {key} 가 없다 — "
                         f"제어판 '실험 정보' 탭에서 기록해야 조건 단위 분할이 가능하다")
    return str(v)


# ── 평가 ─────────────────────────────────────────────────────────────────────
def evaluate(F, y, groups):
    """세션지문 필터 + 그룹 단위 leave-one-out 최근접 중심. (acc, macro_f1, 쓴 특징 수, 버린 특징)"""
    labels = np.unique(y)
    chance = 1.0 / len(labels)
    stab = session_stability(F, y, groups)
    keep = []
    for i in range(F.shape[1]):
        _, cm_i = loso_nearest_centroid(F, y, groups, cols=[i])
        acc_i = np.trace(cm_i) / cm_i.sum() if cm_i.sum() else 0.0
        if acc_i > chance + 0.05 and stab[i] <= 1.0:
            keep.append(i)
    if not keep:
        keep = [0]
    lab, cm = loso_nearest_centroid(F, y, groups, cols=keep)
    if cm.sum() == 0:
        return float("nan"), float("nan"), len(keep), F.shape[1] - len(keep)
    acc = np.trace(cm) / cm.sum()
    f1 = []
    for i in range(len(lab)):
        tp = cm[i, i]; fp = cm[:, i].sum() - tp; fn = cm[i].sum() - tp
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        f1.append(2 * p * r / (p + r) if p + r else 0.0)
    return acc, float(np.mean(f1)), len(keep), F.shape[1] - len(keep)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-root", type=Path, default=REPO_ROOT / "mac_collector_output" / "raw")
    ap.add_argument("--split-by", choices=("session", "date", "placement", "subject"), default="session")
    ap.add_argument("--windows-s", type=float, nargs="+", default=[1.0, 3.0, 5.0])
    ap.add_argument("--features", nargs="+", default=["amp52", "amp64", "amp52+phase"])
    ap.add_argument("--rx-ids", type=int, nargs="+", default=None, help="기본: 세션에 있는 전부")
    ap.add_argument("--out", type=Path, default=None, help="markdown 표 저장 경로")
    args = ap.parse_args()

    sessions = cs.find_sessions(args.raw_root)
    if not sessions:
        print(f"error: 세션이 없습니다 ({args.raw_root})")
        return 1

    # 사용할 RX 구성: 각 1대 + 전부
    all_rx = sorted({d for s in sessions for d in cs.read_session(s)}) if args.rx_ids is None else list(args.rx_ids)
    rx_configs = [(d,) for d in all_rx] + ([tuple(all_rx)] if len(all_rx) > 1 else [])

    labels_of, groups_of = {}, {}
    for s in sessions:
        try:
            labels_of[s] = read_manifest(s)["label"]
            groups_of[s] = session_group(s, args.split_by)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            print(f"  [건너뜀] {s.name}: {exc}")
    sessions = [s for s in sessions if s in labels_of and s in groups_of]
    n_groups = len(set(groups_of.values()))
    print(f"세션 {len(sessions)}개 · 분할={args.split_by} · 그룹 {n_groups}개 · RX {all_rx}")
    if n_groups < 2:
        print("error: 그룹이 2개 이상이어야 leave-one-out 이 가능하다")
        return 1

    rows = []
    cache = {}
    for rx, w_s, mode in itertools.product(rx_configs, args.windows_s, args.features):
        window, stride = int(F_S * w_s), max(1, int(F_S * STRIDE_S))
        Fs, ys, gs, skipped = [], [], [], 0
        for s in sessions:
            key = (s, rx, window, mode)
            try:
                if key not in cache:
                    cache[key] = session_feature_matrix(s, rx, window, stride, mode)
                f = cache[key]
            except ValueError as exc:
                skipped += 1
                continue
            Fs.append(f); ys.append(np.full(len(f), labels_of[s])); gs.append(np.full(len(f), groups_of[s]))
        if not Fs:
            continue
        acc, f1, used, dropped = evaluate(np.concatenate(Fs), np.concatenate(ys), np.concatenate(gs))
        rows.append({"rx": "+".join(map(str, rx)), "window_s": w_s, "features": mode,
                     "acc": acc, "macro_f1": f1, "feat_used": used, "feat_dropped": dropped,
                     "sessions": len(Fs), "skipped": skipped})
        print(f"  rx={rows[-1]['rx']:<12} win={w_s:>3}s  {mode:<12}  acc={acc:.3f}  F1={f1:.3f}"
              f"  (특징 {used} 사용/{dropped} 세션지문 제외{f', 세션 {skipped} 건너뜀' if skipped else ''})")

    chance = 1.0 / len(set(labels_of.values()))
    md = [f"# Ablation — split_by={args.split_by}, 그룹 {n_groups}개, 무작위={chance:.3f}", "",
          "| RX | window | features | acc | macro-F1 | 특징 사용/제외 | 세션 |",
          "|---|---:|---|---:|---:|---:|---:|"]
    for r in rows:
        md.append(f"| {r['rx']} | {r['window_s']:g}s | {r['features']} | {r['acc']:.3f} | {r['macro_f1']:.3f} "
                  f"| {r['feat_used']}/{r['feat_dropped']} | {r['sessions']} |")
    md += ["", "분류기: 세션지문 필터 + 최근접 중심, leave-one-group-out. "
               "학습 모델은 이 표를 넘어야 복잡도가 정당화된다."]
    text = "\n".join(md) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"\n[표] {args.out}")
    else:
        print("\n" + text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
