# 호스트 도구 오류 (리더·CLI·GUI)

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

## 2026-10-12 — CLI 화면에서 막대가 든 줄만 테두리가 어긋났다

- **증상**: `realtime/ui.py` 의 24행 화면에서 확신 막대 줄만 오른쪽 테두리가 왼쪽으로 당겨져
  있었다. 다른 줄은 62칸인데 그 줄만 40칸이었다.
- **원인**: 표시 폭을 세는 `_cells()` 가 `ord(ch) > 0x2500` 이면 두 칸으로 봤다. 한글은 맞지만
  **괘선·블록 문자(U+2500~U+259F)는 터미널에서 한 칸**이다. 막대에 쓰는 `█`(U+2588)·`░`(U+2591)
  24개가 48칸으로 계산돼 `_pad` 가 여백을 아예 넣지 않았다.
- **해결**: `ord(ch) <= 0x259F` 이면 한 칸, 그 위면 두 칸. ASCII·괘선·블록이 모두 한 칸이 되고
  한글만 두 칸이 된다.
- **재발 방지**: `tests/test_realtime.py::test_every_row_is_the_same_width_on_screen` 이 화면
  모든 줄의 **표시 폭**이 같은지 본다(상태별로). 글자 수(`len`)로 재면 이 버그를 못 잡는다.


## 2026-10-27 — 인증 메일 실패가 "Connection unexpectedly closed" 로만 보임

- **증상**: 클라우드(EC2)에서 가입하면 "메일을 보내지 못했어요". 서버 로그는 `인증 메일 실패: error: Connection
  unexpectedly closed` 뿐이었다.
- **원인**: `smtplib.SMTP.login()` 은 `AUTH PLAIN` 이 거절되면 `AUTH LOGIN` 으로 **다시 시도한다**. Gmail 은 첫
  시도에 **535 BadCredentials**(아이디·앱 비밀번호가 틀림)를 돌려준 뒤, 두 번째 시도에서 **말없이 연결을 끊는다**.
  마지막 예외만 남아 진짜 이유가 가려졌다. 맥에서 일부러 틀린 비밀번호로 재현했다(`set_debuglevel(1)`).
  EC2 네트워크 문제가 아니다 — 맥에서도 똑같이 끊겼다.
- **해결**: `smtp.auth("PLAIN", smtp.auth_plain)` 하나로 고정하고 `SMTPAuthenticationError` 를
  `Gmail 로그인 거절(535) — SMTP_USER 나 앱 비밀번호가 맞지 않습니다` 로 남긴다. 다른 예외는 종류 이름까지 남긴다.
- **재발 방지**: 외부 서비스 로그인 실패는 **첫 거절 이유**가 남게 한다. 라이브러리의 자동 재시도가 원인을 덮을 수 있다.
