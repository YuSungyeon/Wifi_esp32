# ESP-NOW CSI/USB 펌웨어

> 상태: **CURRENT**
> `*_poc` 디렉터리 이름은 유지하지만 이 두 프로젝트가 현재 공식 firmware다.

## 1. 프로젝트

| 역할 | 프로젝트 | source |
|---|---|---|
| TX | `esp32s3_csi_send_poc` | [`main/app_main.c`](../esp32s3_csi_send_poc/main/app_main.c) |
| RX | `esp32s3_csi_recv_poc` | [`main/app_main.c`](../esp32s3_csi_recv_poc/main/app_main.c) |

두 firmware는 AP/STA association이나 IP network를 만들지 않는다. 양쪽 모두 STA mode에서 channel 11을 고정하고 ESP-NOW frame으로 CSI를 유도한다.

## 2. 공통 RF 설정

| 항목 | ESP32-S3 현재 값 |
|---|---|
| Wi-Fi mode | STA |
| association | 없음 |
| synthetic STA MAC | `1a:00:00:00:00:00` |
| channel | 11 |
| bandwidth | HT20 |
| secondary channel | none |
| ESP-NOW phymode | HT20 |
| ESP-NOW rate | MCS0 LGI |
| encryption | false |
| power save | disabled |

TX와 RX 설정은 함께 변경해야 한다. channel/bandwidth/rate 중 하나만 바꾸면 frame 수신과 CSI shape가 달라질 수 있다.

### 2.1 `STA` mode이지만 association은 없음

`STA`(Station)는 Wi-Fi 라디오의 인터페이스 역할을 뜻한다. 반면 `association`은 STA가 특정 AP에 접속해 AP의 네트워크에 참여하는 절차를 뜻한다. 따라서 다음 두 상태는 동시에 가능하다.

```text
Wi-Fi interface: STA
AP association: 없음
```

현재 TX는 `WIFI_MODE_STA`로 라디오를 초기화하고 channel 11, HT20을 설정하지만 다음 작업은 하지 않는다.

- SSID 검색
- AP 비밀번호 인증
- `esp_wifi_connect()`를 통한 AP 접속
- IP 주소 획득

그 대신 Wi-Fi 라디오가 켜진 뒤 ESP-NOW를 초기화하고 broadcast peer(`ff:ff:ff:ff:ff:ff`)로 frame을 송신한다. 즉 STA라는 말은 여기서 “공유기에 접속한 클라이언트”가 아니라, **ESP-NOW 송신에 사용할 Wi-Fi STA 인터페이스를 선택했다**는 의미다. 이 구조에서는 AP, DHCP, IP 주소, UDP가 필요 없다.

## 3. TX 동작

TX 초기화:

1. **NVS 초기화**
   - ESP-IDF가 Wi-Fi 관련 내부 설정을 사용할 수 있도록 비휘발성 저장소를 준비한다.
   - 이 프로젝트에서 실험 payload나 session 데이터를 NVS에 저장하는 단계는 아니다.
2. **STA mode, HT20, channel 11 설정**
   - Wi-Fi 라디오를 STA 인터페이스로 켜고, TX와 RX가 만날 무선 조건을 고정한다.
   - STA mode는 AP 접속을 의미하지 않으며, IP network도 만들지 않는다.
3. **synthetic STA MAC 설정**
   - 무선 frame의 송신자 주소를 `1a:00:00:00:00:00`으로 설정한다.
   - RX는 이 주소에서 온 frame만 CSI 처리하므로, 실험용 TX를 식별하는 표식 역할을 한다.
   - 보드 자체의 물리 chip MAC과는 다른 주소다.
4. **ESP-NOW broadcast peer 등록**
   - `ff:ff:ff:ff:ff:ff`를 peer로 등록해 주변의 여러 RX가 같은 frame을 받을 수 있게 한다.
   - 특정 RX 한 대의 IP 주소나 MAC으로 보내는 방식이 아니다.
5. **HT20 MCS0 LGI rate 적용**
   - ESP-NOW frame의 PHY 전송 방식을 고정한다.
   - MCS0 LGI는 높은 속도보다 안정적인 반복 송신과 일정한 CSI shape를 우선하는 현재 실험 조건이다.

송신 loop:

```c
for (uint32_t count = 0; ; ++count) {
    esp_now_send(peer.peer_addr, (const uint8_t *)&count, sizeof(count));
    usleep(1000 * 1000 / 100);
}
```

payload의 `count`는 RX binary header의 `tx_seq`가 된다. 목표 송신률은 100Hz다.

### 송신 loop를 한 줄씩 읽기

```text
count = 0, 1, 2, 3, ...
```

- `count`는 TX가 frame마다 1씩 증가시키는 32-bit 번호다.
- 이 번호 자체가 움직임 정보나 CSI 값은 아니다.
- RX가 같은 frame을 받으면 이 번호를 `tx_seq`로 저장해 여러 RX의 공통 순서를 맞추는 데 사용한다.

```text
esp_now_send(..., &count, sizeof(count))
```

- 현재 `count` 값을 ESP-NOW payload로 넣어 한 frame을 송신한다.
- 송신 시 Wi-Fi PHY가 LLTF 같은 훈련 구간을 자동으로 붙인다.
- RX는 이 frame을 관찰하면서 CSI를 계산한다.

```text
usleep(1000 * 1000 / 100)
```

- 1초를 100번으로 나눈 약 10ms 동안 기다린다.
- 그래서 목표 송신 주기는 10ms, 목표 빈도는 100Hz다.
- 실제 수신률은 무선 간섭, RX 처리 지연, ring buffer drop에 따라 100Hz보다 낮아질 수 있다.

전체 TX 동작은 다음과 같다.

```text
Wi-Fi/ESP-NOW 초기화
  → count=0 frame 송신
  → 10ms 대기
  → count=1 frame 송신
  → 10ms 대기
  → 반복
```

송신 API가 실패해도 firmware는 오류 로그를 남기고 다음 loop를 계속 시도한다. 따라서 TX 로그의 송신 호출 횟수와 RX가 실제로 받은 CSI frame 수는 항상 같다고 보장되지 않는다.

## 4. RX CSI capture

RX는 sender source MAC과 일치하는 frame만 처리한다.

ESP32-S3 CSI 설정:

| field | 값 |
|---|---|
| `lltf_en` | true |
| `htltf_en` | false |
| `stbc_htltf2_en` | false |
| `ltf_merge_en` | false |
| `channel_filter_en` | true |
| `manu_scale` | false |

LLTF only + HT20 조합에서 기대 raw CSI는 128 bytes다.

```text
128 raw bytes
  = 64 I/Q pair
  = reader 변환 후 amplitude 64개
```

RX firmware는 amplitude 변환, z-score, moving average, outlier clip을 하지 않는다. raw signed int8 I/Q bytes를 그대로 Mac으로 전송한다.

### 4.1 LLTF의 의미와 CSI 계산

LLTF(Legacy Long Training Field)는 Wi-Fi 프레임의 실제 payload 앞에 있는 **수신 훈련용 기준 신호**다. TX 애플리케이션이 LLTF payload를 직접 만드는 것이 아니다. Wi-Fi PHY가 프레임을 송신할 때 표준에 정의된 훈련 구간을 자동으로 앞에 붙이고, RX PHY가 이를 이용해 수신 상태를 추정한다.

프레임을 단순화하면 다음과 같다.

```text
Wi-Fi frame
├── preamble / training fields
│   └── LLTF  ← 현재 CSI 계산에 사용
└── payload  ← ESP-NOW의 uint32_t count 등
```

RX는 LLTF의 원래 기준값 `X[k]`와 안테나에서 실제로 받은 값 `Y[k]`를 subcarrier별로 비교해 채널 응답을 추정한다.

```text
H[k] ≈ Y[k] / X[k]
```

여기서 `k`는 OFDM subcarrier 번호이고, `H[k]`가 CSI의 복소수 채널 추정값이다. 복소수 한 개는 다음 두 성분으로 저장된다.

```text
H[k] = I[k] + jQ[k]
```

- `I`: In-phase 성분
- `Q`: Quadrature 성분
- `|H[k]| = sqrt(I[k]² + Q[k]²)`: 현재 Mac reader가 저장하는 amplitude

사람이나 물체가 TX와 RX 사이에서 움직이면 직접 경로와 반사 경로가 바뀐다. 그러면 같은 LLTF를 보내도 `Y[k]`가 달라지고, 결과적으로 `H[k]`, I/Q, amplitude가 변한다. 따라서 LLTF는 움직임 그 자체가 아니라 **움직임으로 인해 변한 무선 채널을 측정하기 위한 기준**이다.

### 4.2 왜 LLTF만 사용하는가

Wi-Fi 프레임에는 LLTF 외에도 HT-LTF, STBC HT-LTF 같은 추가 훈련 구간이 있을 수 있다. 현재 RX는 다음처럼 설정되어 있다.

| 훈련 구간 | 현재 설정 | 의미 |
|---|---:|---|
| LLTF | `true` | CSI 계산에 사용 |
| HT-LTF | `false` | 추가 CSI 구간으로 사용하지 않음 |
| STBC HT-LTF | `false` | 사용하지 않음 |
| LTF merge | `false` | 여러 LTF를 합치지 않음 |

LLTF-only는 가장 많은 CSI 정보를 모으기 위한 설정이 아니라, RX 간 데이터 shape와 의미를 단순하고 일정하게 유지하기 위한 현재 실험 조건이다. HT20에서 기대하는 결과는 64개 subcarrier의 I/Q pair, 즉 raw CSI 128 bytes다.

```text
LLTF CSI
  → 64 subcarrier
  → [I0,Q0, I1,Q1, ... I63,Q63]
  → raw 128 bytes
  → Mac에서 amplitude 64개
```

`LLTF only`를 `HT-LTF까지 사용`으로 변경하면 CSI 길이, subcarrier 구성, frame contract, 모델 입력 shape가 달라질 수 있다. 그러므로 LTF 설정 변경은 firmware만 수정하는 작업이 아니며 [serial frame schema](data-schema.md)와 reader·후처리 문서를 함께 변경해야 한다.

## 5. Callback과 USB writer

```text
wifi_csi_rx_cb
  → source MAC 검사
  → callback count 증가
  → tx_seq 추출
  → 32-byte header + raw CSI 생성
  → 64KiB no-split ring buffer에 non-blocking push

uart_writer_task
  → ring buffer receive
  → usb_serial_jtag_write_bytes
  → USB frame count 증가
```

주요 상수:

| 상수 | 값 | 의미 |
|---|---:|---|
| `CSI_FRAME_VERSION` | 2 | binary contract version |
| `CSI_MAX_RAW_BYTES` | 384 | firmware safety upper bound |
| `CSI_RINGBUF_BYTES` | 64KiB | callback/USB decoupling |
| `CSI_USJ_TX_BUF_BYTES` | 16KiB | USB driver TX buffer |
| `CSI_TX_SEQ_OFFSET` | 15 | `wifi_csi_info_t.payload` 내 TX count offset |
| `POC_DUMP_CSV` | 0 | CSV 출력 비활성, binary mode |

Ring buffer가 가득 차면 callback은 기다리지 않고 `g_ringbuf_drop`을 증가시킨다.

## 6. Binary stream

RX는 32-byte packed v2 header 뒤에 raw CSI를 붙여 USB로 보낸다.

```text
[header 32 bytes][raw CSI raw_len bytes]
```

정확한 field 계약은 [serial frame schema](data-schema.md)를 참조한다.

5초 진단 로그:

```text
5s: cb=N (+M, X.XHz) uart=K (+L, Y.YHz) ringbuf_drop=D
```

정상 상태에서는 callback과 USB frame 증가량이 비슷하고 `ringbuf_drop=0`이어야 한다.

## 7. Build와 flash

권장:

```bash
python3 scripts/meshsense_cli.py
```

수동 build:

```bash
cd esp32s3_csi_send_poc
idf.py set-target esp32s3
idf.py build

cd ../esp32s3_csi_recv_poc
idf.py set-target esp32s3
idf.py build
```

수동 flash:

```bash
idf.py -p /dev/cu.usbmodemXXXX flash
```

TX는 monitor로 상태를 확인할 수 있다. RX는 binary stream과 reader가 같은 USB port를 사용하므로 수집할 때 monitor를 닫는다.

## 8. 변경 규칙

다음 변경은 TX/RX/reader/schema 문서를 같은 변경에서 함께 수정해야 한다.

- frame version/layout
- channel/bandwidth/rate
- TX frequency 또는 payload
- CSI LTF 구성
- `tx_seq` offset
- raw length upper bound
- I/Q 순서나 amplitude 표현

[문서 주도 개발 규칙](documentation-policy.md)의 Definition of Done을 적용한다.

## 9. RX 수집 안정성 (실측으로 확인된 것)

> 상태: **CURRENT**

- **DTR/RTS 를 건드리면 보드가 리셋된다.** ESP32-S3 의 USB-C 는 외부 UART 브리지가 아니라
  네이티브 USB-Serial-JTAG 라, 브리지 칩용 관례대로 `dtr=False, rts=False` 를 눌러두면
  **오히려** 리셋이 걸린다. 실측: 손대면 12초에 2회 재부팅, 손대지 않으면 0회.
  reader 는 이 선들을 건드리지 않는다 (`--force-lines` 로만 옛 동작).
- **ring buffer 는 keep-newest** 다. 가득 차면 가장 오래된 프레임을 버리고 새 것을 넣는다.
  기본 ringbuf 는 새 것을 버리므로, host 가 없는 동안 옛 프레임이 버퍼를 점유해 수집을
  시작한 순간 수십 초 묵은 데이터가 먼저 나온다 (46초 방치 시 `tx_seq` 격자에 4134스텝 구멍).
- **reader 는 붙을 때 백로그를 비운다.** 보드 시계와 벽시계의 진행 비율을 0.3초 슬라이딩
  윈도로 보고, 실시간을 따라잡으면 멈춘다. 실측: 방치 후 472프레임 0.53초, 백로그 없으면
  48프레임 0.38초.
- **`ESP_LOG` 는 USB 로 나오지 않는다.** console primary 가 GPIO43 UART 이고
  (`CONFIG_ESP_CONSOLE_UART_CUSTOM`) USB-Serial-JTAG 드라이버를 app 이 직접 설치한다.
  진단 카운터는 IDENT 프레임으로 host 에 전달된다 ([data-schema.md](data-schema.md)).
- **TX 는 전원만 있으면 된다.** TX 펌웨어는 host 입출력 코드가 없다. USB 포트가 모자라면
  충전기에 꽂아두고 RX 만 노트북에 연결한다. 단 수집 중 TX 가 재부팅하면 `tx_seq` 가 0부터
  다시 시작해 정렬 키가 깨지므로, reader·`measure_csi_hz` 가 `tx_back` 으로 감지하고
  전처리는 그런 세션을 거부한다.

## 10. 손대면 안 되는 것

> 상태: **CURRENT**

- CSI 콜백 안에 동기 I/O 금지 — `ets_printf` 계열을 넣으면 WiFi driver task 가 막혀 ~50Hz 로 붕괴
- 보드에서 진폭 계산·정규화 금지 — raw I/Q 를 그대로 보내고 host 가 처리
- `esp32s3_csi_send_poc/sdkconfig.defaults` 의 `CONFIG_FREERTOS_HZ=1000` 삭제 금지 —
  100이면 `usleep(10000)` 이 1 tick 으로 반올림돼 실효 50Hz
- 수집 시작 시 esptool 로 포트 프로브 금지 — 보드(TX 포함)가 리셋되어 `tx_seq` 가 끊긴다

## 11. 실보드 검증 (2026-08-26)

> 상태: **HISTORICAL**

TX1(외부 전원) + RX101 + RX103, 60초:

```text
device_101.csi: n=5944 hz=99.91 gaps>200ms=0 seq_gap=0 tx_back=0 tx_cov=0.999 rssi_med=-28
device_103.csi: n=5880 hz=98.99 gaps>200ms=0 seq_gap=0 tx_back=0 tx_cov=0.990 rssi_med=-11
cross-RX 공통 tx_seq: 5870 / 5880 = 99.8%
reader: crc_fail=0 invalid=0 resync=0   펌웨어: ringbuf_drop=0 partial=0
```

경위는 [sprint/2026-08-collection-hardening.md](sprint/2026-08-collection-hardening.md).
