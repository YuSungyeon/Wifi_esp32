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


#: 매니페스트가 있는 세션 디렉터리 이름에서 순번을 뽑는 패턴.
_SESSION_ID_RE = re.compile(r"(?:_s|^session_)(\d+)$")


def next_session_id(output_dir: Path) -> int:
    """`session.json`이 있는 세션들의 순번 중 최댓값 + 1.

    예전에는 `session_meta.yaml` 의 `session_id` 를 사람이 매번 손으로 올려야 했고,
    잊으면 같은 디렉터리에 데이터가 덧붙었다. 순번은 파일시스템이 가진 정보만으로
    결정할 수 있으므로 사람이 관리할 이유가 없다. 매니페스트가 없는 구형
    `session_<N>` 폴더는 새 수집의 순번을 차지하지 않는다.
    """
    raw = Path(output_dir) / "raw"
    if not raw.is_dir():
        return 1
    used = [
        int(m.group(1))
        for d in raw.glob("*/*")
        if (
            d.is_dir()
            and (d / "session.json").is_file()
            and (m := _SESSION_ID_RE.search(d.name))
        )
    ]
    return max(used, default=0) + 1


#: 여러 상태를 섞어 찍는 실시간 세션. 학습 클래스가 아니므로 학습·평가·내보내기 목록에서 빠진다
#: (그쪽은 전부 `LABELS` 로 거른다). `LABELS` 자체는 전처리 `LABEL_MAP` 과 같아야 하므로 넣지 않는다.
LIVE_LABEL = "live"
SESSION_LABELS = (*LABELS, LIVE_LABEL)


def session_dir_name(label: str, session_id: int, when: Optional[float] = None) -> str:
    """`<HHMMSS>_<label>_s<session_id>` — 라벨이 경로에서 바로 보이게."""
    if label not in SESSION_LABELS:
        raise ValueError(f"unknown label {label!r}; expected one of {list(SESSION_LABELS)}")
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
    device_registry: Optional[Path] = None,
    tx_registry: Optional[Path] = None,
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
    # device_registry.csv/tx_registry.csv 는 계속 바뀌는 현재값이라, 이 세션을 찍을 때
    # 실제로 어떤 좌표였는지는 스냅샷 없이는 나중에 알 방법이 없다 (파일 하나가 배치
    # 이력을 덮어쓰며 지나간다). session_meta 와 같은 이유로 같이 남긴다.
    if device_registry and Path(device_registry).is_file():
        (d / "device_registry_snapshot.csv").write_text(
            Path(device_registry).read_text(encoding="utf-8"), encoding="utf-8"
        )
    if tx_registry and Path(tx_registry).is_file():
        (d / "tx_registry_snapshot.csv").write_text(
            Path(tx_registry).read_text(encoding="utf-8"), encoding="utf-8"
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


def set_session_note(session_dir: Path, text: str) -> None:
    """세션 고유 사정(시야 가림 등)을 스냅샷 끝 `session_note:` 한 줄로 남긴다. 다시 쓰면 교체."""
    p = Path(session_dir) / "session_meta_snapshot.yaml"
    lines = p.read_text(encoding="utf-8").splitlines() if p.is_file() else []
    lines = [l for l in lines if not l.startswith("session_note:")]
    if text.strip():
        lines.append("session_note: " + json.dumps(text.strip(), ensure_ascii=False))  # JSON 문자열 = 유효한 YAML
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


#: 품질 판정의 **유일한 정의**. 제어판 세션 목록·학습 목록·CLI 수집 직후 판정이 모두 이것을
#: 쓴다. 전에는 두 벌이었다 — 제어판은 seq_gap 까지 봤고 학습 목록은 RX 3대만 봤다.
#: 기준값은 `doc/collection-protocol.md` §7 과 같다. 순서가 곧 센 정도다.
_LEVELS = ("ok", "check", "recollect")
#: 화면에 쓰는 말. 제어판 태그와 CLI 판정 줄이 같은 말을 써야 사람이 둘을 같은 것으로 읽는다.
VERDICT = {"ok": "정상", "check": "확인", "recollect": "재수집"}


def quality(devices: list) -> tuple:
    """세션 품질 → `(level, text)`. `level ∈ ok | check | recollect`.

    여러 조건이 겹치면 **가장 센 level 하나**와 문장들을 ` / ` 로 잇는다. 옛 매니페스트에
    없는 키는 0 으로 본다 — 키가 없다고 죽으면 지난 세션을 못 읽는다.
    """
    notes: list = []
    worst = 0
    if len(devices) != 3:
        notes.append(f"RX {len(devices)}대만 기록됐습니다 — 3대가 필요합니다")
        worst = 2
    for d in devices:
        rx = d.get("device_id", "?")
        if d.get("tx_back", 0):
            notes.append("TX 가 수집 중 재부팅했습니다 — 시간 격자가 깨져 "
                         "같은 라벨로 다시 찍으세요")
            worst = max(worst, 2)
        if d.get("boot_changes", 0):
            notes.append(f"RX{rx} 가 수집 중 재부팅했습니다 — 전원·케이블 확인 후 "
                         "같은 라벨로 다시 찍으세요")
            worst = max(worst, 2)
        if d.get("crc_fail", 0):
            notes.append(f"RX{rx} CRC 실패 {d['crc_fail']}건 — 케이블 확인")
            worst = max(worst, 1)
        frames = max(d.get("frames", 1), 1)
        if d.get("seq_gap", 0) > 0.01 * frames:
            notes.append(f"RX{rx} 손실 {100 * d['seq_gap'] / frames:.1f}% — "
                         "학습엔 쓸 수 있으나 무선 경합 의심")
            worst = max(worst, 1)
    return _LEVELS[worst], " / ".join(notes)


def verdict_line(devices: list) -> str:
    """수집 직후 CLI 에 찍는 한 줄."""
    level, text = quality(devices)
    return f"[판정] {VERDICT[level]}" + (f" — {text}" if text else "")
