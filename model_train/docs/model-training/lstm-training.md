# 3-RX LSTM Design and Training

> 상태: **CURRENT — 공식 3-RX LSTM 학습·검증·최종 평가와 baseline 실험 완료**
>
> 입력 생성 코드: [`preprocess_3rx.py`](../../preprocessing/preprocess_3rx.py)
>
> 학습 코드: [`LSTM.py`](../../lstm/LSTM.py) · 테스트: [`test_lstm.py`](../../../tests/test_lstm.py)
>
> 전처리 기준: [3-RX CSI Preprocessing Design](../preprocessing/design.md)
>
> 모델 비교 근거: [Model Comparison and Selection](model-comparison.md)
>
> 실제 실험 결과: [3-RX LSTM Baseline Training and Final Evaluation](lstm-baseline-report.md)

이 문서는 `preprocess_3rx.py`가 만든 3-RX 전처리 결과만 입력으로 사용하는 LSTM
baseline의 공식 설계이자 현재 구현 기준이다. `LSTM.py`는 구형
`Preprocessing.py`를 실행하거나 import하지 않고, 저장된 공식 split을 직접 읽는다.

자동 테스트에서 소형 fixture로 학습·validation·checkpoint·최종 test 흐름을
확인했고, 실제 `20260616` 산출물 전체에 대해서도 입력 계약과 NaN·무한대 검사를
통과했다. 실제 데이터의 seed·class-weight 비교와 최종 test는 2026-09-02
완료했으며 상세 수치와 해석은 실험 결과 문서를 기준으로 한다.

## 1. 목적과 분류 대상

RX101·RX102·RX103에서 같은 TX frame을 수신해 만든 3초 CSI window 하나를 다음
세 class 중 하나로 분류한다.

```text
empty  = 0  # 사람이 없음
static = 1  # 사람이 정지해 있음
motion = 2  # 사람이 움직임
```

LSTM은 시간에 따라 변하는 CSI amplitude 패턴을 학습한다. 전처리의 record 제거,
RX 정렬, 보간, window 생성 규칙은 이 문서에서 다시 정의하지 않고 공식 전처리
설계를 그대로 따른다.

## 2. 공식 입력 산출물

20260616 데이터의 기본 입력 디렉터리는 다음과 같다.

```text
model_train/preprocessing/output/20260616/
├── train/
│   ├── X.npy
│   ├── y.npy
│   └── windows.jsonl
├── validation/
│   ├── X.npy
│   ├── y.npy
│   └── windows.jsonl
├── test/
│   ├── X.npy
│   ├── y.npy
│   └── windows.jsonl
├── normalization.npz
└── manifest.json
```

각 파일의 역할은 다음과 같다.

| 파일 | 내용 |
|---|---|
| `X.npy` | 정규화 전 3-RX CSI amplitude window, `float32` |
| `y.npy` | window별 class 번호, `int64` |
| `windows.jsonl` | window의 session, 시작 `tx_seq`, 관측률 등 출처 metadata |
| `normalization.npz` | train에서 계산한 `mean`, `std`, `std_safe` |
| [`manifest.json`](../preprocessing/manifest-reference.md) | class map, RX 순서, split, 전처리 설정과 제외 근거 |

현재 생성된 20260616 데이터의 shape와 class별 window 수는 다음과 같다.

| split | `X.shape` | empty | static | motion |
|---|---|---:|---:|---:|
| train | `(16728, 300, 192)` | 5,857 | 5,922 | 4,949 |
| validation | `(5933, 300, 192)` | 1,971 | 1,980 | 1,982 |
| test | `(5924, 300, 192)` | 1,961 | 1,982 | 1,981 |

이 수치는 현재 `manifest.json`의 결과다. 전처리 설정이나 원본 데이터가 바뀌면
코드에 window 수를 고정하지 않고 새 manifest와 실제 배열 shape를 기준으로 한다.

## 3. Session split 계약

같은 session에서 만들어진 window는 원본 frame을 많이 공유하므로 window 단위로
무작위 분할하지 않는다. 한 session의 모든 window는 하나의 split에만 속한다.

| split | empty | static | motion | session 수 |
|---|---|---|---|---:|
| train | 1~6 | 11~16 | 21, 23~26 | 17 |
| validation | 7, 8 | 17, 18 | 27, 28 | 6 |
| test | 9, 10 | 19, 20 | 29, 30 | 6 |

Session 22는 RX102 record가 3개뿐이어서 전처리 품질 gate에서 제외된다. 학습
코드는 split을 다시 무작위로 만들지 않고 `manifest.json`의 배정 결과를 그대로
사용한다. Train은 파라미터 학습, validation은 설정과 checkpoint 선택, test는
설정을 모두 고정한 뒤 최종 성능 확인에만 사용한다.

## 4. Feature와 Tensor shape

한 RX가 TX frame 하나에서 제공하는 `csi_amp` 64개를 64개 feature로 사용한다.
세 RX를 manifest의 고정 순서 `[101, 102, 103]`으로 이어 붙이면 한 시점의 feature는
192개다.

```text
feature 0~63    = RX101의 서브캐리어 0~63 amplitude
feature 64~127  = RX102의 서브캐리어 0~63 amplitude
feature 128~191 = RX103의 서브캐리어 0~63 amplitude
```

Feature 하나는 **특정 RX에서 측정한 특정 서브캐리어의 amplitude**다. 같은
서브캐리어 번호라도 RX가 다르면 서로 다른 feature다.

```text
한 시점:       (192,)
window 하나:   (300, 192)  # 100Hz × 3초
전체 split X:  (N_split, 300, 192)
전체 split y:  (N_split,)
학습 batch X:  (B, 300, 192)
학습 batch y:  (B,)
```

`tx_seq`, RX의 `seq`, 수신 시각은 정렬과 추적을 위한 metadata이며 LSTM 입력
feature에 포함하지 않는다.

## 5. 데이터 로딩과 normalization

### 5.1 메모리 사용 방식

Train `X.npy`는 약 3.6GB이므로 전체 배열을 `torch.tensor(X)`로 한 번에 복사하지
않는다. `np.load(..., mmap_mode="r")`로 열고 Dataset이 필요한 window만 읽어
batch Tensor로 변환한다.

```text
X.npy를 memory map으로 열기
  → Dataset이 index에 해당하는 (300, 192) window 읽기
  → train mean/std_safe로 정규화
  → float32 Tensor로 변환
  → DataLoader가 batch 생성
```

기본 batch size 32일 때 입력 batch 하나의 raw 크기는 약 7MiB다. DataLoader의
worker 수는 실행 장치와 메모리 사용량을 확인하며 설정하고 config에 기록한다.
기본값은 `num_workers=0`이며, 이 경우 학습 프로세스가 직접 데이터를 읽는다.

### 5.2 Feature별 train 통계

`normalization.npz`의 평균과 표준편차는 특정 class 하나가 아니라 train의
`empty`, `static`, `motion` window 전체에서 계산한다. Train 배열이
`(N_train, 300, 192)`이면 feature `k`의 통계는 모든 train window와 300개
시점에 있는 `X_train[:, :, k]`를 대상으로 한다.

```python
mean[k] = X_train[:, :, k].mean()
std[k] = X_train[:, :, k].std()
```

따라서 각 feature마다 평균과 표준편차가 하나씩 존재한다.

```text
mean.shape     = (192,)
std.shape      = (192,)
std_safe.shape = (192,)
```

### 5.3 세 split에 같은 기준 적용

저장된 `X.npy`에는 정규화 전 raw amplitude가 들어 있다. Dataset은 window를
읽을 때 다음 공식을 적용한다.

```text
X_normalized = (X_raw - train_mean) / train_std_safe
```

```text
train      → train mean/std_safe 적용
validation → 같은 train mean/std_safe 적용
test       → 같은 train mean/std_safe 적용
실제 예측  → 같은 train mean/std_safe 적용
```

Validation과 test에서 평균과 표준편차를 새로 계산하지 않는다. 평가 데이터의
분포를 미리 사용하면 data leakage가 발생한다. 표준편차가 `1e-6`보다 작은
feature는 `std_safe=1.0`을 사용해 0으로 나누는 문제를 막는다.

## 6. LSTM baseline 구조

첫 baseline은 다음 구조로 고정한다.

```text
입력 batch (B, 300, 192)
  │
  ▼
2-layer LSTM
  input_size=192
  hidden_size=128
  batch_first=True
  │
  ▼
전체 시점 출력 (B, 300, 128)
  │ 마지막 시점 선택
  ▼
window 표현 (B, 128)
  │
  ▼
Dropout(p=0.2)
  │
  ▼
Linear(128, 3)
  │
  ▼
logits (B, 3)
```

| 항목 | 기준값 |
|---|---:|
| input size | 192 |
| hidden size | 128 |
| LSTM layers | 2 |
| dropout | 0.2 |
| class 수 | 3 |

마지막 Linear 출력은 확률이 아니라 class별 `logits`다. 학습에서는 softmax를
직접 적용하지 않고 `CrossEntropyLoss`에 logits를 전달한다. 확률이 필요한 평가와
예측 저장 단계에서만 softmax를 적용한다.

## 7. 학습 설정

공식 baseline의 시작 설정은 다음과 같다.

| 항목 | 기준값 |
|---|---|
| batch size | 32 |
| optimizer | Adam |
| learning rate | 0.001 |
| loss | CrossEntropyLoss |
| 최대 epoch | 50 |
| early stopping patience | 5 epoch |
| DataLoader worker | 0 |
| train shuffle | `True` |
| validation/test shuffle | `False` |
| checkpoint 선택 | validation window-level macro-F1 최대 |
| 비교 seed | `0`, `1`, `2` |
| 장치 우선순위 | CUDA → Apple MPS → CPU |

Early stopping은 validation window-level macro-F1이 더 이상 좋아지지 않을 때 최대 50 epoch를
모두 채우기 전에 학습을 종료하는 기능이다. `patience`는 최고 점수가 갱신되지
않아도 기다릴 epoch 수다. 예를 들어 `patience=5`이고 epoch 12에서 최고 점수가
나온 뒤 epoch 13~17까지 개선되지 않으면 epoch 17 평가 후 학습을 종료하고,
최종 평가에는 epoch 12의 best checkpoint를 사용한다.

공식 baseline의 `patience` 기본값은 `5`다. 실행할 때 `--patience`로 바꿀 수
있지만 train과 validation만 보고 정한 뒤 첫 test 전에 고정한다. 실제 값은
`config.json`과 checkpoint에 기록해 같은 종료 조건을 재현할 수 있게 한다. Test
결과가 좋지 않다는 이유로 patience를 바꾸지 않는다.

비교 seed `0`, `1`, `2`는 class나 session 번호가 아니라 모델 초기값, train
shuffle 순서, Dropout mask 등의 무작위 시작점을 정하는 번호다. 같은 데이터와
설정으로 seed만 바꿔 세 번 학습하고, seed마다 validation window-level macro-F1이 가장 높은
checkpoint를 선택한다. 최종 결과는 세 실행의 평균과 표준편차를 함께 보고하며,
test가 가장 잘 나온 seed 하나만 골라 보고하지 않는다.

### 7.1 Class weight 결정

Validation과 test 데이터를 복제하거나 삭제해 class 수를 맞추지 않는다. 현재
train window 수는 다음과 같다.

```text
empty  = 5,857
static = 5,922
motion = 4,949
```

Class 수만 보고 불리하다고 미리 판단하지 않는다. 같은 session split, 모델 설정,
seed `0`, `1`, `2`로 다음 두 실험을 모두 실행한다.

```text
실험 A: class weight 없이 학습
실험 B: train loss에만 class weight를 적용해 학습
```

실험 B에는 다음 공식으로 계산한 후보 weight를 사용한다.

```text
class_weight[c] = 전체 train window 수 / (class 수 × class c의 window 수)
```

현재 window 수로 계산한 후보는 대략 다음과 같다.

```text
empty  ≈ 0.95
static ≈ 0.94
motion ≈ 1.13
```

두 실험은 seed 3개의 validation window-level macro-F1 평균을 우선 기준으로 비교하고,
class별 recall·F1과 confusion matrix로 적은 class가 실제로 개선됐는지 함께
확인한다. Weight 적용 결과가 명확하게 개선되지 않으면 더 단순한 실험 A를
선택한다. 이 선택에는 test를 사용하지 않으며, 확정한 한 방식만 최종 test에서
평가한다.

Class weight는 오분류 loss의 중요도만 조정한다. 입력 `X`와 normalization의
`mean`, `std_safe`는 두 실험에서 동일하다.

## 8. Epoch 처리와 validation

Epoch 하나는 **Train 단계에서 파라미터를 갱신한 뒤, Validation 단계에서 갱신된
모델을 고정하고 성능을 측정하는 순서**로 실행한다.

```text
Epoch 시작
  → Train 전체: batch마다 파라미터 갱신
  → Validation 전체: 파라미터 갱신 없이 성능 측정
  → Best checkpoint와 early stopping 판단
Epoch 종료
```

### 8.1 Train 단계: 파라미터 갱신

`model.train()`으로 학습 모드를 켠다. Train loader가 전체 train window index를
무작위로 섞어 32개씩 CPU batch로 전달하면, 학습 반복문이 batch를 선택한 장치로
이동한다. 각 batch에서는 다음 순서로 파라미터를 한 번 갱신한다.

1. `optimizer.zero_grad()`로 이전 gradient를 비운다.
2. Forward로 `(B, 3)` logits를 계산한다.
3. CrossEntropyLoss를 계산한다.
4. `loss.backward()`로 gradient를 계산한다.
5. `optimizer.step()`으로 모델 파라미터를 갱신한다.
6. 해당 batch의 loss와 예측 수를 train 지표에 누적한다.

현재 train window 16,728개와 batch size 32에서는 마지막 24개 window도 사용하는
`drop_last=False`를 기준으로 epoch마다 523개 batch, 즉 523번의 파라미터 갱신이
발생한다. Train loader 전체를 처리하면 epoch의 train loss와 accuracy를 계산한다.

### 8.2 Validation 단계: 파라미터 고정

Train 단계가 끝난 직후의 파라미터를 그대로 유지한 채 `model.eval()`과
`torch.no_grad()`로 validation loader 전체를 평가한다. 각 validation batch에서도
forward와 loss·예측 계산은 하지만 다음 작업은 실행하지 않는다.

```text
loss.backward()     실행하지 않음
optimizer.step()    실행하지 않음
```

따라서 validation은 Train 단계에서 갱신한 파라미터를 되돌리거나 다시 수정하지
않고, 현재 모델이 학습에 사용하지 않은 session에서도 잘 동작하는지만 측정한다.
현재 validation window 5,933개와 batch size 32에서는 186개 batch를 평가한다.

### 8.3 Checkpoint와 다음 epoch 결정

Validation 전체의 window-level macro-F1이 이전 최고점보다 높으면 현재 파라미터를 best
checkpoint로 저장하고 patience 횟수를 0으로 되돌린다. 개선되지 않으면
checkpoint를 덮어쓰지 않고 patience를 1 증가시킨다. Patience 한도에 도달하지
않았으면 다음 epoch에서 다시 `model.train()`으로 전환해 Train 단계부터 반복하고,
한도에 도달하면 학습을 종료한다. 최종 test에는 마지막 epoch가 아니라 validation
window-level macro-F1이 가장 높았던 best checkpoint를 사용한다.

Epoch마다 최소 다음 값을 기록한다.

```text
train loss
train accuracy
validation loss
validation accuracy
validation macro precision / recall / F1
validation class별 precision / recall / F1
learning rate
epoch 소요 시간
```

Accuracy만으로 checkpoint를 선택하지 않는다. 한 class에 치우친 모델을 구분하기
위해 세 class의 F1을 동일 비중으로 평균한 validation macro-F1을 기준으로 한다.

## 9. 최종 test

모델 구조, normalization, class weight, early stopping, checkpoint 선택 기준을
train과 validation으로 모두 고정한 뒤 test를 평가한다. Test 결과를 본 뒤 같은
test에 유리하게 설정을 바꾸지 않는다.

최종 지표는 다음과 같다.

- Window-level accuracy와 macro precision·recall·F1
- `empty`, `static`, `motion`별 precision·recall·F1
- 3×3 confusion matrix
- Session별 window accuracy
- Session 단위로 집계한 전체 accuracy와 macro-F1
- 각 class와 session의 window 수

Window가 90% 겹치므로 window-level 결과만으로 결론을 내리지 않는다. Session-level
예측은 같은 session의 모든 window softmax 확률을 class별로 평균하고, 평균값이
가장 큰 class를 그 session의 예측으로 정한다. 이 집계 규칙도 test 전에 고정한다.

세 seed의 결과는 평균과 표준편차를 함께 보고한다. 각 seed는 동일하게 고정된
설정으로 test를 평가하며, seed별 test 결과를 보고 설정을 다시 선택하지 않는다.

## 10. 저장 산출물

학습 한 번의 결과는 독립된 run 디렉터리에 저장한다.

```text
model_train/lstm/runs/<run-id>/
├── config.json
├── dataset-manifest.json
├── normalization.npz
├── best-model.pt
├── history.jsonl
├── validation-metrics.json
├── run-summary.json
├── test-metrics.json
├── confusion-matrix.png
└── test-predictions.jsonl
```

Checkpoint에는 최소 다음 값을 저장한다.

```text
model_state_dict
optimizer_state_dict
epoch
validation metrics
class map
model config
preprocessing config
normalization reference 또는 값
dataset manifest reference
source commit
random seed
```

`test-predictions.jsonl`에는 window index, session ID, 시작 `tx_seq`, 정답, 예측,
class별 확률을 저장해 오분류 구간을 원본 session까지 추적할 수 있게 한다.

## 11. 학습 시작 전 검증

학습 코드는 시작할 때 다음 조건을 확인하고 하나라도 맞지 않으면 오류로 중단한다.

- `manifest.json`의 `rx_order`가 `[101, 102, 103]`이다.
- Class map이 `empty=0`, `static=1`, `motion=2`다.
- 세 split의 `X` shape가 `(N, 300, 192)`이고 dtype이 `float32`다.
- 세 split의 `y` shape가 `(N,)`이고 dtype이 `int64`다.
- 각 split에서 `len(X) == len(y) == windows.jsonl record 수`다.
- Train·validation·test의 session이 서로 겹치지 않는다.
- `normalization.npz`의 `mean`과 `std_safe` 길이가 192다.
- Normalization 통계가 train에서 계산됐다고 manifest에 기록되어 있다.
- 입력과 정규화 결과에 NaN 또는 무한대가 없다.

## 12. 실행 방식

공식 전처리는 현재 다음 명령으로 실행할 수 있다.

```bash
conda run -n wifi-csi-lstm python model_train/preprocessing/preprocess_3rx.py \
  --raw-dir mac_collector_output/raw/20260616
```

최초 한 번 모델 실행 의존성을 설치한다.

```bash
conda run -n wifi-csi-lstm python -m pip install -r requirements-model.txt
```

먼저 실제 배열 전체의 shape, dtype, metadata, split, normalization, NaN·무한대를
검증한다.

```bash
conda run -n wifi-csi-lstm python model_train/lstm/LSTM.py validate \
  --dataset-dir model_train/preprocessing/output/20260616
```

학습 명령은 train으로 파라미터를 갱신하고 validation으로 best checkpoint를
선택한다. 시작할 때 test 파일도 11절의 형식·유한값 검사는 하지만, test를 모델에
입력하거나 test 성능을 계산·선택 기준으로 사용하지 않는다. `--run-dir`을 생략하면
`model_train/lstm/runs/<시각>-seed<번호>-<weight>/`가 자동 생성된다.

```bash
conda run -n wifi-csi-lstm python model_train/lstm/LSTM.py train \
  --dataset-dir model_train/preprocessing/output/20260616 \
  --seed 0 \
  --class-weight none
```

7.1절 비교를 위해 seed `0`, `1`, `2` 각각에 대해 `--class-weight none`과
`--class-weight balanced`를 별도 run으로 실행한다. Validation 결과로 한 방식을
선택한 뒤, 선택된 각 seed의 run에만 다음 최종 test를 한 번 실행한다.

```bash
conda run -n wifi-csi-lstm python model_train/lstm/LSTM.py test \
  --dataset-dir model_train/preprocessing/output/20260616 \
  --run-dir model_train/lstm/runs/<선택한-run-id>
```

`test`는 동일 run에 `test-metrics.json`이 이미 있으면 다시 실행하지 않는다. 이는
같은 test 결과를 반복해서 확인하며 설정을 바꾸는 일을 막기 위한 장치다.
`--skip-full-scan`은 개발 중 빠른 확인용이며 공식 학습·test에서는 사용하지 않는다.

## 13. 구현 확인 결과

- [x] `LSTM.py`가 구형 전처리 모듈을 import하지 않는다.
- [x] `preprocess_3rx.py`가 저장한 세 split의 `X.npy`, `y.npy`를 직접 읽는다.
- [x] 입력 shape가 `(B, 300, 192)`이고 `input_size=192`다.
- [x] Train의 `mean`, `std_safe`를 세 split에 동일하게 적용한다.
- [x] 대용량 `X.npy`를 memory map으로 읽고 전체 Tensor 복사를 만들지 않는다.
- [x] Session split을 다시 섞지 않는다.
- [x] Validation window-level macro-F1 기준 best checkpoint와 early stopping이 동작한다.
- [x] 설정 고정 후 test와 confusion matrix, session-level 지표를 저장한다.
- [x] Config, dataset manifest, normalization, seed, source commit을 함께 보존한다.

`tests/test_lstm.py`의 소형 end-to-end 학습·test와 저장소 전체 테스트가 통과했다.
실제 `20260616` 데이터는 28,585개 window의 계약과 전체 유한값 검사를 통과했다.
실제 seed·class-weight 6개 학습과 선택된 balanced seed 3개의 최종 test 결과는
[3-RX LSTM Baseline Training and Final Evaluation](lstm-baseline-report.md)에
별도로 기록한다.
