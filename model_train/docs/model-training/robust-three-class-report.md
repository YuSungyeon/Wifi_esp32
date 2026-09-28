# 상대 진폭·긴 문맥 기반 3클래스 모델 결과

> 상태: **SUPPORTING ANALYSIS — 학습·조건별 검증·중첩 검증 완료**
> 실행일: 2026-09-24 · 자료: 9/19·20, 60세션, 클래스별 20세션
> 설계: [robust-three-class-design.md](robust-three-class-design.md)
> 산출물: `model_train/analysis/output/20260924-robust-three-class/`

핵심 결과와 한계는 [쉬운 결과 설명](robust-three-class-report-easy.md)에 먼저 정리했다.

## 1. 결론과 사용 범위

**30초 상대 진폭 + 공유 시간 인코더 + 3-seed 평균**을 최종 사용 후보로 저장했다.
고정 후보의 날짜·배치 제외 5조건과 설정 선택을 학습 fold 안으로 제한한 중첩 검증
5조건 모두 실행 전 정한 성능 기준을 통과했다.

이 결과는 **기존 두 날짜 자료 안의 검증**이다. 9/20은 이전 실험에서도 이미
확인한 자료이며, 통계 모델과 신경망 중 최종 추천에는 이번 강건성 진단도 참고했다.
완전히 새로운 독립 시험 점수로 해석하지 않는다. 실제 설치 환경의 안정성 확정에는
새 날짜·조건에서 설정을 고정한 평가가 필요하다.

아래 점수는 각 평가 세션을 학습에서 제외한 checkpoint의 점수다. 최종
`neural/model.pt`는 검증 후 60세션 전체로 재학습한 모델 3개를 묶은 파일이다.
배포용 복사본 `model_train/robust/model.pt`를 Git에 포함했다. 두 파일은 동일한
체크포인트이며, 저장소에서 기본 추론 명령은 배포용 복사본을 읽는다.
이는 기존 데이터 학습 파일의 배포일 뿐 새 날짜·새 환경 검증을 뜻하지 않는다.
그 파일의 학습 자료 재평가 점수를 검증 성능으로 보고하지 않았다.

## 2. 고정한 30초 A 모델의 성능

아래는 seed 0/1/2의 **확률 평균 앙상블**이다. 윈도 30초, stride 15초다.
세션 판정은 해당 세션의 유효 윈도 확률 평균으로, 대략 5분을 집계한 오프라인 지표다.

| 평가에서 제외한 조건 | 세션 | Window macro F1 | Window accuracy | Session 정답 | 첫 유효 30초 정답 |
|---|---:|---:|---:|---:|---:|
| 9/20 (9/19로 학습) | 30 | 0.9729 | 97.31% | 30/30 | 30/30 |
| 9/19 (9/20으로 학습) | 30 | 0.9551 | 95.59% | 30/30 | 28/30 |
| P1 배치 | 24 | 0.9552 | 95.59% | 24/24 | 23/24 |
| P2 배치 | 18 | 0.9732 | 97.33% | 18/18 | 18/18 |
| P3 배치 | 18 | 0.9878 | 98.80% | 18/18 | 18/18 |

각 조건의 개별 seed도 session F1 1.000이었다. 같은 60세션을 반복 평가한 것이며
독립적인 360세션을 시험한 것이 아니다. 배치번호는 기록된 RX 위치이며 가구·사람·
전자기기 조건까지 같음을 보장하지 않는다.

**세션 점수 1.000은 30초마다 항상 맞는다는 뜻이 아니다.** 9/20으로 학습해 9/19를
예측할 때 static window recall은 86.77%였고, 첫 유효 윈도에서는 static 10세션 중
2세션을 empty로 판단했다. 상태 전환 구간·실시간 오경보율·전환 지연은 이 고정 라벨
5분 세션 자료로 검증하지 않았다.

30/30 정답의 세션 정확도 Wilson 95% 구간은 약 [0.8865, 1.0000]이다. 정답만 있는
표본의 단순 bootstrap이 [1,1]로 나오더라도 확실성 100%로 해석하지 않는다. 이 구간도
두 날짜가 새로운 환경을 대표한다는 증거는 아니다.

## 3. 설정 선택까지 분리한 중첩 검증

고정 후보의 역방향 진단에는 최초 개발 날짜가 평가 대상으로 들어간다. 이를 보강하기
위해 outer 학습 세션 안에서만 3-fold로 10/30초·A/P/AP를 비교했다. 5개 outer 설정을
모두 먼저 저장한 뒤 outer 예측을 계산했다. 동일 세션의 중첩 윈도는 fold를 넘지 않는다.

| Outer 평가 조건 | Inner에서 선택한 설정 | Session F1 평균 | Window F1 평균 | 최저 class recall | 기준 |
|---|---|---:|---:|---:|---|
| 9/20 | 30초 AP | 1.0000 | 0.9651 | 1.00 | 통과 |
| 9/19 | 10초 A | 0.9889 | 0.9319 | 0.90 | 통과 |
| P1 | 30초 A | 1.0000 | 0.9559 | 1.00 | 통과 |
| P2 | 30초 A | 1.0000 | 0.9675 | 1.00 | 통과 |
| P3 | 30초 AP | 1.0000 | 0.9868 | 1.00 | 통과 |

표는 seed 0/1/2 평균이고, 최저 recall은 모든 seed·클래스 중 최솟값이다. 이는 고정
30초 A checkpoint 자체의 점수와 다른 **모델 선택 절차의 검증**이다. 중첩 검증도
이미 탐색한 60세션의 재검증이므로 완전히 새로운 수집을 대신하지 않는다.

## 4. 긴 윈도와 상대 변화

기존 실험은 3초 절대 진폭·위상 채널을 CNN에 입력했다. 이번 A 후보는 각 톤의
윈도 평균으로 진폭을 나누고 윈도 안에서 중심화해 상대 변화를 입력한다. 모든 톤에
같은 시간 인코더를 적용하고 통계로 모아 특정 톤 번호 조합에 대한 의존을 줄였다.
위상은 후보 비교에 썼지만 최종 A 모델의 입력은 진폭뿐이다.

같은 통계 특징(A_rx)·같은 선형 모델(C=0.1)의 **9/19 development** 비교:

| 윈도 | Window F1 | Session F1 |
|---|---:|---:|
| 3초 | 0.9142 | 0.9666 |
| 5초 | 0.9353 | 1.0000 |
| 10초 | 0.9773 | 1.0000 |
| 20초 | 0.9873 | 1.0000 |
| 30초 | 0.9965 | 1.0000 |

긴 문맥은 이 비교에서 도움이 됐다. 그러나 이전 AP의 session F1 0.377에서 개선된
폭 전체를 윈도 길이 하나의 효과로 설명할 수는 없다. 정규화·구조·사용 윈도·학습
방식도 바뀌었다. 물리적으로 호흡을 검출했다는 원인 검증도 수행하지 않았다.

통계 모델은 5길이 × 6특징 묶음 × 6분류기 설정 = 180후보를 5-fold로 비교했다.
선택된 30초 A_rx 선형 모델의 날짜 제외 session F1도 0.9327/0.9312로 기준을 통과했다.
선형 모델은 결정적이므로 seed 반복을 독립적인 난수 안정성 증거로 보지 않는다.
신경망은 10/30초 × A/P/AP의 6후보를 별도 비교했다.

## 5. 최종 모델과 확인 사항

| 항목 | 값 |
|---|---|
| 파일 | 학습 산출물 `neural/model.pt` 및 Git 배포용 복사본 `model_train/robust/model.pt`; 모델 3개의 state dict와 입력 계약 포함 |
| 구조 | 톤 공유 Conv 8/16/16 → 시간 통계 → 톤 통계 → 분류 head |
| 파라미터 | 모델당 4,731개, seed 0/1/2 softmax 평균 |
| 원본 입력 | RX 101/102/103, RX당 raw 인덱스 `[38..63,2..26]` 51톤 |
| 실제 입력 | RX별 3개 간격 17톤, 총 51개 시계열 |
| 시간 | 100Hz → 0.1초 평균 → 30초/300개 시간 위치 |
| 진폭 | 윈도 내 톤 평균+0.5로 나눔 → 윈도 중심화 → ×3 → ±20 제한 |
| 학습 | AdamW 0.001, weight decay 0.01, label smoothing 0.1, dropout 0.3 |
| 종료·증강 | 15 epoch 고정; RX gain 0.8~1.2, 무작위 39/51톤 사용 |
| 최종 적합 | 60세션 전체, 1,124개 윈도, seed 0/1/2 |
| 판정 | 첫 관측 30초, stride 15초; 긴 미보간 gap 포함 윈도는 미판정 |

전처리 상세는 [robust-window-features.md](../preprocessing/robust-window-features.md)와
[ADR](../../../doc/adr-robust-three-class-experiment.md)에 있다. 사진·파일명·날짜·
세션 번호·배치·라벨·누락률은 모델 입력이 아니다. softmax는 별도 확률 보정을 거친
신뢰도가 아니다. GUI/실시간 스트림과의 연결은 이번 작업에 포함되지 않았다.

- 180개 raw 파일 hash를 원본 inventory와 재대조했고 중복 파일이 없었다.
- 30초 후보 1,133개 중 1,124개 유지. 9개는 미보간 진폭 gap으로 제외됐으며,
  이 길이에서는 위상 gate로 추가 제외된 윈도가 없었다.
- 신경망 입력 1,124개를 원래 정렬 배열에서 재생성해 정확히 일치함을 확인했다.
- 고정 통계·신경망 평가 30개에서 정답·세션 분리·지표·저장 모델을 재검증했다.
  MPS와 CPU 확률 차이는 5.97e-7 미만이고 argmax도 모두 일치했다.
- 49회 학습 세션 라벨 섞기에서 session F1 평균 0.2443, 최대 0.7500이었다.
  정상 라벨의 1.000은 재현되지 않았다. 선택 보정된 유의성 검정이나 모든 종류의
  누출·환경 교란이 없다는 증명으로 해석하지 않는다.
- 저장한 최종 모델을 다시 읽고 raw empty/static/motion 각 1세션에서 추론했다.
  진폭 배열과 예측이 캐시 경로와 정확히 같았다. 이는 학습 자료에서의 입출력 검사다.
- 후속 실험 테스트 13개와 기존 위상 테스트 11개가 통과했다.

NumPy 2.0.2의 정상 범위 행렬곱에서도 경고가 발생했다. 별도 비-BLAS 계산과 일치했고,
표준화 평균·분산과 선형 모델 확률을 독립 계산으로 검증했다. 관련
[NumPy 공식 이슈](https://github.com/numpy/numpy/issues/28687)와 같은 증상이다.
경고는 적합 결과에 보존하며 비정상 값 검사를 전역으로 끄지 않는다.

## 6. 실행·재현

```bash
.venv/bin/pip install -r requirements-robust.txt
.venv/bin/python model_train/robust/predict.py \
  --features model_train/analysis/output/20260924-phase-ablation/sessions/20260920-s45/features.npy

# <session-directory>를 실제 원본 수집 디렉터리로 교체한다.
.venv/bin/python model_train/robust/predict.py --session-dir '<session-directory>'
```

`--session-dir`은 CRC/RX 식별/tx_seq 정렬부터 검사하며, 기존 공통 27,000프레임 이상
gate를 재사용하는 **완료된 수집 세션용**이다. 정렬된 `(T,153)` 진폭 또는 `(T,459)`
phase51 배열은 `--features`로 30초부터 처리한다. `--output`은 윈도별 판정까지 JSON으로
저장하고 `--details`는 이를 출력한다. 추론은 CPU에서 동작한다.

새 결과 디렉터리에 재현:

```bash
ROBUST_RUN=model_train/analysis/output/robust-reproduction
.venv/bin/python model_train/robust/run_experiment.py prepare --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/run_experiment.py search --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/run_experiment.py stress --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/shared_temporal.py prepare --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/shared_temporal.py search --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/shared_temporal.py stress --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/shared_temporal.py negative-control --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/shared_temporal.py export --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/nested_validation.py --output "$ROBUST_RUN"
.venv/bin/python model_train/robust/verify_results.py --output "$ROBUST_RUN"
```

신경망 학습에는 Apple MPS 접근이 필요하며 같은 seed의 비트 단위 재현은 보장하지
않는다. `manifest.json`·`quality.json`·`cache/`는 입력·hash·유효량, `leaderboard.json`과
`selected.json`은 선택 과정, `neural/evaluation/`과 `neural/nested/`는 검증 checkpoint·
확률·분할을 보존한다. `neural/model-metadata.json`은 최종 입력·사용 범위,
`verification.json`은 재검산 결과다.

산출물 기준 경로는 문서 상단과 같다. 모델·배열은 Git에서 제외되는 로컬 파일이다.
원본 CSI, session manifest, 이전 모델은 변경하지 않았다. 다음 검증은 새 날짜의
empty/static/motion 교차 반복 및 상태 전환 자료에서 설정을 고정해 진행한다.
