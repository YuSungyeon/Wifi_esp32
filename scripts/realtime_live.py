#!/usr/bin/env python3
"""현재 SINK 수집과 모델 판단을 함께 관리한다. 정답 라벨 없는 별도 실행만 생성한다."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from csi_session import finalize_session, repo_provenance, write_manifest
from csi_store import FRAME_VERSION
from realtime_launch import MODEL, ROOT, build_command, check_runtime, new_output_dir, runtime_python, model_spec, resolve_model


def ensure_port_free(port: str) -> None:
    """다른 reader가 소유한 포트를 열지 않는다. DTR/RTS나 보드는 조작하지 않는다."""
    if not Path(port).exists():
        raise ValueError(f"SINK 포트가 없습니다: {port} — 보드 다시 검색을 해주세요")
    if shutil.which("lsof"):
        result = subprocess.run(["lsof", "-t", port], capture_output=True, text=True, timeout=5)
        if result.stdout.strip():
            raise ValueError(f"포트를 다른 프로그램이 사용 중입니다: {port}")
        if result.returncode not in (0, 1):
            raise ValueError(f"포트 사용 여부 확인 실패: {port}")


def create_capture(run_dir: Path, port: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=False)
    capture = run_dir / "capture"
    capture.mkdir()
    write_manifest(capture, {
        "schema": 1, "pipeline": "usb", "frame_version": FRAME_VERSION,
        "session_id": None, "label": None, "purpose": "realtime_monitor",
        "started_at_unix_us": int(time.time() * 1_000_000),
        "ended_at_unix_us": None, "provenance": repo_provenance(),
        "source_port": port, "devices": [],
    })
    registry = ROOT / "mac_collector" / "device_registry.csv"
    if registry.is_file():
        shutil.copyfile(registry, capture / "device_registry_snapshot.csv")
    return capture


def stop_child(child: subprocess.Popen, *, reader: bool = False) -> None:
    if child.poll() is not None:
        return
    # 현행 reader는 KeyboardInterrupt에서 finally로 flush·품질 통계를 남긴다.
    child.send_signal(signal.SIGINT if reader else signal.SIGTERM)
    try:
        child.wait(timeout=2)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=2)


def finish_status(path: Path, error: str | None, *, model_kind: str = "amplitude", model=None,
                  stride: int = 30) -> None:
    spec = model_spec(model_kind)
    view = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {
        "state": "deciding", "events": [], "t": 0, "mode": "live", "n_rx": 0,
        "probs": {"empty": 0, "static": 0, "motion": 0}, "confidence": 0,
        "held_s": 0, "candidate": None, "remaining_s": 0, "threshold": 0,
        "decision_mode": "instant", "display_interval_s": stride/100, "stride_s": stride/100,
        "warmup_remaining_s": spec['window_s'], "window_s": spec['window_s'], "hz": 0, "predictions": 0,
        "model_kind": model_kind, "model_label": spec['label'], "model": str(resolve_model(model_kind, model)),
        "session": "capture", "session_path": str(path.parent.parent / "capture"),
        "output_dir": str(path.parent),
    }
    view["running"] = False
    if "model_sha256" not in view:
        checkpoint = resolve_model(model_kind, model)
        view["model_sha256"] = hashlib.sha256(checkpoint.read_bytes()).hexdigest() if checkpoint.is_file() else None
    if error:
        view.update(state="suspended", confidence=0, n_rx=0,
                    note=f"실시간 수집/판단 오류: {error}",
                    probs={"empty": 0, "static": 0, "motion": 0})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)
    # The reader may fail before the engine can write a status or stop reason.
    row = {"t": view.get("t", 0), "wall": time.time(), "monotonic": time.monotonic(),
           "reason": "supervisor_error" if error else "supervisor_stopped", "error": error,
           "inferred": False, "state": view["state"], "note": view.get("note", ""),
           "probs": [view["probs"].get(c, 0) for c in ("empty", "static", "motion")],
           "n_rx": view.get("n_rx", 0), "model_kind": model_kind,
           "model_sha256": view.get("model_sha256"), "mode": "live",
           "input_start_tx_seq": None, "input_end_tx_seq_exclusive": None,
           "window_frames": spec["window_s"]*100, "stride_frames": stride}
    with (path.parent / "realtime_predictions.jsonl").open("a", encoding="utf-8") as fp:
        fp.write(json.dumps(row, ensure_ascii=False, allow_nan=False)+"\n")


def run_live(port: str, run_dir: Path, *, model: Path | None = None, model_kind: str = "amplitude", stride: int = 30,
             plain: bool = True, duration: float = 0, stop: threading.Event | None = None) -> int:
    if stride <= 0 or duration < 0:
        raise ValueError("stride > 0, duration ≥ 0이 필요합니다")
    stop = stop if stop is not None else threading.Event()
    spec = model_spec(model_kind)
    model = resolve_model(model_kind, model)
    ensure_port_free(port)
    check_runtime(model, model_kind=model_kind)
    if stop.is_set():
        return 0
    capture = create_capture(run_dir, port)
    prediction = run_dir / "prediction"
    children = []
    error = None
    rc = 0
    print(f"[실시간] {spec['label']} · 현재 보드 · {port} · {spec['window_s']}초 준비 후 {stride/100:g}초마다 판단", flush=True)
    print(f"[입력] {capture} · 정답 라벨 없음 · 학습 데이터와 분리", flush=True)
    try:
        reader_cmd = [str(runtime_python()), "-u", str(ROOT / "scripts" / "csi_serial_reader.py"),
                      "--port", port, "--session-dir", str(capture),
                      "--flush-interval", "0.1",
                      "--registry", str(capture / "device_registry_snapshot.csv")]
        children.append((subprocess.Popen(reader_cmd, cwd=ROOT, stdin=subprocess.DEVNULL), True))
        cmd = build_command(capture, follow=True, model=model, stride=stride,
                            output_dir=prediction, plain=plain, model_kind=model_kind)
        children.append((subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.DEVNULL), False))
        started = time.monotonic()
        while not stop.wait(0.1):
            for child, is_reader in children:
                code = child.poll()
                if code is not None:
                    raise RuntimeError(f"{'수집' if is_reader else '판단'} 프로세스 종료 (코드 {code})")
            if duration and time.monotonic() - started >= duration:
                break
    except (OSError, ValueError, RuntimeError) as exc:
        error, rc = str(exc), 1
        print(f"[실시간 오류] {error}", flush=True)
    finally:
        for child, is_reader in reversed(children):
            stop_child(child, reader=is_reader)
        finalize_session(capture)
        finish_status(prediction / "realtime_status.json", error, model_kind=model_kind, model=model, stride=stride)
        print("[실시간] 수집·판단 종료 (다른 수집 작업은 변경하지 않음)", flush=True)
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", required=True, help="SINK USB 포트")
    ap.add_argument("--output-dir", type=Path)
    ap.add_argument("--model", type=Path)
    ap.add_argument("--model-kind", choices=("amplitude", "phase60", "phase60_room"), default="amplitude")
    ap.add_argument("--stride", type=int, default=30)
    ap.add_argument("--plain", action="store_true")
    ap.add_argument("--duration", type=float, default=0, help="0=정지할 때까지, 양수=검증용 제한 초")
    args = ap.parse_args()
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        return run_live(args.port, args.output_dir or new_output_dir(), model=args.model,
                        stride=args.stride, plain=args.plain, duration=args.duration, stop=stop, model_kind=args.model_kind)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"[실시간 시작 실패] {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
