"""GUI·수집 CLI의 공통 판단 실행 계약. 이 모듈은 모델 패키지를 import하지 않는다."""
from __future__ import annotations

import datetime
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "model_train" / "robust" / "model.pt"
UI = ROOT / "realtime" / "ui.py"
LIVE = ROOT / "scripts" / "realtime_live.py"


def runtime_python() -> Path:
    candidate = ROOT / ".venv" / "bin" / "python"
    return candidate if candidate.is_file() else Path(sys.executable)


def setup_hint() -> str:
    return ("python3 -m venv .venv\n"
            ".venv/bin/python -m pip install -r requirements-robust.txt")


def runtime_info() -> dict:
    return {"python": str(runtime_python()), "model": str(MODEL.relative_to(ROOT)),
            "model_exists": MODEL.is_file(), "setup": setup_hint(),
            "window_s": 30, "stride_s": 0.3}


def check_runtime(model: Path = MODEL) -> None:
    if not Path(model).is_file():
        raise ValueError(f"모델 파일 없음: {model}")
    probe = subprocess.run(
        [str(runtime_python()), "-c",
         "from model_train.robust.predict import load_bundle; "
         "import sys; load_bundle(sys.argv[1])", str(Path(model).resolve())],
        cwd=ROOT, capture_output=True, text=True, timeout=30,
    )
    if probe.returncode:
        detail = (probe.stderr or probe.stdout).strip().splitlines()
        raise ValueError("판단 환경/모델을 확인할 수 없습니다: "
                         + (detail[-1] if detail else "알 수 없는 오류")
                         + "\n" + setup_hint())


def new_output_dir() -> Path:
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return ROOT / "mac_collector_output" / "realtime" / f"{stamp}-{uuid.uuid4().hex[:8]}"


def build_command(session: Path, *, follow: bool = False, plain: bool = True,
                  model: Path = MODEL, stride: int = 30,
                  output_dir: Path | None = None, speed: float = 0.0) -> list[str]:
    session = Path(session).resolve()
    if not (session / "session.json").is_file():
        raise ValueError(f"session.json이 없는 세션입니다: {session}")
    if stride <= 0 or speed < 0:
        raise ValueError("stride는 양수, speed는 0 이상이어야 합니다")
    cmd = [str(runtime_python()), "-u", str(UI), "--session", str(session),
           "--model", str(Path(model).resolve()), "--stride", str(stride),
           "--output-dir", str(output_dir or new_output_dir()), "--speed", str(speed)]
    if follow:
        cmd.append("--follow")
    if plain:
        cmd.append("--plain")
    return cmd


def build_live_command(port: str, *, output_dir: Path, model: Path = MODEL,
                       stride: int = 30, plain: bool = True) -> list[str]:
    if not port or stride <= 0:
        raise ValueError("SINK 포트와 양수 stride가 필요합니다")
    cmd = [str(runtime_python()), "-u", str(LIVE), "--port", port,
           "--output-dir", str(output_dir), "--model", str(model), "--stride", str(stride)]
    if plain:
        cmd.append("--plain")
    return cmd
