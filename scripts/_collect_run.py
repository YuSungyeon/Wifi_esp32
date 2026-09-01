#!/usr/bin/env python3
"""세션 생성 → 포트별 reader 병렬 실행 → 매니페스트 마감 → 워터폴 PNG (GUI 용).

    python scripts/_collect_run.py <label> <duration_sec>

CLI 의 대화형 수집과 같은 절차를 비대화형으로 수행한다. 포트를 프로브하지 않고 모든
시리얼 포트에 reader 를 붙인 뒤, IDENT 가 오지 않는 포트(TX·미등록)는 스스로 빠지게 둔다.
"""
import glob
import subprocess
import sys
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from csi_session import create_session, finalize_session, next_session_id, summarize  # noqa: E402

READER = REPO_ROOT / "scripts" / "csi_serial_reader.py"
VISUALIZE = REPO_ROOT / "scripts" / "visualize_csi.py"
SESSION_META = REPO_ROOT / "mac_collector" / "session_meta.yaml"
OUTPUT_DIR = REPO_ROOT / "mac_collector_output"
RC_NOTE = {0: "정상", 2: "RX 아님 — 제외", 3: "스트림 정지(보드 확인 필요)", 4: "파일 충돌"}


def main() -> int:
    label, duration = sys.argv[1], float(sys.argv[2])
    ports = sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))
    if not ports:
        print("[중단] USB 시리얼 포트를 찾지 못했습니다.")
        return 1

    session_id = next_session_id(OUTPUT_DIR)
    sd = create_session(OUTPUT_DIR, label=label, session_id=session_id, session_meta=SESSION_META)
    print(f"[세션] {sd.relative_to(REPO_ROOT)}  (label={label}, session_id={session_id})", flush=True)
    print(f"[포트] {', '.join(ports)}", flush=True)

    procs = []
    for port in ports:
        p = subprocess.Popen(
            [sys.executable, str(READER), "--port", port, "--session-dir", str(sd),
             "--duration", str(duration), "--stats-every", "1000"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            cwd=str(REPO_ROOT))
        procs.append((port, p))
        threading.Thread(target=lambda pr=p: [print(l.rstrip(), flush=True) for l in pr.stdout],
                         daemon=True).start()

    collected = 0
    for port, p in procs:
        rc = p.wait()
        collected += rc == 0
        print(f"  {port}: rc={rc} ({RC_NOTE.get(rc, '오류')})", flush=True)

    manifest = finalize_session(sd)
    print("\n[요약]", flush=True)
    for line in summarize(manifest):
        print("  " + line, flush=True)

    if collected == 0:
        print("\n[경고] 수집된 RX 보드가 없습니다.")
        print("  ① RX 보드에 최신 펌웨어가 플래시되었는지")
        print("  ② device_registry.csv 에 보드 MAC 이 등록되어 있는지 확인하세요.")
        return 1

    print("\n[시각화] 워터폴 PNG 생성…", flush=True)
    subprocess.run([sys.executable, str(VISUALIZE), "--session-dir", str(sd)], cwd=str(REPO_ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
