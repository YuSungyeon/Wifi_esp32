import argparse
import csv
import re
from pathlib import Path
from typing import Dict, List, Set, Tuple


DEVICE_FILE_RE = re.compile(r"^device_(\d+)\.jsonl$")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Build labels.csv from session timeline markers for all devices."
    )
    p.add_argument("--markers-csv", type=Path, required=True)
    p.add_argument("--raw-root", type=Path, default=Path("mac_collector_output/raw"))
    p.add_argument("--output-csv", type=Path, default=Path("data_tools/labels.csv"))
    return p.parse_args()


def load_markers(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"markers csv not found: {path}")
    out: List[Dict[str, str]] = []
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        required = {"session_id", "start_us", "end_us", "label"}
        if not required.issubset(set(reader.fieldnames or [])):
            raise ValueError("markers csv must include: session_id,start_us,end_us,label")
        for row in reader:
            out.append(row)
    return out


def discover_session_devices(raw_root: Path) -> Dict[int, Set[int]]:
    by_session: Dict[int, Set[int]] = {}
    for session_dir in raw_root.glob("**/session_*"):
        if not session_dir.is_dir():
            continue
        suffix = session_dir.name.split("session_")[-1]
        if not suffix.isdigit():
            continue
        session_id = int(suffix)
        for child in session_dir.iterdir():
            m = DEVICE_FILE_RE.match(child.name)
            if not m:
                continue
            device_id = int(m.group(1))
            by_session.setdefault(session_id, set()).add(device_id)
    return by_session


def build_rows(markers: List[Dict[str, str]], session_devices: Dict[int, Set[int]]) -> List[Tuple[int, int, int, int, int]]:
    rows: List[Tuple[int, int, int, int, int]] = []
    for mk in markers:
        session_id = int(mk["session_id"])
        start_us = int(mk["start_us"])
        end_us = int(mk["end_us"])
        label = int(mk["label"])
        if end_us < start_us:
            raise ValueError(f"end_us < start_us for session {session_id}")
        devices = sorted(session_devices.get(session_id, []))
        if not devices:
            raise ValueError(
                f"No devices discovered for session {session_id}. "
                f"Check raw root path: {session_devices}"
            )
        for device_id in devices:
            rows.append((session_id, device_id, start_us, end_us, label))
    return rows


def write_labels(path: Path, rows: List[Tuple[int, int, int, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["session_id", "device_id", "start_us", "end_us", "label"])
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    markers = load_markers(args.markers_csv)
    session_devices = discover_session_devices(args.raw_root)
    rows = build_rows(markers, session_devices)
    write_labels(args.output_csv, rows)
    print("[markers_to_labels] done")
    print(f"- markers: {len(markers)}")
    print(f"- label rows: {len(rows)}")
    print(f"- output: {args.output_csv}")


if __name__ == "__main__":
    main()
