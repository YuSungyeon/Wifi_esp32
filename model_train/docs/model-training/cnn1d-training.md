# 3-RX 1D-CNN Training

> 상태: **CURRENT** — 구현 및 CPU 소형 fixture 검증 완료. 실제 데이터 비교 실험은 미실행.

실행 코드: [`CNN1D.py`](../../cnn1d/CNN1D.py).
입력은 [공식 전처리](../preprocessing/design.md)의 `(N, 300, 192)` raw amplitude다.
LSTM 학습 모듈의 memory map 로딩, train 통계 정규화, session split 검증,
학습·평가 루프를 공유한다. 구형 `Preprocessing.py`는 사용하지 않는다.

## 모델 구조

```text
(B, 300, 192)
 → transpose → (B, 192, 300)
 → Conv1d(192, 32, kernel=5, padding=2) → BatchNorm1d → ReLU → MaxPool1d(2)
 → Conv1d(32, 64, kernel=5, padding=2) → BatchNorm1d → ReLU → MaxPool1d(2)
 → Conv1d(64, 64, kernel=3, padding=1) → BatchNorm1d → ReLU
 → AdaptiveAvgPool1d(1) → Flatten → Dropout(0.2) → Linear(64, 3)
 → logits (B, 3)
```

192개 RX·서브캐리어 feature를 입력 channel로 두고 시간축에 합성곱을 적용한다.
Conv는 bias를 사용하지 않고 BatchNorm이 affine 변환을 담당한다.
기본 학습 파라미터는 53,763개다. 출력 class는 empty=0, static=1, motion=2다.
CrossEntropyLoss에 logits를 전달하며 평가에서만 softmax를 계산한다.

## 학습과 평가

Adam 1e-3, batch 32, 최대 50 epoch, patience 5, dropout 0.2를 기본값으로 한다.
Validation window macro-F1로 best checkpoint와 early stopping을 결정한다.
`--class-weight none|balanced`와 `--seed`를 지원한다. 비교는 seed 0·1·2로 수행하고
설정은 validation에서 선택한다. Test는 별도 명령으로만 예측·평가한다.
공통 dataset 검증은 train 단계에서도 세 split의 형식과 유한값을 검사한다.
기존 test split은 이미 LSTM 평가에 사용했으므로 추가 결과는
[비교 프로토콜](model-comparison.md)에 따라 탐색적 비교로 해석한다.

```bash
conda run -n wifi-csi-lstm python model_train/cnn1d/CNN1D.py validate \
  --dataset-dir model_train/preprocessing/output/20260616

conda run -n wifi-csi-lstm python model_train/cnn1d/CNN1D.py train \
  --dataset-dir model_train/preprocessing/output/20260616 \
  --seed 0 --class-weight balanced

conda run -n wifi-csi-lstm python model_train/cnn1d/CNN1D.py test \
  --dataset-dir model_train/preprocessing/output/20260616 \
  --run-dir model_train/cnn1d/runs/<선택한-run-id>
```

의존성은 `requirements-model.txt`를 사용한다. `--device auto`는 CUDA → MPS → CPU
순서로 선택한다. `--channels 32 64 64`, `--kernel-sizes 5 5 3`으로 세 블록을
설정할 수 있다. Kernel 크기는 양의 홀수여야 한다. 학습 결과는 기본적으로
`model_train/cnn1d/runs/`에 저장하며 `--run-dir`로 경로를 지정할 수 있다.

Run에는 config, dataset manifest, normalization, seed, source commit,
모델 종류·구조·파라미터 수, best checkpoint, epoch별 학습시간과 validation 지표를
기록한다. Test에서는 window/session 지표, 예측 JSONL, confusion matrix PNG를
추가한다. Session 예측은 window별 softmax 확률을 평균한 뒤 argmax한다.
같은 run의 test 재실행은 거부한다. LSTM checkpoint와 CNN checkpoint를 구분하며,
기존 모델 종류 필드가 없는 LSTM checkpoint도 LSTM 명령으로 읽을 수 있다.

## 검증 범위

[`test_cnn1d.py`](../../../tests/test_cnn1d.py)는 시간축/channel 배치, 출력과 역전파,
잘못된 구조·입력 거부, 소형 데이터 학습, checkpoint 복원 후 예측 일치,
모델 종류 불일치 거부, test 결과 저장·재실행 거부를 검사한다.
학습 중에는 train·validation DataLoader만 생성하는지도 확인한다.
실제 데이터 성능과 CUDA/MPS 실행은 이 fixture 검증에 포함되지 않는다.
