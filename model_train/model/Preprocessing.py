import numpy as np
import json
from collections import defaultdict
from pathlib import Path


np.set_printoptions(precision=16, floatmode="maxprec_equal", threshold=np.inf, suppress=False)

# === SESSION_DIR 밑의 모든 JSONL 로드 ===
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SESSION_DIR = PROJECT_ROOT / "mac_collector_output/raw/20260525/session_2"
raw_json_lines = []

# 각 파일별 공백 확인
for jsonl_path in sorted(SESSION_DIR.glob("*.jsonl")):
    with jsonl_path.open() as f:
        raw_json_lines.extend(line.strip() for line in f if line.strip())
'''
 raw_json_lines: list[str]
   JSONL 파일의 각 줄을 문자열로 저장
   예: [
       '{"received_at_unix_us": ..., "device_id": 101, "seq": 4871, "csi_amp": [...]}',
       '{"received_at_unix_us": ..., "device_id": 101, "seq": 4872, "csi_amp": [...]}',
       ...
   ]
'''

# ======================== ① 파싱 후 device_id 별 버퍼에 적재 ========================
buffers = defaultdict(list)

for line in raw_json_lines:
    pkt = json.loads(line)
    dev = pkt['device_id']
    seq = int(pkt['tx_seq'])
    amp = np.array(pkt['csi_amp'], dtype=np.float64)
    buffers[dev].append( (seq, amp) ) # seq를 100Hz 시간 인덱스로 사용

'''
{
    101: [(seq, amp), ...], 
    102: [...], 
    103: [...]
}
'''

# device별 seq 필드로 정렬
for dev in buffers:
    buffers[dev].sort(key=lambda item: item[0])

'''
 buffers: defaultdict[list]
   key: device_id
   value: list[ tuple[int, np.ndarray] ]

   tuple 구조: (seq, amp)
     seq: int, 송신기가 10ms마다 증가시키는 순번
     amp: np.ndarray, shape=(64,) 원본 csi_amp
     
   정렬 후 각 RX별 패킷은 seq 오름차순

- 정렬 후 상태
buffers[101] = [
    (4871, amp),
    (4872, amp),
    (4873, amp),
]
'''


'''
buffers.items() = 
dict_items([
    (101, [(1, array([...])), (2, array([...])) ... ]), 
    (102, [(1, array([...])), (2, array([...])) ... ])
    ...
])
'''

print("[1단계] device_id별 버퍼")
for dev, items in buffers.items():
    print(f"  RX{dev}: {len(items)}개 패킷")
    # print(buffers[102][0][1])


# ======================== ② seq 기반 시간 동기화: 공통 seq 격자에 보간(interpolate) ========================
RX_IDS  = [102]   # 사용자 환경에 맞춰 device_id 매핑
F_S     = 100               # Hz, 송신기가 10ms마다 seq 1개 증가
WINDOW_SECONDS = 3.0
STRIDE_SECONDS = 0.3
SESSION_SECONDS = 5 * 60
WINDOW  = int(F_S * WINDOW_SECONDS)       # 3초 = 300 samples
STRIDE  = int(F_S * STRIDE_SECONDS)       # 0.3초 = 30 samples
MAX_SESSION_SAMPLES = int(F_S * SESSION_SECONDS) # 30000
N_SUB   = 52
LABEL_MAP = {
    "empty": 0,
    "static": 1,
    "action": 2,
}


def _clean_yaml_value(value):
    # YAML 한 줄에서 값 부분만 단순 추출한다.
    # 예: ' "empty"  # comment' -> 'empty'
    return value.split("#", 1)[0].strip().strip('"').strip("'")


def read_experiment_meta(session_dir):
    # 세션 디렉터리의 session_meta_snapshot.yaml에서
    # experiment.label_target, experiment.split_strategy를 읽는다.
    #
    # 현재 필요한 값이 experiment 아래의 단순 key/value라서
    # 외부 yaml 패키지 없이 필요한 부분만 가볍게 파싱한다.
    meta_path = session_dir / "session_meta_snapshot.yaml"
    if not meta_path.exists():
        raise FileNotFoundError(f"session metadata not found: {meta_path}")

    experiment = {}
    in_experiment = False

    with meta_path.open() as f:
        for raw_line in f:
            line = raw_line.rstrip()

            if line.startswith("experiment:"):
                in_experiment = True
                continue

            # experiment 블록을 벗어나면 파싱을 멈춘다.
            # YAML에서 들여쓰기 없는 다음 top-level key가 나오면 블록 종료로 본다.
            if in_experiment and line and not line.startswith(" "):
                break

            if in_experiment and ":" in line:
                key, value = line.strip().split(":", 1)
                experiment[key] = _clean_yaml_value(value)

    # label_target은 class 번호로 바꾸고,
    # split_strategy는 train/val/test 분류에 사용할 문자열로 유지한다.
    label_target = experiment.get("label_target")
    split_strategy = experiment.get("split_strategy")

    if label_target not in LABEL_MAP:
        raise ValueError(
            f"unknown label_target={label_target!r}. "
            f"expected one of {sorted(LABEL_MAP)}"
        )

    return label_target, LABEL_MAP[label_target], split_strategy


# 현재 세션의 라벨 이름, 클래스 번호, split 정보를 전역 변수로 준비한다.
# 예: LABEL_NAME='empty', LABEL=0, SPLIT='train'

# LABEL_NAME, LABEL, SPLIT = read_experiment_meta(SESSION_DIR)
LABEL_NAME='empty'
LABEL=0
SPLIT='train'

# 버퍼를 시간배열과 진폭배열 두 개로 분리.
def to_array(buf):
    """버퍼를 (seq,), (T, 52) 두 배열로"""
    if not buf:
        return np.array([]), np.empty((0, N_SUB), np.float64)

    seq = np.array([s for s, _ in buf], dtype=np.float64)
    amp = np.stack([a[:N_SUB] for _, a in buf]) # seq별 amplitude 벡터를 2차원 배열로 쌓기

    return seq, amp

# === 공통 seq축 생성 + 선형보간 ===
rx_arrays = {
    dev: to_array(buffers[dev]) for dev in RX_IDS
}
'''
 rx_arrays: dict[ int, tuple[np.ndarray, np.ndarray] ]
   key: device_id
   value: (seq, amp)
     seq shape: (T_dev,)
     amp shape: (T_dev, 52), 스택형태

   예: rx_arrays[101] = (
       array([4871., 4872., ...]),
       array([[...52개...], [...52개...], ...])
   )
'''
# print(rx_arrays[103][0])
# print(rx_arrays[101][1][0])

start_seq = int(max(seq[0] for seq, _ in rx_arrays.values() if len(seq) > 0)) # 세 RX가 모두 존재하는 공통 시작 seq
end_seq = int(min(seq[-1] for seq, _ in rx_arrays.values() if len(seq) > 0))  # 세 RX가 모두 존재하는 공통 종료 seq
end_seq = min(end_seq, start_seq + MAX_SESSION_SAMPLES - 1)                   # 세션 5분 상한
seq_grid = np.arange(start_seq, end_seq + 1, dtype=np.float64)
'''
 seq_grid: np.ndarray, shape=(T_common,)
   세 RX가 모두 겹치는 공통 seq 시간축
   seq 1칸 = 10ms = 100Hz의 샘플 1개
   5분 세션이면 T_common 최대 30000
'''
# print(seq_grid.shape)

def resample_to_grid(buf, seq_grid):
    seq, amp = to_array(buf)
    out = np.empty((len(seq_grid), N_SUB), dtype=np.float64)

    for k in range(N_SUB):
        out[:, k] = np.interp(seq_grid, seq, amp[:, k]) # 선형 보간

    '''    
     out: np.ndarray, shape=(T_common, 52)
       RX 하나의 amplitude를 공통 seq_grid에 맞춘 결과
       해당 RX에서 빠진 seq는 앞뒤 seq 값으로 선형 보간됨
    '''
    return out

aligned = np.stack( [resample_to_grid(buffers[d], seq_grid) for d in RX_IDS] )
# aligned: np.ndarray, shape=(3, T_common, 52)
#   axis 0: RX, RX_IDS 순서 [101, 102, 103]
#   axis 1: 공통 seq 시간축
#   axis 2: 서브캐리어 amplitude 52개
print(f"\n[2단계] seq 기반 시간 동기화 완료")
print(f"  seq range: {start_seq} ~ {end_seq}")
print(f"  duration: {len(seq_grid) / F_S:.3f}s")
print(f"  seq_grid samples: {len(seq_grid)}")
print(f"  aligned shape: {aligned.shape}   (RX, 시점, 서브캐리어)")


if len(seq_grid) < WINDOW:
    print(f"  warning: WINDOW={WINDOW} requires at least {WINDOW} samples")


# ======================== ③ 윈도잉 → (N, 300, 156) ========================
T = aligned.shape[1]
windows = []

for start in range(0, T - WINDOW + 1, STRIDE):
    w = aligned[:, start:start+WINDOW, :]       # (3, 300, 52)
    w = w.transpose(1, 0, 2)                    # (300, 3, 52)
    w = w.reshape(WINDOW, len(RX_IDS) * N_SUB)  # (300, 156)
    windows.append(w) # '리스트'에 추가

'''
 windows: list[np.ndarray]
   len(windows) = N
   각 원소 w shape: (300, 156)
     axis 0: 시간, 3초 window = 300 samples
     axis 1: feature, RX 3개 * 서브캐리어 52개 = 156
'''

X = np.stack(windows)    # (N, 300, 156), 3차원 배열로 쌓기, N=윈도 개수

# 윈도우에 대응하는 라벨값
y = np.full(len(windows), LABEL, dtype=np.int64)


'''
 X: np.ndarray, shape=(N, 300, 156)
   axis 0: window index
   axis 1: 시간, 3초 window = 300 samples
   axis 2: feature, RX 3개 * 서브캐리어 52개 = 156
'''

print(f"\n[3단계] 윈도잉 결과")
print(f"  X shape: {X.shape}   ← (윈도 수, 시간, feature)")
print(f"  y shape: {y.shape}")
print(f"  label: {LABEL_NAME} -> class {LABEL}")
print(f"  split: {SPLIT}")
print(f"  {SESSION_SECONDS}초 → 윈도 {len(windows)}개 ({WINDOW_SECONDS}초 윈도, {STRIDE_SECONDS}초 stride)")

