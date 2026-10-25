# 집 상태 서비스 (IoT 조명 + 긴급 알림) 구현 계획

> 상태: **PLANNED** (2026-10-22 작성, 구현 2026-10-23 시작 · 목표 3일)
> 목적: 졸업 작품 시연용 예시 서비스. 모델 정확도에 따른 오보는 **범위 밖**이다 — 이
> 서비스는 판단 결과를 어떻게 쓰는지를 보여 주는 것이지 판단을 개선하지 않는다.

## 1. 한 줄 요약

집 안 Mac 은 지금처럼 상태를 판단하고 **조명(싱크보드 LED)을 직접** 켜고 끈다. 상태가
바뀔 때와 60초마다 클라우드(EC2)에 상태를 보내고, 클라우드는 기록을 남기며 **알림을
판단해** 문자를 보낸다.

```text
[싱크보드] ──CSI──▶ csi_serial_reader ──.csi──▶ engine (상태 판단 + LED 판단)
     ▲                    │                        │
     └── LED 1바이트(1초) ─┘◀─ realtime_status.json ─┤ "led": true/false
                                                   │
                         realtime/home.py 스레드: 상태가 바뀔 때 + 60초마다
                                                   ▼
          POST /api/state ──▶ cloud/server.py (EC2 · SQLite · 알림 판단 · SOLAPI)
          ◀── 응답에 설정(수면 시간·N·M·X·외출 모드)을 실어 돌려준다
```

## 2. 확정된 결정 (2026-10-22)

| 항목 | 결정 | 이유 |
|---|---|---|
| LED 판단 | **로컬** | 인터넷이 끊겨도 불이 동작해야 하고, 클라우드 왕복만큼 늦어지면 안 된다 |
| 알림 판단 | **클라우드** | 집 시스템이 죽은 것(정전·인터넷 단절)은 집 밖에서만 알아챌 수 있다. 로컬 SOLAPI 도 어차피 인터넷이 필요하다 |
| 전송 | 상태 변경 시 + **60초 생존 신호**, 같은 요청 | 변경만 보내면 "3시간째 static" 과 "3시간 전에 꺼짐" 을 가를 수 없다 |
| 수면 중 motion | **N초 이상** 이어질 때만 조명 켬 | 뒤척임에 불이 켜지면 잠을 깨운다 |
| 본인 확인 시간 M | **사용자 설정** | — |
| 수면 자세 데이터 | 별도 수집 **안 함** | 시연 범위 |
| 서버 | **EC2 + 보유 도메인** | 접속 정보는 배포 시점에 받는다 |
| 오보 대응 | 하지 않음 | 시연 범위 |

## 3. 규칙

### 3.1 시간 기준

수면 시간은 `HH:MM–HH:MM` 이고 자정을 넘을 수 있다(`00:00–07:00`, `23:00–06:00`).
**모든 시각은 `Asia/Seoul`** 로 계산한다 — EC2 기본 시간대는 UTC 라 그대로 쓰면 9시간 어긋난다.

"이 상태가 얼마나 이어졌나" 는 수면 경계에서 다시 센다:

```text
LED 끄기 판단(수면 중)   : 유지 시간 = now − max(상태 시작, 가장 최근 수면 시작)
응급 판단(깨어 있는 시간): 유지 시간 = now − max(상태 시작, 가장 최근 수면 종료)
```

뒤쪽 덕분에 23:50 에 쓰러져도 수면 시간 동안 시계가 멈췄다가 07:00 부터 다시 세어,
07:00 + N분에 알림이 간다(아침 안부 확인).

### 3.2 조명 (로컬, `realtime/home.py:led()`)

| 상태 | 깨어 있는 시간 | 수면 시간 |
|---|---|---|
| `motion` | 켬 | `held ≥ motion_wake_s` 일 때만 켬 |
| `static` | 켬 | 수면 기준 유지 `≥ sleep_off_min` 이면 끔, 아니면 그대로 |
| `empty` | 끔 | 끔 |
| `deciding` / `suspended` | 그대로 | 그대로 |

### 3.3 알림 (클라우드, 30초마다 검사)

| 조건 | 받는 사람 | 비고 |
|---|---|---|
| 깨어 있는 시간에 `static` 응급 기준 유지 `≥ static_alert_min` | 본인에게 확인 링크 → `confirm_min` 안에 확인 안 하면 **지인 전원** | 상태가 바뀌면 그 건은 닫힌다 |
| `away = true` 이고 `motion` 으로 바뀜 | 본인 | 외출 모드는 웹 설정에서 켠다 |
| 마지막 수신 후 **3분** 경과 | 본인 | "집 시스템 연결 끊김" |

한 조건은 **올라가는 모서리에서 한 번**만 보낸다(`realtime/sms.py` 와 같은 원칙). 조건이
거짓이 되면 다시 무장한다.

### 3.4 설정 기본값 (웹에서 편집, 응답으로 로컬에 내려감)

```json
{"sleep_start": "00:00", "sleep_end": "07:00",
 "motion_wake_s": 5, "sleep_off_min": 10,
 "static_alert_min": 60, "confirm_min": 5,
 "away": false, "contacts": []}
```

시연 때는 `static_alert_min=1`, `confirm_min=1` 로 줄인다.

## 4. 계약

### 4.1 Mac → 클라우드

```text
POST https://<도메인>/api/state
Authorization: Bearer <device_token>
{"state": "static", "since": 1761120000.0, "led": true}
→ 200 {"config": { …3.4 의 형식… }}
→ 400 형식 오류 (`state` 가 아래 어휘 밖, `since` 가 수가 아님)
→ 401 토큰 무효 (웹에서 기기 해제됨) — 로컬은 전송을 멈추고 화면/로그에 알린다
```

- `state` 어휘: `empty`·`static`·`motion`·`deciding`·`suspended`.
- `since` = 상태가 시작된 벽시계 시각(`time.time() - held_s`). 엔진의 단조 시각은 보내지 않는다.
- `led` = 로컬이 판단한 조명 상태. 웹 홈 화면이 보여 주기만 한다(클라우드는 조명을 판단하지 않는다).
- 서버는 직전 상태와 다를 때만 `events` 에 한 줄 넣는다. 매 요청이 `devices.last_seen` 을 갱신한다.
- 직전 수신이 **3분 넘게** 전이면, 새 상태보다 먼저 `offline` 이벤트를 `since=직전 수신 시각` 으로
  넣는다. 기록 막대에 끊긴 구간이 그대로 보인다.
- 전송 실패 시 쌓아 두지 않는다. 다음 요청이 현재 상태를 다시 맞춘다
  (`# ponytail:` 끊긴 동안의 짧은 전환은 기록에서 빠진다 — 필요하면 jsonl 대기열).

### 4.2 Mac → 싱크보드 (USB-Serial-JTAG, host→board)

- 1바이트: `'1'` = 켬, `'0'` = 끔. 다른 바이트는 무시한다.
- `csi_serial_reader` 가 **1초마다 현재 값을 그대로 다시 쓴다** — 보드가 리셋돼도 1초 안에
  복구되고, 변경 추적이 필요 없다.
- 쓰기만 한다. **DTR/RTS 는 건드리지 않는다**(CLAUDE.md).

### 4.3 연동 (홈페이지 → 로컬)

1. 웹 로그인 → `연동 코드 발급` → 6자리 코드, **10분 유효, 1회용**
2. 로컬에서 `python3 -m realtime.home pair <도메인> <코드>`
3. 서버가 코드를 소비하고 `device_token`(32바이트 랜덤) 을 돌려준다. 서버는 **SHA-256 해시만** 저장
4. 로컬은 `mac_collector/home_service.json` (**`.gitignore`**) 에 `{url, token, config}` 저장

## 5. 클라우드 (`cloud/server.py` 한 파일)

- 표준 라이브러리 `http.server`(제어판과 같은 방식) + `sqlite3`. 앞단은 **Caddy**(도메인만
  적으면 HTTPS 인증서 자동). systemd 로 상시 실행.
- 문자 발송은 `realtime.sms.send()` 재사용 — EC2 에 저장소를 clone 하고 `pip install numpy solapi`
  (`sms` → `csi_store` 가 numpy 를 import 한다).

한 계정 = 한 집이다(여러 집은 범위 밖). 그래서 설정은 기기가 아니라 **계정**에 둔다 — 기기를
연동하기 전에도 설정할 수 있고, 기기를 바꿔 끼워도 설정이 남는다.

| 테이블 | 열 |
|---|---|
| `users` | id, email, name, phone, pw_hash(scrypt), salt, config(JSON — 3.4 + `contacts`), created |
| `devices` | id, user_id, name, token_hash, state, since, led, last_seen, created |
| `pair_codes` | code, user_id, expires |
| `events` | device_id, state, since, received |
| `alerts` | id, device_id, kind(`emergency`·`intrusion`·`offline`), token, created, confirmed, escalated, resolved, status |

`alerts.resolved` 가 비어 있는 동안이 "조건이 참인 동안" 이다 — 같은 종류를 다시 만들지 않는다
(올라가는 모서리에서 한 번). `status` 는 `sms.send()` 가 돌려준 문장(`sent 1/1`·`error: …`)이다.
SOLAPI 설정이 없으면 보내지 못하고 `error: API 키가 없습니다` 가 남는다 — 화면은 그대로 동작한다.

### 5.1 화면 (시안 v2 통과, 2026-10-25)

참고: `design/ex.md`(Cal.com). **주소 하나 + 반응형 + 웹앱(PWA)** — 문자로 보내는 확인 링크는
어느 기기에서 열어도 같은 주소여야 하고, 주소를 나누면 화면을 두 벌 만든다.

| 폭 | 내비 | 배치 |
|---|---|---|
| PC (≥ 768px) | 상단 64px, 가운데 알약 묶음(홈·기록·설정·기기) | 홈 5:7 두 단, 설정·기록·기기 1:1 두 단 |
| 휴대폰 (< 768px) | **하단 탭 바**(아이콘+글자, `safe-area-inset-bottom`) | 한 단. 설정의 저장 버튼은 하단에 붙는다 |

- 홈 화면에 추가하면 주소창 없이 열린다: `manifest.json`(`display: standalone`) + `theme-color` +
  `apple-mobile-web-app-capable`.
- 시각 규칙: 흑백 + 검정 주 버튼, 카드 12px(현재 상태 카드만 16px), 큰 제목은 음수 자간,
  글꼴 Pretendard. **색은 상태 표시와 하루 막대에만** 쓴다(빈 방 회색·정지 파랑·움직임 주황·끊김 빨강·
  수면 보라). **어두운(반전) 카드는 응급 확인 하나뿐** — 뜨면 바로 눈에 띈다.
- 밝은/어두운 화면 둘 다(`prefers-color-scheme`). 밤에 휴대폰으로 볼 일이 많다.

| 주소 | 내용 |
|---|---|
| `/` | 로그인(PC 는 왼쪽에 제목 + 상태 카드 모양, 오른쪽에 폼) · `/?signup=1` 가입(이름·이메일·비밀번호·전화) |
| `/home` | (응급 대기 시) 반전 카드 + 남은 시간 + "괜찮아요" · 현재 상태(크게) · 조명/집 기기 마지막 신호/수면 여부 · 외출 모드 · 오늘 막대 · 최근 알림. 15초마다 새로 고침 |
| `/history?date=` | 하루 막대 + 합계(방에 있음·빈 방·알림 수) · 상태 변화 표 · 이날 알림 |
| `/settings` | 수면 시간(시작·끝·조명 끄기 X분·조명 켜기 N초) · 긴급 알림(확인 N분·지인 M분·내 번호) · 지인 추가/삭제 |
| `/devices` | 연동 코드 발급(6자리·남은 시간·붙여 넣을 명령) · 연동된 기기 + 해제 |
| `/c/<token>` | 확인 페이지(로그인 없음) — **GET 은 버튼만 보여 주고, POST 로 확인한다** (문자 앱의 링크 미리보기가 GET 을 먼저 불러 자동 확인되는 것을 막는다) |
| `POST /api/pair` | `{code, name}` → `{token}` (로컬 `pair` 명령이 부른다) |

보안: 비밀번호 `hashlib.scrypt`, 세션 쿠키 `HttpOnly; Secure; SameSite=Lax` + HMAC 서명,
기기·확인 토큰은 `secrets.token_urlsafe`. 문자 본문은 단문 90바이트 안에 들어가게 짧게 쓴다.

## 6. 파일

| 파일 | 변경 |
|---|---|
| `esp32s3_csi_sink/main/app_main.c` | USB 읽기 task → LED. `LED_GPIO` 상수 |
| `esp32s3_csi_sink/main/idf_component.yml` | `espressif/led_strip` (내장 RGB 는 WS2812) |
| `scripts/csi_serial_reader.py` | 1초마다 세션 폴더 `realtime_status.json` 의 `led` 를 읽어 1바이트 쓰기 |
| `realtime/home.py` **(신규)** | 시간창 계산·`led()`·`alert_due()`·클라우드 전송 스레드·`pair` CLI |
| `realtime/engine.py` | `view()` 에 `led`, 실시간일 때만 전송 스레드 시작(재생은 과거라 보내지 않는다) |
| `cloud/server.py` **(신규)** | 5장 전체 |
| `tests/test_home.py` **(신규)** | 시간창(자정 넘김)·`led()` 표·응급 시계 멈춤·모서리 1회 |
| `tests/test_cloud.py` **(신규)** | 가입 → 코드 → 연동 → 상태 전송 → 응급 → 확인 링크 → 확대 발송, 실제 서버를 띄워 HTTP 로 |
| `.gitignore` | `mac_collector/home_service.json`, `cloud/*.db`, `cloud/secret` |
| `doc/realtime-inference.md` 등 | 구현 후 CURRENT 로 옮기고 이 문서는 기록으로 남긴다 |

## 7. 일정

### 1일 차 (10/23) — 조명

- [ ] `realtime/home.py`: `in_sleep()`, `last_boundary()`, `led()` + `tests/test_home.py`
- [ ] `engine.py`: 설정 파일(없으면 기본값)을 읽어 `view()["led"]`
- [ ] 싱크 펌웨어: `usb_serial_jtag_read_bytes` task → LED. 수신·전달 task 우선순위를 건드리지 않는다
- [ ] reader: LED 바이트 1초 주기 쓰기
- [ ] 보드 확인: `LED_GPIO` 가 DevKitC v1.0 은 48, v1.1 은 38 — 실제 보드로 맞춘다
- **완료 기준**: 손을 흔들면 LED 켜짐, 방을 비우면 꺼짐. 수면 시간을 지금으로 바꾸면 static X분 뒤 꺼짐.
  싱크 수신률(`SINK_STATUS` recv/sent)이 LED 추가 전과 같다.

### 2일 차 (10/24) — 클라우드

- [x] `cloud/server.py`: 가입·로그인·연동 코드·`/api/state`·설정·기록 — **2026-10-25** 보드 없이 먼저
  했다. 3일 차의 알림 검사 루프·확인 페이지·지인 확대 발송까지 같은 파일에 들어갔다.
  `tests/test_cloud.py` 6개 + `tests/test_home.py` 6개 통과. 시연 데이터를 넣어 PC(1280)·휴대폰(375)·
  밝은/어두운 화면에서 7개 화면을 확인했다. SOLAPI 키가 없는 환경이라 실제 발송은 확인하지 않았다
  (`alerts.status` 에 `error: API 키가 없습니다` 가 남는다).
- [ ] `realtime/home.py`: `pair` CLI, 전송 스레드(상태 변경 즉시 + 60초) — 시간 규칙(`in_sleep`·
  `last_boundary`·`emergency_held`)만 먼저 들어갔다
- [ ] 로컬에서 서버를 띄워 연동 → 기록 화면에 전환이 쌓이는지 확인
- **완료 기준**: 웹에서 수면 시간을 바꾸면 60초 안에 로컬 LED 판단이 따라간다.

### 3일 차 (10/25) — 알림·배포·리허설

- [ ] 알림 검사 루프(30초) + `sms.send` + 확인 페이지 + 지인 확대 발송
- [ ] EC2 배포: 보안 그룹 80/443, 도메인 A 레코드 → Elastic IP, Caddy, systemd
- [ ] 시연 리허설 (8장)
- **완료 기준**: 8장 시나리오가 끝까지 한 번에 돈다.

## 8. 시연 시나리오

1. 웹 로그인 → 연동 코드 → 로컬 등록 → 홈 화면에 현재 상태가 뜬다
2. 들어가 움직인다 → LED 켜짐, 기록에 `motion`
3. 나간다 → LED 꺼짐, 기록에 `empty`
4. 외출 모드 켜고 들어간다 → 본인에게 침입 문자
5. `static_alert_min=1` 로 두고 가만히 있는다 → 본인 확인 문자 → 누르지 않고 1분 → 지인 문자
6. 수면 시간을 현재 시각으로 → 가만히 있으면 X분 뒤 LED 꺼짐, 뒤척임(짧은 motion)에는 안 켜짐
7. Mac 의 Wi-Fi 를 끈다 → 3분 뒤 "연결 끊김" 문자

## 9. 하지 않는 것

이메일 인증, 비밀번호 재설정, 로그인 시도 제한, 여러 집, 앱 푸시, 위치 기반 외출 감지,
차트 라이브러리, 기존 로컬 문자 알림(`realtime/sms.py` Watcher) 제거 — 꺼 둔 채 둔다.
