# AP 실시간 수집 파이프라인 (SoftAP + UDP)

> [!WARNING]
> **이 경로는 deprecated 입니다 (2026-08-25).** 실측 수집률이 0.18~22.8Hz 로 100Hz 목표에
> 못 미치고 `tx_seq` 유효 데이터가 한 건도 없습니다. 원인은 AP/STA association + DTIM
> 게이팅 + 자극·데이터 채널 공유라는 구조적 문제입니다
> ([csi-rate-troubleshooting.md](../overview/csi-rate-troubleshooting.md)).
> 추가로 RX 온디바이스 z-score 가 시간축 진폭 변동(=움직임 신호 본체)을 지워버려
> USB 경로 데이터와 스케일도 맞지 않습니다.
>
> - **학습 데이터 수집**: [USB 수집 파이프라인](usb-collection.md)
> - **실시간 경로**: ESP-NOW 업링크 + USB 싱크 보드로 재설계 예정
>   ([sprint/2026-08-collection-hardening.md](../sprint/2026-08-collection-hardening.md))
>
> 아래 내용은 기록용으로 남깁니다.

TX가 SoftAP를 열고 RX들이 STA로 접속해 CSI를 UDP로 Mac 수집기에 실시간 전송합니다.

```text
TX/AP (esp32s3_tx_ap_node) ── ESP-NOW 10ms unicast ──▶ RX (esp32s3_csi_sender) × N
                                                        │ CSI 콜백 → 전처리 → UDP
Mac (Wi-Fi로 TX SoftAP 접속) ◀────────────────────── udp://collector.ip:9999
```

실행은 CLI 메뉴 **[2] AP 실시간 수집**:
`[1] 전체 가이드 · [2] 보드 플래시 · [3] 수집기 실행 · [4] 사전 점검 · [5] 보드 관리`

## TX/AP 노드 (`esp32s3_tx_ap_node`)

- **SoftAP** — `meshsense_config.json` `ap.ssid`/`ap.pass` (기본 `MeshSense_TX_AP`/`mstx1234`), 비콘 `beacon_interval_tu`(기본 100 TU)
- **ESP-NOW 10ms** (`espnow_interval_ms`) — CSI 100Hz 유도용 **유일한 자극원**.
  STA가 접속하면 unicast로 전환해 DTIM 게이팅을 우회하고, 페이로드에 `g_enow_seq`
  카운터(=UDP v2 `tx_seq`)를 실어 보냄
- 대역폭 HT20 고정 (HT40 secondary 채널에서 RX CSI 콜백 누락 문제 회피)

플래시:

```bash
python scripts/tx_registry.py add --port /dev/cu.usbmodem101 --board-name TX1
python scripts/flash_tx.py -p /dev/cu.usbmodem101 --monitor
```

`meshsense_config.json`의 `ap.*` → CMake `TX_AP_*` 주입. `tx_registry.csv`의 `tx_node_id`는
호스트 측 보드 식별용(플래시 상태 추적)이며 펌웨어에는 주입되지 않습니다.

## RX 노드 (`esp32s3_csi_sender`)

TX SoftAP에 STA로 접속해 CSI 콜백을 받고, 전처리 후 UDP로 전송합니다.

- **CSI 샘플링** — `SEND_INTERVAL_US=9000` (9ms 상한 = 100Hz + jitter 허용).
  콜백은 큐에 넣고 워커 태스크가 전처리·`sendto` 수행 (콜백 블로킹 방지).
  `WIFI_PS_NONE`, `listen_interval=1`
- **전처리** — 이동평균(3-tap) → z-score → 이상치 클리핑(±3σ)
- **tx_seq 추출** — ESP-NOW 프레임(category `0x7f` + OUI `18:fe:34`)에서 TX 카운터를
  추출해 UDP 헤더 `tx_seq`에 탑재 (cross-RX 동기화 키, [udp-packet-schema.md](../mac-collector/udp-packet-schema.md))
- **ESP-NOW 전용 필터** — `rx.espnow_only: true` (CMake `CSI_ESPNOW_ONLY=1`, 기본 0)이면
  ESP-NOW 프레임 CSI만 전송해 데이터 균질성·tx_seq 커버리지 확보.
  걸러진 수는 5초 로그 `eo_drop`으로 확인

플래시 (USB MAC → `device_registry.csv` → `CSI_DEVICE_ID` 주입):

```bash
python scripts/device_registry.py verify
python scripts/flash_rx.py -p /dev/cu.usbmodemXXXX --monitor
# 보드 전환 시: --clean -y
```

## Mac 네트워크·수집기

1. Mac Wi-Fi를 TX SoftAP(`ap.ssid`)에 접속하고 IP 확인: `ipconfig getifaddr en0`
   (보통 `192.168.4.2` — `meshsense_config.json` `collector.ip`와 일치해야 RX가 도달)
2. 수집기 실행 — CLI `[3] 수집기 실행` 또는 [collector.md](../mac-collector/collector.md)의 수동 명령

## 수동 빌드 (고급)

```bash
cd esp32s3_tx_ap_node   # 또는 esp32s3_csi_sender
idf.py set-target esp32s3
idf.py -DTX_AP_SSID="MeshSense_TX_AP" -DTX_AP_PASS="mstx1234" build          # TX
idf.py -DCSI_WIFI_SSID="MeshSense_TX_AP" -DCSI_WIFI_PASS="mstx1234" \
       -DCSI_COLLECTOR_IP="192.168.4.2" -DCSI_DEVICE_ID=101 build            # RX
idf.py -p /dev/cu.usbmodemXXXX flash monitor
```

## 진단

- Hz 확인: `python scripts/measure_csi_hz.py mac_collector_output/raw/.../session_<id>`
- 100Hz 미달 이력·원인 분석: [csi-rate-troubleshooting.md](../overview/csi-rate-troubleshooting.md)
- registry에 MAC 없음 → `tx_registry.py add` / `device_registry.py add`
- 수집기에 패킷이 안 옴 → `collector.ip`(Mac IP 변동 주의)·포트·Mac Wi-Fi 접속 확인
