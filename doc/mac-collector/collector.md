# Mac Collector

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

`mac_collector/udp_collector_mvp.py` — **AP 실시간 파이프라인**에서 RX가 보낸 CSI UDP 패킷을
검증하고 JSONL로 저장합니다. (USB 파이프라인은 `scripts/csi_serial_reader.py`가 같은 레이아웃으로 저장 —
[usb-collection.md](../pipeline/usb-collection.md))

registry 2종과 `session_meta.yaml`은 **양 파이프라인 공용** 자산입니다.

## 관련 파일

| 파일 | 용도 |
|------|------|
| [udp-packet-schema.md](udp-packet-schema.md) | 바이너리 UDP 규격 (v2) |
| `udp_collector_mvp.py` | 수집기 |
| `device_registry.csv` / `tx_registry.csv` | RX / TX 등록표 (SSOT) |
| `session_meta.yaml` | run `session_id` SSOT + 실험 조건 |

## 실행

CLI: `python scripts/meshsense_cli.py` → `[2] AP 실시간 수집` → `[3] 수집기 실행`.
수동 실행 (프로젝트 루트):

```bash
python mac_collector/udp_collector_mvp.py \
  --host 0.0.0.0 \
  --port 9999 \
  --output-dir mac_collector_output \
  --device-registry-csv mac_collector/device_registry.csv \
  --session-meta mac_collector/session_meta.yaml
```

- **`--expected-device-ids "101,102"`** — 기대 RX를 제한. 비어 있으면 registry의 **모든**
  `device_id`를 기대 목록으로 사용 (`session_meta.yaml`의 `devices:` 블록은 기록용, 수집기가 읽지 않음)
- **`--session-meta`** 의 `session_id`로 `session_<id>/` 경로·JSONL `session_id` 결정
  (파싱은 `scripts/session_meta.py` 공용 구현). 수집 시작 시 run 폴더에
  `session_meta_snapshot.yaml` 복사
- **`--duration-sec N`** — N초 후 자동 종료
- UDP 헤더의 `session_id` 필드는 펌웨어에서 항상 0 (0이 아니면 1회 경고)

## 저장 구조

```text
mac_collector_output/
  raw/
    YYYYMMDD/
      session_<session_id>/
        device_<device_id>.jsonl
```

JSONL 1줄(레코드) 주요 필드: `received_at_unix_us` · `session_id`(yaml SSOT) ·
`firmware_session_id`(패킷, 0) · `device_id` · `seq` · `timestamp_us` ·
`tx_seq`(v2, 비 ESP-NOW 프레임이면 `null`) · `channel` · `rssi_dbm` · `noise_floor_dbm` ·
`sample_count` · `csi_amp`

## 제공 기능 (MVP)

- 패킷 검증 (`magic` / `version`(1·2) / `header_len` / `payload_type` / 길이)
- `device_id`별 `seq` 누락 추정 + 구간 Hz + `tx_seq` 유효 비율 출력
- 주기적 상태 로그 (패킷 수, drop 추정, 샘플 수)
- 기대 RX 대비 `missing_devices`, `stale_devices` (`--stale-sec`)

## CSI 워터폴 PNG (수집 종료 후)

`scripts/visualize_csi.py` — 세션의 `device_*.jsonl`을 100Hz 격자로 보간해 RX별 heatmap PNG 생성.

```bash
python scripts/visualize_csi.py --session-dir mac_collector_output/raw/YYYYMMDD/session_1
python scripts/visualize_csi.py --output-dir mac_collector_output --session-id 1   # 최신 자동 검색
```

CLI 수집기 메뉴 종료 시 `.venv/bin/python`으로 자동 호출하며, `.venv`가 없으면 생성을
안내합니다 (환경 준비: [quickstart.md](../overview/quickstart.md) §0).

## 장치 등록표 (SSOT)

`device_registry.csv`(RX)·`tx_registry.csv`(TX)는 수집기·플래시 스크립트·CLI가 공통 사용합니다.

```bash
python scripts/device_registry.py add --port /dev/cu.usbmodemXXXX --board-name RX4
python scripts/device_registry.py list
python scripts/tx_registry.py add --port /dev/cu.usbmodemXXXX --board-name TX1
```

권장: 실험 기간 동안 `device_id` 고정, 좌표 단위 meter·원점 통일, 안테나 높이·방향 기록.

## 세션 메타 (run SSOT)

1. run마다 `mac_collector/session_meta.yaml`의 **`session_id`** 갱신 → **수집기 재시작**
   (플래시 불필요 — `session_id`는 펌웨어에 없음)
2. `network:`·`experiment:`·`operator:` 등은 실험 조건 기록용 (코드가 소비하지 않음)

### TODO: `session_meta.yaml` ↔ `meshsense_config.json` (`network:`)

실험 전 `network:` 블록을 config와 **수동 일치** (자동 동기화 미구현):

| meshsense_config.json | session_meta.yaml `network:` |
|----------------------|------------------------------|
| `ap.ssid` | `ssid` |
| `ap.channel` | `channel` |
| `collector.ip` | `collector_ip` |
| `collector.port` | `collector_port` |

## 향후 개선 (미구현)

- JSONL → Parquet 변환
- 세션 메타 필수 키 자동 검사
- `meshsense_config.json` → `session_meta.yaml` `network:` 자동 반영
