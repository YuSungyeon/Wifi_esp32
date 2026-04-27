# UDP 패킷 스키마 (ESP32-S3 CSI -> MacBook)

본 문서는 ESP32-S3 RX 노드가 Mac 수집기로 보내는 **MVP 바이너리 UDP 패킷** 규격입니다.

## 1) 설계 목표

- 구현 단순성(ESP/파이썬 모두 쉽게 파싱)
- 최소 메타데이터 + `csi_amp[]` 전송
- 패킷 길이 검증으로 잘못된 프레임 조기 탐지

## 2) Endianness 및 타입

- Endianness: **Little-endian**
- 정수 타입: unsigned/signed는 C 표준 고정폭 타입 기준
- 실수 타입: IEEE754 `float32`

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
| session_id | `uint32` | 4 | 세션 식별자 |
| device_id | `uint32` | 4 | 장치 식별자 (RX1/RX2 구분) |
| seq | `uint32` | 4 | 장치별 증가 시퀀스 번호 |
| timestamp_us | `uint64` | 8 | ESP 측 타임스탬프(마이크로초) |
| channel | `uint8` | 1 | Wi-Fi 채널 |
| rssi_dbm | `int8` | 1 | RSSI (dBm) |
| noise_floor_dbm | `int8` | 1 | 노이즈 플로어(없으면 `-128`) |
| reserved1 | `uint8` | 1 | 예약 |
| sample_count | `uint16` | 2 | amplitude 샘플 개수 |
| reserved2 | `uint16` | 2 | 예약 |
| crc32 | `uint32` | 4 | 헤더+페이로드 CRC32(옵션; MVP에서는 0 허용) |

페이로드:
- `csi_amp`: `float32 * sample_count`

총 패킷 길이:
- `header_len + sample_count * 4`

## 4) MVP 제약

- `payload_type`는 현재 `1`만 허용
- 권장 `sample_count`는 52 (기존 학습 파이프라인과 호환)
- MTU 안전을 위해 패킷 1개 길이 512 bytes 이하 권장

## 5) ESP32-S3 측 C 구조체 예시

```c
#pragma pack(push, 1)
typedef struct {
    uint16_t magic;           // 0x4353
    uint8_t  version;         // 1
    uint8_t  header_len;      // 40
    uint8_t  payload_type;    // 1
    uint8_t  flags;           // 0
    uint16_t reserved0;       // 0
    uint32_t session_id;
    uint32_t device_id;
    uint32_t seq;
    uint64_t timestamp_us;
    uint8_t  channel;
    int8_t   rssi_dbm;
    int8_t   noise_floor_dbm;
    uint8_t  reserved1;       // 0
    uint16_t sample_count;
    uint16_t reserved2;       // 0
    uint32_t crc32;           // optional
} csi_udp_header_v1_t;
#pragma pack(pop)
```

`sendto()` 시에는 위 헤더 뒤에 `float amp[sample_count]`를 메모리 복사하여 전송합니다.

## 6) Mac 수집기 파싱 포인트

- `magic/version/header_len` 1차 검증
- `sample_count`로 총 길이 계산 후 불일치 패킷 drop
- `device_id`별로 `seq` 누락량 계산
- 수신 시각(`received_at_unix_us`)을 별도 메타로 기록
