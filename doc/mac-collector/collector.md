# Mac Collector

`mac_collector/udp_collector_mvp.py` — ESP32-S3 RX가 보낸 CSI UDP 패킷을 검증하고 JSONL로 저장합니다.

## 관련 파일

| 파일 | 용도 |
|------|------|
| [udp-packet-schema.md](udp-packet-schema.md) | 바이너리 UDP 규격 |
| `udp_collector_mvp.py` | 수집기 |
| `device_registry.csv` | RX 등록표 (SSOT) |
| `session_meta.yaml` | run `session_id` SSOT + 실험 조건 |

## 실행

프로젝트 루트에서:

```bash
python mac_collector/udp_collector_mvp.py \
  --host 0.0.0.0 \
  --port 9999 \
  --output-dir mac_collector_output \
  --device-registry-csv mac_collector/device_registry.csv \
  --session-meta mac_collector/session_meta.yaml
```

RX 대수만 명시할 때:

```bash
python mac_collector/udp_collector_mvp.py \
  --host 0.0.0.0 \
  --port 9999 \
  --output-dir mac_collector_output \
  --expected-device-ids "101,102,103" \
  --stale-sec 10
```

- **`--expected-device-ids` 우선**: 비어 있으면 `--device-registry-csv`의 **모든** `device_id`를 기대 목록으로 사용합니다.
- `session_meta.yaml`의 `devices.expected_device_ids`는 **수집기가 읽지 않음** (실험 기록·문서용). 수집 시 기대 RX를 제한하려면 CLI `--expected-device-ids "101,102"`를 사용하세요.
- `--session-meta`(기본 `mac_collector/session_meta.yaml`)의 **`session_id`로** `session_<id>/` 경로·JSONL `session_id` 결정.
- 수집 시작 시 해당 run 폴더에 `session_meta_snapshot.yaml` 복사.
- UDP v1 헤더의 `session_id` 필드는 펌웨어에서 **0** (레거시 펌웨어는 경고만).

## 저장 구조

```text
mac_collector_output/
  raw/
    YYYYMMDD/
      session_<session_id>/
        device_<device_id>.jsonl
```

JSONL 1줄(레코드) 주요 필드:

- `received_at_unix_us`
- `session_id` (yaml SSOT), `firmware_session_id` (패킷, 0), `device_id`, `seq`, `timestamp_us`
- `channel`, `rssi_dbm`, `noise_floor_dbm`
- `sample_count`, `csi_amp`

## 제공 기능 (MVP)

- 패킷 검증 (`magic` / `version` / `header_len` / `payload_type` / 길이)
- `device_id`별 `seq` 누락 추정
- 주기적 상태 로그 (패킷 수, drop 추정, 샘플 수)
- 기대 RX 대비 `missing_devices`, `stale_devices`
- **`--duration-sec N`**: N초 후 자동 종료 (`meshsense_cli` 수집기 메뉴에서도 시간 입력)

## CSI 워터폴 PNG (수집 종료 후)

`scripts/visualize_csi.py` — 세션 폴더의 `device_*.jsonl`을 100Hz 격자로 보간한 뒤 RX별 heatmap PNG 생성.

```bash
python scripts/visualize_csi.py --session-dir mac_collector_output/raw/YYYYMMDD/session_1
# 또는 최신 session_<id> 자동 검색
python scripts/visualize_csi.py --output-dir mac_collector_output --session-id 1
```

출력: `csi_waterfall.png` 1장 (RX `device_id`별 세로 서브플롯).  
`meshsense_cli` → **[3] 수집기만** 종료 시 위 스크립트를 자동 호출합니다.

## 장치 등록표 (SSOT)

`device_registry.csv`는 수집기·RX 플래시 스크립트가 공통으로 사용합니다.

1. `python scripts/device_registry.py add --port …` 로 등록하거나 `device_registry.csv` 직접 편집
2. 각 RX의 `device_id`, `sta_mac`, 설치 좌표 기록 — [scripts/README.md](../../scripts/README.md)
3. 수집 시 `--device-registry-csv` 사용 (권장)
4. 플래시: `python scripts/flash_rx.py -p <PORT>`

권장: 실험 기간 동안 `device_id` 고정, 좌표 단위 meter·원점 통일, 안테나 높이·방향 기록.

## 세션 메타 (run SSOT)

1. `mac_collector/session_meta.yaml` 작성·갱신
2. **`session_id`**: 실험 run 구분 — 바꾼 뒤 **수집기 재시작** (플래시 불필요)
3. `network:`·`experiment:`·`operator:` 등 실험 조건 기록
4. 수집: `python mac_collector/udp_collector_mvp.py ...` (`--session-meta` 기본값 있음)

망 플래시 SSOT는 `scripts/meshsense_config.json` (`ap`, `collector`만).  
`session_id`는 펌웨어에 없음.

### TODO: `session_meta.yaml` ↔ `meshsense_config.json` (`network:`)

실험 전 `network:` 블록을 config와 **수동 일치** (자동 동기화 미구현):

| meshsense_config.json | session_meta.yaml `network:` |
|----------------------|------------------------------|
| `ap.ssid` | `ssid` |
| `ap.channel` | `channel` |
| `collector.ip` | `collector_ip` |
| `collector.port` | `collector_port` |

## 향후 개선 (미구현)

- CRC32 검증 연동
- JSONL → Parquet 변환
- 세션 메타 필수 키 자동 검사
- `meshsense_config.json` → `session_meta.yaml` `network:` 자동 반영
