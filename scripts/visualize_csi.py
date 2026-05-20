#!/usr/bin/env python3
"""
JSONL 세션 디렉터리 → RX별 CSI 워터폴을 한 PNG에 device_id 서브플롯으로 저장.

  python scripts/visualize_csi.py --session-dir session_1
  python scripts/visualize_csi.py --session-dir mac_collector_output/raw/20260521/session_1
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

F_S = 100  # Hz — 펌웨어/RX 전송 주기와 동일
MAX_DISPLAY_ROWS = 4000  # PNG 가독성·용량용 시간축 다운샘플 상한
DEFAULT_OUT_NAME = "csi_waterfall.png"


def load_device_buffers(session_dir: Path) -> Dict[int, List[Tuple[int, np.ndarray]]]:
    """device_<id>.jsonl → {device_id: [(received_at_unix_us, amp), ...]}."""
    buffers: Dict[int, List[Tuple[int, np.ndarray]]] = defaultdict(list)
    paths = sorted(session_dir.glob("device_*.jsonl"))
    if not paths:
        raise FileNotFoundError(f"device_*.jsonl 없음: {session_dir}")

    pat = re.compile(r"device_(\d+)\.jsonl$")
    for jsonl_path in paths:
        m = pat.search(jsonl_path.name)
        if not m:
            continue
        device_id = int(m.group(1))
        with jsonl_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                pkt = json.loads(line)
                t_us = int(pkt["received_at_unix_us"])
                amp = np.asarray(pkt["csi_amp"], dtype=np.float64)
                buffers[device_id].append((t_us, amp))

    for dev in buffers:
        buffers[dev].sort(key=lambda x: x[0])
    return dict(buffers)


def buffer_to_waterfall(
    buf: List[Tuple[int, np.ndarray]],
    *,
    f_s: float = F_S,
) -> Tuple[np.ndarray, np.ndarray, int]:
    """
    단일 RX 버퍼 → (T, N_sub) 행렬, 상대 시간축(초), 서브캐리어 수.
    불균일 타임스탬프는 100Hz 격자에 선형 보간.
    """
    if not buf:
        return np.empty((0, 0), dtype=np.float64), np.array([]), 0

    ts = np.array([t for t, _ in buf], dtype=np.float64) / 1e6
    amps = [a for _, a in buf]
    n_sub = int(max(a.shape[0] for a in amps))
    amp_stack = np.empty((len(amps), n_sub), dtype=np.float64)
    for i, a in enumerate(amps):
        if a.shape[0] < n_sub:
            row = np.zeros(n_sub, dtype=np.float64)
            row[: a.shape[0]] = a
            amp_stack[i] = row
        else:
            amp_stack[i] = a[:n_sub]

    t0 = ts[0]
    t_grid = np.arange(ts[0], ts[-1], 1.0 / f_s, dtype=np.float64)
    if len(t_grid) == 0:
        t_grid = ts[:1]

    out = np.empty((len(t_grid), n_sub), dtype=np.float64)
    for k in range(n_sub):
        out[:, k] = np.interp(t_grid, ts, amp_stack[:, k])

    rel_t = t_grid - t0
    return out, rel_t, n_sub


def downsample_time(matrix: np.ndarray, rel_t: np.ndarray, max_rows: int) -> Tuple[np.ndarray, np.ndarray]:
    if matrix.shape[0] <= max_rows:
        return matrix, rel_t
    idx = np.linspace(0, matrix.shape[0] - 1, max_rows, dtype=int)
    return matrix[idx], rel_t[idx]


def render_combined_waterfall_png(
    panels: Dict[int, Tuple[np.ndarray, np.ndarray]],
    *,
    session_dir: Path,
    out_name: str = DEFAULT_OUT_NAME,
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.gridspec as gridspec
    import matplotlib.pyplot as plt

    device_ids = sorted(panels)
    if not device_ids:
        raise ValueError("시각화할 device 없음")

    n = len(device_ids)
    fig_h = max(3.5 * n, 4.0)
    fig = plt.figure(figsize=(13, fig_h), dpi=120)
    # 좌: 워터폴, 우: colorbar 전용 열 (워터폴과 겹치지 않음)
    gs = gridspec.GridSpec(
        n,
        2,
        figure=fig,
        width_ratios=[1, 0.045],
        wspace=0.12,
        hspace=0.28,
        left=0.07,
        right=0.96,
        top=0.94,
        bottom=0.06,
    )

    vmin = min(float(p[0].min()) for p in panels.values() if p[0].size > 0)
    vmax = max(float(p[0].max()) for p in panels.values() if p[0].size > 0)
    if vmin == vmax:
        vmax = vmin + 1e-6

    last_im = None
    plot_axes = []
    for i, device_id in enumerate(device_ids):
        ax = fig.add_subplot(gs[i, 0])
        plot_axes.append(ax)
        matrix, rel_t = panels[device_id]
        if matrix.size == 0:
            ax.set_visible(False)
            continue
        extent = [float(rel_t[0]), float(rel_t[-1]), 0, matrix.shape[1]]
        last_im = ax.imshow(
            matrix.T,
            aspect="auto",
            origin="lower",
            extent=extent,
            cmap="viridis",
            interpolation="nearest",
            vmin=vmin,
            vmax=vmax,
        )
        duration_s = rel_t[-1] - rel_t[0] if len(rel_t) > 1 else 0.0
        ax.set_ylabel("Subcarrier")
        ax.set_title(f"device_id = {device_id}  ({matrix.shape[0]} samples, ~{duration_s:.1f}s)")

    if plot_axes:
        plot_axes[-1].set_xlabel("Time (s, from first packet per RX)")
    if last_im is not None:
        cax = fig.add_subplot(gs[:, 1])
        fig.colorbar(last_im, cax=cax, label="Amplitude (on-device norm)")
    fig.suptitle("CSI amplitude waterfall")
    out_path = session_dir / out_name
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def generate_session_waterfall(
    session_dir: Path,
    *,
    f_s: float = F_S,
    max_display_rows: int = MAX_DISPLAY_ROWS,
    out_name: str = DEFAULT_OUT_NAME,
) -> Path:
    session_dir = session_dir.resolve()
    buffers = load_device_buffers(session_dir)
    if not buffers:
        raise ValueError(f"패킷 없음: {session_dir}")

    panels: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
    for device_id in sorted(buffers):
        matrix, rel_t, n_sub = buffer_to_waterfall(buffers[device_id], f_s=f_s)
        matrix, rel_t = downsample_time(matrix, rel_t, max_display_rows)
        panels[device_id] = (matrix, rel_t)
        duration_s = rel_t[-1] - rel_t[0] if len(rel_t) > 1 else 0.0
        print(
            f"[viz] device_{device_id}: {matrix.shape[0]}×{n_sub} (~{duration_s:.1f}s)"
        )

    out_path = render_combined_waterfall_png(panels, session_dir=session_dir, out_name=out_name)
    print(f"[viz] → {out_path.name} ({len(panels)} RX)")
    return out_path


def find_latest_session_dir(output_base: Path, session_id: int) -> Optional[Path]:
    candidates = list(output_base.glob(f"raw/*/session_{session_id}"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_session_id_from_meta(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = re.match(r"^session_id:\s*(\d+)\s*$", stripped)
        if m:
            return int(m.group(1))
    raise ValueError(f"session_id not found in {path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="CSI JSONL → 워터폴 PNG (RX별 서브플롯 1장)")
    parser.add_argument(
        "--session-dir",
        type=Path,
        help="session_<id> 디렉터리 (device_*.jsonl 포함)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("mac_collector_output"),
        help="--session-id 사용 시 raw/*/session_<id> 검색 기준",
    )
    parser.add_argument(
        "--session-id",
        type=int,
        default=None,
        help="output-dir 아래 최신 session_<id> 폴더 자동 선택",
    )
    parser.add_argument(
        "--session-meta",
        type=Path,
        default=None,
        help="session_id 추출용 yaml (--session-id 미지정 시)",
    )
    parser.add_argument(
        "--out-name",
        type=str,
        default=DEFAULT_OUT_NAME,
        help=f"출력 PNG 파일명 (기본: {DEFAULT_OUT_NAME})",
    )
    parser.add_argument(
        "--max-display-rows",
        type=int,
        default=MAX_DISPLAY_ROWS,
        help="PNG 시간축 최대 행 수 (다운샘플)",
    )
    args = parser.parse_args()

    if args.session_dir:
        session_dir = args.session_dir.resolve()
    else:
        session_id = args.session_id
        if session_id is None:
            meta = args.session_meta or (Path(__file__).resolve().parent.parent / "mac_collector" / "session_meta.yaml")
            session_id = load_session_id_from_meta(meta)
        found = find_latest_session_dir(args.output_dir.resolve(), session_id)
        if found is None:
            print(f"error: session_{session_id} 폴더 없음 under {args.output_dir}", file=sys.stderr)
            return 1
        session_dir = found
        print(f"[viz] session_dir={session_dir}")

    if not session_dir.is_dir():
        print(f"error: not a directory: {session_dir}", file=sys.stderr)
        return 1

    try:
        out_path = generate_session_waterfall(
            session_dir,
            max_display_rows=args.max_display_rows,
            out_name=args.out_name,
        )
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"[viz] 완료: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
