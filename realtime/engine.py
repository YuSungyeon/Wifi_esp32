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
from realtime import home  # noqa: E402
from realtime.state import Event, Reading, StateMachine  # noqa: E402
from realtime.stream import CsiStream  # noqa: E402

MODEL = ROOT / "model_train" / "amp60" / "model_amp1500_all74.pt"   # 실시간 기본: 진폭 15초, 74세션
ROBUST = ROOT / "model_train" / "robust" / "model.pt"               # 담당자 배포 체크포인트 (30초)
#: 1단계 — 움직임 여부만 내는 짧은 창 모델. 상태 판단에는 **쓰지 않는다**: 화면에 활동량을
#: 그리는 용도다. 기준값을 정하려면 전환 세션이 필요한데 아직 없다
#: (doc/realtime-inference.md §6-②). 없으면 활동량 줄만 빠지고 나머지는 그대로 돈다.
STAGE1 = ROOT / "model_train" / "amp60" / "model_motion3s.pt"
ACTIVITY_KEEP = 600                  # 0.3초 간격이면 3분치
MODEL_DIRS = (ROOT / "model_train" / "robust", ROOT / "model_train" / "phase60",
              ROOT / "model_train" / "amp60")


def available_models(store: dict | None = None, only_realtime: bool = True) -> list[dict]:
    """실시간에 쓸 수 있는 체크포인트. 화면이 고르게 하려고 요약만 읽는다.

    이름·설명·노출 여부는 체크포인트가 아니라 `model_train/models.json` 에 있다
    (`scripts/model_registry.py`). `only_realtime=False` 면 꺼 둔 것까지 돌려준다 —
    관리 화면은 목록 전체를 보여야 한다.
    """
    import model_registry

    store = model_registry.load() if store is None else store
    out = []
    for d in MODEL_DIRS:
        for p in sorted(d.glob("model*.pt")):
            try:
                b = torch.load(p, map_location="cpu", weights_only=False)
            except Exception:
                continue
            if len(b.get("class_names", [0, 0, 0])) != 3:
                continue        # 이진 1단계 모델은 상태 모델 자리에 올리지 않는다
            rel = str(p.relative_to(ROOT))
            e = model_registry.entry(rel, store)
            if only_realtime and not e["realtime"]:
                continue
            m = b.get("metrics", {})
            out.append({"path": rel, "name": e["name"], "note": e["note"],
                        "pinned": e["pinned"], "realtime": e["realtime"],
                        "file": p.stem.replace("model_", ""),
                        "mode": b.get("mode", "?"), "window_s": b.get("window_frames", 0) / 100,
                        "score": m.get("sessions"), "window_acc": m.get("window_accuracy"),
                        "trained_at": b.get("trained_at", ""),
                        "is_deployed": p.resolve() == MODEL.resolve()})
    return out


def load_stage1(path: Path = STAGE1, device: str = "cpu") -> dict | None:
    """1단계 체크포인트. 파일이 없으면 None — 1단계는 선택 사항이다."""
    if not Path(path).is_file():
        return None
    head = torch.load(path, map_location="cpu", weights_only=False)
    if head.get("mode") != "A-motion":
        raise ValueError(f"1단계 체크포인트가 아니다: {path} (mode={head.get('mode')})")
    from model_train.amp60.train import build

    models = []
    for state in head["state_dicts"]:
        m = build(len(head["class_names"]))
        m.load_state_dict(state)
        models.append(m.to(device).eval())
    return {"window": head["window_frames"], "models": models,
            "i_motion": head["class_names"].index("motion"),
            "name": Path(path).stem.replace("model_", "")}


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
                 conf: float = 0.6, device: str = "cpu", stage1: Path | None = STAGE1,
                 stage1_gate: tuple | None = (0.9, 3)) -> None:
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
        self.stage1 = load_stage1(stage1, device) if stage1 else None
        self._activity: list[float] = []          # 최근 p_motion — 화면 활동량 띠용
        # (기준, 연속 횟수) — 1단계가 이만큼 연속으로 움직임을 보면 상태 모델보다 먼저 motion 후보를 낸다.
        # None 이면 1단계는 화면 표시 전용.
        self.stage1_gate = stage1_gate
        self._cloud: home.Sender | None = None    # 실시간일 때만 생긴다 (_alert)
        self._led: bool | None = None             # 싱크 LED("집의 불") — None 이면 아직 모른다

    def _alert(self) -> None:
        """집 상태 서비스 — 조명을 판단하고 클라우드로 상태를 보낸다. **실시간에서만** 부른다.

        알림 판단은 클라우드가 한다(집 상태 서비스 계획 §3.3). 재생은 지난 상태를 다시 흘리는
        것이라 보내면 과거를 지금 일처럼 알린다. `step(now=...)` 가 주어졌으면 재생이므로
        호출부가 걸러 준다.
        """
        if self._cloud is None:
            self._cloud = home.Sender()
        # 조명 기준은 클라우드가 내려 준 설정(연동 전이면 기본값). reader 가 view()["led"] 를 싱크로 쓴다
        cfg = {**home.DEFAULT, **(home.load().get("config") or {})}
        self._led = home.led(self.sm.state, self.sm.held_s, time.time(), cfg, self._led)
        self._cloud.offer(self.sm.state, time.time() - self.sm.held_s, self._led)

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

    def _motion_now(self) -> bool:
        """1단계 게이트: 최근 n 번의 p_motion 이 전부 기준 이상인가."""
        if not self.stage1_gate or not self.stage1:
            return False
        thr, n = self.stage1_gate
        recent = self._activity[-n:]
        return len(recent) == n and all(a is not None and a >= thr for a in recent)

    def activity(self) -> float | None:
        """지금 이 순간의 움직임 세기 — 1단계의 p_motion. 창이 덜 찼으면 None.

        상태 기계에는 넣지 않는다. 창이 3초라 상태 모델(30~90초)과 시간 축이 달라, 둘을
        섞으려면 기준값과 유지 시간을 전환 세션으로 다시 잡아야 한다.
        """
        if not self.stage1:
            return None
        win = self.stream.latest(self.stage1["window"])
        if win is None or not np.isfinite(win.amp).all():
            return None
        x = torch.from_numpy(amplitude_input(win.amp)[None, ...]).to(self.device)
        with torch.no_grad():
            p = torch.stack([m(x).softmax(1) for m in self.stage1["models"]]).mean(0)
        return float(p[0, self.stage1["i_motion"]])

    def step(self, now: float | None = None) -> Event | None:
        self._frames += self.stream.poll()
        t = now if now is not None else time.monotonic() - self._t0
        self._activity.append(self.activity())
        del self._activity[:-ACTIVITY_KEEP]
        got = self.predict()
        # 실시간에선 "지금" 살아 있는 RX 를 **수신 시각**으로 본다 — 창으로 보면 안 된다. 창 끝은 모든 RX 가
        # 도달한 지점이라 한 대가 멈추면 창도 그 순간에 멈추고, 멈춘 창 안에선 세 대가 다 살아 있어 마지막
        # 상태를 굳힌 채 내보냈다(doc/realtime-inference.md §5 ⑤). 재생은 파일이 고정이라 창으로 본다.
        live = self.stream.rx_alive() if now is None else None
        # 켜자마자 아직 아무것도 안 들어왔으면 기다린다 — 들어오기 시작했는데 모자라면 그때 멈춘다
        lost = live is not None and len(live) < self.sm.required_rx and (live or self.sm.state != "deciding")
        if lost:
            reading = Reading(t=t, probs=(0.0, 0.0, 0.0), n_rx=len(live),
                              missing=tuple(rx for rx in self.stream.rx_order if rx not in live))
        elif got is None:
            # 창이 덜 찼거나 긴 공백으로 이번 판단을 건너뛴다. **후보 유지 시간은 그대로 둔다** —
            # 이것을 "확신 없음" 으로 넘기면 후보가 지워져 15초 유지가 공백마다 처음부터 다시 쌓인다.
            self.sm._t = t
            if now is None:
                self._alert()
            write_status(self.dir / STATUS_NAME, self.view())
            return None
        else:
            probs, alive = got
            if self._motion_now():
                probs = (0.0, 0.0, 1.0)      # 1단계가 본 움직임이 30초 창의 평균을 이긴다
            reading = Reading(t=t, probs=probs, n_rx=len(alive if live is None else live))
            self._window_probs.append((t, probs))
        ev = self.sm.update(reading)
        if ev:
            self.events.append(ev)
            with (self.dir / EVENTS_NAME).open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({"t": round(ev.t, 2), "from": ev.frm, "to": ev.to,
                                     "confidence": round(ev.confidence, 4),
                                     "wall": time.time()}, ensure_ascii=False) + "\n")
        if now is None:
            self._alert()
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
                "activity": [None if a is None else round(a, 3) for a in self._activity],
                "activity_step_s": round(self.stride / 100.0, 2),
                "activity_window_s": self.stage1["window"] / 100 if self.stage1 else None,
                "activity_model": self.stage1["name"] if self.stage1 else None,
                "n_rx": len(self._rx) if self._rx else len(self.stream.rx_alive()),
                "hz": round(self._hz, 1),
                "session": self.dir.name, "window_s": self.window / 100,
                "cloud": self._cloud.last if self._cloud else None, "led": self._led,
                "model": self.model_name, "model_kind": self.kind,
                "events": [{"t": round(e.t, 2), "from": e.frm, "to": e.to,
                            "confidence": round(e.confidence, 4)} for e in self.events[-30:]]}

    # ── 루프 ────────────────────────────────────────────────────────────────
    def replay_capacity(self) -> int:
        """재생용 버퍼 크기 — 파일 전체. 링버퍼(240초)보다 긴 세션은 앞부분이 밀려 나가 처음부터 못 돈다."""
        import csi_store as cs
        n = max((p.stat().st_size // cs.CSI_FRAME_SIZE for p in self.dir.glob("device_*.csi")), default=0)
        return max(self.stream.capacity, n + 1000)

    def start_replay(self) -> float:
        """파일 전체를 버퍼에 올리고 첫 창 끝에 맞춘다. 돌려주는 값은 첫 판단 시각(초)."""
        self.stream = CsiStream(self.dir, capacity=self.replay_capacity())
        self.stream.poll()
        self.stream.rewind(self.window)
        return self.window / 100.0

    def run(self, follow: bool) -> Iterator[Event]:
        if follow:
            while True:
                ev = self.step()
                if ev:
                    yield ev
                time.sleep(self.stride / 100.0)
        self.start_replay()
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
