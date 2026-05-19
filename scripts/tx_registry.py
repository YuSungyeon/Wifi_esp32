"""tx_registry.csv 로드·검증·MAC 조회 (TX 보드 SSOT)."""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_TX_REGISTRY_PATH = REPO_ROOT / "mac_collector" / "tx_registry.csv"
TX_PROJECT = REPO_ROOT / "esp32s3_tx_ap_node"

MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")


@dataclass(frozen=True)
class TxRecord:
    tx_node_id: int
    board_name: str
    chip_mac: str
    room_x_m: str = ""
    room_y_m: str = ""
    height_m: str = ""
    firmware_version: str = ""
    notes: str = ""

    def as_csv_row(self) -> Dict[str, str]:
        return {
            "tx_node_id": str(self.tx_node_id),
            "board_name": self.board_name,
            "chip_mac": self.chip_mac,
            "room_x_m": self.room_x_m,
            "room_y_m": self.room_y_m,
            "height_m": self.height_m,
            "firmware_version": self.firmware_version,
            "notes": self.notes,
        }


def normalize_mac(mac: str) -> str:
    raw = mac.strip().upper().replace("-", ":")
    if ":" not in raw and len(raw) == 12 and re.fullmatch(r"[0-9A-F]{12}", raw):
        raw = ":".join(raw[i : i + 2] for i in range(0, 12, 2))
    if not MAC_RE.match(raw):
        raise ValueError(f"invalid MAC address: {mac!r}")
    return raw


def load_tx_registry(path: Path = DEFAULT_TX_REGISTRY_PATH) -> List[TxRecord]:
    if not path.exists():
        raise FileNotFoundError(f"tx registry not found: {path}")

    records: List[TxRecord] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "tx_node_id" not in reader.fieldnames or "chip_mac" not in reader.fieldnames:
            raise ValueError(f"tx registry missing required columns (tx_node_id, chip_mac): {path}")

        for line_no, row in enumerate(reader, start=2):
            raw_id = (row.get("tx_node_id") or "").strip()
            raw_mac = (row.get("chip_mac") or "").strip()
            if not raw_id and not raw_mac:
                continue
            if not raw_id or not raw_mac:
                raise ValueError(f"{path}:{line_no}: tx_node_id and chip_mac are required")

            records.append(
                TxRecord(
                    tx_node_id=int(raw_id),
                    board_name=(row.get("board_name") or "").strip(),
                    chip_mac=normalize_mac(raw_mac),
                    room_x_m=(row.get("room_x_m") or "").strip(),
                    room_y_m=(row.get("room_y_m") or "").strip(),
                    height_m=(row.get("height_m") or "").strip(),
                    firmware_version=(row.get("firmware_version") or "").strip(),
                    notes=(row.get("notes") or "").strip(),
                )
            )
    return records


def build_tx_indexes(records: List[TxRecord]) -> Tuple[Dict[str, TxRecord], Dict[int, TxRecord]]:
    by_mac: Dict[str, TxRecord] = {}
    by_id: Dict[int, TxRecord] = {}
    for rec in records:
        by_mac[rec.chip_mac] = rec
        by_id[rec.tx_node_id] = rec
    return by_mac, by_id


def lookup_tx_by_mac(mac: str, path: Path = DEFAULT_TX_REGISTRY_PATH) -> Optional[TxRecord]:
    normalized = normalize_mac(mac)
    by_mac, _ = build_tx_indexes(load_tx_registry(path))
    return by_mac.get(normalized)


def lookup_tx_by_node_id(tx_node_id: int, path: Path = DEFAULT_TX_REGISTRY_PATH) -> Optional[TxRecord]:
    _, by_id = build_tx_indexes(load_tx_registry(path))
    return by_id.get(tx_node_id)


def verify_tx_registry(path: Path = DEFAULT_TX_REGISTRY_PATH) -> List[str]:
    errors: List[str] = []
    try:
        records = load_tx_registry(path)
    except (FileNotFoundError, ValueError) as exc:
        return [str(exc)]

    seen_ids: Dict[int, int] = {}
    seen_macs: Dict[str, int] = {}
    for idx, rec in enumerate(records, start=1):
        if rec.tx_node_id in seen_ids:
            errors.append(
                f"duplicate tx_node_id {rec.tx_node_id} (rows {seen_ids[rec.tx_node_id]} and {idx})"
            )
        else:
            seen_ids[rec.tx_node_id] = idx

        if rec.chip_mac in seen_macs:
            errors.append(f"duplicate chip_mac {rec.chip_mac} (rows {seen_macs[rec.chip_mac]} and {idx})")
        else:
            seen_macs[rec.chip_mac] = idx

    return errors


def suggest_next_tx_node_id(path: Path = DEFAULT_TX_REGISTRY_PATH) -> int:
    if not path.exists():
        return 1
    records = load_tx_registry(path)
    if not records:
        return 1
    return max(rec.tx_node_id for rec in records) + 1


def save_tx_registry(records: List[TxRecord], path: Path = DEFAULT_TX_REGISTRY_PATH) -> None:
    fieldnames = [
        "tx_node_id",
        "board_name",
        "chip_mac",
        "room_x_m",
        "room_y_m",
        "height_m",
        "firmware_version",
        "notes",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in sorted(records, key=lambda r: r.tx_node_id):
            writer.writerow(rec.as_csv_row())


# --- CLI ---

def _cmd_list(registry: Path) -> int:
    records = load_tx_registry(registry)
    if not records:
        print("(empty tx registry)")
        return 0
    print(f"{'id':>3}  {'board':<8}  {'chip_mac':<17}  notes")
    print("-" * 58)
    for rec in sorted(records, key=lambda r: r.tx_node_id):
        note = rec.notes[:36] + ("…" if len(rec.notes) > 36 else "")
        print(f"{rec.tx_node_id:3d}  {rec.board_name:<8}  {rec.chip_mac:<17}  {note}")
    return 0


def _cmd_show(registry: Path, tx_node_id: Optional[int], mac: Optional[str]) -> int:
    rec = None
    if tx_node_id is not None:
        rec = lookup_tx_by_node_id(tx_node_id, registry)
        if rec is None:
            print(f"error: tx_node_id {tx_node_id} not found", file=sys.stderr)
            return 1
    elif mac is not None:
        rec = lookup_tx_by_mac(mac, registry)
        if rec is None:
            print(f"error: MAC {normalize_mac(mac)} not found", file=sys.stderr)
            return 1
    else:
        print("error: specify --id or --mac", file=sys.stderr)
        return 2

    for key, val in rec.as_csv_row().items():
        print(f"{key}: {val}")
    return 0


def _cmd_verify(registry: Path) -> int:
    errors = verify_tx_registry(registry)
    if errors:
        for err in errors:
            print(f"error: {err}", file=sys.stderr)
        return 1
    print(f"[ok] {registry} is valid ({len(load_tx_registry(registry))} TX node(s))")
    return 0


def _cmd_add(
    registry: Path,
    *,
    port: Optional[str],
    mac: Optional[str],
    tx_node_id: Optional[int],
    board_name: Optional[str],
    notes: str,
) -> int:
    sys.path.insert(0, str(SCRIPT_DIR))
    from esptool_mac import read_mac  # noqa: WPS433

    if mac is None:
        if port is None:
            print("error: --mac or --port is required for add", file=sys.stderr)
            return 2
        try:
            mac = read_mac(port, cwd=str(TX_PROJECT))
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    else:
        mac = normalize_mac(mac)

    if lookup_tx_by_mac(mac, registry) is not None:
        print(f"error: MAC {mac} already registered", file=sys.stderr)
        return 1

    records = load_tx_registry(registry) if registry.exists() else []

    if tx_node_id is None:
        tx_node_id = suggest_next_tx_node_id(registry)
    elif lookup_tx_by_node_id(tx_node_id, registry) is not None:
        print(f"error: tx_node_id {tx_node_id} already in use", file=sys.stderr)
        return 1

    if not board_name:
        board_name = f"TX{tx_node_id}"

    records.append(
        TxRecord(
            tx_node_id=tx_node_id,
            board_name=board_name,
            chip_mac=mac,
            room_x_m="1.5",
            room_y_m="1.5",
            height_m="1.2",
            firmware_version="v0.1.0",
            notes=notes,
        )
    )
    save_tx_registry(records, registry)
    print(f"[ok] added tx_node_id={tx_node_id} board_name={board_name} chip_mac={mac}")
    print("     flash: python scripts/flash_tx.py -p <PORT>")
    print("     RX/TX flash: scripts/meshsense_config.json (ap.ssid / ap.pass)")
    return 0


def _cmd_remove(registry: Path, tx_node_id: int, force: bool) -> int:
    records = load_tx_registry(registry)
    kept: List[TxRecord] = []
    removed: Optional[TxRecord] = None
    for rec in records:
        if rec.tx_node_id == tx_node_id:
            removed = rec
        else:
            kept.append(rec)

    if removed is None:
        print(f"error: tx_node_id {tx_node_id} not found", file=sys.stderr)
        return 1

    if not force:
        answer = input(
            f"Remove tx_node_id={tx_node_id} ({removed.board_name}, {removed.chip_mac})? [y/N]: "
        ).strip().lower()
        if answer not in ("y", "yes"):
            print("aborted.")
            return 0

    save_tx_registry(kept, registry)
    print(f"[ok] removed tx_node_id={tx_node_id}")
    return 0


def main_cli() -> int:
    parser = argparse.ArgumentParser(description="Manage mac_collector/tx_registry.csv")
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_TX_REGISTRY_PATH,
        help=f"tx registry CSV (default: {DEFAULT_TX_REGISTRY_PATH})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="List all TX nodes")
    sub.add_parser("verify", help="Validate tx registry file")

    show_p = sub.add_parser("show", help="Show one TX node")
    show_p.add_argument("--id", type=int, dest="tx_node_id")
    show_p.add_argument("--mac", type=str)

    add_p = sub.add_parser("add", help="Add a TX node row")
    add_p.add_argument("--port", type=str, help="Read chip MAC from serial port")
    add_p.add_argument("--mac", type=str, help="chip MAC (if not using --port)")
    add_p.add_argument("--id", type=int, dest="tx_node_id", help="tx_node_id (default: next free)")
    add_p.add_argument("--board-name", type=str, help="board_name (default: TX<id>)")
    add_p.add_argument("--notes", type=str, default="", help="notes column")

    rm_p = sub.add_parser("remove", help="Remove a TX node by tx_node_id")
    rm_p.add_argument("--id", type=int, dest="tx_node_id", required=True)
    rm_p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    args = parser.parse_args()

    if args.command == "list":
        return _cmd_list(args.registry)
    if args.command == "verify":
        return _cmd_verify(args.registry)
    if args.command == "show":
        return _cmd_show(args.registry, args.tx_node_id, args.mac)
    if args.command == "add":
        return _cmd_add(
            args.registry,
            port=args.port,
            mac=args.mac,
            tx_node_id=args.tx_node_id,
            board_name=args.board_name,
            notes=args.notes,
        )
    if args.command == "remove":
        return _cmd_remove(args.registry, args.tx_node_id, force=args.yes)

    return 2


if __name__ == "__main__":
    raise SystemExit(main_cli())
