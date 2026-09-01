#!/usr/bin/env python3
"""수집한 세션들로 3-class 가 실제로 갈리는지 진단한다 (PyTorch 불필요).

본격적으로 수십 세션을 찍기 전에, **지금 배치·RX 구성으로 클래스가 갈리기는 하는지**를
파일럿 몇 세션으로 먼저 확인하기 위한 도구다. 안 갈린다면 데이터를 더 모아도 소용없고
보드 배치나 RX 대수를 바꿔야 한다.

    python scripts/check_separability.py
    python scripts/check_separability.py --raw-root mac_collector_output/raw --out report.png

**평가는 반드시 세션 단위 교차검증(leave-one-session-out)으로 한다.** 윈도가 3초 길이에
0.3초 stride 라 이웃끼리 90% 겹치므로, 윈도를 무작위로 나누면 같은 3초가 학습·평가 양쪽에
들어가 정확도가 실제보다 훨씬 높게 나온다.

특히 보는 것: `empty` vs `static`. `action` 은 시간축 진폭 변동이 커서 잘 갈리지만, 정지한
사람은 다중경로를 바꿀 뿐 시간에 따라 흔들지 않아 훨씬 어렵다.
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import csi_store as cs  # noqa: E402
from csi_session import read_manifest  # noqa: E402

F_S = 100          # Hz — tx_seq 1스텝 = 10ms
WINDOW = 300       # 3초
STRIDE = 30        # 0.3초

# 공식 학습 전처리는 model_train/preprocessing/preprocess_3rx.py 다. 이 진단 도구는
# 그보다 훨씬 단순한 정렬만 쓴다 — "이 배치로 갈리기는 하는가"를 빨리 보는 게 목적이고,
# 공식 전처리의 손상 제거·mask 규칙에 결과가 좌우되지 않게 하려는 의도도 있다.

#: (키, 표시명, 설명). 전부 **스케일 불변**이거나 그렇게 정규화한 값이다 —
#: 절대 진폭은 세션마다 배치·거리로 달라져 세션 간 일반화가 안 된다.
FEATURES = [
    ("cv_mean", "변동계수 평균", "서브캐리어별 (시간축 std / 평균)의 평균 — 움직임 에너지"),
    ("cv_max", "변동계수 최대", "가장 민감한 서브캐리어의 변동 — 국소 움직임"),
    ("jitter", "프레임간 변화율", "|Δ진폭| 평균 / 평균 진폭 — 빠른 움직임"),
    ("hi_band", "고주파 비율", "시간축 스펙트럼에서 2Hz 이상이 차지하는 비율"),
    ("sc_spread", "서브캐리어 분산", "서브캐리어 프로파일의 변동계수 — 다중경로 형상"),
]


def window_features(X: np.ndarray) -> np.ndarray:
    """(N, T, F) 진폭 윈도 → (N, len(FEATURES)) 특징."""
    m = X.mean(axis=1)                                   # (N, F) 윈도별 평균 진폭
    m = np.where(m > 1e-9, m, 1e-9)
    s = X.std(axis=1)                                    # (N, F)
    cv = s / m

    d = np.abs(np.diff(X, axis=1)).mean(axis=1) / m      # (N, F)

    # 시간축 스펙트럼에서 2Hz 이상 비율 (DC 제외)
    Xc = X - X.mean(axis=1, keepdims=True)
    P = np.abs(np.fft.rfft(Xc, axis=1)) ** 2             # (N, T//2+1, F)
    freq = np.fft.rfftfreq(X.shape[1], d=1.0 / F_S)
    tot = P[:, 1:, :].sum(axis=1)
    hi = P[:, freq >= 2.0, :].sum(axis=1)
    hi_band = hi / np.where(tot > 1e-12, tot, 1e-12)

    prof = X.mean(axis=1)                                # (N, F) 서브캐리어 프로파일
    sc_spread = prof.std(axis=1) / prof.mean(axis=1)

    return np.column_stack([
        cv.mean(axis=1), cv.max(axis=1), d.mean(axis=1),
        hi_band.mean(axis=1), sc_spread,
    ])


def auc(pos: np.ndarray, neg: np.ndarray) -> float:
    """순위 기반 AUC. 0.5 = 구분 불가, 1.0 또는 0.0 = 완전 분리."""
    if len(pos) == 0 or len(neg) == 0:
        return float("nan")
    both = np.concatenate([pos, neg])
    r = np.empty(len(both), float)
    order = both.argsort()
    r[order] = np.arange(1, len(both) + 1)
    # 동점 처리
    _, inv, cnt = np.unique(both, return_inverse=True, return_counts=True)
    if (cnt > 1).any():
        sums = np.zeros(len(cnt))
        np.add.at(sums, inv, r)
        r = (sums / cnt)[inv]
    n1, n0 = len(pos), len(neg)
    return (r[:n1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)


def loso_nearest_centroid(F: np.ndarray, y: np.ndarray, sess: np.ndarray, cols=None):
    """세션 단위 leave-one-out 최근접 중심. z-score·centroid 를 **학습 폴드에서만** 구한다.

    `cols` 로 쓸 특징을 고를 수 있다. 특징을 고르지 않고 전부 넣으면, 세션마다 값이 널뛰는
    특징(예: 서브캐리어 프로파일)이 유클리드 거리에서 동등한 표를 행사해 판정을 뒤집는다 —
    실제로 그렇게 정확도가 우연 수준까지 떨어지는 것을 관측했다.
    """
    if cols is None:
        cols = list(range(F.shape[1]))
    F = F[:, cols]
    labels = np.unique(y)
    cm = np.zeros((len(labels), len(labels)), int)
    idx = {l: i for i, l in enumerate(labels)}
    for held in np.unique(sess):
        tr, te = sess != held, sess == held
        if len(np.unique(y[tr])) < len(labels):
            continue                     # 남은 폴드에 모든 클래스가 없으면 평가 불가
        mu, sd = F[tr].mean(0), F[tr].std(0)
        sd = np.where(sd > 1e-12, sd, 1e-12)
        Z, Zt = (F[tr] - mu) / sd, (F[te] - mu) / sd
        cent = np.stack([Z[y[tr] == l].mean(0) for l in labels])
        pred = labels[np.argmin(((Zt[:, None, :] - cent[None]) ** 2).sum(-1), axis=1)]
        for t, p in zip(y[te], pred):
            cm[idx[t], idx[p]] += 1
    return labels, cm


def session_stability(F: np.ndarray, y: np.ndarray, sess: np.ndarray) -> np.ndarray:
    """특징별 (같은 클래스 안 세션 간 흩어짐) / (클래스 간 흩어짐).

    1 보다 크면 그 특징은 **클래스보다 세션을 더 잘 구분한다** — 즉 방 배치·보드 위치 같은
    세션 고유 지문이지 행동의 특징이 아니다. 그런 특징으로 학습하면 새 세션에서 무너진다.
    """
    labels = np.unique(y)
    per = {l: np.stack([F[sess == s].mean(0) for s in np.unique(sess[y == l])]) for l in labels}
    within = np.mean([p.std(0, ddof=0) for p in per.values()], axis=0)
    between = np.stack([p.mean(0) for p in per.values()]).std(0, ddof=0)
    return within / np.where(between > 1e-12, between, 1e-12)


def session_windows(session_dir: Path, rx_ids=None) -> np.ndarray:
    """세션 → (N, WINDOW, RX수×52) 진폭 윈도. tx_seq 공통 격자에 선형 보간."""
    buffers = {}
    for dev, frames in cs.read_session(session_dir).items():
        if len(frames) == 0:
            continue
        tx = frames["hdr"]["tx_seq"].astype(np.int64)
        if (np.diff(tx) < 0).any():
            raise ValueError(f"RX{dev}: 수집 중 TX 재부팅 (tx_seq 역행) — 재수집 필요")
        amp = cs.amplitude(frames)
        order = np.argsort(tx, kind="stable")
        tx, amp = tx[order], amp[order]
        keep = np.concatenate(([True], np.diff(tx) > 0))
        buffers[dev] = (tx[keep].astype(np.float64), amp[keep])

    ids = tuple(rx_ids) if rx_ids else tuple(sorted(buffers))
    missing = [d for d in ids if d not in buffers]
    if missing:
        raise ValueError(f"no data for RX {missing} (available: {sorted(buffers)})")

    start = int(max(buffers[d][0][0] for d in ids))
    end = int(min(buffers[d][0][-1] for d in ids))
    if end - start + 1 < WINDOW:
        raise ValueError(f"세션이 너무 짧다: {end - start + 1} < {WINDOW}")
    grid = np.arange(start, end + 1, dtype=np.float64)

    aligned = np.stack([
        np.column_stack([np.interp(grid, buffers[d][0], buffers[d][1][:, k])
                         for k in range(cs.N_SUB)])
        for d in ids
    ])                                              # (RX, T, 52)
    T = aligned.shape[1]
    return np.stack([
        aligned[:, i:i + WINDOW, :].transpose(1, 0, 2).reshape(WINDOW, len(ids) * cs.N_SUB)
        for i in range(0, T - WINDOW + 1, STRIDE)
    ])


def collect(raw_root: Path, rx_ids):
    feats, ys, sess, skipped = [], [], [], []
    per_label = defaultdict(list)
    for d in cs.find_sessions(raw_root):
        try:
            label = read_manifest(d)["label"]
            X = session_windows(d, rx_ids)
        except (ValueError, FileNotFoundError, KeyError) as exc:
            skipped.append((d.name, str(exc).splitlines()[0]))
            continue
        f = window_features(X)
        feats.append(f)
        ys.append(np.full(len(f), label))
        sess.append(np.full(len(f), d.name))
        per_label[label].append(d.name)
    if not feats:
        return None, None, None, per_label, skipped
    return np.concatenate(feats), np.concatenate(ys), np.concatenate(sess), per_label, skipped


def render(F, y, sess, labels, cm, out: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 한글 라벨이 두부(□)로 깨지지 않게. 없으면 조용히 기본 폰트로 둔다.
    have = {f.name for f in font_manager.fontManager.ttflist}
    for cand in ("AppleGothic", "Apple SD Gothic Neo", "Nanum Gothic", "Malgun Gothic",
                 "Noto Sans CJK KR", "NanumGothic"):
        if cand in have:
            plt.rcParams["font.family"] = cand
            plt.rcParams["axes.unicode_minus"] = False
            break

    n = len(FEATURES)
    fig, axes = plt.subplots(1, n + 1, figsize=(3.0 * (n + 1), 3.6), dpi=120)
    rng = np.random.default_rng(0)
    colors = {l: c for l, c in zip(labels, ["#4c78a8", "#f58518", "#54a24b", "#e45756"])}
    for i, (key, title, _) in enumerate(FEATURES):
        ax = axes[i]
        for j, l in enumerate(labels):
            v = F[y == l, i]
            ax.scatter(j + rng.uniform(-0.16, 0.16, len(v)), v, s=4, alpha=0.35,
                       color=colors[l], edgecolors="none")
            ax.plot([j - 0.28, j + 0.28], [np.median(v)] * 2, color="k", lw=1.6)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, fontsize=8)
        ax.set_title(title, fontsize=9)
        ax.tick_params(labelsize=7)

    ax = axes[-1]
    tot = cm.sum(1, keepdims=True)
    norm = cm / np.where(tot > 0, tot, 1)
    ax.imshow(norm, cmap="Blues", vmin=0, vmax=1)
    for a in range(len(labels)):
        for b in range(len(labels)):
            ax.text(b, a, f"{norm[a, b]:.2f}", ha="center", va="center", fontsize=8,
                    color="white" if norm[a, b] > 0.5 else "black")
    ax.set_xticks(range(len(labels))); ax.set_xticklabels(labels, fontsize=8)
    ax.set_yticks(range(len(labels))); ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("예측", fontsize=8); ax.set_ylabel("실제", fontsize=8)
    ax.set_title("세션 단위 LOSO 혼동행렬", fontsize=9)
    fig.suptitle("클래스 분리 가능성 진단", y=0.99)
    fig.tight_layout(rect=[0, 0, 1, 0.93])
    fig.savefig(out)
    plt.close(fig)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-root", type=Path, default=REPO_ROOT / "mac_collector_output" / "raw")
    ap.add_argument("--rx-ids", type=int, nargs="+", default=None)
    ap.add_argument("--out", type=Path, default=None, help="진단 그림 PNG 경로")
    args = ap.parse_args()

    F, y, sess, per_label, skipped = collect(args.raw_root, args.rx_ids)
    if skipped:
        print("[건너뜀]")
        for name, why in skipped:
            print(f"  {name}: {why}")
    if F is None:
        print(f"error: 사용할 세션이 없습니다 ({args.raw_root})")
        return 1

    labels = sorted(per_label)
    print(f"\n[세션 구성]  윈도 {len(F)}개")
    for l in labels:
        print(f"  {l:7s} 세션 {len(per_label[l])}개: {', '.join(per_label[l])}")

    thin = [l for l in labels if len(per_label[l]) < 2]
    if thin:
        print(f"\n[경고] 라벨 {thin} 의 세션이 1개뿐입니다 — 그 클래스는 세션 단위 검증이 불가능하고,")
        print("       '세션 고유의 특징'을 클래스 특징으로 착각할 수 있습니다. 라벨당 2세션 이상 권장.")
    if len(labels) < 2:
        print("\n[중단] 클래스가 1종뿐이라 분리도를 잴 수 없습니다.")
        return 1

    print("\n[특징별 진단]")
    print("  AUC 는 모든 세션의 윈도를 한데 모아 계산한 값이라 **부풀려진다** — 세션마다 다른")
    print("  기저 채널이 클래스 차이처럼 보이기 때문이다. 실제로 세 클래스가 통계적으로 동일한")
    print("  데이터에서도 AUC 가 1.000 까지 나오는 것을 확인했다. 판정은 LOSO 로만 한다.")
    pairs = [(a, b) for i, a in enumerate(labels) for b in labels[i + 1:]]
    print(f"\n  {'특징':<16}" + "".join(f"{a[:5]}/{b[:5]:<7}" for a, b in pairs) + "  LOSO정확도  세션지문")
    stab = session_stability(F, y, sess)
    best_pair, per_feat_acc = {}, []
    for i, (key, title, desc) in enumerate(FEATURES):
        cells = ""
        for a, b in pairs:
            v = auc(F[y == a, i], F[y == b, i])
            v = max(v, 1 - v)                      # 방향 무관
            cells += f"{v:>13.3f}"
            if v > best_pair.get((a, b), (0, ""))[0]:
                best_pair[(a, b)] = (v, title)
        lab_i, cm_i = loso_nearest_centroid(F, y, sess, cols=[i])
        acc_i = np.trace(cm_i) / cm_i.sum() if cm_i.sum() else float("nan")
        per_feat_acc.append(acc_i)
        flag = "  ⚠세션특징" if stab[i] > 1.0 else ""
        print(f"  {title:<16}{cells}{acc_i:>12.3f}{stab[i]:>11.2f}{flag}")
    for (key, title, desc) in FEATURES:
        print(f"    · {title}: {desc}")
    print("  세션지문 = (같은 클래스 안 세션 간 흩어짐)/(클래스 간 흩어짐). 1 이상이면")
    print("             클래스보다 세션을 더 잘 구분한다는 뜻 — 학습에 쓰면 새 세션에서 무너진다.")

    chance = 1.0 / len(labels)
    keep = [i for i, a in enumerate(per_feat_acc) if a > chance + 0.05 and stab[i] <= 1.0]
    if not keep:
        keep = [int(np.nanargmax(per_feat_acc))]
        print(f"\n[경고] 세션 간 일반화되는 특징이 하나도 없습니다 — 가장 나은 것 하나만 씁니다.")
    used = ", ".join(FEATURES[i][1] for i in keep)

    lab, cm = loso_nearest_centroid(F, y, sess, cols=keep)
    total = cm.sum()
    if total == 0:
        print("\n[중단] 세션 단위 교차검증을 할 수 없습니다 — 각 라벨에 세션이 2개 이상 필요합니다.")
        return 1
    acc = np.trace(cm) / total
    print(f"\n[세션 단위 LOSO · 최근접 중심]  정확도 {acc:.3f}  (무작위 = {chance:.3f})")
    print(f"  사용한 특징: {used}")
    print(f"  {'실제\\예측':<10}" + "".join(f"{l:>9}" for l in lab) + "     recall")
    for i, l in enumerate(lab):
        rec = cm[i].sum() and cm[i, i] / cm[i].sum()
        print(f"  {l:<10}" + "".join(f"{c:>9d}" for c in cm[i]) + f"   {rec:>7.3f}")
    print("  (특징 선택에 전체 폴드를 썼으므로 이 수치는 다소 낙관적이다. 특징별 LOSO 정확도가")
    print("   더 보수적인 값이다.)")

    print("\n[판정]  쌍별 2-class LOSO 정확도 기준 (무작위 = 0.500)")
    for a, b in pairs:
        m = (y == a) | (y == b)
        _, cm2 = loso_nearest_centroid(F[m], y[m], sess[m], cols=keep)
        if cm2.sum() == 0:
            print(f"  {a} vs {b}: 세션 부족으로 평가 불가")
            continue
        acc2 = np.trace(cm2) / cm2.sum()
        verdict = ("잘 갈림" if acc2 >= 0.85 else "약하게 갈림" if acc2 >= 0.70 else "사실상 구분 불가")
        pooled = best_pair[(a, b)]
        print(f"  {a} vs {b}: LOSO {acc2:.3f} → {verdict}"
              f"   (참고: 세션 혼합 AUC {pooled[0]:.3f} — 부풀려진 값)")
    if acc < chance + 0.15:
        print("\n  세션 간 일반화가 안 됩니다. 데이터를 더 모으기 전에 배치를 바꾸는 편이 낫습니다:")
        print("   · TX–RX 거리를 벌려 RSSI 를 −25~−40 범위로 (measure_csi_hz.py 의 rssi_med)")
        print("   · RX 를 사람 동선 양쪽으로 분산 배치 (같은 벽면에 몰면 같은 경로를 본다)")
        print("   · RX 대수 늘리기")

    if args.out:
        render(F, y, sess, lab, cm, args.out)
        print(f"\n[그림] {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
