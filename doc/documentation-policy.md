# 문서 주도 개발 규칙

> 상태: **PROCESS CONTRACT**
> 적용 범위: firmware, host scripts, data schema, preprocessing, model

## 1. 원칙

MeshSense에서는 코드가 동작해도 문서가 현재 상태를 설명하지 못하면 완료가 아니다.

변경 순서:

```text
요구사항
  → 문서의 CURRENT/CONTRACT/OFFICIAL DESIGN/PLANNED 변경
  → 구현
  → 검증
  → 문서-코드 대조
```

문서는 목표를 과장하지 않고 현재 구현 경계를 드러내야 한다.

## 2. 문서 상태

모든 핵심 문서는 제목 아래에 상태를 표시한다.

| 상태 | 의미 |
|---|---|
| `CURRENT` | 현재 code로 실행되는 동작 |
| `CURRENT CONTRACT` | producer/consumer가 함께 지키는 형식 |
| `OFFICIAL DESIGN` | 아직 구현 전일 수 있지만 앞으로 구현할 공식 기준 |
| `SUPPORTING ANALYSIS` | 공식 설계의 근거가 되는 데이터 분석·해설 |
| `EXPERIMENTAL` | 실행 코드는 있으나 공식 pipeline이 아님 |
| `PLANNED` | 설계만 있고 아직 구현되지 않음 |
| `HISTORICAL` | 과거 실험·결정 기록 |
| `ACCEPTED` | 채택된 architecture decision |

현재값과 과거값을 같은 표의 “현재 설정”으로 섞지 않는다.

`model_train/docs/`는 `preprocessing/`과 `model-training/`으로 분류하고,
파일명은 영어 소문자 kebab-case를 사용한다. 예: `design.md`,
`model-comparison.md`.

## 3. 변경별 선행 문서

| 변경 | 먼저 수정할 문서 |
|---|---|
| 전체 data path/지원 topology | `architecture.md`, ADR |
| TX/RX RF·CSI 설정 | `firmware.md` |
| binary header/JSONL field | `data-schema.md` |
| flash·수집 실행 순서 | `quickstart.md`, `scripts/README.md` |
| window/feature/label/split | `model_train/docs/preprocessing/`과 `model_train/docs/model-training/`의 설계 문서 |
| 개발 방식/완료 조건 | 이 문서 |

## 4. Data contract 변경

Binary/JSONL 변경은 다음을 한 change set에서 처리한다.

1. schema 문서 version과 field 정의
2. firmware producer
3. Python reader consumer
4. downstream preprocessing/visualization 영향
5. valid/invalid frame 검증
6. architecture/quickstart 링크 검증

Producer만 또는 consumer만 먼저 merge하지 않는다.

## 5. Architecture decision

다음 변경은 `doc/adr-<decision>.md` 파일에 기록한다.

- 공식 transport 변경
- firmware 역할·토폴로지 변경
- 저장 format 변경
- multi-RX sync 기준 변경
- 모델 input contract 변경

ADR은 Context, Decision, Consequences, Status를 포함한다. 채택된 결정을 뒤집을 때 기존 ADR을 삭제하지 않고 새 ADR로 supersede한다.

## 6. Definition of Done

변경 완료 전 확인:

- [ ] 관련 CURRENT 문서가 실제 구현과 일치한다.
- [ ] 미구현 기능은 PLANNED/EXPERIMENTAL로 표시했다.
- [ ] 삭제한 file/path를 가리키는 링크와 command가 없다.
- [ ] firmware producer와 host consumer 상수가 일치한다.
- [ ] 문서의 실행 command가 현재 file 구조에서 유효하다.
- [ ] Markdown 상대 링크가 모두 존재한다.
- [ ] Python syntax/import 검증을 통과한다.
- [ ] registry/session 같은 사용자 데이터는 보존했다.
- [ ] destructive 변경은 삭제 대상과 복구 가능성을 기록했다.

## 7. 리뷰 질문

리뷰어는 다음을 먼저 확인한다.

1. 이 문서가 CURRENT와 PLANNED를 구분하는가?
2. 실행 가능한 command가 실제 entrypoint와 같은가?
3. field, unit, endian, shape가 명확한가?
4. multi-device 값의 clock/ID 범위가 명확한가?
5. 실패 조건과 미지원 범위가 기록되어 있는가?
6. 같은 사실이 여러 문서에서 서로 다른 값으로 복제되어 있지 않은가?

## 8. Canonical source

중복을 줄이기 위해 상세 정의는 한 문서에만 둔다.

- 전체 구조: architecture
- frame/record field: serial frame schema
- 실행 순서: quickstart
- firmware 상수: firmware document
- 공식 전처리 설계: `model_train/docs/preprocessing/design.md`
- 모델 현재값: `model_train/docs/model-training/`의 모델별 문서

다른 문서는 상세값을 복사하기보다 canonical 문서에 링크한다.
