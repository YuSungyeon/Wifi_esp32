# 후처리와 학습 파이프라인

> 상태: visualization은 **CURRENT**, model preprocessing/training은
> **EXPERIMENTAL**

## 1. 입력 계약

입력은 [JSONL record schema v1](data-schema.md)을 따른다.

```text
mac_collector_output/raw/YYYYMMDD/session_<id>/
├── device_<id>.jsonl
└── session_meta_snapshot.yaml
```

CSI 값은 raw signed I/Q에서 계산한 amplitude다. firmware에서 normalization이나 outlier clipping을 하지 않는다.

## 2. CURRENT: 수집률 측정

```bash
python3 scripts/measure_csi_hz.py \
  mac_collector_output/raw/YYYYMMDD/session_<id>
```

RX callback에서 기록한 `timestamp_us`만 사용한다. Mac 수신 시각은 계산에
사용하지 않는다. `--gap-ms`로 큰 RX 수신 간격의 기준을 바꿀 수 있으며
기본값은 200ms다.

`seq`와 `timestamp_us`가 함께 작아지는 재부팅 경계를 만나면 그전 데이터는
버린다. 따라서 각 장치에서 **마지막 재부팅 이후 데이터만 계산**한다. 결과는
장치별 한 행의 표로 출력한다.

- `RESETS`: 파일에서 발견한 재부팅 횟수
- `RECORDS`: 마지막 재부팅 이후 저장된 record 수
- `RX_HZ`: 저장된 record의 RX 기준 수집률
- `RX_SEQ_HZ`: sequence 범위로 빠진 record까지 포함한 수신률 추정값
- `DT_MS`, `GAPS`: 일반 수신 간격과 긴 공백 수
- `SEQ_GAP`, `DUP`, `ANOM`: 누락·중복·순서 이상

`RX_HZ`는 JSONL에 실제 남은 record만 나타낸다. `RX_SEQ_HZ`와 `SEQ_GAP`은
사라진 record를 간접 추정하지만, 누락 CSI를 복구하거나 손실 원인을 구분하지는
않는다.

## 3. CURRENT: waterfall

```bash
.venv/bin/python scripts/visualize_csi.py \
  --session-dir mac_collector_output/raw/YYYYMMDD/session_<id>
```

동작:

1. `device_*.jsonl`을 device별로 로드한다.
2. 각 RX의 `received_at_unix_us`를 정렬한다.
3. RX별로 독립적인 100Hz grid에 선형 보간한다.
4. 긴 session은 표시용 최대 4,000 row로 downsample한다.
5. device별 subplot을 한 `csi_waterfall.png`에 저장한다.

각 RX가 독립 시간축을 사용하므로 이 PNG는 cross-RX 정렬 결과가 아니다.

## 4. CURRENT: RX별 `tx_seq` 범위 시각화

### 4.1 목적과 범위

한 session의 RX 101·102·103이 기록한 `tx_seq` 범위를 한 그림에서 비교하고,
각 RX가 어디서 시작하고 끝나는지와 범위 안의 실제 누락을 확인한다. 이 도구는
세 RX의 공통 범위를 계산하거나 선택하지 않는다.

실행 파일:

```text
scripts/visualize_tx_seq_overlap.py
```

이 도구는 학습 Tensor를 만들거나 누락값을 보간하지 않는다. `csi_amp`도
시각화하지 않는다. 범위·실제 수신 여부·누락을 보여 주고 결과를 PNG와 터미널
요약으로 출력하는 역할만 담당한다.

### 4.2 실행 인터페이스

```bash
.venv/bin/python scripts/visualize_tx_seq_overlap.py \
  --session-dir mac_collector_output/raw/20260616/session_1 \
  --rx-ids 101 102 103 \
  --out-name tx_seq_overlap.png
```

| option | 기본값 | 의미 |
|---|---|---|
| `--session-dir` | 필수 | 한 session 디렉터리 |
| `--rx-ids` | `101 102 103` | 비교할 RX 순서 |
| `--out-name` | `tx_seq_overlap.png` | session 디렉터리 안에 저장할 PNG 파일명 |
| `--max-columns` | `6000` | 긴 범위를 화면용으로 압축할 최대 열 수 |
| `--max-grid-length` | `2000000` | 비정상적으로 큰 grid의 메모리 사용 방지 상한 |

출력 위치는 항상 입력 session 디렉터리 안이다. 예를 들어 session 1을 입력하면
다음 파일이 생성된다.

```text
mac_collector_output/raw/20260616/session_1/tx_seq_overlap.png
```

`--out-name`에는 디렉터리 경로를 넣을 수 없으며 파일명만 지정할 수 있다.

입력은 RX별 `device_<id>.jsonl`이며 다음 metadata만 사용한다.

```text
device_id
seq
timestamp_us
tx_seq
received_at_unix_us
```

JSON을 읽는 과정에서 `csi_amp`가 함께 파싱되더라도 메모리에 보관하지 않는다.

### 4.3 파일 순서와 경계 진단

각 RX의 JSONL은 파일에 기록된 순서대로 읽으며 모든 record의 `tx_seq`를 RX별로
보존한다. `seq`와 `received_at_unix_us`는 다음 경계 개수를 터미널에 보고하는
진단값으로만 사용한다.

| 경계 | 판정 | 의미 |
|---|---|---|
| RX 재부팅 | 6.4.2의 `seq` 역행 조건 | RX가 새로 시작됨 |
| TX epoch 변경 | 현재 `tx_seq < 이전 tx_seq` | TX counter가 다시 시작됨 |
| 파일 순서 이상 | `received_at_unix_us`가 이전보다 작음 | append·순서 손상 후보 |

경계가 있어도 그래프에서는 record를 제거하거나 공통 segment를 고르지 않는다.
해당 RX JSONL에 실제 존재하는 `tx_seq` 전체를 그린다.

### 4.4 RX별 독립 범위

각 RX의 시작과 끝을 다른 RX와 독립적으로 계산한다.

```text
rx_start = min(해당 RX JSONL의 tx_seq)
rx_end   = max(해당 RX JSONL의 tx_seq)
```

그래프의 전체 가로축만 세 RX를 모두 담도록 정한다.

```text
graph_start = min(rx101_start, rx102_start, rx103_start)
graph_end   = max(rx101_end,   rx102_end,   rx103_end)
```

예를 들어 RX 범위가 `200~400`, `210~410`, `50~600`이면 그래프의 가로축은
`50~600`이다. 각 RX 막대에는 각각 `200~400`, `210~410`, `50~600`을 그대로
표시하며 `210~400` 같은 공통 범위를 별도로 계산하거나 강조하지 않는다.

### 4.5 RX별 존재 mask

전체 가로축에서 RX별로 독립적인 존재 mask를 만든다.

```text
tx_grid = [graph_start, ..., graph_end]
present_mask[rx, tx_seq] = 해당 RX JSONL에 tx_seq가 있으면 true
```

실제 존재하면 RX별 색을 칠하고, 존재하지 않으면 흰색으로 둔다. RX 사이의 AND
연산이나 보간은 하지 않는다.

### 4.6 PNG 구성

PNG 한 장 안에 그래프 축도 하나만 사용하고 RX별 세 행을 그린다.

1. RX 101: 흰 가로 막대에서 `tx_seq`가 실제 존재하는 위치만 진한 파랑
2. RX 102: 흰 가로 막대에서 `tx_seq`가 실제 존재하는 위치만 진한 보라
3. RX 103: 흰 가로 막대에서 `tx_seq`가 실제 존재하는 위치만 진한 청록

수신하지 못한 `tx_seq`는 별도 색을 칠하지 않고 흰색으로 남긴다. 공통 범위나
공통 시작·끝 점선은 그리지 않는다.

가로축 단위는 `tx_seq` 1 frame이며 목표 100Hz 기준 약 10ms다. 세로축은 RX
`device_id`다. 각 RX 막대 안에는 해당 JSONL 전체의 `start=<최소 tx_seq>`와
`end=<최대 tx_seq>`를 직접 표시한다.

5분 session의 약 30,000개 열을 그대로 PNG pixel에 대응시키면 읽기 어렵다.
`--max-columns`보다 길면 연속 `tx_seq`를 화면 열 하나로 묶고, 그 열의 수신률을
표시한다. 묶인 `tx_seq` 중 하나라도 누락되면 해당 화면 열 전체를 완전한 흰색으로
표시해 누락이 흐린 색으로 묻히지 않게 한다. 터미널 요약과 통계는 압축 전 원본
mask로 계산한다.

### 4.7 터미널 요약

RX별로 다음 값을 한눈에 출력한다.

| 값 | 계산 |
|---|---|
| `START`, `END` | 해당 RX의 최소·최대 `tx_seq` |
| `SPAN` | `END - START + 1` |
| `OBSERVED` | 해당 RX의 고유 `tx_seq` 수 |
| `RATIO` | `OBSERVED / SPAN` |
| `MISSING` | `SPAN - OBSERVED` |
| RX별 `MAX_GAP` | 가장 긴 연속 누락 길이 |
| `RX_RESETS`, `TX_RESETS` | 파일 순서에서 발견한 경계 진단값 |

실제 `20260616/session_1` 검증 결과:

```text
RX   START   END     SPAN   OBSERVED  MISSING  RATIO   MAX_GAP
101  109153  143536  34384  29946     4438     0.8709  4375
102  108350  143536  35187  28942     6245     0.8225  5178
103  108874  143536  34663  29929     4734     0.8634  4654
```

화면용 mask를 압축해도 위 통계는 RX별 원본 `tx_seq`로 계산한다.

### 4.8 오류와 경계 처리

- RX 파일이 하나라도 없으면 PNG를 만들지 않고 누락된 device ID를 출력한다.
- 유효 record가 없는 RX가 있으면 PNG를 만들지 않고 해당 device ID를 출력한다.
- `tx_seq` 감소는 `TX_RESETS`로 보고하지만 그래프에는 JSONL의 실제 값 전체를
  표시한다.
- 값을 보간하거나 범위 밖으로 extrapolation하지 않는다.
- 이 도구의 결과만으로 session을 자동 제외하지 않는다. 학습 제외 여부는 전처리
  품질 gate가 결정한다.
- 정상적으로 PNG를 만들면 종료 코드 0, 입력 오류는 2를 반환한다.

### 4.9 검증 범위

다음 경우를 자동 test로 검증한다.

1. 세 RX의 서로 다른 시작·끝 범위를 그대로 유지
2. RX별 누락 위치를 완전한 흰색으로 표시
3. `tx_seq` 감소와 RX 재부팅 경계 진단
4. RX 파일 누락과 빈 파일 처리
5. 출력 파일이 session 디렉터리 밖으로 나가지 못하는지 확인
6. 작은 가상 session과 실제 `20260616/session_1`에서 PNG 생성

## 5. EXPERIMENTAL: 모델별 전처리와 학습

모델 실행 코드는 `model_train/<model-name>/`, 전처리·설계·학습 문서는
`model_train/docs/`에서 관리한다.

현재 LSTM 실험:

- [공식 전처리 설계](../model_train/docs/%5B전처리%5D-설계.md)
- [LSTM 현재 전처리 구현](../model_train/docs/%5B전처리%5D-현재%20구현.md)
- [LSTM 모델 설계와 학습](../model_train/docs/%5B모델%5D-장단기메모리%20설계와%20학습.md)

현재 구현은 단일 session·단일 RX·hardcoded path/label을 사용하는
**EXPERIMENTAL** prototype이다. 공식 preprocessing/학습 pipeline으로 취급하지
않는다.

다른 모델을 추가할 때도 코드와 문서를 분리한다. 전체 목록은
[model_train 문서](../model_train/docs/%5B문서%5D-목록.md)를 참조한다.

## 수집 → 전처리 사이 (`.csi` → JSONL)

> 상태: **CURRENT**

수집은 binary frame v4(`.csi`)로 하고, 전처리는 JSONL record schema v1 을 소비한다.
그 사이는 `scripts/export_jsonl.py` 가 잇는다.

```bash
python scripts/export_jsonl.py --print-labels
python model_train/preprocessing/preprocess_3rx.py \
    --raw-dir mac_collector_output/jsonl/raw/<날짜> --dry-run
```

내보내기는 날짜 폴더에 `labels.json`(session_id → label)을 함께 만들고, 전처리가 이것을
**라벨 정본**으로 읽는다. 없으면 `LABEL_SESSION_RANGES` 로 떨어진다(구 데이터 호환).

이 배관이 없을 때 실제로 사고가 났다: 세션 10/11/12 를 넣었더니 하드코딩 범위
(`empty`=1~10, `static`=11~20)에 걸려 session 12(`motion`)가 `static` 으로 분류됐다
— `{static: 1814, motion: 0}`. `labels.json` 을 두면 `{static: 907, motion: 907}` 로 맞는다.

### 유효 서브캐리어

LLTF 64 SC 중 인덱스 `0`(DC)과 `27~37`(가드)은 **상시 0**이다. 실보드 확인:

```text
상시 0 인덱스: [0, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37]
유효 데이터 톤: [1..26] + [38..63] = 52개   (scripts/csi_store.py 의 LLTF_DATA_IDX)
```

`features_per_rx=64` 로 64개를 모두 쓰면 3-RX 192 feature 중 **36개(19%)가 상수 0**이 된다.

## 클래스 분리 가능성 진단

> 상태: **CURRENT**

본격 수집 전에 "이 배치로 3-class 가 갈리기는 하는가"를 파일럿 몇 세션으로 확인한다.
PyTorch 불필요.

```bash
python scripts/check_separability.py --out separability.png
```

판정은 **쌍별 2-class 세션 단위 LOSO** 정확도로 한다. 세션을 한데 모아 계산한 AUC 는
참고로만 표시한다 — 세 클래스가 통계적으로 완전히 동일한 합성 데이터에서도 AUC 가 1.000 이
나온다 (세션마다 다른 기저 채널이 클래스 차이처럼 보인다). 같은 데이터에서 LOSO 는 0.583 을
낸다. 함께 출력하는 `세션지문` 지표는 "클래스보다 세션을 더 잘 구분하는" 특징을 걸러낸다.

이 도구의 정렬은 공식 전처리(`preprocess_3rx.py`)보다 훨씬 단순하다 — 배치 판단이 공식
전처리의 손상 제거·mask 규칙에 좌우되지 않게 하려는 의도다. 학습 데이터 생성은 공식
전처리가 정본이다.
