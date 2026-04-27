import argparse
import csv
import json
import shutil
import signal
import socket
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# 전송 스키마 상수:
# ESP 송신 코드의 헤더 정의와 "완전히 동일"해야 파싱이 맞습니다.
MAGIC = 0x4353
VERSION = 1
HEADER_LEN = 40
PAYLOAD_TYPE_CSI_AMP = 1
NOISE_FLOOR_UNKNOWN = -128

# little-endian
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


class DeviceStats:
    def __init__(self) -> None:
        self.packets = 0
        self.samples = 0
        self.dropped_packets = 0
        self.last_seq: Optional[int] = None
        self.last_seen_unix_us: Optional[int] = None

    def update_seq(self, seq: int) -> None:
        # 장치별 seq 공백으로 유실 패킷 수를 추정합니다.
        # 예: last=10, current=13 이면 11/12가 유실된 것으로 계산.
        if self.last_seq is not None and seq > self.last_seq + 1:
            self.dropped_packets += seq - (self.last_seq + 1)
        self.last_seq = seq


def now_us() -> int:
    return int(time.time() * 1_000_000)


def parse_header(packet: bytes) -> Optional[PacketHeader]:
    # 고정 길이 헤더를 먼저 파싱합니다.
    # 길이가 부족하면 즉시 None 반환(불완전 패킷 보호).
    if len(packet) < HEADER_STRUCT.size:
        return None

    values = HEADER_STRUCT.unpack_from(packet, 0)
    header = PacketHeader(*values)
    return header


def validate_packet(header: PacketHeader, packet_len: int) -> Tuple[bool, str]:
    # 스키마 무결성 검증:
    # magic/version/header_len/payload_type/sample_count/전체 길이를 확인해
    # 손상된 패킷을 디스크 저장 전에 빠르게 제거합니다.
    if header.magic != MAGIC:
        return False, "invalid_magic"
    if header.version != VERSION:
        return False, "invalid_version"
    if header.header_len != HEADER_LEN:
        return False, "invalid_header_len"
    if header.payload_type != PAYLOAD_TYPE_CSI_AMP:
        return False, "invalid_payload_type"
    if header.sample_count <= 0:
        return False, "invalid_sample_count"

    expected_len = header.header_len + (header.sample_count * 4)
    if packet_len != expected_len:
        return False, "invalid_packet_len"

    return True, "ok"


def parse_payload(packet: bytes, header: PacketHeader) -> List[float]:
    # payload는 float32 amplitude 배열이 연속 저장된 구조입니다.
    fmt = "<" + ("f" * header.sample_count)
    return list(struct.unpack_from(fmt, packet, header.header_len))


def build_record(
    header: PacketHeader,
    amp: List[float],
    recv_unix_us: int,
    addr: Tuple[str, int],
) -> Dict:
    # 후속 도구(라벨 생성, MAT 변환, 실시간 판정)가 공통으로 쓰는 JSON 스키마를 만듭니다.
    return {
        "received_at_unix_us": recv_unix_us,
        "source_ip": addr[0],
        "source_port": addr[1],
        "session_id": header.session_id,
        "device_id": header.device_id,
        "seq": header.seq,
        "timestamp_us": header.timestamp_us,
        "channel": header.channel,
        "rssi_dbm": header.rssi_dbm,
        "noise_floor_dbm": header.noise_floor_dbm,
        "sample_count": header.sample_count,
        "csi_amp": amp,
    }


def open_device_file(base_dir: Path, session_id: int, device_id: int):
    # 날짜/세션/장치 단위로 파일을 분리 저장해
    # 재현 실험, 재처리, 문제 재현(replay)을 쉽게 만듭니다.
    date_dir = time.strftime("%Y%m%d")
    out_dir = base_dir / "raw" / date_dir / f"session_{session_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"device_{device_id}.jsonl"
    return out_path.open("a", encoding="utf-8")


def print_stats(stats: Dict[int, DeviceStats], invalid_packets: int) -> None:
    print("\n[collector] current stats")
    print(f"- invalid_packets: {invalid_packets}")
    if not stats:
        print("- no valid packets yet")
        return

    for device_id, st in stats.items():
        total_expected = st.packets + st.dropped_packets
        drop_rate = (st.dropped_packets / total_expected * 100.0) if total_expected > 0 else 0.0
        print(
            f"- device={device_id} packets={st.packets} dropped={st.dropped_packets} "
            f"drop_rate={drop_rate:.2f}% samples={st.samples}"
        )


def parse_expected_device_ids(raw: str) -> Set[int]:
    if not raw.strip():
        return set()
    out: Set[int] = set()
    for part in raw.split(","):
        token = part.strip()
        if token:
            out.add(int(token))
    return out


def load_device_ids_from_registry(path: Path) -> Set[int]:
    # 운영 편의 기능:
    # CLI에 expected IDs를 매번 입력하지 않아도,
    # 등록표(device_registry.csv)에서 자동 로드할 수 있습니다.
    if not path.exists():
        return set()
    out: Set[int] = set()
    with path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            raw_id = (row.get("device_id") or "").strip()
            if not raw_id:
                continue
            out.add(int(raw_id))
    return out


def maybe_copy_session_meta(session_meta: Optional[Path], output_dir: Path, session_id: int) -> None:
    # 세션 메타 스냅샷을 결과 폴더에 같이 저장합니다.
    # 데이터만 따로 복사되어도 "어떤 조건에서 수집했는지"가 남도록 하기 위함입니다.
    if session_meta is None:
        return
    if not session_meta.exists():
        print(f"[collector] session meta not found: {session_meta}")
        return
    date_dir = time.strftime("%Y%m%d")
    out_dir = output_dir / "raw" / date_dir / f"session_{session_id}"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "session_meta_snapshot.yaml"
    shutil.copyfile(session_meta, target)
    print(f"[collector] session meta snapshot: {target}")


def print_expected_health(expected_ids: Set[int], stats: Dict[int, DeviceStats], now_unix_us: int, stale_sec: int) -> None:
    if not expected_ids:
        return
    stale_us = stale_sec * 1_000_000
    missing: List[int] = []
    stale: List[int] = []
    for device_id in sorted(expected_ids):
        st = stats.get(device_id)
        if st is None:
            missing.append(device_id)
            continue
        if st.last_seen_unix_us is None or (now_unix_us - st.last_seen_unix_us) > stale_us:
            stale.append(device_id)

    print(f"- expected_devices: {sorted(expected_ids)}")
    print(f"- missing_devices: {missing}")
    print(f"- stale_devices(>{stale_sec}s): {stale}")


def run_collector(
    host: str,
    port: int,
    output_dir: Path,
    print_every_sec: int,
    expected_device_ids: Set[int],
    stale_sec: int,
    session_meta: Optional[Path],
) -> None:
    # 메인 루프 동작:
    # recv -> header/payload 검증 -> JSONL 저장 -> 장치 상태 갱신
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, port))
    sock.settimeout(1.0)

    print(f"[collector] listening on udp://{host}:{port}")
    print(f"[collector] output directory: {output_dir}")

    stats: Dict[int, DeviceStats] = {}
    device_files: Dict[Tuple[int, int], object] = {}
    invalid_packets = 0
    last_print = time.time()
    should_stop = False
    session_meta_saved_for: Set[int] = set()

    def _stop_handler(signum, frame):
        nonlocal should_stop
        should_stop = True
        print(f"\n[collector] received signal={signum}, shutting down...")

    signal.signal(signal.SIGINT, _stop_handler)
    signal.signal(signal.SIGTERM, _stop_handler)

    while not should_stop:
        try:
            packet, addr = sock.recvfrom(4096)
        except socket.timeout:
            packet = None
        except OSError:
            break

        now = time.time()
        if now - last_print >= print_every_sec:
            print_stats(stats, invalid_packets)
            print_expected_health(expected_device_ids, stats, now_us(), stale_sec)
            last_print = now

        if packet is None:
            continue

        # 디스크 쓰기 전에 먼저 파싱/검증 수행(오염 데이터 저장 방지)
        header = parse_header(packet)
        if header is None:
            invalid_packets += 1
            continue

        ok, reason = validate_packet(header, len(packet))
        if not ok:
            invalid_packets += 1
            if invalid_packets <= 10:
                print(f"[collector] dropped invalid packet reason={reason} from={addr}")
            continue

        amp = parse_payload(packet, header)
        recv_unix_us = now_us()
        record = build_record(header, amp, recv_unix_us, addr)

        key = (header.session_id, header.device_id)
        if key not in device_files:
            device_files[key] = open_device_file(output_dir, header.session_id, header.device_id)
        if header.session_id not in session_meta_saved_for:
            maybe_copy_session_meta(session_meta, output_dir, header.session_id)
            session_meta_saved_for.add(header.session_id)

        f = device_files[key]
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

        if header.device_id not in stats:
            stats[header.device_id] = DeviceStats()
        st = stats[header.device_id]
        st.update_seq(header.seq)
        st.packets += 1
        st.samples += header.sample_count
        st.last_seen_unix_us = recv_unix_us

    print_stats(stats, invalid_packets)
    print_expected_health(expected_device_ids, stats, now_us(), stale_sec)
    for f in device_files.values():
        try:
            f.flush()
            f.close()
        except OSError:
            pass
    sock.close()
    print("[collector] stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="ESP32-S3 CSI UDP collector MVP")
    parser.add_argument("--host", default="0.0.0.0", help="UDP bind host")
    parser.add_argument("--port", type=int, default=9999, help="UDP bind port")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("mac_collector_output"),
        help="output base directory",
    )
    parser.add_argument(
        "--print-every-sec",
        type=int,
        default=5,
        help="periodic stats print interval",
    )
    parser.add_argument(
        "--expected-device-ids",
        type=str,
        default="",
        help='comma-separated device IDs, e.g. "101,102,103"',
    )
    parser.add_argument(
        "--stale-sec",
        type=int,
        default=10,
        help="mark expected device as stale if unseen longer than this",
    )
    parser.add_argument(
        "--device-registry-csv",
        type=Path,
        default=Path("mac_collector/device_registry.csv"),
        help="device registry CSV path used when expected-device-ids is empty",
    )
    parser.add_argument(
        "--session-meta",
        type=Path,
        default=None,
        help="optional session meta yaml to snapshot into each session directory",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    expected_device_ids = parse_expected_device_ids(args.expected_device_ids)
    if not expected_device_ids:
        # fallback: CLI 입력이 없으면 등록표에서 expected device 목록을 자동 추정
        expected_device_ids = load_device_ids_from_registry(args.device_registry_csv)
        if expected_device_ids:
            print(
                f"[collector] loaded expected device IDs from registry "
                f"{args.device_registry_csv}: {sorted(expected_device_ids)}"
            )
        else:
            print(
                f"[collector] no expected device IDs configured "
                f"(empty --expected-device-ids and no registry data at {args.device_registry_csv})"
            )
    run_collector(
        host=args.host,
        port=args.port,
        output_dir=args.output_dir,
        print_every_sec=args.print_every_sec,
        expected_device_ids=expected_device_ids,
        stale_sec=args.stale_sec,
        session_meta=args.session_meta,
    )


if __name__ == "__main__":
    main()
