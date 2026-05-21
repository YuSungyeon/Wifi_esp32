"""플래시 완료 여부 (mac_collector/flash_state.json). 시간 없이 id 목록만 기록."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FLASH_STATE_PATH = REPO_ROOT / "mac_collector" / "flash_state.json"

FlashKind = Literal["tx", "rx"]


def _empty_state() -> dict:
    return {"tx": [], "rx": []}


def load_flash_state(path: Path = DEFAULT_FLASH_STATE_PATH) -> dict:
    if not path.is_file():
        return _empty_state()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _empty_state()
    if not isinstance(raw, dict):
        return _empty_state()
    out = _empty_state()
    for kind in ("tx", "rx"):
        vals = raw.get(kind, [])
        if isinstance(vals, list):
            out[kind] = sorted({int(x) for x in vals if isinstance(x, (int, float)) and int(x) == x})
    return out


def save_flash_state(state: dict, path: Path = DEFAULT_FLASH_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tx": sorted(state.get("tx", [])),
        "rx": sorted(state.get("rx", [])),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _flashed_set(state: dict, kind: FlashKind) -> Set[int]:
    return set(state.get(kind, []))


def mark_flashed(kind: FlashKind, node_id: int, path: Path = DEFAULT_FLASH_STATE_PATH) -> None:
    state = load_flash_state(path)
    ids = _flashed_set(state, kind)
    ids.add(int(node_id))
    state[kind] = sorted(ids)
    save_flash_state(state, path)


def clear_flashed(kind: FlashKind, node_id: int, path: Path = DEFAULT_FLASH_STATE_PATH) -> None:
    state = load_flash_state(path)
    ids = _flashed_set(state, kind)
    ids.discard(int(node_id))
    state[kind] = sorted(ids)
    save_flash_state(state, path)


def is_flashed(kind: FlashKind, node_id: int, path: Path = DEFAULT_FLASH_STATE_PATH) -> bool:
    state = load_flash_state(path)
    return int(node_id) in _flashed_set(state, kind)


def flash_label(kind: FlashKind, node_id: int, path: Path = DEFAULT_FLASH_STATE_PATH) -> str:
    return "플래시됨" if is_flashed(kind, node_id, path) else "미플래시"


def flash_symbol(kind: FlashKind, node_id: int, path: Path = DEFAULT_FLASH_STATE_PATH) -> str:
    return "●" if is_flashed(kind, node_id, path) else "○"


def prune_flash_state(
    *,
    tx_ids: Set[int],
    rx_ids: Set[int],
    path: Path = DEFAULT_FLASH_STATE_PATH,
) -> None:
    """registry에 없는 id는 상태 파일에서 제거."""
    state = load_flash_state(path)
    state["tx"] = sorted(_flashed_set(state, "tx") & tx_ids)
    state["rx"] = sorted(_flashed_set(state, "rx") & rx_ids)
    save_flash_state(state, path)


def count_flashed(
    kind: FlashKind,
    registered_ids: Set[int],
    path: Path = DEFAULT_FLASH_STATE_PATH,
) -> Tuple[int, int]:
    """(플래시됨 수, 등록 수)."""
    state = load_flash_state(path)
    flashed = _flashed_set(state, kind) & registered_ids
    return len(flashed), len(registered_ids)
