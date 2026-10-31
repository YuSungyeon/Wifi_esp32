# 모델 학습 문서 안내

> 상태: **CURRENT** — 지금 돌아가는 모델과 끝난 실험 문서를 구분한 안내

**지금 실시간이 쓰는 모델은 여기 문서들이 설명하는 30초 모델이 아니다.**
진폭 15초 3분류(`amp60/model_amp1500_all74.pt`) + 진폭 3초 1단계
(`amp60/model_motion3s.pt`) 둘이고, 계약·실측은
[실시간 상태 판단](../../doc/realtime-inference.md) §2 가 정본이다.

이 폴더는 **담당자의 30초 3분류 모델**(`robust/model.pt`)을 설명한다. 9월 19·20일의
60세션으로 학습했고, 날짜·배치를 나눠 평가한 점수와 60세션 전체로 학습한 최종 파일의
독립 성능은 구분해야 한다. 조건이 다른 날(9/16·17)로 가면 세션 **12/15** 다.

## 먼저 읽을 문서

- [75세션 진폭·위상 재비교 계획](model-training/pilot-75-session-plan.md): 별도 파일럿의 후보·분할·평가 기준.
- [75세션 진폭·위상 비교 결과](model-training/pilot-75-session-report.md): 108회 학습 결과와 60초 위상·진폭의 장단점.
- [여섯 후보의 10/13 실시간 기록 비교 계획](model-training/pilot-live-comparison-plan.md): 진폭 3초 모델을 고정한 재생 평가. 10/6 제외.
- [10/13 실시간 기록 비교 결과](model-training/pilot-live-comparison-report.md): 여섯 후보와 현재 배포 모델의 최종 표시·전환 반응 비교.

| 문서 | 알 수 있는 내용 |
|---|---|
| [실시간 상태 판단](../../doc/realtime-inference.md) | **지금 돌아가는 모델**과 판정 규칙·실측 |
| [쉽게 읽는 학습 결과](model-training/robust-three-class-report-easy.md) | 30초 모델을 어떤 자료로 학습·평가했는지, 점수를 어디까지 믿을 수 있는지 |
| [전처리부터 CNN·학습까지 전체 흐름](model-training/robust-three-class-pipeline.md) | 서브캐리어 선택, 상대 진폭, CNN 크기와 특징 통합 과정 |
| [쉽게 읽는 실험 설계](model-training/robust-three-class-design-easy.md) | 무엇을 왜 비교하고 어떻게 평가하기로 했는지 |

## 현재 모델의 상세 근거

쉬운 설명보다 정확한 설정·수치·재현 방법이 필요할 때 읽는다.

- [30초 모델 실험 설계](model-training/robust-three-class-design.md): 후보와 선택·평가 기준.
- [30초 모델 학습·검증 결과](model-training/robust-three-class-report.md): 실제 점수, 최종 파일, 실행·재현 명령.
- [상대 진폭·시간 특징 만들기](preprocessing/robust-window-features.md): 입력 변환의 상세 규칙.
- [상대 진폭을 단계별로 보는 HTML](model-training/relative-amplitude-window-explainer.html): 평균을 구하고 300시점으로 줄이는 예시.

구조도는 전체 흐름과 쉬운 결과 문서 안에서 볼 수 있다. 그림 파일도 보존한다.

배포용 파일은 `model_train/robust/model.pt`다. 실시간 코드·제어판과 오프라인 평가의
판단 간격을 구분하려면 [실시간 상태 판단](../../doc/realtime-inference.md)을 읽는다.

## 공통 전처리

| 문서 | 용도 |
|---|---|
| [수신기 3대의 CSI 전처리 설계](preprocessing/design.md) | 원본 정렬·결측 처리와 3초 입력 규격 |
| [수신·송신 순번 분석](preprocessing/sequence-analysis.md) | `seq`와 `tx_seq`를 구분하는 근거 |
| [전처리 기록 파일 읽는 법](preprocessing/manifest-reference.md) | 설정·품질·학습/검증/평가 배정 기록 확인 |
| [수신기 3대의 1차원 CNN 학습 방법](model-training/cnn1d-training.md) | 1차원 CNN 기준 모델 실행법 |

## 이전 실험·검토 기록

현재 모델을 처음 이해할 때는 읽지 않아도 된다. 당시 결과와 데이터 사용 이력을
확인할 수 있도록 삭제하지 않고 보관했다. 과거 점수를 현재 모델의 점수로 쓰지 않는다.

- [이전 모델 실험·검토 기록 14개](model-training/archive/README.md) —
  2026-10-20 에 끝난 평가 보고서 3개(9/16·17, 9/17 평가·데이터 확인), LSTM 학습 방법,
  진폭·위상 3초 비교 설계를 여기로 모았다.
- [이전 LSTM 전처리 구현 기록](preprocessing/archive/legacy-preprocessing.md)

## 문서 관리 기준

상세 설정은 설계·결과 문서를 기준으로 하고, 쉬운 설명과 이 안내는 그 문서에 연결한다.
제목과 안내 링크는 한글로 표시하며, 파일명은 기존 참조를 유지하기 위해 영문으로 둔다.
코드·원본 수집 데이터·학습 산출물은 문서 보관 작업의 대상이 아니다.
