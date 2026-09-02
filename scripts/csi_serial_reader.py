#!/usr/bin/env python3
"""esp32s3_csi_recv_poc 바이너리 CSI 프레임(v3)을 USB 시리얼로 읽어 세션에 저장.

프레임을 **변환 없이 그대로** `<session_dir>/device_<id>.csi` 에 이어붙인다. 저장 단계에
변환이 없으므로 변환 버그가 낄 자리가 없고, 위상(raw I/Q)이 보존된다. 진폭 계산과
서브캐리어 선별은 후처리(`scripts/csi_store.py`)가 맡는다.

`device_id` 는 보드가 2초마다 보내는 **IDENT 프레임의 eFuse MAC** 으로 결정한다.
호스트가 esptool 로 포트를 프로브할 필요가 없다 — esptool 프로브는 DTR/RTS 로 보드를
리셋시켜 TX 의 `tx_seq` 까지 되감아 놓는다.

    python scripts/csi_serial_reader.py \\
        --port /dev/cu.usbmodem101 \\
        --session-dir mac_collector_output/raw/20260825/143000_static_s21

종료 코드: 0 정상 / 2 IDENT 미수신(RX 보드가 아님) / 3 스트림 정지 / 4 파일 충돌
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from csi_session import write_device_stats  # noqa: E402
from csi_store import (  # noqa: E402
    FRAME_TYPE_IDENT,
    FRAME_TYPE_SINK,
    parse_sink_status,
    FrameSplitter,
    FrameStats,
    header_of,
    parse_ident,
)

try:
    import serial  # type: ignore[import-not-found]
except ImportError:
    print("pyserial이 필요합니다: pip install pyserial", file=sys.stderr)
    sys.exit(1)

EXIT_NO_IDENT = 2
EXIT_STALLED = 3
EXIT_FILE_EXISTS = 4

READ_CHUNK = 4096
#: 백로그 판별 — (보드 시계 진행 / 벽시계 진행)이 이 값 아래면 실시간을 따라잡은 것.
#: 실시간이면 1.0 근처, 백로그를 비우는 동안에는 수십~수백이 된다.
FLUSH_CAUGHT_UP_RATIO = 2.0
#: 비율을 재는 슬라이딩 윈도 길이(초)
FLUSH_WINDOW_S = 0.3
FLUSH_INTERVAL_S = 1.0


def open_serial(port: str, baud: int, *, force_lines: bool = False) -> "serial.Serial":
    """DTR/RTS 를 **건드리지 않고** 연다.

    ESP32-S3 dev 보드의 USB-C 는 외부 USB-UART 브리지가 아니라 네이티브
    USB-Serial-JTAG 다. 브리지 칩(CP210x/CH340)에서는 open() 시 DTR/RTS 가 asserted 되어
    auto-reset 회로가 보드를 리셋하므로 눌러두는 것이 맞지만, USB-Serial-JTAG 는 ESP-IDF
    드라이버가 이 선들을 직접 해석하기 때문에 **명시적으로 눌러두는 행위 자체가 리셋을
    유발한다.**

    실측 (RX103, 2026-08-25): `dtr=False, rts=False` → 포트 open 0.43초 뒤 보드 재부팅
    (`boot_id` 변화, uptime 0.31s 로 리셋). 손대지 않으면 재부팅 0회.
    이 때문에 매 수집마다 앞부분 6초 가량이 재부팅 구간으로 날아가고 있었다.

    브리지 칩 보드를 쓸 일이 생기면 ``force_lines=True`` 로 옛 동작을 되살린다.
    """
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 0.2
    if force_lines:
        ser.dtr = False
        ser.rts = False
    ser.open()
    if force_lines:
        try:
            ser.dtr = False
            ser.rts = False
        except OSError:
            pass
    return ser


def lookup_device_id(mac: str, registry: Path) -> tuple[Optional[int], str]:
    from registry import lookup_by_mac  # noqa: WPS433 (registry는 scripts/ 안)

    try:
        rec = lookup_by_mac(mac, registry)
    except (FileNotFoundError, ValueError):
        return None, ""
    if rec is None:
        return None, ""
    return rec.device_id, rec.board_name


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", required=True, help="시리얼 포트 (예: /dev/cu.usbmodem101)")
    p.add_argument("--baud", type=int, default=921600, help="USB-CDC라 실제 속도와 무관 (호환용)")
    p.add_argument("--session-dir", type=Path, default=None, help="csi_session.create_session()이 만든 디렉터리")
    p.add_argument("--device-id", type=int, default=None, help="IDENT 자동 식별을 덮어쓸 device_id")
    p.add_argument("--registry", type=Path,
                   default=Path(__file__).resolve().parents[1] / "mac_collector" / "device_registry.csv")
    p.add_argument("--ident-timeout", type=float, default=6.0,
                   help="이 시간 안에 IDENT가 없으면 RX 보드가 아니라고 보고 종료")
    p.add_argument("--stall-timeout", type=float, default=3.0,
                   help="이 시간 동안 프레임이 하나도 없으면 스트림 정지로 보고 종료")
    p.add_argument("--duration", type=float, default=0.0, help="0이면 무한, 양수면 그 시간 뒤 정상 종료")
    p.add_argument("--stats-every", type=int, default=500, help="N 프레임마다 진행 상태 출력")
    p.add_argument("--flush-idle-ms", type=float, default=200.0,
                   help="포트 open 직후 이 시간 동안 새 바이트가 없으면 백로그 없음으로 판단")
    p.add_argument("--flush-max-s", type=float, default=3.0, help="백로그 비우기 최대 시간")
    p.add_argument("--identify", action="store_true",
                   help="IDENT 만 읽어 보드 정보를 JSON 으로 출력하고 종료 (수집 안 함). "
                        "esptool 과 달리 보드를 리셋하지 않는다.")
    p.add_argument("--force-lines", action="store_true",
                   help="포트 open 시 DTR/RTS를 눌러둠 (USB-UART 브리지 칩 보드용. "
                        "ESP32-S3 USB-Serial-JTAG에서는 오히려 리셋을 유발하니 쓰지 말 것)")
    args = p.parse_args()

    tag = f"[reader {Path(args.port).name}]"
    if not args.identify and args.session_dir is None:
        p.error("--session-dir 가 필요합니다 (--identify 는 예외)")
    if not args.identify:
        print(f"{tag} open {args.port}", file=sys.stderr)
    ser = open_serial(args.port, args.baud, force_lines=args.force_lines)

    if args.identify:
        # 보드가 2초마다 보내는 IDENT 하나만 잡으면 된다. 백로그에 있는 것도 MAC 은 같으므로
        # 그대로 쓴다 — 단 카운터는 과거 스냅샷이라 여기서는 보고하지 않는다.
        sp = FrameSplitter()
        t0 = time.monotonic()
        found = None
        sink_mac = None
        rx_seen: dict[str, dict] = {}
        # 싱크 포트에는 여러 RX 의 IDENT 가 전달되어 온다. 첫 IDENT 만 보고 끝내면 싱크가
        # "RX103" 으로 표시된다. SINK_STATUS 가 보이거나 IDENT MAC 이 2개 이상이면 싱크다.
        #
        # 백로그 함정: 포트를 열자마자 나오는 IDENT 는 최대 ~4초 과거의 것이다. 재플래시
        # 직후 identify 가 옛 rx_id 를 보고한 적이 있다 (2026-09-03). 그래서 (1) 같은 MAC 의
        # IDENT 는 항상 **마지막 값**으로 덮어쓰고, (2) 백로그가 마르도록 최소 1초는 읽는다.
        MIN_READ_S = 1.0
        while time.monotonic() - t0 < args.ident_timeout:
            for frame in sp.feed(ser.read(READ_CHUNK)):
                h = header_of(frame)
                ft = int(h["frame_type"])
                if ft == FRAME_TYPE_SINK:
                    sink_mac, _ = parse_sink_status(frame)
                elif ft == FRAME_TYPE_IDENT:
                    mac, fw_id, _ = parse_ident(frame)
                    if mac not in rx_seen:
                        dev, name = lookup_device_id(mac, args.registry)
                        rx_seen[mac] = {"sta_mac": mac, "firmware": fw_id, "device_id": dev,
                                        "board_name": name, "registered": dev is not None}
                    rx_seen[mac]["rx_id"] = int(h["rx_id"])   # 항상 최신 IDENT 기준
            elapsed = time.monotonic() - t0
            if elapsed < MIN_READ_S:
                continue
            if sink_mac and rx_seen:
                break                         # 싱크 확정
            if not sink_mac and len(rx_seen) == 1 and elapsed > 2.5:
                break                         # RX 직결 — SINK_STATUS(2초 주기)가 없음을 확인
        ser.close()
        if sink_mac or len(rx_seen) > 1:
            found = {"port": args.port, "role": "sink", "sta_mac": sink_mac, "firmware": "sink",
                     "device_id": None, "board_name": "SINK", "registered": False,
                     "via": sorted(rx_seen.values(), key=lambda r: r["device_id"] or 0)}
        elif rx_seen:
            found = {"port": args.port, "role": "rx", **next(iter(rx_seen.values()))}
        print(json.dumps(found or {"port": args.port, "role": None, "registered": False,
                                   "device_id": None, "sta_mac": None, "firmware": None,
                                   "board_name": ""}, ensure_ascii=False))
        return 0 if found else EXIT_NO_IDENT

    # 보드는 호스트가 없는 동안에도 CSI 를 계속 링버퍼에 쌓는다. 붙자마자 읽으면 그 백로그가
    # 먼저 나와 수집 앞부분이 수십 초 묵은 프레임이 된다 — tx_seq 격자에 큰 구멍이 생긴다
    # (실측: 46초 방치 후 4134스텝 구멍).
    #
    # 백로그는 USB 속도로 쏟아지므로 실시간(100Hz)보다 훨씬 빠르게 도착한다. 도착 속도가
    # 실시간 수준으로 떨어지면 다 비운 것이다. 고정 시간으로 버리면 살아 있는 스트림에서도
    # 매번 그만큼을 버리게 되므로 속도로 판별한다.
    flush_stats = FrameStats()
    flusher = FrameSplitter(flush_stats)
    t_flush = time.monotonic()
    t_data = t_flush
    # (벽시계, 보드시계) 샘플의 슬라이딩 윈도. 기준점을 고정하면 백로그를 다 비운 뒤에도
    # 누적 비율이 천천히 떨어져 한참을 더 버리게 된다 — 최근 구간만 본다.
    samples: list[tuple[float, int]] = []
    flushed_ident: bytes | None = None
    while time.monotonic() - t_flush < args.flush_max_s:
        chunk = ser.read(READ_CHUNK)
        now = time.monotonic()
        if not chunk:
            if now - t_data > args.flush_idle_ms / 1000.0:
                break               # 버퍼가 비어 있던 보드 (막 부팅한 경우)
            continue
        t_data = now
        caught_up = False
        for frame in flusher.feed(chunk):
            if int(header_of(frame)["frame_type"]) == FRAME_TYPE_IDENT:
                # 백로그와 함께 버리면 보드 식별이 다음 IDENT(2초 뒤)까지 미뤄진다
                flushed_ident = frame
                continue
            samples.append((now, int(header_of(frame)["timestamp_us"])))
            while len(samples) > 2 and now - samples[1][0] >= FLUSH_WINDOW_S:
                samples.pop(0)
            wall_s = now - samples[0][0]
            if wall_s < FLUSH_WINDOW_S:
                continue
            board_s = (samples[-1][1] - samples[0][1]) / 1e6
            # 백로그를 비우는 동안에는 보드 시계가 벽시계보다 훨씬 빨리 흐른다
            # (USB 속도로 쏟아지므로). 두 시계 속도가 비슷해지면 실시간을 따라잡은 것이다.
            if board_s / wall_s < FLUSH_CAUGHT_UP_RATIO:
                caught_up = True
                break
        if caught_up:
            break
    if flush_stats.frames:
        print(f"{tag} 백로그 {flush_stats.frames}프레임 버림 "
              f"({time.monotonic() - t_flush:.2f}s)", file=sys.stderr)

    stats = FrameStats()            # 포트 단위 (crc_fail·invalid·resync)
    splitter = FrameSplitter(stats)
    streams: dict[int, RxStream] = {}   # rx_id → 스트림. USB 직결은 {0: ...} 하나뿐
    sink: dict = {}                      # 싱크 경유일 때 싱크 카운터 (첫 값 기준 델타)
    sink_base: dict = {}
    start = time.monotonic()
    last_frame_at = start
    last_flush = start
    rc = 0

    def stream_for(rx_id: int) -> "RxStream":
        st = streams.get(rx_id)
        if st is None:
            st = streams[rx_id] = RxStream(rx_id)
            # --device-id 는 단일 스트림(USB 직결)에서만 의미가 있다
            if args.device_id is not None and rx_id == 0:
                st.device_id = args.device_id
                st.open_output(args.session_dir, tag)   # 실패하면 fp 가 None 으로 남는다
        return st

    def handle_ident(frame: bytes, *, live: bool = True) -> bool:
        """IDENT 로 보드를 식별하고 출력 파일을 연다. 실패면 False (rc 는 exit_code 에)."""
        nonlocal rc
        h = header_of(frame)
        st = stream_for(int(h["rx_id"]))
        mac, fw_id, counters = parse_ident(frame)
        if live:
            st.fw_counters = counters
            if not st.fw_baseline:
                # 백로그 IDENT 는 최대 링버퍼 깊이(~4초)만큼 과거 스냅샷이라 기준점으로 쓰지 않는다
                st.fw_baseline.update(counters)
        if st.base_mac:
            return True
        st.base_mac, st.fw = mac, fw_id
        dev, name = lookup_device_id(mac, args.registry)
        st.board_name = name
        if st.device_id is not None:
            return True
        if dev is None:
            print(f"{tag} [중단] MAC {mac} 이 registry에 없습니다 — "
                  f"`python scripts/device_registry.py add --port {args.port} "
                  f"--board-name RXn` 으로 등록하세요.", file=sys.stderr)
            rc = EXIT_NO_IDENT
            return False
        st.device_id = dev
        if not st.open_output(args.session_dir, tag):
            rc = EXIT_FILE_EXISTS
            return False
        print(f"{tag} IDENT: {mac} → RX{dev} ({name}, fw={fw_id}"
              f"{f', rx_id={st.rx_id}' if st.rx_id else ''})", file=sys.stderr)
        return True

    try:
        if args.device_id is not None:
            # 명시적 override 는 시작 즉시 파일을 연다 — 충돌을 프레임 도착과 무관하게 드러낸다
            st = stream_for(0)
            if st.fp is None:
                return EXIT_FILE_EXISTS
        if flushed_ident is not None and handle_ident(flushed_ident, live=False) is False:
            return rc

        while True:
            now = time.monotonic()
            if args.duration and now - start >= args.duration:
                break

            for frame in splitter.feed(ser.read(READ_CHUNK)):
                now = time.monotonic()
                last_frame_at = now
                h = header_of(frame)

                if int(h["frame_type"]) == FRAME_TYPE_IDENT:
                    if handle_ident(frame) is False:
                        return rc
                    continue
                if int(h["frame_type"]) == FRAME_TYPE_SINK:
                    mac, ctr = parse_sink_status(frame)
                    if not sink_base:
                        sink_base.update(ctr)
                        print(f"{tag} SINK: {mac}", file=sys.stderr)
                    sink = {"sink_mac": mac, **{k: v - sink_base[k] for k, v in ctr.items()}}
                    continue

                st = stream_for(int(h["rx_id"]))
                st.on_csi(h, frame, tag)

                total = sum(x.frames for x in streams.values())
                if args.stats_every and total % args.stats_every == 0:
                    elapsed = now - start
                    print(f"{tag} frames={total} hz={total / elapsed:.1f} "
                          f"crc_fail={stats.crc_fail} invalid={stats.invalid} resync={stats.resync} "
                          + "  ".join(x.progress() for x in streams.values() if x.device_id is not None),
                          file=sys.stderr)

            now = time.monotonic()
            if now - last_flush >= FLUSH_INTERVAL_S:
                for x in streams.values():
                    x.flush()
                last_flush = now

            identified = any(x.device_id is not None for x in streams.values())
            if not identified and now - start > args.ident_timeout:
                print(f"{tag} IDENT 미수신 {args.ident_timeout:.0f}s — RX 보드가 아닌 것으로 보고 종료",
                      file=sys.stderr)
                return EXIT_NO_IDENT

            if identified and now - last_frame_at > args.stall_timeout:
                # 예전에는 serial timeout 을 continue 로 삼켜서, 보드가 죽어도 reader 가
                # 빈 파일을 만들며 영원히 정상인 척했다.
                print(f"{tag} [중단] {args.stall_timeout:.0f}s 동안 프레임 없음 — "
                      f"보드 정지 또는 USB 분리", file=sys.stderr)
                rc = EXIT_STALLED
                break

    except KeyboardInterrupt:
        print(f"\n{tag} interrupted", file=sys.stderr)
    finally:
        for x in streams.values():
            x.close()
        ser.close()
        elapsed = time.monotonic() - start
        for x in streams.values():
            if x.device_id is None:
                continue
            record = x.record(args.port, elapsed, rc, stats)
            if sink:
                record.update(sink)     # 같은 포트의 모든 RX 레코드에 싱크 카운터를 함께 남긴다
            write_device_stats(args.session_dir, x.device_id, record)
        total = sum(x.frames for x in streams.values())
        print(f"{tag} done. frames={total} crc_fail={stats.crc_fail} invalid={stats.invalid} "
              f"resync={stats.resync} elapsed={elapsed:.2f}s hz={total / elapsed if elapsed else 0:.1f}",
              file=sys.stderr)
        for x in streams.values():
            if x.device_id is not None:
                print(f"{tag}   RX{x.device_id}: {x.summary()}", file=sys.stderr)
        if sink:
            print(f"{tag}   SINK(세션 구간): recv={sink['sink_recv']} sent={sink['sink_sent']} "
                  f"drop={sink['sink_drop']} usb_timeout={sink['sink_usb_timeout']} "
                  f"foreign={sink['sink_foreign']}", file=sys.stderr)
    return rc


class RxStream:
    """한 포트 위의 RX 하나. 싱크를 거치면 여러 RX 가 한 스트림에 섞여 오므로 rx_id 로 가른다.

    USB 직결에서는 rx_id=0 인 스트림 하나뿐이라, 이 클래스가 그 경우도 그대로 처리한다.
    """

    def __init__(self, rx_id: int) -> None:
        self.rx_id = rx_id
        self.device_id: Optional[int] = None
        self.board_name = ""
        self.base_mac = ""
        self.fw = ""
        self.fw_counters: dict = {}
        self.fw_baseline: dict = {}
        self.fp = None
        self.out_path: Optional[Path] = None
        self.pending: list[bytes] = []     # IDENT 이전에 도착한 CSI (2초 ≈ 200프레임)
        self.frames = 0
        self.seq_gap = 0
        self.boot_changes = 0
        self.tx_back = 0
        self.last_seq: Optional[int] = None
        self.last_boot: Optional[int] = None
        self.first_tx_seq = self.last_tx_seq = None
        self.first_ts_us = self.last_ts_us = None

    def open_output(self, session_dir: Path, tag: str) -> bool:
        self.out_path = Path(session_dir) / f"device_{self.device_id}.csi"
        try:
            self.fp = self.out_path.open("xb")   # 배타적 생성 — append 로 두 런이 섞이던 경로를 막는다
        except FileExistsError:
            print(f"{tag} [중단] 이미 존재: {self.out_path}", file=sys.stderr)
            return False
        for buffered in self.pending:
            self.fp.write(buffered)
        self.pending.clear()
        print(f"{tag} → {self.out_path}", file=sys.stderr)
        return True

    def on_csi(self, h, frame: bytes, tag: str) -> None:
        boot = int(h["boot_id"])
        if self.last_boot is not None and boot != self.last_boot:
            self.boot_changes += 1
            self.last_seq = None
            print(f"{tag} [경고] RX{self.device_id or '?'} 수집 중 보드 재부팅 "
                  f"(boot_id {self.last_boot} → {boot}). 이 구간은 데이터가 비어 있습니다.",
                  file=sys.stderr)
        self.last_boot = boot

        seq = int(h["seq"])
        if self.last_seq is not None and seq > self.last_seq + 1:
            self.seq_gap += seq - (self.last_seq + 1)
        self.last_seq = seq

        tx = int(h["tx_seq"])
        if self.first_tx_seq is None:
            self.first_tx_seq = tx
            self.first_ts_us = int(h["timestamp_us"])
        elif tx < self.last_tx_seq:
            # TX 가 재부팅하면 tx_seq 가 0부터 다시 시작한다 — RX 간 정렬 키가 깨진다.
            self.tx_back += 1
            if self.tx_back == 1:
                print(f"{tag} [경고] TX 재부팅으로 보입니다 (tx_seq {self.last_tx_seq} → {tx}). "
                      f"이 세션은 시간 격자가 깨져 학습에 쓸 수 없습니다 — TX 전원을 확인하고 "
                      f"재수집하세요.", file=sys.stderr)
        self.last_tx_seq = tx
        self.last_ts_us = int(h["timestamp_us"])

        if self.fp is None:
            self.pending.append(frame)
            if len(self.pending) > 2000:     # ~20초분. IDENT 가 안 오면 어차피 끊긴다
                self.pending.pop(0)
        else:
            self.fp.write(frame)
        self.frames += 1

    def fw_delta(self) -> dict:
        # 펌웨어 카운터는 부팅 이후 누적이라 세션 구간 델타로 바꾼다
        return {k: v - self.fw_baseline.get(k, 0) for k, v in self.fw_counters.items()}

    def progress(self) -> str:
        d = self.fw_delta()
        fw = (f" fw[cb={d['fw_csi_cb']} sent={d['fw_sent']} rbdrop={d['fw_ringbuf_drop']} "
              f"fail={d['fw_send_fail']}]") if d else ""
        return f"RX{self.device_id}[n={self.frames} gap={self.seq_gap} tx={self.last_tx_seq}{fw}]"

    def summary(self) -> str:
        d = self.fw_delta()
        return (f"frames={self.frames} seq_gap={self.seq_gap} boot_changes={self.boot_changes} "
                f"tx_back={self.tx_back}"
                + (f"  펌웨어(세션 구간): csi_cb={d['fw_csi_cb']} sent={d['fw_sent']} "
                   f"ringbuf_drop={d['fw_ringbuf_drop']} fail={d['fw_send_fail']}" if d else ""))

    def flush(self) -> None:
        if self.fp is not None:
            self.fp.flush()

    def close(self) -> None:
        if self.fp is not None:
            self.fp.flush()
            self.fp.close()
            self.fp = None

    def record(self, port: str, elapsed: float, rc: int, port_stats: FrameStats) -> dict:
        span = ((self.last_ts_us - self.first_ts_us) / 1e6) if self.last_ts_us else 0.0
        return {
            "device_id": self.device_id,
            "board_name": self.board_name,
            "sta_mac": self.base_mac,
            "port": port,
            "rx_id": self.rx_id,
            "firmware": self.fw,
            "boot_id": self.last_boot,
            "elapsed_s": round(elapsed, 3),
            # 수집 구간 길이는 보드 시계 기준 — host wall clock 에는 백로그 비우기·기동이 섞인다
            "span_s": round(span, 3),
            "first_tx_seq": self.first_tx_seq,
            "last_tx_seq": self.last_tx_seq,
            "tx_back": self.tx_back,
            "exit_code": rc,
            **self.fw_delta(),
            "frames": self.frames,
            "seq_gap": self.seq_gap,
            "boot_changes": self.boot_changes,
            # 아래 셋은 포트(스트림) 단위 지표다. 싱크를 거쳐 여러 RX 가 한 포트에 오면
            # 값이 같게 반복된다 — 소비자(요약·GUI)가 device 별로 읽는 계약을 유지하려는 것
            "crc_fail": port_stats.crc_fail,
            "invalid": port_stats.invalid,
            "resync": port_stats.resync,
        }


if __name__ == "__main__":
    sys.exit(main())
