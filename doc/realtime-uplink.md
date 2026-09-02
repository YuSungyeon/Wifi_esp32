# 실시간 경로 — ESP-NOW 업링크 + USB 싱크

> 상태: **CURRENT** (1-RX 실보드 검증 완료, 2026-09-02)
> firmware: `esp32s3_csi_recv_poc` (`CSI_UPLINK_ENABLED=1`) · `esp32s3_csi_sink`

학습 데이터 수집은 RX 를 USB 로 직결하지만, 실시간 분류는 RX 를 방 안에 흩어 놓아야 한다.
RX 는 호스트 연결이 없으면 데이터를 버리므로(링버퍼 4초) 무선 업링크가 필요하다.

```text
TX ──ESP-NOW 10ms 자극──▶ RX × N ──ESP-NOW 업링크──▶ SINK ──USB──▶ Mac
   (전원만)                (전원만)                    (유일한 USB 연결)
```

## 왜 싱크 보드가 따로 필요한가

맥북 Wi-Fi 로는 RX 의 업링크를 받을 수 없다 — ESP-NOW 는 Espressif 독자 프로토콜이라
일반 Wi-Fi 클라이언트가 해석하지 못한다. RX 를 일반 Wi-Fi 로 붙여 UDP 를 쓰는 경로는
association + DTIM 게이팅 때문에 22Hz 천장에 막혀 폐기했다
([ADR-0001](adr-poc-only.md), [troubleshooting/01-csi-rate.md](troubleshooting/01-csi-rate.md)).

**싱크는 RX 를 겸할 수 있다.** 어차피 USB 로 붙어 있으니 자기 CSI 도 함께 흘려보내면
된다. 그러면 필요한 보드는 N+2 가 아니라 **N+1** 이다.

## 역할별 MAC

모두 같은 MAC 을 쓰면 RX 가 송신을 시작하는 순간 서로의 업링크를 CSI 로 잡아 자기오염된다.

| 역할 | STA MAC | 비고 |
|---|---|---|
| TX | `1a:00:00:00:00:00` | **RX 의 CSI 필터 기준** — 이 MAC 의 프레임만 CSI 로 통과 |
| RX | `1a:00:00:00:00:<id>` | `CSI_RX_ID` (1~254), 보드마다 달라야 한다 |
| SINK | `1a:00:00:00:00:ff` | RX 가 unicast 로 보내는 주소 |

RX 의 CSI 필터는 TX MAC 에만 걸려 있어, 자기 업링크나 다른 RX 의 업링크·싱크의 ACK 는
CSI 로 잡히지 않는다.

## 프레임은 그대로 흘러간다

싱크는 프레임을 **해석하지 않고 그대로 전달한다.** RX 가 만든 v4 프레임(CRC 포함)이
host 까지 무손상으로 도달하는지 검증할 수 있고, host 파서가 USB 직결과 완전히 같다
(`scripts/csi_store.py`). 프레임 규격이 바뀌어도 싱크는 고칠 게 없다.

따라서 **수집 도구가 그대로 쓰인다** — reader 는 싱크 포트를 RX 인 것처럼 읽는다.
RX 가 보내는 IDENT 도 그대로 전달되므로 `device_id` 자동 식별도 동작한다.

### 싱크는 TX 자극을 걸러야 한다

같은 채널에는 RX 업링크만 오는 게 아니다 — TX 의 자극 broadcast(4바이트 카운터)도
같이 들어온다. 그대로 흘려보내면 프레임 사이에 4바이트가 끼어 host 가 매 프레임
재동기화한다. 싱크는 프레임 서명(magic + version)으로 우리 프레임만 통과시키고,
나머지는 `foreign` 으로 센다.

## 빌드·플래시

```bash
# SINK (registry 등록 불필요 — device_id 를 쓰지 않는다)
cd esp32s3_csi_sink && idf.py set-target esp32s3 && idf.py -p <포트> flash

# RX 업링크 모드 (보드마다 CSI_RX_ID 를 다르게)
cd esp32s3_csi_recv_poc
idf.py -DCSI_UPLINK_ENABLED=1 -DCSI_RX_ID=1 -p <포트> flash

# RX 를 학습 수집(USB)으로 되돌리려면
idf.py -DCSI_UPLINK_ENABLED=0 -p <포트> flash
```

업링크 모드에서 RX 는 **USB-Serial-JTAG 드라이버를 아예 설치하지 않는다** — USB 로
데이터를 내보내지 않는다. USB 는 플래시·전원 용도로만 쓴다.

다만 그 덕에 콘솔 secondary 경로가 비어 있어, 업링크 RX 의 USB 포트에는 **5초 진단 로그가
텍스트로 나온다**: `5s: cb=… (+…, 99.4Hz) uplink_ok=… fail=… ringbuf_drop=…`.
싱크 없이도 그 RX 의 CSI 콜백률과 업링크 성공률을 바로 볼 수 있다 (`screen`/`cat` 으로 충분).
`fail=0` 이면 싱크가 켜져 ACK 하고 있다는 뜻이다.

## 수집

싱크 포트를 평소처럼 읽으면 된다.

```bash
python scripts/csi_serial_reader.py --port <싱크 포트> --session-dir <세션>
python scripts/measure_csi_hz.py <세션>
```

## 실측 (2026-09-02, TX1 + RX103 + SINK(RX101), 60초)

TX 는 외부 전원, RX 는 USB 전원만(데이터 경로 미사용), 싱크만 데이터 연결.

```text
device_103.csi: n=5973 hz=99.58 median_dt=10.0ms gaps>200ms=0
      seq_gap=0 tx_back=0 tx_cov=0.996 rssi_med=-24 agc_levels=22
reader        : crc_fail=0 invalid=0 resync=0
RX 펌웨어     : csi_cb=5775 uplink_ok=5804 uplink_fail=0 ringbuf_drop=0
```

- **업링크 성공률 100%** (`uplink_fail=0`), 링버퍼 드롭 0
- **호스트 도달률 손실 0%** (`seq_gap=0`)
- **업링크가 CSI 수신을 방해하지 않는다** — 콜백 ~99Hz 로 USB 직결(~99Hz)과 같다.
  RX 가 송신하는 동안 반이중이라 못 듣는 시간은 100Hz × ~300µs ≈ 3% 인데, 실측상
  구분되지 않는다.

### 에어타임 여유

프레임 172B 를 MCS0 로 보내면 패킷당 약 450µs (프리앰블·백오프·ACK 포함) → RX 1대당
약 4.5%. RX 3대 + TX 자극이면 총 ~18% 로 여유가 있다. 다만 **RX 를 늘렸을 때도
CSI 콜백률이 유지되는지는 아직 미검증**이다 (현재 RX 1대로만 확인).

## 다중 RX — host demux

> 상태: **CURRENT** (host 측은 pty 로 검증, 실보드 다중 RX 는 미검증)

RX 는 업링크 모드에서 헤더 `rx_id` 에 `CSI_RX_ID` 를 찍는다. reader 는 한 포트 위의
프레임을 `rx_id` 로 갈라 RX 마다 `RxStream` 을 두고, 각 RX 의 IDENT(MAC)로 `device_id`
를 정해 `device_<id>.csi` 를 따로 쓴다. USB 직결은 `rx_id=0` 스트림 하나뿐인 특수 경우다.

pty 검증: RX 2대(rx_id 1·2)의 프레임을 한 포트에 교대로 흘렸을 때
`device_101.csi` / `device_103.csi` 로 분리되고 각각 `seq` 연속, USB 직결 회귀 없음.

## 다중 RX 실측 (2026-09-03)

TX(외부) + RX103(외부, 업링크) + RX102(업링크 rx_id=2) → SINK, 60초 × 3회:

```text
RX102: 99.8Hz seq_gap=0          RX103: 93.0Hz seq_gap≈400 (~7%)
싱크: drop=0 usb_timeout=0 foreign≈96Hz     cross-RX 공통 tx_seq 99.5%
```

**CSI 콜백률은 유지된다**(두 RX 모두 `csi_cb`≈5770/60s). 손실은 CSI 수신이 아니라
**업링크 수신 쪽** — RX 는 ACK 를 받아 성공으로 셌는데 싱크 `recv_cb` 에 안 온 프레임이
약한 링크에 ~7%. 업링크를 1대로 줄이면 0. 상세와 완화 후보는
[troubleshooting/08](troubleshooting/08-wireless-uplink.md).

## 남은 일

- **2-RX 동시 업링크 손실 완화** — 싱크 rx 버퍼 증가 → 송신 시점 분산 → 프레임 축소 순으로
  측정. 완화 전에는 RX 3대로 늘리지 않는다.
- **싱크의 RX 겸용**: 싱크가 자기 CSI 도 함께 흘려보내면 보드 1대를 아낀다.
- **실시간 추론 훅**: 현재는 파일 저장까지다. 슬라이딩 윈도를 모델에 넘기는 경로는 없다.
