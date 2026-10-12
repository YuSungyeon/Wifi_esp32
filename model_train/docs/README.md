# 모델 학습 문서 안내

> 상태: **CURRENT** — 현재 30초 3분류 모델과 이전 실험 문서를 구분한 안내

현재 실시간의 기본 모델은 **30초 상대 진폭 + 공유 CNN + 모델 3개의 예측 평균**이다.
9월 19·20일의 60세션으로 최종 파일을 학습했다. 날짜·수신기 배치를 나눠
평가한 점수와, 60세션 전체로 학습한 최종 파일의 독립 성능은 구분해야 한다.
9월 16·17일의 15세션에 최종 파일을 적용한 최신 결과는 구간
**234/280(83.57%)**, 세션 **12/15(80.00%)**다. 빈 방·정지 혼동이 남았고,
특히 9/16에서는 정지한 사람을 빈 방으로 판단하는 오류가 확인됐다.
이후 [10월 6일 첫 실시간 전환 시험](model-training/20261006-live-transition-evaluation.md)에서는
빈 방을 주로 정지로 오판했다. 실시간 연결 완료를 실사용 안정성 확보로 해석하지 않는다.

2026-10-08에는 main의 **위상 60초 모델**을 `model_train/phase60/`에 별도로 가져와
같은 9/16·17의 **14/15세션** 결과를 재현했다. 기존 진폭 학습 코드·가중치는 그대로다.
[10/6 전환 원본 재생 비교](model-training/20261006-phase60-transition-replay.md)에서는
위상이 빈 방을 잘 맞혔으나 앉기 구간의 정지 일치율은 0%였다.
2026-10-10에는 GUI·CLI에서 **위상 60초를 시험 후보로 선택**할 수 있게 연결했다.
기본값은 진폭이며 새 실제 위상 시험은 작업 목록의 4번으로 남아 있다.

## 먼저 읽을 문서

| 문서 | 알 수 있는 내용 |
|---|---|
| [10월 6일 자료의 파인튜닝 전후 비교](model-training/20261013-room-finetuning-oct6-evaluation.md) | 정지 60초 이후 0.75%·정지 34창 전부 빈 방 오판, 걷기 하락과 전환 지연 |
| [이번 방 자료로 위상 모델 추가 학습·실시간 연결](model-training/20261013-phase60-room-finetuning.md) | 기존 3분류 모델 파인튜닝 완료, 학습 자료 105/105 재판정, 새 실시간 시험 방법 |
| [10월 12일 새 방 학습용 첫 측정 확인](model-training/20261012-room-adaptation-capture.md) | 침대 앉기·걷기·빈 방 원본 품질, 잠정 정답 구간, 정지 구간 뒤 RX103 재부팅 확인 |
| [위상 60초 모델의 2단계 분류 설계](model-training/phase60-two-stage-design.md) | PLANNED — 후속 후보. 현재는 기존 3분류 파인튜닝·실시간 시험을 우선 진행 |
| [새 방 위상 모델 추가 학습 작업 목록](../../doc/plans/2026-10-12-phase60-room-finetuning.md) | PLANNED — 소량 수집, 자료 분리, 일부 가중치 학습, 채택 기준 |
| [위상 60초 모델 도입 작업 목록](../../doc/plans/2026-10-08-phase60-introduction.md) | 다음 평가·재생·실시간 연결·전환 시험을 1~4번 순서로 체크하며 진행하는 계획 |
| [9월 16·17일 위상 60초 모델 재평가](model-training/20260916-17-phase60-evaluation.md) | 14/15 재현 결과, 기존 진폭과 세션별 비교, 남은 정지 오분류 |
| [10월 6일 진폭·위상 전환 재생 비교](model-training/20261006-phase60-transition-replay.md) | 같은 원본의 빈 방·정지·걷기 성능과 지연, 누락으로 30초/60초 판단 불가 |
| [위상 모델의 정지 오판 원인 분석](model-training/20261008-phase60-static-diagnosis.md) | 학습 60·평가 15세션 대조, 시간 패턴 의존 확인, 물리적 환경 원인은 미확정 |
| [쉽게 읽는 위상 모델의 앉기 오판 원인](model-training/20261008-phase60-static-diagnosis-easy.md) | 원인 분석 1~4번을 가상 숫자 예시와 검사 결과로 풀어쓴 설명 |
| [쉽게 읽는 학습 결과](model-training/robust-three-class-report-easy.md) | 어떤 자료로 학습·평가했는지, 점수를 어디까지 믿을 수 있는지 |
| [첫 실시간 상태 전환 시험](model-training/20261006-live-transition-evaluation.md) | 사용자 방의 실제 행동 전환·반응 지연·빈 방 오판·입력 누락 |
| [9월 16·17일 데이터 확인·최종 모델 평가](model-training/20260916-17-model-evaluation.md) | 최신 15세션의 품질·날짜별 점수와 정지 상태 오분류 |
| [9월 17일 최종 모델 평가 결과](model-training/20260917-model-evaluation.md) | 저장된 최종 파일의 실제 추가 평가와 빈 방 오분류 |
| [전처리부터 CNN·학습까지 전체 흐름](model-training/robust-three-class-pipeline.md) | 서브캐리어 선택, 상대 진폭, CNN 크기와 특징 통합 과정 |
| [쉽게 읽는 실험 설계](model-training/robust-three-class-design-easy.md) | 무엇을 왜 비교하고 어떻게 평가하기로 했는지 |
| [9월 17일 데이터 구성·품질 확인](model-training/20260917-data-audit.md) | 평가에 사용한 8세션의 구성, 사용 가능한 구간과 과거 사용 기록 |

## 현재 모델의 상세 근거

쉬운 설명보다 정확한 설정·수치·재현 방법이 필요할 때 읽는다.

- [30초 모델 실험 설계](model-training/robust-three-class-design.md): 후보와 선택·평가 기준.
- [30초 모델 학습·검증 결과](model-training/robust-three-class-report.md): 실제 점수, 최종 파일, 실행·재현 명령.
- [상대 진폭·시간 특징 만들기](preprocessing/robust-window-features.md): 입력 변환의 상세 규칙.
- [상대 진폭을 단계별로 보는 HTML](model-training/relative-amplitude-window-explainer.html): 평균을 구하고 300시점으로 줄이는 예시.

구조도는 전체 흐름과 쉬운 결과 문서 안에서 볼 수 있다. 그림 파일도 보존한다.

배포용 파일은 `model_train/robust/model.pt`다. 실시간 코드·제어판과 오프라인 평가의
판단 간격을 구분하려면 [실시간 상태 판단](../../doc/realtime-inference.md)을 읽는다.

## 기존 기준 모델과 공통 전처리

아래 문서는 기존 **3초·192차원 LSTM/1차원 CNN 경로**를 설명한다.
현재 **30초·51신호 공유 CNN 경로**와 입력 규칙이 같다는 뜻은 아니다.

| 문서 | 용도 |
|---|---|
| [수신기 3대의 CSI 전처리 설계](preprocessing/design.md) | 원본 정렬·결측 처리와 기존 3초 입력 규격 |
| [수신·송신 순번 분석](preprocessing/sequence-analysis.md) | `seq`와 `tx_seq`를 구분하는 근거 |
| [전처리 기록 파일 읽는 법](preprocessing/manifest-reference.md) | 설정·품질·학습/검증/평가 배정 기록 확인 |
| [수신기 3대의 LSTM 학습 방법](model-training/lstm-training.md) | 기존 기준 모델의 구조·학습·평가 실행법 |
| [수신기 3대의 1차원 CNN 학습 방법](model-training/cnn1d-training.md) | 기존 CNN 기준 모델 실행법 |

## 이전 실험·검토 기록

현재 모델을 처음 이해할 때는 읽지 않아도 된다. 당시 결과와 데이터 사용 이력을
확인할 수 있도록 삭제하지 않고 보관했다. 과거 점수를 현재 모델의 점수로 쓰지 않는다.

- [이전 모델 실험·검토 기록 9개](model-training/archive/README.md)
- [이전 LSTM 전처리 구현 기록](preprocessing/archive/legacy-preprocessing.md)
- [진폭·위상 3초 모델 비교 설계](model-training/amplitude-phase-classification-design.md):
  실험 코드가 출처 확인에 사용하는 경로이므로 위치는 유지했다. 현재 설계가 아닌 이전 기록이다.

## 문서 관리 기준

상세 설정은 설계·결과 문서를 기준으로 하고, 쉬운 설명과 이 안내는 그 문서에 연결한다.
제목과 안내 링크는 한글로 표시하며, 파일명은 기존 참조를 유지하기 위해 영문으로 둔다.
코드·원본 수집 데이터·학습 산출물은 문서 보관 작업의 대상이 아니다.
