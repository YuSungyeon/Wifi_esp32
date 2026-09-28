# 3-RX LSTM Baseline Training and Final Evaluation

> 상태: **SUPPORTING ANALYSIS — 2026-09-02 학습·validation·최종 test 완료**
>
> 대상 데이터: `model_train/preprocessing/output/20260616`
>
> 모델·평가 기준: [3-RX LSTM Design and Training](lstm-training.md)
>
> 전처리 기준: [3-RX CSI Preprocessing Design](../preprocessing/design.md)

이 문서는 공식 3-RX 전처리 산출물로 LSTM 기준모델을 학습하고 평가한 결과를
기록한다. Class weight 사용 여부를 test가 아닌 validation으로 선택한 뒤, 선택된
설정의 seed 3개를 test에 각각 한 번만 평가했다.

구체적으로 `none`과 `balanced`를 seed 0·1·2로 각각 학습하고, validation
macro-F1 평균이 더 높은 `balanced`를 선택했다. 그 후 `balanced`의 세 seed만
test했으며, test 결과를 보고 설정이나 checkpoint를 다시 고르지 않았다. 이는
test 데이터가 모델 선택에 섞여 성능이 부풀려지는 것을 막기 위한 절차다.

## 1. 요약

- `seed=0,1,2`와 class weight `none`, `balanced`를 조합한 학습 6회를 완료했다.
- Validation window-level macro-F1 평균은 `none`이 `0.9386`, `balanced`가
  `0.9860`이었다.
- 평균 성능이 높고 seed 간 편차가 작은 `balanced`를 최종 설정으로 선택했다.
- 선택된 seed 3개의 test window-level macro-F1은 평균 `0.7051 ± 0.0066`이었다.
- Test session-level macro-F1은 세 seed 모두 `0.6667`이었다.
- 세 seed 모두 session 10의 `empty`를 `static`으로, session 19의 `static`을
  `empty`로 분류했다.
- `motion`은 test에서 거의 완벽했지만 `empty`와 `static`의 새 세션 일반화가
  충분하지 않았다.

따라서 이 결과는 **현재 LSTM 기준모델이 학습 파이프라인 검증에는 유효하지만
실사용 모델로 채택하기에는 세션 일반화 성능이 부족함**을 의미한다.

## 2. 데이터와 split

입력은 RX101·RX102·RX103의 CSI amplitude를 같은 `tx_seq`로 정렬한 3초
window다.

```text
window shape = (300, 192)
300          = 100Hz × 3초
192          = RX 3개 × amplitude feature 64개
stride       = 30 frame = 0.3초
```

Session 간 원본 frame 공유를 막기 위해 session 단위 split을 그대로 사용했다.

| Split | Session | Empty | Static | Motion | 전체 window |
|---|---|---:|---:|---:|---:|
| Train | 1~6, 11~16, 21, 23~26 | 5,857 | 5,922 | 4,949 | 16,728 |
| Validation | 7, 8, 17, 18, 27, 28 | 1,971 | 1,980 | 1,982 | 5,933 |
| Test | 9, 10, 19, 20, 29, 30 | 1,961 | 1,982 | 1,981 | 5,924 |

Normalization의 `mean`과 `std_safe`는 train에서만 계산해 세 split에 동일하게
적용했다. 학습 전 세 split의 shape, dtype, metadata, session 중복, normalization,
NaN·무한대 전체 검사를 통과했다.

## 3. 모델과 실행 환경

| 항목 | 값 |
|---|---|
| 모델 | 2-layer LSTM + Dropout + Linear |
| 입력 크기 | `(B, 300, 192)` |
| Hidden size | 128 |
| Dropout | 0.2 |
| Batch size | 32 |
| Optimizer | Adam |
| Learning rate | 0.001 |
| 최대 epoch | 50 |
| Early stopping | validation macro-F1, patience 5 |
| Seed | 0, 1, 2 |
| 실행 환경 | Conda `wifi-csi-lstm` |
| Python / NumPy / PyTorch | 3.11.16 / 2.4.6 / 2.13.0 |
| 장치 | Apple MPS |
| 플랫폼 | macOS 26.5.2 arm64 |

`balanced`의 train class weight는 다음과 같다.

```text
empty  = 0.9520232
static = 0.9415738
motion = 1.1266923
```

모든 run은 source commit
`6894e9cf535d629a51c61f9f11f34c7d55b52051`을 기록했다. 다만 `config.json`의
`source.dirty`가 `true`이므로 commit ID만으로 작업 트리 전체를 완전히 복원할 수
없다는 재현성 제한이 있다.

## 4. 평가 절차

다음 순서를 test 전에 고정했다.

1. Class weight `none`, `balanced` 각각을 seed 0·1·2로 학습한다.
2. 각 run에서 validation window-level macro-F1이 가장 높은 checkpoint를 저장한다.
3. 두 방식의 seed 3개 validation window-level macro-F1 평균과 class별 지표를 비교한다.
4. Validation에서 선택한 한 방식의 seed 3개만 test한다.
5. Test 결과를 본 뒤 설정이나 checkpoint를 다시 선택하지 않는다.

보고서의 `평균 ± 표준편차`에서 평균은 같은 설정으로 학습한 seed 0·1·2의
지표 3개를 더해 3으로 나눈 값이다. 표준편차는 각 seed의 결과가 이 평균에서
얼마나 떨어져 있는지를 나타낸다. 여기서는 세 seed 결과를 이번 실험의 전체 결과로
보고 3으로 나누는 모집단 표준편차(`ddof=0`)를 사용했다. 따라서 `±` 값이 작으면
seed가 달라져도 결과가 비슷했다는 뜻이며, 이 값은 신뢰구간이나 오차 범위가 아니다.

Window는 300 frame이고 다음 window는 30 frame 뒤에서 시작하므로, 이웃한 두
window는 270 frame, 즉 90%를 공유한다. 서로 매우 비슷한 window가 반복되어
window-level 지표만으로는 새로운 세션 전체를 제대로 분류하는지 판단하기 어렵다.
그래서 각 window를 개별 예측으로 평가한 window-level 지표와, 한 세션에 속한
모든 window의 class별 softmax 확률을 평균한 뒤 가장 큰 class를 세션 대표 예측으로
정해 평가한 session-level 지표를 함께 보고한다.

## 5. 학습과 validation 결과

### 5.1 개별 run

| Class weight | Seed | Best epoch | 완료 epoch | Validation window macro-F1 | Validation session macro-F1 | 학습 시간 |
|---|---:|---:|---:|---:|---:|---:|
| None | 0 | 29 | 34 | 0.8746 | 0.8222 | 10분 32초 |
| None | 1 | 2 | 7 | 0.9998 | 1.0000 | 1분 59초 |
| None | 2 | 45 | 50 | 0.9414 | 1.0000 | 13분 10초 |
| Balanced | 0 | 29 | 34 | 0.9845 | 1.0000 | 10분 34초 |
| Balanced | 1 | 4 | 9 | 0.9742 | 1.0000 | 2분 38초 |
| Balanced | 2 | 2 | 7 | 0.9995 | 1.0000 | 2분 01초 |

#### Run 종료 기준

- `Best epoch`은 validation window-level macro-F1이 가장 높았던 epoch이다.
- 이 점수가 개선될 때마다 `best-model.pt`를 갱신한다.
- 이후 점수가 patience 기간 동안 개선되지 않으면 해당 run을 종료한다.
- Train window accuracy가 1.0에 도달하는 것은 종료 조건이 아니다.

#### Train window accuracy가 1.0이어도 학습이 계속되는 이유

Train window accuracy는 전체 train window 16,728개 중 모델이 최종 class를
올바르게 선택한 비율이다. Accuracy가 1.0이면 16,728개를 모두 맞혔다는 뜻이다.
하지만 모든 정답을 충분히 높은 확률로 예측했다는 뜻은 아니다.

예를 들어 실제 정답이 `empty`일 때 `empty` 확률이 51%이고 `static` 확률이
49%여도 최종 선택은 `empty`이므로 accuracy에는 정답으로 기록된다. 그러나 정답
확률이 낮아 train loss는 남아 있다. 이 loss를 줄이기 위한 역전파와 가중치 갱신은
계속된다.

```text
train window accuracy = 1.0
→ 모든 train window의 최종 class를 맞힘
→ train loss가 반드시 0이라는 뜻은 아님
→ loss가 남아 있으면 가중치 갱신이 계속됨
```

#### 긴 run에서 관찰한 현상

- 긴 run에서는 validation window-level macro-F1이 조금 좋아지는 동안
  validation loss가 증가했다.
- Macro-F1은 최종 class 예측으로 계산한 클래스별 F1의 평균이다. 이 값의 상승이
  전체 정답 window 수의 증가를 반드시 뜻하지는 않는다.
- Validation loss는 실제 정답 class에 부여한 확률이 낮을수록 커진다.
- 일부 오답을 더 강하게 확신하거나, 정답으로 분류한 window의 정답 확률이
  낮아지는 경우에도 loss가 증가할 수 있다.

이는 과적합을 의심할 근거다. 다만 epoch별 window 확률을 저장하지 않았으므로
어느 현상이 loss 증가에 얼마나 기여했는지는 확정하지 못했다. 확인된 수치와
추가 분석에 필요한 기록은 [Troubleshooting Log](troubleshooting-log.md)의
학습·validation loss 항목에 정리했다.

### 5.2 Class weight 비교

| Class weight | Validation accuracy | Validation macro-F1 | Session macro-F1 |
|---|---:|---:|---:|
| None | 0.9402 ± 0.0493 | 0.9386 ± 0.0512 | 0.9407 ± 0.0838 |
| Balanced | **0.9861 ± 0.0104** | **0.9860 ± 0.0104** | **1.0000 ± 0.0000** |

`balanced`는 validation macro-F1 평균이 `none`보다 `0.0474` 높았고 표준편차도
약 5분의 1이었다. Seed 1에서는 `none`이 높았지만 seed 3개의 평균과
안정성은 `balanced`가 우세했다. 사전에 정한 선택 기준에 따라 `balanced`를
최종 test 설정으로 확정했다.

## 6. 최종 test 결과

### 6.1 Seed별 결과

| Seed | Accuracy | Macro precision | Macro recall | Macro-F1 | Session accuracy | Session macro-F1 |
|---:|---:|---:|---:|---:|---:|---:|
| 0 | 0.7005 | 0.7003 | 0.6999 | 0.6990 | 0.6667 | 0.6667 |
| 1 | 0.7037 | 0.7034 | 0.7031 | 0.7019 | 0.6667 | 0.6667 |
| 2 | 0.7166 | 0.7171 | 0.7158 | 0.7142 | 0.6667 | 0.6667 |
| **평균 ± 표준편차** | **0.7070 ± 0.0069** | **0.7069 ± 0.0073** | **0.7063 ± 0.0069** | **0.7051 ± 0.0066** | **0.6667 ± 0.0000** | **0.6667 ± 0.0000** |

세 seed의 test macro-F1 편차가 작다는 것은 낮은 test 성능이 특정 초기값 하나의
우연한 실패가 아님을 보여준다.

### 6.2 Class별 결과

| Class | Precision | Recall | F1 |
|---|---:|---:|---:|
| Empty | 0.5664 ± 0.0135 | 0.4946 ± 0.0000 | 0.5280 ± 0.0058 |
| Static | 0.5551 ± 0.0081 | 0.6241 ± 0.0207 | 0.5875 ± 0.0137 |
| Motion | **0.9993 ± 0.0009** | **1.0000 ± 0.0000** | **0.9997 ± 0.0005** |

`motion`은 세 seed 모두 안정적으로 구분했다. 전체 성능 하락은 `empty`와
`static` 사이의 교차 오분류에서 발생했다.

### 6.3 Session별 결과

| Session | 실제 class | 세 seed의 session 예측 | Window accuracy 범위 | 결과 |
|---:|---|---|---:|---|
| 9 | Empty | Empty | 1.0000 | 세 seed 모두 성공 |
| 10 | Empty | Static | 0.0000 | 세 seed 모두 실패 |
| 19 | Static | Empty | 0.2099~0.3058 | 세 seed 모두 실패 |
| 20 | Static | Static | 0.9960~1.0000 | 세 seed 모두 성공 |
| 29 | Motion | Motion | 1.0000 | 세 seed 모두 성공 |
| 30 | Motion | Motion | 1.0000 | 세 seed 모두 성공 |

Session 10과 19는 세 seed에서 같은 방향으로 틀렸다. Session 10의 RX별 전체
관측률은 약 94.90~98.84%, session 19는 약 95.20~99.76%였고 두 session 모두
gap 기준으로 제외된 window가 없었다. 따라서 현재 metadata만으로는 단순 수신
누락을 공통 원인으로 보기 어렵다.

## 7. Validation과 test의 차이

선택된 `balanced` 설정의 validation macro-F1은 `0.9860 ± 0.0104`였지만 test는
`0.7051 ± 0.0066`으로 평균 `0.2810` 하락했다. Validation의 6개 session은 모두
맞혔지만 test에서는 세 seed가 동일한 2개 session을 틀렸다.

이 결과는 다음과 같이 해석한다.

- 모델이 행동 class 외에 session 고유의 환경·배치·신호 특성을 함께 학습했을
  가능성이 있다.
- 현재 split의 validation session이 상대적으로 쉬웠거나 test session의
  `empty`와 `static` 분포 차이를 충분히 대표하지 못했을 가능성이 있다.
- Class weight는 이번 validation에서 평균 성능과 seed 간 안정성을 개선했다.
  그러나 선택된 `balanced` 모델에도 test session의 반복 오분류가 남았다.
  `none`은 test하지 않았으므로 class weight의 test 성능 개선 효과는 비교하지 못했다.
- Test session이 class당 2개뿐이므로 한 session 실패가 session accuracy를
  16.7%p 바꾼다. 더 많은 독립 session이 필요하다.

Session domain shift(세션 간 환경·신호 분포 변화)는 원인 후보다. 수집 환경 차이,
라벨 문제, 전처리·모델의 한계 중 무엇이 주원인인지는 아직 확정하지 않았다.

## 8. 결론

1. 공식 전처리 산출물을 읽고 학습·checkpoint·validation·test 결과를 저장하는
   LSTM 파이프라인은 실제 데이터에서 정상 동작했다.
2. Validation 기준으로는 `balanced`가 `none`보다 평균 성능과 seed 안정성이
   우수했다.
3. 최종 test에서는 motion 분류가 안정적이었지만 empty/static 일반화가 실패했다.
4. 현재 모델은 baseline 비교 기준으로 보존하되 실사용 모델로 채택하지 않는다.
5. 이 test 결과를 보고 현재 test set에 맞춰 설정을 다시 고르지 않는다.

## 9. 후속 작업

2026-09-10에 session 10·19의 원본 record·reader 로그 대조와 신호 1차 비교를
실행했다. [Session Signal Audit](session-signal-audit.md)에 결과와 그림을 기록했다.
실제 라벨·환경의 독립 확인과 추가 수집·학습은 완료하지 않았다. 현재 모델과 실험
산출물은 후속 실험의 비교 기준으로 보존한다.

### 9.1 라벨과 수집 기록 확인

모델 담당자와 수집 담당자가 session 10·19의 실제 사람 유무, 정지 여부, 사람 위치,
장비 배치, 공간 상태, 수집 시간과 간섭 기록을 대조한다. 모델이 틀렸다는 이유만으로
라벨을 변경하지 않고 원본 기록으로 확인한다. 기록이 없으면 확인 불가로 남긴다.

사용자의 확인에 따라 `session_meta_snapshot.yaml`의 환경 정보는 신뢰하지 않으며
대조 근거에서 제외했다. 원본 record와 로그의 시각·순번·저장 건수는 확인했지만,
수집 당시의 실제 사람 상태와 배치를 입증한 것은 아니다.

산출물은 세션별 확인표다. 확인된 사실과 미확정 사항을 구분해 라벨 문제나 수집 조건
차이가 오분류 원인인지 검토할 근거를 마련한다.

### 9.2 성공·실패 세션의 신호 비교

모델 담당자는 같은 배정 class인 empty session 9·10과 static session 19·20을
비교하고, train/validation의 같은 class 세션도 함께 살펴본다. RX별 CSI amplitude의
평균·변동·시간 패턴을 원본과 전처리 결과에서 비교해 어느 단계에서 차이가 나타나는지
확인한다.

산출물은 세션별 신호 비교와 원인 가설이다. 차이를 발견해도 곧바로 원인으로 단정하지
않는다. 기존 test를 이용한 이 분석은 진단·탐색용이며, 최종 성능 검증은 새 데이터로 한다.

1차 분석에서 29개 세션의 28,585개 window를 원본에서 정확히 재현했다. 평균 CSI
형태는 S10이 train static, S19가 train empty에 더 가까웠으며, S19에서는 약 90초
전후 RX102 신호 변화와 오분류 증가가 함께 관측됐다. 이는 원인 후보를 좁히는 근거이며
환경 변화나 라벨 오류를 확정하지 않는다. 상세 수치는 위 분석 문서를 기준으로 한다.

### 9.3 세션 단위 교차검증으로 평가 보강

기존 train 17개와 validation 6개, 총 23개 세션에서 검증에 남기는 세션 묶음을
바꿔가며 반복 평가한다. 같은 세션의 window는 항상 같은 묶음에 두고, 각 분할의
train/validation에 세 class가 포함되도록 구성한다. 새로운 날짜·배치에 대한 성능을
평가하려면 해당 조건을 공유하는 세션들도 함께 묶는 방안을 검토한다.
다만 이번 데이터의 환경 그룹을 신뢰할 수 없는 snapshot으로 추정하지 않는다.
별도 확인 자료가 없으면 세션 단위 검증으로 범위를 한정한다.

각 분할마다 모델을 새로 학습하고 정규화 통계와 class weight는 해당 train에서만
다시 계산한다. 기존 17개 train 세션으로 계산한 정규화 통계를 그대로 재사용하지 않는다.
분할별 window/session macro-F1, class별 precision·recall·F1, 실패 세션과 seed 간
변동을 기록해 현재 validation 6개에서만 높은 성능이 나오는지 확인한다.

### 9.4 다양한 조건의 독립 세션 추가 수집

수집 담당자는 실제 사용 환경에서 예상되는 날짜·사람·위치·장비 배치 변화가 포함되도록
독립 세션을 추가 수집한다. 같은 장비 배치에서 empty/static/motion을 모두 수집해
특정 class와 배치가 우연히 연결되는 문제를 줄인다. 각 세션의 라벨과 수집 조건도
함께 기록한다.

이웃 window는 90% 중첩되므로 window 수뿐 아니라 독립 세션 수와 조건의 다양성을
확보한다. 추가 수집분 일부는 처음부터 최종 test용 holdout으로 지정해 모델 선택 중
열어보지 않는다.

### 9.5 동일한 평가 기준으로 모델 개선 비교

교차검증 분할과 선택 지표를 고정한 뒤 더 작은 LSTM, weight decay, 정규화 변경 등을
하나씩 비교한다. 전체 macro-F1뿐 아니라 empty/static의 class별 성능과 세션별
실패를 함께 확인한다. 모델의 성능 개선은 여러 검증 분할과 seed에서 확인한다.

다음 학습 전에는 실행 코드를 커밋하고 설정·데이터·정규화 hash를 보존한다.
Epoch별 validation window ID·정답·예측 확률·loss도 기록해 loss 증가 원인을
분석할 수 있게 한다. 기록 한계는 [Troubleshooting Log](troubleshooting-log.md)의
4.3절과 6절을 참고한다.

### 9.6 새로운 미사용 세션으로 최종 검증

모델·전처리·설정 선택을 끝낸 뒤 9.4에서 확보한 미사용 holdout으로 최종 평가한다.
Window/session 지표와 class별 성능을 함께 보고하고 새 세션에서 empty/static
구분이 개선됐는지 확인한다. 실사용에 필요한 허용 오분류 수준도 평가 전에 정한다.

기존 test의 추가 결과는 진단·탐색적 비교로 취급하며, 그 점수에 맞춰 설정을 고르지
않는다. 새 holdout 결과를 보고 다시 모델을 조정한다면, 그 데이터도 개발에 영향을 준
것이므로 이후 최종 검증에는 또 다른 미사용 데이터가 필요하다.

## 10. 재현 산출물

### 10.1 Class weight 없음

| Seed | Run summary | 학습 로그 | Validation 결과 |
|---:|---|---|---|
| 0 | [summary](../../lstm/runs/20260901-232143-019347-seed0-none/run-summary.json) | [history](../../lstm/runs/20260901-232143-019347-seed0-none/history.jsonl) | [metrics](../../lstm/runs/20260901-232143-019347-seed0-none/validation-metrics.json) |
| 1 | [summary](../../lstm/runs/20260901-235744-975624-seed1-none/run-summary.json) | [history](../../lstm/runs/20260901-235744-975624-seed1-none/history.jsonl) | [metrics](../../lstm/runs/20260901-235744-975624-seed1-none/validation-metrics.json) |
| 2 | [summary](../../lstm/runs/20260902-000408-455264-seed2-none/run-summary.json) | [history](../../lstm/runs/20260902-000408-455264-seed2-none/history.jsonl) | [metrics](../../lstm/runs/20260902-000408-455264-seed2-none/validation-metrics.json) |

### 10.2 Class weight 적용 및 최종 test

| Seed | Run summary | 학습 로그 | Validation | Test | Confusion matrix |
|---:|---|---|---|---|---|
| 0 | [summary](../../lstm/runs/20260902-205721-200700-seed0-balanced/run-summary.json) | [history](../../lstm/runs/20260902-205721-200700-seed0-balanced/history.jsonl) | [validation](../../lstm/runs/20260902-205721-200700-seed0-balanced/validation-metrics.json) | [test](../../lstm/runs/20260902-205721-200700-seed0-balanced/test-metrics.json) | [PNG](../../lstm/runs/20260902-205721-200700-seed0-balanced/confusion-matrix.png) |
| 1 | [summary](../../lstm/runs/20260902-210816-041966-seed1-balanced/run-summary.json) | [history](../../lstm/runs/20260902-210816-041966-seed1-balanced/history.jsonl) | [validation](../../lstm/runs/20260902-210816-041966-seed1-balanced/validation-metrics.json) | [test](../../lstm/runs/20260902-210816-041966-seed1-balanced/test-metrics.json) | [PNG](../../lstm/runs/20260902-210816-041966-seed1-balanced/confusion-matrix.png) |
| 2 | [summary](../../lstm/runs/20260902-211113-658687-seed2-balanced/run-summary.json) | [history](../../lstm/runs/20260902-211113-658687-seed2-balanced/history.jsonl) | [validation](../../lstm/runs/20260902-211113-658687-seed2-balanced/validation-metrics.json) | [test](../../lstm/runs/20260902-211113-658687-seed2-balanced/test-metrics.json) | [PNG](../../lstm/runs/20260902-211113-658687-seed2-balanced/confusion-matrix.png) |

원본 데이터 계약과 품질 통계는
[`manifest.json`](../../preprocessing/output/20260616/manifest.json), 실행별 모델·환경·
hash는 각 run의 `config.json`, window별 test 예측은 각 run의
`test-predictions.jsonl`을 기준으로 한다.
