"""session_meta.yaml 파서 — 다음 수집의 기본 라벨(`label_target`) 읽기.

`session_id` 는 이 파일에 없다 — `csi_session.next_session_id` 가 자동 부여한다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

_LABEL_TARGET_RE = re.compile(r"^label_target:\s*\"?([A-Za-z_]+)\"?\s*(?:#.*)?$")


def read_label_target(path: Path, *, default: Optional[str] = None) -> Optional[str]:
    """session_meta.yaml experiment.label_target 을 읽는다 (수집 라벨의 기본값).

    들여쓰기 깊이를 따지지 않고 키 이름만 본다 — 이 파일에 label_target 은 하나뿐이다.
    """
    if not path.is_file():
        return default
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _LABEL_TARGET_RE.match(stripped)
        if m:
            return m.group(1)
    return default
