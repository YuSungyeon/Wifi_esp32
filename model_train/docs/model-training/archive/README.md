# 이전 모델 실험·검토 기록

> 상태: **HISTORICAL** — 현재 30초 모델 이전의 실험·검토 자료

처음 읽는 사람은 [모델 학습 문서 안내](../../README.md)로 돌아가면 된다.
이 폴더는 당시 결과와 데이터 사용 이력을 확인하기 위한 보관 공간이다.
문서의 ‘현재’, ‘다음 작업’, 추천 설정은 작성 당시 기준이며 지금의 진행 상태가 아니다.

## 보관 문서

| 문서 | 보존 이유 |
|---|---|
| [초기 모델 후보 비교와 선택 근거](model-comparison.md) | 초기 LSTM·CNN 선택의 배경 |
| [6월 16일 LSTM 기준 모델 평가 결과](lstm-baseline-report.md) | 기준 모델의 학습·검증·최종 평가 수치 |
| [6월 16일 1차원 CNN·LSTM 비교 결과](cnn1d-vs-lstm-report.md) | 같은 자료에서 두 모델을 비교한 결과 |
| [6월 16일 세션 10·19의 신호·오분류 분석](session-signal-audit.md) | 반복 오분류에 대한 원본 분석 |
| [이전 LSTM·CNN 학습 결과 요약](training-results-summary.md) | 당시 결과를 공유한 요약 |
| [9월 16·17일 파일럿 배치별 교차검증 결과](cnn1d-pilot-cv-report.md) | 9월 17일 세션의 과거 사용 기록 |
| [진폭·위상 3초 모델 비교 결과](amplitude-phase-classification-report.md) | 30초 후속 모델로 넘어간 비교 근거 |
| [공개 와이파이 CSI 데이터셋 검토](public-dataset-review.md) | 외부 자료의 당시 적합성 검토 |
| [이전 전처리·모델 학습의 문제 해결 기록](troubleshooting-log.md) | 해결 과정과 과거 미해결 문제 |

실험 그림·수치 파일은 기존 `../assets/`에 그대로 두었다.
원본 수집 데이터와 모델 파일은 옮기거나 삭제하지 않았다.
