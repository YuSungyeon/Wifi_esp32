# MeshSense 호스트 스크립트

`device_registry.csv`(RX) · `tx_registry.csv`(TX) · **`meshsense_config.json`**(망 설정 SSOT).

## 최초 설정

```bash
cp scripts/meshsense_config.example.json scripts/meshsense_config.json
# ap.pass, collector.ip 등 실험 환경에 맞게 수정 (Mac IP: ipconfig getifaddr en0 on TX SoftAP)
```

### 사전 요구사항

1. **ESP-IDF v5.x** — `export.sh`로 `IDF_PATH`·`idf.py` 활성화 (빌드·플래시)
2. **esptool** — USB로 **칩 MAC 읽기** (`flash_rx.py` / `flash_tx.py`, `registry add --port`)
   - `IDF_PATH`가 있으면 `idf.py -p PORT esptool read_mac` 사용
   - 없으면 PATH의 `esptool` / `esptool.py` 또는 `python -m esptool`
   - 래퍼: [`esptool_mac.py`](esptool_mac.py)

플래시 본체는 `idf.py flash`(ESP-IDF 내장 esptool)입니다. 별도 `esp_tool` 패키지는 없습니다.

## TX 플래시

```bash
python scripts/tx_registry.py add --port /dev/cu.usbmodem101 --board-name TX1
python scripts/flash_tx.py -p /dev/cu.usbmodem101 --monitor
```

## RX 플래시

```bash
python scripts/device_registry.py add --port /dev/cu.usbmodem102 --board-name RX1
python scripts/flash_rx.py -p /dev/cu.usbmodem102
```

## meshsense_config.json

| 블록 | 용도 |
|------|------|
| `ap.ssid` / `ap.pass` | TX SoftAP = RX STA 접속 Wi-Fi |
| `ap.channel`, `interval_ms`, … | TX 전용 |
| `collector.ip` / `collector.port` | RX → Mac 수집기 |

## Registry

| 대상 | 파일 | CLI |
|------|------|-----|
| RX | `mac_collector/device_registry.csv` | `python scripts/device_registry.py` |
| TX | `mac_collector/tx_registry.csv` | `python scripts/tx_registry.py` |

## TODO

- [ ] **`session_meta.yaml` `network:` 자동 동기화**: `meshsense_config.json`의 `ap`/`collector`를 `session_meta.yaml` `network:`에 반영 (run `session_id`는 yaml 전용). 상세: [collector.md](../doc/mac-collector/collector.md).

## 파일

| 파일 | 설명 |
|------|------|
| `meshsense_config.py` | 통합 설정 로드 |
| `meshsense_config.example.json` | 설정 템플릿 |
| `registry.py` / `tx_registry.py` | CSV 라이브러리 (+ TX CLI) |
| `esptool_mac.py` | esptool로 USB MAC 읽기 |
| `idf_util.py` | `idf.py` subprocess (`IDF_PATH` 필요) |
| `flash_rx.py` / `flash_tx.py` | registry 조회 → build·flash |
| `device_registry.py` | RX registry CLI |
