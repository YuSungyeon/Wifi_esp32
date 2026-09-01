# 후처리 파이프라인

수집 세션 → 진폭 변환 → `tx_seq` 격자 정렬 → 슬라이딩 윈도 → 학습 텐서 `X = (N, 300, RX수×52)`.

| 구현 | 역할 |
|------|------|
| [`scripts/csi_store.py`](../../scripts/csi_store.py) | 프레임 파싱·검증·진폭 계산·유효 서브캐리어 선별 (공용 I/O) |
| [`model_train/model/Preprocessing.py`](../../model_train/model/Preprocessing.py) | 세션 1개 → `(X, y)` |
| [`model_train/model/build_dataset.py`](../../model_train/model/build_dataset.py) | 여러 세션 → `dataset.npz` (세션 단위 split) |

## 실행

```bash
source .venv/bin/activate    # numpy 필요 (quickstart.md §0)

# 세션 하나 확인
python model_train/model/Preprocessing.py                    # 최신 세션 자동 선택
python model_train/model/Preprocessing.py \
    --session-dir mac_collector_output/raw/20260825/143000_static_s21

# 학습용 데이터셋 조립 (여러 세션 · 3-class)
python model_train/model/build_dataset.py --out model_train/dataset.npz
python model_train/model/LSTM.py --dataset model_train/dataset.npz --epochs 20
```

| 인자 | 기본 | 의미 |
|------|------|------|
| `--session-dir` | 최신 세션 자동 | `raw/YYYYMMDD/<HHMMSS>_<label>_s<id>` 디렉터리 |
| `--rx-ids` | 세션에 있는 전부 | 사용할 RX `device_id` 목록 (공백 구분) |
| `--label` | **세션 매니페스트 값** | 라벨 덮어쓰기 (`empty`/`static`/`action`) |

## 입력

```text
mac_collector_output/raw/YYYYMMDD/<HHMMSS>_<label>_s<id>/
    device_<device_id>.csi   # 40B 헤더 + raw I/Q (프레임 규격: usb-collection.md)
    session.json             # 라벨 SSOT
```

**라벨은 `session.json`에서 옵니다.** 수집 시점에 박힌 값이 정본이고 `--label`은 덮어쓰기용
입니다. 이전에는 라벨이 CLI 인자에만 있었고 기본값이 `empty`라, 데이터만 보고는 어떤 세션이
무슨 상태였는지 알 수 없었습니다.

구 JSONL 세션은 매니페스트가 없어 `FileNotFoundError`로 걸러집니다 — 라벨이 없을 뿐 아니라
같은 `session_id`로 재수집하며 여러 런이 한 파일에 섞였기 때문에 학습에 쓰면 안 됩니다.

## 상수

값은 [architecture.md 상수표](../overview/architecture.md)가 정본입니다.
요약: `F_S=100`, `WINDOW=300`(3초), `STRIDE=30`(0.3초), `N_SUB=52`, 세션 상한 5분.

## 처리 단계

1. **로드·진폭** — `.csi` 프레임을 numpy로 한 번에 읽고 `sqrt(I²+Q²)` 벡터 연산.
   위상이 필요하면 `csi_store.complex_csi()` — 수집 단계에서 버리지 않으므로 나중에 살릴 수 있습니다
2. **유효 서브캐리어 선별** — `csi_store.LLTF_DATA_IDX` = `[1..26] + [38..63]` (52개).
   HT20 LLTF 64 SC 중 **0(DC)과 27~37(가드)은 상시 0**입니다. 예전에는 앞 52개를 그냥
   잘랐는데, 그 안에 이 12개가 들어가고 실제 데이터인 52~63이 버려졌습니다 —
   모델 feature의 23%가 상수 0이었고 유효 톤의 23%가 유실됐습니다
3. **tx_seq 격자 정렬** — TX ESP-NOW 카운터는 모든 RX가 같은 프레임에 같은 값을 기록하므로
   네트워크 지터 없는 10ms 클럭입니다. 모든 RX가 겹치는 공통 구간에 1스텝(=10ms) 격자를
   만들고, 빠진 라운드는 서브캐리어별 선형 보간 → `aligned = (RX수, T, 52)`
4. **윈도잉** — `WINDOW`/`STRIDE` 슬라이딩 → `X = (N, 300, RX수×52)`
   (RX축을 feature축으로 병합: RX 1대=52, 3대=156)
5. **라벨** — 매니페스트 라벨을 `LABEL_MAP`(empty=0, static=1, action=2)으로 변환해
   `y = (N,)` 전체에 부여 (세션 단위 단일 라벨)

주의 — 수집 중 보드가 재부팅한 세션:

- **TX 재부팅**: `tx_seq`가 0부터 다시 시작해 시간 격자가 깨집니다. 그냥 정렬하면 재부팅
  이후 프레임이 세션 앞쪽으로 끌려가 시간 순서가 뒤집히고 그 사이 수천 스텝이 선형 보간으로
  채워지므로, `load_buffers`가 **거부합니다**(`build_dataset`은 건너뜁니다). 재수집이 답입니다.
  TX를 별도 전원(보조배터리 등)에 두면 조용히 일어날 수 있으니 `measure_csi_hz.py`의
  `tx_back`을 확인하세요.
- **RX 재부팅**: `boot_id` 변화로 감지되며 `measure_csi_hz.py`의 `boot_changes`에 나옵니다.
  `tx_seq`는 유효하므로 데이터 자체는 쓸 수 있지만 재부팅 구간만큼 빈 구멍이 생깁니다.

## 데이터셋 조립 (`build_dataset.py`)

여러 세션의 윈도를 라벨과 함께 이어 붙여 `X_train/y_train/X_val/y_val`을 만듭니다.

**split은 반드시 세션 단위입니다.** 윈도가 3초 길이에 0.3초 stride라 이웃 윈도끼리 90%가
겹치므로, 윈도 단위로 무작위 분할하면 같은 3초 구간이 train과 val 양쪽에 들어가 검증
정확도가 실제보다 높게 나옵니다. `--val-ratio`는 라벨별로 세션을 나눠 클래스가 한쪽에
몰리지 않게 합니다.

출력 `dataset.npz` 키: `X_train`, `y_train`, `sessions_train`, `X_val`, `y_val`,
`sessions_val`, `label_names`. 같은 이름의 `.json`에 조립 조건이 기록됩니다.

## 다음 단계

`dataset.npz`는 [`LSTM.py`](../../model_train/model/LSTM.py)의 입력입니다 —
[lstm-design.md](lstm-design.md) 참고. `LSTM.py`에는 PyTorch가 별도로 필요합니다
(`pip install torch`).
