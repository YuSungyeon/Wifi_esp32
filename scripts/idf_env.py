"""ESP-IDF export.sh — bash source 기반 실행 (env dict 캡처보다 안정적)."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from idf_paths import (
    REPO_ROOT,
    build_idf_path_prefixes,
    find_idf_venv_python,
    idf_venv_root,
    resolve_idf_path,
)

TROUBLESHOOTING_DOC = "doc/overview/esp-idf-troubleshooting.md"


def _path_export_clause(prefixes: List[str]) -> str:
    if not prefixes:
        return ""
    joined = ":".join(prefixes)
    return f'export PATH="{joined}:$PATH"; '


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

    path_prefixes = build_idf_path_prefixes()
    prefix += _path_export_clause(path_prefixes)

    venv_root = idf_venv_root()
    if venv_root is not None:
        prefix += f'export IDF_PYTHON_ENV_PATH="{venv_root}"; '

    # stderr 유지: export 실패 시 bootstrap/플래시에서 원인 확인 가능
    prefix += f'source "{export_sh}" >/dev/null; '
    return prefix


def _resolve_idf_command(
    command: List[str],
    *,
    idf_path: Path,
) -> List[str]:
    """
    idf.py 는 #!/usr/bin/env python shebang — PATH 에 python 이 없는 Mac 에서 실패.
    항상 IDF venv (또는 meshsense python) 으로 tools/idf.py 를 직접 실행.
    """
    if not command or command[0] != "idf.py":
        return command

    idf_script = idf_path / "tools" / "idf.py"
    if not idf_script.is_file():
        return command

    for py_candidate in (find_idf_venv_python(), Path(sys.executable).resolve()):
        if py_candidate.is_file() and py_candidate.name.startswith("python"):
            return [str(py_candidate), str(idf_script), *command[1:]]
    return command


def run_in_idf_shell(
    command: List[str],
    *,
    cwd: Path,
    repo_root: Path = REPO_ROOT,
    idf_path: Optional[Path] = None,
    tools_path: Optional[Path] = None,
    check: bool = True,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    """export.sh 활성화 후 command 실행."""
    idf = idf_path or resolve_idf_path(repo_root)
    resolved = _resolve_idf_command(command, idf_path=idf)
    prefix = idf_shell_prefix(repo_root, idf_path=idf, tools_path=tools_path)
    cmd_line = " ".join(shlex.quote(part) for part in resolved)
    return subprocess.run(
        ["bash", "-lc", prefix + cmd_line],
        cwd=str(cwd),
        text=True,
        check=check,
        capture_output=capture_output,
    )


def idf_environment_hints() -> List[str]:
    """다른 Mac 에서 idf.py 실패 시 점검용 한 줄 힌트."""
    lines: List[str] = []
    venv_py = find_idf_venv_python()
    if venv_py is None:
        lines.append("  IDF Python venv 없음 → python scripts/idf_bootstrap.py -y")
    else:
        lines.append(f"  IDF venv: {venv_py}")
    prefixes = build_idf_path_prefixes()
    if prefixes:
        lines.append(f"  PATH 우선: {':'.join(prefixes[:4])}{'…' if len(prefixes) > 4 else ''}")
    if not Path("/opt/homebrew/bin/python3").is_file() and not Path("/usr/local/bin/python3").is_file():
        lines.append("  Homebrew python3 없음 — install.sh 가 시스템 python3 만 쓸 수 있음")
    return lines


def idf_diagnose(repo_root: Path = REPO_ROOT) -> str:
    """사전 점검·실패 메시지용 짧은 진단 텍스트."""
    lines = ["ESP-IDF 환경 진단:"]
    idf = resolve_idf_path(repo_root)
    lines.append(f"  IDF_PATH: {idf} ({'OK' if (idf / 'export.sh').is_file() else 'export.sh 없음'})")
    lines.extend(idf_environment_hints())
    lines.append(f"  문서: {TROUBLESHOOTING_DOC}")
    return "\n".join(lines)


def idf_py_works(repo_root: Path = REPO_ROOT) -> bool:
    if find_idf_venv_python() is None:
        print(
            "[idf] ~/.espressif/python_env/idf5.2_py*_env 가 없습니다.\n"
            "  python scripts/idf_bootstrap.py -y",
            file=sys.stderr,
        )
        return False

    proc = run_in_idf_shell(
        ["idf.py", "--version"],
        cwd=repo_root,
        repo_root=repo_root,
        check=False,
        capture_output=True,
    )
    if proc.returncode == 0:
        return True

    err = f"{proc.stderr or ''}\n{proc.stdout or ''}".strip()
    if err:
        print(err, file=sys.stderr)
    print(idf_diagnose(repo_root), file=sys.stderr)
    return False


def augmented_subprocess_env(base: Optional[dict[str, str]] = None) -> dict[str, str]:
    """install.sh 등 ESP-IDF 셸 스크립트용 PATH·IDF_PYTHON_ENV_PATH 보강."""
    env = dict(base or os.environ)
    prefixes = build_idf_path_prefixes()
    if prefixes:
        env["PATH"] = ":".join(prefixes) + (":" + env["PATH"] if env.get("PATH") else "")
    venv_root = idf_venv_root()
    if venv_root is not None:
        env["IDF_PYTHON_ENV_PATH"] = str(venv_root)
    return env
