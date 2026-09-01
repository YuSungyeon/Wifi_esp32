#!/usr/bin/env python3
"""Visualize each RX's own ``tx_seq`` range and existing records."""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np


DEFAULT_RX_IDS = (101, 102, 103)
DEFAULT_OUTPUT_NAME = "tx_seq_overlap.png"
DEFAULT_MAX_COLUMNS = 6000
DEFAULT_MAX_GRID_LENGTH = 2_000_000


@dataclass(frozen=True)
class RecordMeta:
    seq: int
    timestamp_us: int
    tx_seq: int
    received_at_unix_us: int


@dataclass(frozen=True)
class RxSegment:
    device_id: int
    index: int
    records: tuple[RecordMeta, ...]
    duplicate_count: int

    @property
    def start_tx_seq(self) -> int:
        return self.records[0].tx_seq

    @property
    def end_tx_seq(self) -> int:
        return self.records[-1].tx_seq

    @property
    def wall_start_us(self) -> int:
        return self.records[0].received_at_unix_us

    @property
    def wall_end_us(self) -> int:
        return self.records[-1].received_at_unix_us


@dataclass(frozen=True)
class RxTrace:
    device_id: int
    path: Path
    records: tuple[RecordMeta, ...]
    segments: tuple[RxSegment, ...]
    valid_records: int
    rx_resets: int
    tx_resets: int
    order_boundaries: int

def is_rx_boot_boundary(previous: RecordMeta, current: RecordMeta) -> bool:
    """Return true for the RX ``seq`` rollback rule used by preprocessing."""

    went_back = current.seq < previous.seq
    looks_like_restart = current.seq <= 10
    large_drop = previous.seq - current.seq >= 100
    return went_back and (looks_like_restart or large_drop)


def _deduplicate_records(records: Sequence[RecordMeta]) -> tuple[tuple[RecordMeta, ...], int]:
    """Keep the first record for each ``tx_seq`` in one monotonic segment."""

    unique: list[RecordMeta] = []
    seen: set[int] = set()
    duplicates = 0
    for record in records:
        if record.tx_seq in seen:
            duplicates += 1
            continue
        seen.add(record.tx_seq)
        unique.append(record)
    return tuple(unique), duplicates


def split_rx_segments(device_id: int, records: Iterable[RecordMeta]) -> RxTrace:
    """Split file-ordered records at RX reboot, TX reset, or wall-clock rollback."""

    raw_segments: list[list[RecordMeta]] = []
    current_segment: list[RecordMeta] = []
    previous: RecordMeta | None = None
    rx_resets = 0
    tx_resets = 0
    order_boundaries = 0
    valid_records = 0
    all_records: list[RecordMeta] = []

    def finish_segment() -> None:
        nonlocal current_segment
        if current_segment:
            raw_segments.append(current_segment)
            current_segment = []

    for current in records:
        valid_records += 1
        all_records.append(current)
        if previous is None:
            current_segment.append(current)
            previous = current
            continue

        rx_boundary = is_rx_boot_boundary(previous, current)
        tx_boundary = current.tx_seq < previous.tx_seq
        order_boundary = current.received_at_unix_us < previous.received_at_unix_us

        if rx_boundary:
            rx_resets += 1
            tx_resets += int(tx_boundary)
            order_boundaries += int(order_boundary)
            finish_segment()
            # The record that reveals the reboot is excluded by the preprocessing contract.
            previous = current
            continue

        if tx_boundary or order_boundary:
            tx_resets += int(tx_boundary)
            order_boundaries += int(order_boundary)
            finish_segment()

        current_segment.append(current)
        previous = current

    finish_segment()

    segments: list[RxSegment] = []
    for raw_segment in raw_segments:
        unique, duplicate_count = _deduplicate_records(raw_segment)
        if not unique:
            continue
        segments.append(
            RxSegment(
                device_id=device_id,
                index=len(segments),
                records=unique,
                duplicate_count=duplicate_count,
            )
        )

    return RxTrace(
        device_id=device_id,
        path=Path(),
        records=tuple(all_records),
        segments=tuple(segments),
        valid_records=valid_records,
        rx_resets=rx_resets,
        tx_resets=tx_resets,
        order_boundaries=order_boundaries,
    )


def _parse_record(path: Path, line_number: int, line: str, expected_device_id: int) -> RecordMeta:
    try:
        record = json.loads(line)
        device_id = int(record["device_id"])
        parsed = RecordMeta(
            seq=int(record["seq"]),
            timestamp_us=int(record["timestamp_us"]),
            tx_seq=int(record["tx_seq"]),
            received_at_unix_us=int(record["received_at_unix_us"]),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{path}:{line_number}: invalid JSONL record: {exc}") from exc

    if device_id != expected_device_id:
        raise ValueError(
            f"{path}:{line_number}: device_id={device_id}, expected {expected_device_id}"
        )
    if min(parsed.seq, parsed.timestamp_us, parsed.tx_seq, parsed.received_at_unix_us) < 0:
        raise ValueError(f"{path}:{line_number}: sequence and timestamp values must be non-negative")
    return parsed


def load_rx_trace(path: Path, device_id: int) -> RxTrace:
    """Load only sequence/time metadata from one device JSONL file."""

    records: list[RecordMeta] = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            records.append(_parse_record(path, line_number, line, device_id))

    trace = split_rx_segments(device_id, records)
    return RxTrace(
        device_id=trace.device_id,
        path=path,
        records=trace.records,
        segments=trace.segments,
        valid_records=trace.valid_records,
        rx_resets=trace.rx_resets,
        tx_resets=trace.tx_resets,
        order_boundaries=trace.order_boundaries,
    )


def load_session_traces(session_dir: Path, rx_ids: Sequence[int]) -> dict[int, RxTrace]:
    traces: dict[int, RxTrace] = {}
    missing: list[int] = []
    for device_id in rx_ids:
        path = session_dir / f"device_{device_id}.jsonl"
        if not path.is_file():
            missing.append(device_id)
            continue
        traces[device_id] = load_rx_trace(path, device_id)

    if missing:
        joined = ", ".join(str(device_id) for device_id in missing)
        raise FileNotFoundError(f"missing RX JSONL for device_id: {joined}")
    return traces


def compress_mask(mask: np.ndarray, max_columns: int) -> np.ndarray:
    """Aggregate display columns as observation ratios; statistics remain uncompressed."""

    if max_columns <= 0:
        raise ValueError("max_columns must be greater than zero")
    if mask.shape[1] <= max_columns:
        return mask.astype(np.float64)

    block_size = math.ceil(mask.shape[1] / max_columns)
    columns: list[np.ndarray] = []
    for start in range(0, mask.shape[1], block_size):
        columns.append(mask[:, start : start + block_size].mean(axis=1))
    return np.stack(columns, axis=1)


def _blend_rows(
    inside_ratio: np.ndarray,
    observed_ratio: np.ndarray,
    colors: Sequence[tuple[float, float, float]],
    missing_colors: Sequence[tuple[float, float, float]],
) -> np.ndarray:
    """Build opaque RGBA rows from range coverage and observed-record ratios."""

    row_count, column_count = inside_ratio.shape
    rgba = np.ones((row_count, column_count, 4), dtype=np.float64)
    white = np.ones(3, dtype=np.float64)

    for row in range(row_count):
        inside = inside_ratio[row][:, np.newaxis]
        # A display column may represent several tx_seq values after compression.
        # Keep it fully white when even one tx_seq in that column is missing.
        observed_within = (
            (inside_ratio[row] > 0)
            & np.isclose(observed_ratio[row], inside_ratio[row])
        ).astype(np.float64)[:, np.newaxis]
        missing = np.asarray(missing_colors[row], dtype=np.float64)
        observed = np.asarray(colors[row], dtype=np.float64)
        inside_color = missing * (1.0 - observed_within) + observed * observed_within
        rgba[row, :, :3] = white * (1.0 - inside) + inside_color * inside
    return rgba


def _trace_display_data(
    traces: dict[int, RxTrace],
    rx_ids: Sequence[int],
    *,
    max_columns: int,
    max_grid_length: int,
) -> tuple[np.ndarray, int, int, list[tuple[int, int]]]:
    """Build three independent RX rows on one global ``tx_seq`` axis."""

    if any(not traces[device_id].records for device_id in rx_ids):
        empty = [str(device_id) for device_id in rx_ids if not traces[device_id].records]
        raise ValueError("no valid records for RX: " + ", ".join(empty))

    rx_values = {
        device_id: sorted({record.tx_seq for record in traces[device_id].records})
        for device_id in rx_ids
    }
    ranges = [(values[0], values[-1]) for values in rx_values.values()]
    global_start = min(start for start, _ in ranges)
    global_end = max(end for _, end in ranges)
    global_length = global_end - global_start + 1
    if global_length > max_grid_length:
        raise ValueError(
            f"display tx_seq range is too large ({global_length}); "
            f"increase --max-grid-length if this is intentional"
        )

    inside_mask = np.zeros((3, global_length), dtype=bool)
    observed_mask = np.zeros((3, global_length), dtype=bool)
    for row, device_id in enumerate(rx_ids):
        start, end = ranges[row]
        inside_mask[row, start - global_start : end - global_start + 1] = True
        for tx_seq in rx_values[device_id]:
            observed_mask[row, tx_seq - global_start] = True

    inside_ratio = compress_mask(inside_mask, max_columns)
    observed_ratio = compress_mask(observed_mask, max_columns)
    rgba = _blend_rows(
        inside_ratio,
        observed_ratio,
        colors=(
            (0.00, 0.18, 0.55),  # dark blue
            (0.36, 0.00, 0.58),  # dark purple
            (0.00, 0.39, 0.35),  # dark teal
        ),
        missing_colors=(
            (1.00, 1.00, 1.00),
            (1.00, 1.00, 1.00),
            (1.00, 1.00, 1.00),
        ),
    )
    return rgba, global_start, global_end, ranges


def render_overlap_png(
    traces: dict[int, RxTrace],
    rx_ids: Sequence[int],
    output_path: Path,
    *,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_grid_length: int = DEFAULT_MAX_GRID_LENGTH,
) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    output_path.parent.mkdir(parents=True, exist_ok=True)

    from matplotlib.patches import Patch, Rectangle

    rgba, global_start, global_end, ranges = _trace_display_data(
        traces,
        rx_ids,
        max_columns=max_columns,
        max_grid_length=max_grid_length,
    )
    figure, axis = plt.subplots(figsize=(16, 6), dpi=140)
    axis.imshow(
        rgba,
        aspect="auto",
        origin="upper",
        interpolation="nearest",
        extent=(global_start, global_end + 1, 3, 0),
    )

    for row, (start, end) in enumerate(ranges):
        width = end - start + 1
        axis.add_patch(
            Rectangle(
                (start, row),
                width,
                1,
                facecolor="none",
                edgecolor="#111827",
                linewidth=1.2,
            )
        )
        text_padding = max(1, int(width * 0.006))
        for x, label, alignment in (
            (start + text_padding, f"start={start:,}", "left"),
            (end - text_padding, f"end={end:,}", "right"),
        ):
            axis.text(
                x,
                row + 0.5,
                label,
                color="#ffffff",
                fontsize=9,
                fontweight="bold",
                ha=alignment,
                va="center",
                bbox={"facecolor": "#111827", "alpha": 0.68, "edgecolor": "none", "pad": 2},
            )

    axis.set_yticks([0.5, 1.5, 2.5])
    axis.set_yticklabels([f"RX {device_id}" for device_id in rx_ids], fontweight="bold")
    axis.set_ylim(3, 0)
    axis.set_xlabel(
        "tx_seq (unit: 1 transmitted frame ≈ 10 ms at nominal 100 Hz)",
        fontweight="bold",
    )
    axis.set_ylabel("Receiver (device_id)", fontweight="bold")
    axis.set_title("RX tx_seq ranges and existing records", fontsize=16, fontweight="bold")
    axis.grid(axis="x", color="#111827", alpha=0.18, linewidth=0.8)
    axis.legend(
        handles=[
            Patch(facecolor="#002e8a", label="RX 101 tx_seq exists"),
            Patch(facecolor="#5c0094", label="RX 102 tx_seq exists"),
            Patch(facecolor="#00635a", label="RX 103 tx_seq exists"),
            Patch(facecolor="#ffffff", edgecolor="#111827", label="tx_seq missing (white)"),
        ],
        loc="upper center",
        bbox_to_anchor=(0.5, -0.12),
        ncol=4,
        frameon=True,
    )
    figure.tight_layout()

    figure.savefig(output_path, bbox_inches="tight")
    plt.close(figure)
    return output_path


def _print_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def print_summary(
    session_dir: Path,
    traces: dict[int, RxTrace],
    rx_ids: Sequence[int],
) -> None:
    print(f"session: {session_dir}")
    rows: list[list[str]] = []
    for device_id in rx_ids:
        trace = traces[device_id]
        values = sorted({record.tx_seq for record in trace.records})
        start = values[0]
        end = values[-1]
        span = end - start + 1
        observed = len(values)
        max_gap = max((right - left - 1 for left, right in zip(values, values[1:])), default=0)
        rows.append(
            [
                str(device_id),
                str(start),
                str(end),
                str(span),
                str(observed),
                str(span - observed),
                f"{observed / span:.4f}",
                str(max_gap),
                str(trace.rx_resets),
                str(trace.tx_resets),
            ]
        )
    _print_table(
        ["RX", "START", "END", "SPAN", "OBSERVED", "MISSING", "RATIO", "MAX_GAP", "RX_RESETS", "TX_RESETS"],
        rows,
    )


def generate_overlap_visualization(
    session_dir: Path,
    *,
    rx_ids: Sequence[int] = DEFAULT_RX_IDS,
    out_name: str = DEFAULT_OUTPUT_NAME,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_grid_length: int = DEFAULT_MAX_GRID_LENGTH,
) -> tuple[Path, dict[int, RxTrace]]:
    session_dir = session_dir.resolve()
    if len(rx_ids) != 3 or len(set(rx_ids)) != 3:
        raise ValueError("exactly three distinct --rx-ids are required")
    if max_columns <= 0:
        raise ValueError("--max-columns must be greater than zero")
    if max_grid_length <= 0:
        raise ValueError("--max-grid-length must be greater than zero")
    if not out_name or Path(out_name).name != out_name:
        raise ValueError("--out-name must be a file name without directory components")

    traces = load_session_traces(session_dir, rx_ids)
    resolved_output = session_dir / out_name
    rendered = render_overlap_png(
        traces,
        rx_ids,
        resolved_output.resolve(),
        max_columns=max_columns,
        max_grid_length=max_grid_length,
    )
    print_summary(session_dir, traces, rx_ids)
    print(f"\n[viz] output: {rendered}")
    return rendered, traces


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Visualize each RX tx_seq range and existing records on one graph"
    )
    parser.add_argument(
        "--session-dir",
        type=Path,
        required=True,
        help="session_<id> directory containing device_<id>.jsonl",
    )
    parser.add_argument(
        "--rx-ids",
        type=int,
        nargs=3,
        default=DEFAULT_RX_IDS,
        metavar=("RX1", "RX2", "RX3"),
        help="three device IDs in display order (default: 101 102 103)",
    )
    parser.add_argument(
        "--out-name",
        type=str,
        default=DEFAULT_OUTPUT_NAME,
        help=f"PNG file name created inside the session directory (default: {DEFAULT_OUTPUT_NAME})",
    )
    parser.add_argument(
        "--max-columns",
        type=int,
        default=DEFAULT_MAX_COLUMNS,
        help=f"maximum display columns after aggregation (default: {DEFAULT_MAX_COLUMNS})",
    )
    parser.add_argument(
        "--max-grid-length",
        type=int,
        default=DEFAULT_MAX_GRID_LENGTH,
        help=f"safety limit for the displayed tx_seq grid (default: {DEFAULT_MAX_GRID_LENGTH})",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.session_dir.is_dir():
        print(f"error: not a directory: {args.session_dir}", file=sys.stderr)
        return 2

    try:
        generate_overlap_visualization(
            args.session_dir,
            rx_ids=tuple(args.rx_ids),
            out_name=args.out_name,
            max_columns=args.max_columns,
            max_grid_length=args.max_grid_length,
        )
    except (FileNotFoundError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
