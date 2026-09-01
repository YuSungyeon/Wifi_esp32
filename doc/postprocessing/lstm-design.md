# LSTM 설계 문서

구현: [`model_train/model/LSTM.py`](../../model_train/model/LSTM.py) ·
전처리: [pipeline.md](pipeline.md)

> [!NOTE]
> **2026-08-25 이후 라벨·데이터셋 경로가 바뀌었습니다.** 이 문서 곳곳의
> "라벨은 `Preprocessing.py --label`로 사람이 지정한다"는 서술은 옛 방식입니다.
> 이제 라벨은 **수집 시점에 세션 매니페스트(`session.json`)에 박히고**, 학습은
> `build_dataset.py`가 만든 `dataset.npz`(여러 세션 · 세션 단위 split)를 씁니다:
>
> ```bash
> python model_train/model/build_dataset.py --out model_train/dataset.npz
> python model_train/model/LSTM.py --dataset model_train/dataset.npz --epochs 20
> ```
>
> §14의 개선 목록 중 "라벨을 세션 메타데이터에서 읽도록 변경"과 "train/validation split
> 추가"는 이것으로 해결됐습니다. 남은 것은 **epoch마다 `X_val`로 검증하고 best 모델을
> `torch.save`** 하는 부분입니다 (`LSTM.py`의 `TODO(모델 담당)` 주석).
> 상세: [pipeline.md](pipeline.md) · [sprint/2026-08-collection-hardening.md](../sprint/2026-08-collection-hardening.md)

> 이 문서의 shape 예시는 **RX 3대(feature 156)** 기준이다.
> feature 수는 `RX수 × 52`로 결정되며(RX 1대면 52), 코드는 `X.shape[2]`에서 자동 파생한다.

## 1. 전처리 결과 (모델 입력)

`Preprocessing.run_preprocessing()`이 만드는 입력은 다음 형태이다.

```python
X.shape == (N, 300, RX수 * 52)   # 예: RX 3대면 (N, 300, 156)
y.shape == (N,)
```

각 차원의 의미는 다음과 같다.

```text
N   = 윈도우 개수
300 = 시간 길이, 3초 * 100Hz
feature = RX 개수 * 서브캐리어 52개
```

즉, 윈도우 하나는 다음 형태이다.

```python
X[0].shape == (300, 156)
```

이 뜻은 하나의 학습 샘플이 300개 시점으로 구성되고, 각 시점마다 156개의 CSI amplitude feature를 가진다는 의미이다.

`y`는 각 윈도우의 클래스 라벨이다.

```text
y[0] = 0 또는 1 또는 2
```

현재 클래스 정의는 다음과 같다.

```text
0: empty
1: static
2: action
```

현재 데이터 수집 방식에서는 세션 하나가 5분 단위로 측정되고, 하나의 세션은 하나의 클래스에 대한 데이터만 담는다.

예를 들어 `mac_collector_output/raw/20260616/session_21`을 empty 상태에서 측정했다면,
이 세션에서 만든 모든 window의 라벨은 같은 값이 된다.

```python
y = np.full(len(windows), 0, dtype=np.int64)   # empty = class 0
```

**현재 라벨 지정 방식**: 세션 라벨은 `Preprocessing.py`의 `--label empty|static|action`
CLI 인자(또는 `run_preprocessing(label_name=...)`)로 사람이 직접 지정한다.
`session_meta_snapshot.yaml`에서 자동으로 읽는 방식은 §14의 개선 항목이다
(현재 snapshot들의 `label_target` 값 체계가 3-class와 달라 정비가 선행되어야 함).

`CrossEntropyLoss`를 사용할 것이므로 `y`는 one-hot 벡터가 아니라 정수 클래스 번호여야 한다.

---

## 2. LSTM 입력 형태

PyTorch의 `nn.LSTM`은 기본적으로 다음 형태의 입력을 받는다.

```text
(seq_len, batch, input_size)
```

하지만 `batch_first=True`를 사용하면 다음 형태로 입력할 수 있다.

```text
(batch, seq_len, input_size)
```

현재 전처리 결과가 이미 다음 형태이므로:

```python
X.shape == (N, 300, 156)
```

`batch_first=True`를 사용하는 것이 가장 자연스럽다.

따라서 LSTM 입력 설정은 다음과 같다.

```text
batch     = 미니배치 크기
seq_len   = 300
input_size = 156
```

예를 들어 `batch_size = 32`이면 학습 중 한 배치의 입력 형태는 다음과 같다.

```python
batch_x.shape == (32, 300, 156)
batch_y.shape == (32,)
```

---

## 3. 모델의 역할

모델은 3초 길이의 CSI 시계열 윈도우 하나를 보고, 해당 윈도우가 어떤 클래스인지 예측한다.

입력:

```text
(batch, 300, 156)
```

출력:

```text
(batch, num_classes)
```

예를 들어 클래스가 3개라면:

```text
(batch, 3)
```

출력값은 확률이 아니라 `logits`이다. `CrossEntropyLoss`는 logits를 입력으로 받기 때문에 모델 마지막에 `softmax`를 넣지 않는다.

---

## 4. 모델 구조 (현행)

```text
입력 X
  shape: (batch, 300, 156)

LSTM
  input_size: X.shape[2] (예: 156)
  hidden_size: 128
  num_layers: 2
  batch_first: True

마지막 time step 출력 선택
  shape: (batch, 128)

Dropout
  p: 0.2

Linear
  128 -> num_classes

출력 logits
  shape: (batch, num_classes)
```

전체 흐름은 다음과 같다.

```text
(batch, 300, 156)
        |
      LSTM
        |
(batch, 300, hidden_size)
        |
마지막 시점만 사용
        |
(batch, hidden_size)
        |
     Dropout
        |
     Linear
        |
(batch, num_classes)
```

---

## 5. 왜 마지막 time step을 쓰는가

LSTM은 각 시점마다 출력을 만든다.

입력이 다음과 같으면:

```text
(batch, 300, 156)
```

LSTM 출력은 다음과 같다.

```text
(batch, 300, hidden_size)
```

여기서 `300`은 시간축이다.

분류 문제에서는 보통 전체 3초 윈도우를 하나의 클래스로 예측한다. 따라서 각 시점마다 클래스를 예측할 필요가 없다.

그래서 마지막 시점의 출력만 사용한다.

```python
last = lstm_out[:, -1, :]
```

이 결과는 다음 형태이다.

```text
(batch, hidden_size)
```

마지막 시점의 출력은 앞의 시계열 정보를 LSTM 내부 상태를 통해 반영한 값으로 볼 수 있다.

---

## 6. 초기 하이퍼파라미터 (현행 `LSTM.py` 기본값)

```text
input_size   = X.shape[2] 자동 파생
hidden_size  = 128
num_layers   = 2
num_classes  = 3
dropout      = 0.2
batch_size   = 32
learning_rate = 0.001
epochs       = 20  (--epochs 로 변경 가능)
```

이 설정은 너무 크지 않아서 먼저 코드 동작과 데이터 흐름을 확인하기 좋다.

성능이 부족하면 이후에 다음 항목을 조정한다.

```text
hidden_size: 128 -> 256
dropout: 0.2 -> 0.3 또는 0.5
learning_rate: 0.001 -> 0.0005
```

---

## 7. 데이터 타입

PyTorch 학습 전에 NumPy 배열을 Tensor로 바꿔야 한다.

`X`는 실수 입력이므로 `float32`가 적절하다.

```text
X dtype: torch.float32
```

`y`는 클래스 번호이므로 `long` 타입이어야 한다.

```text
y dtype: torch.long
```

형태는 다음과 같아야 한다.

```text
X_tensor.shape == (N, 300, 156)
y_tensor.shape == (N,)
```

---

## 8. Dataset과 DataLoader

학습할 때는 `TensorDataset`과 `DataLoader`를 사용한다.

```text
TensorDataset:
  X_tensor, y_tensor를 하나로 묶음

DataLoader:
  batch_size 단위로 데이터를 나눠서 모델에 공급
```

예를 들어:

```text
전체 X: (991, 300, 156)
batch_size: 32
```

이면 학습 중 대부분의 배치는 다음 형태가 된다.

```text
batch_x: (32, 300, 156)
batch_y: (32,)
```

마지막 배치는 남은 개수에 따라 32보다 작을 수 있다.

---

## 9. Loss 함수

다중 클래스 분류이므로 `CrossEntropyLoss`를 사용한다.

모델 출력:

```text
logits.shape == (batch, num_classes)
```

라벨:

```text
y.shape == (batch,)
```

예:

```text
logits.shape == (32, 3)
y.shape == (32,)
```

`CrossEntropyLoss` 사용 시 주의할 점:

```text
모델 마지막에 softmax를 넣지 않는다.
y는 one-hot이 아니라 클래스 번호여야 한다.
```

---

## 10. 학습 루프 흐름

학습 루프는 다음 순서로 구성한다.

```text
1. 모델 생성
2. loss 함수 생성
3. optimizer 생성
4. epoch 반복
5. batch 반복
6. 모델 예측
7. loss 계산
8. gradient 초기화
9. backpropagation
10. optimizer step
11. loss와 accuracy 출력
```

한 batch에서의 흐름은 다음과 같다.

```text
batch_x: (32, 300, 156)
batch_y: (32,)

logits = model(batch_x)
logits: (32, 3)

loss = criterion(logits, batch_y)
```

정확도 계산은 다음 방식으로 한다.

```text
pred = logits.argmax(dim=1)
accuracy = pred와 batch_y가 같은 비율
```

---

## 11. 세션 단위 라벨링

현재 데이터는 세션 단위로 라벨을 주는 구조이다.

즉, 하나의 세션이 하나의 클래스에 해당한다.

```text
session 하나 = 5분 측정 데이터
session 하나 = class 하나
```

현재 클래스 정의는 다음과 같다.

```text
class 0: empty
class 1: static
class 2: action
```

각 세션의 라벨은 현재 `Preprocessing.py`의 `--label` 인자로 지정한다
(세션 메타 YAML 자동 반영은 §14의 개선 항목).

```bash
python model_train/model/Preprocessing.py --session-dir <세션 경로> --label empty
```

이 경우 이 세션의 모든 window 라벨이 `0`이 된다.

```python
y = np.full(len(windows), 0, dtype=np.int64)
```

라벨 문자열과 클래스 번호는 다음 규칙(`LABEL_MAP`)으로 매핑한다.

```text
"empty"  -> 0
"static" -> 1
"action" -> 2
```

다만 이 세션 하나만으로 학습하면 모델은 class 0만 보게 된다. 따라서 학습 코드가 돌아가는지 확인하는 용도로는 충분하지만, 분류 성능을 평가하기에는 부족하다.

실제 분류 학습을 하려면 class별 세션을 함께 넣어야 한다.

예:

```text
class 0: empty
class 1: static
class 2: action
```

세션별 라벨·split을 각 세션의 `session_meta_snapshot.yaml`에서 관리하는 방향이 목표다.

```text
raw/20260616/session_1/session_meta_snapshot.yaml -> label_target: "empty",  split_strategy: "train"
raw/20260616/session_2/session_meta_snapshot.yaml -> label_target: "static", split_strategy: "train"
raw/20260616/session_3/session_meta_snapshot.yaml -> label_target: "action", split_strategy: "train"
```

이후에는 `--label`을 사람이 넘기는 방식보다, 각 세션의 YAML에서 `label_target`과
`split_strategy`를 자동으로 반영하는 방식이 좋다. 이를 위해서는 수집 시점에
`session_meta.yaml`의 `label_target`을 위 3-class 값으로 기록하는 컨벤션 정비가 선행되어야 한다.

---

## 12. Train, Validation, Test 분류 정책

데이터는 window 단위가 아니라 세션 단위로 나누는 것이 좋다.

현재 한 세션은 다음 특징을 가진다.

```text
세션 하나 = 5분 측정 데이터
세션 하나 = 3개 클래스 중 하나의 클래스만 포함
세션 하나 = 약 991개 window 생성
```

3초 window와 0.3초 stride를 사용하면 한 세션 안의 window들은 서로 많이 겹친다. 따라서 같은 세션에서 나온 window를 랜덤으로 train, validation, test에 섞으면 안 된다.

잘못된 방식:

```text
20260513/session_1의 window 일부 -> train
20260513/session_1의 window 일부 -> validation
20260513/session_1의 window 일부 -> test
```

이 방식은 train과 test에 거의 비슷한 window가 같이 들어갈 수 있다. 그러면 test 성능이 실제보다 좋게 보일 수 있다.

권장 방식:

```text
20260513/session_1 전체 -> train
20260514/session_1 전체 -> validation
20260515/session_1 전체 -> test
```

즉, 하나의 세션은 반드시 train, validation, test 중 하나에만 들어가야 한다.

분류 정책은 다음을 따른다.

```text
1. split은 세션 단위로 한다.
2. 같은 세션에서 나온 window를 여러 split에 나누지 않는다.
3. train, validation, test 각각에 class 0, 1, 2가 모두 들어가게 한다.
4. 클래스별 세션 수 비율이 split마다 크게 치우치지 않게 한다.
5. test set은 최종 평가용으로 두고, 학습 중 튜닝에는 사용하지 않는다.
```

클래스별 세션이 충분히 많다면 다음 비율을 기본으로 사용한다.

```text
train:      70%
validation: 10~15%
test:       15~20%
```

예를 들어 클래스마다 세션이 10개씩 있다면 다음처럼 나눌 수 있다.

```text
class 0 empty:
  train:      7 sessions
  validation: 1 session
  test:       2 sessions

class 1 static:
  train:      7 sessions
  validation: 1 session
  test:       2 sessions

class 2 action:
  train:      7 sessions
  validation: 1 session
  test:       2 sessions
```

클래스마다 세션이 적을 때는 성능 평가를 신뢰하기 어렵다. 그래도 최소한 다음 정도는 필요하다.

```text
class 0 empty:  train 1, validation 1, test 1
class 1 static: train 1, validation 1, test 1
class 2 action: train 1, validation 1, test 1
```

이 경우에도 데이터가 매우 작으므로, 결과는 참고용으로만 보는 것이 좋다. 가능하면 클래스마다 5~10개 이상의 세션을 수집하는 것이 좋다.

세션과 라벨, split은 별도 CSV가 아니라 각 세션 디렉터리의 `session_meta_snapshot.yaml`을 기준으로 관리한다.

예:

```yaml
experiment:
  objective: "3-class activity classification"
  label_target: "empty"
  split_strategy: "train"
```

이 방식이 구현되면 전처리 코드는 각 세션을 순회하면서 해당 YAML의 `experiment` 아래 값을 읽는다.

```text
label_target:
  "empty"  -> class 0
  "static" -> class 1
  "action" -> class 2

split_strategy:
  "train"      -> train set
  "val"        -> validation set
  "validation" -> validation set
  "test"       -> test set
```

예를 들어 다음과 같이 해석한다.

```text
mac_collector_output/raw/20260616/session_1
  session_meta_snapshot.yaml
    experiment.label_target: "empty"
    experiment.split_strategy: "train"

  결과:
    이 세션에서 나온 모든 window의 y = 0
    이 세션에서 나온 모든 window는 train set에 추가
```

이렇게 하면 전처리 코드는 세션별 YAML을 읽어서 `X_train`, `y_train`, `X_val`, `y_val`, `X_test`, `y_test`를 만들 수 있다.

중요한 점은 `split_strategy`도 세션 단위로 적용된다는 것이다. 세션 하나의 window 일부만 다른 split으로 보내면 안 된다.

---

## 13. LSTM.py 구성 (현행)

```text
1. import
2. LSTMClassifier 클래스 (input_size는 생성 시 주입)
3. 학습용 설정값
4. make_dataloader(X, y) — Tensor 변환 + DataLoader
5. train(X, y, label_name, epochs) — 학습 루프
6. __main__ — argparse로 run_preprocessing() 호출 후 train() 실행
```

실행:

```bash
python model_train/model/LSTM.py \
    --session-dir mac_collector_output/raw/20260616/session_21 \
    --label empty --epochs 20
```

모델 클래스는 다음 책임만 갖게 한다.

```text
입력: (batch, 300, feature)
출력: (batch, num_classes)
```

전처리 책임은 `Preprocessing.py`에 두고, 학습 책임은 `LSTM.py`에 둔다.
`LSTM.py`를 import해도 전처리·학습이 자동 실행되지 않는다 (데이터는 `train(X, y)`로 주입).

---

## 14. 나중에 개선할 수 있는 부분

처음 버전이 동작하면 다음 개선을 고려한다.

```text
train/validation split 추가
모델 저장 torch.save 추가
validation accuracy 기준 best model 저장
confusion matrix 출력
클래스 불균형 확인
라벨을 세션 메타데이터에서 읽도록 변경
양방향 LSTM 적용
여러 LSTM layer 적용
```

처음부터 모두 넣기보다는, 입력 shape와 학습 루프가 정상 동작하는 것을 먼저 확인하는 것이 좋다.
