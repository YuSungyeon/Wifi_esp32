#!/usr/bin/env python3
"""
device_registry.csv 관리 CLI (list | show | add | remove | verify).

사용 예:
  python scripts/device_registry.py list
  python scripts/device_registry.py verify
  python scripts/device_registry.py add --port /dev/cu.usbmodem101 --board-name RX4
  python scripts/device_registry.py add --mac E8:F6:0A:8A:XX:XX --id 104
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from esptool_mac import read_mac  # noqa: E402
from registry import (  # noqa: E402
    DEFAULT_REGISTRY_PATH,
    DeviceRecord,
    load_registry,
    lookup_by_device_id,
    lookup_by_mac,
    normalize_mac,
    save_registry,
    suggest_next_device_id,
    verify_registry,
)

REPO_ROOT = SCRIPT_DIR.parent
RX_PROJECT = REPO_ROOT / "esp32s3_csi_sender"


def cmd_list(registry: Path) -> int:
    records = load_registry(registry)
    if not records:
        print("(empty registry)")
        return 0
    print(f"{'id':>4}  {'board':<8}  {'sta_mac':<17}  notes")
    print("-" * 60)
    for rec in sorted(records, key=lambda r: r.device_id):
        note = rec.notes[:40] + ("…" if len(rec.notes) > 40 else "")
        print(f"{rec.device_id:4d}  {rec.board_name:<8}  {rec.sta_mac:<17}  {note}")
    return 0


def cmd_show(registry: Path, device_id: Optional[int], mac: Optional[str]) -> int:
    rec = None
    if device_id is not None:
        rec = lookup_by_device_id(device_id, registry)
        if rec is None:
            print(f"error: device_id {device_id} not found", file=sys.stderr)
            return 1
    elif mac is not None:
        rec = lookup_by_mac(mac, registry)
        if rec is None:
            print(f"error: MAC {normalize_mac(mac)} not found", file=sys.stderr)
            return 1
    else:
        print("error: specify --id or --mac", file=sys.stderr)
        return 2

    for key, val in rec.as_csv_row().items():
        print(f"{key}: {val}")
    return 0


def cmd_verify(registry: Path) -> int:
    errors = verify_registry(registry)
    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"[ok] {registry} is valid ({len(load_registry(registry))} device(s))")
    return 0


def cmd_add(
    registry: Path,
    *,
    port: Optional[str],
    mac: Optional[str],
    device_id: Optional[int],
    board_name: Optional[str],
    notes: str,
) -> int:
    if mac is None:
        if port is None:
            print("error: --mac or --port is required for add", file=sys.stderr)
            return 2
        try:
            mac = read_mac(port, cwd=str(RX_PROJECT))
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        mac = normalize_mac(mac)

    if lookup_by_mac(mac, registry) is not None:
        print(f"error: MAC {mac} already registered", file=sys.stderr)
        return 1

    records = load_registry(registry) if registry.exists() else []

    if device_id is None:
        device_id = suggest_next_device_id(registry)
    elif lookup_by_device_id(device_id, registry) is not None:
        print(f"error: device_id {device_id} already in use", file=sys.stderr)
        return 1

    if not board_name:
        board_name = f"RX{device_id}"

    new_rec = DeviceRecord(
        device_id=device_id,
        board_name=board_name,
        sta_mac=mac,
        room_x_m="0",
        room_y_m="0",
        height_m="1.2",
        orientation_deg="0",
        firmware_version="v0.1.0",
        notes=notes,
    )
    records.append(new_rec)
    save_registry(records, registry)
    print(f"[ok] added device_id={device_id} board_name={board_name} sta_mac={mac}")
    print(f"     flash: python scripts/flash_rx.py -p <PORT>")
    return 0


def cmd_remove(registry: Path, device_id: int, force: bool) -> int:
    records = load_registry(registry)
    kept: List[DeviceRecord] = []
    removed: Optional[DeviceRecord] = None
    for rec in records:
        if rec.device_id == device_id:
            removed = rec
        else:
            kept.append(rec)

    if removed is None:
        print(f"error: device_id {device_id} not found", file=sys.stderr)
        return 1

    if not force:
        answer = input(
            f"Remove device_id={device_id} ({removed.board_name}, {removed.sta_mac})? [y/N]: "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("aborted.")
            return 0

    save_registry(kept, registry)
    from flash_state import clear_flashed  # noqa: WPS433

    clear_flashed("rx", device_id)
    print(f"[ok] removed device_id={device_id}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage mac_collector/device_registry.csv")
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY_PATH,
        help=f"registry CSV path (default: {DEFAULT_REGISTRY_PATH})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all devices")
    sub.add_parser("verify", help="Validate registry file")

    show_p = sub.add_parser("show", help="Show one device")
    show_p.add_argument("--id", type=int, dest="device_id")
    show_p.add_argument("--mac", type=str)

    add_p = sub.add_parser("add", help="Add a device row")
    add_p.add_argument("--port", type=str, help="Read MAC from this serial port")
    add_p.add_argument("--mac", type=str, help="STA MAC (if not using --port)")
    add_p.add_argument("--id", type=int, dest="device_id", help="device_id (default: next free)")
    add_p.add_argument("--board-name", type=str, help="board_name (default: RX<id>)")
    add_p.add_argument("--notes", type=str, default="", help="notes column")

    rm_p = sub.add_parser("remove", help="Remove a device by device_id")
    rm_p.add_argument("--id", type=int, dest="device_id", required=True)
    rm_p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    args = parser.parse_args()

    if args.command == "list":
        return cmd_list(args.registry)
    if args.command == "verify":
        return cmd_verify(args.registry)
    if args.command == "show":
        return cmd_show(args.registry, args.device_id, args.mac)
    if args.command == "add":
        return cmd_add(
            args.registry,
            port=args.port,
            mac=args.mac,
            device_id=args.device_id,
            board_name=args.board_name,
            notes=args.notes,
        )
    if args.command == "remove":
        return cmd_remove(args.registry, args.device_id, force=args.yes)

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
