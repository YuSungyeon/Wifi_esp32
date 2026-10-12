#!/usr/bin/env python3
"""저장 중인 `.csi` 를 따라 읽어 tx_seq 격자에 정렬한 모델별 링버퍼로 유지한다.

수집 프로세스(`csi_serial_reader.py`)는 건드리지 않는다. 파일 끝에 붙는 프레임만 읽고,
잘린 프레임은 다음 poll 로 미룬다. 정렬 규칙은 공식 전처리와 같다 — tx_seq 가 격자,
빈 칸은 NaN, 5 frame 이하 짧은 공백만 보간.

RX 101/102/103 순서로 진폭은 raw 톤 `[38..63, 2..26]` 153열,
위상은 `[38..63, 1..26]` 복소 I/Q 156열을 보관한다.
"""
from __future__ import annotations

import sys
import time
import json
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
    amp: np.ndarray | None  # amplitude only: (T,153)
    tx_seq0: int         # 첫 칸의 tx_seq
    rx_alive: tuple      # 이 창 구간에서 프레임이 있던 RX
    hz: float            # 이 창의 RX당 평균 수집률 — 실시간·재생 모두에서 같은 뜻
    phase: np.ndarray | None = None  # phase60 only: (T,156) residual


class CsiStream:
    """세션 디렉터리의 `.csi` 들을 tail 로 읽어 최근 구간을 메모리에 유지한다."""

    def __init__(self, session_dir: Path, capacity: int = 24000,
                 rx_order: tuple = RX_ORDER, model_kind: str = "amplitude") -> None:
        self.dir = Path(session_dir)
        if capacity <= 0 or tuple(rx_order) != RX_ORDER:
            raise ValueError("capacity는 양수, RX 순서는 101/102/103이어야 합니다")
        self.rx_order = rx_order
        if model_kind not in ("amplitude", "phase60"):
            raise ValueError("unsupported stream model kind")
        self.model_kind = model_kind
        if model_kind == "phase60":
            from model_train.phase60 import features as phase
            self.raw_indices = phase.RAW_TONE_ORDER
        else:
            self.raw_indices = RAW_INDICES
        self.tones = len(self.raw_indices)
        self.n_cols = self.tones * len(rx_order)
        self.capacity = capacity
        self._offset = {rx: 0 for rx in rx_order}
        self._last_seen = {rx: 0.0 for rx in rx_order}
        dtype = np.complex64 if model_kind == "phase60" else np.float32
        self._amp = np.full((capacity, self.n_cols), np.nan, dtype)
        self._present = np.zeros((len(rx_order), capacity), bool)
        self._base: int | None = None    # 0번 칸의 tx_seq
        self._end = 0                    # 채워진 칸 수
        self._cursor: int | None = None  # 재생용 창 끝. None 이면 항상 최신
        self._first: dict[int, int] = {}
        self._last: dict[int, int] = {}
        self._boot: dict[int, int] = {}
        self._wire_ids: dict[int, int] = {}
        self._replay = False
        self._replay_limit = None
        manifest = json.loads((self.dir / "session.json").read_text(encoding="utf-8"))
        self.input_kind = manifest.get("purpose")
        self._expected_ids = {int(d["device_id"]): int(d["rx_id"])
                              for d in manifest.get("devices", []) if "rx_id" in d}

    def load_replay(self) -> None:
        """정식 raw 추론과 동일한 전체 공통 구간. 라벨은 읽어 예측에 사용하지 않는다."""
        if self.model_kind == "phase60":
            # Use the same validated ingest as follow, with enough room for the
            # complete common interval. Never invoke the offline 27000-frame gate.
            frames = cs.read_session(self.dir)
            if any(rx not in frames or not len(frames[rx]) for rx in self.rx_order):
                raise ValueError("위상 재생에는 RX 3대의 원본이 필요합니다")
            start = max(int(frames[rx]['hdr']['tx_seq'][0]) for rx in self.rx_order)
            end = min(int(frames[rx]['hdr']['tx_seq'][-1]) for rx in self.rx_order)
            length = end-start+1
            if length < 6000:
                raise ValueError("재생할 공통 구간이 60초 미만입니다")
            if length > sum(len(f) for f in frames.values())*10:
                raise ValueError("송신 순번 공백이 과도한 재생 입력입니다")
            self.capacity = length
            self._amp = np.full((length, self.n_cols), np.nan, np.complex64)
            self._present = np.zeros((3, length), bool)
            self._base, self._replay_limit = start, end+1
            self.poll()
            self._replay = True
            return
        from model_train.robust.predict import load_raw_amplitude
        amp, quality, present = load_raw_amplitude(self.dir, include_presence=True, min_common_length=3000)
        self._amp = amp.copy()
        self._present = present.copy()
        self.capacity = self._end = len(amp)
        self._base = quality["common_start_tx_seq"]
        self._first = {rx: self._base for rx in self.rx_order}
        self._last = {rx: self._base + len(amp) - 1 for rx in self.rx_order}
        self._replay = True

    # ── 읽기 ────────────────────────────────────────────────────────────────
    def poll(self) -> int:
        """새로 붙은 프레임을 읽어 넣고 개수를 돌려준다."""
        added = 0
        if self._replay:
            return added
        for ri, rx in enumerate(self.rx_order):
            path = self.dir / f"device_{rx}.csi"
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size < self._offset[rx]:
                raise ValueError(f"RX {rx}: 입력 파일이 축소됨 — 새 세션으로 시작하세요")
            whole = (size - self._offset[rx]) // cs.CSI_FRAME_SIZE
            if whole <= 0:
                continue
            with path.open("rb") as fp:
                fp.seek(self._offset[rx])
                blob = fp.read(whole * cs.CSI_FRAME_SIZE)
            complete = len(blob) // cs.CSI_FRAME_SIZE * cs.CSI_FRAME_SIZE
            blob = blob[:complete]
            if not blob:
                continue
            for pos in range(0, len(blob), cs.CSI_FRAME_SIZE):
                why = cs.validate_frame(blob[pos:pos + cs.CSI_FRAME_SIZE])
                if why:
                    raise ValueError(f"RX {rx}: 잘못된 CSI 프레임 ({why})")
            frames = np.frombuffer(blob, dtype=cs.CSI_FRAME_DTYPE)
            h = frames["hdr"]
            if not ((h["frame_type"] == cs.FRAME_TYPE_CSI) & (h["channel"] == 11)
                    & (h["rate"] == 11) & (h["sig_len"] == 47)).all():
                raise ValueError(f"RX {rx}: 학습과 다른 RF/프레임 규격")
            wire = int(h["rx_id"][0])
            expected = self._expected_ids.get(rx, self._wire_ids.get(rx, wire))
            if not (h["rx_id"] == expected).all():
                raise ValueError(f"RX {rx}: 수신기 식별 불일치")
            if wire and any(other != rx and value == wire for other, value in self._wire_ids.items()):
                raise ValueError("서로 다른 RX 파일의 rx_id가 같습니다")
            self._wire_ids[rx] = wire
            boot = int(h["boot_id"][0])
            if not (h["boot_id"] == self._boot.get(rx, boot)).all():
                raise ValueError(f"RX {rx}: 재부팅 경계 — 새 세션으로 시작하세요")
            self._boot[rx] = boot
            tx = h["tx_seq"].astype(np.int64)
            if (np.diff(tx) < 0).any() or (rx in self._last and tx[0] < self._last[rx]):
                raise ValueError(f"RX {rx}: tx_seq 역행 — 정렬하지 않고 중지합니다")
            fresh = rx not in self._last or int(tx[-1]) > self._last[rx]
            self._ingest(ri, frames)
            self._offset[rx] += len(blob)
            if fresh:
                self._last_seen[rx] = time.monotonic()
            added += len(frames)
        return added

    def _ingest(self, ri: int, frames: np.ndarray) -> None:
        if self._replay_limit is not None:
            seq = frames['hdr']['tx_seq']
            frames = frames[(seq >= self._base) & (seq < self._replay_limit)]
            if not len(frames):
                return
        tx = frames["hdr"]["tx_seq"].astype(np.int64)
        rx = self.rx_order[ri]
        self._first.setdefault(rx, int(tx[0]))
        self._last[rx] = int(tx[-1])
        if self._base is None:
            self._base = int(tx.min())
        idx = tx - self._base
        if int(idx.max()) >= self.capacity:
            self._roll(int(idx.max()) - self.capacity + 1)
            idx = tx - self._base
        keep = (idx >= 0) & (idx < self.capacity)
        idx, frames = idx[keep], frames[keep]
        if len(idx) == 0:
            return
        idx, first = np.unique(idx, return_index=True)   # 중복 tx_seq 는 첫 프레임
        new = ~self._present[ri, idx]
        idx, first = idx[new], first[new]
        if not len(idx):
            return
        z = cs.complex_csi(frames[first], valid_only=False)[:, self.raw_indices]
        values = z.astype(np.complex64) if self.model_kind == "phase60" else np.abs(z).astype(np.float32)
        self._amp[idx, ri * self.tones:(ri + 1) * self.tones] = values
        self._present[ri, idx] = True
        self._end = max(self._end, int(idx.max()) + 1)

    def _roll(self, shift: int) -> None:
        if shift >= self.capacity:
            self._amp.fill(np.nan)
            self._present.fill(False)
        else:
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
        end = self.common_end if self._cursor is None else self._cursor
        if self._base is None or end < width:
            return None
        s = end - width
        if s < max(0, max(self._first.values(), default=self._base) - self._base):
            return None
        # 학습은 전체 공통 구간을 보간한 뒤 창을 자른다. 창 바로 앞의 관측값도
        # 최대 5-frame 공백의 근거로 사용해야 창 시작이 인위적인 결측이 되지 않는다.
        common_start = max(0, max(self._first.values()) - self._base)
        lo = max(common_start, s - pre.DEFAULT_CONFIG.max_interp_gap - 1)
        hi = min(self.common_end, end + pre.DEFAULT_CONFIG.max_interp_gap + 1)
        amp = self._amp[lo:hi].copy()
        present = self._present[:, lo:hi].copy()
        # (3, T, 모델별 톤 수) 로 보간 — 공식 함수, 같은 최대 공백(5)
        view = amp.reshape(hi-lo, len(self.rx_order), self.tones).transpose(1, 0, 2).copy()
        if self.model_kind == "phase60":
            from model_train.phase60 import features as phase
            real, imag = view.real.copy(), view.imag.copy()
            pre.interpolate_short_gaps(real, present, pre.DEFAULT_CONFIG)
            pre.interpolate_short_gaps(imag, present, pre.DEFAULT_CONFIG)
            view = phase.phase_residual(real + 1j*imag).astype(np.float32)
        else:
            pre.interpolate_short_gaps(view, present, pre.DEFAULT_CONFIG)
        values = view.transpose(1, 0, 2).reshape(hi-lo, self.n_cols)[s-lo:end-lo]
        counts = present[:, s-lo:end-lo].sum(axis=1)
        # RX 가 창의 10% 미만만 채웠으면 끊긴 것으로 본다 (한두 프레임 잔재에 속지 않게)
        alive = tuple(rx for ri, rx in enumerate(self.rx_order) if counts[ri] > 0.1 * width)
        hz = float(counts[[self.rx_order.index(rx) for rx in alive]].mean() / (width / 100)) if alive else 0.0
        return Window(amp=values if self.model_kind == "amplitude" else None,
                      phase=values if self.model_kind == "phase60" else None,
                      tx_seq0=self._base + s, rx_alive=alive, hz=hz)

    @property
    def common_end(self) -> int:
        if self._base is None or len(self._last) < len(self.rx_order):
            return 0
        return max(0, min(self._last.values()) + 1 - self._base)

    @property
    def available_frames(self) -> int:
        if self._base is None or len(self._first) < len(self.rx_order):
            return 0
        end = self.common_end if self._cursor is None else self._cursor
        return max(0, end - max(0, max(self._first.values()) - self._base))

    def advance(self, stride: int) -> bool:
        """재생 커서를 stride 만큼 민다. 끝까지 갔으면 False."""
        if self._cursor is None:
            self._cursor = self._end
            return self._end > 0
        if self._cursor + stride > self.common_end:
            return False
        self._cursor += stride
        return True

    def rewind(self, width: int) -> None:
        """재생을 첫 창 끝에 맞춘다."""
        self._cursor = min(width, self.common_end)

    def rx_alive(self, within_s: float = 3.0) -> tuple:
        """최근에 프레임이 들어온 RX (실시간 모드용)."""
        now = time.monotonic()
        return tuple(rx for rx in self.rx_order
                     if self._last_seen[rx] and now - self._last_seen[rx] <= within_s)
