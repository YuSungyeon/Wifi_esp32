"""device_registry.csv 로드·검증·MAC 조회 (MeshSense SSOT)."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY_PATH = REPO_ROOT / "mac_collector" / "device_registry.csv"

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


@dataclass(frozen=True)
class DeviceRecord:
    device_id: int
    board_name: str
    sta_mac: str
    room_x_m: str = ""
    room_y_m: str = ""
    height_m: str = ""
    orientation_deg: str = ""
    firmware_version: str = ""
    notes: str = ""

    def as_csv_row(self) -> Dict[str, str]:
        return {
            "device_id": str(self.device_id),
            "board_name": self.board_name,
            "sta_mac": self.sta_mac,
            "room_x_m": self.room_x_m,
            "room_y_m": self.room_y_m,
            "height_m": self.height_m,
            "orientation_deg": self.orientation_deg,
            "firmware_version": self.firmware_version,
            "notes": self.notes,
        }


def normalize_mac(mac: str) -> str:
    """다양한 MAC 표기를 AA:BB:CC:DD:EE:FF (대문자)로 통일."""
    raw = mac.strip().upper().replace("-", ":")
    if ":" not in raw and len(raw) == 12 and re.fullmatch(r"[0-9A-F]{12}", raw):
        raw = ":".join(raw[i : i + 2] for i in range(0, 12, 2))
    if not MAC_RE.match(raw):
        raise ValueError(f"invalid MAC address: {mac!r}")
    return raw


def load_registry(path: Path = DEFAULT_REGISTRY_PATH) -> List[DeviceRecord]:
    if not path.exists():
        raise FileNotFoundError(f"registry not found: {path}")

    records: List[DeviceRecord] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "device_id" not in reader.fieldnames or "sta_mac" not in reader.fieldnames:
            raise ValueError(f"registry missing required columns (device_id, sta_mac): {path}")

        for line_no, row in enumerate(reader, start=2):
            raw_id = (row.get("device_id") or "").strip()
            raw_mac = (row.get("sta_mac") or "").strip()
            if not raw_id and not raw_mac:
                continue
            if not raw_id or not raw_mac:
                raise ValueError(f"{path}:{line_no}: device_id and sta_mac are required")

            records.append(
                DeviceRecord(
                    device_id=int(raw_id),
                    board_name=(row.get("board_name") or "").strip(),
                    sta_mac=normalize_mac(raw_mac),
                    room_x_m=(row.get("room_x_m") or "").strip(),
                    room_y_m=(row.get("room_y_m") or "").strip(),
                    height_m=(row.get("height_m") or "").strip(),
                    orientation_deg=(row.get("orientation_deg") or "").strip(),
                    firmware_version=(row.get("firmware_version") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return records


def build_indexes(records: List[DeviceRecord]) -> Tuple[Dict[str, DeviceRecord], Dict[int, DeviceRecord]]:
    by_mac: Dict[str, DeviceRecord] = {}
    by_id: Dict[int, DeviceRecord] = {}
    for rec in records:
        by_mac[rec.sta_mac] = rec
        by_id[rec.device_id] = rec
    return by_mac, by_id


def load_device_ids(path: Path = DEFAULT_REGISTRY_PATH) -> Set[int]:
    if not path.exists():
        return set()
    return {rec.device_id for rec in load_registry(path)}


def lookup_by_mac(mac: str, path: Path = DEFAULT_REGISTRY_PATH) -> Optional[DeviceRecord]:
    normalized = normalize_mac(mac)
    by_mac, _ = build_indexes(load_registry(path))
    return by_mac.get(normalized)


def lookup_by_device_id(device_id: int, path: Path = DEFAULT_REGISTRY_PATH) -> Optional[DeviceRecord]:
    _, by_id = build_indexes(load_registry(path))
    return by_id.get(device_id)


def verify_registry(path: Path = DEFAULT_REGISTRY_PATH) -> List[str]:
    """검증 오류 메시지 목록. 비어 있으면 OK."""
    errors: List[str] = []
    try:
        records = load_registry(path)
    except (FileNotFoundError, ValueError) as exc:
        return [str(exc)]

    seen_ids: Dict[int, int] = {}
    seen_macs: Dict[str, int] = {}
    for idx, rec in enumerate(records, start=1):
        if rec.device_id in seen_ids:
            errors.append(
                f"duplicate device_id {rec.device_id} (rows {seen_ids[rec.device_id]} and {idx})"
            )
        else:
            seen_ids[rec.device_id] = idx

        if rec.sta_mac in seen_macs:
            errors.append(f"duplicate sta_mac {rec.sta_mac} (rows {seen_macs[rec.sta_mac]} and {idx})")
        else:
            seen_macs[rec.sta_mac] = idx

    return errors


def suggest_next_device_id(path: Path = DEFAULT_REGISTRY_PATH) -> int:
    if not path.exists():
        return 101
    records = load_registry(path)
    if not records:
        return 101
    return max(rec.device_id for rec in records) + 1


def save_registry(records: List[DeviceRecord], path: Path = DEFAULT_REGISTRY_PATH) -> None:
    fieldnames = [
        "device_id",
        "board_name",
        "sta_mac",
        "room_x_m",
        "room_y_m",
        "height_m",
        "orientation_deg",
        "firmware_version",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in sorted(records, key=lambda r: r.device_id):
            writer.writerow(rec.as_csv_row())
