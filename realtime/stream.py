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
from model_train.phase60 import features as phase_features  # noqa: E402
from model_train.preprocessing.phase_features import RAW_INDICES  # noqa: E402

RX_ORDER = (101, 102, 103)
#: 모델마다 쓰는 톤이 다르다. 복소 I/Q 를 64톤 그대로 들고 있다가 창을 꺼낼 때 골라 쓴다.
AMP_TONES = RAW_INDICES                     # 진폭 모델: [38..63, 2..26] 51톤
PHASE_TONES = phase_features.RAW_TONE_ORDER  # 위상 모델: [38..63, 1..26] 52톤
TONES = len(AMP_TONES)            # 51 (기존 호출부 호환)
N_COLS = TONES * len(RX_ORDER)    # 153


@dataclass
class Window:
    amp: np.ndarray      # (T, 153) float32 — 진폭 모델 입력, 누락은 NaN
    phase: np.ndarray    # (T, 156) float32 — 위상 잔차, 누락은 NaN
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
        # 64톤 복소를 그대로 둔다 — 진폭·위상 어느 모델이 와도 여기서 만들어 낸다
        self._re = np.full((len(rx_order), capacity, 64), np.nan, np.float32)
        self._im = np.full((len(rx_order), capacity, 64), np.nan, np.float32)
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
        z = cs.complex_csi(frames[first], valid_only=False)
        self._re[ri, idx], self._im[ri, idx] = z.real, z.imag
        self._present[ri, idx] = True
        self._end = max(self._end, int(idx.max()) + 1)

    def _roll(self, shift: int) -> None:
        # 한 번에 용량보다 큰 묶음(긴 파일을 통째로 읽는 재생)이 와도 base 는 shift 만큼 민다.
        # 버퍼 이동만 용량으로 자른다 — 전부 밀리면 전부 비운다.
        move = min(shift, self.capacity)
        if move == self.capacity:
            self._re[:], self._im[:], self._present[:] = np.nan, np.nan, False
        else:
            for buf in (self._re, self._im):
                buf[:, :-move] = buf[:, move:]
                buf[:, -move:] = np.nan
            self._present[:, :-move] = self._present[:, move:]
            self._present[:, -move:] = False
        self._base += shift
        self._end = max(0, self._end - shift)
        if self._cursor is not None:
            self._cursor = max(0, self._cursor - shift)

    # ── 창 꺼내기 ───────────────────────────────────────────────────────────
    def latest(self, width: int) -> Window | None:
        """최근 width frame. 아직 덜 찼으면 None. 짧은 공백은 공식 규칙대로 보간한다."""
        end = self._end if self._cursor is None else self._cursor
        if self._cursor is None:
            # 실시간: RX 파일은 서로 조금씩 늦는다(버퍼 flush·USB 타이밍). 가장 새 프레임까지 창을
            # 잡으면 늦은 RX 의 꼬리가 비어 창마다 NaN 이 생겨 판단이 한 번도 안 난다. **모든 RX 가
            # 도달한 지점**을 창 끝으로 한다. 한 RX 가 멈추면 창도 멈춘다 — 그 편이 그 RX 를 빼고
            # 영원히 판단 못 하는 것보다 낫고, 끊김 자체는 rx_alive()(파일 갱신 시각)가 드러낸다.
            ends = [int(nz[-1]) + 1 for ri in range(len(self.rx_order))
                    if len(nz := np.flatnonzero(self._present[ri, :self._end]))]
            if ends:
                end = min(ends)
        if self._base is None or end < width:
            return None
        s = end - width
        present = self._present[:, s:end].copy()
        re = self._re[:, s:end].copy()
        im = self._im[:, s:end].copy()
        # **진폭은 크기를 보간한다** — 공식 진폭 경로(robust/predict.load_raw_amplitude)와 같다.
        # I/Q 를 따로 보간하면 CFO 로 프레임마다 회전하는 성분이 상쇄돼 진폭이 0 으로 꺼지고,
        # 짧은 창에서는 그 한 줄이 움직임처럼 보인다 (공백 1칸에 |z| 0.0, 학습 경로 최소 1.4).
        amp3 = np.abs(re[:, :, AMP_TONES] + 1j * im[:, :, AMP_TONES]).astype(np.float32)
        pre.interpolate_short_gaps(amp3, present.copy(), pre.DEFAULT_CONFIG)
        amp = amp3.transpose(1, 0, 2).reshape(width, len(self.rx_order) * len(AMP_TONES))
        # 위상은 성분별 보간이 맞다 — 공식 위상 경로(phase60/features.session_phase)와 같다
        pre.interpolate_short_gaps(re, present.copy(), pre.DEFAULT_CONFIG)
        pre.interpolate_short_gaps(im, present.copy(), pre.DEFAULT_CONFIG)
        residual = phase_features.phase_residual(
            (re + 1j * im)[:, :, PHASE_TONES])
        phase = (residual.astype(np.float32)
                 .transpose(1, 0, 2).reshape(width, len(self.rx_order) * len(PHASE_TONES)))
        counts = present.sum(axis=1)
        # RX 가 창의 10% 미만만 채웠으면 끊긴 것으로 본다 (한두 프레임 잔재에 속지 않게)
        alive = tuple(rx for ri, rx in enumerate(self.rx_order) if counts[ri] > 0.1 * width)
        hz = float(counts[[self.rx_order.index(rx) for rx in alive]].mean() / (width / 100)) if alive else 0.0
        return Window(amp=amp, phase=phase, tx_seq0=self._base + s, rx_alive=alive, hz=hz)

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
