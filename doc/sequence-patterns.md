# `seq`와 `tx_seq` 패턴 기준

> 상태: **CURRENT** — 값의 생성 방식과 해석은 현재 firmware 기준
> 데이터 확인 범위: `mac_collector_output/raw/20260616/session_1` ~ `session_30`
> 확인일: 2026-08-10

## 1. 이 문서의 목적

JSONL에는 순번이 두 개 있다.

- `seq`: 한 RX가 CSI callback을 처리한 순서
- `tx_seq`: TX가 ESP-NOW frame을 송신한 순서

두 값은 비슷하게 증가하지만 기준 장치와 용도가 다르다. `seq`는 RX 재부팅과 RX
내부 저장 누락을 검사하는 값이고, `tx_seq`는 세 RX가 같은 TX frame을 받았는지
맞추는 값이다.

```text
TX frame 1000 ─┬─ RX101: seq 50, tx_seq 1000
               ├─ RX102: seq 71, tx_seq 1000
               └─ RX103: seq 33, tx_seq 1000
```

위 예에서 RX별 `seq`는 서로 다르지만 `tx_seq=1000`은 같다. 따라서 세 RX 정렬에는
`tx_seq`를 사용하고, 서로 다른 RX의 `seq`를 직접 비교하지 않는다.

## 2. 값이 만들어지는 위치

### 2.1 `seq`: RX가 붙이는 독립 순번

RX firmware는 지정된 TX MAC에서 CSI callback이 들어올 때마다 다음 순서로
처리한다.

```text
CSI callback 수신
  → 현재 g_frame_seq를 binary header의 seq에 기록
  → g_frame_seq를 1 증가
  → binary frame을 ring buffer에 넣기
  → USB reader가 JSONL에 기록
```

특징은 다음과 같다.

- RX 101·102·103이 각자 따로 가진다.
- RX가 부팅되면 `0`부터 시작한다.
- callback 뒤 ring buffer에 넣기 전에 순번이 먼저 증가한다.
- RX가 ESP-NOW frame 자체를 받지 못해 callback이 없으면 그 frame에 대한 `seq`도
  만들어지지 않는다.
- `uint32`이므로 이론적으로 최댓값 `4,294,967,295` 다음에는 0으로 순환한다.
  약 100Hz로 쉬지 않고 증가하면 약 497일 뒤다. 현재 데이터에서는 순환이
  관찰되지 않았다.

### 2.2 `tx_seq`: TX가 frame에 넣는 공통 순번

TX firmware의 무한 loop는 `count=0,1,2,...`를 ESP-NOW payload에 넣어 목표
100Hz로 broadcast한다. RX는 받은 payload에서 이 값을 꺼내 binary header와
JSONL의 `tx_seq`에 기록한다.

특징은 다음과 같다.

- 하나의 TX가 만들기 때문에 세 RX가 같은 값을 공유할 수 있다.
- TX가 처음 부팅되거나 실제로 재부팅되면 `0`부터 다시 시작한다.
- 수집 session을 시작하거나 종료하는 동작은 TX를 재부팅하지 않는다.
- 송신 loop의 호출 순번이므로 특정 RX가 frame을 놓쳐도 TX에서는 계속 증가한다.
- `uint32` 순환 시간은 목표 100Hz에서 약 497일이며 현재 데이터에서는 관찰되지
  않았다.

## 3. 인접 값의 기본 패턴

아래에서 `delta`는 `현재 값 - 이전 값`이다.

### 3.1 `seq` 패턴

| 패턴 | 예 | 기본 해석 | 처리 |
|---|---|---|---|
| `delta = 1` | `4000 → 4001` | 연속된 RX callback frame이 JSONL에 존재 | 정상 |
| `delta > 1` | `4000 → 4003` | 중간 `seq`가 JSONL 경로에서 사라짐 | 누락 수와 위치 기록 |
| `delta = 0` | `4000 → 4000` | 중복 또는 손상 가능성 | 자동 정상 처리 금지 |
| `delta < 0` | `29999 → 0` | RX 재부팅, 손상, 순환 중 하나 | 앞뒤 record까지 검사 |

`4000 → 4003`은 RX 재부팅이 아니다. `seq=4001,4002`는 callback에서 순번을
받았지만 ring buffer drop, USB 전송, reader 재동기화 등의 구간에서 JSONL에
남지 않았을 수 있다. JSONL만으로 정확한 손실 위치를 구분할 수는 없다.

RX 재부팅 후보는 현재 전처리 설계에서 다음 조건으로 찾는다.

```text
current_seq < previous_seq
and
(current_seq <= 10 or previous_seq - current_seq >= 100)
```

`seq=0` record는 손상되거나 reader가 놓칠 수 있으므로 반드시 존재해야 하는
조건으로 사용하지 않는다.

### 3.2 `tx_seq` 패턴

| 패턴 | 예 | 기본 해석 | 처리 |
|---|---|---|---|
| `delta = 1` | `8000 → 8001` | 이 RX 파일에 연속 TX frame이 존재 | 정상 |
| `delta > 1` | `8000 → 8003` | 이 RX 파일에 `8001,8002`가 없음 | 공통 grid에서 누락 mask 생성 |
| `delta = 0` | `8000 → 8000` | 같은 TX 번호 중복 또는 손상 | 중복 정책 적용 |
| `delta < 0` | `8000 → 200` | 단일 손상, TX 재부팅, 순환, 데이터 혼합 가능 | 다음 record까지 검사 |

`tx_seq` gap은 “TX가 반드시 정상 송신했지만 RX가 무선에서 놓쳤다”는 뜻까지는
아니다. 가능한 원인은 TX send 실패, 무선 손실, RX callback 미생성, callback 이후
저장 손실이다. 세 RX의 같은 번호 존재 여부와 RX `seq`를 같이 봐야 범위를 좁힐
수 있지만 JSONL만으로 원인을 확정할 수는 없다.

## 4. `seq`와 `tx_seq`를 함께 보는 패턴

| `seq` 변화 | `tx_seq` 변화 | 의미 |
|---|---|---|
| `+1` | `+1` | 가장 일반적인 정상 연속 record |
| `+1` | `>+1` | RX callback 사이에서 하나 이상의 TX 번호를 관측하지 못함 |
| `>+1` | `>+1` | callback 뒤 저장 경로에서 record가 빠졌을 가능성이 큼 |
| `>+1` | `+1` | 순번 손상·중복·순서 혼합 가능성이 있어 앞뒤 검증 필요 |
| 감소 | 증가 | RX 재부팅 가능성. TX는 계속 동작할 수 있음 |
| 증가 | 감소 | `tx_seq` 단일 손상 또는 TX epoch 변경 가능성 |
| 둘 다 비정상 | 둘 다 큰 값으로 튐 | binary header 손상 가능성이 큼 |

대표 예는 다음과 같다.

```text
seq:     100 → 101
tx_seq: 5000 → 5002
```

RX가 JSONL에 남긴 callback 순번은 연속이지만 `tx_seq=5001`은 이 RX에서 관측되지
않았다. 즉 `seq`만 보면 RX 저장 경로는 연속이고, `tx_seq`를 봐야 무선 frame
기준의 빈칸을 알 수 있다.

```text
seq:     100 → 102
tx_seq: 5000 → 5002
```

두 값이 함께 한 칸 건너뛰었다. `seq=101`을 부여받은 callback record가 JSONL에
남지 않았을 가능성이 높다.

## 5. 감소 패턴은 세 record로 판정한다

감소 한 번만 보고 재부팅이라고 결론 내리지 않는다. 파일 순서를 유지한 채
`이전 → 현재 → 다음`을 확인한다.

### 5.1 한 record만 아래로 튄 뒤 복귀

```text
5000 → 4950 → 5001
```

가운데 `4950`을 제외하면 `5000 → 5001`로 정상 흐름이 복구된다. 현재 전처리
설계에서는 가운데 record의 `seq`도 앞뒤 흐름을 벗어났는지 확인한 뒤 단일 손상
record로 제외한다. 해당 CSI가 정상처럼 보여도 정확한 TX 위치를 알 수 없으므로
학습에는 사용하지 않는다.

### 5.2 감소한 작은 값부터 계속 증가

```text
5000 → 4950 → 4951 → 4952
```

이 경우 `4950` 하나만 지워도 기존 흐름으로 돌아오지 않는다. 실제 TX 재부팅,
서로 다른 TX epoch가 한 파일에 섞임, 긴 순서 역전 중 하나일 수 있다.

```text
5000 → 0 → 1 → 2
```

위 형태는 TX 재부팅 가능성이 더 분명한 지속 감소 패턴이다. 현재 3-RX 전처리
설계는 단일 record로 복구되지 않는 `tx_seq` 감소가 남으면 해당 session을
자동으로 이어 붙이지 않고 제외한다.

### 5.3 `seq`만 0 근처로 돌아가고 `tx_seq`는 계속 증가

```text
seq:     29999 → 0 → 1 → 2
tx_seq: 50000 → 53000 → 53001 → 53002
```

RX만 재부팅되고 TX는 계속 송신한 패턴이다. RX boot segment를 나눈 뒤 세 RX의
안정 segment 조합에서 가장 긴 `tx_seq` 교집합을 찾는다. `seq`가 다시 0이
되었다고 `tx_seq`까지 0으로 바꾸면 안 된다.

## 6. 20260616 데이터에서 확인된 패턴

### 6.1 원시 인접 transition 집계

30개 session, RX 3대의 JSONL 90개, 총 `2,583,125` record를 파일 순서대로
검사했다.

| 변화 | `seq` 횟수 | `tx_seq` 횟수 |
|---|---:|---:|
| `delta = 1` | 2,582,936 | 2,503,821 |
| `delta > 1` | 9 | 79,206 |
| `delta = 0` | 0 | 0 |
| `delta < 0` | 90 | 8 |

이 표의 원시 `seq` 감소 90회를 “유효 수집 중 RX가 90번 재부팅됐다”로 해석하면
안 된다. 각 JSONL 앞에는 이전 RX 실행에서 남은 2~5개 record가 붙어 있고, 그
뒤에 현재 수집용 RX boot가 시작하는 형태가 대부분이다.

- 정상 또는 손상된 형태로 새 RX boot 경계가 확인된 파일: 89개
- 현재 수집 구간 없이 이전 record 3개만 저장된 파일: session 22 / RX 102
- 유효 구간 중간의 단일 손상으로 감소가 한 번 더 생긴 파일: session 11 / RX 103

따라서 파일 전체를 곧바로 `tx_seq` 정렬하지 않고, 앞의 짧은 RX segment와 현재
수집 segment를 먼저 분리해야 한다.

### 6.2 단일 binary header 손상 8건

`tx_seq`가 감소한 8개 record는 모두 다음 record에서 감소 전보다 큰 정상
`tx_seq`로 즉시 복귀했다. 같은 record의 `seq` 또는 `timestamp_us`도 비정상적인
큰 값이어서 실제 TX 재부팅이 아니라 단일 binary header 손상 패턴으로 판단한다.

| session | RX | JSONL line | 위치 |
|---:|---:|---:|---|
| 3 | 102 | 4 | RX boot 경계 |
| 8 | 102 | 4 | RX boot 경계 |
| 8 | 103 | 5 | RX boot 경계 |
| 11 | 103 | 17,461 | 유효 수집 중간 |
| 17 | 102 | 6 | RX boot 경계 |
| 22 | 103 | 5 | RX boot 경계 |
| 23 | 101 | 5 | RX boot 경계 |
| 26 | 102 | 4 | RX boot 경계 |

session 11 / RX 103의 실제 값은 다음과 같다.

```text
seq:     17456 → 3288334404 → 17459
tx_seq: 617288 →       2411 → 617291
```

가운데 record만 제외하면 두 값 모두 기존 증가 흐름으로 돌아온다. 전체 데이터에서
`5000 → 0 → 1 → 2`처럼 감소한 `tx_seq`가 계속 증가하는 실제 TX 재부팅 패턴은
0건이었다.

### 6.3 정상 구간 안의 gap

8개 손상 record와 RX boot 경계에 연결된 transition을 제외하면 다음 패턴이
남았다.

| 조합 | 횟수 |
|---|---:|
| `seq +1`, `tx_seq +1` | 2,503,821 |
| `seq +1`, `tx_seq gap` | 79,115 |
| `seq gap`, `tx_seq gap` | 1 |
| `seq gap`, `tx_seq +1` | 0 |

정상 구간의 `seq` gap은 session 3 / RX 103에서 한 번 확인됐다.

```text
JSONL line 3995 → 3996
seq:     3991 → 3993
tx_seq: 206210 → 206212
```

정상 구간의 `tx_seq` gap 79,116건은 다음 크기로 분포했다.

| `tx_seq delta` | 횟수 | 빠진 번호 수/회 |
|---|---:|---:|
| 2 | 72,316 | 1 |
| 3~5 | 6,747 | 2~4 |
| 6~10 | 52 | 5~9 |
| 11~100 | 1 | 12 |
| 100 초과 | 0 | - |

최대 delta는 `13`이었고, 모든 gap에서 비어 있는 `tx_seq`를 더하면 87,253개다.
대부분은 한 번호만 빠지는 짧은 gap이지만, gap 개수만 보고 전부 선형보간하지
않는다. 현재 설계는 최대 5 frame 이하만 보간하고 긴 gap이 포함된 학습 window는
제외한다.

### 6.4 세 RX에 나타나는 수신 조합

8개 단일 손상 record를 제거하고 RX별 가장 긴 안정 segment를 선택한 뒤,
session 22를 제외한 29개 session의 공통 `tx_seq` grid를 검사했다. RX boot 경계
record는 제외했으며 공통 길이는 session당 `29,963~30,012`였다.

| 같은 `tx_seq`를 가진 RX 수 | grid 위치 수 | 비율 | 의미 |
|---:|---:|---:|---|
| 3대 | 792,948 | 91.148% | 세 RX 모두 실제 수신 |
| 2대 | 69,234 | 7.958% | 한 RX만 누락 |
| 1대 | 7,315 | 0.841% | 두 RX 누락 |
| 0대 | 464 | 0.053% | 세 RX 모두 해당 번호 없음 |
| 합계 | 869,961 | 100% | 29개 공통 grid 전체 |

이 분포 때문에 세 JSONL의 첫 번째 행끼리, 두 번째 행끼리 단순 결합하면 안 된다.
어떤 RX가 중간 `tx_seq`를 놓치면 그 뒤의 행 번호가 밀리기 때문이다. 공통 정수
`tx_seq` grid를 만든 뒤 같은 번호를 같은 위치에 놓고 RX별 존재 mask를 따로
보존해야 한다.

## 7. 전처리 판정 순서

```text
1. JSONL을 파일 순서대로 읽는다.
2. 이전·현재·다음의 seq와 tx_seq로 단일 손상 record를 먼저 제거한다.
3. seq 감소로 RX boot segment를 나눈다.
4. 각 RX의 가장 긴 안정 segment 후보를 구한다.
5. 손상 제거 뒤에도 tx_seq 감소가 지속되면 session을 제외한다.
6. 세 RX segment의 tx_seq 교집합을 계산한다.
7. 공통 tx_seq grid와 RX별 존재 mask를 만든다.
8. 5 frame 이하의 내부 gap만 보간한다.
9. 긴 gap이 들어간 3초 window는 제외한다.
```

최종 모델 feature에는 `seq`와 `tx_seq`를 넣지 않는다.

- `seq`: RX 재부팅·저장 품질 검사에 사용
- `tx_seq`: 세 RX 정렬·gap mask·window 위치에 사용
- `csi_amp`: 실제 모델 입력으로 사용

## 8. 해석할 때 피해야 할 결론

| 잘못된 해석 | 올바른 해석 |
|---|---|
| `seq`가 증가하므로 TX frame도 하나도 안 빠졌다 | TX frame 누락은 `tx_seq`도 확인해야 한다 |
| `tx_seq`가 감소했으므로 TX가 재부팅됐다 | 다음 record에서 기존 범위로 복귀하는지 먼저 본다 |
| `seq=0`이 없으므로 유효한 boot가 없다 | 0번 record가 손상·누락될 수 있어 작은 seq와 앞뒤 흐름을 함께 본다 |
| 세 RX의 최소~최대 범위를 합집합으로 보간한다 | 세 안정 segment의 교집합만 사용한다 |
| `tx_seq` gap은 전부 무선 손실이다 | TX, RF, RX callback, 저장 경로 원인을 JSONL만으로 확정할 수 없다 |
| 행 번호가 같으면 같은 시각의 frame이다 | 같은 `tx_seq`만 같은 TX frame이다 |

## 9. 관련 문서와 코드

- binary/JSONL field 계약: [data-schema.md](data-schema.md)
- RX·TX 생성 코드 설명: [firmware.md](firmware.md)
- 3-RX 전처리 설계: [공식 전처리 설계](../model_train/docs/%5B전처리%5D-설계.md)
- 전처리 shape와 예시: [LSTM 현재 전처리 구현](../model_train/docs/%5B전처리%5D-현재%20구현.md)
- 수집률·범위 시각화: [postprocessing.md](postprocessing.md)
