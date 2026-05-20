"""ESP-IDF export.sh — bash source 기반 실행 (env dict 캡처보다 안정적)."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

from idf_paths import REPO_ROOT, repo_tools_path, resolve_idf_path


def idf_shell_prefix(
    repo_root: Path = REPO_ROOT,
    *,
    idf_path: Optional[Path] = None,
    tools_path: Optional[Path] = None,
) -> str:
    """IDF_PATH=repo/esp-idf. 툴체인은 기본 ~/.espressif (tools_path로만 재정의)."""
    idf = idf_path or resolve_idf_path(repo_root)
    export_sh = idf / "export.sh"
    if not export_sh.is_file():
        raise FileNotFoundError(f"ESP-IDF export.sh not found: {export_sh}")
    prefix = f'export IDF_PATH="{idf}"; '
    if tools_path is not None:
        tools_path.mkdir(parents=True, exist_ok=True)
        prefix += f'export IDF_TOOLS_PATH="{tools_path}"; '
    return prefix + f'source "{export_sh}" >/dev/null 2>&1; '


def run_in_idf_shell(
    command: List[str],
    *,
    cwd: Path,
    repo_root: Path = REPO_ROOT,
    idf_path: Optional[Path] = None,
    tools_path: Optional[Path] = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """export.sh 활성화 후 command 실행."""
    prefix = idf_shell_prefix(repo_root, idf_path=idf_path, tools_path=tools_path)
    cmd_line = " ".join(shlex.quote(part) for part in command)
    return subprocess.run(
        ["bash", "-lc", prefix + cmd_line],
        cwd=str(cwd),
        text=True,
        check=False,
    )


def idf_py_works(repo_root: Path = REPO_ROOT) -> bool:
    proc = run_in_idf_shell(
        ["idf.py", "--version"],
        cwd=repo_root,
        repo_root=repo_root,
    )
    return proc.returncode == 0
