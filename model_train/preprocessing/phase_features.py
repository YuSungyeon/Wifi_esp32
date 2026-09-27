"""Experimental 51-tone amplitude/phase contract; no model or test-set fitting."""
from __future__ import annotations

import numpy as np

RAW_INDICES = np.r_[38:64, 2:27]
FREQUENCIES = np.r_[-26:0, 2:27].astype(np.float64)
BANDS = (slice(0, 26), slice(26, 51))
FEATURE_VERSION = "phase51-bandwise-ls-sincos-v1"
N_AMP = 153
N_FEATURES = 459


def detrend_phase(theta):
    """Unwrap each contiguous frequency band, remove its least-squares line.

    All leading axes are independent packets/RXs. No time-axis unwrap.
    Returns residual and bandwise [slope, intercept] coefficients in radians.
    """
    theta = np.asarray(theta, dtype=np.float64)
    if theta.shape[-1] != 51:
        raise ValueError("expected 51 frequency-ordered tones")
    residual = np.empty_like(theta)
    coefficients = []
    for band in BANDS:
        k = FREQUENCIES[band]
        kc = k - k.mean()
        values = np.unwrap(theta[..., band], axis=-1)
        mean = values.mean(axis=-1, keepdims=True)
        slope = ((values - mean) * kc).sum(axis=-1, keepdims=True) / (kc @ kc)
        residual[..., band] = values - mean - slope * kc
        coefficients.append(np.concatenate((slope, mean - slope * k.mean()), axis=-1))
    return residual, np.stack(coefficients, axis=-2)


def extract(z64):
    """Complex CSI (T,64) -> amplitude, sin, cos, validity, fit coefficients."""
    z = np.asarray(z64)[:, RAW_INDICES]
    amp = np.abs(z)
    valid = (np.isfinite(z).all(axis=1) & (amp > 0).all(axis=1))
    residual = np.full(amp.shape, np.nan, dtype=np.float64)
    fits = np.full((len(z), 2, 2), np.nan, dtype=np.float64)
    if valid.any():
        residual[valid], fits[valid] = detrend_phase(np.angle(z[valid]))
    return amp.astype(np.float32), np.sin(residual).astype(np.float32), np.cos(residual).astype(np.float32), valid, fits


def interpolate_phase(sin_values, cos_values, valid, max_gap=5):
    """Fill short internal gaps using normalized chords, rejecting >=90° endpoints.

    Arrays are (RX,T,51). The original validity mask is never modified.
    """
    filled = np.zeros_like(valid)
    rejected = {"angle": 0, "norm": 0, "long": 0}
    for rx in range(len(valid)):
        observed = np.flatnonzero(valid[rx])
        for left, right in zip(observed[:-1], observed[1:]):
            gap = int(right - left - 1)
            if not gap:
                continue
            if gap > max_gap:
                rejected["long"] += 1
                continue
            sl, sr = sin_values[rx, left], sin_values[rx, right]
            cl, cr = cos_values[rx, left], cos_values[rx, right]
            # dot <= 0 means the shortest angle is >= pi/2.
            if np.any(sl.astype(float) * sr + cl.astype(float) * cr <= 0):
                rejected["angle"] += 1
                continue
            w = (np.arange(1, gap + 1, dtype=float) / (right - left))[:, None]
            si, co = (1-w)*sl + w*sr, (1-w)*cl + w*cr
            norm = np.hypot(si, co)
            if np.any(norm <= 1e-6) or not np.isfinite(norm).all():
                rejected["norm"] += 1
                continue
            sin_values[rx, left+1:right] = si / norm
            cos_values[rx, left+1:right] = co / norm
            filled[rx, left+1:right] = True
    return filled, rejected


def normalization_from_sessions(sessions, window=300):
    """Train windows only; overlap repeats exactly as the official baseline."""
    total = np.zeros(N_AMP, dtype=np.float64)
    squares = total.copy()
    n = 0
    for amp, starts in sessions:
        # Count frame multiplicity without allocating overlapping windows.
        delta = np.zeros(len(amp) + 1, dtype=np.int64)
        np.add.at(delta, starts, 1)
        np.add.at(delta, np.asarray(starts) + window, -1)
        counts = np.cumsum(delta[:-1])
        used = counts > 0
        values = np.asarray(amp[used], dtype=np.float64)
        if not np.isfinite(values).all():
            raise ValueError("non-finite amplitude in retained train windows")
        weights = counts[used, None]
        total += (values * weights).sum(axis=0)
        squares += (values * values * weights).sum(axis=0)
        n += int(counts.sum())
    if n == 0:
        raise ValueError("no train windows")
    mean = np.zeros(N_FEATURES, dtype=np.float64)
    std = np.ones(N_FEATURES, dtype=np.float64)
    mean[:N_AMP] = total / n
    std[:N_AMP] = np.sqrt(np.maximum(squares/n - mean[:N_AMP]**2, 0))
    return mean, std, np.where(std < 1e-6, 1.0, std), n


def transform_window(x, mean, std_safe, mode):
    if mode not in ("A", "P", "AP"):
        raise ValueError(f"unknown mode: {mode}")
    out = ((np.asarray(x, dtype=np.float32) - mean) / std_safe).astype(np.float32)
    if mode == "A":
        out[:, N_AMP:] = 0
    elif mode == "P":
        out[:, :N_AMP] = 0
    if not np.isfinite(out).all():
        raise ValueError("non-finite model input")
    return out
