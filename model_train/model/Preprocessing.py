"""수집 세션 → tx_seq 격자 정렬 → LSTM 입력 텐서 생성.

세션 디렉터리의 `device_*.csi`(raw I/Q)를 읽어 진폭을 계산하고, tx_seq(100Hz) 공통
격자에 보간한 뒤 슬라이딩 윈도로 X=(N, WINDOW, len(rx_ids)*N_SUB), y=(N,) 을 만든다.

라벨은 **세션 매니페스트(`session.json`)** 에서 온다 — 수집 시점에 박힌 값이 정본이고,
`--label` 은 덮어쓰기 용도다. 이전에는 라벨이 이 CLI 인자에만 있었고 기본값이 `empty`라
어떤 세션이 무슨 상태였는지 데이터만 보고는 알 수 없었다.

  python model_train/model/Preprocessing.py                     # 최신 세션 자동 선택
  python model_train/model/Preprocessing.py \
      --session-dir mac_collector_output/raw/20260825/143000_static_s21

여러 세션을 묶어 학습 데이터셋을 만들 때는 build_dataset.py 를 쓴다.
"""

import argparse
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_ROOT = PROJECT_ROOT / "mac_collector_output" / "raw"

sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import csi_store as cs  # noqa: E402
from csi_session import read_manifest  # noqa: E402

F_S = 100  # Hz, 송신기가 10ms마다 tx_seq 1개 증가
WINDOW_SECONDS = 3.0
STRIDE_SECONDS = 0.3
SESSION_SECONDS = 5 * 60
WINDOW = int(F_S * WINDOW_SECONDS)  # 3초 = 300 samples
STRIDE = int(F_S * STRIDE_SECONDS)  # 0.3초 = 30 samples
MAX_SESSION_SAMPLES = int(F_S * SESSION_SECONDS)  # 세션 5분 상한 = 30000

#: 모델 입력 서브캐리어 = LLTF 유효 데이터 톤 52개.
#: 예전에는 64개 중 앞 52개를 잘랐는데, 그 안에 상시 0인 DC(0)·가드(27~37) 12개가
#: 섞여 있고 실제 데이터인 52~63은 버려졌다. 선별 로직은 csi_store 가 단일 소스다.
N_SUB = cs.N_SUB
LABEL_MAP = dict(cs.LABEL_MAP)


def find_latest_session_dir(raw_root: Path = DEFAULT_RAW_ROOT) -> Path:
    """raw/YYYYMMDD/<세션> 중 session.json 이 있는 최근 수정 디렉터리."""
    candidates = cs.find_sessions(raw_root)
    if not candidates:
        raise FileNotFoundError(
            f"매니페스트를 가진 세션이 없습니다: {raw_root}\n"
            "  구 JSONL 세션은 라벨이 없고 여러 런이 섞여 있어 학습에 쓸 수 없습니다 — 재수집이 필요합니다."
        )
    return max(candidates, key=lambda p: p.stat().st_mtime)


def load_buffers(session_dir: Path):
    """세션 → {device_id: (tx_seq[T], amp[T, 52])} (tx_seq 오름차순, 중복 제거)."""
    buffers = {}
    for dev, frames in cs.read_session(session_dir).items():
        if len(frames) == 0:
            continue
        tx = frames["hdr"]["tx_seq"].astype(np.int64)
        amp = cs.amplitude(frames)

        # TX 가 수집 중 재부팅하면 tx_seq 가 0부터 다시 시작한다. 그대로 정렬하면 재부팅
        # 이후 프레임이 세션 앞쪽으로 끌려가 시간 순서가 뒤집히고, 그 사이 수천 스텝이
        # 선형 보간으로 채워진다 — 경고 하나 없이 세션 대부분이 가짜 데이터가 된다.
        # 정렬로 덮지 말고 거부한다.
        back = int((np.diff(tx) < 0).sum())
        if back:
            where = int(np.argmax(np.diff(tx) < 0))
            raise ValueError(
                f"RX{dev}: 수집 중 TX 가 재부팅했습니다 "
                f"(tx_seq {tx[where]} → {tx[where + 1]}, {back}회). "
                f"시간 격자가 깨져 학습에 쓸 수 없습니다 — 재수집하세요: {session_dir.name}"
            )

        # 같은 tx_seq 가 두 번 잡히면(재전송 등) 뒤엣것을 버린다. np.interp 가 단조 증가를 요구.
        order = np.argsort(tx, kind="stable")
        tx, amp = tx[order], amp[order]
        keep = np.concatenate(([True], np.diff(tx) > 0))
        buffers[dev] = (tx[keep].astype(np.float64), amp[keep])
    return buffers


def resample_to_grid(buf, seq_grid):
    """RX 하나의 amplitude를 공통 seq_grid에 선형 보간. 결과 (T_common, N_SUB)."""
    seq, amp = buf
    out = np.empty((len(seq_grid), N_SUB), dtype=np.float64)
    for k in range(N_SUB):
        out[:, k] = np.interp(seq_grid, seq, amp[:, k])
    return out


def session_label(session_dir: Path, override: str | None = None) -> str:
    if override:
        return override
    return read_manifest(session_dir)["label"]


def run_preprocessing(session_dir: Path, rx_ids=None, label_name: str | None = None, *, verbose=True):
    """세션 하나를 X=(N, WINDOW, len(rx_ids)*N_SUB), y=(N,) 으로 변환."""
    session_dir = Path(session_dir)
    label_name = session_label(session_dir, label_name)
    if label_name not in LABEL_MAP:
        raise ValueError(f"unknown label {label_name!r}. expected one of {sorted(LABEL_MAP)}")
    label = LABEL_MAP[label_name]

    buffers = load_buffers(session_dir)
    rx_ids = tuple(rx_ids) if rx_ids else tuple(sorted(buffers))
    if verbose:
        print(f"[1단계] device_id별 버퍼 ({session_dir.name})")
        for dev in sorted(buffers):
            print(f"  RX{dev}: {len(buffers[dev][0])}개 프레임")

    missing = [dev for dev in rx_ids if dev not in buffers]
    if missing:
        raise ValueError(f"no data for RX {missing} in {session_dir} (available: {sorted(buffers)})")

    # tx_seq 기반 시간 동기화: 모든 RX가 겹치는 공통 seq 격자에 보간
    start_seq = int(max(buffers[d][0][0] for d in rx_ids))
    end_seq = int(min(buffers[d][0][-1] for d in rx_ids))
    end_seq = min(end_seq, start_seq + MAX_SESSION_SAMPLES - 1)
    if end_seq < start_seq:
        raise ValueError(f"RX 간 겹치는 tx_seq 구간이 없습니다: {session_dir}")
    seq_grid = np.arange(start_seq, end_seq + 1, dtype=np.float64)

    aligned = np.stack([resample_to_grid(buffers[d], seq_grid) for d in rx_ids])
    if verbose:
        print("\n[2단계] tx_seq 기반 시간 동기화 완료")
        print(f"  seq range: {start_seq} ~ {end_seq}")
        print(f"  duration: {len(seq_grid) / F_S:.3f}s")
        print(f"  aligned shape: {aligned.shape}   (RX, 시점, 서브캐리어)")

    t_common = aligned.shape[1]
    if t_common < WINDOW:
        raise ValueError(f"session too short: {t_common} samples < WINDOW={WINDOW}")

    # 윈도잉: (RX, T, N_SUB) → (N, WINDOW, len(rx_ids)*N_SUB)
    windows = []
    for start in range(0, t_common - WINDOW + 1, STRIDE):
        w = aligned[:, start : start + WINDOW, :]  # (RX, WINDOW, N_SUB)
        w = w.transpose(1, 0, 2).reshape(WINDOW, len(rx_ids) * N_SUB)
        windows.append(w)

    X = np.stack(windows)
    y = np.full(len(windows), label, dtype=np.int64)

    if verbose:
        print("\n[3단계] 윈도잉 결과")
        print(f"  X shape: {X.shape}   ← (윈도 수, 시간, feature)")
        print(f"  y shape: {y.shape}")
        print(f"  label: {label_name} -> class {label}")
        print(f"  윈도 {len(windows)}개 ({WINDOW_SECONDS}초 윈도, {STRIDE_SECONDS}초 stride)")
    return X, y


def _parse_args():
    parser = argparse.ArgumentParser(description="세션 → LSTM 입력 텐서")
    parser.add_argument("--session-dir", type=Path, default=None,
                        help=f"세션 디렉터리 (기본: {DEFAULT_RAW_ROOT} 아래 최신 세션)")
    parser.add_argument("--rx-ids", type=int, nargs="+", default=None,
                        help="사용할 RX device_id 목록 (기본: 세션에 있는 전부)")
    parser.add_argument("--label", choices=sorted(LABEL_MAP), default=None,
                        help="세션 매니페스트의 라벨을 덮어쓸 값")
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    session_dir = args.session_dir or find_latest_session_dir()
    run_preprocessing(session_dir, rx_ids=args.rx_ids, label_name=args.label)
