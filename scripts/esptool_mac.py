"""esptool로 연결된 ESP의 MAC 주소 읽기."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

from registry import normalize_mac

MAC_LINE_RE = re.compile(
    r"(?:MAC|Wi-?Fi\s+STA\s+MAC)\s*[:=]\s*"
    r"((?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{12})",
    re.IGNORECASE,
)


def _esptool_argv_candidates(port: str) -> List[List[str]]:
    """read_mac 시도 순서. idf.py esptool 서브커맨드는 프로젝트에 ninja target이 없어 실패할 수 있음."""
    candidates: List[List[str]] = []
    esptool = shutil.which("esptool.py") or shutil.which("esptool")
    if esptool:
        candidates.append([esptool, "--port", port, "read_mac"])
    candidates.append([sys.executable, "-m", "esptool", "--port", port, "read_mac"])
    if os.environ.get("IDF_PATH") and shutil.which("idf.py"):
        candidates.append(["idf.py", "-p", port, "esptool", "read_mac"])
    return candidates


def parse_mac_from_esptool_output(text: str) -> str:
    for line in text.splitlines():
        match = MAC_LINE_RE.search(line)
        if match:
            return normalize_mac(match.group(1))
    raise RuntimeError(
        "could not parse MAC from esptool output:\n" + text.strip()
    )


def read_mac(
    port: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    errors: List[str] = []
    run_env = env if env is not None else None
    for cmd in _esptool_argv_candidates(port):
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            env=run_env,
            capture_output=True,
            text=True,
            check=False,
        )
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode == 0:
            return parse_mac_from_esptool_output(combined)
        errors.append(f"{' '.join(cmd)} (exit {proc.returncode}):\n{combined.strip()}")
    raise RuntimeError("esptool read_mac failed:\n" + "\n---\n".join(errors))
