# UDP 패킷 스키마 (ESP32-S3 CSI → Mac)

> [!WARNING]
> **이 경로는 deprecated 입니다 (2026-08-25).** 실측 수집률이 0.18~22.8Hz 로 100Hz 목표에
> 못 미치고 `tx_seq` 유효 데이터가 한 건도 없습니다. 원인은 AP/STA association + DTIM
> 게이팅 + 자극·데이터 채널 공유라는 구조적 문제입니다
> ([csi-rate-troubleshooting.md](../overview/csi-rate-troubleshooting.md)).
> 추가로 RX 온디바이스 z-score 가 시간축 진폭 변동(=움직임 신호 본체)을 지워버려
> USB 경로 데이터와 스케일도 맞지 않습니다.
>
> - **학습 데이터 수집**: [USB 수집 파이프라인](../pipeline/usb-collection.md)
> - **실시간 경로**: ESP-NOW 업링크 + USB 싱크 보드로 재설계 예정
>   ([sprint/2026-08-collection-hardening.md](../sprint/2026-08-collection-hardening.md))
>
> 아래 내용은 기록용으로 남깁니다.

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
| version | `uint8` | 1 | 스키마 버전 (현재 `2`, 수집기는 `1`·`2` 모두 수용) |
| header_len | `uint8` | 1 | 고정 헤더 길이(현재 `40`) |
| payload_type | `uint8` | 1 | `1` = CSI amplitude |
| flags | `uint8` | 1 | bit0 = `tx_seq` 유효 (v2, ESP-NOW 프레임에서 추출됨). 나머지 예약 |
| reserved0 | `uint16` | 2 | 정렬용 예약값 |
| session_id | `uint32` | 4 | **reserved — 펌웨어는 항상 `0`**. run ID는 Mac `session_meta.yaml` SSOT |
| device_id | `uint32` | 4 | RX 장치 ID (`device_registry.csv`) |
| seq | `uint32` | 4 | 장치별 증가 시퀀스 |
| timestamp_us | `uint64` | 8 | ESP 측 타임스탬프(µs) |
| channel | `uint8` | 1 | Wi-Fi 채널 |
| rssi_dbm | `int8` | 1 | RSSI (dBm) |
| noise_floor_dbm | `int8` | 1 | 없으면 `-128` |
| reserved1 | `uint8` | 1 | 예약 |
| sample_count | `uint16` | 2 | `csi_amp` 샘플 개수 |
| reserved2 | `uint16` | 2 | 예약 |
| tx_seq | `uint32` | 4 | **v2**: TX ESP-NOW 송신 카운터 — 모든 RX 공통, cross-RX 동기화 키 (flags bit0=1일 때만 유효). **v1**: crc32 자리(항상 `0`) |

페이로드:

- `csi_amp`: `float32 * sample_count`

총 패킷 길이:

- `header_len + sample_count * 4`

## 4) tx_seq (v2 — cross-RX 동기화 키)

TX/AP 노드(`tx_ap_main.c`)는 ESP-NOW 프레임 페이로드 맨 앞에 `uint32_t g_enow_seq`를 실어
같은 라운드에 모든 RX에게 **동일한 값**을 보낸다. RX 펌웨어는 CSI 콜백의
`info->payload`에서 ESP-NOW vendor action frame 서명(category `0x7f` + Espressif OUI
`18:fe:34`)을 확인한 뒤 offset 15에서 이 값을 추출해 `tx_seq`에 채우고 flags bit0을 세운다.

- 비콘·UDP data 프레임 등 ESP-NOW가 아닌 프레임에서 유발된 CSI는 flags bit0=0, `tx_seq=0`
- 수집기는 flags bit0=1일 때만 JSONL `tx_seq`에 기록 (아니면 `null`)
- 후처리에서 `tx_seq`를 join key로 쓰면 여러 RX 보드의 CSI를 프레임 단위로 정렬 가능
  (USB 파이프라인 시리얼 프레임 v2의 `tx_seq`와 같은 의미 — [usb-collection.md](../pipeline/usb-collection.md))

## 5) session_id (펌웨어 vs Mac)

| 위치 | 필드 | 의미 |
|------|------|------|
| UDP 헤더 | `session_id` | **0 고정**. 레거시 펌웨어가 0이 아니면 수집기가 1회 경고 |
| JSONL | `session_id` | Mac `session_meta.yaml` 루트 `session_id` (run SSOT) |
| JSONL | `firmware_session_id` | 패킷 헤더 `session_id` 그대로 저장 |

저장 경로: `mac_collector_output/raw/YYYYMMDD/session_<session_id>/device_<device_id>.jsonl`

## 6) MVP 제약

- `payload_type`는 `1`만 허용
- 펌웨어는 최대 **64**개 진폭 전송 (`MAX_AMP_SAMPLES`). 유효 OFDM 톤 **52**개 선별·매핑은 PC 후처리([pipeline.md](../postprocessing/pipeline.md))
- MTU 안전: 패킷 1개 **512 bytes 이하** 권장

## 7) ESP32-S3 C 구조체

```c
#pragma pack(push, 1)
typedef struct {
    uint16_t magic;           // 0x4353
    uint8_t  version;         // 2
    uint8_t  header_len;      // 40
    uint8_t  payload_type;    // 1
    uint8_t  flags;           // bit0 = tx_seq valid
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
    uint32_t tx_seq;          // v1: crc32(=0) — v2: TX ESP-NOW 카운터
} csi_udp_header_t;
#pragma pack(pop)
```

## 8) Mac 수집기 파싱

Python `struct`: `"<HBBBBHIIIQbbbBHHI"` (40 bytes, v1·v2 동일)

- `magic` / `version`(1·2) / `header_len` / `payload_type` / 길이 검증
- `device_id`별 `seq` 누락 추정 + 구간 Hz + `tx_seq` 유효 비율 출력
- 수신 시각 `received_at_unix_us`를 JSONL 메타로 추가
- JSONL `tx_seq`: flags bit0=1이면 값, 아니면 `null`

관련: [collector.md](collector.md)
