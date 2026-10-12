#!/usr/bin/env python3
"""녹화 세션을 이어 붙여 **상태가 바뀌는 합성 세션**을 만든다 (화면 시연·전환 실험용).

    python scripts/make_demo_session.py --plan empty motion static motion empty --hold 30
    python scripts/make_demo_session.py --plan empty:120 motion:60 static:180 motion:60 empty:120 \
        --date 20260920            # 구간마다 길이를 다르게, 원본은 그 날짜의 세션들에서 순서대로

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
LABELS = ("empty", "static", "motion")
#: 같은 날·같은 배치에서 고른다 — 다른 배치를 섞으면 경계가 배치 변화까지 담는다
SOURCES = {
    "empty": RAW / "20260919" / "153454_empty_s1",
    "static": RAW / "20260919" / "181915_static_s10",
    "motion": RAW / "20260919" / "163507_motion_s5",
}
EDGE = 3000          # 원본의 앞 30초는 버린다 — 시작 직후 사람이 자리 잡는 구간


def sources_for(date: str | None, skip: set | None = None) -> dict:
    """라벨별 원본 세션 목록. 날짜를 주면 그 날짜의 라벨별 세션 전부(이름 순), 아니면 기존 고정 셋."""
    if not date:
        return {k: [v] for k, v in SOURCES.items()}
    out = {k: [] for k in LABELS}
    for d in sorted((RAW / date).iterdir()):
        if (d / "session.json").is_file() and d.name.split("_")[1] in out \
                and f"{date}_{d.name.rsplit('_s', 1)[1]}" not in (skip or set()):
            out[d.name.split("_")[1]].append(d)
    return out


class Cursor:
    """라벨별로 원본을 앞에서부터 순서대로 떠 온다. 한 세션이 바닥나면 다음 세션으로."""

    def __init__(self, paths: list) -> None:
        self.paths, self.i, self.pos, self.cache = list(paths), 0, EDGE, {}

    def _frames(self):
        p = self.paths[self.i]
        if p not in self.cache:
            self.cache[p] = cs.read_session(p)
        return self.cache[p]

    def take(self, width: int) -> dict:
        while True:
            fr = self._frames()
            tx = fr[RX_ORDER[0]]["hdr"]["tx_seq"].astype(np.int64)
            start = int(tx[0]) + self.pos
            if start + width <= int(tx[-1]) - EDGE:
                self.pos += width
                return {rx: fr[rx][(fr[rx]["hdr"]["tx_seq"].astype(np.int64) >= start)
                                   & (fr[rx]["hdr"]["tx_seq"].astype(np.int64) < start + width)]
                        for rx in RX_ORDER}, self.paths[self.i]
            self.i, self.pos = self.i + 1, EDGE
            if self.i >= len(self.paths):
                raise SystemExit(f"[중단] 원본이 모자란다: {self.paths[0].name.split('_')[1]} 구간 합이 "
                                 f"{len(self.paths)}세션을 넘는다")


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
                    help="이어 붙일 상태 순서. `static:180` 처럼 길이(초)를 붙일 수 있다")
    ap.add_argument("--hold", type=float, default=30.0, help="길이를 안 붙인 구간의 길이 (초)")
    ap.add_argument("--date", default=None, help="원본을 이 날짜의 세션들에서 순서대로 떠 온다")
    ap.add_argument("--skip", nargs="*", default=[], help="쓰지 않을 세션 키 (예: 20260919_12)")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    plan = []
    for item in args.plan:
        label, _, sec = item.partition(":")
        if label not in LABELS:
            ap.error(f"알 수 없는 상태 {label!r}")
        plan.append((label, float(sec) if sec else args.hold))

    name = args.name or f"{time.strftime('%H%M%S')}_demo_{int(args.hold)}s"
    out_dir = DEMO / name
    out_dir.mkdir(parents=True, exist_ok=False)

    cursors = {k: Cursor(v) for k, v in sources_for(args.date, set(args.skip)).items() if v}
    parts = {rx: [] for rx in RX_ORDER}
    truth, tx_base, seq_base, t_base, t_s = [], 100_000, 0, 0, 0.0
    used = set()
    for i, (label, hold) in enumerate(plan):
        width = int(hold * 100)
        if label not in cursors:
            raise SystemExit(f"[중단] {label} 원본이 없다 (날짜 {args.date})")
        keep, src = cursors[label].take(width)
        used.add(str(src.relative_to(ROOT)))
        taken = 0
        for rx in RX_ORDER:
            parts[rx].append(rewrite(keep[rx], tx_base, seq_base, t_base))
            taken = max(taken, len(keep[rx]))
        truth.append({"state": label, "from_s": round(t_s, 1), "to_s": round(t_s + hold, 1),
                      "frames": int(taken), "source": src.name})
        print(f"  {i + 1}. {label:7s} {hold:>5.0f}초  프레임 {taken}  ← {src.name}", flush=True)
        tx_base += width
        seq_base += width
        t_base += width * 10_000
        t_s += hold

    for rx in RX_ORDER:
        (out_dir / f"device_{rx}.csi").write_bytes(
            b"".join(p.tobytes() for p in parts[rx]))
    now = int(time.time() * 1_000_000)
    (out_dir / "session.json").write_text(json.dumps({
        "schema": 1, "pipeline": "synthetic", "frame_version": cs.FRAME_VERSION,
        "session_id": 0, "label": "test",
        "started_at_unix_us": now, "ended_at_unix_us": now + int(t_s * 1e6),
        "devices": [{"device_id": rx, "rx_id": i + 1, "frames": sum(len(p) for p in parts[rx])}
                    for i, rx in enumerate(RX_ORDER)],
        "note": "합성 세션 — 녹화 세션을 이어 붙였다. 수집 데이터가 아니다.",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out_dir / "demo_truth.json").write_text(json.dumps(
        {"plan": [f"{l}:{h:g}" for l, h in plan], "hold_s": args.hold, "segments": truth,
         "date": args.date, "sources": sorted(used)},
        ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    total = t_s
    print(f"\n[생성] {out_dir.relative_to(ROOT)}  총 {total:.0f}초 "
          f"({sum(len(p) for p in parts[101])} 프레임/RX)")
    print(f"  보기: python realtime/ui.py --session {out_dir.relative_to(ROOT)} --speed 2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
