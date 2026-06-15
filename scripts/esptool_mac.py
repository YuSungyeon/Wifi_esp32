"""esptool로 연결된 ESP의 MAC 주소 읽기."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from registry import normalize_mac

MAC_LINE_RE = re.compile(
    r"(?:MAC|Wi-?Fi\s+STA\s+MAC)\s*[:=]\s*"
    r"((?:[0-9A-Fa-f]{2}[:\-]){5}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{12})",
    re.IGNORECASE,
)
USB_DEVICE_RE = re.compile(r"USB JTAG[/_]serial debug unit@([0-9A-Fa-f]{8})")
USB_SERIAL_RE = re.compile(r'"kUSBSerialNumberString"\s*=\s*"([^"]+)"')


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


def _mac_from_macos_usb_registry(port: str) -> Optional[str]:
    """ESP32-S3 USB Serial/JTAG descriptor의 serial number(MAC)를 포트별로 조회."""
    if sys.platform != "darwin":
        return None

    try:
        proc = subprocess.run(
            ["ioreg", "-p", "IOUSB", "-l", "-w", "0"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    target = Path(port).name.removeprefix("cu.").removeprefix("tty.")
    current_port: Optional[str] = None
    for line in proc.stdout.splitlines():
        device_match = USB_DEVICE_RE.search(line)
        if device_match:
            location = device_match.group(1)
            location_prefix = location[:-5].lstrip("0") or "0"
            current_port = f"usbmodem{location_prefix}01"
            continue
        serial_match = USB_SERIAL_RE.search(line)
        if current_port == target and serial_match:
            try:
                return normalize_mac(serial_match.group(1))
            except ValueError:
                return None
    return None


def read_mac(
    port: str,
    cwd: Optional[str] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    usb_mac = _mac_from_macos_usb_registry(port)
    if usb_mac is not None:
        return usb_mac

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
