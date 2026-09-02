# 실시간 무선 경로 (ESP-NOW 업링크 + SINK)

> 설계와 현재 상태는 [realtime-uplink.md](../realtime-uplink.md).

## 2026-09-02 — 역할별 MAC 분리 (선결 과제)

- **문제**: 모든 보드가 STA MAC `1a:00:00:00:00:00` 을 썼다(esp-csi 의 association-free 트릭).
  RX 가 송신을 시작하면 서로의 업링크를 CSI 로 잡아 자기오염된다.
- **해결**: TX `..:00`(CSI 필터 기준) / RX `..:<CSI_RX_ID>` / SINK `..:ff`. RX 의 CSI 필터는
  TX MAC 에만 걸려 있어 업링크·ACK 는 CSI 로 안 잡힌다.
- **재발 방지**: `CLAUDE.md` — 역할별 MAC 을 같게 하지 말 것.

## 2026-09-02 — 세 번째 보드가 인식되지 않음

- **증상**: 3보드를 꽂았는데 `/dev/cu.usbmodem*` 에 2개만.
- **원인**: (사용자 측 연결 문제 — 데이터선 없는 충전 케이블 또는 허브 전력.) TX 를 외부
  전원으로 빼고 RX 2대만 연결하는 것으로 해결.
- **부수 확인**: TX 는 외부 전원에서 정상 송신 (RX 콜백 ~100Hz 로 확인).

## 2026-09-02 — 첫 측정 51Hz·46% 손실 → 백로그

[06](06-measurement-pitfalls.md) 참조. 비우고 재면 99.5Hz·손실 0%.

## 2026-09-02 — 프레임마다 4B 덧붙음 (두 원인이 겹침)

1. ring buffer 4B 정렬 패딩 — RX·SINK 양쪽 수정.
2. 고쳐도 그대로 → TX 자극 broadcast 가 싱크를 거쳐 새어 나옴 — 프레임 서명 필터.

상세는 [02](02-serial-stream.md) 2026-09-02 두 항목. 데이터는 `crc_fail=0 seq_gap=0` 으로
멀쩡했기 때문에 카운터만 봤으면 못 찾았을 문제.

## 2026-09-02 — 결과

TX(외부 전원) + RX(업링크) + SINK, 60초: `hz=99.58 seq_gap=0 tx_cov=0.996`,
`uplink_ok=5804 fail=0 ringbuf_drop=0`, reader `resync=0`. 업링크가 CSI 수신을 방해하지 않음.

## 2026-09-03 — 2-RX 동시 업링크에서 약한 링크에 ~7% 손실 (실보드)

- **구성**: TX(외부) + RX103(외부, 업링크) + RX102(USB 전원, 업링크 rx_id=2) → SINK RX101 → Mac.
- **증상**: 세 번 연속 같은 패턴 — RX102 `seq_gap=0` 99.8Hz, **RX103 `seq_gap≈360~410` 93Hz**
  (약 7%). host demux 자체는 정상 (`device_102/103.csi` 분리, cross-RX 99.5%).
- **어디서 잃었나 (싱크 상태 프레임 추가 후)**: RX103 `uplink_fail=0 ringbuf_drop=0`,
  싱크 `drop=0 usb_timeout=0`, host `crc_fail=0 invalid=0`. 그런데 RX 두 대 `sent` 합 −
  싱크 `recv` ≈ **400 = RX103 의 seq_gap**. 즉 RX 는 ACK 를 받아 성공으로 셌는데 싱크
  `recv_cb` 에는 안 온 프레임이 있다 → **싱크 Wi-Fi 수신 경로(MAC-ACK 이후, 콜백 이전)**.
  같은 RX 에서 `DUP=11` — ACK 유실 후 재전송 — 링크가 marginal 하다는 신호.
- **결정적 실험**: RX102 를 USB 직결로 되돌려 업링크를 RX103 하나만 남기니
  **RX103 손실 0** (`seq_gap=0`, 99.66Hz), 싱크 `recv=5808` = RX103 `sent=5808` 정확히 일치.
- **결론**: 손실은 RX103 보드나 위치의 고정 문제가 아니라 **동시 업링크 2개 + TX broadcast
  (`foreign≈96Hz`) 가 싱크 수신에 겹칠 때** 발생하며 약한 링크가 먼저 무너진다.
  1-RX 업링크는 손실 0.
- **아직 못 가른 것**: 손실이 (a) 싱크 드라이버 rx 버퍼/큐 포화인지 (b) 공중 충돌 후
  ACK 만 통과한 것인지. RX102·RX103 위치를 맞바꿔 손실이 보드를 따라가는지 위치를
  따라가는지 보면 (b) 가 갈린다 — 물리 이동이 필요해 미실행.
- **완화 후보** (측정 순서대로): 싱크 `CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM`/static rx
  buffer 증가 → RX 송신 시점 분산(RX 마다 위상 오프셋) → 진폭 uint8 양자화로 프레임 축소
  → 업링크 속도 MCS 상향. 어느 것이든 **RX 2대 동시 + seq_gap 으로 판정**한다.

## 미검증

- RX 3대 동시 업링크. 2대에서 이미 약한 링크 7% 손실이라 완화 없이는 늘리지 않는다.
- 싱크의 RX 겸용.
