#!/usr/bin/env python3
"""수집 세션 디렉터리와 매니페스트(`session.json`) 관리.

라벨은 **수집 시점에** 매니페스트로 박힌다. 이전에는 라벨이 후처리 CLI 인자
(`Preprocessing.py --label`, 기본 empty)에만 있어서 어떤 세션이 무슨 상태였는지
데이터만 보고는 알 수 없었다.

디렉터리 이름에 수집 시각이 들어가 **충돌이 원천적으로 불가능**하다. 이전 레이아웃은
`session_<id>` 뿐이라 `session_meta.yaml` 의 session_id 갱신을 잊으면 기존 파일에
조용히 append 됐고, 실제로 여러 세션이 그렇게 오염됐다.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Iterable, Optional

from csi_store import FRAME_VERSION, LABELS

MANIFEST_SCHEMA = 1


#: 세션 디렉터리 이름에서 순번을 뽑는 패턴. 구 레이아웃(`session_<N>`)도 함께 인식한다.
_SESSION_ID_RE = re.compile(r"(?:_s|^session_)(\d+)$")


def next_session_id(output_dir: Path) -> int:
    """이미 수집한 세션들의 순번 중 최댓값 + 1.

    예전에는 `session_meta.yaml` 의 `session_id` 를 사람이 매번 손으로 올려야 했고,
    잊으면 같은 디렉터리에 데이터가 덧붙었다. 순번은 파일시스템이 가진 정보만으로
    결정할 수 있으므로 사람이 관리할 이유가 없다.
    """
    raw = Path(output_dir) / "raw"
    if not raw.is_dir():
        return 1
    used = [
        int(m.group(1))
        for d in raw.glob("*/*")
        if d.is_dir() and (m := _SESSION_ID_RE.search(d.name))
    ]
    return max(used, default=0) + 1


def session_dir_name(label: str, session_id: int, when: Optional[float] = None) -> str:
    """`<HHMMSS>_<label>_s<session_id>` — 라벨이 경로에서 바로 보이게."""
    if label not in LABELS:
        raise ValueError(f"unknown label {label!r}; expected one of {list(LABELS)}")
    return f"{time.strftime('%H%M%S', time.localtime(when))}_{label}_s{session_id}"


def repo_provenance() -> dict:
    """수집에 쓰인 코드의 신원. 세션마다 박아둔다.

    "이 데이터는 어느 코드로 찍었나"를 사후에 재구성할 방법이 없어서 수집 시점에 남긴다.
    펌웨어와 호스트 스크립트가 같은 저장소에 있으므로 커밋 하나가 둘 다 식별한다.
    `dirty=True` 면 커밋되지 않은 변경이 섞인 상태로 찍은 것이라 재현이 보장되지 않는다.
    """
    root = Path(__file__).resolve().parents[1]

    def git(*args) -> str | None:
        try:
            r = subprocess.run(["git", *args], cwd=root, capture_output=True,
                               text=True, timeout=5)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain")
    return {
        "git_commit": commit,
        "git_branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
        "git_describe": git("describe", "--tags", "--always", "--dirty"),
    }


def create_session(
    output_dir: Path,
    *,
    label: str,
    session_id: int,
    session_meta: Optional[Path] = None,
    pipeline: str = "usb",
) -> Path:
    """세션 디렉터리를 만들고 초기 매니페스트를 쓴다. 이미 있으면 FileExistsError."""
    now = time.time()
    d = Path(output_dir) / "raw" / time.strftime("%Y%m%d", time.localtime(now)) / session_dir_name(
        label, session_id, now
    )
    d.mkdir(parents=True, exist_ok=False)

    if session_meta and Path(session_meta).is_file():
        (d / "session_meta_snapshot.yaml").write_text(
            Path(session_meta).read_text(encoding="utf-8"), encoding="utf-8"
        )

    write_manifest(
        d,
        {
            "schema": MANIFEST_SCHEMA,
            "pipeline": pipeline,
            "frame_version": FRAME_VERSION,
            "session_id": session_id,
            "label": label,
            "started_at_unix_us": int(now * 1_000_000),
            "ended_at_unix_us": None,
            "provenance": repo_provenance(),
            "devices": [],
        },
    )
    return d


def read_manifest(session_dir: Path) -> dict:
    return json.loads((Path(session_dir) / "session.json").read_text(encoding="utf-8"))


def write_manifest(session_dir: Path, manifest: dict) -> None:
    (Path(session_dir) / "session.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def write_device_stats(session_dir: Path, device_id: int, stats: dict) -> Path:
    """reader 가 종료 시 자기 통계를 남긴다. 매니페스트 동시 쓰기를 피하려고 파일을 분리."""
    p = Path(session_dir) / f"device_{device_id}.stats.json"
    p.write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def finalize_session(session_dir: Path) -> dict:
    """reader 들이 남긴 `device_*.stats.json` 을 매니페스트로 합치고 조각 파일을 지운다."""
    session_dir = Path(session_dir)
    manifest = read_manifest(session_dir)
    devices = []
    for p in sorted(session_dir.glob("device_*.stats.json")):
        devices.append(json.loads(p.read_text(encoding="utf-8")))
        p.unlink()
    manifest["devices"] = sorted(devices, key=lambda d: d.get("device_id", 0))
    manifest["ended_at_unix_us"] = int(time.time() * 1_000_000)
    write_manifest(session_dir, manifest)
    return manifest


def summarize(manifest: dict) -> Iterable[str]:
    """CLI 출력용 한 줄 요약들."""
    dur_us = (manifest.get("ended_at_unix_us") or 0) - (manifest.get("started_at_unix_us") or 0)
    dur = dur_us / 1e6 if dur_us > 0 else 0.0
    yield f"label={manifest['label']}  session_id={manifest['session_id']}  {dur:.1f}s"
    for d in manifest.get("devices", []):
        span = d.get("span_s") or dur          # 보드 시계 기준 수집 구간
        hz = d["frames"] / span if span > 0 else 0.0
        yield (
            f"  RX{d['device_id']} ({d.get('board_name', '?')}): frames={d['frames']} "
            f"{hz:.1f}Hz crc_fail={d['crc_fail']} invalid={d['invalid']} "
            f"resync={d['resync']} seq_gap={d['seq_gap']} boot_changes={d['boot_changes']}"
        )
