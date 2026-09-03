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
3. 두 방식의 seed 3개 validation macro-F1 평균과 class별 지표를 비교한다.
4. Validation에서 선택한 한 방식의 seed 3개만 test한다.
5. Test 결과를 본 뒤 설정이나 checkpoint를 다시 선택하지 않는다.

보고서의 `평균 ± 표준편차`는 seed 3개에 대한 모집단 표준편차(`ddof=0`)다.
Window가 90% 겹치므로 window-level 지표와 session-level 지표를 함께 보고한다.

## 5. 학습과 validation 결과

### 5.1 개별 run

| Class weight | Seed | Best epoch | 완료 epoch | Validation macro-F1 | Session macro-F1 | 학습 시간 |
|---|---:|---:|---:|---:|---:|---:|
| None | 0 | 29 | 34 | 0.8746 | 0.8222 | 10분 32초 |
| None | 1 | 2 | 7 | 0.9998 | 1.0000 | 1분 59초 |
| None | 2 | 45 | 50 | 0.9414 | 1.0000 | 13분 10초 |
| Balanced | 0 | 29 | 34 | 0.9845 | 1.0000 | 10분 34초 |
| Balanced | 1 | 4 | 9 | 0.9742 | 1.0000 | 2분 38초 |
| Balanced | 2 | 2 | 7 | 0.9995 | 1.0000 | 2분 01초 |

모든 run은 best checkpoint 이후 patience 조건에 따라 종료되었다. 긴 run에서는
train 성능이 일찍 1.0에 도달한 뒤 validation macro-F1이 아주 작게 개선되는 동안
validation loss가 증가했다. 이는 맞고 틀림의 경계 지표는 소폭 개선되지만 일부
오분류 확률은 더 확신하는 과적합·확률 보정 저하 패턴이다.

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
  가능성이 크다.
- 현재 split의 validation session은 상대적으로 쉬웠으며 test session의
  `empty`와 `static` 분포 차이를 충분히 대표하지 못했다.
- Class weight는 validation 안정성을 개선했지만 session domain shift 자체를
  해결하지 못했다.
- Test session이 class당 2개뿐이므로 한 session 실패가 session accuracy를
  16.7%p 바꾼다. 더 많은 독립 session이 필요하다.

이는 로그와 예측에서 확인한 현상에 대한 해석이며, 수집 환경 차이 또는 라벨
오류를 직접 입증한 것은 아니다.

## 8. 결론

1. 공식 전처리 산출물을 읽고 학습·checkpoint·validation·test 결과를 저장하는
   LSTM 파이프라인은 실제 데이터에서 정상 동작했다.
2. Validation 기준으로는 `balanced`가 `none`보다 평균 성능과 seed 안정성이
   우수했다.
3. 최종 test에서는 motion 분류가 안정적이었지만 empty/static 일반화가 실패했다.
4. 현재 모델은 baseline 비교 기준으로 보존하되 실사용 모델로 채택하지 않는다.
5. 이 test 결과를 보고 현재 test set에 맞춰 설정을 다시 고르지 않는다.

## 9. 후속 작업

우선순위는 다음과 같다.

1. Session 10과 19의 라벨, 사람 위치, 장비 배치, 공간 상태, 수집 시각과 간섭
   조건을 원본 기록과 대조한다.
2. Train의 empty/static session과 실패 session의 feature 분포 및 시간 패턴을
   비교해 session 고유 shortcut을 찾는다.
3. 기존 데이터에서는 session group 기반 교차검증 또는 leave-one-session-out
   평가를 사용해 평균 일반화 성능과 어려운 session을 식별한다.
4. 다양한 위치·날짜·사람·장비 배치에서 독립 session을 추가 수집한다.
5. 모델이나 전처리를 조정한 뒤 최종 성능을 다시 주장하려면 기존 test를 재사용해
   선택하지 말고 새로운 untouched holdout session을 확보한다.
6. 데이터 평가 체계를 보완한 뒤 weight decay, 더 작은 모델, sequence pooling,
   세션 변화에 강한 정규화 같은 모델 개선을 비교한다.

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
