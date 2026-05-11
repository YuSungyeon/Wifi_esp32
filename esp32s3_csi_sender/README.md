# ESP32-S3 CSI UDP Sender (MeshSense MVP)

MeshSense: ESP32-S3에서 CSI를 수집하고 1차 전처리(이동평균 + z-score + 이상치 클리핑) 후 Mac 수집기로 UDP 전송하는 ESP-IDF 프로젝트입니다.

## 1) 사전 준비

- ESP-IDF v5.x 설치 및 환경 로드
- Mac 수집기 실행 준비:
  - `python "mac_collector/udp_collector_mvp.py" --host 0.0.0.0 --port 9999 --output-dir "mac_collector_output"`

## 2) 설정 방법 (권장)

코드를 직접 수정하지 말고, 빌드 시 `-D` 파라미터로 값을 주는 방식을 권장합니다.

주요 파라미터:

- `CSI_WIFI_SSID`
- `CSI_WIFI_PASS`
- `CSI_COLLECTOR_IP`
- `CSI_COLLECTOR_PORT` (기본 9999)
- `CSI_SESSION_ID`
- `CSI_DEVICE_ID` (RX 구분용; `0`이면 MAC 기반 자동 ID)

예시:

```bash
idf.py -DCSI_WIFI_SSID="my-ap" -DCSI_WIFI_PASS="my-pass" -DCSI_COLLECTOR_IP="192.168.0.10" -DCSI_DEVICE_ID=101 build
```

## 3) 필수 수정값(대체 방식)

파일: `esp32s3_csi_sender/main/csi_sender_main.c`

아래 매크로를 본인 환경으로 수정하세요.

- `WIFI_SSID`
- `WIFI_PASS`
- `COLLECTOR_IP` (MacBook IP)
- `COLLECTOR_PORT` (기본 9999)
- `SESSION_ID`, `DEVICE_ID`

`DEVICE_ID`는 보드마다 다르게 설정해야 합니다.
- 예: RX1 = 101, RX2 = 102
- `DEVICE_ID=0`으로 두면 펌웨어가 STA MAC에서 자동으로 ID를 생성합니다.

## 4) 빌드/플래시 방법

프로젝트 루트가 아니라 `esp32s3_csi_sender` 폴더에서 실행합니다.

```bash
cd "esp32s3_csi_sender"
idf.py set-target esp32s3
idf.py -DCSI_WIFI_SSID="my-ap" -DCSI_WIFI_PASS="my-pass" -DCSI_COLLECTOR_IP="192.168.0.10" -DCSI_DEVICE_ID=101 build
idf.py -p /dev/tty.usbmodemXXXX flash monitor
```

포트 확인 예시(macOS):

```bash
ls /dev/tty.usbmodem*
```

## 5) RX1 / RX2 빠른 명령 예시

RX1:

```bash
idf.py set-target esp32s3
idf.py -DCSI_WIFI_SSID="my-ap" -DCSI_WIFI_PASS="my-pass" -DCSI_COLLECTOR_IP="192.168.0.10" -DCSI_SESSION_ID=1 -DCSI_DEVICE_ID=101 build
idf.py -p /dev/tty.usbmodemRX1 flash monitor
```

RX2:

```bash
idf.py set-target esp32s3
idf.py -DCSI_WIFI_SSID="my-ap" -DCSI_WIFI_PASS="my-pass" -DCSI_COLLECTOR_IP="192.168.0.10" -DCSI_SESSION_ID=1 -DCSI_DEVICE_ID=102 build
idf.py -p /dev/tty.usbmodemRX2 flash monitor
```

참고: 같은 빌드 디렉터리를 재사용하면 이전 `-D` 값이 캐시될 수 있으니, 보드 전환 시 `idf.py fullclean` 후 빌드하는 것을 권장합니다.

## 6) RX 개수 확장 가이드 (N대)

- 장치 추가 시 `CSI_DEVICE_ID`만 새로운 값으로 지정하면 동일 펌웨어를 그대로 사용 가능합니다.
- 권장 ID 정책:
  - 고정 ID: `101, 102, 103, ...`
  - 자동 ID: `CSI_DEVICE_ID=0` (MAC 기반, 보드 교체 시에도 충돌 위험 낮음)
- 운영 체크:
  - Mac 수집기 실행 시 `--expected-device-ids`에 모든 RX ID를 등록
  - 예: `--expected-device-ids "101,102,103,104"`
- 세션 운영:
  - 같은 실험 묶음은 동일 `CSI_SESSION_ID` 유지
  - 장치 추가/제거 시 세션 메타 파일에 변경 이력 기록

## 7) 동작 확인

1. Mac에서 수집기 실행
2. ESP32-S3 보드 플래시/모니터 실행
3. 수집기 로그에서 아래를 확인
   - `device=<id> packets=...`
   - `drop_rate=...`
4. 결과 파일 확인
   - `mac_collector_output/raw/YYYYMMDD/session_<id>/device_<id>.jsonl`

## 8) 패킷 스키마 정합성

전송 패킷은 `mac_collector/UDP_패킷_스키마.md` 기준을 따릅니다.

- 헤더: 40 bytes
- payload: `float32 csi_amp[sample_count]`
- `magic=0x4353`, `version=1`, `payload_type=1`

## 9) 트러블슈팅

- 수집기에서 invalid packet이 계속 발생:
  - `header_len`/`sample_count`/전체 길이 계산 확인
  - `COLLECTOR_IP`와 `COLLECTOR_PORT` 재확인
- 패킷이 아예 안 들어오는 경우:
  - ESP와 Mac이 같은 네트워크인지 확인
  - macOS 방화벽 또는 AP 격리 기능 확인
  - `idf.py monitor`에서 Wi-Fi 연결 로그 확인

## 10) 다음 개선 항목

- CRC32 계산/검증 활성화
- 재전송(ACK) 또는 TCP 모드 추가
- `sdkconfig.defaults` 추가로 설정 자동화
