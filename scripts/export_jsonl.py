#!/usr/bin/env python3
"""수집 세션(`.csi`) → JSONL record schema v1 내보내기.

수집은 binary frame v4(`.csi`, raw I/Q 보존)로 하지만, 학습 전처리
(`model_train/preprocessing/preprocess_3rx.py`)는 JSONL record schema v1 을 소비한다.
이 스크립트가 그 사이를 잇는다 — 전처리 코드를 수정하지 않고 새 수집 포맷을 쓸 수 있다.

    python scripts/export_jsonl.py                      # raw/ 전체를 변환
    python scripts/export_jsonl.py --session <세션 디렉터리>
    python scripts/export_jsonl.py --out-root /tmp/jsonl

출력 경로는 전처리가 기대하는 구 레이아웃을 따른다::

    <out-root>/raw/<YYYYMMDD>/session_<session_id>/device_<device_id>.jsonl

라벨은 세션 매니페스트(`session.json`)에 있다. 전처리의 `LABEL_SESSION_RANGES`
(session_id → label 하드코딩)를 갱신할 때 `--print-labels` 출력을 그대로 쓰면 된다.

계약: doc/data-schema.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import csi_store as cs  # noqa: E402
from csi_session import read_manifest  # noqa: E402

RECORD_SCHEMA_VERSION = 1


def export_device(frames: np.ndarray, *, session_id: int, device_id: int,
                  started_at_unix_us: int, out_path: Path) -> int:
    """프레임 배열 → JSONL. 기존 파일은 덮어쓴다 (append 로 여러 run 이 섞이던 문제 회피)."""
    h = frames["hdr"]
    # 진폭은 64개 전부 낸다 — 전처리가 features_per_rx=64 를 쓰고, 유효 톤 선별은
    # 소비자 책임이라는 것이 기존 계약이다 (csi_store.LLTF_DATA_IDX 참고).
    amp = cs.amplitude(frames, valid_only=False)
    # 보드 시계로부터 호스트 수신 시각을 복원한다. 원본 reader 가 기록하던
    # received_at_unix_us 와 같은 의미이되, 프레임 간격은 보드 시계가 더 정확하다.
    t0 = int(h["timestamp_us"][0]) if len(h) else 0
    recv_us = started_at_unix_us + (h["timestamp_us"].astype(np.int64) - t0)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as f:
        for i in range(len(frames)):
            f.write(json.dumps({
                "record_schema_version": RECORD_SCHEMA_VERSION,
                "transport": "usb_serial_jtag",
                "csi_representation": "raw_iq_amplitude",
                "received_at_unix_us": int(recv_us[i]),
                "source_ip": "usb-serial",
                "source_port": 0,
                "session_id": session_id,
                "firmware_session_id": 0,
                "device_id": device_id,
                "seq": int(h["seq"][i]),
                "tx_seq": int(h["tx_seq"][i]),
                "timestamp_us": int(h["timestamp_us"][i]),
                "channel": int(h["channel"][i]),
                "rssi_dbm": int(h["rssi"][i]),
                "noise_floor_dbm": int(h["noise_floor"][i]),
                "rate": int(h["rate"][i]),
                "sig_len": int(h["sig_len"][i]),
                "sample_count": amp.shape[1],
                "csi_amp": [round(v, 6) for v in amp[i].tolist()],
            }, ensure_ascii=False) + "\n")
    return len(frames)


def export_session(session_dir: Path, out_root: Path) -> tuple[int, str, int]:
    m = read_manifest(session_dir)
    session_id, label = int(m["session_id"]), m["label"]
    started = int(m.get("started_at_unix_us") or 0)
    date_dir = session_dir.parent.name
    total = 0
    for dev, frames in cs.read_session(session_dir).items():
        if len(frames) == 0:
            continue
        out = out_root / "raw" / date_dir / f"session_{session_id}" / f"device_{dev}.jsonl"
        total += export_device(frames, session_id=session_id, device_id=dev,
                               started_at_unix_us=started, out_path=out)
    return session_id, label, total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-root", type=Path, default=REPO_ROOT / "mac_collector_output" / "raw")
    ap.add_argument("--session", type=Path, default=None, help="이 세션 하나만 변환")
    ap.add_argument("--out-root", type=Path, default=REPO_ROOT / "mac_collector_output" / "jsonl")
    ap.add_argument("--print-labels", action="store_true",
                    help="LABEL_SESSION_RANGES 에 넣을 session_id 배정을 출력")
    args = ap.parse_args()

    sessions = [args.session] if args.session else cs.find_sessions(args.raw_root)
    if not sessions:
        print(f"error: 세션이 없습니다 ({args.raw_root})")
        return 1

    by_label: dict[str, list[int]] = {}
    labels_by_session: dict[int, str] = {}
    date_dirs: set[Path] = set()
    for d in sessions:
        try:
            sid, label, n = export_session(d, args.out_root)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            print(f"  [건너뜀] {d.name}: {exc}")
            continue
        by_label.setdefault(label, []).append(sid)
        labels_by_session[sid] = label
        date_dirs.add(args.out_root / "raw" / d.parent.name)
        print(f"  {d.name} → session_{sid}  ({label}, {n} records)")

    # 전처리가 읽는 라벨 정본. 세션 manifest 에 박힌 값을 그대로 옮긴다 —
    # session_id 범위를 사람이 손으로 맞추다 라벨이 어긋나는 사고를 없앤다.
    for dd in sorted(date_dirs):
        (dd / "labels.json").write_text(
            json.dumps({str(k): v for k, v in sorted(labels_by_session.items())},
                       ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"  labels.json → {dd}")

    print(f"\n[완료] {args.out_root}")
    if args.print_labels:
        print("\nLABEL_SESSION_RANGES = (")
        for label in cs.LABELS:
            ids = sorted(by_label.get(label, []))
            print(f'    ("{label}", frozenset({ids})),')
        print(")")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
