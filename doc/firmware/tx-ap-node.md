# TX/AP 노드 펌웨어

`esp32s3_tx_ap_node` — MeshSense TX/AP 노드.

- SoftAP (`MeshSense_TX_AP` / `mstx1234` 기본 예시)
- UDP broadcast (기본 **10ms**, 포트 **3333**)

## 사전 준비

- ESP-IDF v5.x 및 `export.sh`
- `mac_collector/tx_registry.csv` — [scripts/README.md](../../scripts/README.md)
- `scripts/meshsense_config.json` (example 복사)

## 플래시 (권장)

```bash
cp scripts/meshsense_config.example.json scripts/meshsense_config.json

python scripts/tx_registry.py add --port /dev/cu.usbmodem101 --board-name TX1
python scripts/flash_tx.py -p /dev/cu.usbmodem101 --monitor
```

`meshsense_config.json`의 `ap.*` → TX CMake `TX_AP_*`. `tx_registry.csv` → `TX_AP_NODE_ID`.  
run `session_id`는 Mac `session_meta.yaml` (펌웨어 미사용).

## meshsense_config.json (망 SSOT)

| 키 | CMake |
|----|--------|
| `ap.ssid` / `ap.pass` | `TX_AP_SSID` / `TX_AP_PASS` |
| `ap.channel` | `TX_AP_CHANNEL` |
| `ap.broadcast_port`, `interval_ms`, … | TX UDP |

## RX와 맞출 것

동일 `meshsense_config.json`의 `ap.ssid` / `ap.pass`가 RX STA Wi-Fi(`CSI_WIFI_*`)로 플래시됩니다. 별도 파일 불필요.

## 수동 빌드 (고급)

```bash
cd esp32s3_tx_ap_node
idf.py set-target esp32s3
idf.py -DTX_AP_SSID="MeshSense_TX_AP" -DTX_AP_PASS="mstx1234" -DTX_AP_NODE_ID=1 build
idf.py -p /dev/tty.usbmodemXXXX flash monitor
```

## 트러블슈팅

- **registry에 MAC 없음**: `tx_registry.py add --port …`
- **RX 미접속**: `meshsense_config.json` `ap` 확인
- **IDF_PATH 없음**: `export.sh`
