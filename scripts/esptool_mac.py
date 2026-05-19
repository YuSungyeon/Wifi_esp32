"""esptool로 연결된 ESP의 MAC 주소 읽기."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import List, Optional

from registry import normalize_mac

MAC_LINE_RE = re.compile(
    r"(?:MAC|Wi-?Fi\s+STA\s+MAC)\s*[:=]\s*"
    r"((?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{12})",
    re.IGNORECASE,
)


def _esptool_argv(port: str) -> List[str]:
    if os.environ.get("IDF_PATH"):
        idf_py = shutil.which("idf.py")
        if idf_py:
            return ["idf.py", "-p", port, "esptool", "read_mac"]
    esptool = shutil.which("esptool.py") or shutil.which("esptool")
    if esptool:
        return [esptool, "--port", port, "read_mac"]
    return [sys.executable, "-m", "esptool", "--port", port, "read_mac"]


def parse_mac_from_esptool_output(text: str) -> str:
    for line in text.splitlines():
        match = MAC_LINE_RE.search(line)
        if match:
            return normalize_mac(match.group(1))
    raise RuntimeError(
        "could not parse MAC from esptool output:\n" + text.strip()
    )


def read_mac(port: str, cwd: Optional[str] = None) -> str:
    cmd = _esptool_argv(port)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        raise RuntimeError(
            f"esptool failed (exit {proc.returncode}): {' '.join(cmd)}\n{combined.strip()}"
        )
    return parse_mac_from_esptool_output(combined)
