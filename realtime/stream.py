#!/usr/bin/env python3
"""저장 중인 `.csi` 를 따라 읽어 tx_seq 격자에 정렬한 진폭 링버퍼로 유지한다.

수집 프로세스(`csi_serial_reader.py`)는 건드리지 않는다. 파일 끝에 붙는 프레임만 읽고,
잘린 프레임은 다음 poll 로 미룬다. 정렬 규칙은 공식 전처리와 같다 — tx_seq 가 격자,
빈 칸은 NaN, 5 frame 이하 짧은 공백만 보간.

열 순서는 배포 모델의 입력 계약을 그대로 따른다: RX 101/102/103 × raw 톤 `[38..63, 2..26]`
= 153열 (`model_train/robust/model.pt` 의 `raw_tone_order`).
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import csi_store as cs  # noqa: E402
from model_train.preprocessing import preprocess_3rx as pre  # noqa: E402
from model_train.preprocessing.phase_features import RAW_INDICES  # noqa: E402

RX_ORDER = (101, 102, 103)
TONES = len(RAW_INDICES)          # 51
N_COLS = TONES * len(RX_ORDER)    # 153


@dataclass
class Window:
    amp: np.ndarray      # (T, 153) float32, 누락은 NaN
    tx_seq0: int         # 첫 칸의 tx_seq
    rx_alive: tuple      # 이 창 구간에서 프레임이 있던 RX
    hz: float            # 이 창의 RX당 평균 수집률 — 실시간·재생 모두에서 같은 뜻


class CsiStream:
    """세션 디렉터리의 `.csi` 들을 tail 로 읽어 최근 구간을 메모리에 유지한다."""

    def __init__(self, session_dir: Path, capacity: int = 24000,
                 rx_order: tuple = RX_ORDER) -> None:
        self.dir = Path(session_dir)
        self.rx_order = rx_order
        self.capacity = capacity
        self._offset = {rx: 0 for rx in rx_order}
        self._last_seen = {rx: 0.0 for rx in rx_order}
        self._amp = np.full((capacity, N_COLS), np.nan, np.float32)
        self._present = np.zeros((len(rx_order), capacity), bool)
        self._base: int | None = None    # 0번 칸의 tx_seq
        self._end = 0                    # 채워진 칸 수
        self._cursor: int | None = None  # 재생용 창 끝. None 이면 항상 최신

    # ── 읽기 ────────────────────────────────────────────────────────────────
    def poll(self) -> int:
        """새로 붙은 프레임을 읽어 넣고 개수를 돌려준다."""
        added = 0
        for ri, rx in enumerate(self.rx_order):
            path = self.dir / f"device_{rx}.csi"
            if not path.is_file():
                continue
            whole = (path.stat().st_size - self._offset[rx]) // cs.CSI_FRAME_SIZE
            if whole <= 0:
                continue
            with path.open("rb") as fp:
                fp.seek(self._offset[rx])
                blob = fp.read(whole * cs.CSI_FRAME_SIZE)
            self._offset[rx] += len(blob)
            frames = np.frombuffer(blob, dtype=cs.CSI_FRAME_DTYPE)
            self._ingest(ri, frames)
            self._last_seen[rx] = time.monotonic()
            added += len(frames)
        return added

    def _ingest(self, ri: int, frames: np.ndarray) -> None:
        tx = frames["hdr"]["tx_seq"].astype(np.int64)
        if self._base is None:
            self._base = int(tx.min())
        idx = tx - self._base
        if int(idx.max()) >= self.capacity:
            self._roll(int(idx.max()) - self.capacity + 1)
            idx = tx - self._base
        keep = idx >= 0
        idx, frames = idx[keep], frames[keep]
        if len(idx) == 0:
            return
        idx, first = np.unique(idx, return_index=True)   # 중복 tx_seq 는 첫 프레임
        z = cs.complex_csi(frames[first], valid_only=False)[:, RAW_INDICES]
        self._amp[idx, ri * TONES:(ri + 1) * TONES] = np.abs(z).astype(np.float32)
        self._present[ri, idx] = True
        self._end = max(self._end, int(idx.max()) + 1)

    def _roll(self, shift: int) -> None:
        shift = min(shift, self.capacity)
        self._amp[:-shift] = self._amp[shift:]
        self._present[:, :-shift] = self._present[:, shift:]
        self._amp[-shift:] = np.nan
        self._present[:, -shift:] = False
        self._base += shift
        self._end = max(0, self._end - shift)
        if self._cursor is not None:
            self._cursor = max(0, self._cursor - shift)

    # ── 창 꺼내기 ───────────────────────────────────────────────────────────
    def latest(self, width: int) -> Window | None:
        """최근 width frame. 아직 덜 찼으면 None. 짧은 공백은 공식 규칙대로 보간한다."""
        end = self._end if self._cursor is None else self._cursor
        if self._base is None or end < width:
            return None
        s = end - width
        amp = self._amp[s:end].copy()
        present = self._present[:, s:end].copy()
        # (3, T, 51) 로 보고 보간 — 공식 전처리와 같은 함수, 같은 최대 공백(5)
        view = amp.reshape(width, len(self.rx_order), TONES).transpose(1, 0, 2).copy()
        pre.interpolate_short_gaps(view, present, pre.DEFAULT_CONFIG)
        amp = view.transpose(1, 0, 2).reshape(width, N_COLS)
        counts = present.sum(axis=1)
        # RX 가 창의 10% 미만만 채웠으면 끊긴 것으로 본다 (한두 프레임 잔재에 속지 않게)
        alive = tuple(rx for ri, rx in enumerate(self.rx_order) if counts[ri] > 0.1 * width)
        hz = float(counts[[self.rx_order.index(rx) for rx in alive]].mean() / (width / 100)) if alive else 0.0
        return Window(amp=amp, tx_seq0=self._base + s, rx_alive=alive, hz=hz)

    def advance(self, stride: int) -> bool:
        """재생 커서를 stride 만큼 민다. 끝까지 갔으면 False."""
        if self._cursor is None:
            self._cursor = self._end
            return self._end > 0
        if self._cursor + stride > self._end:
            return False
        self._cursor += stride
        return True

    def rewind(self, width: int) -> None:
        """재생을 첫 창 끝에 맞춘다."""
        self._cursor = min(width, self._end)

    def rx_alive(self, within_s: float = 3.0) -> tuple:
        """최근에 프레임이 들어온 RX (실시간 모드용)."""
        now = time.monotonic()
        return tuple(rx for rx in self.rx_order
                     if self._last_seen[rx] and now - self._last_seen[rx] <= within_s)
