#!/usr/bin/env python3
"""
프로젝트 루트 esp-idf 서브모듈 + .espressif 툴체인 bootstrap.

  python scripts/idf_bootstrap.py
  python scripts/idf_bootstrap.py -y
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from idf_env import idf_py_works
from idf_paths import (
    IDF_GIT_TAG,
    IDF_GIT_URL,
    IDF_TARGET,
    REPO_ROOT,
    idf_export_sh,
    repo_idf_path,
    repo_tools_path,
    resolve_idf_path,
    tools_ready_file,
)


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    print("[cmd]", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(cwd), env=env, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"command failed (exit {proc.returncode}): {' '.join(cmd)}")


def clone_idf_source(repo_root: Path) -> None:
    """esp-idf 소스가 없을 때 v5.2.2 태그로 clone (--recursive)."""
    dest = repo_idf_path(repo_root)
    if dest.exists():
        raise RuntimeError(f"refusing to clone over existing path: {dest}")
    print(f"[bootstrap] cloning ESP-IDF {IDF_GIT_TAG} into {dest} ...")
    _run(
        [
            "git",
            "clone",
            "--recursive",
            "--depth",
            "1",
            "--branch",
            IDF_GIT_TAG,
            IDF_GIT_URL,
            str(dest),
        ],
        cwd=repo_root,
    )


def git_submodule_init(repo_root: Path) -> None:
    export = idf_export_sh(repo_root)
    if export.is_file():
        return

    if (repo_root / ".git").is_dir():
        print("[bootstrap] git submodule update --init esp-idf ...")
        proc = subprocess.run(
            ["git", "submodule", "update", "--init", "--recursive", "esp-idf"],
            cwd=str(repo_root),
            check=False,
        )
        if export.is_file():
            return
        if proc.returncode != 0:
            print("[bootstrap] submodule init failed; falling back to git clone")

    clone_idf_source(repo_root)
    if not export.is_file():
        raise FileNotFoundError(f"ESP-IDF export.sh missing after clone: {export}")


def tools_are_ready(repo_root: Path) -> bool:
    if not idf_export_sh(repo_root).is_file():
        return False
    if tools_ready_file(repo_root).is_file() and idf_py_works(repo_root):
        return True
    return idf_py_works(repo_root)


def run_install(repo_root: Path, idf_path: Path) -> None:
    install_sh = idf_path / "install.sh"
    if not install_sh.is_file():
        raise FileNotFoundError(f"install.sh not found: {install_sh}")

    env = os.environ.copy()
    env["IDF_PATH"] = str(idf_path)
    # 툴체인·Python venv는 기본 ~/.espressif (IDF_TOOLS_PATH 미설정)

    print(
        "[bootstrap] installing ESP-IDF tools for "
        f"{IDF_TARGET} (~/.espressif, may take 10–30 min)..."
    )
    _run(["bash", str(install_sh), IDF_TARGET], cwd=idf_path, env=env)
    tools_ready_file(repo_root).write_text(f"idf={idf_path}\ntarget={IDF_TARGET}\n", encoding="utf-8")
    print("[bootstrap] tools install finished.")


def ensure_idf_ready(
    repo_root: Path = REPO_ROOT,
    *,
    yes: bool = False,
    skip_bootstrap: bool = False,
    allow_system_fallback: bool = True,
) -> Path:
    """
    repo esp-idf + 툴체인이 준비됐는지 확인. 없으면 submodule/init + install.sh.
    반환: 사용할 IDF_PATH.
    """
    repo_idf = repo_idf_path(repo_root)
    export = idf_export_sh(repo_root)

    if not export.is_file():
        if skip_bootstrap:
            if allow_system_fallback:
                path = resolve_idf_path(repo_root)
                if (path / "export.sh").is_file():
                    print(f"[bootstrap] using existing IDF_PATH: {path}")
                    return path
            raise FileNotFoundError(
                f"ESP-IDF not found at {repo_idf}\n"
                "  git submodule update --init esp-idf\n"
                "  python scripts/idf_bootstrap.py"
            )
        print(f"[bootstrap] initializing esp-idf submodule at {repo_idf} ...")
        git_submodule_init(repo_root)
        if not export.is_file():
            raise FileNotFoundError(f"esp-idf still missing after submodule init: {export}")

    idf_path = repo_idf.resolve()

    if tools_are_ready(repo_root):
        print(f"[bootstrap] ESP-IDF ready: {idf_path}")
        return idf_path

    if skip_bootstrap:
        if allow_system_fallback:
            fallback = resolve_idf_path(repo_root)
            if fallback != repo_idf and (fallback / "export.sh").is_file() and idf_py_works(repo_root):
                print(f"[bootstrap] using fallback IDF: {fallback}")
                return fallback
        raise RuntimeError(
            "ESP-IDF tools not installed for this project.\n"
            "  python scripts/idf_bootstrap.py"
        )

    if not yes:
        print(
            "[bootstrap] ESP-IDF tools are not installed yet.\n"
            f"  IDF_PATH: {idf_path}\n"
            "  Tools:    ~/.espressif (default)\n"
            "  This may take 10–30 minutes."
        )
        answer = input("Run install.sh now? [y/N]: ").strip().lower()
        if answer not in ("y", "yes"):
            raise RuntimeError("aborted — run: python scripts/idf_bootstrap.py -y")

    run_install(repo_root, idf_path)
    if not tools_are_ready(repo_root):
        raise RuntimeError("install finished but idf.py --version still fails")
    print(f"[bootstrap] ESP-IDF ready: {idf_path}")
    return idf_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap project-local ESP-IDF (submodule + tools)")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip install confirmation")
    parser.add_argument(
        "--skip-submodule",
        action="store_true",
        help="Do not run git submodule update (only install.sh if IDF source exists)",
    )
    args = parser.parse_args()
    try:
        if args.skip_submodule and not idf_export_sh().is_file():
            print("error: esp-idf/export.sh missing", file=sys.stderr)
            return 1
        if not args.skip_submodule:
            ensure_idf_ready(yes=args.yes, skip_bootstrap=False)
        else:
            idf = resolve_idf_path()
            if not tools_are_ready(REPO_ROOT):
                run_install(REPO_ROOT, idf)
        if not idf_py_works():
            raise RuntimeError("idf.py --version failed after bootstrap")
        print("[ok] idf environment loads successfully")
        return 0
    except (RuntimeError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
