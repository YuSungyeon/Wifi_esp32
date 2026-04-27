# TX/AP 노드 적용 가이드 (ESP32-S3)

이 문서는 `esp32s3_tx_ap_node` 펌웨어를 처음 적용하는 사람이 그대로 따라해서 동작 확인까지 할 수 있도록 작성되었습니다.

## 1) 이 펌웨어가 하는 일

`esp32s3_tx_ap_node`는 아래 2가지를 수행합니다.

1. **SoftAP 생성**
   - SSID/채널을 고정한 AP를 띄워 RX 노드가 동일 채널에서 CSI를 안정적으로 받게 함
2. **주기적 UDP Broadcast 송신**
   - `192.168.4.255:<port>`로 heartbeat 패킷을 지속 송신하여 무선 트래픽을 공급

즉, 기존 공유기/AP 없이도 TX/AP 역할을 ESP32 한 대로 대체할 수 있습니다.

---

## 2) 프로젝트 위치

- TX/AP 펌웨어: `esp32s3_tx_ap_node`
- RX 펌웨어: `esp32s3_csi_sender`
- Mac 수집기: `mac_collector/udp_collector_mvp.py`

---

## 3) 사전 준비

- ESP-IDF v5.x 설치
- USB 케이블로 ESP32-S3 연결
- macOS에서 포트 확인:

```bash
ls /dev/tty.usbmodem*
```

---

## 4) 설정값(빌드 파라미터)

`esp32s3_tx_ap_node/CMakeLists.txt`의 캐시 변수로 설정하거나, `idf.py -D...`로 직접 주입합니다.

주요 파라미터:

- `TX_AP_SSID` (기본 `WiSLAR_TX_AP`)
- `TX_AP_PASS` (기본 `wislartx123`, 8자 이상 권장)
- `TX_AP_CHANNEL` (기본 `6`)
- `TX_AP_MAX_CONN` (기본 `4`)
- `TX_AP_BROADCAST_PORT` (기본 `3333`)
- `TX_AP_INTERVAL_MS` (기본 `20`)
- `TX_AP_PAYLOAD_BYTES` (기본 `64`)
- `TX_AP_SESSION_ID` (기본 `1`)
- `TX_AP_NODE_ID` (기본 `1`)

권장 운영:
- RX 노드와 TX/AP 노드의 채널을 동일하게 유지
- 세션마다 `TX_AP_SESSION_ID`를 명시적으로 증가

---

## 5) 빌드/플래시

프로젝트 디렉터리로 이동 후 실행:

```bash
cd "esp32s3_tx_ap_node"
idf.py set-target esp32s3
idf.py \
  -DTX_AP_SSID="WiSLAR_TX_AP" \
  -DTX_AP_PASS="wislartx123" \
  -DTX_AP_CHANNEL=6 \
  -DTX_AP_BROADCAST_PORT=3333 \
  -DTX_AP_INTERVAL_MS=20 \
  -DTX_AP_SESSION_ID=1 \
  -DTX_AP_NODE_ID=1 \
  build
idf.py -p /dev/tty.usbmodemXXXX flash monitor
```

---

## 6) RX + Mac과 함께 붙이는 순서

1. **Mac 수집기 실행**
   - `mac_collector/device_registry.csv`
   - `mac_collector/session_meta.yaml`
   를 최신 값으로 채운 뒤 실행

2. **TX/AP 노드 실행**
   - SoftAP 생성 로그 확인
   - heartbeat 송신 로그 확인(`seq` 증가)

3. **RX 노드 실행**
   - RX 펌웨어를 각각 다른 `CSI_DEVICE_ID`로 플래시
   - RX가 TX/AP 채널에서 CSI 송신하는지 확인

4. **수집기 상태 확인**
   - `missing_devices`가 비어있는지 확인
   - `stale_devices`가 비어있는지 확인
   - `drop_rate` 허용범위 확인

---

## 7) 성공 기준(현장 체크)

- TX/AP 모니터에 `SoftAP started ...` 로그 출력
- TX/AP 모니터에 `tx heartbeat seq=...` 로그가 주기적으로 증가
- Mac 수집기에서 RX 장치별 packet 카운트 증가
- 세션 폴더 생성:
  - `mac_collector_output/raw/YYYYMMDD/session_<id>/device_<id>.jsonl`
  - `session_meta_snapshot.yaml`

---

## 8) 자주 발생하는 문제와 해결

## 문제 A: RX 데이터가 수집기에 안 들어옴

- RX와 TX/AP 채널 불일치 확인
- RX 빌드 시 `CSI_COLLECTOR_IP` 확인
- TX/AP와 RX가 같은 공간/거리에서 동작하는지 확인

## 문제 B: AP 연결은 되는데 품질이 불안정함

- `TX_AP_INTERVAL_MS`를 20 -> 30/40으로 늘려 부하 완화
- 2.4GHz 간섭이 적은 채널로 변경
- 안테나 방향/높이 고정

## 문제 C: 여러 실험에서 설정이 섞임

- 세션마다 `TX_AP_SESSION_ID` 증가
- `session_meta.yaml` 세션 정보 갱신 후 실행
- `중요변경_문서화_로그.md`에 변경점 즉시 기록

---

## 9) 운영 권장사항

- TX/AP 1대는 전용 전원/위치로 고정
- RX 장치 추가 시 `device_registry.csv`를 먼저 갱신
- 실험 시작 전 `실장비_값_입력_체크리스트.md`를 체크
- 구조/운영 변경은 반드시 `중요변경_문서화_로그.md`에 기록

---

## 10) 현재 구현 범위와 향후 확장

현재 TX/AP 노드는 **SoftAP + UDP broadcast 트래픽 생성**까지 구현되었습니다.

향후 확장(선택):
- TX heartbeat를 Mac 수집기에서도 별도 수집/동기화
- 고정 길이/주기의 raw 802.11 frame 송신 모드 추가
- TX/AP 원격 제어(시작/정지/세션 변경) API 추가
