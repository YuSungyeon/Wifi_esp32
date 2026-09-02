# 수집률(Hz)

> 목표는 TX 100Hz 자극에 RX CSI 콜백 100Hz. 현재 USB 직결·무선 업링크 모두 **~99.5Hz, 손실 0%**.

## 2026-05-22 — AP 경로(SoftAP+UDP) 17Hz → 22Hz 천장, 경로 폐기

- **증상**: 평균 17.4Hz, 패킷 간격 중앙값 12.1ms(활성 구간은 ~83Hz), 200ms+ 공백 14.2%.
  "100Hz burst 후 0.2~0.5초 침묵" 패턴.
- **시도 (11단계, 각각 실측)**:
  | # | 가설·시도 | 결과 |
  |---|---|---|
  | 1 | AMPDU 비활성화 | 14.15Hz, gaps 10.9% — 미세 개선 |
  | 2 | TX UDP broadcast 끄기 (에어타임 경쟁 가설) | **9.94Hz, gaps 24.1% — 악화.** UDP 는 경쟁이 아니라 추가 자극원이었다 |
  | 3 | 진단 카운터 추가 | TX 송신은 정확히 100Hz, tx_done 실패 <1%. **RX 콜백이 절반만 발생** |
  | 4 | HT20 강제 + channel_filter off + throttle 제거 | cb 182~763Hz 폭증, JSONL 566Hz — **주변 2.4GHz 잡음이 다 잡힌 것**, ML 부적합 |
  | 5 | BSSID 필터 | 우리 AP 프레임은 본래 8~15Hz 뿐이라는 게 드러남. JSONL 1.91Hz |
  | 6 | `info->mac` 직접 로깅 | 필터 로직 정상. 본질 문제: 우리 프레임 cb 트리거가 ~9Hz |
  | 7 | ESP-NOW OFDM rate 강제 (DSSS 가설) | 2~16Hz — rate 는 원인 아님 |
  | 8 | promiscuous 끄기 | **0.2Hz** — STA 만으로는 broadcast CSI 콜백이 안 일어남. 즉시 복원 |
  | 9 | unicast 확인 | 이미 unicast 였음. 그래도 5~10Hz |
  | 10 | 발열 의심 | 세션 후반 association 자체 실패. 일부 측정이 thermal-degraded 였을 가능성 |
  | 11 | 30분 cooldown + 거리 확보 (RSSI −37) | cb 12Hz, sent 2~3Hz — **RSSI 포화 가설 기각** |
- **원인 (확정)**: AP/STA association + DTIM 게이팅 + 자극·데이터 채널 공유. 코드·설정 옵션은
  전부 시도됐고 ESP-IDF v5.2.2 + 이 펌웨어 베이스로는 **5~10% 트리거율이 천장**.
  200ms+ 공백은 14.2%→0.3%로 거의 잡았지만 총량이 안 나왔다.
- **해결**: Espressif `esp-csi` 공식 예제 기반으로 펌웨어 베이스 교체 (아래).
  결정 기록 [ADR-0001](../adr-poc-only.md).
- **재발 방지**: SoftAP/UDP 경로를 현재 기능으로 다시 언급하지 않는다. 필요하면 새 ADR 로 supersede.
- **부수 이슈 (같은 날)**: Mac collector IP 가 `.2` 가 아니라 `.4` 로 잡힘 — SoftAP DHCP 가
  association 순서대로 IP 를 주는데 RX 가 먼저 붙으면 Mac 이 밀린다. `COLLECTOR_IP` 컴파일
  고정이라 패킷 0개 수신. 경로 폐기로 함께 소멸.

## 2026-05-22 — esp-csi PoC 로 교체, 97.5Hz

- **시도**: STA-only(association 없음), 채널 11, HT20, custom MAC `1a:00:00:00:00:00`,
  ESP-NOW MCS0 OFDM 강제, `CONFIG_FREERTOS_HZ=1000`.
- **결과**: RX 5초 카운터 평균 **97.5Hz** (96.8~98.4). 동일 보드·동일 IDF 에서 100Hz 달성.
- **결정적 발견**: 이전 22Hz 천장은 ESP32-S3 한계가 아니라 토폴로지 문제였다.
- **부수 발견**: `POC_DUMP_CSV=1`(콜백 안 `ets_printf` CSV 덤프)에서 cb 가 ~46Hz 로 떨어짐.
  921600 baud 가 ~50Hz 처리 한계라 WiFi driver task 를 백프레셔로 막은 것.
  → **CSI 콜백 안 동기 I/O 절대 금지** (`CLAUDE.md` 손대면 안 되는 것).
- `CONFIG_FREERTOS_HZ=1000` 이 없으면 `usleep(10000)` 이 tick 반올림으로 20ms(=50Hz) 가 된다.
  삭제 금지 근거를 `sdkconfig.defaults` 주석으로 남김.

## 2026-05-23 — USB 스트리밍 end-to-end 100Hz · 손실 0%

- **시도**: 바이너리 ring buffer + USB-Serial-JTAG 스트리밍.
- **결과**: reader 측 500 frames/5s = 100Hz 정확, `invalid=0`, `seq_drop=0`.
- **막힌 지점**: ESP32-S3 dev 보드 USB-C 는 UART0 가 아니라 **USB-Serial-JTAG 페리페럴**.
  처음에 `uart_write_bytes` 를 썼더니 데이터가 물리 핀으로 나가 USB 로 안 보였다.
  `usb_serial_jtag_write_bytes` 를 써야 한다.

## 2026-08-25 — "Hz 가 낮다"는 전제 자체가 틀렸음을 확인

- **증상**: 작업 시작 전제가 "유선·무선 둘 다 Hz 가 낮다".
- **실측**: 저장된 세션 재측정 — `20260615/session_21` dev101 **100.94Hz**, dev103 **100.84Hz**,
  gaps 0%. AP 경로 세션 12개는 0.18~22.8Hz.
- **해석**: USB 경로 Hz 는 이미 해결돼 있었다. 진짜 문제는 데이터를 학습에 쓸 수 없다는 것
  ([04](04-session-labels.md), [05](05-signal-features.md)).

## 2026-08-26 — 실보드 재검증 (프레임 v4 이후)

- TX1 + RX103, 60초: `hz=99.83  gaps=0  seq_gap=0  tx_cov=0.998`.
- RX101 + RX103 (TX 외부 전원), 60초: `99.91Hz / 98.99Hz`, **cross-RX 공통 tx_seq 99.8%**.
  두 RX 의 평균 진폭 상관 −0.062 — 서로 다른 경로의 채널을 본다.
- 여러 측정에서 hz 96.5~99.8, RSSI −21~−28 에 따라 `tx_cov` 0.986~0.998.
  **남은 편차는 소프트웨어가 아니라 무선 환경** — `seq_gap`(시리얼 구간)은 항상 0.

## 2026-09-02 — 무선 업링크(ESP-NOW → SINK → USB) 99.58Hz

- TX(외부 전원) + RX(업링크) + SINK, 60초: `hz=99.58  seq_gap=0  tx_cov=0.996`,
  RX `uplink_ok=5804 fail=0 ringbuf_drop=0`.
- **업링크 송신이 CSI 수신을 방해하지 않는다** — 콜백 ~99Hz 로 USB 직결과 같다.
  반이중이라 송신 중 못 듣는 시간이 100Hz × ~300µs ≈ 3% 인데 실측상 구분되지 않는다.
- 첫 측정은 51Hz·46% 손실로 나왔는데 측정 오류였다 → [06](06-measurement-pitfalls.md).
- 미검증: RX 2대 이상 업링크 시 에어타임 경합. 계산상 RX 3대 + TX 자극 ~18%.

## 판정 기준 (현재)

`measure_csi_hz.py` 기준 `hz=100±3`, `gaps>200ms=0`, `seq_gap=0`, `tx_cov>0.99`.
Hz 가 낮으면 순서대로: RX 5초 카운터(`fw_csi_cb`, IDENT 로 전달) → `ringbuf_drop` →
호스트 `seq_gap` → RSSI(−25~−40 권장). 카운터가 낮으면 무선, 카운터는 정상인데 파일이
적으면 USB/호스트 쪽이다.
