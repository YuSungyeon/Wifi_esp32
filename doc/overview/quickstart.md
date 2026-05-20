# 빠른 시작

실험 순서: **TX 플래시 → Mac이 SoftAP 접속 → 수집기 → RX 플래시 → 후처리**.

터미널에서 단계별 안내가 필요하면:

```bash
python scripts/meshsense_cli.py
```

메뉴 **[1] 전체 가이드** 가 위 순서와 동일합니다. 아래는 수동 명령 참고용입니다.

## 0. 호스트 설정 (최초 1회)

```bash
git clone --recursive <repo-url>
cd Wifi_esp32
cp scripts/meshsense_config.example.json scripts/meshsense_config.json
# collector.ip = Mac on TX SoftAP (ipconfig getifaddr en0, often 192.168.4.2)

python scripts/idf_bootstrap.py -y   # esp-idf/ + .espressif/ (최초만 오래 걸림)
```

TX/RX 플래시·Wi-Fi·수집기 포트는 `meshsense_config.json`만 수정합니다.  
플래시 스크립트가 툴체인이 없으면 bootstrap을 자동 호출합니다. 상세: [scripts/README.md](../../scripts/README.md).

`mac_collector/session_meta.yaml`: **`session_id`**(run 구분, 수집기 SSOT) 및 `network:`를 config와 수동 일치 ([collector.md](../mac-collector/collector.md)).

## 1. TX/AP 노드

```bash
python scripts/tx_registry.py add --port /dev/cu.usbmodem101 --board-name TX1
python scripts/flash_tx.py -p /dev/cu.usbmodem101 --monitor
```

[tx-ap-node.md](../firmware/tx-ap-node.md)

## 2. Mac 네트워크·수집기

1. Mac Wi-Fi → TX SoftAP (`meshsense_config.json` → `ap.ssid`)  
2. 수집기 (`collector.port`와 CLI `--port` 일치):

```bash
python mac_collector/udp_collector_mvp.py \
  --host 0.0.0.0 --port 9999 \
  --output-dir mac_collector_output \
  --device-registry-csv mac_collector/device_registry.csv \
  --session-meta mac_collector/session_meta.yaml
```

## 3. RX 노드

```bash
python scripts/device_registry.py verify
python scripts/flash_rx.py -p /dev/cu.usbmodem102 --monitor
```

[rx-csi-sender.md](../firmware/rx-csi-sender.md)

## 4. 후처리

[`add/main.py`](../../add/main.py) 상단 `SESSION_DIR`·`RX_IDS`를 수집 경로에 맞게 수정한 뒤:

```bash
python add/main.py
```

[pipeline.md](../postprocessing/pipeline.md)
