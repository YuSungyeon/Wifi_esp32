# Training Results Summary

> 상태: **SUPPORTING ANALYSIS — 2026-09-17 LSTM 기준모델 및 1D-CNN 비교 반영**
>
> 상세 보고서: [3-RX LSTM Baseline Training and Final Evaluation](lstm-baseline-report.md)

## 1D-CNN 추가 비교 (2026-09-17)

[실데이터 비교 보고서](cnn1d-vs-lstm-report.md)에 6회 학습·validation 선택·3회 test 결과를 기록했다.

| 지표 | LSTM balanced | 1D-CNN none |
|---|---:|---:|
| Validation macro-F1 | 0.9860 ± 0.0104 | 1.0000 ± 0.0000 |
| 탐색적 test macro-F1 | 0.7051 ± 0.0066 | 0.7015 ± 0.0084 |
| Test static recall | 0.6241 ± 0.0207 | 0.6127 ± 0.0263 |
| Test session accuracy | 0.6667 ± 0.0000 | 0.6667 ± 0.0000 |
| 파라미터 | 297,347 | 53,763 |
| 관측 epoch 시간 | 17.84초 | 4.87초 |

CNN은 학습 비용을 줄였지만 S10·S19 오류와 empty/static 일반화 문제를 해결하지 못했다.
Validation의 none·balanced가 모두 1.0000으로 동률이어서 사전 규칙에 따라 none을 선택했다.
속도는 동일 장치 종류의 서로 다른 날짜 측정이며 추론 속도는 측정하지 않았다.

## LSTM 기준모델 실험 개요

- 데이터: `20260616` 수집분, 사용 가능 session 29개
- 입력: 3 RX × CSI amplitude 64개, 3초 window `(300, 192)`
- 분할: train 17 / validation 6 / test 6 session
- 모델: 2-layer LSTM, hidden size 128, 파라미터 297,347개
- 실행: class weight `none`·`balanced` × seed `0`·`1`·`2`, 총 6회

## 주요 결과

Validation 결과에 따라 `balanced` class weight를 선택했다.

| 지표 | 결과 (seed 3개 평균 ± 표준편차) |
|---|---:|
| Validation macro-F1 | **0.9860 ± 0.0104** |
| Test accuracy | **0.7070 ± 0.0069** |
| Test macro-F1 | **0.7051 ± 0.0066** |
| Test static recall | **0.6241 ± 0.0207** |
| Test session accuracy | **0.6667 ± 0.0000** |

`motion` recall은 세 seed 모두 `1.0000`이었다. 반면 `empty`와 `static` 구분에서
성능이 하락했고, 세 seed 모두 test session 10과 19를 잘못 분류했다.

## 결론

학습·validation·checkpoint·test pipeline은 정상 동작하며 LSTM 기준모델로 사용할
수 있다. 그러나 validation macro-F1 대비 test macro-F1이 약 `0.2810` 낮아,
현재 모델을 실사용 모델로 채택하기에는 새 session 일반화 성능이 부족하다.

## 다음 작업

2026-09-10에 session 10·19의 원본 record·reader 로그 대조와 신호 1차 비교를
완료했다. [분석 결과와 그림](session-signal-audit.md)을 참고한다. Snapshot의 환경
정보는 신뢰하지 않아 근거에서 제외했으며 실제 사람 상태·배치는 미확인이다.
Domain shift를 포함한 주원인은 확정하지 않았다. 또한 LSTM의 `none`은 test하지 않아
class weight의 test 개선 효과는 비교할 수 없다.

1. **공동 확인 — 라벨·수집 기록:** 실제 사람 유무·정지 여부·위치·장비 배치·수집
   조건의 독립 확인이 남아 있다. 원본 record·로그의 구조적 일치는 확인했다.
   오분류만으로 라벨을 바꾸지 않고 미확정 사항을 남긴다.
2. **모델 담당 — 신호 비교:** empty session 9·10, static session 19·20과
   train/validation 세션의 RX별 진폭 평균·변동·시간 패턴 비교를 완료했다.
   S19의 약 90초 전후 신호 변화와 오분류 증가를 확인했으며 원인은 미확정이다.
3. **모델 담당 — 세션 단위 교차검증:** 기존 train/validation 23개 세션의 검증
   묶음을 바꿔가며 평가한다. 같은 세션의 window를 분리하지 않고 매 분할의 train으로
   정규화·class weight를 다시 계산한다. Class별 지표와 세션별 실패도 기록한다.
4. **수집 담당 — 독립 세션 확보:** 날짜·사람·위치·배치를 다양하게 구성하고 같은
   배치에서 세 class를 모두 수집한다. 조건을 기록하고 일부는 최종 holdout으로 남긴다.
5. **모델 담당 — 개선 비교:** 같은 교차검증 기준에서 더 작은 LSTM, weight decay,
   정규화 변경 등을 하나씩 비교한다. 코드·설정과 epoch별 validation 확률·loss를 보존한다.
6. **최종 평가 — 미사용 holdout:** 설정 선택을 완료한 뒤 새 세션으로 평가한다.
   실사용 허용 오분류 수준을 사전에 정하고 window/session·class별 결과를 함께 확인한다.

구체적인 목적과 실행 방법은 [보고서의 후속 작업](lstm-baseline-report.md#9-후속-작업)에
정리했다. 원본 대조·신호 비교는 완료했으며, 교차검증·추가 수집은 아직 실행하지 않았다. 이후 CNN 학습·비교는 위 추가 실험으로 완료했다.

기존 test split은 이미 결과를 확인했으므로 후속 모델의 설정 선택에는 사용하지
않는다. 같은 split의 추가 결과는 기존 LSTM과의 탐색적 비교로만 취급한다.
