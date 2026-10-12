#!/usr/bin/env python3
"""실시간 판단 엔진 — 녹화 파일 재생과 실시간 tail 이 같은 코드를 쓴다.

    python realtime/engine.py --session <세션 디렉터리>            # 재생 (오프라인 확인)
    python realtime/engine.py --session <세션 디렉터리> --follow   # 실시간

진폭 30초(기본) 또는 위상 60초(시험 후보)를 선택한다. 3-class 를 바로 내며
seed 3개 softmax 평균이다. 엔진은 창을 만들고 상태 기계에 넘기는 일만 한다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import hashlib
from collections import deque
from pathlib import Path
from typing import Iterator

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime.models import load_model_bundle, predict_model_window  # noqa: E402
from scripts.realtime_launch import new_output_dir, resolve_model, model_spec  # noqa: E402
from realtime.state import Event, Reading, StateMachine  # noqa: E402
from realtime.stream import CsiStream  # noqa: E402

MODEL = ROOT / "model_train" / "robust" / "model.pt"
STATUS_NAME = "realtime_status.json"
EVENTS_NAME = "realtime_events.jsonl"
MARKS_NAME = "realtime_marks.jsonl"
PREDICTIONS_NAME = "realtime_predictions.jsonl"


def write_status(path: Path, view: dict) -> None:
    """GUI 가 반쯤 쓰인 파일을 읽지 않도록 임시 파일에 쓰고 rename 한다."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Engine:
    def __init__(self, session_dir: Path, model_path: Path | None = None, stride: int = 30,
                 conf: float = 0.7, device: str = "cpu", output_dir: Path | None = None,
                 model_kind: str = "amplitude") -> None:
        if stride <= 0 or not 0 <= conf <= 1 or device != "cpu":
            raise ValueError("stride > 0, 0 ≤ conf ≤ 1, device=cpu가 필요합니다")
        torch.set_num_threads(2)
        self.dir = Path(session_dir).resolve()
        self.out = Path(output_dir or new_output_dir()).resolve()
        if self.out == self.dir or self.out in self.dir.parents or self.dir in self.out.parents:
            raise ValueError("판단 출력은 원본 세션 밖의 별도 폴더를 사용하세요")
        spec = model_spec(model_kind)
        self.model_kind = model_kind
        self.model_label = spec['label']
        self.stride = stride
        self.device = device
        self.events = deque(maxlen=1000)
        self.model_path = resolve_model(model_kind, model_path)
        self.bundle, self.models = load_model_bundle(self.model_path, model_kind)
        self.model_sha256 = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        self.window = self.bundle["window_frames"]
        self.stream = CsiStream(self.dir, model_kind=spec.get('input_kind', model_kind))
        self.sm = StateMachine(conf=conf, immediate=True, window_s=self.window/100)
        self.follow = True
        self.running = True
        self.out.mkdir(parents=True, exist_ok=True)
        self._t0 = time.monotonic()
        self._frames = 0
        self._hz, self._rx = 0.0, ()
        self._window_probs = deque(maxlen=12000)  # 장시간 실행 시 메모리 무한 증가 방지
        self.n_predictions = 0
        self._last_window_end = None
        self._last_reason = "warmup"

    def prepare_replay(self) -> None:
        self.follow = False
        self.stream.load_replay()
        if self.stream.available_frames < self.window:
            raise ValueError(f"재생할 공통 구간이 {self.window/100:g}초 미만입니다")
        self.stream.rewind(self.window)

    def record_failure(self, message: str) -> None:
        self.running = False
        self._rx, self._hz = (), 0.0
        self.sm._to(self.sm._t, "suspended", 0.0)
        self.sm.probs = (0.0, 0.0, 0.0)
        self.sm.note = f"판단 오류로 중지: {message}"
        self._log_decision("error")
        write_status(self.out / STATUS_NAME, self.view())

    def close(self) -> None:
        self.running = False
        self._log_decision("stopped")
        write_status(self.out / STATUS_NAME, self.view())

    def _log_decision(self, reason: str, *, inferred: bool = False) -> None:
        end = ((self.stream._base or 0) +
               (self.stream.common_end if self.follow else (self.stream._cursor or 0)))
        row = {"t": self.sm._t, "wall": time.time(), "monotonic": time.monotonic(),
               "reason": reason, "inferred": inferred, "state": self.sm.state,
               "note": self.sm.note, "probs": list(self.sm.probs), "n_rx": len(self._rx),
               "model_kind": self.model_kind, "model_sha256": self.model_sha256,
               "mode": self.view()["mode"], "input_start_tx_seq": max(self.stream._base or 0, end-self.window),
               "input_end_tx_seq_exclusive": end, "window_frames": self.window,
               "stride_frames": self.stride}
        with (self.out / PREDICTIONS_NAME).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n")

    # ── 판단 한 번 ──────────────────────────────────────────────────────────
    def predict(self) -> tuple | None:
        """최근 창의 3-class 확률. 창이 덜 찼거나 누락이 있으면 None."""
        win = self.stream.latest(self.window)
        if win is None:
            self._last_reason = "warmup" if self.stream.available_frames < self.window else "common_window_missing"
            return None
        values = win.amp if self.model_kind == "amplitude" else win.phase
        if not np.isfinite(values).all():
            self._last_reason = "unfilled_gap"
            return None
        self._hz, self._rx = win.hz, win.rx_alive
        self._last_reason = "inference"
        return predict_model_window(win, self.bundle, self.models, self.model_kind), win.rx_alive

    def step(self, now: float | None = None) -> Event | None:
        self._frames += self.stream.poll()
        t = now if now is not None else time.monotonic() - self._t0
        alive = self.stream.rx_alive() if self.follow else tuple(self.stream.rx_order)
        self._rx = alive
        end = (self.stream._base or 0) + (self.stream.common_end if self.follow else self.stream._cursor)
        if (self.follow and len(alive) == 3 and self._last_window_end is not None
                and 0 <= end - self._last_window_end < self.stride):
            self.sm._t = t
            self._log_decision("waiting_for_new_common_frames")
            write_status(self.out / STATUS_NAME, self.view())
            return None
        got = self.predict() if len(alive) == 3 else None
        if len(alive) != 3:
            self._last_reason = "rx_missing"
        if got is None:
            self._hz = 0.0
            reading = Reading(t=t, probs=(0.0, 0.0, 0.0), n_rx=len(alive), valid=False)
            if self.sm.state == "deciding" and self.stream.available_frames < self.window:
                self.sm._t = t
                self.sm._cand = None
                self.sm.note = f"{self.window/100:g}초 입력 준비 중 · 수신 RX {len(alive)}/3"
                self._log_decision(self._last_reason)
                write_status(self.out / STATUS_NAME, self.view())
                return None
        else:
            probs, alive = got
            reading = Reading(t=t, probs=probs, n_rx=len(alive))
            self._window_probs.append((t, probs))
            self.n_predictions += 1
            self._last_window_end = end
        ev = self.sm.update(reading)
        if ev:
            self.events.append(ev)
            with (self.out / EVENTS_NAME).open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({"t": round(ev.t, 2), "from": ev.frm, "to": ev.to,
                                     "confidence": round(ev.confidence, 4),
                                     "wall": time.time(), "monotonic": time.monotonic(),
                                     "model_kind": self.model_kind}, ensure_ascii=False) + "\n")
        self._log_decision(self._last_reason, inferred=got is not None)
        write_status(self.out / STATUS_NAME, self.view())
        return ev

    def mark(self, note: str = "") -> dict:
        """운영자가 실제로 상태를 바꾼 시각을 정답으로 남긴다 (반응 지연 측정용).

        tx_seq 로 남겨야 나중에 프레임 위치로 환산된다 — 호스트 시각만으로는 맞출 수 없다.
        """
        win = self.stream.latest(1)
        row = {"t": round(self.sm._t, 2), "wall": time.time(), "monotonic": time.monotonic(), "note": note,
               "tx_seq": int(win.tx_seq0) if win else None, "state": self.sm.state}
        with (self.out / MARKS_NAME).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    # ── 화면용 ──────────────────────────────────────────────────────────────
    def view(self) -> dict:
        cand, remaining = self.sm.pending
        return {"state": self.sm.state, "held_s": round(self.sm.held_s, 1),
                "confidence": round(self.sm.confidence, 4), "note": self.sm.note,
                "candidate": cand, "remaining_s": round(remaining, 1),
                "threshold": self.sm.threshold, "t": round(self.sm._t, 2),
                "decision_mode": "instant", "display_interval_s": self.stride / 100,
                "probs": {c: round(p, 4) for c, p in
                          zip(("empty", "static", "motion"), self.sm.probs)},
                "n_rx": len(self._rx),
                "hz": round(self._hz, 1),
                "session": self.dir.name, "window_s": self.window / 100,
                "session_path": str(self.dir), "output_dir": str(self.out),
                "mode": ("live" if self.stream.input_kind == "realtime_monitor" else "follow")
                        if self.follow else "replay", "running": self.running,
                "model": str(self.model_path),
                "model_kind": self.model_kind, "model_label": self.model_label,
                "model_sha256": self.model_sha256, "model_count": len(self.models),
                "stride_s": self.stride / 100, "predictions": self.n_predictions,
                "tx_seq": self._last_window_end - 1 if self._last_window_end is not None else None,
                "warmup_remaining_s": max(0, self.window - self.stream.available_frames) / 100,
                "events": [{"t": round(e.t, 2), "from": e.frm, "to": e.to,
                            "confidence": round(e.confidence, 4)} for e in list(self.events)[-30:]]}

    # ── 루프 ────────────────────────────────────────────────────────────────
    def run(self, follow: bool) -> Iterator[Event]:
        if follow:
            while True:
                ev = self.step()
                if ev:
                    yield ev
                time.sleep(self.stride / 100.0)
        self.prepare_replay()
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
    ap.add_argument("--model", type=Path)
    ap.add_argument("--model-kind", choices=("amplitude", "phase60", "phase60_room"), default="amplitude")
    ap.add_argument("--output-dir", type=Path)
    args = ap.parse_args()
    torch.set_num_threads(2)
    eng = None
    try:
        eng = Engine(args.session, model_path=args.model, stride=args.stride, conf=args.conf,
                     output_dir=args.output_dir, model_kind=args.model_kind)
        print(f"모델: {eng.model_path} · 기록: {eng.out}", flush=True)
        for ev in eng.run(args.follow):
            print(f"{ev.t:8.1f}s  {ev.frm} → {ev.to}  ({ev.confidence:.0%})", flush=True)
    except (OSError, ValueError, KeyError) as exc:
        if eng is not None:
            eng.record_failure(str(exc))
        print(f"[판단 오류] {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        if eng is not None:
            eng.close()
    v = eng.view()
    print(f"\n마지막 상태: {v['state']} ({v['confidence']:.0%}) · 판단 {eng.n_predictions}회")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
