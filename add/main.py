import numpy as np
import json
from collections import defaultdict
from pathlib import Path

np.set_printoptions(precision=16, floatmode="maxprec_equal", threshold=np.inf, suppress=False)

# === dataset/20260429/session_1 밑의 모든 JSONL 로드 ===
SESSION_DIR = Path("dataset/20260429/session_1")
raw_json_lines = []

# 각 파일별 공백 확인
for jsonl_path in sorted(SESSION_DIR.glob("*.jsonl")):
    with jsonl_path.open() as f:
        raw_json_lines.extend(line.strip() for line in f if line.strip())

# ======================== ① 파싱 후 device_id 별 버퍼에 적재 ========================
buffers = defaultdict(list)   # {101: [(t, amp), ...], 102: [...], 103: [...]}

for line in raw_json_lines:
    pkt = json.loads(line)
    dev = pkt['device_id']
    t   = pkt['received_at_unix_us']
    amp = np.array(pkt['csi_amp'], dtype=np.float64)
    rounded = amp.round(16) # 진폭: 소수점 16자리 까지 정밀도 설정
    buffers[dev].append((t, amp)) # 튜플로 저장

# for dev in buffers:
#     buffers[dev].sort(key=lambda item: item[0])

print("[1단계] device_id별 버퍼")
for dev, items in buffers.items():
    print(f"  RX{dev}: {len(items)}개 패킷")
    # print(buffers[102][0][1])


# # ======================== ② 시간 동기화: 공통 시간축에 보간(interpolate) ========================
# 50Hz 기준 타깃 timestamp 격자 생성 (20ms 간격)
RX_IDS  = [101, 102, 103]   # 사용자 환경에 맞춰 device_id 매핑
F_S     = 50                # Hz
WINDOW  = 100               # 2초
STRIDE  = 50                # 1초
N_SUB   = 52

# 버퍼를 시간배열과 진폭배열 두 개로 분리.
def to_array(buf):
    """버퍼를 (T,), (T, 52) 두 배열로"""
    if not buf:
        return np.array([]), np.empty((0, N_SUB), np.float64)

    ts  = np.array([t for t, _ in buf], dtype=np.float64) / 1e6   # us -> s, 초단위 변환
    amp = np.stack([a for _, a in buf]) # 시간별로 여러개의 amplitude 벡터를 2차원 배열로 쌓기
    return ts, amp

# === 공통 시간축 (50Hz 격자) 생성 + 선형보간 ===
rx_arrays = {
    dev: to_array(buffers[dev]) for dev in RX_IDS
}

# print(rx_arrays[103][0])
# print(rx_arrays[101][1][0])

start_s = max(ts[0] for ts, _ in rx_arrays.values() if len(ts) > 0) # 세 RX가 모두 존재하는 공통 시작 시각
end_s = min(ts[-1] for ts, _ in rx_arrays.values() if len(ts) > 0)  # 세 RX가 모두 존재하는 공통 종료 시각
t_grid = np.arange(start_s, end_s, 1.0 / F_S, dtype=np.float64)
# print(t_grid)

def resample_to_grid(buf, t_grid):
    ts, amp = to_array(buf)
    out = np.empty((len(t_grid), N_SUB), dtype=np.float64)
    for k in range(N_SUB):
        out[:, k] = np.interp(t_grid, ts, amp[:, k])
    return out

aligned = np.stack([resample_to_grid(buffers[d], t_grid) for d in RX_IDS])
# aligned: (3, T, 52)
print(f"\n[2단계] 시간 동기화 완료")
print(f"  overlap: {end_s - start_s:.3f}s")
print(f"  t_grid samples: {len(t_grid)}")
print(f"  aligned shape: {aligned.shape}   (RX, 시점, 서브캐리어)")

if len(t_grid) < WINDOW:
    print(f"  warning: WINDOW={WINDOW} requires at least {WINDOW} samples")


# ======================== ③ 윈도잉 → (N, 3, 52, 100) ========================
T = aligned.shape[1]
windows = []
for start in range(0, T - WINDOW + 1, STRIDE):
    w = aligned[:, start:start+WINDOW, :]   # (3, 100, 52)
    w = w.transpose(0, 2, 1)                # (3, 52, 100)  ← 모델 입력 순서로
    windows.append(w) # '리스트'에 추가

X = np.stack(windows)    # (N, 3, 52, 100), 4차원 배열로 쌓기, N=윈도 개수
y = np.random.randint(0, 3, size=len(windows))   # 가짜 라벨, 3-class

print(f"\n[3단계] 윈도잉 결과")
print(f"  X shape: {X.shape}   ← (윈도 수, RX, 서브캐리어, 시간)")
print(f"  y shape: {y.shape}")
print(f"  60초 → 윈도 {len(windows)}개 (2초 윈도, 1초 stride)")

# ======================== ④ 학습 batch ========================
# B = 32
# batch_x = X[:B]
# batch_y = y[:B]
# print(f"\n[4단계] Batch")
# print(f"  batch_x: {batch_x.shape}   ← 모델 입력")
# print(f"  batch_y: {batch_y.shape}")
