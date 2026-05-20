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
from typing import List, Optional, Sequence, Tuple

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


def _run_python(script: Path, args: List[str], *, cwd: Optional[Path] = None) -> int:
    cmd = [sys.executable, str(script), *args]
    print("\n[실행]", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(cwd or REPO_ROOT))
    return proc.returncode


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


def _preflight() -> bool:
    print("\n--- 사전 점검 ---")
    all_ok = True

    ok_cfg, msg_cfg = _check_config()
    print(f"  설정: {msg_cfg}")
    all_ok = all_ok and ok_cfg

    idf_export = REPO_ROOT / "esp-idf" / "export.sh"
    if not idf_export.is_file():
        print(f"  ESP-IDF 소스: 없음 ({idf_export})")
        print("    → git submodule update --init esp-idf")
        all_ok = False
    else:
        try:
            from idf_env import idf_py_works  # noqa: WPS433

            if idf_py_works(REPO_ROOT):
                print("  ESP-IDF: OK (idf.py 동작)")
            else:
                print("  ESP-IDF: export.sh 있음, idf.py 미동작")
                print("    → python scripts/idf_bootstrap.py -y")
                print("    → doc/overview/esp-idf-troubleshooting.md")
                all_ok = False
        except Exception as exc:
            print(f"  ESP-IDF: 검사 실패 ({exc})")
            all_ok = False

    for name, path in [
        ("RX registry", DEVICE_REGISTRY),
        ("TX registry", TX_REGISTRY),
        ("session_meta", SESSION_META),
    ]:
        print(f"  {name}: {'OK' if path.is_file() else '없음'} ({path})")
        all_ok = all_ok and path.is_file()

    if ok_cfg:
        try:
            from meshsense_config import load_meshsense_config  # noqa: WPS433

            cfg = load_meshsense_config(CONFIG_PATH)
            print(f"  AP SSID: {cfg.ap_ssid}")
            print(f"  수집기: {cfg.collector_ip}:{cfg.collector_port}")
        except Exception as exc:
            print(f"  config 파싱 실패: {exc}")
            all_ok = False

    return all_ok


def _flash_board(kind: str) -> bool:
    """kind: 'tx' | 'rx'. 성공 시 True."""
    print(f"\n--- {'TX/AP' if kind == 'tx' else 'RX'} 플래시 ---")
    print("  USB로 해당 보드 1대만 연결하는 것을 권장합니다.")
    port = _pick_port()
    if not port:
        print("취소되었습니다.")
        return False

    flash_script = SCRIPT_DIR / ("flash_tx.py" if kind == "tx" else "flash_rx.py")
    extra: List[str] = ["-p", port, "-y"]
    if _ask_yes_no("플래시 후 시리얼 모니터를 열까요?", default_no=True):
        extra.append("--monitor")
    if _ask_yes_no("빌드 캐시를 지우고 fullclean 할까요? (느림, 문제 있을 때만)", default_no=True):
        extra.append("--clean")

    rc = _run_python(flash_script, extra)
    if rc != 0:
        print(f"\n[실패] 종료 코드 {rc}")
        registry_cli = "tx_registry.py" if kind == "tx" else "device_registry.py"
        print(f"  registry 미등록 MAC이면: python scripts/{registry_cli} add --port {port}")
        return False
    print("\n[완료] 플래시 성공")
    return True


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
    print("\n--- CSI 워터폴 PNG 생성 ---")
    rc = _run_python(
        VISUALIZE_SCRIPT,
        [
            "--output-dir",
            str(OUTPUT_DIR),
            "--session-id",
            str(session_id),
        ],
    )
    if rc != 0:
        print("[경고] PNG 생성 실패 (JSONL·matplotlib 확인)")


def _run_collector() -> bool:
    print("\n--- Mac 수집기 ---")
    if not _ensure_config_interactive():
        return False
    try:
        from meshsense_config import load_meshsense_config  # noqa: WPS433

        cfg = load_meshsense_config(CONFIG_PATH)
    except Exception as exc:
        print(f"설정 로드 실패: {exc}")
        return False

    print(f"\n[안내] Mac Wi-Fi를 TX SoftAP에 연결하세요: SSID = {cfg.ap_ssid}")
    print(f"  수집기 IP는 보통 SoftAP 대역 (예: ipconfig getifaddr en0 → {cfg.collector_ip})")
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

    if not _ask_yes_no("수집기를 지금 시작할까요?", default_no=False):
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
        _run_visualize_after_collect(session_id)
    return rc == 0


def _menu_flash_only() -> None:
    idx = _choose("플래시 대상", ["TX/AP 노드", "RX CSI 노드", "돌아가기"])
    if idx == 0:
        _flash_board("tx")
    elif idx == 1:
        _flash_board("rx")


def _guide_full() -> None:
    """전체 실험 순서 가이드."""
    _banner()
    print(
        "\n[전체 가이드 모드]\n"
        "권장 순서: 사전 설정 → TX 플래시 → Mac Wi-Fi → 수집기 → RX 플래시(여러 대)\n"
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
    print("  TX 보드만 USB에 연결하세요.")
    if _ask_yes_no("TX 플래시를 진행할까요?", default_no=False):
        _flash_board("tx")
    if not _ask_yes_no("다음 단계(Mac 네트워크·수집기)로 진행할까요?", default_no=False):
        return

    # 2. Mac + collector prep
    print("\n" + "=" * 60)
    print("  단계 2 / 4 — Mac 네트워크")
    print("=" * 60)
    try:
        from meshsense_config import load_meshsense_config  # noqa: WPS433

        cfg = load_meshsense_config(CONFIG_PATH)
        print(f"  Mac Wi-Fi에서 SSID 「{cfg.ap_ssid}」 로 TX SoftAP에 접속하세요.")
        print(f"  터미널에서 확인: ipconfig getifaddr en0  (보통 {cfg.collector_ip} 대역)")
    except Exception:
        print("  meshsense_config.json 을 확인하세요.")
    _pause("Wi-Fi 연결 후 Enter…")

    # 3. Collector
    print("\n" + "=" * 60)
    print("  단계 3 / 4 — 수집기 실행")
    print("=" * 60)
    print("  수집기는 이 터미널을 점유합니다. Ctrl+C 로 종료합니다.")
    if _ask_yes_no("수집기를 지금 시작할까요?", default_no=False):
        _run_collector()
    else:
        print("  나중에 메인 메뉴 [3] 수집기만 으로 시작할 수 있습니다.")

    if not _ask_yes_no("다음 단계(RX 플래시)로 진행할까요?", default_no=False):
        return

    # 4. RX loop
    print("\n" + "=" * 60)
    print("  단계 4 / 4 — RX 노드 플래시 (보드별 반복)")
    print("=" * 60)
    print("  수집기가 켜진 상태에서 RX를 하나씩 플래시하세요.")
    while True:
        if not _ask_yes_no("RX 보드 1대를 플래시할까요?", default_no=False):
            break
        _flash_board("rx")
        if not _ask_yes_no("다른 RX 보드도 더 플래시할까요?", default_no=True):
            break

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
            "전체 가이드 (TX → Wi-Fi → 수집기 → RX)",
            "플래시만 (TX / RX 선택)",
            "수집기만",
            "사전 점검",
            "종료",
        ]
        idx = _choose("선택", options)
        if idx == 0:
            _guide_full()
        elif idx == 1:
            _menu_flash_only()
        elif idx == 2:
            _run_collector()
        elif idx == 3:
            ok = _preflight()
            print("\n[결과]", "준비됨" if ok else "일부 항목 확인 필요")
            _pause()
        else:
            print("\n종료합니다.")
            break


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MeshSense 터미널 가이드 — 플래시·수집기·전체 실험 순서",
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
        print("\n\n[중단] Ctrl+C")
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
