# Sequence Analysis for `seq` and `tx_seq`

> 상태: **SUPPORTING ANALYSIS** — 공식 구현 기준은 [Preprocessing Design](design.md)
> 작성일: 2026-08-10
> 근거 데이터: `mac_collector_output/raw/20260616/session_1~30` (record 2,583,125개)
> 근거 문서: [sequence-patterns.md](../../../doc/sequence-patterns.md) · [Preprocessing Design](design.md) · [Legacy LSTM Preprocessing Implementation](legacy-preprocessing.md) · [data-schema.md](../../../doc/data-schema.md)

## 1. 결론 요약

두 순번의 역할을 분리하는 것이 전처리 설계의 출발점이다.

| 순번 | 만드는 장치 | 용도 | 쓰면 안 되는 용도 |
|---|---|---|---|
| `seq` | RX 각각 (부팅마다 0부터) | RX 재부팅 경계 탐지, 저장 경로 품질 검사 | RX 간 비교, cross-RX 정렬 |
| `tx_seq` | TX 하나 (모든 RX 공통) | 세 RX 정렬(join key), 누락 mask, window 위치 | RX 재부팅 판정, 모델 feature |

공식 전처리 설계를 한 문장으로 요약하면 다음과 같다.

> **파일 순서를 유지한 채 단일 손상 record를 먼저 제거하고, `seq`로 RX 재부팅 segment를 나눈 뒤, 세 RX 안정 segment의 `tx_seq` 교집합 위에 정수 grid와 수신 mask를 만들어, 5 frame 이하 누락만 보간하고 긴 누락이 낀 3초 window는 버린다.**

이 방식을 20260616 데이터(30 session)에 적용하면 session 22 하나만 제외되고, 29개 session에서 약 30,000 frame 공통 구간과 총 **28,585개의 (300, 192) window**를 얻을 수 있다 (empty 9,789 / static 9,884 / motion 8,912).

시간 기준으로 Mac 수신 시각(`received_at_unix_us`)은 **판정·정렬 어디에도 쓰지 않는다**. 이 값은 무선 수신 시각이 아니라 USB 버퍼링과 macOS 스케줄링의 영향을 받은 reader 읽기 시각이기 때문이다.

## 2. 왜 `tx_seq`가 정렬 키인가

각 RX의 `seq`와 `timestamp_us`는 보드마다 독립적인 값이라(부팅 시점이 다름) 서로 비교할 수 없다. 반면 `tx_seq`는 TX가 ESP-NOW payload에 실어 보내는 송신 카운터라서, 같은 무선 frame을 받은 RX는 모두 같은 값을 기록한다.

```text
TX frame 1000 ─┬─ RX101: seq 50, tx_seq 1000
               ├─ RX102: seq 71, tx_seq 1000
               └─ RX103: seq 33, tx_seq 1000
```

여기서 중요한 함정은 **행 번호 결합 금지**다. 어떤 RX가 중간 `tx_seq` 하나를 놓치면 그 뒤 모든 행 번호가 밀리므로, "세 파일의 n번째 행끼리"를 같은 시각으로 취급하면 정렬이 통째로 어긋난다. 실측에서 세 RX가 모두 받은 grid 위치는 91.1%뿐이다 (아래 4절). 반드시 같은 `tx_seq` 값을 같은 위치에 놓아야 한다.

또한 TX는 목표 100Hz로 10ms마다 1씩 증가시키므로, `tx_seq` 1칸 = 10ms로 간주할 수 있어 별도 시계 동기화 없이 시간축 역할까지 한다. 단, 100Hz는 목표값이므로 "1칸 = 10ms"는 **가정**이다 — 실제 송신 간격의 미세한 흔들림은 현재 설계에서 무시하며, RX의 실제 수신 시각을 복원하지는 않는다.

## 3. 왜 `seq`는 품질 검사 전용인가

`seq`는 CSI callback 처리 순서라서 다음 두 가지를 알려 준다.

1. **RX 재부팅**: `seq`가 큰 값에서 0 근처로 돌아가면 RX가 새로 시작한 것이다. 재부팅 전후의 `tx_seq`가 이어져 보여도 서로 다른 실행 구간이므로 segment를 나눠야 한다.
2. **저장 경로 손실**: header가 정상이라는 전제에서 `seq`가 1보다 크게 증가하면(예: 4000→4003) 중간 callback record가 ring buffer/USB/reader 구간에서 사라진 것이다. 이는 재부팅이 아니다.

`seq`와 `tx_seq`를 함께 보면 손실 위치를 좁힐 수 있다.

여기서 `delta`는 `현재 record의 값 - 이전 record의 값`이다. 예를 들어
`100→101`은 `delta = 1`, `100→103`은 `delta = 3`이므로 `delta > 1`이다.

| `seq delta` | `tx_seq delta` | 예 | 해석 |
|---|---|---|---|
| `= 1` | `= 1` | `100→101`, `5000→5001` | 정상 연속 |
| `= 1` | `> 1` | `100→101`, `5000→5003` | 이 RX에 중간 TX 번호가 없음. TX 송신 실패·무선 손실·callback 미생성 중 무엇인지는 JSONL만으로 구분할 수 없음 |
| `> 1` | `> 1` | `100→103`, `5000→5003` | callback에서 `seq`를 받은 중간 record가 ring buffer·USB·reader 저장 경로에서 유실됐을 가능성이 큼 |
| `> 1` | `= 1` | `100→103`, `5000→5001` | 정상 흐름으로 설명하기 어려움. 순번 손상·중복·record 순서 혼합 여부를 앞뒤 record로 검사 |
| `< 0` | `> 0` | `29999→0`, `5000→8000` | RX 재부팅 또는 `seq` 손상 후보. TX는 계속 동작했을 수 있음 |
| `> 0` | `< 0` | `100→101`, `5000→10` | 단일 header 손상, TX 재부팅 또는 TX epoch 혼합 후보. 다음 record까지 검사 |
| `= 0` | 임의 | `100→100`, `5000→5001` | 중복 또는 header 손상 후보. 정상 record로 자동 처리하지 않음 |
| 임의 | `= 0` | `100→101`, `5000→5000` | 동일 TX 번호 중복 또는 header 손상 후보. 정상 record로 자동 처리하지 않음 |
| `< 0` | `< 0` | `29999→0`, `5000→0` | RX·TX 동시 재부팅, data epoch 혼합 또는 header 손상 후보. 앞뒤 흐름으로 판정 |

**감소는 반드시 3-record(이전–현재–다음)로 판정한다.** 감소 한 번을 보고 바로 "TX 재부팅"이라 단정하면 안 된다. 실데이터의 `tx_seq` 감소 8건은 전부 다음 record에서 원래 흐름으로 복귀하는 **단일 binary header 손상**이었고, 진짜 TX 재부팅(감소한 작은 값이 계속 증가하는 패턴)은 0건이었다.

## 4. 실데이터가 보여주는 근거 (20260616, 30 session)

공식 설계의 임계값은 다음 집계에서 나왔다.

**원시(정리 전) 인접 transition 집계** — record 2,583,125개, JSONL 90개. 각 파일의 첫 record는 비교 대상이 없으므로 transition 수는 2,583,035이며 아래 각 열의 합과 일치한다:

(transition: 각 RX JSONL에서 바로 앞뒤에 있는 두 record를 비교하여 seq와 tx_seq가 얼마나 변했는지 센 결과)

| 변화 | `seq` | `tx_seq` |
|---|---:|---:|
| delta = 1 | 2,582,936 | 2,503,821 |
| delta > 1 (gap) | 9 | 79,206 |
| delta = 0 (중복) | 0 | 0 |
| delta < 0 (감소) | 90 | 8 |

- `seq` 감소 90회는 "수집 중 재부팅 90번"이 아니다. 대부분 파일 앞에 붙은 이전 실행의 잔여 record 2~5개와 현재 수집 boot의 경계다. 그래서 **파일 전체를 바로 정렬하지 말고 boot segment 분리가 먼저**다.
- `tx_seq` 감소 8건은 모두 단일 손상. 예: session 11/RX 103의 `seq 17456→3288334404→17459`, `tx_seq 617288→2411→617291` — 가운데 하나만 제거하면 두 값 모두 정상 흐름으로 복귀한다. 이 record 하나를 TX 재부팅으로 오판하면 29,963 frame짜리 정상 세션에서 앞 17,529 frame만 쓰는 손해를 본다(과거 실제 발생했던 판정 오류).

**정상 구간의 `tx_seq` gap 분포** (79,116건):

| gap 크기 (빠진 개수) | 횟수 |
|---|---:|
| 1개 | 72,316 |
| 2~4개 | 6,747 |
| 5~9개 | 52 |
| 10개 이상 (최대 12) | 1 |

gap의 91.4%가 1 frame(10ms)이다. 그래서 "5 frame(50ms) 이하만 보간"이라는 기준이 대부분의 gap을 살리면서 긴 공백의 조작을 막는다.

**세 RX 동시 수신률** (29 session 공통 grid 869,961 위치):

| 수신 RX 수 | 비율 |
|---:|---:|
| 3대 | 91.148% |
| 2대 | 7.958% |
| 1대 | 0.841% |
| 0대 | 0.053% |

grid 위치의 약 9%에서 최소 한 RX가 빠진다 — `present_mask` 없이 "다 받았다"고 가정할 수 없는 이유다.

## 5. 공식 전처리 절차 요약 (9단계)

[sequence-patterns.md 7절](../../../doc/sequence-patterns.md)의 판정 순서와 [Preprocessing Design](design.md)을 합친 전체 흐름이다.

```text
1. RX별 JSONL을 파일 순서대로 읽는다 (tx_seq로 미리 정렬 금지)
2. 이전·현재·다음 3-record 비교로 단일 손상 record 제거
3. seq 감소 조건으로 RX 재부팅 segment 분리
4. 정리 후에도 tx_seq 감소가 지속되면 session 제외
5. 세 RX 안정 segment 조합별 tx_seq 교집합 계산 → 가장 긴 유효 교집합 선택
6. 품질 gate: 공통 길이·RX별 관측률 검사
7. 공통 tx_seq 정수 grid + RX별 present_mask 생성
8. 내부 gap 5 frame 이하만 서브캐리어별 선형 보간
9. 긴 gap이 낀 3초 window 제외 → (300, 192) window 생성
```

단계별 핵심 판정 규칙:

**2단계 — 단일 손상 판정** (파일 순서 유지 상태에서):

연속된 세 record를 다음과 같이 부른다.

| 이름 | 의미 |
|---|---|
| A | 이전 정상 후보 record |
| B | 단일 손상 여부를 검사할 가운데 record |
| C | 다음 정상 후보 record |

판정은 다음 순서로 진행한다.

1. **B를 건너뛴 TX 흐름을 먼저 확인한다.** A의 `tx_seq`보다 C의 `tx_seq`가
   커야 한다. 그렇지 않으면 B 하나를 제거해도 TX 흐름이 복구되지 않으므로 단일
   손상으로 자동 제거하지 않는다.
2. **A와 C가 같은 RX boot 구간인지 확인한다.** A의 `seq`보다 C의 `seq`가 크면
   같은 RX boot 구간으로 본다.
3. **같은 RX boot 구간이면 B의 두 순번을 모두 확인한다.** B의 `tx_seq`가 A와 C
   사이에 없거나, B의 `seq`가 A와 C 사이에 없으면 B를 단일 손상으로 판정한다.
   두 값이 모두 앞뒤 사이에 있으면 B는 정상 record이므로 제거하지 않는다.
4. **A와 C 사이에 RX 재부팅이 있으면 별도로 판정한다.** C의 `seq`가 10 이하로
   다시 시작했고 B의 `tx_seq`만 A와 C 사이를 벗어났다면 B를 재부팅 경계의 단일
   손상으로 판정한다. C에는 `boot_start_after_corrupt` 표시를 남겨 새 RX
   segment의 첫 record로 사용한다.
5. **판정 조건이 모호하면 자동 제거하지 않는다.** 연속 손상, TX 흐름이 복구되지
   않는 경우, C가 작은 `seq`로 시작하지 않는 불명확한 감소는 품질 오류 후보로
   남긴다.

여기서 “B가 A와 C 사이에 있다”는 것은 값이 두 경계보다 엄격하게 크고 작다는
뜻이다. A 또는 C와 같은 중복값도 정상 증가로 보지 않는다.

| 상황 | 단일 손상 판정 |
|---|---|
| 같은 RX boot이며 B의 `seq`와 `tx_seq`가 모두 A와 C 사이 | 아님 |
| 같은 RX boot이며 B의 `seq` 또는 `tx_seq` 하나 이상이 A와 C 밖 | 맞음 |
| RX 재부팅 경계이며 C의 `seq`가 10 이하이고 B의 `tx_seq`가 A와 C 밖 | 맞음 |
| B를 빼도 A의 `tx_seq`보다 C의 `tx_seq`가 작거나 같음 | 아님, 자동 복구 금지 |

예를 들어 `tx_seq`가 `5000→4950→5001` 또는 `5000→999999→5001`이면 가운데
값을 제외한 `5000→5001`이 복구되므로 단일 손상 후보가 된다. 반면
`5000→4950→4951`은 가운데를 제외해도 감소가 남으므로 단일 손상으로 자동
제거하지 않는다.

단일 손상으로 판정하면 가운데 record의 CSI와 metadata를 모두 제외하고 그 위치와
이유를 품질 보고서에 기록한다. 첫 record와 마지막 record는 비교할 양쪽 이웃이
없으므로 이 규칙으로 자동 제거하지 않으며, 가장자리 이상은 뒤 단계의
교집합·품질 gate가 걸러낸다.

제외해도 이전→다음이 이어지지 않으면 자동 복구하지 않고 세션을 품질 오류 후보로 남긴다. CSI 값이 멀쩡해 보여도 어느 TX frame인지 알 수 없으므로 손상 record의 CSI는 쓰지 않는다.

**3단계 — RX 재부팅 판정**:

```text
current_seq < previous_seq  이고
(current_seq <= 10  또는  previous_seq - current_seq >= 100)
```

`seq=0` record가 반드시 존재한다고 가정하지 않는다(경계 record 자체가 손상·유실될 수 있음). 경계 record는 제외하고 다음 정상 record부터 새 segment를 시작한다. 단, 2단계에서 손상 제거 후 `boot_start_after_corrupt` 표시가 남은 record는 새 segment의 첫 record로 사용한다.

수집률 도구(`measure_csi_hz.py`)가 쓰는 "`seq`와 `timestamp_us` 동시 감소" 조건은 전처리에 재사용하지 않는다 — 실데이터(session 17·26의 RX 102)에서 경계 record의 `timestamp_us`가 비정상적으로 큰 값이어서, "동시 감소" 조건이 성립하지 않는 재부팅 경계가 실제로 존재하기 때문이다. 전처리의 재부팅 판정은 `seq`만 사용한다.

**5단계 — 교집합 계산** (segment 조합별):

```python
common_start = max(rx101_start, rx102_start, rx103_start)  # 가장 늦은 시작
common_end   = min(rx101_end,   rx102_end,   rx103_end)    # 가장 이른 끝
```

합집합(최소 시작~최대 끝)을 쓰면 일부 RX에 없는 구간을 보간으로 만들어내게 되므로 금지. 무조건 마지막 segment만 고르지 말고, 조합 중 공통 길이가 가장 긴 것을 선택한다(동률이면 파일 앞쪽 우선 — 결과 재현성).

**6단계 — session 품질 gate**. 다음 중 하나라도 해당하면 세션을 제외한다:

```text
세 RX 파일 또는 정상 안정 segment 조합이 없음
단일 손상 제거 후에도 tx_seq 감소가 지속됨
공통 길이 < 27,000 frame
RX 하나라도 관측률 < 0.85
```

내부 gap이 5 frame을 넘는다는 이유**만으로는** 세션을 제외하지 않는다 — 그 gap과 겹치는 window만 뒤 단계(9단계)에서 버린다. 20260616에 적용하면 session 22만 탈락한다 (RX 102 파일에 이전 실행의 잔여 record 3개뿐, 세 RX 공통 구간이 3 frame).

**공식 파라미터** (test 결과를 본 뒤 바꾸지 않도록 manifest에 고정):

| 항목 | 값 | 근거 |
|---|---:|---|
| 최소 공통 길이 | 27,000 frame | 5분×100Hz 목표(30,000)의 90% |
| 최소 RX 관측률 | 0.85 | 29개 정상 세션의 실측 최솟값이 0.8652 |
| 최대 보간 gap | 5 frame (50ms) | gap의 99.9% 이상이 이 범위 |
| window / stride | 300 / 30 frame | 3초 / 0.3초 |
| RX 순서 | [101, 102, 103] 고정 | feature 결합 재현성 |
| RX당 feature | 64 (`csi_amp` 전체) | baseline은 절단 없이 사용 |

정확히 30,000 frame으로 자르거나 채우지 않는다. 공통 길이가 29,963이든 30,012든 실제 교집합 전체를 쓴다.

**7~9단계 — grid·mask·window**:

```text
aligned:      (3, T, 64)  # RX × 공통 tx_seq 길이 × amplitude
present_mask: (3, T)      # 실제 수신 여부 (보간해도 False 유지)
결합:         (T, 192) → window (300, 192)
최종:         X (N, 300, 192) float32, y (N,) int64
```

같은 segment 안에 중복 `tx_seq`가 있으면 파일 순서상 첫 record만 쓰고 중복 수를 기록한다 (20260616 실측에서는 중복 0건).

보간한 frame은 계산에는 쓰되 **수신한 record로 세지 않는다** (관측률·mask는 원본 기준). 세 RX 중 하나라도 6 frame 이상 누락이 window 범위에 있으면 그 window는 버린다. 서로 다른 session이나 segment를 이어 붙여 window를 만들지 않는다.

## 6. 학습 연결 시 필수 규칙

전처리가 만든 window를 학습에 넘길 때 다음 규칙을 지킨다.

1. **session을 먼저 train·validation·test로 나눈다.** 
   같은 session에서 만든
   인접 window는 최대 90%가 겹치므로, window를 무작위로 나누면 거의 같은 데이터가
   train과 test에 함께 들어간다. 20260616 초안은 session 22를 제외하고
   train 17개, validation 6개, test 6개다.
2. **평균과 표준편차는 train 데이터로만 계산한다.** 그 값을 validation과 test에도
   그대로 적용한다. Validation은 모델 설정 선택에 사용하고 test는 최종 모델의
   성능 확인에 한 번 사용한다.
3. **label 이름과 번호를 고정한다.** Dataset metadata와 현재 공식 구현은
   `empty=0`, `static=1`, `motion=2`를 사용한다. `action=2`는 구형
   `Preprocessing.py`에만 남아 있는 과거 표기다.
4. **모델에는 CSI만 넣는다.** 한 window의 입력은 `csi_amp`로 만든
   `(300, 192)` 배열이다. `seq`, `tx_seq`, `timestamp_us`는 학습 feature가 아니다.
   `session_id`와 시작 `tx_seq`는 결과를 원본 구간과 연결하기 위한 정보로만
   저장한다.
5. **manifest에 전처리 결과를 기록한다.** 사용한 session 분할, 선택한 공통 범위,
   제외 이유, 관측률, 임계값, label 규칙, normalization 값을 저장한다. 같은 설정으로
   다시 전처리하고 학습할 수 있게 만드는 기록이다.

## 7. 피해야 할 판정 (요약표)

| 하지 말 것 | 대신 할 것 |
|---|---|
| 세 파일의 같은 행 번호끼리 결합 | 같은 `tx_seq` 값끼리 결합 |
| `tx_seq` 감소 한 번 → TX 재부팅 판정 | 다음 record 복귀 여부로 단일 손상 먼저 확인 |
| `seq` 감소 횟수 = 재부팅 횟수 | 파일 앞 잔여 record 경계와 구분 |
| 세 RX 범위의 합집합 사용 | 안정 segment의 교집합만 사용 |
| 모든 gap을 `np.interp`로 보간 | 5 frame 이하 내부 gap만, 범위 밖 extrapolation 금지 |
| `received_at_unix_us`로 정렬·판정 | `seq`(품질)·`tx_seq`(정렬)만 사용 |
| 보간 frame을 수신 record로 집계 | `present_mask`는 원본 수신 여부 유지 |
| 처음부터 `tx_seq` 오름차순 정렬 | 파일 순서 유지 → 손상 제거·segment 분리 후 정렬 |

## 8. 구형 `Preprocessing.py`와 공식 구현의 차이

구형 [`Preprocessing.py`](../../lstm/Preprocessing.py)(HISTORICAL)는 공식 구현과 다음이 다르다.

1. **단일 손상 제거·boot segment 분리 없음** — 모든 record를 바로 `tx_seq` 정렬하므로 손상 record와 이전 실행의 잔여 record가 정렬·공통 범위 계산에 그대로 섞인다
2. **RX 1대(`[102]`)** — 3-RX 교집합·mask 미구현
3. **모든 크기의 gap을 무제한 보간** — 5 frame 제한·window 제외 없음
4. `csi_amp[:52]` 절단 — 공식 baseline은 64개 전체 사용
5. 결과 미저장·manifest 없음, label/split 하드코딩

전체 구현 명세와 완료 조건 체크리스트는 [Preprocessing Design](design.md) 7~8절에 있다.

## 9. 이 레포트가 결정하지 않는 것

- 64개 중 null/DC/guard 서브캐리어 제외 여부 (baseline은 64개 전체)
- 위상(phase) feature 추가
- `first_word_invalid` flag 처리 (현재 JSONL에 미저장)
- 모델 구조 변경

이 항목들은 현재 baseline 범위에 포함되지 않으며, 필요할 때 train·validation
비교로 별도 결정한다.
