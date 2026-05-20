"""MeshSense 프로젝트 내 ESP-IDF 경로 상수."""

from __future__ import annotations

import os
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

IDF_SUBMODULE_DIR = "esp-idf"
TOOLS_DIR = ".espressif"
TOOLS_READY_MARKER = ".meshsense_tools_ready"
IDF_TARGET = "esp32s3"
IDF_GIT_TAG = "v5.2.2"
IDF_GIT_URL = "https://github.com/espressif/esp-idf.git"


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
