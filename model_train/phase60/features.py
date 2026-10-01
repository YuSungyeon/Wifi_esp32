#!/usr/bin/env python3
"""위상 60초 모델의 입력 — `.csi` 에서 위상 잔차를 뽑아 창으로 자른다.

**학습과 추론이 반드시 이 파일 하나를 쓴다.** 전처리가 한 줄만 어긋나도 성능이 조용히
떨어지고, 그 사실은 평가 숫자에 드러나지 않는다.

위상 처리: ESP32 LLTF 버퍼를 주파수 순서로 재배열하고(`38..63` = −26..−1, `1..26` = +1..+26),
주파수축으로 unwrap 한 뒤 직선 성분을 최소자승으로 뺀다. 직선의 상수항은 CFO, 기울기는
심볼 타이밍 오프셋이라 패킷마다 요동한다 — 사람 때문에 생긴 변화가 아니다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import csi_store as cs  # noqa: E402
from model_train.preprocessing import preprocess_3rx as pre  # noqa: E402

FEATURE_VERSION = "phase-residual-ls-v1"
RX_ORDER = (101, 102, 103)
#: raw 버퍼 인덱스를 주파수 오름차순으로. 0(DC)과 27~37(가드)은 상시 0이라 제외한다.
RAW_TONE_ORDER = np.r_[38:64, 1:27]
FREQUENCIES = np.r_[-26:0, 1:27].astype(np.float64)
TONES = len(RAW_TONE_ORDER)          # 52
N_COLS = TONES * len(RX_ORDER)       # 156
MIN_COMMON = 27000                   # 공식 전처리와 같은 최소 공통 길이 (5분의 90%)

_DESIGN = np.vstack([FREQUENCIES, np.ones_like(FREQUENCIES)]).T
_PROJ = _DESIGN @ np.linalg.pinv(_DESIGN)


def phase_residual(z: np.ndarray) -> np.ndarray:
    """복소 CSI (..., 52) → 직선 성분을 뺀 위상 잔차."""
    phi = np.unwrap(np.angle(z), axis=-1)
    return phi - phi @ _PROJ.T


def session_phase(session_dir: Path) -> np.ndarray:
    """세션 디렉터리 → (T, 156) 위상 잔차. 누락은 NaN, 5 frame 이하 공백만 보간."""
    frames = cs.read_session(Path(session_dir))
    missing = [rx for rx in RX_ORDER if rx not in frames]
    if missing:
        raise ValueError(f"RX 누락: {missing} — 3-RX 세션이 아니다")
    start = max(int(frames[rx]["hdr"]["tx_seq"][0]) for rx in RX_ORDER)
    end = min(int(frames[rx]["hdr"]["tx_seq"][-1]) for rx in RX_ORDER)
    length = end - start + 1
    if length < MIN_COMMON:
        raise ValueError(f"공통 길이 {length} < {MIN_COMMON}")

    real = np.full((3, length, TONES), np.nan, np.float32)
    imag = np.full((3, length, TONES), np.nan, np.float32)
    present = np.zeros((3, length), bool)
    for ri, rx in enumerate(RX_ORDER):
        f = frames[rx]
        idx = f["hdr"]["tx_seq"].astype(np.int64) - start
        keep = np.flatnonzero((idx >= 0) & (idx < length))
        idx, first = np.unique(idx[keep], return_index=True)   # 중복 tx_seq 는 첫 프레임
        z = cs.complex_csi(f[keep[first]], valid_only=False)[:, RAW_TONE_ORDER]
        real[ri, idx], imag[ri, idx] = z.real, z.imag
        present[ri, idx] = True
    pre.interpolate_short_gaps(real, present, pre.DEFAULT_CONFIG)
    pre.interpolate_short_gaps(imag, present, pre.DEFAULT_CONFIG)

    residual = phase_residual(real + 1j * imag)
    return residual.transpose(1, 0, 2).reshape(length, N_COLS).astype(np.float32)


def valid_starts(phase: np.ndarray, window: int, stride: int) -> np.ndarray:
    """보간되지 않은 누락(NaN)이 낀 창은 제외한다 (공식 전처리 5.11 과 같은 규칙)."""
    bad = np.concatenate(([0], np.cumsum(np.isnan(phase).any(axis=1))))
    starts = np.arange(0, len(phase) - window + 1, stride)
    return starts[bad[starts + window] == bad[starts]]


def windows(phase: np.ndarray, starts: np.ndarray, window: int, downsample: int) -> np.ndarray:
    """(n, window/downsample, 156) — 다운샘플 평균 후 창 평균을 뺀다.

    창 평균 차감이 정규화의 전부다. 위상 잔차는 이미 상대값이라 스케일 조정이 필요 없다.
    """
    view = np.lib.stride_tricks.sliding_window_view(phase, window, axis=0)
    w = view[starts].transpose(0, 2, 1)                 # (n, window, 156)
    n, t, f = w.shape
    w = w[:, : t - t % downsample].reshape(n, t // downsample, downsample, f).mean(axis=2)
    return (w - w.mean(axis=1, keepdims=True)).astype(np.float32)
