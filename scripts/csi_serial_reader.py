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
        while time.monotonic() - t0 < args.ident_timeout and found is None:
            for frame in sp.feed(ser.read(READ_CHUNK)):
                if int(header_of(frame)["frame_type"]) == FRAME_TYPE_IDENT:
                    mac, fw_id, _ = parse_ident(frame)
                    dev, name = lookup_device_id(mac, args.registry)
                    found = {"port": args.port, "sta_mac": mac, "firmware": fw_id,
                             "device_id": dev, "board_name": name,
                             "registered": dev is not None}
                    break
        ser.close()
        print(json.dumps(found or {"port": args.port, "registered": False, "device_id": None,
                                   "sta_mac": None, "firmware": None, "board_name": ""},
                         ensure_ascii=False))
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

    stats = FrameStats()
    splitter = FrameSplitter(stats)
    device_id: Optional[int] = args.device_id
    board_name = ""
    base_mac = ""
    fw = ""
    fw_counters: dict = {}
    fw_baseline: dict = {}   # 첫 IDENT 값 — 카운터는 부팅 이후 누적이라 델타로 바꾼다
    fp = None
    out_path: Optional[Path] = None
    pending: list[bytes] = []      # IDENT 이전에 도착한 CSI 프레임 (2초 ≈ 200프레임)
    last_seq: Optional[int] = None
    last_boot: Optional[int] = None
    first_tx_seq = last_tx_seq = None
    first_ts_us = last_ts_us = None
    tx_back = 0          # tx_seq 역행 횟수 = TX 재부팅

    start = time.monotonic()
    last_frame_at = start
    last_flush = start
    rc = 0

    def open_output(dev: int) -> bool:
        nonlocal fp, out_path
        out_path = Path(args.session_dir) / f"device_{dev}.csi"
        try:
            fp = out_path.open("xb")   # 배타적 생성 — append 로 두 런이 섞이던 경로를 막는다
        except FileExistsError:
            print(f"{tag} [중단] 이미 존재: {out_path}", file=sys.stderr)
            return False
        print(f"{tag} → {out_path}", file=sys.stderr)
        return True

    exit_code = 0

    def handle_ident(frame: bytes, *, live: bool = True) -> bool:
        """IDENT 프레임으로 보드를 식별하고 출력 파일을 연다. 실패면 False.

        `live=False` 는 백로그에서 건진 IDENT — 식별에는 쓰지만 **카운터 기준점으로는
        쓰지 않는다.** 백로그 IDENT 는 최대 링버퍼 깊이(~4초)만큼 과거의 스냅샷이라,
        기준점으로 삼으면 세션 델타에 수집 전 구간이 섞인다.
        """
        nonlocal base_mac, fw, fw_counters, device_id, board_name, fp, exit_code
        mac, fw_id, counters = parse_ident(frame)
        if live:
            fw_counters = counters
            if not fw_baseline:
                fw_baseline.update(counters)
        if base_mac:
            return True
        base_mac, fw = mac, fw_id
        dev, name = lookup_device_id(mac, args.registry)
        board_name = name
        if device_id is not None:
            return True
        if dev is None:
            print(f"{tag} [중단] MAC {mac} 이 registry에 없습니다 — "
                  f"`python scripts/device_registry.py add --port {args.port} "
                  f"--board-name RXn` 으로 등록하세요.", file=sys.stderr)
            exit_code = EXIT_NO_IDENT
            return False
        device_id = dev
        if not open_output(device_id):
            exit_code = EXIT_FILE_EXISTS
            return False
        for buffered in pending:
            fp.write(buffered)
        pending.clear()
        print(f"{tag} IDENT: {mac} → RX{device_id} ({board_name}, fw={fw})", file=sys.stderr)
        return True

    try:
        if device_id is not None and not open_output(device_id):
            return EXIT_FILE_EXISTS
        if flushed_ident is not None and handle_ident(flushed_ident, live=False) is False:
            return exit_code

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
                        return exit_code
                    continue

                # CSI 프레임
                boot = int(h["boot_id"])
                if last_boot is not None and boot != last_boot:
                    stats.boot_changes += 1
                    last_seq = None      # 재부팅이면 seq 연속성 비교를 리셋
                    print(f"{tag} [경고] 수집 중 보드 재부팅 (boot_id {last_boot} → {boot}). "
                          f"이 구간은 데이터가 비어 있습니다.", file=sys.stderr)
                last_boot = boot

                seq = int(h["seq"])
                if last_seq is not None and seq > last_seq + 1:
                    stats.seq_gap += seq - (last_seq + 1)
                last_seq = seq

                tx = int(h["tx_seq"])
                if first_tx_seq is None:
                    first_tx_seq = tx
                    first_ts_us = int(h["timestamp_us"])
                elif tx < last_tx_seq:
                    # TX 가 재부팅하면 tx_seq 가 0부터 다시 시작한다. tx_seq 는 RX 간
                    # 정렬 키라서, 이게 되감기면 세션의 시간 격자가 깨진다.
                    # TX 를 별도 전원(보조배터리 등)에 두면 조용히 일어날 수 있다.
                    tx_back += 1
                    if tx_back == 1:
                        print(f"{tag} [경고] TX 재부팅으로 보입니다 "
                              f"(tx_seq {last_tx_seq} → {tx}). 이 세션은 시간 격자가 깨져 "
                              f"학습에 쓸 수 없습니다 — TX 전원을 확인하고 재수집하세요.",
                              file=sys.stderr)
                last_tx_seq = tx
                last_ts_us = int(h["timestamp_us"])

                if fp is None:
                    pending.append(frame)
                    if len(pending) > 2000:      # ~20초분. IDENT가 안 오면 어차피 아래에서 끊긴다
                        pending.pop(0)
                else:
                    fp.write(frame)

                if args.stats_every and stats.frames % args.stats_every == 0:
                    elapsed = now - start
                    d = {k: v - fw_baseline.get(k, 0) for k, v in fw_counters.items()}
                    fw_note = (f" fw[cb={d['fw_csi_cb']} sent={d['fw_uart_sent']} "
                               f"rbdrop={d['fw_ringbuf_drop']} "
                               f"partial={d['fw_uart_partial']}]") if fw_counters else ""
                    print(f"{tag} frames={stats.frames} hz={stats.frames / elapsed:.1f} "
                          f"crc_fail={stats.crc_fail} invalid={stats.invalid} "
                          f"resync={stats.resync} seq_gap={stats.seq_gap} "
                          f"rssi={int(h['rssi'])} tx_seq={tx}{fw_note}", file=sys.stderr)

            now = time.monotonic()
            if fp is not None and now - last_flush >= FLUSH_INTERVAL_S:
                fp.flush()
                last_flush = now

            if device_id is None and now - start > args.ident_timeout:
                print(f"{tag} IDENT 미수신 {args.ident_timeout:.0f}s — RX 보드가 아닌 것으로 보고 종료",
                      file=sys.stderr)
                return EXIT_NO_IDENT

            if device_id is not None and now - last_frame_at > args.stall_timeout:
                # 예전에는 serial timeout 을 continue 로 삼켜서, 보드가 죽어도 reader 가
                # 빈 파일을 만들며 영원히 정상인 척했다.
                print(f"{tag} [중단] {args.stall_timeout:.0f}s 동안 프레임 없음 — "
                      f"보드 정지 또는 USB 분리", file=sys.stderr)
                rc = EXIT_STALLED
                break

    except KeyboardInterrupt:
        print(f"\n{tag} interrupted", file=sys.stderr)
    finally:
        if fp is not None:
            fp.flush()
            fp.close()
        ser.close()
        elapsed = time.monotonic() - start
        if device_id is not None:
            record = {
                "device_id": device_id,
                "board_name": board_name,
                "sta_mac": base_mac,
                "port": args.port,
                "firmware": fw,
                "boot_id": last_boot,
                "elapsed_s": round(elapsed, 3),
                # 수집 구간 길이는 보드 시계 기준. 호스트 wall clock 에는 백로그 비우기와
                # 기동 시간이 섞여 있어 Hz 가 낮게 보인다.
                "span_s": round((last_ts_us - first_ts_us) / 1e6, 3) if last_ts_us else 0.0,
                "first_tx_seq": first_tx_seq,
                "last_tx_seq": last_tx_seq,
                "tx_back": tx_back,
                "exit_code": rc,
                # 펌웨어 카운터는 부팅 이후 누적이라 그대로 쓰면 오해를 부른다
                # (수집 전 방치 시간의 ringbuf_drop 이 그대로 섞임). 세션 구간 델타로 기록.
                **{k: v - fw_baseline.get(k, 0) for k, v in fw_counters.items()},
                **stats.as_dict(),
            }
            write_device_stats(args.session_dir, device_id, record)
        print(f"{tag} done. frames={stats.frames} crc_fail={stats.crc_fail} "
              f"invalid={stats.invalid} resync={stats.resync} seq_gap={stats.seq_gap} "
              f"tx_back={tx_back} "
              f"elapsed={elapsed:.2f}s hz={stats.frames / elapsed if elapsed else 0:.1f}",
              file=sys.stderr)
        if fw_counters:
            d = {k: v - fw_baseline.get(k, 0) for k, v in fw_counters.items()}
            print(f"{tag} 펌웨어(세션 구간): csi_cb={d['fw_csi_cb']} "
                  f"uart_sent={d['fw_uart_sent']} ringbuf_drop={d['fw_ringbuf_drop']} "
                  f"partial={d['fw_uart_partial']}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
