# Model Training and Evaluation Troubleshooting Log

> 상태: **SUPPORTING ANALYSIS — 2026-09-09 기준**
>
> 범위: 20260616 데이터 전처리, LSTM 학습 6회, 선택된 설정의 test 3회,
> 결과 해석·재현성·문서 관리 과정에서 확인한 문제와 조치.
>
> 기존 코드·로그·문서를 대조한 기록이다. 이번 정리에서 추가 학습이나 test를
> 실행하지 않았으며, 원인이 미확정인 항목은 해결된 것으로 표시하지 않는다.

## 1. Issue Overview

| 항목 | 증상·문제 | 현재 상태 |
|---|---|---|
| 손상 record 판정 | Session 11의 정상 수집 구간을 너무 짧게 선택 | 전처리 판정 수정·반영 |
| 불완전한 세션 | Session 22의 RX102 데이터가 3개 record뿐 | 학습 대상에서 제외, 원본 부족은 미해결 |
| 중첩 window | 이웃 window가 90% 겹쳐 독립 표본으로 오해하기 쉬움 | 세션별 split·train 전용 정규화 적용 |
| 지표 해석 | Accuracy와 F1, window와 session, 확률 평균과 다수결 혼동 | 코드·로그 기준으로 설명 정정 |
| 학습 종료 | Train accuracy가 1.0인데도 학습이 계속됨 | 정상 동작 확인 |
| Validation loss | 일부 run에서 macro-F1은 소폭 상승하나 loss는 증가 | 현상 확인, 확률 변화 원인은 미확정 |
| Test 일반화 | 높은 validation 성능과 달리 세션 10·19 반복 오분류 | 미해결 |
| 재현성·Git | 학습 시 dirty 작업 트리, 원격 추적 대상 혼동 | 기록 확인·문서 커밋 분리, 당시 변경 복원은 미확정 |

## 2. Preprocessing Issues

### 2.1 Corrupted Record Mistaken for a Restart

**증상:** Session 11에서 약 5분의 수집 데이터 중 17,529 frame만 선택했다.

**확인 근거:** RX103에 다음과 같은 순번이 있었다.

```text
이전: seq=17456,      tx_seq=617288
현재: seq=3288334404, tx_seq=2411
다음: seq=17459,      tx_seq=617291
```

가운데 record의 번호만 비정상적이고 다음 record는 원래 흐름으로 돌아왔다.
`tx_seq` 감소 한 번을 TX 재부팅으로 판단한 것이 구간을 잘못 자른 원인이었다.

**조치·결과:** 앞·현재·뒤 record를 함께 검사해 단일 손상을 먼저 제거하고,
`seq`로 RX 실행 구간을 나눈 뒤 공통 `tx_seq` 범위를 선택하도록 정리했다.
Session 11의 공통 길이는 29,963 frame, 사용 window는 989개로 복구되었다.
손상 record 제거 후 남은 2-frame gap은 최대 5-frame 보간 기준 안에 있다.

**재발 방지:** 파일 전체를 먼저 순번 정렬하지 않는다. 손상과 재부팅을 구분하고,
제거 사유·선택 범위를 manifest에 남긴다.
근거: [Preprocessing Design](../preprocessing/design.md),
[Sequence Analysis](../preprocessing/sequence-analysis.md).

### 2.2 Incomplete Session 22

**증상:** RX102 파일의 JSON 형식은 정상이지만 record가 3개뿐이었다.
세 RX가 공유하는 구간도 3 frame으로, 최소 공통 길이 27,000 frame에 미달했다.

**조치·결과:** Session 22를 제외하고 29개 세션을 사용했다.
긴 누락을 반복 복사나 무제한 보간으로 채워 정상 세션처럼 만들지 않았다.
다른 세션의 짧은 내부 누락은 최대 5 frame까지만 보간하고, 긴 누락과 겹치는
window는 제외하는 규칙을 적용했다.

이는 불완전한 데이터의 학습 유입을 막은 조치다. Session 22의 원본 데이터를
복구했거나 수집 부족의 원인을 해결했다는 뜻은 아니다.
근거: [전처리 기준과 세션별 집계](../preprocessing/design.md).

### 2.3 Overlapping Windows and Data Leakage

**문제:** 300-frame window를 30 frame씩 이동하므로 이웃 window는 270 frame을
공유한다. 이를 무작위로 train/test에 나누면 같은 원본 frame이 양쪽에 들어갈 수 있다.

**적용한 예방 조치:** 세션 단위로 split을 고정하고 정규화 통계는 train에서만
계산했다. 결과는 train 16,728 / validation 5,933 / test 5,924 window다.
Window-level과 session-level 지표를 함께 보고한다.

90% 중첩만으로 점수가 반드시 높아지는 것은 아니다. 다만 window 수만큼 독립적인
실험을 했다고 볼 수 없고, session-level 결과를 추가해도 새 환경 검증을 대신할 수는 없다.
근거: [Baseline Report의 데이터와 split](lstm-baseline-report.md#2-데이터와-split).

## 3. Metric Interpretation Corrections

### 3.1 Accuracy, F1, and Evaluation Units

설명 과정에서 F1을 정답 비율처럼 표현하거나, 세션마다 F1을 구해 평균하는 것으로
오해할 여지가 있었다. 실제 계산은 다음과 같다.

| 지표 | 계산 대상·방법 |
|---|---|
| Window accuracy | 개별 window의 정답 수 ÷ 전체 window 수 |
| Window macro-F1 | 전체 window의 예측을 모아 `empty/static/motion` 각각의 F1을 구한 뒤 세 값을 평균 |
| Session macro-F1 | 세션마다 대표 예측 하나를 만든 뒤, 전체 세션 예측으로 클래스별 F1을 계산해 평균 |
| Seed 평균 ± 표준편차 | 같은 설정의 seed 0·1·2에서 나온 지표 3개의 평균과 표준편차 |

`empty`의 precision은 “empty라고 예측한 것 중 실제 empty 비율”, recall은
“실제 empty 중 empty로 찾아낸 비율”이다. F1은 두 값을 함께 평가한다.
따라서 macro-F1 `0.7051`을 “전체 window의 70.51%를 맞혔다”고 읽으면 안 된다.
현재 test window accuracy는 별도 값인 `0.7070`이다.

표준편차는 세 seed 결과에 대해 `ddof=0`으로 계산했다. `±`는 seed 간 변동이며
새 환경의 성능 범위나 신뢰구간을 뜻하지 않는다.
근거: [지표 계산 코드](../../lstm/LSTM.py), [실험 결과](lstm-baseline-report.md).

### 3.2 Session Prediction Uses Mean Probabilities

**정정:** 앞선 대화에서 session 대표값을 단순 다수결로 설명했지만, 구현은
모든 window의 클래스별 softmax 확률을 평균하고 가장 큰 클래스를 선택한다.

예를 들어 `empty/static/motion` 확률이 다음과 같다면:

```text
Window 1: 51%, 48%, 1% → empty
Window 2: 51%, 48%, 1% → empty
Window 3:  1%, 98%, 1% → static

세션 평균: 34.33%, 64.67%, 1% → static
```

다수결은 `empty`지만 실제 집계 방식은 `static`을 선택한다.
Session 10·19의 대표 예측도 이 확률 평균 방식으로 계산된 결과다.
근거: [LSTM.py의 session_level_results](../../lstm/LSTM.py),
[공식 평가 규칙](lstm-training.md).

### 3.3 Train Macro-F1 Is Also Logged

**정정:** 앞선 “train macro-F1은 로그에 기록하지 않는다”는 설명은 잘못되었다.
`history.jsonl`의 `train`에는 `accuracy`, `macro_f1`, 클래스별 지표와 `loss`가
실제로 저장되어 있다. 콘솔에는 그중 `train_loss`, `train_acc`만 출력된다.

보고서의 “train window accuracy가 1.0”이라는 설명은 `train.accuracy`를 가리킨다.
이는 한 epoch에서 각 batch를 학습하며 누적한 정답 비율이다. epoch가 끝난 후
고정된 모델로 train 전체를 다시 평가한 값은 아니다.
근거: [학습·출력 코드](../../lstm/LSTM.py),
[실제 history 예시](../../lstm/runs/20260902-205721-200700-seed0-balanced/history.jsonl).

## 4. Training and Validation Loss

### 4.1 Training Continues After Perfect Train Accuracy

**증상:** 모든 run에서 train accuracy가 epoch 3~4에 1.0에 도달했으나 학습이 계속됐다.

**원인:** Accuracy가 1.0이어도 정답에 부여한 확률이 충분히 높다는 뜻은 아니다.
실제 `empty`일 때 예측이 `empty 51%, static 48%, motion 1%`이면 정답이지만
loss는 남는다. 학습 코드는 매 batch마다 loss를 계산하고 역전파·Adam 갱신을 실행한다.

**판정:** 오류가 아니다. 종료는 validation window-level macro-F1의 개선 여부,
patience 5, 최대 epoch 50으로 결정한다. 개선 판정에는 `min_delta`도 적용된다.
Train accuracy가 1.0이 되는 순간 중단하는 조건은 없다.
근거: [학습 코드](../../lstm/LSTM.py), [실행 설정](lstm-baseline-report.md#3-모델과-실행-환경).

### 4.2 Validation Loss Increases While Macro-F1 Improves

**실제 로그:** 아래는 각 run의 가장 낮은 validation loss와 마지막 epoch의 값이다.

| 설정·로그 | 최저 loss (epoch) | 마지막 loss (epoch) |
|---|---:|---:|
| [none, seed 0](../../lstm/runs/20260901-232143-019347-seed0-none/history.jsonl) | 0.6216 (2) | 1.2756 (34) |
| [none, seed 1](../../lstm/runs/20260901-235744-975624-seed1-none/history.jsonl) | 0.0011 (4) | 0.0012 (7) |
| [none, seed 2](../../lstm/runs/20260902-000408-455264-seed2-none/history.jsonl) | 0.5246 (1) | 1.2778 (50) |
| [balanced, seed 0](../../lstm/runs/20260902-205721-200700-seed0-balanced/history.jsonl) | 0.1260 (2) | 0.2483 (34) |
| [balanced, seed 1](../../lstm/runs/20260902-210816-041966-seed1-balanced/history.jsonl) | 0.1239 (4) | 0.1498 (9) |
| [balanced, seed 2](../../lstm/runs/20260902-211113-658687-seed2-balanced/history.jsonl) | 0.0043 (2) | 0.0146 (7) |

`balanced` seed 0의 epoch 2 → 29에서는 macro-F1이 `0.9818 → 0.9845`로
조금 좋아졌지만 loss는 `0.1260 → 0.2386`으로 증가했다. 분류 점수 개선을 기준으로
checkpoint를 선택하므로 loss가 증가해도 best epoch가 뒤로 이동할 수 있다.
마지막 가중치와 저장된 best 가중치는 구분해야 한다.

**해석:** Macro-F1은 최종 클래스 예측에서 계산한다. 현재 validation loss는
무가중 cross-entropy로, 각 window의 실제 정답 클래스 확률이 낮을수록 커진다.
두 값은 함께 좋아져야 하는 지표가 아니다. Macro-F1 상승만으로 전체 정답 수가
반드시 늘었다고 판단할 수도 없다.

Train 성능이 높게 유지되는 동안 validation loss가 증가하는 것은 과적합을
의심할 근거다. 그러나 run마다 정도가 다르며, 특히 `none` seed 1의 loss는 매우 낮다.
모든 run을 동일하게 “심한 과적합”으로 결론 내리지 않는다.

### 4.3 Missing Per-Window Probability History

**미확정 사항:** Loss가 증가한 이유가 “오답의 확신 증가”인지, “정답으로 분류한
window의 정답 확률 감소”인지, 두 현상의 조합인지 현재 저장 자료로 정확히 분해하지 못했다.
전체 loss만으로 확률 보정 성능이 악화되었다고 확정할 수도 없다.

**확인한 저장 한계:**

- `history.jsonl`: epoch별 전체·클래스별 지표는 있지만 window별 확률은 없다.
- `validation-metrics.json`: 선택된 best epoch의 지표와 세션 평균 확률이 있다.
- `best-model.pt`: best가 갱신될 때 덮어쓰므로 과거 epoch 가중치를 모두 보존하지 않는다.
- `test-predictions.jsonl`: test 예측이므로 validation의 epoch별 변화 분석을 대신할 수 없다.

**가능한 후속 조치:** 저장된 best 모델을 validation에서 다시 추론하면 그 시점의
정답·오답별 loss 기여는 확인할 수 있다. 그러나 이전 epoch와의 변화를 복원하는 것은 아니다.
추가 학습에서는 같은 window ID의 정답·예측·3개 확률과 window별 loss를 epoch마다
저장하고, 정답 유지·오답 유지·정답 전환·오답 전환 집단의 loss 변화 기여를 비교한다.
집단별 평균뿐 아니라 개수와 loss 합도 기록해야 전체 loss 증가를 설명할 수 있다.

이 기록 기능과 추가 분석은 아직 구현·실행하지 않았다.
근거: [평가·산출물 저장 코드](../../lstm/LSTM.py).

## 5. Model Selection and Test Generalization

### 5.1 Class Weight Selection Without Test Feedback

**판단 문제:** `none`과 `balanced` 중 어느 설정을 사용할지 결정해야 했다.

**적용한 절차:** 각각 seed 0·1·2를 학습하고 validation window-level macro-F1의
seed 평균과 클래스별 지표를 비교했다. 평균은 `none 0.9386`, `balanced 0.9860`이었다.
`balanced`를 선택한 후 그 설정의 seed 3개만 test에 각각 한 번 평가했다.

Train class weight는 train 클래스 개수로 계산하며, validation loss는 두 방식 모두
무가중으로 계산한다. Class weight 사용 여부를 test 결과로 선택한 실험은 아니다.
근거: [평가 절차와 결과](lstm-baseline-report.md#4-평가-절차), [코드](../../lstm/LSTM.py).

### 5.2 Repeated Failures on Sessions 10 and 19

**증상:** 선택된 설정의 평균 validation window macro-F1은 `0.9860`이었지만,
test window macro-F1은 `0.7051 ± 0.0066`이었다. 세 seed 모두 같은 두 세션을 틀렸다.

| 세션 | 실제 라벨 | 세션 대표 예측 | 해당 세션의 window accuracy |
|---|---|---|---:|
| 10 | empty | static | 세 seed 모두 0.0000 |
| 19 | static | empty | seed에 따라 0.2099~0.3058 |

**확인한 범위:** 두 세션 모두 gap 기준으로 제외된 window가 없었고 RX 관측률도
기준을 통과했다. 단순 수신 누락만으로 공통 오분류 원인을 설명하기는 어렵다.
세 seed의 반복 실패는 seed 하나의 우연만을 원인으로 보기 어렵다는 근거다.

**미해결:** 환경·배치 차이, 라벨 문제, 특징 표현 또는 모델의 한계 중 무엇이
주원인인지는 확정하지 않았다. 데이터셋이 유일한 원인이라고 단정하지 않는다.
Class weight 도입도 이 일반화 문제를 해결한 것은 아니다.

**다음 조치:** 수집 담당자와 라벨·위치·배치·수집 기록을 대조하고 모델 담당자는
세션별 특징 분포를 비교한다. 설정 선택은 train/validation에서 수행하고, 기존 test
재사용 결과는 탐색적 분석으로 구분한다. 최종 검증에는 새 미사용 holdout이 필요하다.
근거: [세션별 결과와 한계](lstm-baseline-report.md#63-session별-결과),
[Training Results Summary](training-results-summary.md).

## 6. Reproducibility and Repository Management

**학습 당시 상태:** 모든 run의 `config.json`에 source commit
`6894e9cf535d629a51c61f9f11f34c7d55b52051`, `source.dirty=true`가 기록되어 있다.
이 commit만으로 당시 커밋되지 않은 작업 트리까지 복원할 수는 없다.
Dirty였다는 사실이 학습 코드 변경이나 run 간 코드 차이를 입증하는 것은 아니다.

**남은 조치:** 다음 실험은 실행 코드를 먼저 커밋하고, 데이터·정규화 hash와
설정·패키지 버전을 함께 보존한다. 커밋되지 않은 상태로 실행해야 한다면 diff와
필요한 미추적 소스 파일도 별도로 보관한다. 원래 실험의 변경 내용이 이미 복구됐다고
주장하지 않는다. 근거: [실제 run config](../../lstm/runs/20260902-205721-200700-seed0-balanced/config.json).

**원격 혼동:** 로컬 `feat/model`은 `forked-origin/feat/model`을 추적하지만 실제
문서 푸시 대상은 `origin/feat/model`이었다. 따라서 push 후에도 status의
`ahead`가 남을 수 있다. 이는 추적 중인 다른 원격과 비교한 결과다.

**문서 커밋 조치:** 문서 3개를 `228e798`로 커밋해 `origin/feat/model`에 푸시했다.
이미 stage되어 있던 `.gitmodules` 삭제는 경로를 지정한 `git commit --only`로
제외했다. 이후에도 커밋 전 staged 파일과 push 대상 원격을 함께 확인한다.
현재 문서는 그 이후 작성된 별도 작업이다.

## 7. Remaining Work

1. Epoch별 validation 확률·loss 기록을 추가해 loss 증가 원인을 직접 분석한다.
2. Session 10·19의 수집 조건과 라벨을 대조하고 일반화 실패 가설을 검증한다.
3. Train/validation에서 세션 단위 교차검증과 모델·전처리 개선을 비교한다.
4. 새로운 독립 holdout을 확보하고 선택 완료된 설정만 최종 평가한다.
5. 외부 데이터는 [Public Dataset Review](public-dataset-review.md)의 입력·라벨
   조건을 확인한 뒤 보조 실험으로 도입한다. 현재 파이프라인과 즉시 호환되는
   것으로 간주하거나 자체 수집 검증을 대체하지 않는다.
