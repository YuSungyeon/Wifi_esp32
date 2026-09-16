# Session Boundary Trim Experiment

> 상태: **SUPPORTING ANALYSIS — 2026-09-16 전처리·seed 3개 재학습·비교 평가 완료**

앞뒤 5초 제거 재학습은 동일 중앙 구간의 empty↔static 교차 오분류율을 평균
43.83%에서 42.68%로 **1.16%p 줄였다**. 그러나 세 seed 모두 S10(empty)을 static,
S19(static)을 empty로 분류했다. 이번 실험에서 작은 수치 개선은 있었지만
static/empty의 세션 일반화 문제는 해결되지 않았다.

목적은 모든 세션의 앞뒤 5초를 제거한 재학습이 empty/static 혼동을 줄이는지
기존 LSTM baseline과 비교하는 것이다. 원본과 기존 run은 읽기 전용으로 사용한다.

## 고정한 실험 조건

- 데이터: 20260616, 기존 train/validation/test 세션 split과 class 배정 유지.
- 전처리: 안정 공통 구간의 양쪽 500 frame 제거 후 보간·window 재생성.
  자세한 범위 정의는 [전처리 계약](../preprocessing/design.md)을 따른다.
- 모델: 기존 2-layer LSTM, hidden 128, dropout 0.2, batch 32, Adam 0.001.
- Balanced class weight, seed 0·1·2, 최대 50 epoch, validation macro-F1 patience 5.
- 정규화와 class weight는 제거 후 train에서 다시 계산.
- 설정과 checkpoint는 validation으로만 선택하며 test 결과에 따라 수정하지 않는다.
- 기존 test는 이미 확인했으므로 이번 수치는 탐색적 비교다.

## 비교할 항목

기존 전체 test 결과와 제거 후 test 결과는 평가 구간도 달라 직접 비교에 제한이 있다.
이를 구분하기 위해 기존 checkpoint도 새 test window에 평가하되 **기존 학습의
정규화**를 사용한다. 새 모델은 새 학습의 정규화를 사용한다. 동일한 중앙 구간에서
두 모델의 macro-F1, empty/static recall·F1, 교차 오분류율, 세션별 예측을 비교한다.
기존 run 파일이나 checkpoint의 dataset hash를 변경하지 않고 별도 분석 산출물을 쓴다.

## 데이터와 재현

| Split | 기존 window | 앞뒤 5초 제거 후 window |
|---|---:|---:|
| Train | 16,728 | 16,177 |
| Validation | 5,933 | 5,732 |
| Test | 5,924 | 5,721 |

기존과 같은 29개 세션을 사용하고 session 22는 제외한다. 이 작업 트리에는 원본
데이터가 없으므로 원래 저장소의 원본과 baseline run을 읽는다. 결과는 현재 작업
트리에 저장한다. 아래 입력 경로는 실행 환경에 맞게 바꿀 수 있다.

```bash
conda run -n wifi-csi-lstm python model_train/preprocessing/preprocess_3rx.py \
  --raw-dir /Users/jaehyeog/my/Wifi_esp32/mac_collector_output/raw/20260616 \
  --output-dir model_train/preprocessing/output/20260616-trim5s \
  --trim-seconds 5

conda run -n wifi-csi-lstm python -u model_train/analysis/trim_boundary_experiment.py \
  --dataset-dir model_train/preprocessing/output/20260616-trim5s \
  --baseline-root /Users/jaehyeog/my/Wifi_esp32/model_train/lstm/runs \
  --output-dir model_train/analysis/output/20260916-trim5s \
  --device mps
```

재실행 시 새로운 출력 경로를 사용한다. 실행 스크립트는 seed 3개 학습을 모두
완료한 뒤 기존 checkpoint의 중앙 구간 평가와 새 모델 test를 수행한다.
`comparison.json`에 seed별 비교와 평균·모집단 표준편차를 기록하고, 각 run에
checkpoint·epoch metrics·test 예측을 남긴다. 실험 디렉터리의 `source/`에 실행
코드와 사전 실험 계획을 복사하고 hash를 기록한다.

## 결과

평균 ± 표준편차는 seed 0·1·2 세 결과의 모집단 표준편차(`ddof=0`)이며
신뢰구간이 아니다. 주 비교는 **같은 5,721개 중앙 구간 window**에서 한다.
Baseline에는 기존 train 정규화, 새 모델에는 제거 후 train 정규화를 각각 적용했다.

| Test 지표 | 기존 모델·기존 전체 구간 | 기존 모델·중앙 구간 | 재학습 모델·중앙 구간 |
|---|---:|---:|---:|
| 전체 accuracy | 0.7070 ± 0.0069 | 0.7082 ± 0.0068 | 0.7160 ± 0.0032 |
| 전체 macro-F1 | 0.7051 ± 0.0066 | 0.7061 ± 0.0065 | 0.7136 ± 0.0030 |
| Empty recall | 0.4946 ± 0.0000 | 0.4947 ± 0.0000 | 0.4947 ± 0.0000 |
| Static recall | 0.6241 ± 0.0207 | 0.6275 ± 0.0204 | 0.6510 ± 0.0095 |
| Motion recall | 1.0000 ± 0.0000 | 1.0000 ± 0.0000 | 1.0000 ± 0.0000 |
| Empty/static 두 class F1 평균 | 0.5578 ± 0.0098 | 0.5594 ± 0.0096 | 0.5704 ± 0.0045 |
| Empty/static 대상 accuracy | 0.5597 ± 0.0104 | 0.5614 ± 0.0103 | 0.5732 ± 0.0048 |
| Empty↔static 교차 오분류율 | 0.4399 ± 0.0103 | 0.4383 ± 0.0102 | 0.4268 ± 0.0048 |
| 전체 session accuracy | 0.6667 ± 0.0000 | 0.6667 ± 0.0000 | 0.6667 ± 0.0000 |

Empty/static 대상 accuracy의 분모는 실제 라벨이 empty 또는 static인 모든
window이며 motion으로 잘못 예측한 경우도 오답에 포함한다. 교차 오분류율은
같은 분모에서 empty→static과 static→empty의 합이다. 기존 모델에서 motion으로
틀린 일부 window 때문에 두 지표의 합이 항상 1인 것은 아니다. 두 class F1 평균도
원래 3-class confusion matrix에서 구하므로 binary classifier 실험 결과가 아니다.

### Seed별 학습과 test

| Seed | Best epoch | 완료 epoch | Best validation macro-F1 | 기존 모델 중앙 test macro-F1 | 재학습 중앙 test macro-F1 | 학습 시간 |
|---|---:|---:|---:|---:|---:|---:|
| 0 | 1 | 6 | 0.9660 | 0.6999 | 0.7106 | 88.2초 |
| 1 | 3 | 8 | 0.9210 | 0.7036 | 0.7126 | 114.7초 |
| 2 | 11 | 16 | 0.9921 | 0.7150 | 0.7177 | 224.0초 |

세 run 모두 validation 기준 조기 종료했다. 학습 시간은 epoch별 기록의 합이며
데이터 검증·모델 초기화·test 시간은 제외한다. 실행 환경은 기존 baseline과 같은
Python 3.11.16 / NumPy 2.4.6 / PyTorch 2.13.0 / Apple MPS / macOS 26.5.2다.
새 balanced weight는 empty `0.950526`, static `0.942551`, motion `1.127396`이다.

새 validation macro-F1 평균은 `0.9597 ± 0.0294`다. 기존 `0.9860 ± 0.0104`보다
낮으며 validation 평가 구간도 달라졌으므로 같은 window에 대한 직접 비교는 아니다.
Test만 소폭 오른 것을 이유로 기존 모델보다 일반화가 좋아졌다고 확정하지 않는다.

### Session별 결과

두 모델을 같은 중앙 구간에서 평가한 window accuracy다. 대표 예측은 window
softmax 확률의 세션 평균으로 구했다.

| Session | 실제 class | 기존 모델 accuracy 범위 | 재학습 accuracy 범위 | 재학습 대표 예측 (세 seed 공통) |
|---|---|---:|---:|---|
| 9 | empty | 100% | 100% | empty |
| 10 | empty | 0% | 0% | **static — 오분류** |
| 19 | static | 21.53–31.14% | 28.32–32.81% | **empty — 오분류** |
| 20 | static | 99.69–100% | 100% | static |
| 29 | motion | 100% | 100% | motion |
| 30 | motion | 100% | 100% | motion |

S19의 일부 window에서 개선됐으나 세션 대표 예측은 바뀌지 않았다. S10은 남은
957개 window 전부를 세 seed 모두 static으로 예측했다. Empty/static 세션만 보면
기존·재학습 모두 4개 중 2개를 맞혔다.

## 해석과 제한

- 같은 중앙 구간에서 전체 macro-F1은 `+0.0075`, static recall은 `+2.35%p`,
  empty/static 대상 accuracy는 `+1.18%p` 개선됐다. Empty recall과 세션 정확도는
  그대로다. 앞뒤 5초 제거만으로 충분한 구분 성능을 얻지는 못했다.
- 시간상 경계 잡음의 존재나 원인을 이 실험으로 입증하지는 못한다. 데이터 제거,
  window 시작 위치 변화, train 정규화·class weight 재계산, 학습 경로 변화가 함께
  발생했다. `500`이 stride `30`의 배수가 아니므로 새 window는 기존 grid에서
  20 frame 이동한 위상에 놓인다.
- 같은 test 세션을 이미 확인한 후 수행한 탐색적 비교이며, test는 class당 2개
  세션뿐이다. 중첩 window를 독립 표본으로 간주하지 않는다. 세 seed에서 같은
  방향의 작은 개선이 나타나도 새로운 날짜·배치의 성능 개선을 입증하지는 않는다.
- S10의 전 구간 오분류와 S19의 중간 신호 변화는 계속 검토할 대상이다. 후속 작업은
  라벨·수집 조건 확인과 기존 train/validation의 세션 단위 교차검증이며,
  최종 성능은 새로 수집한 미사용 세션에서 확인해야 한다.

## 검증과 산출물

- 전처리 테스트 29개, LSTM 테스트 4개, 기존 signal audit 테스트 2개 통과.
- 모든 27,630개 window의 경계·유한값 검사 완료. 기존 세션 split·사용/제외 여부·
  공통 범위·품질 gate 결과가 유지됨을 확인했다.
- Train window 전체에서 정규화 mean/std를 독립 재계산해 저장값과 대조했다.
- 기존·재학습 모델 3 seed의 총 34,326개 예측에서 window metadata 대응,
  확률 argmax, confusion matrix와 세션 집계를 독립 재검증했다.
- 제거한 경계의 극단값이 입력/정규화에 섞이지 않고, 제거된 frame을 사용해 경계
  누락을 보간하지 않는 테스트를 추가했다.
- Baseline checkpoint·manifest·normalization hash 대응을 검증하고 기존 파일은
  수정하지 않았다. 원본 JSONL 90개 hash는 로컬 `raw-provenance.json`에 기록했다.
- 공유용 seed별 수치: [comparison.json](assets/trim5s/comparison.json).
- 전처리 결과: `model_train/preprocessing/output/20260616-trim5s/` (Git 제외).
- 학습·평가·코드 snapshot: `model_train/analysis/output/20260916-trim5s/` (Git 제외).
- 재현 코드: [trim_boundary_experiment.py](../../analysis/trim_boundary_experiment.py).
