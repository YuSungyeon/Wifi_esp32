# `manifest.json` Field Reference

> 상태: **CURRENT — `preprocess_3rx.py`와 20260616 실제 산출물 기준**
>
> 생성 코드: [`preprocess_3rx.py`](../../preprocessing/preprocess_3rx.py)
>
> 전처리 기준: [3-RX CSI Preprocessing Design](design.md)

## 1. 파일 이름과 역할

실제 파일 이름은 `manifest.jsonl`이 아니라 **`manifest.json`**이다.

```text
model_train/preprocessing/output/20260616/manifest.json
```

두 형식의 차이는 다음과 같다.

| 파일 | 형식 |
|---|---|
| `manifest.json` | 전처리 전체 결과를 담은 JSON 객체 하나 |
| `windows.jsonl` | 한 줄에 window metadata 객체 하나 |

`manifest.json`은 다음 질문에 답하는 전처리 결과표다.

- 어떤 원본 디렉터리와 설정으로 전처리했는가?
- 각 session을 어느 class와 split에 배정했는가?
- 어떤 session이 실제 학습 데이터로 사용되거나 제외됐는가?
- RX별 파싱 오류, 손상 record, 재부팅 segment 상태는 어땠는가?
- 세 RX의 공통 `tx_seq` 범위와 실제 관측률은 얼마인가?
- 각 split에 최종 window가 몇 개 만들어졌는가?
- Normalization 평균과 표준편차를 어떤 train 데이터에서 계산했는가?

Manifest 자체는 LSTM의 CSI feature로 입력하지 않는다. 데이터 로더의 검증,
normalization, 성능 집계, 문제 추적과 실험 재현에 사용한다.

## 2. 다른 산출물과의 관계

```text
manifest.json
  ├─ 전처리 설정, class map, split, 품질 결과
  ├─ train/validation/test의 window 수
  └─ train normalization 통계

<split>/X.npy
  └─ 실제 CSI 입력, shape (N_split, 300, 192)

<split>/y.npy
  └─ X의 각 window에 대응하는 class 번호, shape (N_split,)

<split>/windows.jsonl
  └─ X의 각 window에 대응하는 session과 시작 tx_seq

normalization.npz
  └─ 학습 코드가 실제로 읽을 mean, std, std_safe 배열
```

정상 산출물에서는 다음 수가 일치해야 한다.

```text
manifest.split_summary[split].window_count
  = len(<split>/X.npy)
  = len(<split>/y.npy)
  = <split>/windows.jsonl의 record 수
```

## 3. 최상위 구조

Manifest의 전체 구조를 줄이면 다음과 같다.

```json
{
  "generated_by": "...",
  "design_doc": "...",
  "raw_dir": "...",
  "dry_run": false,
  "config": { "...": "..." },
  "label_map": { "...": "..." },
  "rx_order": [101, 102, 103],
  "splits": { "...": "..." },
  "unassigned_sessions": [],
  "split_summary": { "...": "..." },
  "normalization": { "...": "..." },
  "note_normalization": "...",
  "sessions": [
    { "session_id": 1, "...": "..." }
  ]
}
```

## 4. 실행 정보 필드

| 필드 | 타입 | 의미 |
|---|---|---|
| `generated_by` | string | manifest를 생성한 코드 경로 |
| `design_doc` | string | 생성 코드가 따라야 하는 공식 전처리 설계 문서 경로 |
| `raw_dir` | string | 입력으로 읽은 `session_*` 디렉터리들의 상위 경로 |
| `dry_run` | boolean | 실제 `X.npy`, `y.npy`, normalization을 저장하지 않는 점검 실행 여부 |

현재 생성기가 새 manifest에 기록하는 값은 다음과 같다.

```json
{
  "generated_by": "model_train/preprocessing/preprocess_3rx.py",
  "design_doc": "model_train/docs/preprocessing/design.md",
  "raw_dir": "mac_collector_output/raw/20260616",
  "dry_run": false
}
```

`20260616/manifest.json`은 문서 이동 전에 생성되어 `design_doc`에 기존 경로
`model_train/docs/[전처리]-설계.md`를 보존한다. 이 파일은 완료된 LSTM run의
`dataset_manifest_sha256` 대상이므로 경로 문자열만 고쳐 다시 저장하지 않는다.
기존 값이 가리키는 현재 문서는
[3-RX CSI Preprocessing Design](design.md)이다. 새로 생성한 manifest부터 위의
영문 경로를 기록한다.

`dry_run=true`이면 세션 품질 검사와 window 수 계산은 수행하지만 split별 배열과
normalization 파일은 만들지 않는다. 이때 최상위 `normalization`은 `null`이다.

## 5. `config`: 전처리 설정

`config`는 해당 결과를 만들 때 사용한 임계값과 shape 계약이다. 코드의 현재
기본값만 보는 대신 manifest의 이 값을 확인해야 해당 데이터셋을 정확히 재현할 수
있다.

| 필드 | 타입 | 현재 값 | 의미 |
|---|---|---:|---|
| `config.rx_order` | integer array | `[101,102,103]` | RX 처리 및 feature 결합 순서 |
| `config.features_per_rx` | integer | `64` | RX 하나에서 사용하는 `csi_amp` 개수 |
| `config.window` | integer | `300` | window 하나의 frame 수, 100Hz에서 3초 |
| `config.stride` | integer | `30` | 다음 window 시작점까지 이동하는 frame 수, 0.3초 |
| `config.min_common_length` | integer | `27000` | 세 RX 공통 범위가 통과해야 하는 최소 frame 수 |
| `config.min_observed_ratio` | number | `0.85` | 공통 범위에서 RX별 실제 관측률 최소값 |
| `config.max_interp_gap` | integer | `5` | 선형보간을 허용하는 최대 내부 누락 길이 |
| `config.reboot_small_seq` | integer | `10` | `seq` 감소 뒤 이 값 이하이면 RX 재부팅 후보 |
| `config.reboot_min_drop` | integer | `100` | `seq`가 이 값 이상 감소하면 RX 재부팅 후보 |
| `config.zero_std_epsilon` | number | `1e-6` | 표준편차를 0에 가깝다고 판단하는 기준 |

`config.rx_order`와 최상위 `rx_order`는 현재 같은 값을 가진다. `config.rx_order`는
전체 실행 설정의 일부이고, 최상위 `rx_order`는 소비 코드가 feature 순서를 쉽게
검사하도록 한 번 더 제공한 값이다.

## 6. Class와 RX 순서

### 6.1 `label_map`

`label_map`은 class 이름을 `y.npy`의 정수 번호에 연결한다.

```json
{
  "empty": 0,
  "static": 1,
  "motion": 2
}
```

| 필드 | 타입 | 의미 |
|---|---|---|
| `label_map.empty` | integer | 사람이 없는 class 번호 |
| `label_map.static` | integer | 사람이 정지한 class 번호 |
| `label_map.motion` | integer | 사람이 움직이는 class 번호 |

### 6.2 `rx_order`

`rx_order`는 한 시점의 192개 feature를 만든 RX 순서다.

```text
rx_order = [101, 102, 103]

feature 0~63    → RX101
feature 64~127  → RX102
feature 128~191 → RX103
```

순서를 바꾸면 같은 feature index의 물리적 의미가 달라지므로 학습과 추론에서
manifest의 순서를 그대로 사용해야 한다.

## 7. Split 배정 필드

### 7.1 `splits`

`splits`는 **배정표**다. 품질 gate 통과 여부와 관계없이 어떤 session을 어느
split 후보로 지정했는지 기록한다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `splits.train` | integer array | train으로 배정한 session ID 목록 |
| `splits.validation` | integer array | validation으로 배정한 session ID 목록 |
| `splits.test` | integer array | test로 배정한 session ID 목록 |

20260616 배정은 다음과 같다.

```text
train      = 1~6, 11~16, 21, 23~26
validation = 7, 8, 17, 18, 27, 28
test       = 9, 10, 19, 20, 29, 30
```

### 7.2 `unassigned_sessions`

입력 `raw_dir`에서 발견했지만 `splits`의 어느 목록에도 없는 session ID다.
20260616에서는 품질 문제로 미리 배정하지 않은 session 22가 들어 있다.

```json
"unassigned_sessions": [22]
```

Split 미배정 session이 품질 gate를 통과하면 코드는 데이터를 조용히 버리지 않고
오류로 중단한다. 따라서 정상적으로 생성된 manifest의 미배정 session은 품질
gate에서 제외된 session이어야 한다.

## 8. `split_summary`: split별 최종 결과

`split_summary`는 `train`, `validation`, `test`에 같은 하위 구조를 반복한다.

```text
split_summary.train.<field>
split_summary.validation.<field>
split_summary.test.<field>
```

| 하위 필드 | 타입 | 의미 |
|---|---|---|
| `sessions` | integer array | 배정표에 있고 실제 입력 디렉터리에서도 발견된 session |
| `used_sessions` | integer array | 발견된 session 중 품질 gate를 통과해 window 생성에 사용된 session |
| `missing_sessions` | integer array | 배정표에는 있지만 입력 디렉터리가 발견되지 않은 session |
| `window_count` | integer | 해당 split에 최종 저장한 전체 window 수 |
| `class_window_counts` | object | class별 최종 window 수 |
| `class_window_counts.empty` | integer | 해당 split의 empty window 수 |
| `class_window_counts.static` | integer | 해당 split의 static window 수 |
| `class_window_counts.motion` | integer | 해당 split의 motion window 수 |

`sessions`와 `used_sessions`는 의미가 다르다. 예를 들어 split에 배정되고 입력도
존재하지만 품질 gate에서 제외된 session은 `sessions`에는 있고
`used_sessions`에는 없다.

현재 요약값은 다음과 같다.

| split | 사용 session | 전체 window | empty | static | motion |
|---|---:|---:|---:|---:|---:|
| train | 17 | 16,728 | 5,857 | 5,922 | 4,949 |
| validation | 6 | 5,933 | 1,971 | 1,980 | 1,982 |
| test | 6 | 5,924 | 1,961 | 1,982 | 1,981 |

## 9. `normalization`: train 통계

`normalization`은 `dry_run=false`일 때 train window에서 계산한 feature별 통계를
설명한다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `normalization.file` | string | 실제 배열을 저장한 NPZ 파일 이름 |
| `normalization.computed_from` | string | 통계 계산에 사용한 split, 반드시 `train` |
| `normalization.train_frame_count` | integer | 통계에 포함한 train window의 전체 시점 수 |
| `normalization.zero_std_epsilon` | number | 표준편차가 0에 가깝다고 판단한 기준 |
| `normalization.zero_std_replacement` | number | 작은 표준편차 대신 `std_safe`에 넣는 값 |
| `normalization.zero_std_feature_count` | integer | 기준보다 표준편차가 작은 feature 수 |
| `normalization.mean` | number array | 192개 feature 각각의 train 평균 |
| `normalization.std` | number array | 192개 feature 각각의 원래 train 표준편차 |

### 9.1 `train_frame_count`

현재 값은 다음 계산과 같다.

```text
16,728 train window × 300 시점 = 5,018,400
```

Window가 서로 겹치므로 같은 원본 frame이 여러 window에 들어가면 이 통계에도
여러 번 반영된다. 이는 실제 모델에 제공되는 train window 분포를 기준으로 계산한
결과다.

### 9.2 `mean`과 `std`

두 배열의 길이는 모두 192다.

```text
mean[0:64],   std[0:64]   → RX101 feature 통계
mean[64:128], std[64:128] → RX102 feature 통계
mean[128:192],std[128:192]→ RX103 feature 통계
```

Feature `k`의 값은 모든 train window와 모든 300개 시점에서 계산한다.

```python
mean[k] = X_train[:, :, k].mean()
std[k] = X_train[:, :, k].std()
```

특정 class 하나의 통계가 아니라 train의 `empty`, `static`, `motion`을 모두 합친
공통 통계다.

### 9.3 `std_safe`와의 차이

Manifest에는 원래 `std`만 배열로 기록한다. 학습 때 분모로 사용하는 `std_safe`는
`normalization.npz` 안에 저장된다.

```text
std < zero_std_epsilon → std_safe = zero_std_replacement
그 외                 → std_safe = std
```

현재 값은 다음과 같다.

```text
zero_std_epsilon       = 0.000001
zero_std_replacement   = 1.0
zero_std_feature_count = 36
```

따라서 학습 코드는 manifest의 `std` 배열을 바로 분모로 사용하지 않고 NPZ의
`std_safe`를 읽어야 한다.

### 9.4 `note_normalization`

최상위 `note_normalization`은 저장된 `X.npy`가 정규화 전 raw amplitude라는 점을
사람과 소비 코드에 알리는 설명 문자열이다.

```text
저장된 X는 raw amplitude다.
학습 시 train 통계(mean, std_safe)로 정규화해 사용한다.
```

## 10. `sessions`: 세션별 상세 결과

`sessions`는 입력에서 발견한 session을 ID 오름차순으로 담은 배열이다. 각 원소는
session 하나의 품질 검사와 window 생성 결과다.

### 10.1 세션 식별과 사용 여부

| 필드 | 타입 | 의미 |
|---|---|---|
| `session_id` | integer | 원본 `session_<id>`의 숫자 ID |
| `label` | string | `empty`, `static`, `motion` 중 class 이름 |
| `label_id` | integer | `label_map`에 따른 정수 class 번호 |
| `split` | string 또는 null | 배정된 `train`, `validation`, `test`; 미배정이면 `null` |
| `used` | boolean | 품질 gate를 통과해 최종 window 생성에 사용됐는지 여부 |
| `exclusion_reasons` | string array | `used=false`가 된 이유 목록; 사용 세션은 빈 배열 |

`split`과 `used`는 다른 개념이다.

```text
split="train", used=true   → train에 실제 사용
split="train", used=false  → train 배정은 됐지만 품질 문제로 제외
split=null, used=false      → 어느 split에도 배정하지 않은 제외 session
```

현재 session 22는 다음 상태다.

```json
{
  "session_id": 22,
  "label": "motion",
  "label_id": 2,
  "split": null,
  "used": false,
  "exclusion_reasons": ["공통 길이 3 < 27000"]
}
```

### 10.2 `rx`: RX별 파싱과 순번 검사

`rx`는 JSON object이므로 RX ID가 문자열 key로 저장된다.

```text
sessions[i].rx["101"]
sessions[i].rx["102"]
sessions[i].rx["103"]
```

각 RX object의 필드는 다음과 같다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `file` | string | 읽으려 한 RX JSONL 파일 이름 |
| `exists` | boolean | 해당 파일이 존재했는지 여부 |
| `record_count` | integer | 필수 검사를 통과해 파싱된 record 수 |
| `parse_error_count` | integer | 파싱 또는 필수 필드 검사에서 제외된 줄의 전체 수 |
| `parse_errors` | object array | 파싱 오류 상세 중 앞쪽 최대 50개 |
| `removed_single_corrupt_count` | integer | 3-record 규칙으로 제거한 단일 손상 record 전체 수 |
| `removed_single_corrupt` | object array | 제거한 단일 손상 상세 중 앞쪽 최대 50개 |
| `ambiguous_corrupt_count` | integer | 연속 손상 후보라 자동 제거하지 않은 record 전체 수 |
| `ambiguous_corrupt` | object array | 모호한 손상 후보 상세 중 앞쪽 최대 50개 |
| `reboot_boundary_count` | integer | `seq`로 찾은 RX 재부팅 경계 수 |
| `dropped_boundary_lines` | integer array | 일반 재부팅 경계에서 제외한 원본 줄 번호, 최대 50개 |
| `segment_count` | integer | 재부팅 경계로 나눈 RX segment 수 |
| `residual_tx_seq_decrease_count` | integer | 단일 손상 제거와 segment 분리 뒤에도 남은 `tx_seq` 감소 지점 수 |

`record_count`는 원본 파일 줄 수가 아니다. 정상적으로 파싱된 record만 센다.
따라서 오류 줄이 있다면 대체로 `record_count + parse_error_count`가 검사한 줄 수에
대응한다.

RX 파일이 없으면 해당 object에는 다음 두 필드만 존재하고 나머지 필드는 생략된다.

```json
{
  "file": "device_102.jsonl",
  "exists": false
}
```

#### `parse_errors[]` 원소

| 필드 | 타입 | 의미 |
|---|---|---|
| `line` | integer | RX JSONL의 1부터 시작하는 원본 줄 번호 |
| `reason` | string | 빈 줄, JSON 파싱 실패, 필수 field 누락, 장치 불일치 등 오류 이유 |

파서는 다음 문제를 오류로 기록하고 해당 줄을 CSI record로 사용하지 않는다.

- 빈 줄
- 올바르지 않은 JSON
- `device_id`, `seq`, `timestamp_us`, `tx_seq`, `csi_amp` 중 필수 field 누락
- 파일의 RX ID와 record의 `device_id` 불일치
- `seq` 또는 `tx_seq`가 정수가 아님
- `csi_amp`가 배열이 아니거나 길이가 64가 아님
- `csi_amp`에 숫자로 변환할 수 없는 값이 있음

#### `removed_single_corrupt[]` 원소

| 필드 | 타입 | 의미 |
|---|---|---|
| `line` | integer | 제거한 record의 원본 줄 번호 |
| `seq` | integer | 제거한 record의 RX 순번 |
| `tx_seq` | integer | 제거한 record의 TX 순번 |
| `reason` | string | 같은 boot 또는 재부팅 경계에서 판정된 손상 이유 |

#### `ambiguous_corrupt[]` 원소

필드 구조는 `removed_single_corrupt[]`와 동일하다. 연속된 record들이 동시에 손상
후보이면 가운데 하나만 안전하게 제거할 수 없으므로 `reason`에 자동 제거하지
않았다는 내용을 기록한다.

#### `dropped_boundary_lines`

일반적인 RX 재부팅 경계를 나타낸 record는 제외하고 다음 record부터 새 segment를
시작한다. 이때 제외한 경계 record의 원본 줄 번호가 들어간다. 단일 손상 record를
제거하면서 다음 record를 새 boot 시작으로 확정한 경우에는 그 다음 record를
버리지 않으므로 `reboot_boundary_count`는 증가해도 해당 줄이 이 배열에는 없을 수
있다.

#### 상세 목록의 50개 제한

Manifest 크기가 비정상적으로 커지는 것을 막기 위해 상세 배열은 각 항목의 앞쪽
50개만 저장한다. 실제 전체 개수는 대응하는 `*_count` 필드를 기준으로 봐야 한다.

```text
parse_error_count                  ↔ parse_errors
removed_single_corrupt_count       ↔ removed_single_corrupt
ambiguous_corrupt_count            ↔ ambiguous_corrupt
reboot_boundary_count의 일부 상세 ↔ dropped_boundary_lines
```

### 10.3 선택 segment와 세 RX 공통 범위

| 필드 | 타입 | 의미 |
|---|---|---|
| `chosen_segments` | object 또는 null | RX별로 선택한 안정 segment의 0부터 시작하는 index |
| `common_start` | integer 또는 null | 선택된 세 segment가 모두 겹치는 첫 `tx_seq` |
| `common_end` | integer 또는 null | 선택된 세 segment가 모두 겹치는 마지막 `tx_seq` |
| `common_length` | integer 또는 null | 양 끝을 포함한 공통 정수 grid 길이 |
| `observed_ratio` | object 또는 null | 공통 범위 안에서 RX별 실제 고유 `tx_seq` 관측 비율 |

공통 범위는 다음과 같이 계산한다.

```text
common_start  = max(RX101 시작, RX102 시작, RX103 시작)
common_end    = min(RX101 끝,   RX102 끝,   RX103 끝)
common_length = common_end - common_start + 1
```

`chosen_segments`의 예시는 다음과 같다.

```json
"chosen_segments": {
  "101": 1,
  "102": 1,
  "103": 1
}
```

이는 세 RX 모두 분리된 segment 중 index 1, 즉 두 번째 segment를 선택했다는
뜻이다. Segment index는 JSONL 줄 번호나 `seq`, `tx_seq` 값이 아니다.

`observed_ratio`는 각 RX에 대해 다음과 같이 계산한다.

```text
공통 범위 안에서 실제 수신한 고유 tx_seq 수 / common_length
```

보간값은 실제 수신 record가 아니므로 분자에 포함하지 않는다. 또한 공통 길이가
아주 짧으면 session 22처럼 관측률이 1.0이어도 세션 전체는 길이 기준으로 제외될
수 있다.

안정 segment 조합을 만들 수 없으면 `chosen_segments`, `common_start`,
`common_end`, `common_length`, `observed_ratio`가 `null`이 될 수 있다.

### 10.4 Grid, 보간과 window 결과

| 필드 | 타입 | 의미 |
|---|---|---|
| `duplicate_count` | object 또는 null | 선택 segment의 공통 범위에서 RX별 중복 `tx_seq` record 수 |
| `interpolated_frame_count` | object 또는 null | RX별로 짧은 gap 보간으로 채운 grid 위치 수 |
| `window_candidate_count` | integer | stride를 적용해 검사한 window 시작 위치 수 |
| `windows_excluded_by_gap` | integer | 보간되지 않은 누락을 포함해 제외한 후보 window 수 |
| `window_count` | integer | 최종 사용 가능한 window 수 |

`duplicate_count`는 공통 범위 안의 중복만 센다. 같은 RX에서 같은 `tx_seq`가 여러
번 나오면 파일에서 먼저 나온 record를 사용하고 나머지를 중복으로 센다.

`interpolated_frame_count`는 amplitude 숫자 개수가 아니라 **frame 위치 개수**다.
한 위치를 보간하면 그 RX의 64개 amplitude가 함께 채워져도 count는 1 증가한다.
실제로 수신한 record 수에는 포함하지 않는다.

Window 수의 관계는 다음과 같다.

```text
window_count
  = window_candidate_count - windows_excluded_by_gap
```

후보 window 안에 세 RX 중 하나라도 실제 수신되지 않았고 허용된 짧은 보간으로도
채워지지 않은 위치가 있으면 `windows_excluded_by_gap`에 포함한다.

품질 gate에서 먼저 제외된 session은 grid와 window 생성을 수행하지 않는다.
따라서 공통 범위 정보가 있더라도 다음 필드가 `null` 또는 0일 수 있다.

```text
duplicate_count          = null
interpolated_frame_count = null
window_candidate_count   = 0
windows_excluded_by_gap  = 0
window_count             = 0
```

### 10.5 `exclusion_reasons`

`used`는 `exclusion_reasons`가 비어 있을 때만 `true`다. 코드가 현재 기록할 수 있는
주요 제외 이유는 다음과 같다.

- 필요한 RX 파일 없음
- 단일 손상 제거 후에도 `tx_seq` 감소가 지속됨
- 세 RX의 정상 안정 segment 조합이 없음
- 공통 길이가 `min_common_length`보다 짧음
- RX 하나 이상의 관측률이 `min_observed_ratio`보다 낮음

한 session에 문제가 여러 개 있으면 이유 문자열도 여러 개 들어갈 수 있다.
파싱 오류가 하나 있다는 사실만으로 즉시 session 전체를 제외하지는 않는다.
오류 record를 빼고 남은 데이터가 공통 길이와 관측률 등 모든 품질 기준을
통과하는지를 최종 판단한다.

## 11. 실제 세션 예시

### 11.1 사용된 session 1

핵심 필드를 줄여 보면 다음과 같다.

```json
{
  "session_id": 1,
  "label": "empty",
  "label_id": 0,
  "split": "train",
  "chosen_segments": {"101": 1, "102": 1, "103": 1},
  "common_start": 113532,
  "common_end": 143536,
  "common_length": 30005,
  "observed_ratio": {
    "101": 0.9979003499,
    "102": 0.9644392601,
    "103": 0.9973337777
  },
  "window_candidate_count": 991,
  "windows_excluded_by_gap": 0,
  "window_count": 991,
  "used": true,
  "exclusion_reasons": []
}
```

해석하면 세 RX가 공통으로 가진 `tx_seq=113532~143536`의 30,005 frame 범위가
길이와 관측률 기준을 통과했고, 991개 후보 window가 모두 최종 train window로
사용됐다는 뜻이다.

### 11.2 제외된 session 22

```json
{
  "session_id": 22,
  "label": "motion",
  "split": null,
  "common_start": 1364029,
  "common_end": 1364031,
  "common_length": 3,
  "observed_ratio": {"101": 1.0, "102": 1.0, "103": 1.0},
  "window_count": 0,
  "used": false,
  "exclusion_reasons": ["공통 길이 3 < 27000"]
}
```

공통 3개 frame은 세 RX가 모두 받아 관측률은 1.0이지만, 3초 window 300 frame은
물론 최소 공통 길이 27,000 frame에도 미치지 못한다. 관측률만 높다고 사용할 수
있는 session은 아니라는 예다.

## 12. `null`, 빈 배열과 필드 생략 읽기

| 표현 | 의미 |
|---|---|
| `null` | 해당 단계를 수행하지 못했거나 품질 gate 이전에 멈춰 값이 없음 |
| `[]` | 단계는 확인했지만 해당 사건이나 오류가 0개 |
| `{}` | 대상별 결과가 비어 있는 object |
| 필드 자체가 없음 | 현재는 주로 RX 입력 파일이 없어 상세 검사를 수행하지 못한 경우 |

`0`, `null`, 빈 배열은 서로 다른 의미이므로 같은 값처럼 처리하면 안 된다.

## 13. 학습 코드에서 사용하는 방법

학습 시작 시 manifest에서 최소 다음 내용을 검증한다.

```text
label_map == {empty: 0, static: 1, motion: 2}
rx_order == [101, 102, 103]
config.window == 300
config.features_per_rx == 64
split 사이 session 중복 없음
split_summary의 window 수와 X/y/windows.jsonl 길이가 일치
normalization.computed_from == "train"
len(normalization.mean) == 192
len(normalization.std) == 192
```

실제 정규화 계산에는 `normalization.npz`를 사용한다.

```python
import json
import numpy as np

with open(dataset_dir / "manifest.json", encoding="utf-8") as fp:
    manifest = json.load(fp)

stats = np.load(dataset_dir / manifest["normalization"]["file"])
mean = stats["mean"]
std_safe = stats["std_safe"]
```

`manifest.json`은 JSON 객체 하나이므로 한 줄씩 읽는 방식이 아니라 `json.load()`로
전체 구조를 읽는다. Window 단위 반복 처리가 필요하면 각 split의
`windows.jsonl`을 한 줄씩 읽는다.

## 14. 필드 간 핵심 일관성 규칙

```text
session.used == true
  → session.exclusion_reasons == []
  → session.window_count > 0

session.common_length
  = session.common_end - session.common_start + 1

session.window_count
  = session.window_candidate_count - session.windows_excluded_by_gap

split_summary[split].window_count
  = 해당 split에서 used=true인 session.window_count의 합

normalization.train_frame_count
  = split_summary.train.window_count × config.window
```

이 규칙을 자동 테스트에서 확인하면 manifest와 실제 학습 배열이 서로 어긋난 상태를
학습 전에 발견할 수 있다.
