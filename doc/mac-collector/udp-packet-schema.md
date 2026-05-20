# UDP 패킷 스키마 (ESP32-S3 CSI → Mac)

ESP32-S3 RX 노드가 Mac 수집기로 보내는 **MVP 바이너리 UDP 패킷** 규격입니다.  
구현 참고: [`esp32s3_csi_sender/main/csi_sender_main.c`](../../esp32s3_csi_sender/main/csi_sender_main.c), [`mac_collector/udp_collector_mvp.py`](../../mac_collector/udp_collector_mvp.py).

## 1) 설계 목표

- 구현 단순성(ESP/파이썬 모두 쉽게 파싱)
- 최소 메타데이터 + `csi_amp[]` 전송
- 패킷 길이 검증으로 잘못된 프레임 조기 탐지

## 2) Endianness 및 타입

- Endianness: **Little-endian**
- 정수: C 고정폭 타입 기준
- 실수: IEEE754 `float32`

## 3) 패킷 레이아웃

헤더(고정 40 bytes) + 진폭 배열(가변)

| 필드 | 타입 | 크기(bytes) | 설명 |
|---|---:|---:|---|
| magic | `uint16` | 2 | 고정값 `0x4353` ("CS") |
| version | `uint8` | 1 | 스키마 버전 (현재 `1`) |
| header_len | `uint8` | 1 | 고정 헤더 길이(현재 `40`) |
| payload_type | `uint8` | 1 | `1` = CSI amplitude |
| flags | `uint8` | 1 | 예약(현재 `0`) |
| reserved0 | `uint16` | 2 | 정렬용 예약값 |
| session_id | `uint32` | 4 | **v1 reserved — 펌웨어는 항상 `0`**. run ID는 Mac `session_meta.yaml` SSOT |
| device_id | `uint32` | 4 | RX 장치 ID (`device_registry.csv`) |
| seq | `uint32` | 4 | 장치별 증가 시퀀스 |
| timestamp_us | `uint64` | 8 | ESP 측 타임스탬프(µs) |
| channel | `uint8` | 1 | Wi-Fi 채널 |
| rssi_dbm | `int8` | 1 | RSSI (dBm) |
| noise_floor_dbm | `int8` | 1 | 없으면 `-128` |
| reserved1 | `uint8` | 1 | 예약 |
| sample_count | `uint16` | 2 | `csi_amp` 샘플 개수 |
| reserved2 | `uint16` | 2 | 예약 |
| crc32 | `uint32` | 4 | 옵션(MVP에서는 `0` 허용) |

페이로드:

- `csi_amp`: `float32 * sample_count`

총 패킷 길이:

- `header_len + sample_count * 4`

## 4) session_id (펌웨어 vs Mac)

| 위치 | 필드 | 의미 |
|------|------|------|
| UDP 헤더 | `session_id` | v1에서 **0 고정**. 레거시 펌웨어가 0이 아니면 수집기가 1회 경고 |
| JSONL | `session_id` | Mac `session_meta.yaml` 루트 `session_id` (run SSOT) |
| JSONL | `firmware_session_id` | 패킷 헤더 `session_id` 그대로 저장 |

저장 경로: `mac_collector_output/raw/YYYYMMDD/session_<session_id>/device_<device_id>.jsonl`

## 5) MVP 제약

- `payload_type`는 `1`만 허용
- 펌웨어는 최대 **64**개 진폭 전송 (`MAX_AMP_SAMPLES`). 유효 OFDM 톤 **52**개 선별·매핑은 PC 후처리([pipeline.md](../postprocessing/pipeline.md))
- MTU 안전: 패킷 1개 **512 bytes 이하** 권장

## 6) ESP32-S3 C 구조체

```c
#pragma pack(push, 1)
typedef struct {
    uint16_t magic;           // 0x4353
    uint8_t  version;         // 1
    uint8_t  header_len;      // 40
    uint8_t  payload_type;    // 1
    uint8_t  flags;           // 0
    uint16_t reserved0;       // 0
    uint32_t session_id;      // 0 (run ID on Mac)
    uint32_t device_id;
    uint32_t seq;
    uint64_t timestamp_us;
    uint8_t  channel;
    int8_t   rssi_dbm;
    int8_t   noise_floor_dbm;
    uint8_t  reserved1;
    uint16_t sample_count;
    uint16_t reserved2;
    uint32_t crc32;
} csi_udp_header_v1_t;
#pragma pack(pop)
```

## 7) Mac 수집기 파싱

Python `struct`: `"<HBBBBHIIIQbbbBHHI"` (40 bytes)

- `magic` / `version` / `header_len` / `payload_type` / 길이 검증
- `device_id`별 `seq` 누락 추정
- 수신 시각 `received_at_unix_us`를 JSONL 메타로 추가

관련: [collector.md](collector.md)
