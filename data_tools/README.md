# JSONL -> MAT 변환기

`data_tools/jsonl_to_mat.py`는 `mac_collector_output/raw`의 JSONL 데이터를 기존 학습 코드(`train.py`, `test.py`)가 읽는 `.mat` 구조로 변환합니다.

## 생성되는 출력

- `data/train_data.mat`
  - `train_data_amp`
  - `train_label_instance`
  - `train_label_mask`
  - `train_label_time`
- `data/test_data.mat`
  - `test_data_amp`
  - `test_label_instance`
  - `test_label_mask`
  - `test_label_time`

현재 `train.py`는 `*_label_instance`를, `test.py`는 `test_label_mask` 또는 `test_label_instance`를 사용할 수 있습니다.

## 입력 준비

1. 수집 raw 데이터가 존재해야 함
   - `mac_collector_output/raw/YYYYMMDD/session_<id>/device_<id>.jsonl`
2. 라벨 구간 CSV(권장)
   - 방법 1: 직접 작성 (`data_tools/labels_template.csv`)
   - 방법 2(권장): 마커 기반 반자동 생성
     - `data_tools/markers_template.csv` 작성
     - `data_tools/markers_to_labels.py` 실행해 `labels.csv` 생성
3. 세션-피험자 매핑 CSV(사람 기준 분할 권장)
   - 템플릿: `data_tools/session_subject_template.csv`

## 실행 예시

### 0) (권장) markers -> labels 생성

```bash
python "data_tools/markers_to_labels.py" \
  --markers-csv "data_tools/markers.csv" \
  --raw-root "mac_collector_output/raw" \
  --output-csv "data_tools/labels.csv"
```

### A) 사람 기준 분할 (권장)

```bash
python "data_tools/jsonl_to_mat.py" \
  --raw-root "mac_collector_output/raw" \
  --output-dir "data" \
  --window-size 192 \
  --stride 96 \
  --labels-csv "data_tools/labels.csv" \
  --session-subject-csv "data_tools/session_subject.csv" \
  --test-subject-ids "S03"
```

### B) subject 정보 없이 임시 랜덤 분할

```bash
python "data_tools/jsonl_to_mat.py" \
  --raw-root "mac_collector_output/raw" \
  --output-dir "data" \
  --labels-csv "data_tools/labels.csv" \
  --test-ratio 0.2 \
  --seed 42
```

## 라벨 CSV 포맷

`labels.csv` 컬럼:

- `session_id`
- `device_id`
- `start_us`
- `end_us`
- `label`

설명:
- `start_us~end_us` 타임스탬프 구간에 `label`을 부여
- 이진 학습이면 `label`은 0/1 사용
- 라벨 구간 밖은 0으로 처리

## 마커 CSV 포맷 (반자동 생성용)

`markers.csv` 컬럼:

- `session_id`
- `start_us`
- `end_us`
- `label`
- `note` (선택)

설명:
- 마커는 세션 단위로 한 번만 기록
- 변환기(`markers_to_labels.py`)가 해당 세션의 모든 `device_id`에 라벨을 자동 확장

## session-subject CSV 포맷

`session_subject.csv` 컬럼:

- `session_id`
- `subject_id`

설명:
- `--test-subject-ids`로 지정한 subject는 test, 나머지는 train

## 주의사항

- JSONL의 `csi_amp` 길이가 파일 내에서 일관돼야 합니다.
- window 크기(`--window-size`)는 기존 학습 코드 기준 192를 권장합니다.
- 라벨 CSV를 주지 않으면 모든 라벨이 0으로 저장됩니다.
