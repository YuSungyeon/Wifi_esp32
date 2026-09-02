# CSI USB binary와 JSONL 계약

> 상태: **CURRENT CONTRACT**
> producer: `esp32s3_csi_recv_poc/main/app_main.c`
> consumer: `scripts/csi_serial_reader.py` → `scripts/csi_store.py` → `scripts/export_jsonl.py`

## 1. Binary frame v4

- Endianness: little-endian
- Header: packed **44 bytes**
- Payload: signed int8 raw CSI (I/Q 교차) 또는 IDENT payload
- CRC: **CRC-32 (zlib 호환)** — 헤더의 `crc32` 를 0으로 둔 상태의 헤더+payload 전체

```text
[44-byte header][payload[raw_len]]
```

| offset | type | field | 계약 |
|---:|---|---|---|
| 0 | `uint16` | `magic` | `0x4353` |
| 2 | `uint8` | `version` | `4` |
| 3 | `uint8` | `frame_type` | `0`=CSI, `1`=IDENT |
| 4 | `uint16` | `total_len` | `44 + raw_len` |
| 6 | `uint16` | `raw_len` | CSI=`128` (HT20 LLTF 64 SC × I/Q), IDENT=`32` |
| 8 | `uint32` | `seq` | RX 부팅부터 단조 증가 (보드별 독립) |
| 12 | `uint64` | `timestamp_us` | RX `esp_timer_get_time()` |
| 20 | `int8` | `rssi` | dBm |
| 21 | `uint8` | `channel` | `11` 고정 |
| 22 | `int8` | `noise_floor` | dBm |
| 23 | `uint8` | `rate` | `rx_ctrl->rate` |
| 24 | `uint16` | `sig_len` | |
| 26 | `uint16` | `boot_id` | 부팅마다 새 값 — 재부팅과 `seq` 되감김을 구분 |
| 28 | `uint32` | `tx_seq` | TX 공통 sequence (cross-RX 정렬 키) |
| 32 | `uint8` | `agc_gain` | AGC gain 원값 |
| 33 | `int8` | `fft_gain` | FFT gain 원값 |
| 34 | `uint16` | `rx_id` | 업링크 모드: `CSI_RX_ID`. 싱크가 여러 RX 프레임을 한 스트림으로 넘길 때 host 가 이걸로 device 를 가른다. USB 직결은 `0` |
| 36 | `float32` | `gain_comp` | 진폭 gain 보정 배율. `0`=baseline 미완성(첫 100패킷) |
| 40 | `uint32` | `crc32` | 위 참조 |

### CRC 가 필요한 이유

v2 는 magic 2바이트와 `raw_len` 상한만 검사했다. raw CSI 바이트 안에 우연히
`0x53 0x43` 이 나타나면 프레임 헤더로 오인되는데, 실제로 그렇게 저장된 레코드가 있다
(`20260523/session_1` 첫 줄: `channel=159`, `rssi=+11dBm`, `timestamp_us` 가 u64 초과).
길이·범위 검사만으로는 막을 수 없다.

### IDENT frame (`frame_type=1`)

부팅 직후와 2초마다 송출한다. payload 32바이트:

| offset | 크기 | 내용 |
|---:|---:|---|
| 0 | 6 | eFuse base MAC (`esp_efuse_mac_get_default`) |
| 6 | 10 | 펌웨어 식별 문자열 |
| 16 | 4×4 | `csi_cb`, `uart_sent`, `ringbuf_drop`, `uart_partial` (u32) |

MAC 은 `device_registry.csv` 의 `sta_mac` 조회에 쓴다 — host 가 esptool 로 포트를
프로브(=보드 리셋)하지 않고 보드를 식별할 수 있다.
카운터를 프레임에 실은 이유는 console primary 가 GPIO43 UART 라 `ESP_LOG` 가 USB 로
나오지 않기 때문이다 (17초 캡처에 로그 0건).

## 2. `.csi` 세션 저장소

Reader 는 프레임을 **변환 없이 그대로** 이어붙인다. 저장 단계에 변환이 없어 변환 버그가
낄 자리가 없고, 위상(raw I/Q)이 보존된다.

```text
mac_collector_output/raw/<YYYYMMDD>/<HHMMSS>_<label>_s<session_id>/
    device_<device_id>.csi        44B 헤더 + raw I/Q 프레임의 연속
    session.json                  세션 manifest (라벨 SSOT)
    session_meta_snapshot.yaml    수집 시점 실험 조건
```

- 디렉터리 이름에 수집 시각이 들어가 **충돌이 불가능**하다. `.csi` 는 배타적 생성(`open("xb")`)
  이라, 충돌하면 append 가 아니라 즉시 에러다. v2 의 `session_<id>` + append 조합은 실제로
  데이터를 오염시켰다 — `20260615/session_21` 은 100번째 줄에서 `seq 158→0` 으로 되감기고,
  `20260523/session_15` 는 `sample_count` 192 와 128 이 한 파일에 있다.
- `session_id` 는 기존 세션 최댓값+1 로 자동 부여한다 (`csi_session.next_session_id`).
- Python 정본은 `scripts/csi_store.py` 의 `HEADER_DTYPE`. C 쪽에 `offsetof` static assert 가
  걸려 있어 한쪽만 고치면 빌드가 깨진다.

### `session.json`

```json
{ "schema": 1, "pipeline": "usb", "frame_version": 4,
  "session_id": 22, "label": "static",
  "started_at_unix_us": 1787670006419848, "ended_at_unix_us": 1787670069912877,
  "devices": [ {"device_id": 103, "board_name": "RX103", "sta_mac": "E8:F6:0A:8A:D7:E8",
                "port": "/dev/cu.usbmodem1101", "boot_id": 17010, "frames": 6003,
                "span_s": 59.541, "crc_fail": 0, "invalid": 0, "resync": 0, "seq_gap": 0,
                "boot_changes": 0, "tx_back": 0, "first_tx_seq": 62268, "last_tx_seq": 68281,
                "fw_csi_cb": 5921, "fw_uart_sent": 6311, "fw_ringbuf_drop": 0,
                "fw_uart_partial": 0, "exit_code": 0} ] }
```

`provenance` 는 수집에 쓰인 코드의 신원이다 (`git_commit`, `git_branch`, `git_dirty`).
"이 데이터는 어느 코드로 찍었나"를 사후에 재구성할 방법이 없어 수집 시점에 남긴다.
`git_dirty=true` 면 커밋되지 않은 변경이 섞인 상태라 재현이 보장되지 않는다.

**라벨은 수집 시점에 여기 박힌다.** `label` 은 `empty` / `static` / `motion` 중 하나이며
`model_train/preprocessing/preprocess_3rx.py` 의 `LABEL_MAP` 과 같은 어휘여야 한다
(producer 는 `scripts/csi_store.py` 의 `LABELS`).

## 3. JSONL record schema v1 (전처리 입력)

학습 전처리(`model_train/preprocessing/preprocess_3rx.py`)가 소비하는 형식이다.
수집은 `.csi` 로 하고, `scripts/export_jsonl.py` 가 이 형식으로 내보낸다.

```bash
python scripts/export_jsonl.py --print-labels
```

`--print-labels` 는 세션 manifest 의 라벨로 `LABEL_SESSION_RANGES` 에
넣을 배정을 출력한다 — session_id 를 손으로 맞출 필요가 없다.

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

## 4. JSONL 출력 경로

```text
mac_collector_output/jsonl/raw/YYYYMMDD/
  session_<session_id>/device_<device_id>.jsonl
```

전처리가 기대하는 구 레이아웃을 그대로 따른다. 내보내기는 덮어쓰기이며 append 하지 않는다.
날짜 폴더마다 `labels.json`(session_id → label)을 함께 만든다 — 전처리가 이것을 라벨
정본으로 읽으므로 `LABEL_SESSION_RANGES` 를 손으로 맞출 필요가 없다.
정본 데이터는 `.csi` 세션이고 JSONL 은 파생물이므로, 언제든 다시 만들 수 있다.

`csi_amp` 는 64개를 모두 낸다. LLTF 64 SC 중 인덱스 `0`(DC)과 `27~37`(가드)은 **상시 0**이라
(실보드 확인) 유효 데이터 톤은 `[1..26] + [38..63]` 52개다 — `scripts/csi_store.py` 의
`LLTF_DATA_IDX`. 소비자가 64개를 그대로 쓰면 feature 의 19%가 상수 0이 된다.

## 5. Contract 변경 절차

Binary 또는 JSONL field를 변경할 때:

1. 이 문서의 version과 layout을 먼저 수정한다.
2. RX producer와 Python consumer를 같은 변경에서 수정한다.
3. header size, valid frame, invalid version/length test를 실행한다.
4. preprocessing과 visualization consumer 영향을 확인한다.
5. architecture와 firmware 문서 링크가 유효한지 검사한다.
