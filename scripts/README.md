# MeshSense 호스트 스크립트

`device_registry.csv`(RX) · `tx_registry.csv`(TX) · **`meshsense_config.json`**(망 설정 SSOT).

## 최초 설정

```bash
git clone --recursive <repo-url>   # esp-idf 서브모듈 포함
cd Wifi_esp32
cp scripts/meshsense_config.example.json scripts/meshsense_config.json
# ap.pass, collector.ip 등 (Mac on TX SoftAP: ipconfig getifaddr en0)

# ESP-IDF 툴체인 (최초 1회, 10–30분·수 GB)
python scripts/idf_bootstrap.py -y
```

이미 clone 한 경우: `git submodule update --init esp-idf`

### ESP-IDF (프로젝트 로컬)

| 경로 | 설명 |
|------|------|
| `esp-idf/` | git submodule 또는 bootstrap clone (`v5.2.4`) |
| `~/.espressif/` | 툴체인·Python venv (ESP-IDF 기본, 전역) |

`flash_rx.py` / `flash_tx.py`는 실행 시 `idf_bootstrap`으로 위 경로를 준비한 뒤 빌드·플래시합니다.  
전역 `~/esp/esp-idf`만 쓰려면 `--skip-idf-bootstrap` (기존 `export.sh` 필요).

수동: `python scripts/idf_bootstrap.py` · `MESHESENSE_IDF_PATH=/path/to/esp-idf`

### 기타

- **esptool** — USB MAC 읽기: [`esptool_mac.py`](esptool_mac.py) (`pip install esptool` 또는 IDF venv)
- Mac 수집기·후처리는 ESP-IDF **불필요**

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
| `idf_bootstrap.py` | submodule + `install.sh esp32s3` → `.espressif/` |
| `idf_env.py` / `idf_paths.py` | `export.sh` 환경·경로 |
| `esptool_mac.py` | esptool로 USB MAC 읽기 |
| `idf_util.py` | `idf.py` subprocess |
| `flash_rx.py` / `flash_tx.py` | bootstrap → registry → build·flash |
| `device_registry.py` | RX registry CLI |
