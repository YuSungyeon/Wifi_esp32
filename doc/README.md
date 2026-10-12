# MeshSense 프로젝트 문서 안내

이 저장소는 문서 주도 개발을 사용합니다. 현재 동작과 데이터 규격은 문서를 먼저 변경한 뒤 코드에 반영합니다.

현재 완료한 작업과 남은 문제는 [10월 6일까지 작업·결과 요약](work-summary-20261006.md)에 있습니다.

다음 작업은 [위상 60초 모델 도입 작업 목록](plans/2026-10-08-phase60-introduction.md)에서
1~4번 순서로 체크하며 진행합니다. **1번 완료: 위상 모델을 별도 경로에 가져와
9/16·17의 14/15 결과를 재현했습니다.** [위상 모델 평가 결과](../model_train/docs/model-training/20260916-17-phase60-evaluation.md).
**2번 재생 비교도 완료했습니다:** 위상은 빈 방 구분이 개선됐지만 앉기 구간의 정지 일치율은 0%였습니다.
[진폭·위상 전환 재생 비교](../model_train/docs/model-training/20261006-phase60-transition-replay.md).

[정지 오판 원인 분석](../model_train/docs/model-training/20261008-phase60-static-diagnosis.md)에서는
이번 앉기 입력에 학습 정지의 느린 시간 패턴이 약하고, 모델이 그 패턴에 의존함을 확인했습니다.
2026-10-10 **3번 선택 연결도 완료했습니다.** GUI·CLI에서 진폭 30초/위상 60초를 고를 수 있습니다.
기본값은 진폭으로 유지했고, 위상으로 새 실제 전환 시험을 하는 **4번은 남아 있습니다.**
사용 방법은 [실시간 상태 판단](realtime-inference.md)에 있습니다.

새 방 자료로 파인튜닝하는 후속 작업은 [새 방 위상 모델 추가 학습 작업 목록](plans/2026-10-12-phase60-room-finetuning.md)에 있습니다.
**PLANNED** 상태이며 수집·자료 분리·일부 가중치 학습·다른 날 시험 순서로 진행합니다.

2026-10-13에는 요청에 따라 **기존 위상 3분류 모델을 이번 방 자료로 추가 학습하고 GUI·CLI에 연결했습니다.**
현재 다음 작업은 [새 가중치로 실시간 시험](../model_train/docs/model-training/20261013-phase60-room-finetuning.md)입니다.
[2단계 구조 설계](../model_train/docs/model-training/phase60-two-stage-design.md)는 후속 후보로 보존합니다.

현재 학습한 모델을 확인하려면 [모델 학습 문서 안내](../model_train/docs/README.md)에서
[쉬운 결과 설명](../model_train/docs/model-training/robust-three-class-report-easy.md)부터 읽습니다.
최근 자료 구성과 결과는 [9월 16·17일 데이터 확인·최종 모델 평가](../model_train/docs/model-training/20260916-17-model-evaluation.md)에 있습니다.
실제 행동 전환 결과는 [10월 6일 첫 실시간 전환 시험](../model_train/docs/model-training/20261006-live-transition-evaluation.md)에 있습니다.

## 읽는 순서

| 순서 | 문서 | 상태와 목적 |
|---:|---|---|
| 1 | [아키텍처](architecture.md) | **CURRENT** — 공식 ESP-NOW/USB 구조 |
| 2 | [빠른 시작](quickstart.md) | **CURRENT** — 재현 가능한 플래시·수집 절차 |
| 3 | [수집 프로토콜](collection-protocol.md) | **OFFICIAL DESIGN** — 무엇을 얼마나 어떤 순서로 찍을지 |
| 4 | [펌웨어](firmware.md) | **CURRENT** — TX/RX 동작과 상수 |
| 5 | [바이너리·JSONL 데이터 규격](data-schema.md) | **CURRENT CONTRACT** — frame v4·`.csi` 저장소·JSONL 내보내기 |
| 6 | [실시간 경로](realtime-uplink.md) | **CURRENT** — ESP-NOW 업링크 + USB 싱크 (무선 배치) |
| 7 | [실시간 상태 판단](realtime-inference.md) | **CURRENT** — 진폭 30초/위상 60초 선택·기본 0.3초 이동·상태 기계·제어판 |
| 8 | [`seq`와 `tx_seq` 패턴](sequence-patterns.md) | **CURRENT** — 두 순번의 차이·이상 패턴·실데이터 집계 |
| 9 | [공식 전처리 설계](../model_train/docs/preprocessing/design.md) | **CURRENT CONTRACT** — 기존 3초·192차원 기준 모델의 전처리 규칙 |
| 10 | [후처리](postprocessing.md) | **CURRENT** — 수집률·시각화와 공식 3-RX 모델 pipeline 연결 |
| 11 | [호스트 스크립트](../scripts/README.md) | **CURRENT** — CLI와 도구 책임 |
| 12 | [트러블슈팅](troubleshooting/README.md) | **CURRENT + HISTORICAL** — 종류별·시간순 (환경·수집률·스트림·리셋·세션·신호·측정·도구·무선) |
| 13 | [문서 주도 개발 규칙](documentation-policy.md) | **PROCESS CONTRACT** |
| 14 | [ADR-0001](adr-poc-only.md) | **ACCEPTED** — PoC 단일 경로 결정 |
| 15 | [수집 환경 정비 스프린트](sprint/2026-08-collection-hardening.md) | **HISTORICAL** — frame v4·세션/라벨 정비의 시도·막힌 지점·실측 |
| 16 | [모델 학습 문서 안내](../model_train/docs/README.md) | **CURRENT** — 현재 30초 모델 설명과 이전 기록 구분 |
| 17 | [진폭·위상 입력 규격 결정](adr-csi-phase-experiment.md) | **ACCEPTED** — 이전 3초 비교 실험의 별도 입력 경로 |
| 18 | [30초 3분류 입력 규격 결정](adr-robust-three-class-experiment.md) | **ACCEPTED** — 상대 진폭·긴 구간을 쓰는 후속 실험 |
| 19 | [모델 실험·학습 작업 기록](sprint/2026-09-phase-classification.md) | **HISTORICAL** — 실험·검증·설명 문서의 작업 이력 |
| 20 | [위상 실시간 모델 선택 결정](adr-phase60-realtime-selection.md) | **ACCEPTED** — 모델별 입력·실행 고정·판단 기록·검증 경계 |

## 문서 상태 규칙

- **CURRENT**: 현재 source code로 실행되는 동작만 기술합니다.
- **CURRENT CONTRACT**: producer와 consumer가 반드시 함께 지켜야 하는 형식입니다.
- **OFFICIAL DESIGN**: 아직 구현 전일 수 있지만 앞으로 구현할 공식 기준입니다.
- **SUPPORTING ANALYSIS**: 공식 설계의 근거가 되는 데이터 분석·해설입니다.
- **PLANNED**: 아직 구현되지 않았으며 실행 가능한 기능처럼 쓰지 않습니다.
- **HISTORICAL**: 과거 기록입니다. 현재 설정과 같은 표에 섞지 않습니다.

## 현재 저장소 구조

```text
Wifi_esp32/
├── doc/                         문서와 architecture decision
│   └── sprint/                  작업 로그 (시도·막힌 지점·실측)
├── esp-idf/                     ESP-IDF v5.2.2 submodule
├── esp32s3_csi_send_poc/        공식 ESP-NOW TX firmware
├── esp32s3_csi_recv_poc/        공식 CSI RX firmware (USB / ESP-NOW 업링크)
├── esp32s3_csi_sink/            실시간 경로 SINK firmware (업링크 → USB)
├── scripts/                     flash·registry·reader·visualization CLI
├── mac_collector/               registry·session metadata 보관
├── model_train/                 공식 전처리와 모델 학습 코드
│   └── docs/
│       ├── preprocessing/       전처리 설계·분석 문서
│       └── model-training/      모델 설계·학습 결과 문서
├── mac_collector_output/        수집 결과 `.csi` 세션·JSONL 내보내기 (git 제외)
└── log/                         reader 로그 (git 제외)
```

`mac_collector/` 이름은 기존 경로 호환을 위해 유지합니다. UDP collector는 존재하지 않습니다. 펌웨어 디렉터리의 `_poc` 이름도 역사적으로 유지하지만 현재 공식 구현입니다.

문서 불일치를 발견하면 [문서 주도 개발 규칙](documentation-policy.md)의 변경 절차에 따라 문서와 코드를 같은 변경에서 수정합니다.
