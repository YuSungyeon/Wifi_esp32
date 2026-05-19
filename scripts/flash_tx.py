#!/usr/bin/env python3
"""
TX/AP 펌웨어 플래시: USB chip MAC → tx_registry.csv 조회 → idf.py build flash.

사용 예:
  python scripts/flash_tx.py -p /dev/cu.usbmodem101
  python scripts/flash_tx.py -p /dev/cu.usbmodem101 --clean --monitor -y
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
TX_PROJECT = REPO_ROOT / "esp32s3_tx_ap_node"
DEFAULT_TX_REGISTRY = REPO_ROOT / "mac_collector" / "tx_registry.csv"

sys.path.insert(0, str(SCRIPT_DIR))
from esptool_mac import read_mac  # noqa: E402
from idf_util import run_idf  # noqa: E402
from meshsense_config import DEFAULT_CONFIG_PATH, MeshSenseConfig, load_meshsense_config  # noqa: E402
from tx_registry import lookup_tx_by_mac  # noqa: E402


def confirm_flash(
    port: str,
    mac: str,
    tx_node_id: int,
    board_name: str,
    cfg: MeshSenseConfig,
) -> bool:
    print("--- TX flash plan ---")
    print(f"  port:        {port}")
    print(f"  chip MAC:    {mac}")
    print(f"  tx_node_id:  {tx_node_id}  ({board_name or '?'})")
    print(f"  SoftAP:      {cfg.ap_ssid}  ch={cfg.ap_channel}")
    print(f"  UDP:         :{cfg.ap_broadcast_port} every {cfg.ap_interval_ms}ms")
    print("  session:     (Mac session_meta.yaml — not in firmware)")
    answer = input("Proceed? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(description="Flash TX/AP node using tx_registry.csv")
    parser.add_argument("-p", "--port", required=True, help="Serial port")
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_TX_REGISTRY,
        help=f"tx registry CSV (default: {DEFAULT_TX_REGISTRY})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"MeshSense config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--clean", action="store_true", help="Run idf.py fullclean before build")
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--flash-only", action="store_true")
    parser.add_argument("--monitor", action="store_true")
    parser.add_argument("-y", "--yes", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.build_only and args.flash_only:
        print("error: --build-only and --flash-only are mutually exclusive", file=sys.stderr)
        return 2

    if args.flash_only:
        print("warning: --flash-only may flash a binary built for a different tx_node_id", file=sys.stderr)

    try:
        cfg = load_meshsense_config(args.config)
        mac = read_mac(args.port, cwd=str(TX_PROJECT))
        rec = lookup_tx_by_mac(mac, args.registry)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if rec is None:
        print(f"error: chip MAC {mac} is not in {args.registry}", file=sys.stderr)
        print(
            "  Register the TX board first:\n"
            f"    python scripts/tx_registry.py add --port {args.port}\n"
            f"    python scripts/tx_registry.py add --mac {mac} --board-name TX1",
            file=sys.stderr,
        )
        return 1

    if not args.yes and not args.dry_run:
        if not confirm_flash(args.port, mac, rec.tx_node_id, rec.board_name, cfg):
            print("aborted.")
            return 0

    defines = cfg.tx_cmake_defines(rec.tx_node_id)

    try:
        if args.clean and not args.flash_only:
            run_idf(["fullclean"], cwd=TX_PROJECT, dry_run=args.dry_run)

        if not args.flash_only:
            run_idf(["build", *defines], cwd=TX_PROJECT, dry_run=args.dry_run)

        if not args.build_only:
            run_idf(["-p", args.port, "flash"], cwd=TX_PROJECT, dry_run=args.dry_run)

        if args.monitor and not args.build_only and not args.dry_run:
            run_idf(["-p", args.port, "monitor"], cwd=TX_PROJECT, dry_run=False)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        print(f"[ok] flashed tx_node_id={rec.tx_node_id} ({rec.board_name}) chip_mac={mac}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
