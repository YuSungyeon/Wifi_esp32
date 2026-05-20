#!/usr/bin/env python3
"""
RX 펌웨어 플래시: USB MAC → device_registry.csv 조회 → idf.py build flash.

사용 예:
  python scripts/flash_rx.py -p /dev/cu.usbmodem101
  python scripts/flash_rx.py -p /dev/cu.usbmodem101 --clean --monitor
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
RX_PROJECT = REPO_ROOT / "esp32s3_csi_sender"
DEFAULT_REGISTRY = REPO_ROOT / "mac_collector" / "device_registry.csv"

sys.path.insert(0, str(SCRIPT_DIR))
from esptool_mac import read_mac  # noqa: E402
from idf_bootstrap import ensure_idf_ready  # noqa: E402
from idf_util import run_idf  # noqa: E402
from meshsense_config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    MeshSenseConfig,
    load_meshsense_config,
)
from registry import lookup_by_mac  # noqa: E402


def confirm_flash(
    port: str,
    mac: str,
    device_id: int,
    board_name: str,
    cfg: MeshSenseConfig,
) -> bool:
    print("--- RX flash plan ---")
    print(f"  port:       {port}")
    print(f"  MAC:        {mac}")
    print(f"  device_id:  {device_id}  ({board_name or '?'})")
    print(f"  collector:  {cfg.collector_ip}:{cfg.collector_port}")
    print(f"  Wi-Fi AP:   {cfg.ap_ssid}")
    print("  session:    (Mac session_meta.yaml — not in firmware)")
    answer = input("Proceed? [y/N]: ").strip().lower()
    return answer in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(description="Flash RX CSI sender using device_registry.csv")
    parser.add_argument("-p", "--port", required=True, help="Serial port (e.g. /dev/cu.usbmodem101)")
    parser.add_argument(
        "--registry",
        type=Path,
        default=DEFAULT_REGISTRY,
        help=f"device registry CSV (default: {DEFAULT_REGISTRY})",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"MeshSense config JSON (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument("--clean", action="store_true", help="Run idf.py fullclean before build")
    parser.add_argument("--build-only", action="store_true", help="Build only, no flash")
    parser.add_argument("--flash-only", action="store_true", help="Flash only, no build (risky)")
    parser.add_argument("--monitor", action="store_true", help="Open serial monitor after flash")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    parser.add_argument("--dry-run", action="store_true", help="Print commands only")
    parser.add_argument(
        "--skip-idf-bootstrap",
        action="store_true",
        help="Do not run submodule/install; use existing IDF_PATH",
    )
    args = parser.parse_args()

    if not args.dry_run:
        try:
            ensure_idf_ready(
                REPO_ROOT,
                yes=args.yes,
                skip_bootstrap=args.skip_idf_bootstrap,
            )
        except (FileNotFoundError, RuntimeError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

    if args.build_only and args.flash_only:
        print("error: --build-only and --flash-only are mutually exclusive", file=sys.stderr)
        return 2

    if args.flash_only:
        print("warning: --flash-only may flash a binary built for a different device_id", file=sys.stderr)

    try:
        cfg = load_meshsense_config(args.config)
        mac = read_mac(args.port, cwd=str(RX_PROJECT))
        rec = lookup_by_mac(mac, args.registry)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if rec is None:
        print(f"error: MAC {mac} is not in {args.registry}", file=sys.stderr)
        print(
            "  Register the board first, e.g.:\n"
            f"    python scripts/device_registry.py add --port {args.port}\n"
            f"    python scripts/device_registry.py add --mac {mac} --board-name RXn",
            file=sys.stderr,
        )
        return 1

    if not args.yes and not args.dry_run:
        if not confirm_flash(args.port, mac, rec.device_id, rec.board_name, cfg):
            print("aborted.")
            return 0

    defines = cfg.rx_cmake_defines(rec.device_id)

    try:
        if args.clean and not args.flash_only:
            run_idf(["fullclean"], cwd=RX_PROJECT, dry_run=args.dry_run, repo_root=REPO_ROOT)

        if not args.flash_only:
            run_idf(["build", *defines], cwd=RX_PROJECT, dry_run=args.dry_run, repo_root=REPO_ROOT)

        if not args.build_only:
            run_idf(["-p", args.port, "flash"], cwd=RX_PROJECT, dry_run=args.dry_run, repo_root=REPO_ROOT)

        if args.monitor and not args.build_only and not args.dry_run:
            run_idf(["-p", args.port, "monitor"], cwd=RX_PROJECT, dry_run=False, repo_root=REPO_ROOT)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not args.dry_run:
        print(f"[ok] flashed device_id={rec.device_id} ({rec.board_name}) MAC={mac}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
