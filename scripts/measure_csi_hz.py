#!/usr/bin/env python3
"""JSONL에서 마지막 RX 부팅 이후의 CSI 수집률을 계산한다."""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def analyze_jsonl(path: Path, gap_ms: float = 200.0) -> dict:
    """마지막 재부팅 이후 ``seq``와 ``timestamp_us``만 분석한다."""

    if gap_ms <= 0:
        raise ValueError("gap_ms must be greater than zero")

    seq: list[int] = []
    timestamps_us: list[int] = []
    resets = 0

    # JSONL을 한 줄씩 읽으므로 큰 csi_amp 배열을 파일 전체 단위로 쌓지 않는다.
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                current_seq = int(record["seq"]) # RX가 CSI record에 붙인 순번
                current_timestamp_us = int(record["timestamp_us"]) # RX가 CSI를 받은 시각

            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL record: {exc}") from exc

            # seq와 시각이 함께 작아지면 RX 재부팅으로 본다.
            if (
                seq
                and current_seq < seq[-1]
                and current_timestamp_us < timestamps_us[-1]
            ):
                resets += 1
                # 이전 부팅 데이터는 버리고, 현재 record를 새 시작점으로 남긴다.
                seq = [current_seq]
                timestamps_us = [current_timestamp_us]
                # 현재 record는 이미 배열에 넣었으므로 아래 append를 건너뛴다.
                continue

            seq.append(current_seq)
            timestamps_us.append(current_timestamp_us)

    # 마지막 재부팅 이후 데이터에 대한 기본 결과값이다.
    result = {
        "path": path.name,
        "resets": resets,
        "records": len(seq),
        "rx_hz": None,
        "rx_seq_hz": None,
        "median_dt_ms": None,
        "gaps": 0,
        "seq_gap": 0,
        "duplicates": 0,
        "anomalies": 0,
    }
    if len(seq) < 2:
        # record가 하나면 두 record 사이의 시간 간격을 계산할 수 없다.
        return result

    # 바로 앞 record와의 RX 수신 시간 간격(ms), sequence 차이다.
    dt_ms = [
        (timestamps_us[index] - timestamps_us[index - 1]) / 1_000.0
        for index in range(1, len(timestamps_us))
    ]
    seq_delta = [seq[index] - seq[index - 1] for index in range(1, len(seq))]

    # 시각이 거꾸로 간 값은 일반적인 수신 간격 통계에서 제외한다.
    positive_dt_ms = [delta for delta in dt_ms if delta >= 0]
    # 첫 record부터 마지막 record까지 RX에서 흐른 시간이다.
    duration_s = (timestamps_us[-1] - timestamps_us[0]) / 1_000_000.0
    seq_range = seq[-1] - seq[0]

    result.update(
        {
            # JSONL에 실제 저장된 record의 초당 개수다.
            "rx_hz": (len(seq) - 1) / duration_s if duration_s > 0 else None,
            # sequence 범위로 빠진 record까지 포함해 추정한 초당 개수다.
            "rx_seq_hz": seq_range / duration_s
            if duration_s > 0 and seq_range >= 0
            else None,

            # 보통 수신 간격과 긴 수신 공백 수다.
            "median_dt_ms": statistics.median(positive_dt_ms)
            if positive_dt_ms
            else None,

            "gaps": sum(delta > gap_ms for delta in positive_dt_ms),
            # sequence 사이의 누락, 중복, 순서 이상 수다.
            "seq_gap": sum(delta - 1 for delta in seq_delta if delta > 1),
            "duplicates": sum(delta == 0 for delta in seq_delta),
            "anomalies": sum(
                (seq_delta[index] < 0) != (dt_ms[index] < 0)
                for index in range(len(seq_delta))
            ),
        }
    )
    return result


def number(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.2f}"


def result_headers() -> list[str]:
    return [
        "FILE",
        "RESETS",
        "RECORDS",
        "RX_HZ",
        "RX_SEQ_HZ",
        "DT_MS",
        "GAPS",
        "SEQ_GAP",
        "DUP",
        "ANOM",
    ]


def result_row(result: dict) -> list[str]:
    return [
        result["path"],
        str(result["resets"]),
        str(result["records"]),
        number(result["rx_hz"]),
        number(result["rx_seq_hz"]),
        number(result["median_dt_ms"]),
        str(result["gaps"]),
        str(result["seq_gap"]),
        str(result["duplicates"]),
        str(result["anomalies"]),
    ]


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [
        max(len(header), *(len(row[index]) for row in rows))
        for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def analyze_csi(path: Path, gap_ms: float = 200.0) -> dict:
    """`.csi` 세션 파일을 analyze_jsonl 과 같은 결과 dict 로 분석한다.

    수집 포맷이 binary frame v4(`.csi`)로 바뀌었지만 분석 로직은 그대로 쓴다 —
    JSONL 은 `export_jsonl.py` 가 만드는 파생물이고, 정본은 `.csi` 다.
    `.csi` 에서만 알 수 있는 것(CRC 실패, boot_id 변화, tx_seq 역행·커버리지)을 덧붙인다.
    """
    import numpy as np

    frames = _cs().read_device_file(path)
    if len(frames) == 0:
        raise ValueError(f"{path}: 프레임 없음")
    h = frames["hdr"]

    # boot_id 가 바뀌면 재부팅 — 마지막 부팅 구간만 본다 (analyze_jsonl 과 같은 규칙)
    boot = h["boot_id"].astype(np.int64)
    changes = np.nonzero(np.diff(boot))[0]
    resets = int(len(changes))
    start = int(changes[-1]) + 1 if resets else 0
    h = h[start:]

    seq = h["seq"].astype(np.int64)
    ts = h["timestamp_us"].astype(np.int64)
    tx = h["tx_seq"].astype(np.int64)

    dseq = np.diff(seq)
    duplicates = int((dseq == 0).sum())
    anomalies = int((dseq < 0).sum())
    seq_gap = int(dseq[dseq > 1].sum() - (dseq > 1).sum())

    span_us = int(ts[-1] - ts[0])
    n = len(h)
    dt_ms = np.diff(ts) / 1e3
    tx_back = int((np.diff(tx) < 0).sum())
    tx_span = int(tx[-1] - tx[0] + 1)

    return {
        "path": path.name,
        "records": n,
        "resets": resets,
        "duplicates": duplicates,
        "anomalies": anomalies,
        "seq_gap": seq_gap,
        "rx_hz": (n / (span_us / 1e6)) if span_us > 0 else None,
        "rx_seq_hz": ((seq[-1] - seq[0] + 1) / (span_us / 1e6)) if span_us > 0 else None,
        "median_dt_ms": float(np.median(dt_ms)) if len(dt_ms) else None,
        "gaps": int((dt_ms > gap_ms).sum()),
        # `.csi` 전용 지표
        "crc_frames": n,
        "tx_back": tx_back,
        "tx_cov": (len(np.unique(tx)) / tx_span) if tx_span > 0 and not tx_back else 0.0,
        "rssi_med": float(np.median(h["rssi"])),
        "agc_levels": int(len(np.unique(h["agc_gain"]))),
    }


def _cs():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import csi_store
    return csi_store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Measure CSI rate after the latest RX reboot using timestamp_us"
    )
    parser.add_argument("session_dir", type=Path,
                        help="세션 디렉터리 (device_*.csi 또는 구 device_*.jsonl)")
    parser.add_argument(
        "--gap-ms",
        type=float,
        default=200.0,
        help="interval threshold counted as a large gap (default: 200)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.session_dir.is_dir():
        print(f"error: not a directory: {args.session_dir}")
        return 1
    if args.gap_ms <= 0:
        print("error: --gap-ms must be greater than zero")
        return 1

    csi_files = sorted(args.session_dir.glob("device_*.csi"))
    files = csi_files or sorted(args.session_dir.glob("device_*.jsonl"))
    if not files:
        print(f"error: no device_*.csi / device_*.jsonl in {args.session_dir}")
        return 1
    analyze = analyze_csi if csi_files else analyze_jsonl

    manifest = args.session_dir / "session.json"
    results: list[dict] = []
    rows: list[list[str]] = []
    failed = False
    for path in files:
        try:
            r = analyze(path, args.gap_ms)
            results.append(r)
            rows.append(result_row(r))
        except (OSError, ValueError) as exc:
            print(f"error: {exc}")
            failed = True

    if rows:
        print(f"session: {args.session_dir}")
        if manifest.is_file():
            m = json.loads(manifest.read_text(encoding="utf-8"))
            print(f"label: {m.get('label')}  session_id: {m.get('session_id')}  "
                  f"frame_v{m.get('frame_version')}")
        print(f"range: after latest RX reboot | large gap: >{args.gap_ms:g}ms\n")
        print_table(result_headers(), rows)
        print("\nRX_HZ=stored records, RX_SEQ_HZ=sequence estimate, ANOM=ordering anomaly")
        if csi_files:
            for r in results:
                print(f"  {r['path']}: tx_back={r['tx_back']} tx_cov={r['tx_cov']:.3f} "
                      f"rssi_med={r['rssi_med']:.0f} agc_levels={r['agc_levels']}"
                      + ("   [경고] TX 재부팅 — 시간 격자가 깨져 학습에 쓸 수 없다"
                         if r["tx_back"] else ""))
            if len(csi_files) > 1:
                import numpy as np
                sets = [set(_cs().read_device_file(p)["hdr"]["tx_seq"].tolist()) for p in csi_files]
                common, smallest = set.intersection(*sets), min(len(x) for x in sets)
                print(f"  cross-RX 공통 tx_seq: {len(common)} / 최소 RX {smallest} "
                      f"= {100.0 * len(common) / smallest:.1f}%")
        if any("N/A" in row for row in rows):
            print("warning: N/A means too few records or a non-increasing time range")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
