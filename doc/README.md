# MeshSense 문서 인덱스

이 저장소는 문서 주도 개발을 사용합니다. 현재 동작과 data contract는 문서를 먼저 변경한 뒤 코드에 반영합니다.

## 읽는 순서

| 순서 | 문서 | 상태와 목적 |
|---:|---|---|
| 1 | [아키텍처](architecture.md) | **CURRENT** — 공식 ESP-NOW/USB 구조 |
| 2 | [빠른 시작](quickstart.md) | **CURRENT** — 재현 가능한 플래시·수집 절차 |
| 3 | [수집 프로토콜](collection-protocol.md) | **OFFICIAL DESIGN** — 무엇을 얼마나 어떤 순서로 찍을지 |
| 4 | [펌웨어](firmware.md) | **CURRENT** — TX/RX 동작과 상수 |
| 5 | [binary/JSONL 계약](data-schema.md) | **CURRENT CONTRACT** — frame v4·`.csi` 저장소·JSONL 내보내기 |
| 6 | [실시간 경로](realtime-uplink.md) | **CURRENT** — ESP-NOW 업링크 + USB 싱크 (무선 배치) |
| 7 | [`seq`와 `tx_seq` 패턴](sequence-patterns.md) | **CURRENT** — 두 순번의 차이·이상 패턴·실데이터 집계 |
| 8 | [공식 전처리 설계](../model_train/docs/%5B전처리%5D-설계.md) | **OFFICIAL DESIGN** — 모든 모델이 공유하는 3-RX 전처리 기준 |
| 9 | [후처리](postprocessing.md) | **CURRENT + EXPERIMENTAL** — 수집률·시각화와 모델 문서 연결 |
| 10 | [호스트 스크립트](../scripts/README.md) | **CURRENT** — CLI와 도구 책임 |
| 11 | [트러블슈팅](troubleshooting/README.md) | **CURRENT + HISTORICAL** — 종류별·시간순 (환경·수집률·스트림·리셋·세션·신호·측정·도구·무선) |
| 12 | [문서 주도 개발 규칙](documentation-policy.md) | **PROCESS CONTRACT** |
| 13 | [ADR-0001](adr-poc-only.md) | **ACCEPTED** — PoC 단일 경로 결정 |
| 14 | [수집 환경 정비 스프린트](sprint/2026-08-collection-hardening.md) | **HISTORICAL** — frame v4·세션/라벨 정비의 시도·막힌 지점·실측 |
| 15 | [모델 학습 문서](../model_train/docs/%5B문서%5D-목록.md) | **EXPERIMENTAL** — 전처리·모델 비교·설계·학습 문서 |

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
├── model_train/                 실험 단계 모델 코드
│   └── docs/                    전처리·모델 설계·학습 문서
├── mac_collector_output/        수집 결과 `.csi` 세션·JSONL 내보내기 (git 제외)
└── log/                         reader 로그 (git 제외)
```

`mac_collector/` 이름은 기존 경로 호환을 위해 유지합니다. UDP collector는 존재하지 않습니다. 펌웨어 디렉터리의 `_poc` 이름도 역사적으로 유지하지만 현재 공식 구현입니다.

문서 불일치를 발견하면 [문서 주도 개발 규칙](documentation-policy.md)의 변경 절차에 따라 문서와 코드를 같은 변경에서 수정합니다.
