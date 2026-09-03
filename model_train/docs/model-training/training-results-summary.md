# Training Results Summary

> 상태: **SUPPORTING ANALYSIS — 2026-09-02 기준 학습·최종 평가 완료**
>
> 상세 보고서: [3-RX LSTM Baseline Training and Final Evaluation](lstm-baseline-report.md)

## 실험 개요

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

- 모델 담당: session group 교차검증과 empty/static feature 차이 분석
- 데이터 수집 담당: 날짜·사람·위치·장비 배치가 다양한 독립 session 추가 수집
- 공동 확인: 오분류된 session 10·19의 라벨과 수집 조건 점검
- 최종 평가: 모델 선택에 사용하지 않은 새로운 holdout session으로 수행

기존 test split은 이미 결과를 확인했으므로 후속 모델의 설정 선택에는 사용하지
않는다. 같은 split의 추가 결과는 기존 LSTM과의 탐색적 비교로만 취급한다.
