# 파일럿 데이터 배치 단위 교차검증 — 10사이클 (1D-CNN → 위상 기반 모델)

> 상태: **SUPPORTING ANALYSIS — 2026-09-17~18, 탐색적 결과**
>
> 데이터: 파일럿 `20260916`·`20260917` 15세션 / 코드: [`cv_pilot.py`](../../cnn1d/cv_pilot.py)
> (기준 모델은 [`CNN1D.py`](../../cnn1d/CNN1D.py)의 `CNN1DClassifier`, 세션 정렬은 공식
> `preprocess_3rx.process_session`) / 산출물: `model_train/cnn1d/runs/pilot_cv/` (git 제외)

## 1. 결과 요약

- **정지(static)와 빈 방(empty)의 차이는 진폭이 아니라 위상에 있었다.** 같은 구조·같은 용량에서
  입력만 바꿨을 때 세션 정답이 25/39 → 33/39로 올랐다(사이클 6 → 7).
- **모델이 서브캐리어 조합을 외우지 못하게 한 구조**가 그다음으로 컸다(사이클 5 → 6, macro-F1 0.43 → 0.66).
- 최고 설정은 **사이클 9 — 위상 + 서브캐리어 공유 인코더 + 30초 윈도(5Hz)**: window macro-F1
  **0.833**, 세션 34/39, empty·static 세션 24/27(0.89). 수집 쪽 분석과 사이클 1~5에서
  "못 가른다"고 판단했던 empty/static이 우연 이상으로 갈렸다.
- 진폭을 위상과 함께 넣으면 오히려 나빠졌다(사이클 8) — 진폭이 배치 지문을 다시 들여온다.
- **주의: 같은 15세션으로 10개 설정을 비교·선택했다.** 사이클 9 점수는 낙관적이며, 학습·선택에
  쓰지 않은 새 날짜 데이터로 다시 확인하기 전에는 실사용 성능으로 인용할 수 없다.

## 2. 평가 방법

| 배치(조건) | 세션 | 비고 |
|---|---|---|
| A | 9/16 배치1 — s31 motion, s32 empty, s33 static | Y자 배치 |
| B | 9/16 배치2 — s34 static, s35 empty, s36 motion, s37 static | s37은 TX-RX101 직선상 정지 |
| C | 9/17 1주기 — s38 motion, s39 static, s40 empty | |
| D | 9/17 2주기 — s41 motion, s42 static, s43 empty | RX 윗면 벽쪽 회전 |
| E | 9/17 3주기 — s44 motion, s45 empty | static 없음 → 항상 train |

- **Leave-one-block-out 4-fold** (A~D를 하나씩 test, 나머지 전부 train). 모델 담당의 20260616
  실험에서 무작위 세션 분할이 validation 1.00 / test 0.70으로 갈라졌던 점을 반영해, test는 학습에
  없는 **배치**로만 구성했다.
- 15세션 전부 공식 품질 gate 통과. 긴 윈도의 유효 구간은 공식 전처리와 같은 규칙(누락 NaN frame이
  낀 윈도 제외)으로 재계산했고, 300/30 설정에서 공식 시작점과 15세션 모두 일치함을 확인했다.
- validation 없음 → epoch 10 고정. Adam 1e-3, batch 32, dropout 0.2, seed 0·1·2.
- 지표: fold×seed 12회 window macro-F1 평균 ± 표준편차, seed 합산 confusion의 class별 recall,
  세션 예측(세션 내 softmax 평균 argmax) 정확도.

## 3. 사이클별 결과

| # | 변경 | macro-F1 | recall e/s/m | 세션 | motion | empty·static | 주 오답 |
|---|---|---:|---|---:|---:|---:|---|
| 1 | 기준 — 모델 담당 설정(3s, train 통계 전역 정규화) | 0.196 ± 0.151 | .00/.18/.77 | 12/39 | 0.75 | 0.11 | 전부 motion |
| 2 | **윈도 정규화** `(x−윈도평균)/(윈도평균+1)` | 0.471 ± 0.082 | .28/.40/.93 | 21/39 | 1.00 | 0.33 | e·s 뒤섞임 |
| 3 | **10초 윈도·10Hz** (호흡 시간척도) | 0.498 ± 0.095 | .44/.28/.96 | 21/39 | 1.00 | 0.33 | 배치별 한쪽 쏠림 |
| 4 | 정규화를 **빈 방 기준 레벨**로 교체 | 0.388 ± 0.198 | .95/.17/.27 | 17/39 | 0.17 | 0.56 | motion 붕괴 |
| 5 | 윈도 정규화 + 빈 방 기준 결합(384ch) | 0.431 ± 0.241 | 1.0/.12/.44 | 20/39 | 0.50 | 0.52 | motion 붕괴 |
| 6 | **서브캐리어 공유 시간 인코더** | 0.655 ± 0.138 | .53/.48/.99 | 25/39 | 1.00 | 0.48 | s33·s37·s40 |
| 7 | **입력을 위상으로 교체** | 0.754 ± 0.061 | .65/.67/.94 | 33/39 | 1.00 | 0.78 | s37×3 s43×2 |
| 8 | 진폭 + 위상 결합 | 0.691 ± 0.169 | .49/.60/.99 | 29/39 | 1.00 | 0.63 | s35·s37·s40 |
| 9 | **위상 + 30초 윈도(5Hz)** | **0.833 ± 0.176** | .94/.75/.83 | **34/39** | 0.83 | **0.89** | s37×3 s36×2 |
| 10 | 위상 30초 + 다운샘플 평균·표준편차 풀링 | 0.770 ± 0.165 | .73/.80/.82 | 33/39 | 0.75 | 0.89 | s32×3 s36×3 |

배치별 macro-F1 (A/B/C/D): 사이클 9는 0.86 / 0.57 / 0.97 / 0.93.
무작위 기준은 3-class macro-F1 ≈ 0.33, empty·static 세션 이진 판정 0.50.

### 사이클 1 → 2: 입력 스케일

기준 설정은 거의 모든 window를 motion으로 예측했다(empty recall 0). 배치가 바뀌면 서브캐리어별
기저 진폭이 통째로 달라져 처음 보는 배치가 전부 "이상 신호"로 보였다. 윈도 안 상대 변동으로
바꾸자 motion이 분리됐다.

### 사이클 3~5: 호흡 시간척도·캘리브레이션 — 진폭으로는 실패

10초 윈도는 표준편차 안의 차이만 냈다. 빈 방 캘리브레이션(배치별 empty 앞 60초를 기준 진폭으로
삼고 그 구간은 학습·평가에서 제외)은 empty recall을 0.95까지 올렸지만 **시간 근접성 때문에
생긴 지름길**로 본다 — 남은 empty 윈도는 기준 직후이고 static은 5~10분 떨어져 드리프트가 쌓인다.
static recall은 0.12~0.17에 머물렀고 motion이 무너졌다.

### 사이클 6: 구조로 지문 억제

192채널 Conv는 "몇 번 서브캐리어가 어떻게 변하는가"를 배우므로 배치 지문을 외우기 쉽다.
모든 서브캐리어 시계열에 **같은 가중치**의 1채널 Conv를 적용하고 시간 통계(평균·표준편차) →
서브캐리어 통계로 풀링하면 특정 조합을 외울 수 없다. macro-F1 0.498 → 0.655, 배치 D는 0.85.
다만 세션 단위 empty·static은 13/27로 여전히 우연 수준이었다.

### 사이클 7: 위상이 결정적

입력만 진폭 → 위상 잔차로 교체(구조·윈도·epoch 동일). 세션 34→33/39가 아니라 25→33/39,
empty·static 세션 13/27 → 21/27. 표준편차도 0.138 → 0.061로 줄었다. 정지한 사람의 미세 움직임
(호흡 등 mm 단위 변위)은 진폭보다 위상에 먼저 나타난다는 통설과 맞는 방향이다.

위상 전처리: `.csi`의 raw I/Q를 공식 전처리와 같은 tx_seq 격자에 정렬하고(누락은 같은 규칙으로
≤5 frame만 복소 보간), 주파수축으로 unwrap한 뒤 직선 성분(상수=CFO, 기울기=타이밍 오프셋)을
최소자승으로 제거한 잔차를 쓴다. ESP32 LLTF 버퍼는 인덱스 38~63이 −26~−1, 1~26이 +1~+26임을
실데이터 경계 위상 점프로 확인했다(잘못된 순서 2.38rad vs 올바른 순서 0.037rad). 잔차는 최대
1.51rad로 wrap이 없고 시간 변동은 서브캐리어당 약 0.05rad다.

### 사이클 8: 진폭을 다시 넣으면 나빠진다

모달리티별 인코더를 따로 두고 결합해도 0.754 → 0.691, empty recall 0.65 → 0.49. 배치 B·C의
empty 세션이 seed 3개 모두 static으로 뒤집혔다. **진폭 채널이 배치 지문 경로를 다시 열어준다.**

### 사이클 9~10: 30초 문맥

호흡 주기가 3~5초라 10초 윈도에는 2~3주기뿐이다. 30초·5Hz로 늘리자 0.754 → **0.833**,
empty recall 0.94, 배치 C·D fold는 0.97·0.93. 대신 motion recall이 0.94 → 0.83으로 내려갔고
배치 B의 s36(motion)이 seed 2개에서 empty로 분류됐다 — 0.2초 평균 다운샘플이 빠른 위상 변화를
뭉갠 것으로 본다. 다운샘플 구간의 표준편차를 별도 채널로 추가한 사이클 10은 s37(LOS 정지)을
전 seed 정답으로 바꿨지만 s32(empty)를 잃어 전체로는 개선이 없었다(0.770).

## 4. 남은 실패

| 세션 | 증상 | 비고 |
|---|---|---|
| s37 (static, LOS) | 사이클 6~9 내내 empty로 분류 | TX-RX101 직선 위 정지. 사이클 10에서만 정답 |
| s36 (motion) | 사이클 9·10에서 empty로 | 5Hz 다운샘플이 빠른 변화를 평활 |
| 배치 B fold 전반 | 다른 배치보다 낮음(0.57) | 세션 4개(static 2개) 구성이 다름 |

## 5. 판단과 한계

1. **위상 기반 구성(사이클 9)은 처음으로 empty/static을 우연 이상으로 가른다.** 수집 쪽에서 진폭·
   위상 평균 프로파일로 "차이 없음"이라고 본 것과 모순되지 않는다 — 그 분석은 **시간 평균 프로파일**을
   봤고, 여기서 쓰는 건 **위상의 시간 변동 패턴**이다.
2. **실사용 채택은 아직 이르다.** 같은 15세션으로 10개 설정을 비교했고, fold당 test가 3~4세션이라
   세션 하나가 점수를 크게 흔든다. 윈도가 97% 겹쳐 window 수는 독립 표본이 아니다.
3. **파이프라인 영향**: 공식 전처리(`preprocess_3rx.py`)와 JSONL(`csi_amp`)은 진폭만 다룬다.
   위상 경로를 채택하려면 전처리가 `.csi`의 raw I/Q를 직접 읽거나 JSONL에 위상을 추가해야 한다
   ([data-schema.md](../../../doc/data-schema.md) 변경 필요).

## 6. 다음 실험 제안

1. **새 날짜 holdout 검증** — 사이클 9 설정을 고정하고, 학습·선택에 쓰지 않은 새 날 세션에서
   한 번만 평가한다. 이것 없이는 0.833을 인용하지 않는다.
2. **두 시간척도 결합** — motion은 10Hz(사이클 7), 정지는 30초·5Hz(사이클 9)가 좋았다. 빠른 척도와
   느린 척도를 각각 인코딩해 합치면 둘 다 잡을 가능성.
3. **s37 같은 LOS 정지 자세 반복 수집** — 정지 위치(차폐 여부)에 따라 난이도가 갈리는지 확인.
4. 같은 배치에서 empty·static 교차 반복 수집(캘리브레이션 방식 재검증의 전제, 5절 3번과 별개).

## 7. 재현

```bash
python model_train/cnn1d/cv_pilot.py cache          # 진폭(JSONL) 세션 정렬 캐시
python model_train/cnn1d/cv_pilot.py cache-phase    # .csi 위상 잔차 캐시
# 사이클 1~5 (기본 CNN1DClassifier)
python model_train/cnn1d/cv_pilot.py cv --name c1-baseline
python model_train/cnn1d/cv_pilot.py cv --name c2-window-norm --norm window
python model_train/cnn1d/cv_pilot.py cv --name c3-10s-10hz --norm window --window 1000 --downsample 10
python model_train/cnn1d/cv_pilot.py cv --name c4-empty-baseline --norm baseline --window 1000 --downsample 10
python model_train/cnn1d/cv_pilot.py cv --name c5-dynamics+baseline --norm both --window 1000 --downsample 10
# 사이클 6~10 (서브캐리어 공유 인코더)
python model_train/cnn1d/cv_pilot.py cv --name c6-shared-encoder --model shared --norm window --window 1000 --downsample 10
python model_train/cnn1d/cv_pilot.py cv --name c7-shared-phase --model shared --norm window --features phase --window 1000 --downsample 10
python model_train/cnn1d/cv_pilot.py cv --name c8-shared-amp+phase --model shared --norm window --features amp+phase --window 1000 --downsample 10
python model_train/cnn1d/cv_pilot.py cv --name c9-phase-30s --model shared --norm window --features phase --window 3000 --downsample 20
python model_train/cnn1d/cv_pilot.py cv --name c10-phase-30s-meanstd --model shared --norm window --features phase --pool mean+std --window 3000 --downsample 20
```

사이클 1~2는 `window`/`downsample` 옵션 추가 이전에 실행했으며 기본값(300/1)과 동일하다.
사이클 4와 5 사이에 입력 변환을 `featurize` 하나로, 사이클 9와 10 사이에 다운샘플 풀링을
리팩터링했고, 각 변경 후 모든 모드를 1 epoch 스모크 테스트로 확인했다.
Python 3.14, PyTorch 2.14.0, Apple MPS.
