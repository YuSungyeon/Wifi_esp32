"""ESP-IDF idf.py subprocess helpers."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import List


def run_idf(args: List[str], *, cwd: Path, dry_run: bool) -> None:
    cmd = ["idf.py", *args]
    print("[cmd]", " ".join(cmd))
    if dry_run:
        return
    if not os.environ.get("IDF_PATH"):
        raise RuntimeError("IDF_PATH is not set. Run ESP-IDF export.sh first.")
    proc = subprocess.run(cmd, cwd=str(cwd), check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"idf.py failed (exit {proc.returncode})")
