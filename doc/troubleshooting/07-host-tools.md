# reader·CLI·GUI 도구 버그

## 2026-08-25 — reader 가 보드가 죽어도 영원히 대기

- **증상**: `ser.timeout=1` 을 설정해놓고 타임아웃을 `continue` 로 삼킴. 보드가 죽거나 USB 가
  빠져도 빈 파일을 만들며 정상처럼 보였다. flush 도 500프레임(≈5초)에 한 번.
- **해결**: 종료 코드 분리(0 정상 / 2 IDENT 미수신 / 3 스트림 정지 / 4 파일 충돌),
  `--stall-timeout` 3초, flush 1초. pty 로 4경로 재현.
- `find_magic` 의 1바이트 read 루프 → 청크 read + `bytes.find`. 진폭 계산의 서브캐리어당
  `struct.unpack_from` 루프(12,800/s) → numpy 벡터화(`csi_store`).

## 2026-08-25 — pty 가 커스텀 baud·DTR ioctl 을 거부

- **증상**: `IOSSIOSPEED`(921600), `TIOCMBIC`(DTR) 에서 `Errno 25 Inappropriate ioctl`.
- **대응**: 테스트는 115200, DTR/RTS 는 `try/except`.
- **후일담**: 실보드에서 그 DTR 설정 자체가 리셋 원인으로 판명 ([03](03-board-reset-usb.md)).
  지금은 감싸는 게 아니라 아예 건드리지 않는다.

## 2026-08-26 — TX 포트가 rc=3(스트림 정지)로 오분류

- **원인**: TX 는 USB 로 아무것도 안 보내는데 stall timeout(3초)이 ident timeout(6초)보다 먼저.
- **해결**: 식별 전에는 stall 검사를 하지 않음 → rc=2(RX 아님).

## 2026-08-26 — GUI: 1.2초 폴링이 입력 중인 폼을 통째로 지움

- **원인**: 폴링마다 화면 전체 `innerHTML` 재렌더.
- **해결**: 섹션별 데이터 지문을 비교해 바뀐 섹션만 재렌더. 폼 섹션은 한 번만.
- **같은 원인**: 워터폴 이미지가 폴링에 지워짐 → 선택 상태를 변수로 들고 섹션 안에서 그림.
  보드 태그 `RX101 RX101` 중복(`board_name` 에 접두어 있음).

## 2026-08-26 — 워터폴 PNG 라벨·여백

- colorbar 가 `Amplitude (on-device norm)` — USB 경로는 온디바이스 정규화를 안 한다
  (deprecated AP 경로 얘기). `sqrt(I²+Q²)` 로 정정.
- RX 1대일 때 제목 겹침·x축 라벨 잘림 — 여백을 비율이 아니라 인치 기준으로.
- matplotlib 한글 두부(□) — AppleGothic 등 자동 탐색.

## 2026-09-02 — 병합 중 `measure_csi_hz.py` 를 덮어썼다가 되돌림

- **증상**: 저쪽 테스트 `test_measure_csi_hz.py` 가 `analyze_jsonl`, `result_headers` 를
  import 못 함.
- **판단**: 저쪽 구현이 재부팅·중복·순서이상까지 잡아 더 낫다. 저쪽 것을 되살리고 `.csi`
  지원(`analyze_csi`)만 얹음. 테스트 40개 통과.

## 2026-09-02 — reader 재작성 중 `--device-id` 충돌 검출 회귀

- **증상**: 다중 RX demux 로 재구성하면서 `--device-id` override 가 프레임이 와야 파일을
  열게 됨 → 파일 충돌 테스트가 rc 4 대신 rc 2.
- **해결**: 명시적 override 는 시작 즉시 연다. 실패 경로 rc 2/3/4 회귀 테스트로 확인.
