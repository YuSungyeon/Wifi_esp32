# 세션·라벨·데이터 무결성

> 세션 레이아웃과 `session.json` 은 [data-schema.md](../data-schema.md), 수집 설계는 [collection-protocol.md](../collection-protocol.md).

## 2026-08-25 — 같은 `session_id` 재수집이 JSONL 에 append 되어 여러 run 이 섞임

- **증상**: `20260615/session_21/device_101.jsonl` 100번째 줄에서 `seq 158→0` 되감김.
  `20260523/session_15` 는 `sample_count` 192 와 128 이 한 파일에. "깨끗한 100Hz 세션"으로
  보이던 것들이 실은 두 run 이 붙은 파일이었다. `measure_csi_hz` 의 `seq_drop=-106` 같은
  의미 불명 값의 정체.
- **원인**: reader 가 `open("a")`, 경로가 `session_<id>` 뿐. `session_meta.yaml` 의
  `session_id` 갱신을 잊으면 조용히 덧붙는다.
- **해결**: 경로 `raw/<날짜>/<시각>_<label>_s<id>/` (시각으로 충돌 불가) + `.csi` 배타적
  생성(`open("xb")`, 충돌 시 exit 4). 같은 라벨 1초 간격 재수집 → 별도 디렉터리 확인.
- **기존 데이터**: 라벨이 없고 run 이 섞여 있어 학습에 못 쓴다. 라벨 정보가 애초에 없어
  마이그레이션 불가 — 재수집이 전제. legacy 로더는 진단 용도로만.
- **재발 방지**: `CLAUDE.md` — 세션 파일을 append 모드로 열지 말 것.

## 2026-08-25 — 라벨이 어디에도 기록되지 않음

- **증상**: `Preprocessing.py --label`(기본 `empty`)이 유일한 입력. 수집 시점에 라벨 개념이 없다.
- **해결**: CLI/GUI 가 수집 시작 시 라벨을 묻고 `session.json` 에 박는다. 후처리는 이것을 읽는다.
- **부수**: 당시 `session_meta.yaml` 의 `label_target` 이 `"mask"` 로 3-class 와 무관한 값이었다.
  유효 라벨이 아니면 기본값 없이 묻도록 함.

## 2026-08-25 — `session_id` 를 사람이 매번 YAML 에서 올려야 함

- **해결**: `csi_session.next_session_id()` — 기존 세션 `_s<N>`(구 `session_<N>` 포함) 최댓값+1.
  YAML 에서 제거. 세션 메타는 브라우저 폼(`session_form.py`)으로 편집.
- **막힌 지점**: 폼 첫 구현이 POST 를 그대로 파일로 써서, 필드 하나 빠진 요청이 나머지를
  전부 날렸다. 기존 값 위에 **병합**하도록 수정.

## 2026-09-02 — 라벨 어휘 불일치 (`action` vs `motion`)

- **증상**: 모델 레포 병합 시 저쪽 `LABEL_MAP={"empty","static","motion"}`, 우리 `LABELS` 는 `action`.
- **해결**: 저쪽이 공식 설계 문서에 세션 배정까지 확정했으므로 `csi_store.LABELS` 를 `motion` 으로.
  수집 데이터가 없어 마이그레이션 비용 없음.
- **재발 방지**: `csi_store.LABELS` 와 `preprocess_3rx.LABEL_MAP` 이 같아야 한다는 주석.
  producer=수집, consumer=전처리.

## 2026-09-02 — 전처리 라벨이 `LABEL_SESSION_RANGES` 로 하드코딩되어 오분류

- **증상**: 합성 세션 10/11/12 를 넣었더니 하드코딩 범위(`empty`=1~10, `static`=11~20)에 걸려
  session 12(`motion`)가 `static` 으로 분류 — `{static: 1814, motion: 0}`.
- **해결**: `export_jsonl.py` 가 날짜 폴더에 `labels.json`(session_id → label, `session.json`
  에서 그대로)을 만들고 전처리가 이를 정본으로 읽는다. 없으면 기존 범위로 폴백(구 데이터).
  적용 후 `{static: 907, motion: 907}`.

## 2026-09-02 — 출처(provenance) 미기록

- **문제**: `session.json` 의 펌웨어 식별이 고정 문자열 `"meshsense"`. 어느 코드로 찍었는지
  사후 재구성 불가. `device_registry.csv` 좌표가 전부 0 이라 배치도 복원 불가.
- **해결**: `session.json` 에 `provenance`(git commit/branch/dirty). 제어판이 수집 전 경고:
  커밋 안 된 변경 · 좌표 0.
