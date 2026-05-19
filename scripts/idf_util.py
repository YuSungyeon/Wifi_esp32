"""ESP-IDF idf.py subprocess helpers."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from idf_env import run_in_idf_shell
from idf_paths import REPO_ROOT


def run_idf(
    args: List[str],
    *,
    cwd: Path,
    dry_run: bool,
    repo_root: Path = REPO_ROOT,
) -> None:
    cmd_display = ["idf.py", *args]
    print("[cmd]", " ".join(cmd_display))
    if dry_run:
        return
    proc = run_in_idf_shell(cmd_display, cwd=cwd, repo_root=repo_root)
    if proc.returncode != 0:
        raise RuntimeError(f"idf.py failed (exit {proc.returncode})")
