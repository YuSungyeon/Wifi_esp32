# Model Training Documentation

> 상태: **CURRENT** — 전처리와 공식 3-RX LSTM·1D-CNN 코드는 구현되었고, 3-RX LSTM
> 기준모델 실험은 완료되었으며, 다른 모델 비교 실험은 **PLANNED**

어떤 모델을 시도할지에 대한 후보 비교·선정 근거는
[Model Comparison and Selection](model-training/model-comparison.md)을 먼저 본다.

코드와 문서를 분리한다. 실행 코드는 모델별 디렉터리에 두고, 전처리·모델 설계·
학습 문서는 이 `docs/` 디렉터리에 모은다.

```text
model_train/
├── docs/                              전처리·모델 설계·학습 문서
│   ├── README.md                         전체 문서 인덱스
│   ├── preprocessing/                   전처리 문서
│   │   ├── design.md
│   │   ├── sequence-analysis.md
│   │   ├── manifest-reference.md
│   │   └── legacy-preprocessing.md
│   └── model-training/                  모델 학습 문서
│       ├── model-comparison.md
│       ├── lstm-training.md
│       ├── cnn1d-training.md
│       ├── lstm-baseline-report.md
│       ├── training-results-summary.md
│       ├── public-dataset-review.md
│       ├── troubleshooting-log.md
│       └── session-signal-audit.md
├── analysis/                           기존 데이터·모델 산출물 진단
│   └── session_signal_audit.py
├── preprocessing/                      공통 3-RX 전처리 코드
│   └── preprocess_3rx.py
├── lstm/                               LSTM 실행 코드와 공통 학습·평가 runner
│   └── LSTM.py
└── cnn1d/                              1D-CNN 실행 코드
    └── CNN1D.py
```

## 읽는 순서

| 순서 | 분류 | 문서 | 상태 | 목적 |
|---:|---|---|---|---|
| 1 | 전처리 | [Preprocessing Design](preprocessing/design.md) | **CURRENT CONTRACT** | 모든 학습 모델이 공유하는 전처리 규칙과 완료 조건 |
| 2 | 전처리 | [Sequence Analysis](preprocessing/sequence-analysis.md) | **SUPPORTING ANALYSIS** | `seq`·`tx_seq` 판단 근거와 실데이터 집계 |
| 3 | 전처리 | [Manifest Reference](preprocessing/manifest-reference.md) | **CURRENT** | 전처리 설정·품질·split·normalization 필드 해석 |
| 4 | 모델 학습 | [Model Comparison and Selection](model-training/model-comparison.md) | **PLANNED** | 학습 모델 후보와 비교 계획, 완료된 LSTM 기준선 |
| 5 | 모델 학습 | [3-RX LSTM Design and Training](model-training/lstm-training.md) | **CURRENT** | 공식 3-RX 입력을 사용하는 LSTM 구조·학습·평가 방법 |
| 6 | 모델 학습 | [3-RX LSTM Baseline Training and Final Evaluation](model-training/lstm-baseline-report.md) | **SUPPORTING ANALYSIS** | Seed·class-weight 비교와 최종 test 결과·한계 |
| 7 | 모델 학습 | [Training Results Summary](model-training/training-results-summary.md) | **SUPPORTING ANALYSIS** | 현재까지의 학습 결과와 담당별 다음 작업 요약 |
| 8 | 모델 학습 | [Public Wi-Fi CSI Dataset Review](model-training/public-dataset-review.md) | **SUPPORTING ANALYSIS** | 외부 공개 데이터셋 후보·적합성·이용 조건과 적용 계획 |
| 9 | 모델 학습 | [Model Training and Evaluation Troubleshooting Log](model-training/troubleshooting-log.md) | **SUPPORTING ANALYSIS** | 전처리·학습·평가의 문제와 조치, 설명 정정 및 미해결 과제 |
| 10 | 모델 학습 | [Session 10 and 19 Signal Audit](model-training/session-signal-audit.md) | **SUPPORTING ANALYSIS** | 환경 snapshot을 제외한 원본 대조와 세션 신호·예측 비교 |
| 11 | 모델 학습 | [3-RX 1D-CNN Training](model-training/cnn1d-training.md) | **CURRENT** | 시간축 CNN 구조·학습·평가 방법, 실제 비교 실험은 미실행 |

## Historical Reference

다음 문서는 현재 pipeline에서 사용하지 않는 구형 구현 기록이다.

- [Legacy LSTM Preprocessing Implementation](preprocessing/legacy-preprocessing.md) (**HISTORICAL**)

문서 디렉터리와 파일명은 영문 `kebab-case`를 사용한다. 전처리 규칙의 기준 문서는
`preprocessing/design.md`이며 특정 모델에 종속되지 않는다. 모델별 문서는 공식
전처리가 만든 공통 산출물을 입력으로 사용한다.

공식 LSTM 실행법과 생성 파일은 5번 문서, 실제 baseline 상세 결과는 6번 문서를
기준으로 한다. 빠른 공유용 결과는 7번 문서, 외부 데이터 도입 검토는 8번 문서를
사용한다. 외부 데이터용 변환·학습 경로는 아직 구현되지 않았으며 공식 전처리
계약과 구분한다. 구형 `Preprocessing.py`는 공식 LSTM 학습 경로에서 import하거나
실행하지 않는다.
