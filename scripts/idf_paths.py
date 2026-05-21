"""MeshSense 프로젝트 내 ESP-IDF 경로 상수."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

IDF_SUBMODULE_DIR = "esp-idf"
TOOLS_DIR = ".espressif"
TOOLS_READY_MARKER = ".meshsense_tools_ready"
IDF_TARGET = "esp32s3"
IDF_GIT_TAG = "v5.2.2"
IDF_GIT_URL = "https://github.com/espressif/esp-idf.git"
# idf_tools.py venv 디렉터리: idf5.2_py3.12_env
IDF_VENV_IDF_VERSION = "5.2"


def repo_idf_path(repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / IDF_SUBMODULE_DIR


def repo_tools_path(repo_root: Path = REPO_ROOT) -> Path:
    return repo_root / TOOLS_DIR


def idf_export_sh(repo_root: Path = REPO_ROOT) -> Path:
    return repo_idf_path(repo_root) / "export.sh"


def tools_ready_file(repo_root: Path = REPO_ROOT) -> Path:
    return repo_tools_path(repo_root) / TOOLS_READY_MARKER


def default_system_idf_path() -> Path:
    return Path.home() / "esp" / "esp-idf"


def espressif_tools_root() -> Path:
    """ESP-IDF 툴·venv 루트 (~/.espressif 또는 IDF_TOOLS_PATH)."""
    override = os.environ.get("IDF_TOOLS_PATH")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".espressif"


def resolve_idf_path(repo_root: Path = REPO_ROOT) -> Path:
    """사용할 ESP-IDF 루트. MESHESENSE_IDF_PATH → repo submodule → IDF_PATH → ~/esp/esp-idf."""
    override = os.environ.get("MESHESENSE_IDF_PATH")
    if override:
        path = Path(override).expanduser().resolve()
        if (path / "export.sh").is_file():
            return path
        raise FileNotFoundError(f"MESHESENSE_IDF_PATH has no export.sh: {path}")

    repo = repo_idf_path(repo_root)
    if (repo / "export.sh").is_file():
        return repo

    env_idf = os.environ.get("IDF_PATH")
    if env_idf:
        path = Path(env_idf).resolve()
        if (path / "export.sh").is_file():
            return path

    fallback = default_system_idf_path()
    if (fallback / "export.sh").is_file():
        return fallback

    return repo


def _python_runnable(py: Path) -> bool:
    proc = subprocess.run(
        [str(py), "-c", "import sys; print(sys.version_info[0])"],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def find_idf_venv_python(idf_version: str = IDF_VENV_IDF_VERSION) -> Optional[Path]:
    """
    ESP-IDF install.sh 가 만든 venv 의 python.
    Mac·Python 버전마다 idf5.2_py3.12_env / py3.14_env 등 이름이 달라짐 → idf5.2_* 중 동작하는 최신 venv 선택.
    """
    env_root = espressif_tools_root() / "python_env"
    if not env_root.is_dir():
        return None

    pattern = f"idf{idf_version}_py*_env"
    best_py: Optional[Path] = None
    best_name = ""
    for venv_dir in sorted(env_root.glob(pattern)):
        py = venv_dir / "bin" / "python"
        if not py.is_file() or not _python_runnable(py):
            continue
        if venv_dir.name > best_name:
            best_name = venv_dir.name
            best_py = py
    return best_py


def idf_venv_root(py: Optional[Path] = None) -> Optional[Path]:
    """venv 루트 (…/idf5.2_py3.14_env). export 시 IDF_PYTHON_ENV_PATH 힌트용."""
    venv_py = py or find_idf_venv_python()
    if venv_py is None:
        return None
    return venv_py.parent.parent


def build_idf_path_prefixes(*, include_caller_python: bool = True) -> List[str]:
    """
    export.sh / install.sh 가 잘못된 시스템 python3(예: /usr/bin 3.9)를 고르지 않도록
    IDF venv·Homebrew·pyenv·conda·meshsense 실행 Python 순으로 PATH 앞에 둘 디렉터리.
    """
    prefixes: List[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path and path not in seen:
            seen.add(path)
            prefixes.append(path)

    venv_py = find_idf_venv_python()
    if venv_py is not None:
        add(str(venv_py.parent))

    if include_caller_python:
        caller = Path(sys.executable).resolve()
        if caller.name.startswith("python"):
            add(str(caller.parent))

    for candidate in (
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/opt/local/bin"),
    ):
        if (candidate / "python3").is_file():
            add(str(candidate))

    pyenv_root = os.environ.get("PYENV_ROOT")
    if pyenv_root:
        shims = Path(pyenv_root).expanduser() / "shims"
        if shims.is_dir():
            add(str(shims))
    else:
        default_shims = Path.home() / ".pyenv" / "shims"
        if default_shims.is_dir():
            add(str(default_shims))

    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        add(str(Path(conda_prefix) / "bin"))

    return prefixes
