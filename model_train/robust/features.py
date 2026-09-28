"""Window-local CSI features: no labels, session statistics, or fitted transforms."""
from __future__ import annotations

import numpy as np
from scipy.fft import rfft, rfftfreq

VERSION = "robust-csi-window-v1"
LENGTHS = (300, 500, 1000, 2000, 3000)
SETS = ("A_global", "P_global", "AP_global", "A_rx", "AP_rx", "AP_profile")


def eligible(x):
    """Return amplitude validity and phase coverage (one value per RX)."""
    x = np.asarray(x)
    if x.ndim != 2 or x.shape[1] != 459:
        raise ValueError("expected (T,459) phase51 input")
    a = bool(np.isfinite(x[:, :153]).all())
    p = np.isfinite(x[:, 153:]).reshape(len(x), 2, 3, 51).all(axis=(1, 3)).mean(0)
    return a, p


def _moments(v):
    # v: batch,time,RX,tone; all are centered within this window.
    v = v - v.mean(1, keepdims=True)
    energy = np.mean(v * v, axis=1)
    std = np.sqrt(energy)
    delta = np.mean(np.abs(np.diff(v, axis=1)), axis=1)
    result = [np.log(std + 1e-6), np.log(delta + 1e-6)]
    for lag in (1, 5, 10):
        u, w = v[:, :-lag], v[:, lag:]
        ac = np.mean(u * w, axis=1) / np.maximum(
            np.sqrt(np.mean(u*u, axis=1)*np.mean(w*w, axis=1)), 1e-12)
        result.append(np.clip(ac, -1, 1))
    hann = np.hanning(v.shape[1]).astype(np.float32)
    spectrum = np.abs(rfft(v * hann[None, :, None, None], axis=1)) ** 2
    freq = rfftfreq(v.shape[1], d=.1)
    total = spectrum[:, 1:].sum(1)
    for lo, hi in ((.1, .5), (.5, 1.), (1., 2.), (2., 5.01)):
        band = spectrum[:, (freq >= lo) & (freq < hi)].sum(1)
        result.extend([band / np.maximum(total, 1e-12),
                       np.log(np.sqrt(band) / v.shape[1] + 1e-6)])
    # Order-free temporal shape summaries, stable for near-constant inputs.
    result.append(np.mean(np.abs(v), axis=1) / np.maximum(std, 1e-6))
    return np.stack(result, axis=-1)


def _pool(stats):
    # batch,RX,tone,stat -> order-invariant and RX-ordered representations.
    def summary(z, axis):
        return np.concatenate([np.mean(z, axis), np.std(z, axis),
                               *np.quantile(z, [.1, .5, .9], axis=axis)], axis=-1)
    ordered = summary(stats, 2).reshape(len(stats), -1)
    invariant = summary(stats.reshape(len(stats), -1, stats.shape[-1]), 1)
    return invariant.astype(np.float32), ordered.astype(np.float32)


def extract_batch(windows):
    """Extract all feature sets from (B,T,459) without access to other windows."""
    x = np.asarray(windows, dtype=np.float32)
    if x.ndim != 3 or x.shape[2] != 459 or x.shape[1] not in LENGTHS:
        raise ValueError("expected (B,T,459), T in supported window lengths")
    b, t, _ = x.shape
    amp = x[:, :, :153].reshape(b, t, 3, 51)
    if not np.isfinite(amp).all():
        raise ValueError("unfilled amplitude gaps cannot enter a window")
    relative = amp / (amp.mean(1, keepdims=True) + .5) - 1
    shape = amp / (amp.mean(3, keepdims=True) + .5)
    a_stats = []
    for signal in (relative, shape):
        bins = signal.reshape(b, t // 10, 10, 3, 51)
        slow = bins.mean(2)
        stats = _moments(slow)
        fast = np.stack([np.log(signal.std(1) + 1e-6),
                         np.log(np.abs(np.diff(signal, axis=1)).mean(1) + 1e-6),
                         np.log(bins.std(2).mean(1) + 1e-6)], axis=-1)
        a_stats.append(np.concatenate([stats, fast], axis=-1))
    ag, ar = _pool(np.concatenate(a_stats, axis=-1))

    sine = x[:, :, 153:306].reshape(b, t, 3, 51)
    cosine = x[:, :, 306:].reshape(b, t, 3, 51)
    valid = np.isfinite(sine) & np.isfinite(cosine)
    if (valid.all(3).mean(1) < .8).any():
        raise ValueError("phase coverage below frozen 80% gate")
    counts = valid.reshape(b, t // 10, 10, 3, 51).sum(2)
    zb = []
    for value in (cosine, sine):
        value = np.where(valid, value, 0).reshape(b, t // 10, 10, 3, 51).sum(2)
        zb.append(value / np.maximum(counts, 1))
    z = zb[0] + 1j * zb[1]
    observed = counts >= 5
    center = np.sum(np.where(observed, z, 0), axis=1) / np.maximum(observed.sum(1), 1)
    rotation = np.conj(center) / np.maximum(np.abs(center), 1e-6)
    centered = (z - center[:, None]) * rotation[:, None]
    centered = np.where(observed, centered, 0)
    p_stats = np.concatenate([_moments(centered.imag), _moments(centered.real),
                              np.log(np.maximum(1 - np.abs(center), 0) + 1e-6)[..., None]], axis=-1)
    pg, pr = _pool(p_stats)
    # Deliberate profile control; absolute level is retained only in this set.
    profile = np.concatenate([np.log(amp.mean(1).reshape(b, -1) + .5),
                              shape.mean(1).reshape(b, -1)], axis=1)
    result = {"A_global": ag, "P_global": pg, "AP_global": np.hstack([ag, pg]),
              "A_rx": ar, "AP_rx": np.hstack([ar, pr]),
              "AP_profile": np.hstack([ar, pr, profile])}
    if not all(np.isfinite(v).all() for v in result.values()):
        raise ValueError("non-finite extracted feature")
    return {k: np.asarray(v, dtype=np.float32) for k, v in result.items()}
