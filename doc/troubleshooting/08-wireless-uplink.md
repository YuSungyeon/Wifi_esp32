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

## 미검증

- RX 2대 이상 업링크 시 에어타임 경합 (host demux 는 pty 로 검증됨, 실보드는 아직).
- 싱크의 RX 겸용.
