# 시리얼 스트림·프레임 무결성

> 프레임 규격은 [data-schema.md](../data-schema.md). Python 정본 `scripts/csi_store.py`, C 정본 `esp32s3_csi_recv_poc/main/app_main.c`.

## 2026-08-25 — 오탐 magic 이 검증 없이 저장됨 → CRC32

- **증상**: `20260523/session_1` 첫 레코드가 `channel=159`, `rssi=+11dBm`, `timestamp_us` 가
  u64 초과. 물리적으로 불가능한 값.
- **원인**: v2 검증이 magic 2바이트 + `raw_len > 4096` 뿐. raw CSI 바이트 안의 우연한
  `0x53 0x43`(int8 I/Q 로 83, 67 — 흔한 값)을 헤더로 오인했다.
- **해결**: 프레임 v3 — `crc32`(헤더+payload, zlib 호환) + `version`·`total_len`·`channel`·
  `rssi` 범위 검사(`csi_store.validate_frame`). pty 로 오탐 magic 주입 → `crc_fail` 로 잡히고
  파일에 안 들어감.
- **되돌림**: ROM `esp_rom_crc32_le` 를 쓰려다 직접 구현으로 선회 — IDF 버전마다 pre/post
  inversion 관례가 달라 호스트 `zlib.crc32` 와 맞추기 까다롭다. 16엔트리 니블 테이블,
  168B×100Hz 는 무시할 부하.
- **재발 방지**: C `csi_frame_header_t` 와 Python `HEADER_DTYPE` 사이에 `offsetof` static
  assert 6개. 한쪽만 고치면 빌드가 깨진다.

## 2026-08-25 — 부분 write 가 스트림을 자름

- **증상**(코드 검토): `usb_serial_jtag_write_bytes` 가 100ms 타임아웃으로 부분 write 하면
  잔여분을 버렸다. `written > 0` 이면 성공으로 집계돼 통계에도 안 잡힘.
- **해결**: 잔여분 재전송 루프 + `partial` 카운터. 실보드 전 구간 `partial=0`.

## 2026-08-25 — `POC_DUMP_CSV` 지뢰 제거

- upstream 의 CSV 덤프 모드(콜백 안 서브캐리어당 `ets_printf`)는 1로 두면 즉시 ~50Hz 붕괴.
  기본 0 이지만 60여 줄 죽은 코드로 남아 있어 삭제.

## 2026-08-26 — `ESP_LOG` 가 USB 로 나오지 않음 → 진단 카운터를 IDENT 에 실음

- **증상**: `ringbuf_drop`/`partial` 을 보려고 5초 로그를 찾았는데 호스트에서 17초 캡처에
  로그 텍스트 **0건**.
- **원인**: console primary 가 GPIO43 UART(`CONFIG_ESP_CONSOLE_UART_CUSTOM`)이고 앱이
  USB-Serial-JTAG 드라이버를 직접 설치한다. 로그는 물리 핀으로만 나간다.
  문서에 있던 "ESP_LOG 가 바이너리 스트림에 끼어든다"는 제약은 **실제로 존재하지 않았다** —
  대신 진단을 볼 방법이 아예 없었다.
- **해결**: IDENT payload 16B→32B, 카운터 4×u32(`csi_cb`, `sent`, `ringbuf_drop`, `send_fail`).
  reader 출력과 `session.json` 에 그대로 들어온다. 부수로 `resync=0` 확인.
- **재발 방지**: 펌웨어 진단은 로그가 아니라 프레임으로 낸다. 애드혹으로 로그를 보려면 GPIO43.
- **정정 (2026-09-03)**: 이건 **USB 모드에서만** 그렇다. 업링크 모드 RX 는 앱이 USB-Serial-JTAG
  드라이버를 설치하지 않아 콘솔의 secondary 경로가 살아 있고, 5초 로그가 USB 로 그대로 나온다
  (`I (25375) csi_recv: 5s: cb=2495 (+497, 99.4Hz) uplink_ok=2508 fail=0 ringbuf_drop=0`).
  → 업링크 RX 를 USB 전원에 꽂아 두면 싱크 없이도 그 포트에서 CSI 콜백률·업링크 성공률을
  바로 볼 수 있다. `fail=0` 이면 싱크가 켜져 ACK 하고 있다는 뜻이기도 하다.

## 2026-09-02 — ring buffer 가 4바이트 정렬 크기를 돌려줌 (프레임마다 4B 덧붙음)

- **증상**: 싱크 스트림에서 magic 간격이 **176**, `total_len=172`. `resync` 가 프레임 수와 같음.
- **원인**: FreeRTOS ringbuf NOSPLIT 이 172B 항목에 `len=176`(정렬 크기)을 돌려준다.
  그대로 `esp_now_send`/USB write 하면 4B 쓰레기가 붙는다.
- **해결**: RX·SINK 양쪽에서 헤더 `total_len` 을 길이 정본으로 쓴다.
- **그런데 고쳐도 176 이었다** → 아래 항목.
- **재발 방지**: `CLAUDE.md` — ring buffer 가 돌려주는 길이를 그대로 쓰지 말 것.

## 2026-09-02 — TX 자극 broadcast 가 싱크를 거쳐 USB 로 새어 나옴

- **증상**: 위 패딩을 고쳐도 간격 176. 176B 를 통째로 뜯어보니 남는 4B 가 `40616` —
  **다음 프레임의 `tx_seq` 값**.
- **원인**: 같은 채널에 RX 업링크만 오는 게 아니다. TX 의 자극 broadcast(4B 카운터)도
  싱크의 ESP-NOW 수신 콜백에 들어오고, 싱크가 해석 없이 그대로 흘려보냈다.
- **해결**: 싱크가 프레임 서명(magic + version)으로 우리 프레임만 통과, 나머지는 `foreign`
  카운터. `resync` 프레임당 1회 → **0**.
- **교훈**: 데이터 자체는 `crc_fail=0 seq_gap=0` 으로 멀쩡했다 — 카운터만 봤으면 못 찾았다.
  **추측하지 말고 원시 바이트를 본다.**

## 판정 기준 (현재)

reader 종료 시 `crc_fail=0 invalid=0 resync=0`. 하나라도 0 이 아니면 스트림이 깨지는 중.
`resync` 만 증가하면 프레임 사이에 이물이 있는 것(이 파일의 마지막 두 항목), `crc_fail` 이
증가하면 프레임 내부 손상.
