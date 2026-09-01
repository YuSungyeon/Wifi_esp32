#!/usr/bin/env python3
"""`idf.py -p <port> flash` 한 줄 실행기 (GUI 용). 로그를 그대로 흘려보낸다.

    python scripts/_idf_flash.py <project_dir> <port>
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from idf_env import run_in_idf_shell  # noqa: E402

if len(sys.argv) != 3:
    print("usage: _idf_flash.py <project_dir> <port>", file=sys.stderr)
    raise SystemExit(2)

project, port = Path(sys.argv[1]), sys.argv[2]
if not (project / "CMakeLists.txt").is_file():
    print(f"[중단] 프로젝트가 아닙니다: {project}", file=sys.stderr)
    raise SystemExit(2)

print(f"[flash] {project.name} → {port}", flush=True)
raise SystemExit(run_in_idf_shell(["idf.py", "-p", port, "flash"], cwd=project, check=False).returncode)
