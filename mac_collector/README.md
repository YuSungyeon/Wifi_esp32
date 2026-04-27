# Mac Collector MVP

ESP32-S3에서 보낸 CSI UDP 패킷을 받아 JSONL로 저장하는 최소 수집기입니다.

## 포함 파일

- `UDP_패킷_스키마.md`: ESP <-> Mac 바이너리 패킷 규격
- `udp_collector_mvp.py`: 수집기 실행 스크립트
- `device_registry_template.csv`: 장치 등록표 템플릿
- `session_meta_template.yaml`: 세션 메타 템플릿

## 실행 방법

프로젝트 루트에서:

```bash
python "mac_collector/udp_collector_mvp.py" --host 0.0.0.0 --port 9999 --output-dir "mac_collector_output"
```

RX N대 운영 시(권장):

```bash
python "mac_collector/udp_collector_mvp.py" \
  --host 0.0.0.0 \
  --port 9999 \
  --output-dir "mac_collector_output" \
  --expected-device-ids "101,102,103,104" \
  --stale-sec 10
```

등록표 자동 로드 방식(권장):

```bash
python "mac_collector/udp_collector_mvp.py" \
  --host 0.0.0.0 \
  --port 9999 \
  --output-dir "mac_collector_output" \
  --device-registry-csv "mac_collector/device_registry.csv" \
  --session-meta "mac_collector/session_meta.yaml"
```

설명:
- `--expected-device-ids`를 비우면 `--device-registry-csv`에서 `device_id`를 자동 로드합니다.
- `--session-meta`를 지정하면 세션 디렉터리에 `session_meta_snapshot.yaml`이 복사되어 실험 조건이 함께 보존됩니다.

## 저장 구조

수집 데이터는 아래 구조로 생성됩니다.

```text
mac_collector_output/
  raw/
    YYYYMMDD/
      session_<session_id>/
        device_<device_id>.jsonl
```

각 줄(JSONL 1레코드)은 아래 핵심 필드를 포함합니다.

- `received_at_unix_us`
- `session_id`, `device_id`, `seq`, `timestamp_us`
- `channel`, `rssi_dbm`, `noise_floor_dbm`
- `sample_count`, `csi_amp`

## 현재 MVP에서 제공하는 기능

- 패킷 기본 검증(`magic/version/header_len/payload_type/len`)
- `device_id` 기준 시퀀스 누락량 계산
- 주기적 상태 출력(패킷 수, 유실 추정률, 샘플 수)
- 기대 RX 목록 기반 상태 확인:
  - 미수신 장치(`missing_devices`)
  - 최근 수신 없음(`stale_devices`)

## 장치 등록표 운영 (N대 확장 필수)

1. `device_registry_template.csv`를 복사해 `device_registry.csv`를 생성
2. 각 RX 보드의 `device_id`, `sta_mac`, 물리 좌표를 기록
3. 수집 실행 시 `--expected-device-ids`를 등록표와 동일하게 맞춤
4. 보드 교체/위치 이동/펌웨어 변경 시 등록표를 즉시 업데이트

권장 규칙:
- `device_id`는 실험 기간 동안 고정(재사용 금지)
- 좌표계는 방 좌하단 원점(0,0), 단위 meter로 통일
- 안테나 높이/방향도 함께 기록
- 수집기 실행 시에는 등록표 파일을 기본 입력 소스로 사용

## 세션 메타 운영

- 세션 시작 전에 `session_meta_template.yaml`을 복사해 실제 세션 메타 파일 작성
- 일반적으로 파일명은 `session_meta.yaml`로 두고 `--session-meta`로 전달
- 최소 기록 항목:
  - 네트워크/채널/수집 포트
  - 기대 RX 목록
  - 라벨 타깃/전처리 조건
  - 운영자 메모(환경 변화, 장애, 중단 이력)

## 다음 단계 권장

1. CRC32 검증 활성화
2. JSONL -> Parquet 변환 파이프라인 추가
3. `csi_amp`를 기존 학습 `.mat` 구조로 변환하는 스크립트 추가
4. 장치 등록표(`device_id` <-> 물리 보드 MAC <-> 설치 위치) 운영
5. 세션 시작 시 `session_meta.yaml` 유효성 검사(필수 키 누락 체크)
