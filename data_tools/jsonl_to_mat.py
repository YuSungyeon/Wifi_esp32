import argparse
import csv
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import scipy.io as sio


@dataclass
class Record:
    timestamp_us: int
    csi_amp: List[float]
    session_id: int
    device_id: int


@dataclass
class LabeledStream:
    values: np.ndarray  # [T, C]
    labels: np.ndarray  # [T]
    session_id: int
    device_id: int
    subject_id: Optional[str]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Convert raw CSI JSONL into train/test MAT files.")
    p.add_argument("--raw-root", type=Path, default=Path("mac_collector_output/raw"))
    p.add_argument("--output-dir", type=Path, default=Path("data"))
    p.add_argument("--window-size", type=int, default=192)
    p.add_argument("--stride", type=int, default=96)
    p.add_argument(
        "--labels-csv",
        type=Path,
        default=None,
        help="Optional label intervals CSV: session_id,device_id,start_us,end_us,label",
    )
    p.add_argument(
        "--session-subject-csv",
        type=Path,
        default=None,
        help="Optional mapping CSV: session_id,subject_id",
    )
    p.add_argument(
        "--test-subject-ids",
        type=str,
        default="",
        help='Comma-separated subject IDs for test split, e.g. "S03,S05"',
    )
    p.add_argument("--test-ratio", type=float, default=0.2)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def load_jsonl_records(path: Path) -> List[Record]:
    out: List[Record] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            out.append(
                Record(
                    timestamp_us=int(d["timestamp_us"]),
                    csi_amp=[float(x) for x in d["csi_amp"]],
                    session_id=int(d["session_id"]),
                    device_id=int(d["device_id"]),
                )
            )
    out.sort(key=lambda x: x.timestamp_us)
    return out


def load_label_intervals(path: Optional[Path]) -> Dict[Tuple[int, int], List[Tuple[int, int, int]]]:
    by_key: Dict[Tuple[int, int], List[Tuple[int, int, int]]] = {}
    if path is None or not path.exists():
        return by_key
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = (int(row["session_id"]), int(row["device_id"]))
            start_us = int(row["start_us"])
            end_us = int(row["end_us"])
            label = int(row["label"])
            by_key.setdefault(key, []).append((start_us, end_us, label))
    return by_key


def load_session_subject_map(path: Optional[Path]) -> Dict[int, str]:
    mp: Dict[int, str] = {}
    if path is None or not path.exists():
        return mp
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            mp[int(row["session_id"])] = row["subject_id"].strip()
    return mp


def build_labels(
    timestamps: np.ndarray,
    intervals: List[Tuple[int, int, int]],
) -> np.ndarray:
    labels = np.zeros((timestamps.shape[0],), dtype=np.int64)
    for start_us, end_us, label in intervals:
        mask = (timestamps >= start_us) & (timestamps <= end_us)
        labels[mask] = label
    return labels


def collect_streams(
    raw_root: Path,
    label_map: Dict[Tuple[int, int], List[Tuple[int, int, int]]],
    session_subject_map: Dict[int, str],
) -> List[LabeledStream]:
    streams: List[LabeledStream] = []
    jsonl_files = sorted(raw_root.glob("**/device_*.jsonl"))
    for path in jsonl_files:
        records = load_jsonl_records(path)
        if not records:
            continue

        csi_len = len(records[0].csi_amp)
        for r in records:
            if len(r.csi_amp) != csi_len:
                raise ValueError(f"Inconsistent csi_amp length in {path}")

        timestamps = np.array([r.timestamp_us for r in records], dtype=np.int64)
        values = np.array([r.csi_amp for r in records], dtype=np.float32)  # [T, C]
        session_id = records[0].session_id
        device_id = records[0].device_id
        intervals = label_map.get((session_id, device_id), [])
        labels = build_labels(timestamps, intervals)
        subject_id = session_subject_map.get(session_id)

        streams.append(
            LabeledStream(
                values=values,
                labels=labels,
                session_id=session_id,
                device_id=device_id,
                subject_id=subject_id,
            )
        )
    return streams


def window_stream(
    stream: LabeledStream,
    window_size: int,
    stride: int,
) -> Tuple[List[np.ndarray], List[np.ndarray], List[np.ndarray]]:
    x_windows: List[np.ndarray] = []
    label_instance: List[np.ndarray] = []
    label_time: List[np.ndarray] = []

    t_len = stream.values.shape[0]
    if t_len < window_size:
        return x_windows, label_instance, label_time

    for start in range(0, t_len - window_size + 1, stride):
        end = start + window_size
        x = stream.values[start:end, :]  # [W, C]
        y = stream.labels[start:end]  # [W]
        nonzero = np.where(y > 0)[0]
        if nonzero.size > 0:
            y_time = np.array([int(nonzero[0]), int(nonzero[-1])], dtype=np.int64)
        else:
            y_time = np.array([0, 0], dtype=np.int64)

        # train.py expects [N, 52, 192]
        x_windows.append(x.T.astype(np.float32))
        label_instance.append(y.astype(np.int64))
        label_time.append(y_time)

    return x_windows, label_instance, label_time


def split_streams(
    streams: List[LabeledStream],
    test_subject_ids: List[str],
    test_ratio: float,
    seed: int,
) -> Tuple[List[LabeledStream], List[LabeledStream]]:
    if test_subject_ids:
        train_streams = [s for s in streams if s.subject_id not in test_subject_ids]
        test_streams = [s for s in streams if s.subject_id in test_subject_ids]
        return train_streams, test_streams

    idx = list(range(len(streams)))
    random.Random(seed).shuffle(idx)
    n_test = max(1, int(len(idx) * test_ratio)) if len(idx) > 1 else 0
    test_idx = set(idx[:n_test])
    train_streams, test_streams = [], []
    for i, s in enumerate(streams):
        if i in test_idx:
            test_streams.append(s)
        else:
            train_streams.append(s)
    return train_streams, test_streams


def build_dataset(
    streams: List[LabeledStream],
    window_size: int,
    stride: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    xs: List[np.ndarray] = []
    y_inst: List[np.ndarray] = []
    y_mask: List[np.ndarray] = []
    y_time: List[np.ndarray] = []

    for s in streams:
        xw, yi, yt = window_stream(s, window_size, stride)
        for x, y, t in zip(xw, yi, yt):
            xs.append(x)
            y_inst.append(y)
            y_mask.append((y > 0).astype(np.int64))
            y_time.append(t)

    if not xs:
        return (
            np.zeros((0, 52, window_size), dtype=np.float32),
            np.zeros((0, window_size), dtype=np.int64),
            np.zeros((0, window_size), dtype=np.int64),
            np.zeros((0, 2), dtype=np.int64),
        )

    x_arr = np.stack(xs, axis=0).astype(np.float32)
    yi_arr = np.stack(y_inst, axis=0).astype(np.int64)
    ym_arr = np.stack(y_mask, axis=0).astype(np.int64)
    yt_arr = np.stack(y_time, axis=0).astype(np.int64)
    return x_arr, yi_arr, ym_arr, yt_arr


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    label_map = load_label_intervals(args.labels_csv)
    session_subject_map = load_session_subject_map(args.session_subject_csv)
    streams = collect_streams(args.raw_root, label_map, session_subject_map)
    if not streams:
        raise RuntimeError(f"No JSONL streams found under {args.raw_root}")

    test_subject_ids = [x.strip() for x in args.test_subject_ids.split(",") if x.strip()]
    train_streams, test_streams = split_streams(
        streams=streams,
        test_subject_ids=test_subject_ids,
        test_ratio=args.test_ratio,
        seed=args.seed,
    )

    train_x, train_yi, train_ym, train_yt = build_dataset(train_streams, args.window_size, args.stride)
    test_x, test_yi, test_ym, test_yt = build_dataset(test_streams, args.window_size, args.stride)

    train_mat_path = args.output_dir / "train_data.mat"
    test_mat_path = args.output_dir / "test_data.mat"

    sio.savemat(
        str(train_mat_path),
        {
            "train_data_amp": train_x,
            "train_label_instance": train_yi,
            "train_label_mask": train_ym,
            "train_label_time": train_yt,
        },
    )
    sio.savemat(
        str(test_mat_path),
        {
            "test_data_amp": test_x,
            "test_label_instance": test_yi,
            "test_label_mask": test_ym,
            "test_label_time": test_yt,
        },
    )

    print("[jsonl_to_mat] done")
    print(f"- train streams: {len(train_streams)}, windows: {len(train_x)}")
    print(f"- test streams:  {len(test_streams)}, windows: {len(test_x)}")
    print(f"- train mat: {train_mat_path}")
    print(f"- test mat:  {test_mat_path}")


if __name__ == "__main__":
    main()
