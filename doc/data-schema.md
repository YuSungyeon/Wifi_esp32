# CSI USB binary와 JSONL 계약

> 상태: **CURRENT CONTRACT**
> producer: `esp32s3_csi_recv_poc/main/app_main.c`
> consumer: `scripts/csi_serial_reader.py`

## 1. Binary frame v2

- Endianness: little-endian
- Header: packed 32 bytes
- Payload: signed int8 raw CSI
- CRC: 없음

```text
[32-byte header][raw[raw_len]]
```

| offset | type | field | 계약 |
|---:|---|---|---|
| 0 | `uint16` | `magic` | `0x4353` |
| 2 | `uint8` | `version` | `2` |
| 3 | `uint8` | `reserved0` | `0` |
| 4 | `uint16` | `total_len` | `32 + raw_len` |
| 6 | `uint16` | `raw_len` | 양수, 짝수, 최대 384 |
| 8 | `uint32` | `seq` | RX별 frame sequence |
| 12 | `uint64` | `timestamp_us` | RX별 `esp_timer_get_time()` |
| 20 | `int8` | `rssi` | dBm |
| 21 | `uint8` | `channel` | 현재 11 |
| 22 | `int8` | `noise_floor` | dBm |
| 23 | `uint8` | `rate` | ESP-IDF RX rate code |
| 24 | `uint16` | `sig_len` | received frame length metadata |
| 26 | `uint16` | `reserved1` | `0` |
| 28 | `uint32` | `tx_seq` | TX 공통 sequence |
| 32 | `int8[]` | `raw` | I/Q interleaved CSI bytes |

Python format:

```python
HEADER_FORMAT = "<HBBHHIQbBbBHHI"
HEADER_SIZE = 32
```

## 2. Reader validation

Reader는 frame 저장 전에 다음을 모두 확인한다.

- magic이 `0x4353`
- version이 `2`
- `0 < raw_len <= 384`
- `raw_len`이 짝수
- `total_len == 32 + raw_len`

검증 실패 시 `invalid` count를 증가시키고 다음 magic을 다시 찾는다.

CRC가 없으므로 위 검증과 magic scan이 stream 재동기화 수단이다.

## 3. Amplitude 변환

`raw`는 signed int8 I/Q가 교차 배치된 배열이다.

```text
[I0, Q0, I1, Q1, ...]
```

Reader는 pair마다 다음 값을 계산한다.

```text
amplitude[k] = sqrt(Ik² + Qk²)
sample_count = raw_len / 2
```

현재 HT20/LLTF 설정의 기대값은 `raw_len=128`, `sample_count=64`다. 이 값에는 moving average, z-score, clip이 적용되지 않는다.

## 4. Sequence와 시간

| field | 비교 가능한 범위 | 사용 |
|---|---|---|
| `seq` | 같은 RX process | RX frame drop |
| `timestamp_us` | 같은 RX boot | RX 내부 간격 |
| `received_at_unix_us` | Mac wall clock | 저장/시각화 |
| `tx_seq` | 모든 RX | cross-RX join |

다중 RX 학습 입력은 `tx_seq`를 join key로 사용한다.

인접 값의 증가·누락·감소 패턴과 실제 데이터셋 집계는
[`seq`와 `tx_seq` 패턴 기준](sequence-patterns.md)을 따른다.

## 5. JSONL record schema v1

Reader는 frame 하나를 JSON object 한 줄로 저장한다.

필수 field:

| field | type | 의미 |
|---|---|---|
| `record_schema_version` | int | 현재 `1` |
| `transport` | string | `usb_serial_jtag` |
| `csi_representation` | string | `raw_iq_amplitude` |
| `received_at_unix_us` | int | Mac 수신 시각 |
| `source_ip` | string | `usb-serial` |
| `source_port` | int | `0` |
| `session_id` | int | reader CLI 인자/YAML run ID |
| `firmware_session_id` | int | 항상 `0` |
| `device_id` | int | RX registry의 논리 ID |
| `seq` | int | RX별 sequence |
| `tx_seq` | int | TX 공통 sequence |
| `timestamp_us` | int | RX timer |
| `channel` | int | Wi-Fi channel |
| `rssi_dbm` | int | RSSI |
| `noise_floor_dbm` | int | noise floor |
| `rate` | int | RX rate code |
| `sig_len` | int | RF frame length metadata |
| `sample_count` | int | `len(csi_amp)` |
| `csi_amp` | float[] | raw I/Q amplitude |

## 6. 저장 경로와 append

```text
mac_collector_output/raw/YYYYMMDD/
  session_<session_id>/device_<device_id>.jsonl
```

Reader는 append mode를 사용한다. 같은 날짜·session·device 조합을 재사용하면 하나의 파일에 여러 run이 섞일 수 있으므로 session ID를 재사용하지 않는다.

## 7. Contract 변경 절차

Binary 또는 JSONL field를 변경할 때:

1. 이 문서의 version과 layout을 먼저 수정한다.
2. RX producer와 Python consumer를 같은 변경에서 수정한다.
3. header size, valid frame, invalid version/length test를 실행한다.
4. preprocessing과 visualization consumer 영향을 확인한다.
5. architecture와 firmware 문서 링크가 유효한지 검사한다.
