#!/usr/bin/env python3
"""
MeshSense 터미널 가이드 CLI — 플래시·수집기를 메뉴로 실행.

  python scripts/meshsense_cli.py
  python scripts/meshsense_cli.py --quick   # 가이드 없이 메인 메뉴만
"""

from __future__ import annotations

import argparse
import glob
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Literal, NamedTuple, Optional, Sequence, Tuple


class _PreflightRow(NamedTuple):
    name: str
    ok: bool
    detail: str
    action: str = ""
    required: bool = True

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CONFIG_PATH = SCRIPT_DIR / "meshsense_config.json"
CONFIG_EXAMPLE = SCRIPT_DIR / "meshsense_config.example.json"
DEVICE_REGISTRY = REPO_ROOT / "mac_collector" / "device_registry.csv"
TX_REGISTRY = REPO_ROOT / "mac_collector" / "tx_registry.csv"
SESSION_META = REPO_ROOT / "mac_collector" / "session_meta.yaml"
COLLECTOR_SCRIPT = REPO_ROOT / "mac_collector" / "udp_collector_mvp.py"
VISUALIZE_SCRIPT = SCRIPT_DIR / "visualize_csi.py"
OUTPUT_DIR = REPO_ROOT / "mac_collector_output"
RX_PROJECT = REPO_ROOT / "esp32s3_csi_sender"
VENV_DIR = REPO_ROOT / ".venv"
VENV_PYTHON = VENV_DIR / "bin" / "python"
VIZ_REQUIREMENTS = REPO_ROOT / "requirements-viz.txt"

# 새 버전 (esp-csi 베이스 PoC) — doc/firmware/csi-poc.md 참고
SEND_POC_PROJECT = REPO_ROOT / "esp32s3_csi_send_poc"
RECV_POC_PROJECT = REPO_ROOT / "esp32s3_csi_recv_poc"
SERIAL_READER_SCRIPT = SCRIPT_DIR / "csi_serial_reader.py"
POC_LOG_DIR = REPO_ROOT / "log"  # .gitignore 처리됨

BoardKind = Literal["tx", "rx"]

sys.path.insert(0, str(SCRIPT_DIR))


def _pause(msg: str = "계속하려면 Enter…") -> None:
    input(f"\n{msg}")


def _ask_yes_no(prompt: str, *, default_no: bool = True) -> bool:
    hint = "[y/N]" if default_no else "[Y/n]"
    answer = input(f"{prompt} {hint}: ").strip().lower()
    if not answer:
        return not default_no
    return answer in ("y", "yes")


def _choose(prompt: str, options: Sequence[str]) -> int:
    """0-based index. 빈 입력이면 0."""
    print()
    for i, label in enumerate(options, start=1):
        print(f"  [{i}] {label}")
    while True:
        raw = input(f"{prompt} (1-{len(options)}): ").strip()
        if not raw and options:
            return 0
        if raw.isdigit():
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return idx
        print("  잘못된 번호입니다.")


def _banner() -> None:
    print()
    print("=" * 60)
    print("  MeshSense — 실험 보조 CLI")
    print(f"  프로젝트: {REPO_ROOT}")
    print("=" * 60)


def _list_usb_ports() -> List[str]:
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports


def _pick_port() -> Optional[str]:
    ports = _list_usb_ports()
    if not ports:
        print("\n[안내] USB 시리얼 포트를 찾지 못했습니다.")
        print("  ESP32를 USB로 연결한 뒤 다시 시도하세요.")
        manual = input("  직접 입력 (예: /dev/cu.usbmodem101, 빈칸=취소): ").strip()
        return manual or None
    if len(ports) == 1:
        print(f"\n[자동 선택] {ports[0]}")
        return ports[0]
    labels = [f"{p}" for p in ports]
    idx = _choose("보드를 선택하세요", labels)
    return ports[idx]


def _postprocess_venv_python() -> Optional[Path]:
    """후처리·시각화용 .venv Python (플래시/수집기는 sys.executable 유지)."""
    for candidate in (VENV_PYTHON, VENV_DIR / "bin" / "python3"):
        if candidate.is_file():
            return candidate
    return None


def _viz_venv_numpy_ok(py: Path) -> bool:
    proc = subprocess.run(
        [str(py), "-c", "import numpy, matplotlib"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _viz_venv_status() -> Tuple[bool, str]:
    py = _postprocess_venv_python()
    if py is None:
        return False, ".venv 없음"
    if _viz_venv_numpy_ok(py):
        return True, "numpy·matplotlib OK"
    return False, ".venv 있음, 패키지 미설치"


def _viz_venv_bootstrap_hint() -> str:
    req = "requirements-viz.txt" if VIZ_REQUIREMENTS.is_file() else "numpy matplotlib"
    return (
        f"python3 -m venv .venv && .venv/bin/pip install -r {req}"
        if VIZ_REQUIREMENTS.is_file()
        else "python3 -m venv .venv && .venv/bin/pip install numpy matplotlib"
    )


def _pip_install_viz_deps(py: Path) -> bool:
    args = [str(py), "-m", "pip", "install", "-q"]
    if VIZ_REQUIREMENTS.is_file():
        args.extend(["-r", str(VIZ_REQUIREMENTS)])
    else:
        args.extend(["numpy", "matplotlib"])
    print("\n[실행]", " ".join(args))
    proc = subprocess.run(args, cwd=str(REPO_ROOT))
    return proc.returncode == 0 and _viz_venv_numpy_ok(py)


def _ensure_postprocess_venv(*, interactive: bool) -> Optional[Path]:
    """CSI PNG용 .venv. interactive=True 이면 없을 때 생성·pip 설치 제안."""
    py = _postprocess_venv_python()
    if py is not None and _viz_venv_numpy_ok(py):
        return py

    if not interactive:
        return None

    if py is None:
        print("\n[안내] CSI 워터폴 PNG 는 프로젝트 .venv (numpy·matplotlib) 가 필요합니다.")
        if not _ask_yes_no(".venv 후처리 환경을 지금 만들까요?", default_no=False):
            return None
        print("\n[실행]", sys.executable, "-m venv", str(VENV_DIR))
        rc = subprocess.run(
            [sys.executable, "-m", "venv", str(VENV_DIR)],
            cwd=str(REPO_ROOT),
            check=False,
        )
        if rc != 0:
            print("[경고] venv 생성 실패")
            return None
        py = _postprocess_venv_python()
        if py is None:
            print("[경고] .venv/bin/python 을 찾지 못했습니다.")
            return None

    if not _viz_venv_numpy_ok(py):
        if not _ask_yes_no("numpy·matplotlib 을 .venv 에 설치할까요?", default_no=False):
            return None
        if not _pip_install_viz_deps(py):
            print("[경고] 패키지 설치 실패 —", _viz_venv_bootstrap_hint())
            return None

    return py


def _run_python(
    script: Path,
    args: List[str],
    *,
    cwd: Optional[Path] = None,
    python: Optional[Path] = None,
) -> int:
    cmd = [str(python or sys.executable), str(script), *args]
    print("\n[실행]", " ".join(cmd))
    try:
        proc = subprocess.run(cmd, cwd=str(cwd or REPO_ROOT))
        return proc.returncode
    except KeyboardInterrupt:
        print("\n[중단] 실행 취소 (Ctrl+C)")
        return 130


def _mac_wifi_ipv4(iface: str) -> Optional[str]:
    """ipconfig getifaddr <iface> — 실패·미연결 시 None."""
    try:
        result = subprocess.run(
            ["ipconfig", "getifaddr", iface],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    ip = result.stdout.strip()
    return ip or None


def _verify_mac_softap_ip(*, expected_ip: str, ap_ssid: str) -> bool:
    """Mac이 TX SoftAP에 붙어 collector.ip 와 일치하는지 확인. False=가이드 중단."""
    ifaces = ("en0", "en1")
    while True:
        _pause("Wi-Fi 연결 후 Enter…")
        found: List[Tuple[str, str]] = []
        for iface in ifaces:
            ip = _mac_wifi_ipv4(iface)
            if ip:
                found.append((iface, ip))

        print("\n  Mac IP 확인 (meshsense_config collector.ip 기준)")
        if not found:
            print("    en0/en1: IP 없음 (SoftAP 미연결 또는 다른 인터페이스)")
        else:
            for iface, ip in found:
                mark = "일치" if ip == expected_ip else "불일치"
                print(f"    {iface}: {ip}  → {mark} (기대 {expected_ip})")

        if any(ip == expected_ip for _, ip in found):
            matched = next(iface for iface, ip in found if ip == expected_ip)
            print(f"\n  [통과] {matched} = {expected_ip}")
            return True

        print(f"\n  [필요] Mac IP가 collector.ip ({expected_ip}) 와 같아야 합니다.")
        print("  ① TX 보드 전원·SoftAP(SSID)가 켜져 있는지 확인")
        print(f"  ② Mac Wi-Fi를 「{ap_ssid}」에 **먼저** 연결 (RX 플래시·수집기보다 우선)")
        print("  ③ 터미널에서 확인: ipconfig getifaddr en0")
        if found:
            detail = ", ".join(f"{iface}={ip}" for iface, ip in found)
            print(f"     현재: {detail}")
            print(
                "  IP가 다르면 scripts/meshsense_config.json 의 collector.ip 를"
                " 실제 IP에 맞게 고친 뒤 RX를 다시 플래시하세요."
            )
        if not _ask_yes_no("Wi-Fi·IP 확인 후 다시 검사할까요?", default_no=False):
            if _ask_yes_no("IP 확인 없이 다음 단계로 진행할까요? (비권장)", default_no=True):
                return True
            print("[중단] Mac 네트워크 확인 후 다시 가이드를 시작하세요.")
            return False


def _check_config() -> Tuple[bool, str]:
    if CONFIG_PATH.is_file():
        return True, f"OK: {CONFIG_PATH}"
    if CONFIG_EXAMPLE.is_file():
        return False, f"없음: {CONFIG_PATH} (example 복사 필요)"
    return False, "meshsense_config.example.json 도 없습니다."


def _ensure_config_interactive() -> bool:
    ok, msg = _check_config()
    print(msg)
    if ok:
        return True
    if not CONFIG_EXAMPLE.is_file():
        return False
    if _ask_yes_no("example에서 meshsense_config.json 을 만들까요?", default_no=False):
        import shutil

        shutil.copyfile(CONFIG_EXAMPLE, CONFIG_PATH)
        print(f"[ok] 생성: {CONFIG_PATH}")
        print("  collector.ip 등을 실험 환경에 맞게 편집하세요.")
        print("  (TX SoftAP 접속 후 Mac IP: ipconfig getifaddr en0)")
        _pause("편집 후 Enter…")
        return True
    return False


def _registry_row(label: str, path: Path, *, load_fn) -> _PreflightRow:
    if not path.is_file():
        return _PreflightRow(
            label,
            False,
            "파일 없음",
            f"보드 관리에서 등록하거나 {path.name} 생성",
        )
    try:
        n = len(load_fn(path))
        return _PreflightRow(label, True, f"{n}대 등록됨")
    except (FileNotFoundError, ValueError) as exc:
        return _PreflightRow(
            label,
            False,
            f"CSV 오류 ({exc})",
            "python scripts/device_registry.py verify (RX) 등 검증",
        )


def _print_preflight_report(rows: Sequence[_PreflightRow]) -> bool:
    required = [r for r in rows if r.required]
    passed = sum(1 for r in required if r.ok)
    total = len(required)

    print("\n--- 사전 점검 ---\n")
    for row in rows:
        if not row.required:
            tag = "참고"
        else:
            tag = "통과" if row.ok else "필요"
        print(f"  [{tag}] {row.name}")
        if row.detail:
            print(f"         {row.detail}")
        if not row.ok and row.action:
            print(f"         → {row.action}")

    print()
    if passed == total:
        print(f"  결과: {passed}/{total} 필수 항목 통과 — 플래시·수집기 실행 가능")
    else:
        missing = [r.name for r in required if not r.ok]
        print(f"  결과: {passed}/{total} 필수 항목 통과")
        print(f"  조치: {', '.join(missing)}")
    return passed == total


def _preflight() -> bool:
    rows: List[_PreflightRow] = []

    ok_cfg, msg_cfg = _check_config()
    rows.append(
        _PreflightRow(
            "호스트 설정 (meshsense_config.json)",
            ok_cfg,
            "있음" if ok_cfg else msg_cfg.replace("OK: ", ""),
            "cp scripts/meshsense_config.example.json scripts/meshsense_config.json",
        )
    )

    idf_export = REPO_ROOT / "esp-idf" / "export.sh"
    if not idf_export.is_file():
        rows.append(
            _PreflightRow(
                "ESP-IDF 소스 (esp-idf/)",
                False,
                "submodule 없음",
                "git submodule update --init esp-idf",
            )
        )
        rows.append(
            _PreflightRow(
                "ESP-IDF 빌드 (idf.py)",
                False,
                "선행 항목 실패",
                "python scripts/idf_bootstrap.py -y",
            )
        )
    else:
        rows.append(_PreflightRow("ESP-IDF 소스 (esp-idf/)", True, "export.sh 있음"))
        try:
            from idf_env import idf_py_works  # noqa: WPS433

            if idf_py_works(REPO_ROOT):
                rows.append(_PreflightRow("ESP-IDF 빌드 (idf.py)", True, "동작 확인"))
            else:
                rows.append(
                    _PreflightRow(
                        "ESP-IDF 빌드 (idf.py)",
                        False,
                        "툴체인·venv 미준비",
                        "python scripts/idf_bootstrap.py -y",
                    )
                )
        except Exception as exc:
            rows.append(
                _PreflightRow(
                    "ESP-IDF 빌드 (idf.py)",
                    False,
                    f"검사 오류 ({exc})",
                    "python scripts/idf_bootstrap.py -y",
                )
            )

    from registry import load_registry  # noqa: WPS433
    from tx_registry import load_tx_registry  # noqa: WPS433

    rows.append(_registry_row("TX registry", TX_REGISTRY, load_fn=load_tx_registry))
    rows.append(_registry_row("RX registry", DEVICE_REGISTRY, load_fn=load_registry))

    if SESSION_META.is_file():
        rows.append(_PreflightRow("session_meta.yaml", True, str(SESSION_META.name)))
    else:
        rows.append(
            _PreflightRow(
                "session_meta.yaml",
                False,
                "없음 (수집 run ID)",
                f"mac_collector/ 에 session_meta.yaml 준비",
            )
        )

    if COLLECTOR_SCRIPT.is_file():
        rows.append(_PreflightRow("Mac 수집기 스크립트", True, COLLECTOR_SCRIPT.name))
    else:
        rows.append(
            _PreflightRow(
                "Mac 수집기 스크립트",
                False,
                "udp_collector_mvp.py 없음",
                "mac_collector/ 경로 확인",
            )
        )

    viz_ok, viz_detail = _viz_venv_status()
    rows.append(
        _PreflightRow(
            "후처리 venv (CSI PNG)",
            viz_ok,
            viz_detail,
            _viz_venv_bootstrap_hint(),
            required=False,
        )
    )

    if ok_cfg:
        try:
            from meshsense_config import load_meshsense_config  # noqa: WPS433

            cfg = load_meshsense_config(CONFIG_PATH)
            rows.append(
                _PreflightRow(
                    "네트워크 설정 요약",
                    True,
                    f"AP 「{cfg.ap_ssid}」 · 수집 {cfg.collector_ip}:{cfg.collector_port}",
                    required=False,
                )
            )
        except Exception as exc:
            rows.append(
                _PreflightRow(
                    "네트워크 설정 요약",
                    False,
                    f"config 파싱 실패 ({exc})",
                    "meshsense_config.json JSON·필드 확인",
                )
            )

    ports = _list_usb_ports()
    if ports:
        detail = ", ".join(ports) if len(ports) <= 3 else f"{ports[0]} 외 {len(ports) - 1}개"
        rows.append(
            _PreflightRow(
                "USB 시리얼 (참고)",
                True,
                f"{len(ports)}개 — {detail}",
                required=False,
            )
        )
    else:
        rows.append(
            _PreflightRow(
                "USB 시리얼 (참고)",
                True,
                "연결된 ESP32 없음 (플래시 시 USB 연결)",
                required=False,
            )
        )

    return _print_preflight_report(rows)


def _port_busy_hint(port: str) -> str:
    """포트 점유 진단 + 해결 힌트. lsof 으로 잡고 있는 프로세스 한 줄 추출."""
    holder = ""
    try:
        out = subprocess.run(
            ["lsof", "-t", port],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout.strip()
        if out:
            pids = out.splitlines()
            holder = f" (점유 PID: {', '.join(pids)})"
    except (OSError, subprocess.TimeoutExpired):
        pass
    return (
        f"\n[안내] {port} 가 다른 프로세스에 잡혀 있습니다{holder}.\n"
        f"  ① 가장 흔한 원인: 이전에 열어둔 'idf.py monitor' 창 — 그 터미널에서 Ctrl+]\n"
        f"  ② csi_serial_reader.py 가 백그라운드에 떠 있는지: ps aux | grep csi_serial_reader\n"
        f"  ③ 점유 프로세스 확인: lsof {port}\n"
        f"  ④ 강제 종료: kill <PID> (안 되면 kill -9 <PID>)\n"
        f"  포트 점유 해제 후 다시 시도하세요."
    )


def _read_usb_mac(port: str) -> str:
    from esptool_mac import read_mac  # noqa: WPS433

    try:
        return read_mac(port)
    except RuntimeError as exc:
        # 포트 점유는 두 번째 호출(cwd 바꿔서)로도 해결 안 되니 즉시 안내.
        msg = str(exc)
        if "Resource temporarily unavailable" in msg or "port is busy" in msg.lower():
            raise RuntimeError(msg + _port_busy_hint(port)) from exc
        # 그 외 오류는 기존처럼 두 번째 경로로 재시도 (드물게 cwd 의존 케이스)
        return read_mac(port, cwd=str(RX_PROJECT))


def _lookup_board_by_mac(mac: str) -> Tuple[Optional[object], Optional[object]]:
    """(tx_record|None, rx_device_record|None)."""
    from registry import DeviceRecord, lookup_by_mac  # noqa: WPS433
    from tx_registry import TxRecord, lookup_tx_by_mac  # noqa: WPS433

    tx_rec: Optional[TxRecord] = None
    rx_rec: Optional[DeviceRecord] = None
    if TX_REGISTRY.is_file():
        try:
            tx_rec = lookup_tx_by_mac(mac, TX_REGISTRY)
        except (FileNotFoundError, ValueError):
            pass
    if DEVICE_REGISTRY.is_file():
        try:
            rx_rec = lookup_by_mac(mac, DEVICE_REGISTRY)
        except (FileNotFoundError, ValueError):
            pass
    return tx_rec, rx_rec


def _describe_board(mac: str) -> str:
    tx_rec, rx_rec = _lookup_board_by_mac(mac)
    if tx_rec and rx_rec:
        return (
            f"MAC {mac} — TX·RX 양쪽 registry에 등록됨 "
            f"(TX{tx_rec.tx_node_id}, RX{rx_rec.device_id})"
        )
    if tx_rec:
        return f"MAC {mac} → TX {tx_rec.board_name} (tx_node_id={tx_rec.tx_node_id})"
    if rx_rec:
        return f"MAC {mac} → RX {rx_rec.board_name} (device_id={rx_rec.device_id})"
    return f"MAC {mac} — registry 미등록 (device_registry.csv / tx_registry.csv)"


def _resolve_board_kind(mac: str) -> Optional[BoardKind]:
    tx_rec, rx_rec = _lookup_board_by_mac(mac)
    if tx_rec and not rx_rec:
        return "tx"
    if rx_rec and not tx_rec:
        return "rx"
    if tx_rec and rx_rec:
        print(f"\n[안내] {mac} 이(가) TX·RX registry 모두에 있습니다.")
        idx = _choose("플래시 대상", [f"TX — {tx_rec.board_name}", f"RX — {rx_rec.board_name}"])
        return "tx" if idx == 0 else "rx"
    return None


def _flash_board(*, kind: Optional[BoardKind] = None) -> bool:
    """USB MAC → CSV registry 조회 후 TX/RX 펌웨어 플래시. 성공 시 True."""
    print("\n--- 보드 플래시 ---")
    print("  USB 보드 1대 연결 권장. MAC으로 tx_registry / device_registry 를 조회합니다.")
    port = _pick_port()
    if not port:
        print("취소되었습니다.")
        return False

    try:
        mac = _read_usb_mac(port)
    except RuntimeError as exc:
        print(f"\n[실패] MAC 읽기: {exc}")
        return False

    print(f"  {_describe_board(mac)}")
    resolved = kind or _resolve_board_kind(mac)
    if resolved is None:
        print("\n[안내] registry에 없습니다. 메인 메뉴 [3] 보드 관리 → 등록 후 다시 플래시하세요.")
        if _ask_yes_no("지금 보드 관리(등록)로 이동할까요?", default_no=False):
            _menu_board_management()
        return False

    flash_script = SCRIPT_DIR / ("flash_tx.py" if resolved == "tx" else "flash_rx.py")
    label = "TX/AP" if resolved == "tx" else "RX CSI"
    print(f"\n  → {label} 펌웨어 플래시 ({flash_script.name})")
    extra: List[str] = ["-p", port, "-y"]
    if _ask_yes_no("플래시 후 시리얼 모니터를 열까요?", default_no=True):
        extra.append("--monitor")
    if _ask_yes_no("빌드 캐시를 지우고 fullclean 할까요? (느림, 문제 있을 때만)", default_no=True):
        extra.append("--clean")

    rc = _run_python(flash_script, extra)
    if rc != 0:
        print(f"\n[실패] 종료 코드 {rc}")
        print("  보드 관리 메뉴에서 registry·설정을 확인하세요.")
        return False
    print("\n[완료] 플래시 성공")
    return True


def _print_board_table(kind: BoardKind, records: Sequence[object]) -> None:
    from flash_state import flash_symbol, prune_flash_state  # noqa: WPS433

    if kind == "tx":
        tx_ids = {r.tx_node_id for r in records}
        prune_flash_state(tx_ids=tx_ids, rx_ids=set())
        print(f"  {'id':>3}  {'board':<8}  {'chip_mac':<17}  {'플래시':^8}  notes")
        print("  " + "-" * 58)
        for rec in sorted(records, key=lambda r: r.tx_node_id):
            note = rec.notes[:32] + ("…" if len(rec.notes) > 32 else "")
            sym = flash_symbol("tx", rec.tx_node_id)
            print(
                f"  {rec.tx_node_id:3d}  {rec.board_name:<8}  {rec.chip_mac:<17}  "
                f"{sym:^8}  {note}"
            )
    else:
        rx_ids = {r.device_id for r in records}
        prune_flash_state(tx_ids=set(), rx_ids=rx_ids)
        print(f"  {'id':>4}  {'board':<8}  {'sta_mac':<17}  {'플래시':^8}  notes")
        print("  " + "-" * 60)
        for rec in sorted(records, key=lambda r: r.device_id):
            note = rec.notes[:32] + ("…" if len(rec.notes) > 32 else "")
            sym = flash_symbol("rx", rec.device_id)
            print(
                f"  {rec.device_id:4d}  {rec.board_name:<8}  {rec.sta_mac:<17}  "
                f"{sym:^8}  {note}"
            )
    print("  (● 플래시됨  ○ 미플래시)")


def _board_list_all() -> None:
    from registry import load_registry  # noqa: WPS433
    from tx_registry import load_tx_registry  # noqa: WPS433

    print("\n--- TX (tx_registry.csv) ---")
    try:
        tx_records = load_tx_registry(TX_REGISTRY) if TX_REGISTRY.is_file() else []
        if not tx_records:
            print("  (비어 있음)")
        else:
            _print_board_table("tx", tx_records)
    except (FileNotFoundError, ValueError) as exc:
        print(f"  (없음 또는 오류: {exc})")

    print("\n--- RX (device_registry.csv) ---")
    try:
        rx_records = load_registry(DEVICE_REGISTRY) if DEVICE_REGISTRY.is_file() else []
        if not rx_records:
            print("  (비어 있음)")
        else:
            _print_board_table("rx", rx_records)
    except (FileNotFoundError, ValueError) as exc:
        print(f"  (없음 또는 오류: {exc})")


def _board_show_interactive() -> None:
    raw = input("\n  device_id / tx_node_id 또는 MAC (빈칸=취소): ").strip()
    if not raw:
        return
    from device_registry import cmd_show as rx_show  # noqa: WPS433
    from registry import lookup_by_device_id, lookup_by_mac, normalize_mac  # noqa: WPS433
    from tx_registry import _cmd_show as tx_show, lookup_tx_by_mac, lookup_tx_by_node_id  # noqa: WPS433

    if raw.isdigit():
        num = int(raw)
        tx_rec = lookup_tx_by_node_id(num, TX_REGISTRY) if TX_REGISTRY.is_file() else None
        rx_rec = lookup_by_device_id(num, DEVICE_REGISTRY) if DEVICE_REGISTRY.is_file() else None
        if tx_rec and rx_rec:
            print("\n[TX]")
            tx_show(TX_REGISTRY, num, None)
            print("\n[RX]")
            rx_show(DEVICE_REGISTRY, num, None)
            return
        if tx_rec:
            tx_show(TX_REGISTRY, num, None)
            return
        if rx_rec:
            rx_show(DEVICE_REGISTRY, num, None)
            return
        print(f"  id {num} 을(를) 찾지 못했습니다.")
        return

    try:
        mac = normalize_mac(raw)
    except ValueError as exc:
        print(f"  {exc}")
        return
    tx_rec, rx_rec = _lookup_board_by_mac(mac)
    if tx_rec:
        print("\n[TX]")
        tx_show(TX_REGISTRY, None, mac)
    if rx_rec:
        print("\n[RX]")
        rx_show(DEVICE_REGISTRY, None, mac)
    if not tx_rec and not rx_rec:
        print(f"  MAC {mac} — registry에 없습니다.")


def _board_add_interactive() -> None:
    from device_registry import cmd_add as rx_add  # noqa: WPS433
    from tx_registry import _cmd_add as tx_add  # noqa: WPS433

    idx = _choose("등록 대상", ["TX/AP (tx_registry.csv)", "RX CSI (device_registry.csv)"])
    kind: BoardKind = "tx" if idx == 0 else "rx"
    port = _pick_port()
    mac: Optional[str] = None
    if not port:
        manual = input("  MAC 직접 입력 (빈칸=취소): ").strip()
        if not manual:
            print("취소되었습니다.")
            return
        mac = manual

    raw_id = input("  ID (Enter=자동 배정): ").strip()
    node_id: Optional[int] = int(raw_id) if raw_id.isdigit() else None
    board_name = input("  board_name (Enter=자동 TXn/RXn): ").strip() or None
    notes = input("  notes (Enter=비움): ").strip()

    if kind == "tx":
        rc = tx_add(
            TX_REGISTRY,
            port=port,
            mac=mac,
            tx_node_id=node_id,
            board_name=board_name,
            notes=notes,
        )
    else:
        rc = rx_add(
            DEVICE_REGISTRY,
            port=port,
            mac=mac,
            device_id=node_id,
            board_name=board_name,
            notes=notes,
        )
    if rc == 0:
        print("  등록 후 메인 메뉴 [2] 플래시 로 펌웨어를 올릴 수 있습니다.")


def _board_remove_interactive() -> None:
    from device_registry import cmd_remove as rx_remove  # noqa: WPS433
    from tx_registry import _cmd_remove as tx_remove, load_tx_registry  # noqa: WPS433
    from registry import load_registry  # noqa: WPS433

    entries: List[Tuple[str, int, str, str]] = []
    if TX_REGISTRY.is_file():
        try:
            for rec in load_tx_registry(TX_REGISTRY):
                entries.append(("TX", rec.tx_node_id, rec.board_name, rec.chip_mac))
        except (FileNotFoundError, ValueError):
            pass
    if DEVICE_REGISTRY.is_file():
        try:
            for rec in load_registry(DEVICE_REGISTRY):
                entries.append(("RX", rec.device_id, rec.board_name, rec.sta_mac))
        except (FileNotFoundError, ValueError):
            pass
    if not entries:
        print("\n  삭제할 항목이 없습니다.")
        return

    print()
    labels = [f"{kind} id={num_id:>3}  {name:<8}  {mac}" for kind, num_id, name, mac in entries]
    idx = _choose("삭제할 보드", labels + ["취소"])
    if idx >= len(entries):
        return
    kind, num_id, name, mac = entries[idx]
    print(f"\n  선택: {kind} id={num_id} ({name}, {mac})")
    if kind == "TX":
        tx_remove(TX_REGISTRY, num_id, force=_ask_yes_no("삭제 확인", default_no=True))
    else:
        rx_remove(DEVICE_REGISTRY, num_id, force=_ask_yes_no("삭제 확인", default_no=True))


def _board_verify_all() -> None:
    from device_registry import cmd_verify as rx_verify  # noqa: WPS433
    from flash_state import count_flashed, flash_symbol, prune_flash_state  # noqa: WPS433
    from registry import load_registry  # noqa: WPS433
    from tx_registry import _cmd_verify as tx_verify, load_tx_registry  # noqa: WPS433

    tx_records: List[object] = []
    rx_records: List[object] = []

    print("\n--- TX registry ---")
    tx_ok = tx_verify(TX_REGISTRY) == 0 if TX_REGISTRY.is_file() else None
    if tx_ok is None:
        print(f"  파일 없음: {TX_REGISTRY}")
    elif tx_ok:
        try:
            tx_records = load_tx_registry(TX_REGISTRY)
            tx_ids = {r.tx_node_id for r in tx_records}
            prune_flash_state(tx_ids=tx_ids, rx_ids=set())
            for rec in sorted(tx_records, key=lambda r: r.tx_node_id):
                sym = flash_symbol("tx", rec.tx_node_id)
                print(f"  {sym} id={rec.tx_node_id}  {rec.board_name}  {rec.chip_mac}")
            done, total = count_flashed("tx", tx_ids)
            print(f"  플래시: {done}/{total}")
        except (FileNotFoundError, ValueError) as exc:
            print(f"  목록 표시 실패: {exc}")

    print("\n--- RX registry ---")
    rx_ok = rx_verify(DEVICE_REGISTRY) == 0 if DEVICE_REGISTRY.is_file() else None
    if rx_ok is None:
        print(f"  파일 없음: {DEVICE_REGISTRY}")
    elif rx_ok:
        try:
            rx_records = load_registry(DEVICE_REGISTRY)
            rx_ids = {r.device_id for r in rx_records}
            prune_flash_state(tx_ids=set(), rx_ids=rx_ids)
            for rec in sorted(rx_records, key=lambda r: r.device_id):
                sym = flash_symbol("rx", rec.device_id)
                print(f"  {sym} id={rec.device_id}  {rec.board_name}  {rec.sta_mac}")
            done, total = count_flashed("rx", rx_ids)
            print(f"  플래시: {done}/{total}")
        except (FileNotFoundError, ValueError) as exc:
            print(f"  목록 표시 실패: {exc}")

    print("  (● 플래시됨  ○ 미플래시)")
    if tx_ok is False or rx_ok is False:
        print("\n[결과] 일부 registry 검증 실패")
    elif tx_ok is None and rx_ok is None:
        print("\n[결과] registry 파일 없음")
    else:
        print("\n[결과] registry 검증 OK")


def _flash_poc_board(*, kind: Optional[BoardKind] = None) -> bool:
    """esp-csi PoC 펌웨어(esp32s3_csi_send_poc / esp32s3_csi_recv_poc) 플래시.

    기존 device_registry / tx_registry 의 MAC 매핑은 그대로 쓰지만, 플래시 대상
    프로젝트만 PoC 디렉터리로 교체한다. (flash_rx.py / flash_tx.py 는 production
    펌웨어 전용이라 우회.)
    """
    from idf_env import run_in_idf_shell  # noqa: WPS433

    print("\n--- [PoC] 보드 플래시 (esp-csi 베이스) ---")
    print("  대상: " + (f"{kind.upper()} 전용" if kind else "MAC 자동 매칭"))
    for p in (SEND_POC_PROJECT, RECV_POC_PROJECT):
        if not (p / "CMakeLists.txt").is_file():
            print(f"[중단] PoC 프로젝트 없음: {p}")
            return False

    port = _pick_port()
    if not port:
        print("취소되었습니다.")
        return False

    try:
        mac = _read_usb_mac(port)
    except RuntimeError as exc:
        print(f"\n[실패] MAC 읽기: {exc}")
        return False

    print(f"  {_describe_board(mac)}")
    resolved = kind or _resolve_board_kind(mac)
    if resolved is None:
        print("\n[안내] registry에 없습니다. 메인 메뉴 [3] 보드 관리에서 등록하세요.")
        return False

    project = SEND_POC_PROJECT if resolved == "tx" else RECV_POC_PROJECT
    label = "csi_send_poc (TX)" if resolved == "tx" else "csi_recv_poc (RX)"
    print(f"\n  → {label} 플래시 (project: {project.name}, port: {port})")

    if _ask_yes_no("빌드 캐시를 지우고 fullclean 할까요? (느림)", default_no=True):
        print("\n[실행] idf.py fullclean")
        rc = run_in_idf_shell(["idf.py", "fullclean"], cwd=project, check=False)
        if rc.returncode != 0:
            print("[경고] fullclean 실패 — 빌드는 계속 시도합니다.")

    # monitor 옵션:
    # - TX: ESP_LOG로 부팅·송신 상태 확인용으로 유용 (기본 yes)
    # - RX: PoC 펌웨어가 USB-Serial-JTAG로 바이너리 프레임을 흘리므로 monitor에서는 깨진 글자만 보임.
    #       reader가 같은 포트를 점유해야 하므로 monitor는 닫아둬야 함. (기본 no, 경고 포함)
    if resolved == "tx":
        do_monitor = _ask_yes_no("플래시 후 시리얼 모니터를 열까요? (TX 부팅 로그 확인용)", default_no=False)
    else:
        print("\n  [주의] RX는 monitor를 열지 마세요 — 바이너리 스트림이 깨진 글자로 보이고")
        print("         메뉴 [10-2] 수집 시 reader가 같은 포트를 점유해야 합니다.")
        do_monitor = _ask_yes_no("그래도 monitor를 열까요? (디버그 용도만)", default_no=True)

    flash_args = ["idf.py", "-p", port, "flash"]
    if do_monitor:
        flash_args.append("monitor")
    print(f"\n[실행] {' '.join(flash_args)} (project: {project.name})")
    try:
        rc = run_in_idf_shell(
            flash_args,
            cwd=project,
            check=False,
        )
    except KeyboardInterrupt:
        print("\n[중단] Ctrl+C")
        return False
    if rc.returncode != 0:
        print(f"\n[실패] 종료 코드 {rc.returncode}")
        return False
    print("\n[완료] PoC 플래시 성공")
    if resolved == "rx":
        print("  → 메뉴 [10-2] 수집으로 데이터 받기 (USB 시리얼)")
    else:
        print("  → TX는 USB 연결만 유지하면 100Hz로 ESP-NOW 송신 시작")
        print("    monitor 띄워두면 부팅 후 'csi_send: wifi_channel: 11, send_frequency: 100' 확인 가능")
    return True


def _read_session_id_from_yaml() -> int:
    if not SESSION_META.is_file():
        return 1
    try:
        text = SESSION_META.read_text(encoding="utf-8")
    except OSError:
        return 1
    m = re.search(r"^session_id:\s*(\d+)\s*$", text, re.MULTILINE)
    return int(m.group(1)) if m else 1


def _detect_rx_boards() -> List[Tuple[str, int]]:
    """현재 USB 포트들에서 MAC 읽고 RX registry와 매칭된 (port, device_id) 만 반환."""
    from registry import lookup_by_mac  # noqa: WPS433

    found: List[Tuple[str, int]] = []
    if not DEVICE_REGISTRY.is_file():
        print(f"[경고] RX registry 없음: {DEVICE_REGISTRY}")
        return found

    for port in _list_usb_ports():
        try:
            mac = _read_usb_mac(port)
        except RuntimeError as exc:
            print(f"  {port}: MAC 읽기 실패 ({exc})")
            continue
        try:
            rec = lookup_by_mac(mac, DEVICE_REGISTRY)
        except (FileNotFoundError, ValueError):
            rec = None
        if rec is None:
            print(f"  {port}: MAC {mac} — RX registry에 없음 (TX 또는 미등록, 건너뜀)")
            continue
        print(f"  {port}: MAC {mac} → RX device_id={rec.device_id} ({rec.board_name})")
        found.append((port, rec.device_id))
    return found


def _tee_subprocess_lines(proc: "subprocess.Popen[bytes]", log_fp) -> None:
    """proc의 stdout(stderr 병합)을 라인 단위로 읽어 터미널 + log_fp에 동시 출력 (tee).

    부모 콘솔에는 reader가 이미 '[reader devN] ...' prefix를 붙여 출력하므로 그대로 흘림.
    스레드 종료 조건: proc 가 끝나 pipe EOF.
    """
    if proc.stdout is None:
        return
    for line in iter(proc.stdout.readline, b""):
        try:
            log_fp.write(line)
            log_fp.flush()
        except OSError:
            pass
        try:
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            pass


def _collect_poc_interactive() -> bool:
    """USB 시리얼로 연결된 RX 보드들에서 csi_serial_reader.py 병렬 실행."""
    import signal as _signal
    import time as _time

    print("\n--- [PoC] USB 시리얼 수집 ---")
    if not SERIAL_READER_SCRIPT.is_file():
        print(f"[중단] reader 스크립트 없음: {SERIAL_READER_SCRIPT}")
        return False

    print("  연결된 USB 보드 스캔 + RX registry 매칭…")
    boards = _detect_rx_boards()
    if not boards:
        print("\n[중단] 수집 대상 RX 보드를 찾지 못했습니다.")
        print("  ① RX 보드가 USB로 연결되어 있고 PoC 펌웨어가 플래시되었는지 확인")
        print("  ② mac_collector/device_registry.csv 에 등록되어 있는지 확인")
        return False

    session_id = _read_session_id_from_yaml()
    print(f"\n  session_id = {session_id} (session_meta.yaml)")

    duration_sec = _ask_collect_duration_sec()

    POC_LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = _time.strftime("%Y%m%d_%H%M%S")
    import threading

    # (device_id, proc, log_path, log_fp, tee_thread)
    procs: List[Tuple[int, "subprocess.Popen[bytes]", Path, object, threading.Thread]] = []

    for port, device_id in boards:
        log_path = POC_LOG_DIR / f"reader_session{session_id}_dev{device_id}_{timestamp}.log"
        cmd = [
            sys.executable,
            str(SERIAL_READER_SCRIPT),
            "--port", port,
            "--device-id", str(device_id),
            "--session-id", str(session_id),
            "--output-dir", str(OUTPUT_DIR),
        ]
        print(f"\n[실행] dev{device_id} ({port}) → log: {log_path}")
        print("       " + " ".join(cmd))
        log_fp = log_path.open("wb")
        # PIPE로 받아 tee 스레드가 터미널 + 로그 동시 출력
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            cwd=str(REPO_ROOT),
        )
        thr = threading.Thread(
            target=_tee_subprocess_lines,
            args=(proc, log_fp),
            daemon=True,
        )
        thr.start()
        procs.append((device_id, proc, log_path, log_fp, thr))

    if duration_sec > 0:
        print(f"\n[안내] {duration_sec:.0f}초 후 자동 종료 (중간 Ctrl+C 도 가능)")
    else:
        print("\n[안내] 종료: Ctrl+C")
    print("=" * 60)
    print("  ↓↓ reader 실시간 출력 (터미널 + log/ 동시 기록) ↓↓")
    print("=" * 60)

    start = _time.monotonic()
    try:
        if duration_sec > 0:
            # 단순 sleep — tee 스레드가 실시간 stdout/log 모두 처리
            # reader가 조기 종료하면 빨리 빠져나오기 위해 0.5초 단위 폴링
            deadline = start + duration_sec
            while _time.monotonic() < deadline:
                if any(p.poll() is not None for _, p, _, _, _ in procs):
                    print("\n[경고] reader 중 일부가 조기 종료됨")
                    break
                _time.sleep(0.5)
        else:
            # 무한 대기: reader 중 하나라도 죽으면 종료
            while all(p.poll() is None for _, p, _, _, _ in procs):
                _time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[중단] Ctrl+C — reader 종료 중…")

    print("\n" + "=" * 60)
    print("  reader 종료 신호 송신")
    print("=" * 60)
    for device_id, proc, _, _, _ in procs:
        if proc.poll() is None:
            try:
                proc.send_signal(_signal.SIGINT)
            except ProcessLookupError:
                pass

    elapsed = _time.monotonic() - start
    print(f"  {elapsed:.1f}초 수집됨. reader 정리 대기…")
    for device_id, proc, log_path, log_fp, thr in procs:
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            print(f"  dev{device_id}: 응답 없음 → SIGTERM")
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        # tee 스레드가 pipe EOF로 자연 종료할 때까지 대기
        thr.join(timeout=3)
        try:
            log_fp.close()
        except OSError:
            pass
        rc = proc.returncode
        print(f"  dev{device_id} 종료 (rc={rc}, log: {log_path.name})")

    print(f"\n[완료] 출력 디렉터리: {OUTPUT_DIR}/raw/$(date)/session_{session_id}/")
    print(f"       reader 로그: {POC_LOG_DIR}/reader_session{session_id}_*_{timestamp}.log")
    return True


def _menu_poc() -> None:
    """[10] 새로운 버전 — esp-csi 베이스 PoC (USB 시리얼 100Hz 파이프라인)."""
    while True:
        print("\n--- [10] 새로운 버전 (esp-csi PoC) ---")
        print(f"  TX 프로젝트: {SEND_POC_PROJECT.name}")
        print(f"  RX 프로젝트: {RECV_POC_PROJECT.name}")
        print(f"  reader:    {SERIAL_READER_SCRIPT.name}")
        idx = _choose(
            "선택",
            [
                "보드 플래시 (PoC, MAC 자동 매칭)",
                "수집 (USB 시리얼, 시간 입력)",
                "돌아가기",
            ],
        )
        if idx == 0:
            _flash_poc_board()
            _pause()
        elif idx == 1:
            _collect_poc_interactive()
            _pause()
        else:
            break


def _menu_board_management() -> None:
    while True:
        print("\n--- 보드 관리 (registry CRUD) ---")
        print(f"  TX: {TX_REGISTRY}")
        print(f"  RX: {DEVICE_REGISTRY}")
        idx = _choose(
            "보드 관리",
            [
                "목록 (TX + RX)",
                "상세 보기",
                "등록",
                "삭제",
                "검증",
                "돌아가기",
            ],
        )
        if idx == 0:
            _board_list_all()
            _pause()
        elif idx == 1:
            _board_show_interactive()
            _pause()
        elif idx == 2:
            _board_add_interactive()
            _pause()
        elif idx == 3:
            _board_remove_interactive()
        elif idx == 4:
            _board_verify_all()
            _pause()
        else:
            break


def _ask_collect_duration_sec() -> float:
    """수집 시간(초). 0 = 수동 종료(Ctrl+C)만."""
    while True:
        raw = input("수집 시간(초, Enter=60, 0=수동 종료): ").strip()
        if not raw:
            return 60.0
        try:
            val = float(raw)
        except ValueError:
            print("  숫자를 입력하세요.")
            continue
        if val < 0:
            print("  0 이상이어야 합니다.")
            continue
        return val


def _run_visualize_after_collect(session_id: int) -> None:
    if not VISUALIZE_SCRIPT.is_file():
        print(f"[경고] 시각화 스크립트 없음: {VISUALIZE_SCRIPT}")
        return
    venv_py = _ensure_postprocess_venv(interactive=True)
    if venv_py is None:
        print("\n[경고] CSI 워터폴 PNG 생략")
        print(f"  {_viz_venv_bootstrap_hint()}")
        print(
            "  python scripts/visualize_csi.py "
            f"--output-dir {OUTPUT_DIR} --session-id {session_id}"
        )
        return
    print("\n--- CSI 워터폴 PNG 생성 (.venv) ---")
    rc = _run_python(
        VISUALIZE_SCRIPT,
        [
            "--output-dir",
            str(OUTPUT_DIR),
            "--session-id",
            str(session_id),
        ],
        python=venv_py,
    )
    if rc != 0:
        print("[경고] PNG 생성 실패 — .venv 패키지 확인")
        print(f"  {_viz_venv_bootstrap_hint()}")


def _run_collector(*, skip_start_prompt: bool = False, skip_wifi_hint: bool = False) -> bool:
    print("\n--- Mac 수집기 ---")
    if not _ensure_config_interactive():
        return False
    try:
        from meshsense_config import load_meshsense_config  # noqa: WPS433

        cfg = load_meshsense_config(CONFIG_PATH)
    except Exception as exc:
        print(f"설정 로드 실패: {exc}")
        return False

    if not skip_wifi_hint:
        print(f"\n[안내] Mac Wi-Fi를 TX SoftAP에 연결하세요: SSID = {cfg.ap_ssid}")
        print(
            f"  수집기 IP: {cfg.collector_ip} "
            f"(확인: ipconfig getifaddr en0)"
        )
    session_id = 1
    if SESSION_META.is_file():
        try:
            text = SESSION_META.read_text(encoding="utf-8")
            m = re.search(r"^session_id:\s*(\d+)\s*$", text, re.MULTILINE)
            if m:
                session_id = int(m.group(1))
                print(f"  이번 run session_id (yaml): {session_id}")
        except OSError:
            pass

    if not skip_start_prompt and not _ask_yes_no(
        "수집기를 지금 시작할까요?",
        default_no=False,
    ):
        return False

    duration_sec = _ask_collect_duration_sec()
    args = [
        "--host",
        "0.0.0.0",
        "--port",
        str(cfg.collector_port),
        "--output-dir",
        str(OUTPUT_DIR),
        "--device-registry-csv",
        str(DEVICE_REGISTRY),
        "--session-meta",
        str(SESSION_META),
    ]
    if duration_sec > 0:
        args.extend(["--duration-sec", str(duration_sec)])
        print(f"\n[안내] {duration_sec:.0f}초 후 자동 종료 (중단: Ctrl+C)")
    else:
        print("\n[안내] 종료: Ctrl+C")
    rc = _run_python(COLLECTOR_SCRIPT, args)
    if rc == 0 or rc == 130:
        if rc == 130:
            print("\n[안내] 수집기 중단됨 — 메인 메뉴로 돌아갑니다.")
        _run_visualize_after_collect(session_id)
    return rc == 0


def _guide_full() -> None:
    """전체 실험 순서 가이드."""
    _banner()
    print(
        "\n[전체 가이드 모드]\n"
        "권장 순서: 설정 → TX 플래시 → Mac Wi-Fi(IP 확인) → RX 플래시 → 수집기\n"
        "각 단계에서 건너뛰거나 중단할 수 있습니다."
    )
    _pause()

    # 0. 설정
    print("\n" + "=" * 60)
    print("  단계 0 / 4 — 호스트 설정 (meshsense_config.json)")
    print("=" * 60)
    print("  TX SoftAP·수집기 IP·포트는 이 파일이 SSOT 입니다.")
    if not _ensure_config_interactive():
        print("[중단] 설정 파일이 필요합니다.")
        return
    if _ask_yes_no("ESP-IDF bootstrap 을 지금 실행할까요? (최초 1회·오래 걸림)", default_no=True):
        _run_python(SCRIPT_DIR / "idf_bootstrap.py", ["-y"])
    if not _ask_yes_no("다음 단계(TX 플래시)로 진행할까요?", default_no=False):
        return

    # 1. TX
    print("\n" + "=" * 60)
    print("  단계 1 / 4 — TX/AP 노드 플래시")
    print("=" * 60)
    print("  TX 보드만 USB에 연결하세요. 플래시 후 TX 전원·SoftAP를 켜 두세요.")
    if _ask_yes_no("TX 플래시를 진행할까요?", default_no=False):
        _flash_board(kind="tx")
    if not _ask_yes_no("다음 단계(Mac Wi-Fi)로 진행할까요?", default_no=False):
        return

    # 2. Mac Wi-Fi + IP
    print("\n" + "=" * 60)
    print("  단계 2 / 4 — Mac Wi-Fi (SoftAP · IP 확인)")
    print("=" * 60)
    try:
        from meshsense_config import load_meshsense_config  # noqa: WPS433

        cfg = load_meshsense_config(CONFIG_PATH)
        print(f"  Mac Wi-Fi에서 SSID 「{cfg.ap_ssid}」 로 TX SoftAP에 접속하세요.")
        print(f"  자동 확인: ipconfig getifaddr en0/en1 == collector.ip ({cfg.collector_ip})")
        if not _verify_mac_softap_ip(expected_ip=cfg.collector_ip, ap_ssid=cfg.ap_ssid):
            return
    except Exception as exc:
        print(f"  meshsense_config.json 오류: {exc}")
        return

    if not _ask_yes_no("다음 단계(RX 플래시)로 진행할까요?", default_no=False):
        return

    # 3. RX loop
    print("\n" + "=" * 60)
    print("  단계 3 / 4 — RX 노드 플래시 (보드별 반복)")
    print("=" * 60)
    print("  RX는 USB로 하나씩 연결해 플래시합니다. collector.ip 는 위 Wi-Fi 단계에서 확인한 값입니다.")
    while True:
        if not _ask_yes_no("RX 보드 1대를 플래시할까요?", default_no=False):
            break
        _flash_board()
        if not _ask_yes_no("다른 RX 보드도 더 플래시할까요?", default_no=True):
            break

    if not _ask_yes_no("다음 단계(수집기 실행)로 진행할까요?", default_no=False):
        return

    # 4. Collector
    print("\n" + "=" * 60)
    print("  단계 4 / 4 — 수집기 실행")
    print("=" * 60)
    print("  모든 RX가 TX SoftAP에 붙은 뒤 수집을 시작하세요.")
    print("  종료: Ctrl+C (CLI는 메인 메뉴로 돌아갑니다)")
    if _ask_yes_no("수집기를 지금 시작할까요?", default_no=False):
        _run_collector(skip_start_prompt=True, skip_wifi_hint=True)
    else:
        print("  나중에 메인 메뉴 [4] 수집기 실행 으로 시작할 수 있습니다.")

    print("\n" + "=" * 60)
    print("  전체 가이드 종료")
    print("=" * 60)
    print("  데이터: mac_collector_output/raw/YYYYMMDD/session_<id>/")
    print("  후처리: doc/postprocessing/pipeline.md 참고")
    _pause()


def _main_menu(quick: bool) -> None:
    while True:
        _banner()
        if not quick:
            print(
                "\n메인 메뉴\n"
                "  실험 처음이면 [1] 전체 가이드를 권장합니다."
            )
        options = [
            "전체 가이드 (설정 → TX → Wi-Fi → RX → 수집)",
            "보드 플래시 (USB · MAC → TX/RX 자동)",
            "보드 관리 (registry 등록·검증)",
            "수집기 실행",
            "사전 점검",
            "[NEW] esp-csi PoC (100Hz USB 시리얼 파이프라인)",
            "종료",
        ]
        idx = _choose("선택", options)
        if idx == 0:
            _guide_full()
        elif idx == 1:
            _flash_board()
        elif idx == 2:
            _menu_board_management()
        elif idx == 3:
            _run_collector()
        elif idx == 4:
            _preflight()
            _pause()
        elif idx == 5:
            _menu_poc()
        else:
            print("\n종료합니다.")
            break


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MeshSense 터미널 가이드 — 플래시·수집기 실행·전체 실험 순서",
    )
    parser.add_argument(
        "--quick",
        action="store_true",
        help="전체 가이드 없이 메인 메뉴만 표시",
    )
    parser.add_argument(
        "--guide",
        action="store_true",
        help="메인 메뉴 없이 전체 가이드 바로 시작",
    )
    return parser.parse_args()


def main() -> int:
    if not (REPO_ROOT / "mac_collector").is_dir():
        print(f"error: repo root 로 보이지 않습니다: {REPO_ROOT}", file=sys.stderr)
        return 1

    args = _parse_args()
    try:
        if args.guide:
            _guide_full()
        else:
            _main_menu(quick=args.quick)
    except KeyboardInterrupt:
        print("\n\n[중단] Ctrl+C — 메뉴를 종료합니다.")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
