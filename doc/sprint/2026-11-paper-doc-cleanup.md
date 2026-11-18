# 스프린트: 논문 준비 — 문서 레거시 정리 (2026-11-18 ~ )

> 상태: **CURRENT** — 작업 일지

프로젝트를 마치고 논문을 쓰기 전에, 문서에 남은 레거시 서술을 정리한다.
원칙: **시도·실측 기록은 지우지 않는다.** 그 기록을 "현재 상태"처럼 쓴 문장만 고친다 —
측정 기록 절은 `HISTORICAL` 로 표시하고, 구현 전 시제("지금은 ~한다")는 과거형으로 바꾼다.

## 2026-11-18 — 1차: 모델 문서를 제외한 문서

범위: 루트 `README.md`, `CLAUDE.md`, `doc/` 상위 문서, `doc/plans/` 상태 줄, `scripts/README.md`,
`esp32s3_csi_recv_poc/README.md`. 제외: `model_train/docs/`, 각 문서의 모델 수치·전처리 설계 문단,
`doc/sprint/`·`doc/troubleshooting/` 개별 항목(이미 시간순 기록).

코드와 대조해 고친 사실(대표):

| 문서 | 레거시 서술 | 코드 기준 현재 |
|---|---|---|
| README·quickstart·scripts/README | reader 출력이 JSONL, `session_<id>` 경로·append | `.csi` 배타적 생성, `<HHMMSS>_<label>_s<id>`, JSONL 은 내보내기 |
| quickstart | 수동 reader `--session-id --output-dir` | 이 인자는 없다 — `--session-dir`, `device_id` 는 IDENT |
| quickstart·scripts/README | CLI 메뉴 4~5개(전체 가이드·사전 점검) | 9개 항목 (`meshsense_cli._usb_menu_items`) |
| architecture | 수집 시 포트마다 chip MAC 읽기, 기본 60초 | 포트 프로브 없이 IDENT, 기본 300초 + 지연 10초 |
| architecture·firmware | `POC_DUMP_CSV=0`, ESP_LOG 가 USB 로 섞임, 32-byte header | 상수 없음, ESP_LOG 는 GPIO43 UART, 44-byte v4 |
| firmware | ring buffer 가득 차면 새 프레임 버림, reader 가 amplitude 저장 | keep-newest, reader 는 raw I/Q 만 |
| data-schema | IDENT 카운터 USB 모드만, manifest 예시의 `fw_uart_*` | 업링크 모드 카운터 추가, 현재 키 `fw_sent`·`fw_send_fail`·`sink_*` |
| collection-protocol·postprocessing | 제어판 '실험 정보'·'보드' 탭, 라벨 선택, `action` 라벨 | 수집 탭 접힌 설정·준비 줄, 블록 순서가 라벨을 정함, `motion` |
| realtime-uplink | 싱크가 RX 겸용, 다중 RX 미검증, 실시간 추론 PLANNED | 싱크는 CSI 캡처 안 함, 09-03·09-10 실측, realtime CURRENT |
| control-panel·quickstart·doc/README | 탭 4개 | 탭 5개 (연동 2026-10-25) |
| plans/home-service | PLANNED | 구현·EC2 배포 완료, 이 문서가 정본 |

`HISTORICAL` 로 표시한 측정 기록: realtime-uplink 의 1-RX·2-RX·3-RX 실측 절, sequence-patterns §6,
postprocessing §4.7 의 `20260616/session_1` 결과. ADR-0001 은 본문을 고치지 않고 "이후 변경" 절을 덧붙였다.

검증: 범위 안 Markdown 상대 링크 전부 존재(깨진 링크 0). Python 코드는 바꾸지 않았다.

## 2026-11-18 — 2차: 코드 주석·도움말 (모델 코드 제외)

1차에서 남겨 둔 코드 쪽 레거시를 고쳤다. 동작은 바꾸지 않되, **호출하는 곳이 없는 코드와 항상 실패하는
경로**는 주석으로 설명하는 대신 지웠다.

| 위치 | 레거시 | 조치 |
|---|---|---|
| `esp32s3_csi_recv_poc/main/app_main.c` 머리 주석 | 프레임 `v3`, 없는 문서 `doc/pipeline/usb-collection.md`·`doc/overview/csi-rate-troubleshooting.md` | `v4`, `doc/data-schema.md`·`doc/troubleshooting/01-csi-rate.md`, 업링크 모드 한 줄 |
| `csi_serial_reader.py`·`csi_store.py` docstring | 프레임 `v3`, 헤더 `40B`, 없는 `model_train/model/Preprocessing` | `v4`, `44B`, 실제 사용처(export_jsonl·realtime) |
| `measure_csi_hz.py`·`visualize_csi.py` docstring·description | 입력이 JSONL | `.csi` 우선, 구 JSONL 폴백 |
| `visualize_tx_seq_overlap.py --session-dir` 도움말 | `session_<id>` JSONL 폴더만 | `.csi` 세션은 `export_jsonl.py` 출력 폴더를 넣는다고 명시 |
| `meshsense_cli.py` 문구 | "esp-csi PoC", "[PoC] 보드 플래시", "메인 메뉴 [2] 플래시"(실제 [1]), "'수집 (USB 시리얼)'"(실제 항목명 다름), "IDENT 프레임 v3", "파이프라인 선택으로 돌아가기"(실제로는 CLI 종료), `--quick` "전체 가이드" | 현재 메뉴·동작에 맞춤. 마지막 항목은 `종료` |
| `meshsense_cli.py` 죽은 코드 | `_flash_board`(삭제된 `flash_tx.py`·`flash_rx.py` 호출), `_print_preflight_report`·`_registry_row`·`_PreflightRow`(옛 "사전 점검" 메뉴), `_viz_venv_status` — 호출 0곳 | 삭제 |
| `visualize_csi.py` 인자 없이 실행 | `session_meta.yaml` 의 `session_id` 를 읽는 폴백 — 이 키는 의도적으로 없어 항상 실패 | `--session-meta` 와 폴백 삭제, `--session-dir`·`--session-id` 중 하나 필수 (`--session-id` 는 새 레이아웃 `*_s<id>` 도 찾으므로 유지) |
| `session_meta.read_session_id` | 위 폴백 외 사용처 0 | 삭제, 모듈 docstring 을 `label_target` 읽기로 |
| `idf_env.idf_diagnose` docstring | "사전 점검" | 실패 메시지용 |

문서도 맞췄다: CLI 마지막 항목 `[9] 종료` (quickstart §4·scripts/README·control-panel §7).
`_train_run.py` 의 `v1/v2/v3` 는 모델 버전이라 그대로 두었다.

검증: 바꾼 Python 파일 `py_compile` 통과. host 테스트 8개 모듈 201건 중 200건 통과, skip 2 —
실패 1건 `test_control_panel.TrainScreenTest.test_each_date_reports_how_many_models_used_it` 는 실제
`mac_collector_output/raw/` 를 읽어 `20260920` 날짜를 기대하는데 이 컴퓨터에는 그 폴더가 없어 나는
`StopIteration` 이다(이번 변경과 무관, 데이터 의존). `visualize_csi.py` 무인자 → 사용법 오류,
`--session-id 73` → `raw/20261109/204518_live_s73` 를 찾는 것을 확인.

### 남은 것

- 로컬에만 남은 옛 펌웨어 빌드 폴더 `esp32s3_csi_sender/`·`esp32s3_tx_ap_node/` (git 추적 없음, `build/` 만 남음).
- 위 데이터 의존 테스트 — 9/19·20 세션이 없는 컴퓨터에서는 항상 실패한다.
