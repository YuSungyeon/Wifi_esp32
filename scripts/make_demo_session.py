#!/usr/bin/env python3
"""녹화 세션을 이어 붙여 **상태가 바뀌는 합성 세션**을 만든다 (화면 시연·전환 실험용).

    python scripts/make_demo_session.py --plan empty motion static motion empty --hold 30

진짜 `.csi` 프레임으로 쓰므로 실시간 엔진·제어판이 수집 세션과 똑같이 읽는다. 결과는
`mac_collector_output/demo/<이름>/` 에 들어가며 **실험 데이터(raw/)는 건드리지 않는다.**

**합성이라는 점을 잊으면 안 된다.** 이어 붙인 경계는 사람이 실제로 움직여 생긴 변화가 아니라
불연속이다. 실제 반응 지연은 대본대로 상태를 바꾼 전환 세션을 수집해야 확정된다.
정답 구간은 `demo_truth.json` 에 남긴다.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import csi_store as cs  # noqa: E402

RAW = ROOT / "mac_collector_output" / "raw"
DEMO = ROOT / "mac_collector_output" / "demo"
RX_ORDER = (101, 102, 103)
#: 같은 날·같은 배치에서 고른다 — 다른 배치를 섞으면 경계가 배치 변화까지 담는다
SOURCES = {
    "empty": RAW / "20260919" / "153454_empty_s1",
    "static": RAW / "20260919" / "181915_static_s10",
    "motion": RAW / "20260919" / "163507_motion_s5",
}


def rewrite(frames: np.ndarray, tx_base: int, seq_base: int, t_base: int) -> np.ndarray:
    """tx_seq·seq·timestamp 를 새 시간축으로 옮기고 CRC 를 다시 계산한다."""
    out = frames.copy()
    first = int(out["hdr"]["tx_seq"][0])
    out["hdr"]["tx_seq"] = out["hdr"]["tx_seq"].astype(np.int64) - first + tx_base
    out["hdr"]["seq"] = np.arange(len(out), dtype=np.int64) + seq_base
    out["hdr"]["timestamp_us"] = (out["hdr"]["tx_seq"].astype(np.int64) - tx_base) * 10_000 + t_base
    out["hdr"]["boot_id"] = 1
    for i in range(len(out)):
        raw = out[i].tobytes()
        crc = cs.crc32_of(raw)
        buf = bytearray(raw)
        buf[cs._CRC_OFFSET:cs._CRC_OFFSET + 4] = int(crc).to_bytes(4, "little")
        out[i] = np.frombuffer(bytes(buf), dtype=cs.CSI_FRAME_DTYPE)[0]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--plan", nargs="+", default=["empty", "motion", "static", "motion", "empty"],
                    choices=tuple(SOURCES), help="이어 붙일 상태 순서")
    ap.add_argument("--hold", type=float, default=30.0, help="구간 하나의 길이 (초)")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    name = args.name or f"{time.strftime('%H%M%S')}_demo_{int(args.hold)}s"
    out_dir = DEMO / name
    out_dir.mkdir(parents=True, exist_ok=False)
    width = int(args.hold * 100)

    sources = {label: cs.read_session(path) for label, path in SOURCES.items()}
    parts = {rx: [] for rx in RX_ORDER}
    truth, tx_base, seq_base, t_base = [], 100_000, 0, 0
    for i, label in enumerate(args.plan):
        frames = sources[label]
        # 구간마다 원본의 다른 지점에서 떠온다 — 같은 구간을 반복해 쓰면 주기가 생긴다
        start = int(frames[RX_ORDER[0]]["hdr"]["tx_seq"][0]) + 3000 + i * width
        taken = 0
        for rx in RX_ORDER:
            tx = frames[rx]["hdr"]["tx_seq"].astype(np.int64)
            keep = frames[rx][(tx >= start) & (tx < start + width)]
            parts[rx].append(rewrite(keep, tx_base, seq_base, t_base))
            taken = max(taken, len(keep))
        truth.append({"state": label, "from_s": round(i * args.hold, 1),
                      "to_s": round((i + 1) * args.hold, 1), "frames": int(taken)})
        print(f"  {i + 1}. {label:7s} {args.hold:>5.0f}초  프레임 {taken}", flush=True)
        tx_base += width
        seq_base += width
        t_base += width * 10_000

    for rx in RX_ORDER:
        (out_dir / f"device_{rx}.csi").write_bytes(
            b"".join(p.tobytes() for p in parts[rx]))
    now = int(time.time() * 1_000_000)
    (out_dir / "session.json").write_text(json.dumps({
        "schema": 1, "pipeline": "synthetic", "frame_version": cs.FRAME_VERSION,
        "session_id": 0, "label": "test",
        "started_at_unix_us": now, "ended_at_unix_us": now + int(len(args.plan) * args.hold * 1e6),
        "devices": [{"device_id": rx, "rx_id": i + 1, "frames": sum(len(p) for p in parts[rx])}
                    for i, rx in enumerate(RX_ORDER)],
        "note": "합성 세션 — 녹화 세션을 이어 붙였다. 수집 데이터가 아니다.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "demo_truth.json").write_text(json.dumps(
        {"plan": args.plan, "hold_s": args.hold, "segments": truth,
         "sources": {k: str(v.relative_to(ROOT)) for k, v in SOURCES.items()}},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total = len(args.plan) * args.hold
    print(f"\n[생성] {out_dir.relative_to(ROOT)}  총 {total:.0f}초 "
          f"({sum(len(p) for p in parts[101])} 프레임/RX)")
    print(f"  보기: python realtime/ui.py --session {out_dir.relative_to(ROOT)} --speed 2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
