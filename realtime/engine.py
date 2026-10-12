#!/usr/bin/env python3
"""실시간 판단 엔진 — 녹화 파일 재생과 실시간 tail 이 같은 코드를 쓴다.

    python realtime/engine.py --session <세션 디렉터리>            # 재생 (오프라인 확인)
    python realtime/engine.py --session <세션 디렉터리> --follow   # 실시간

모델은 배포 체크포인트(`model_train/robust/model.pt`) 하나다. 3-class 를 바로 내며 seed 3개
softmax 평균이다. 엔진은 창을 만들고 상태 기계에 넘기는 일만 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterator

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model_train.robust.shared_temporal import amplitude_input  # noqa: E402
from realtime.state import Event, Reading, StateMachine  # noqa: E402
from realtime.stream import CsiStream  # noqa: E402

MODEL = ROOT / "model_train" / "robust" / "model.pt"
MODEL_DIRS = (ROOT / "model_train" / "robust", ROOT / "model_train" / "phase60",
              ROOT / "model_train" / "amp60")


def available_models() -> list[dict]:
    """실시간에 쓸 수 있는 체크포인트. 화면이 고르게 하려고 요약만 읽는다."""
    out = []
    for d in MODEL_DIRS:
        for p in sorted(d.glob("model*.pt")):
            try:
                b = torch.load(p, map_location="cpu", weights_only=False)
            except Exception:
                continue
            m = b.get("metrics", {})
            out.append({"path": str(p.relative_to(ROOT)), "name": p.stem.replace("model_", ""),
                        "mode": b.get("mode", "?"), "window_s": b.get("window_frames", 0) / 100,
                        "score": m.get("sessions"), "window_acc": m.get("window_accuracy"),
                        "trained_at": b.get("trained_at", ""),
                        "is_deployed": p.parent.name == "robust"})
    return out


def load_any(path: Path, device: str = "cpu"):
    """진폭(robust)·위상(phase60)·우리 진폭(amp60) 체크포인트를 모두 연다.

    반환: (bundle, models, kind) — kind 는 창 입력을 어떻게 만들지 정한다.
    """
    head = torch.load(path, map_location="cpu", weights_only=False)
    mode = head.get("mode")
    if mode == "P":
        from model_train.phase60.predict import load_bundle as load_phase
        bundle, models = load_phase(path, device)
        return bundle, models, "phase"
    if mode in ("A", "A-ours"):
        from model_train.robust.shared_temporal import SharedEncoder
        if mode == "A":                      # 배포 체크포인트는 입력 계약 검사를 그대로 거친다
            from model_train.robust.predict import load_bundle as load_amp
            bundle, models = load_amp(path)  # 담당자 쪽은 device 인자를 받지 않는다
            return bundle, [m.to(device) for m in models], "amp"
        models = []
        for state in head["state_dicts"]:
            m = SharedEncoder(mode="A")
            m.load_state_dict(state)
            models.append(m.to(device).eval())
        return head, models, "amp"
    raise ValueError(f"알 수 없는 체크포인트 형식: {path} (mode={mode})")
STATUS_NAME = "realtime_status.json"
EVENTS_NAME = "realtime_events.jsonl"
MARKS_NAME = "realtime_marks.jsonl"


def write_status(path: Path, view: dict) -> None:
    """GUI 가 반쯤 쓰인 파일을 읽지 않도록 임시 파일에 쓰고 rename 한다."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Engine:
    def __init__(self, session_dir: Path, model_path: Path = MODEL, stride: int = 30,
                 conf: float = 0.7, device: str = "cpu") -> None:
        self.dir = Path(session_dir)
        self.stream = CsiStream(self.dir)
        self.sm = StateMachine(conf=conf)
        self.stride = stride
        self.device = device
        self.events: list[Event] = []
        self.bundle, self.models, self.kind = (None, [], "amp")
        if model_path is not None:
            self.bundle, self.models, self.kind = load_any(Path(model_path), device)
        self.window = self.bundle["window_frames"] if self.bundle else 3000
        self.model_name = Path(model_path).stem.replace("model_", "") if model_path else "없음"
        self._t0 = time.monotonic()
        self._frames = 0
        self._hz, self._rx = 0.0, ()
        self._window_probs: list[tuple] = []     # (t, probs) — 화면 타임라인용

    # ── 판단 한 번 ──────────────────────────────────────────────────────────
    def _window_input(self, win) -> np.ndarray:
        """체크포인트가 요구하는 입력으로 창을 바꾼다. 학습 때와 같은 변환이어야 한다."""
        if self.kind == "phase":
            from model_train.phase60 import features as F
            return F.windows(win.phase, np.array([0]), self.window,
                             self.bundle["downsample"], self.bundle.get("diff_channel", False),
                             self.bundle.get("pool", "mean"))
        return amplitude_input(win.amp)[None, ...]

    def predict(self) -> tuple | None:
        """최근 창의 3-class 확률. 창이 덜 찼거나 누락이 있으면 None."""
        win = self.stream.latest(self.window)
        series = win.phase if (win and self.kind == "phase") else (win.amp if win else None)
        if win is None or not np.isfinite(series).all():
            return None
        x = torch.from_numpy(self._window_input(win))
        with torch.no_grad():
            probs = torch.stack([m(x).softmax(1) for m in self.models]).mean(0)
        self._hz, self._rx = win.hz, win.rx_alive
        return tuple(probs[0].tolist()), win.rx_alive

    def step(self, now: float | None = None) -> Event | None:
        self._frames += self.stream.poll()
        t = now if now is not None else time.monotonic() - self._t0
        got = self.predict()
        if got is None:
            alive = self.stream.rx_alive() if now is None else ()
            reading = Reading(t=t, probs=(0.0, 0.0, 0.0), n_rx=len(alive) or 3)
            if self.sm.state == "deciding":       # 아직 창이 안 찼다 — 조용히 기다린다
                self.sm._t = t
                write_status(self.dir / STATUS_NAME, self.view())
                return None
        else:
            probs, alive = got
            reading = Reading(t=t, probs=probs, n_rx=len(alive))
            self._window_probs.append((t, probs))
        ev = self.sm.update(reading)
        if ev:
            self.events.append(ev)
            with (self.dir / EVENTS_NAME).open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({"t": round(ev.t, 2), "from": ev.frm, "to": ev.to,
                                     "confidence": round(ev.confidence, 4),
                                     "wall": time.time()}, ensure_ascii=False) + "\n")
        write_status(self.dir / STATUS_NAME, self.view())
        return ev

    def mark(self, note: str = "") -> dict:
        """운영자가 실제로 상태를 바꾼 시각을 정답으로 남긴다 (반응 지연 측정용).

        tx_seq 로 남겨야 나중에 프레임 위치로 환산된다 — 호스트 시각만으로는 맞출 수 없다.
        """
        win = self.stream.latest(1)
        row = {"t": round(self.sm._t, 2), "wall": time.time(), "note": note,
               "tx_seq": int(win.tx_seq0) if win else None, "state": self.sm.state}
        with (self.dir / MARKS_NAME).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    # ── 화면용 ──────────────────────────────────────────────────────────────
    def view(self) -> dict:
        cand, remaining = self.sm.pending
        return {"state": self.sm.state, "held_s": round(self.sm.held_s, 1),
                "confidence": round(self.sm.confidence, 4), "note": self.sm.note,
                "candidate": cand, "remaining_s": round(remaining, 1),
                "threshold": self.sm.threshold, "t": round(self.sm._t, 2),
                "probs": {c: round(p, 4) for c, p in
                          zip(("empty", "static", "motion"), self.sm.probs)},
                "n_rx": len(self._rx) if self._rx else len(self.stream.rx_alive()),
                "hz": round(self._hz, 1),
                "session": self.dir.name, "window_s": self.window / 100,
                "model": self.model_name, "model_kind": self.kind,
                "events": [{"t": round(e.t, 2), "from": e.frm, "to": e.to,
                            "confidence": round(e.confidence, 4)} for e in self.events[-30:]]}

    # ── 루프 ────────────────────────────────────────────────────────────────
    def run(self, follow: bool) -> Iterator[Event]:
        if follow:
            while True:
                ev = self.step()
                if ev:
                    yield ev
                time.sleep(self.stride / 100.0)
        self.stream.poll()
        self.stream.rewind(self.window)
        t = self.window / 100.0
        while True:
            ev = self.step(now=t)
            if ev:
                yield ev
            if not self.stream.advance(self.stride):
                break
            t += self.stride / 100.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--follow", action="store_true", help="실시간(파일 tail). 없으면 재생")
    ap.add_argument("--stride", type=int, default=30, help="판단 간격 (frame, 100Hz)")
    ap.add_argument("--conf", type=float, default=0.7)
    ap.add_argument("--model", type=Path, default=MODEL, help="쓸 체크포인트 (기본: 배포 진폭 모델)")
    args = ap.parse_args()
    torch.set_num_threads(2)
    eng = Engine(args.session, model_path=args.model, stride=args.stride, conf=args.conf)
    for ev in eng.run(args.follow):
        print(f"{ev.t:8.1f}s  {ev.frm} → {ev.to}  ({ev.confidence:.0%})", flush=True)
    v = eng.view()
    print(f"\n마지막 상태: {v['state']} ({v['confidence']:.0%}) · 판단 {len(eng._window_probs)}회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
