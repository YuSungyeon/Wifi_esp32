#!/usr/bin/env python3
"""esp32s3_csi_recv_poc 바이너리 CSI 프레임을 USB serial로 읽어 JSONL로 저장.

사용 예:
    python scripts/csi_serial_reader.py \\
        --port /dev/cu.usbmodem101 \\
        --device-id 101 \\
        --output-dir mac_collector_output \\
        --session-id 1

JSONL 스키마는 기존 mac_collector/udp_collector_mvp.py와 호환:
  received_at_unix_us, session_id, device_id, seq, timestamp_us,
  channel, rssi_dbm, noise_floor_dbm, sample_count, csi_amp
출력 경로:
  <output-dir>/raw/<YYYYMMDD>/session_<id>/device_<device-id>.jsonl
"""
from __future__ import annotations

import argparse
import json
import math
import struct
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import serial  # type: ignore[import-not-found]
except ImportError:
    print("pyserial이 필요합니다: pip install pyserial", file=sys.stderr)
    sys.exit(1)


CSI_FRAME_MAGIC = 0x4353  # 'CS' (LE bytes: 0x53 0x43)
# v2 format: 32 bytes header (adds tx_seq u32 at the end)
HEADER_FORMAT = "<HBBHHIQbBbBHHI"
HEADER_SIZE = struct.calcsize(HEADER_FORMAT)  # 32
assert HEADER_SIZE == 32, f"header size mismatch: {HEADER_SIZE}"


@dataclass
class CsiHeader:
    magic: int
    version: int
    reserved0: int
    total_len: int
    raw_len: int
    seq: int
    timestamp_us: int
    rssi: int
    channel: int
    noise_floor: int
    rate: int
    sig_len: int
    reserved1: int
    tx_seq: int  # v2: cross-RX sync key


def parse_header(buf: bytes) -> CsiHeader:
    fields = struct.unpack(HEADER_FORMAT, buf)
    return CsiHeader(*fields)


def compute_amplitudes(raw: bytes) -> list[float]:
    """raw int8 I/Q 페어를 amplitude로 변환. 길이 홀수면 마지막 바이트 무시."""
    n = len(raw) // 2
    amps: list[float] = []
    for i in range(n):
        # 부호 있는 int8
        ipart = struct.unpack_from("b", raw, 2 * i)[0]
        qpart = struct.unpack_from("b", raw, 2 * i + 1)[0]
        amps.append(math.sqrt(ipart * ipart + qpart * qpart))
    return amps


def read_exact(ser: "serial.Serial", n: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        chunk = ser.read(n - len(out))
        if not chunk:
            continue
        out.extend(chunk)
    return bytes(out)


def find_magic(ser: "serial.Serial") -> bytes:
    """Magic byte 두 바이트(0x53 0x43)를 찾을 때까지 읽음. 동기화 복구용."""
    prev = b""
    while True:
        b = ser.read(1)
        if not b:
            continue
        if prev == b"\x53" and b == b"\x43":
            return b"\x53\x43"
        prev = b


def open_output_file(out_dir: Path, session_id: int, device_id: int) -> Path:
    date_dir = time.strftime("%Y%m%d")
    target = out_dir / "raw" / date_dir / f"session_{session_id}" / f"device_{device_id}.jsonl"
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="시리얼 포트 (예: /dev/cu.usbmodem101)")
    p.add_argument("--baud", type=int, default=921600, help="시리얼 baud (RX 펌웨어 default 921600)")
    p.add_argument("--device-id", type=int, required=True, help="이 RX 보드의 device_id")
    p.add_argument("--session-id", type=int, required=True, help="JSONL에 기록할 run session_id")
    p.add_argument("--output-dir", type=Path, default=Path("mac_collector_output"),
                   help="JSONL 출력 베이스 디렉터리")
    p.add_argument("--max-frames", type=int, default=0, help="0이면 무한, 양수면 그만큼만 읽고 종료")
    p.add_argument("--stats-every", type=int, default=500, help="N 프레임마다 stderr에 진행 상태 출력")
    args = p.parse_args()

    print(f"[reader] open serial: {args.port} @ {args.baud}", file=sys.stderr)
    # macOS+ESP32-S3에서 포트 열 때 DTR/RTS toggle이 보드를 reset/bootloader 모드로 빠뜨리는 것 회피.
    ser = serial.Serial()
    ser.port = args.port
    ser.baudrate = args.baud
    ser.timeout = 1
    ser.dtr = False
    ser.rts = False
    ser.open()
    # 일부 플랫폼은 open() 후에야 DTR/RTS를 다시 설정해야 함
    ser.dtr = False
    ser.rts = False

    out_path = open_output_file(args.output_dir, args.session_id, args.device_id)
    print(f"[reader] writing JSONL → {out_path}", file=sys.stderr)
    fp = out_path.open("a", encoding="utf-8")

    count = 0
    invalid = 0
    last_seq: Optional[int] = None
    seq_drop = 0
    start = time.monotonic()
    try:
        while True:
            # magic 찾기
            magic_bytes = find_magic(ser)
            # 헤더 나머지 26바이트
            rest = read_exact(ser, HEADER_SIZE - 2)
            hdr_buf = magic_bytes + rest
            try:
                hdr = parse_header(hdr_buf)
            except struct.error:
                invalid += 1
                continue
            if hdr.magic != CSI_FRAME_MAGIC or hdr.raw_len > 4096:
                invalid += 1
                continue
            # raw 페이로드
            raw = read_exact(ser, hdr.raw_len)

            # seq drop 추정 (boot reset 시 음수 가능)
            if last_seq is not None and hdr.seq > last_seq + 1:
                seq_drop += hdr.seq - (last_seq + 1)
            last_seq = hdr.seq

            amps = compute_amplitudes(raw)
            record = {
                "received_at_unix_us": int(time.time() * 1_000_000),
                "source_ip": "usb-serial",
                "source_port": 0,
                "session_id": args.session_id,
                "firmware_session_id": 0,
                "device_id": args.device_id,
                "seq": hdr.seq,
                "tx_seq": hdr.tx_seq,  # cross-RX 동기화 키
                "timestamp_us": hdr.timestamp_us,
                "channel": hdr.channel,
                "rssi_dbm": hdr.rssi,
                "noise_floor_dbm": hdr.noise_floor,
                "rate": hdr.rate,
                "sig_len": hdr.sig_len,
                "sample_count": len(amps),
                "csi_amp": amps,
            }
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")

            count += 1
            if args.stats_every and count % args.stats_every == 0:
                elapsed = time.monotonic() - start
                hz = count / elapsed if elapsed > 0 else 0
                print(f"[reader dev{args.device_id}] frames={count} invalid={invalid} "
                      f"seq_drop={seq_drop} hz_avg={hz:.1f} "
                      f"last_rssi={hdr.rssi} last_tx_seq={hdr.tx_seq} last_raw_len={hdr.raw_len}",
                      file=sys.stderr)
                fp.flush()
            if args.max_frames and count >= args.max_frames:
                break
    except KeyboardInterrupt:
        print("\n[reader] interrupted", file=sys.stderr)
    finally:
        fp.flush()
        fp.close()
        ser.close()
        elapsed = time.monotonic() - start
        hz = count / elapsed if elapsed > 0 else 0
        print(f"[reader] done. total frames={count} invalid={invalid} seq_drop={seq_drop} "
              f"elapsed={elapsed:.2f}s hz_avg={hz:.1f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
