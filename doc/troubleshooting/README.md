# 트러블슈팅 — 종류별 · 시간순

> 상태: **HISTORICAL + CURRENT** — 각 항목은 발생 당시 기록이고, "재발 방지"에 적힌 규칙은 현재 코드에 반영되어 있다.

같은 종류의 문제는 같은 파일에, 파일 안에서는 **시간순**으로 쌓는다. 새 문제를 겪으면
해당 파일 맨 아래에 같은 형식으로 덧붙인다. 시간순 작업 일지는
[sprint/2026-08-collection-hardening.md](../sprint/2026-08-collection-hardening.md)에 따로 있다 —
여기는 그 일지를 **종류별로 다시 정리한 참조용**이다.

| 파일 | 종류 | 대표 사례 |
|---|---|---|
| [00-esp-idf-build-port.md](00-esp-idf-build-port.md) | 환경·빌드·포트 | `IDF_PATH` 없음, port busy, MAC 읽기 실패 |
| [01-csi-rate.md](01-csi-rate.md) | 수집률(Hz) | AP 경로 22Hz 천장 → PoC 100Hz, 무선 업링크 99.6Hz |
| [02-serial-stream.md](02-serial-stream.md) | 시리얼 스트림·프레임 무결성 | 오탐 magic, ring buffer 4B 패딩, TX 브로드캐스트 혼입 |
| [03-board-reset-usb.md](03-board-reset-usb.md) | 보드 리셋·USB-Serial-JTAG 특성 | esptool 프로브 리셋, DTR/RTS 리셋, 백로그 |
| [04-session-labels.md](04-session-labels.md) | 세션·라벨·데이터 무결성 | append 혼입, 라벨 미기록, 하드코딩 범위 오분류 |
| [05-signal-features.md](05-signal-features.md) | 신호 처리·특징 | 서브캐리어 상수 0, gain 보정 되돌림 |
| [06-measurement-pitfalls.md](06-measurement-pitfalls.md) | 측정·진단 방법론 함정 | 백로그로 부풀린 Hz, 세션 혼합 AUC |
| [07-host-tools.md](07-host-tools.md) | reader·CLI·GUI 도구 버그 | 무한 대기, 폼 입력 소실, 종료 코드 오분류 |
| [08-wireless-uplink.md](08-wireless-uplink.md) | 실시간 무선 경로 | MAC 충돌, 싱크 스트림 오염 |

## 항목 형식

```markdown
## YYYY-MM-DD — 제목
- **증상**: 무엇이 관측됐나 (수치)
- **원인**: 왜 그랬나
- **시도·되돌림**: 틀린 시도가 있었으면 그것도 남긴다
- **해결**: 무엇을 바꿨나 (코드 위치)
- **재발 방지**: 규칙 · 자동 검사
```

## 전체 시간표

| 날짜 | 무슨 일 | 파일 |
|---|---|---|
| 2026-05-22 | AP 경로 17→22Hz 천장, 11단계 디버깅 끝에 경로 폐기 | 01 |
| 2026-05-22~23 | esp-csi PoC 로 교체, 97.5→100Hz, USB 스트리밍 손실 0% | 01 |
| 2026-08-25 | 조사: 라벨 미기록·append 혼입·오탐 프레임·서브캐리어 상수 0 발견 | 02 04 05 |
| 2026-08-25 | 프레임 v3(CRC32·IDENT·boot_id), 세션/라벨 SSOT, reader 재작성 | 02 03 04 07 |
| 2026-08-26 | 실보드: DTR 리셋·백로그·ESP_LOG 미출력 — 가상 검증으로 못 잡던 3건 | 02 03 |
| 2026-08-26 | gain 보정 되돌림, TX 재부팅 감지, RX 2대 cross-RX 99.8% | 05 03 01 |
| 2026-08-26 | 881Hz 오측정 — 측정 스크립트가 백로그 IDENT 를 읽음 | 06 |
| 2026-09-02 | 모델 레포 병합: 포맷 충돌 → JSONL 내보내기, 라벨 어휘·범위 하드코딩 | 04 |
| 2026-09-02 | 무선 업링크 1-RX 99.58Hz — ring buffer 패딩, TX 브로드캐스트 혼입 | 08 02 06 |
| 2026-09-02 | 분리 진단 도구: AUC 부풀림, 잡음 특징 지배 | 06 |
