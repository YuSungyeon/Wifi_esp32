import argparse
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass
from statistics import mean
from typing import Deque, Dict, List, Optional, Tuple


MAGIC = 0x4353
VERSION = 1
HEADER_LEN = 40
PAYLOAD_TYPE_CSI_AMP = 1
HEADER_STRUCT = struct.Struct("<HBBBBHIIIQbbbBHHI")


@dataclass
class PacketHeader:
    magic: int
    version: int
    header_len: int
    payload_type: int
    flags: int
    reserved0: int
    session_id: int
    device_id: int
    seq: int
    timestamp_us: int
    channel: int
    rssi_dbm: int
    noise_floor_dbm: int
    reserved1: int
    sample_count: int
    reserved2: int
    crc32: int


class DeviceState:
    def __init__(self, baseline_frames: int, score_window: int) -> None:
        self.baseline_frames = baseline_frames
        self.score_window = score_window
        self.energy_hist: Deque[float] = deque(maxlen=baseline_frames)
        self.motion_scores: Deque[float] = deque(maxlen=score_window)
        self.last_seq: Optional[int] = None
        self.drop = 0

    def update_seq(self, seq: int) -> None:
        if self.last_seq is not None and seq > self.last_seq + 1:
            self.drop += seq - (self.last_seq + 1)
        self.last_seq = seq

    def push(self, energy: float) -> None:
        baseline = self.current_baseline()
        score = abs(energy - baseline)
        self.motion_scores.append(score)
        self.energy_hist.append(energy)

    def current_baseline(self) -> float:
        if not self.energy_hist:
            return 0.0
        return mean(self.energy_hist)

    def smoothed_score(self) -> float:
        if not self.motion_scores:
            return 0.0
        return mean(self.motion_scores)


def parse_header(packet: bytes) -> Optional[PacketHeader]:
    if len(packet) < HEADER_STRUCT.size:
        return None
    return PacketHeader(*HEADER_STRUCT.unpack_from(packet, 0))


def validate_packet(header: PacketHeader, packet_len: int) -> bool:
    if header.magic != MAGIC:
        return False
    if header.version != VERSION:
        return False
    if header.header_len != HEADER_LEN:
        return False
    if header.payload_type != PAYLOAD_TYPE_CSI_AMP:
        return False
    if header.sample_count <= 0:
        return False
    expected = header.header_len + header.sample_count * 4
    return packet_len == expected


def parse_amp(packet: bytes, sample_count: int, offset: int) -> List[float]:
    fmt = "<" + ("f" * sample_count)
    return list(struct.unpack_from(fmt, packet, offset))


def frame_energy(csi_amp: List[float]) -> float:
    if not csi_amp:
        return 0.0
    return sum(v * v for v in csi_amp) / len(csi_amp)


def decide_binary_label(score: float, threshold: float) -> int:
    return 1 if score >= threshold else 0


def run(args: argparse.Namespace) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.host, args.port))
    sock.settimeout(1.0)
    print(f"[realtime] listen udp://{args.host}:{args.port}")
    print(f"[realtime] binary classes: 0=static, 1=motion")

    devices: Dict[int, DeviceState] = {}
    invalid_packets = 0
    last_print = time.time()

    while True:
        try:
            packet, _addr = sock.recvfrom(4096)
        except socket.timeout:
            packet = None
        except KeyboardInterrupt:
            print("\n[realtime] stopped by user")
            break

        now = time.time()
        if now - last_print >= args.print_every_sec:
            print("\n[realtime] current status")
            print(f"- invalid_packets: {invalid_packets}")
            for device_id in sorted(devices):
                st = devices[device_id]
                score = st.smoothed_score()
                label = decide_binary_label(score, args.motion_threshold)
                total = (st.last_seq + 1) if st.last_seq is not None else 0
                drop_rate = (st.drop / total * 100.0) if total > 0 else 0.0
                print(
                    f"- device={device_id} label={label} score={score:.4f} "
                    f"baseline={st.current_baseline():.4f} drop_rate={drop_rate:.2f}%"
                )
            last_print = now

        if packet is None:
            continue

        header = parse_header(packet)
        if header is None or not validate_packet(header, len(packet)):
            invalid_packets += 1
            continue

        amp = parse_amp(packet, header.sample_count, header.header_len)
        energy = frame_energy(amp)

        st = devices.get(header.device_id)
        if st is None:
            st = DeviceState(args.baseline_frames, args.score_window)
            devices[header.device_id] = st

        st.update_seq(header.seq)
        st.push(energy)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Realtime binary posture MVP (rule-based).")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=9999)
    p.add_argument("--baseline-frames", type=int, default=100)
    p.add_argument("--score-window", type=int, default=20)
    p.add_argument("--motion-threshold", type=float, default=0.02)
    p.add_argument("--print-every-sec", type=int, default=1)
    return p.parse_args()


if __name__ == "__main__":
    run(parse_args())
