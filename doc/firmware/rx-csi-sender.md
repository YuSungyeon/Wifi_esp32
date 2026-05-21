# RX CSI Sender 펌웨어

`esp32s3_csi_sender` — CSI 수집·1차 전처리·Mac 수집기로 UDP 전송.

## 사전 준비

- ESP-IDF v5.x, TX SoftAP 동작
- `device_registry.csv`, `meshsense_config.json` — [scripts/README.md](../../scripts/README.md)
- Mac 수집기 ([collector.md](../mac-collector/collector.md))

## 플래시 (권장)

```bash
cp scripts/meshsense_config.example.json scripts/meshsense_config.json

python scripts/device_registry.py verify
python scripts/flash_rx.py -p /dev/cu.usbmodemXXXX
python scripts/flash_rx.py -p /dev/cu.usbmodemXXXX --clean --monitor -y
```

USB MAC → `device_registry.csv` → `CSI_DEVICE_ID`.  
`meshsense_config.json` → `CSI_WIFI_*`(=`ap.ssid/pass`), `CSI_COLLECTOR_*`. run `session_id`는 Mac `session_meta.yaml`.

## device_registry.csv (RX SSOT)

```bash
python scripts/device_registry.py add --port /dev/cu.usbmodemXXXX --board-name RX4
python scripts/device_registry.py list
```

MeshSense에서는 `CSI_DEVICE_ID=0`(MAC 자동 ID) 사용 안 함.

## meshsense_config.json

| 키 | 용도 |
|----|------|
| `ap.ssid` / `ap.pass` | TX SoftAP에 STA 접속 |
| `collector.ip` / `collector.port` | UDP 수집기 |

**TODO:** `session_meta.yaml` `network:`와 수동 동기화 — [collector.md](../mac-collector/collector.md).

## 수동 빌드 (고급)

```bash
cd esp32s3_csi_sender
idf.py set-target esp32s3
idf.py -DCSI_WIFI_SSID="MeshSense_TX_AP" -DCSI_WIFI_PASS="mstx1234" \
  -DCSI_COLLECTOR_IP="192.168.4.2" -DCSI_DEVICE_ID=101 build
idf.py -p /dev/tty.usbmodemXXXX flash monitor
```

## CSI 샘플링 (목표 100Hz)

- RX `SEND_INTERVAL_US` = **10ms** — 최대 100Hz UDP.
- CSI 콜백은 **큐+워커**로 전처리·`sendto` 분리 (콜백 블로킹·gap 완화).
- STA `WIFI_PS_NONE`, `listen_interval=1`.
- TX **ESP-NOW 10ms** (`espnow_interval_ms`) + 비콘 **100 TU** (`beacon_interval_tu`).

Hz 확인: `python scripts/measure_csi_hz.py mac_collector_output/raw/.../session_<id>`

## 트러블슈팅

- **registry에 MAC 없음**: `device_registry.py add`
- **invalid packet**: `collector.ip`, 포트
- **IDF_PATH 없음**: `export.sh`
