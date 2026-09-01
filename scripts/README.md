# MeshSense 호스트 스크립트

호스트(Mac)에서 쓰는 CLI·플래시·registry·수집 보조 도구 모음입니다.
최초 셋업 명령은 [quickstart.md](../doc/overview/quickstart.md) §0이 정본입니다.

## CLI 메뉴 맵

**터미널이 익숙하지 않다면 브라우저 제어판을 쓰세요** — 보드 확인·플래시·수집·세션 관리·
진단을 전부 화면에서 할 수 있습니다:

```bash
python scripts/meshsense_gui.py           # 브라우저 제어판 (권장)
```

```bash
python scripts/meshsense_cli.py           # 메인 메뉴
python scripts/meshsense_cli.py --quick   # 안내 문구 없이 메뉴만
python scripts/meshsense_cli.py --guide   # AP 파이프라인 전체 가이드 바로 시작
```

```text
메인
├─ [1] USB 수집 (esp-csi PoC · USB 시리얼 100Hz)   ← 학습 데이터 정본 경로
│    ├─ [1] 보드 플래시 (PoC, MAC 자동 매칭)
│    ├─ [2] 수집 (라벨·시간 입력, session_id 자동 순번)
│    ├─ [3] 세션 메타 편집 (브라우저 폼)
│    └─ [4] 보드 관리 (registry 등록·검증)
├─ [2] AP 실시간 수집 (SoftAP + UDP)              ← deprecated
│    ├─ [1] 전체 가이드 · [2] 보드 플래시 · [3] 수집기 실행
│    └─ [4] 사전 점검 · [5] 보드 관리
└─ [3] 종료
```

- **수집([1]→[2])은 포트를 프로브하지 않는다.** 연결된 모든 시리얼 포트에 reader를 붙이고,
  보드가 2초마다 보내는 IDENT 프레임의 eFuse MAC으로 `device_id`를 정한다. RX가 아닌 포트는
  IDENT가 오지 않아 자동으로 빠진다. 예전에는 `esptool read_mac`으로 포트를 프로브했는데,
  그게 DTR/RTS로 보드를 리셋시켜 TX의 `tx_seq`까지 되감았다
- **`session_id` 는 자동 순번**이다 — 기존 세션 이름의 `_s<N>` 최댓값+1. 사람이 YAML 을 고칠 일이 없다
- 플래시는 USB MAC으로 `tx_registry.csv`/`device_registry.csv`를 조회해 자동 분기 (여기선 esptool 사용이 정상)
- 플래시 완료 여부는 `mac_collector/flash_state.json`(●/○)에 기록되어 보드 관리에 표시
- 다른 Mac 온보딩 시 `idf_bootstrap.py -y` 후 **[2]→[4] 사전 점검** 권장
  ([esp-idf-troubleshooting.md](../doc/overview/esp-idf-troubleshooting.md))

## 스크립트 목록

| 파일 | 설명 |
|------|------|
| `meshsense_cli.py` | 메뉴 CLI — 두 파이프라인의 플래시·수집·registry·사전 점검 |
| `flash_tx.py` / `flash_rx.py` | (deprecated) AP 파이프라인 TX/RX 플래시 |
| `csi_store.py` | **프레임 규격·검증·진폭·유효 서브캐리어의 Python 단일 소스** (공용 I/O) |
| `csi_session.py` | 세션 디렉터리·매니페스트(`session.json`, 라벨 SSOT)·`session_id` 자동 순번 |
| `meshsense_gui.py` | **브라우저 제어판** — 보드·수집·세션·진단·실험정보 (stdlib only, 127.0.0.1) |
| `session_form.py` | `session_meta.yaml` 편집용 로컬 웹 폼 (제어판의 '실험 정보' 탭과 같은 내용) |
| `check_separability.py` | 3-class 가 실제로 갈리는지 세션 단위 교차검증으로 진단 (torch 불필요) |
| `_idf_flash.py` / `_collect_run.py` | 제어판이 백그라운드로 실행하는 플래시·수집 러너 |
| `csi_serial_reader.py` | USB reader: 시리얼 프레임 → `device_<id>.csi` (pyserial) |
| `visualize_csi.py` | 세션 → CSI 워터폴 PNG (`.venv` 필요) |
| `measure_csi_hz.py` | 세션 → RX별 Hz·gap·crc_fail·boot_changes·tx_seq 커버리지 요약 |
| `meshsense_config.py` / `meshsense_config.example.json` | (deprecated) AP 경로 망 설정 |
| `registry_core.py` | RX/TX registry CSV 공통 로직 (load/save/verify) |
| `registry.py` / `device_registry.py` | RX registry 라이브러리 / CLI |
| `tx_registry.py` | TX registry 라이브러리 + CLI |
| `session_meta.py` | `session_meta.yaml` `session_id`·`label_target` 파서 |
| `flash_state.py` | `flash_state.json` 플래시 완료 추적 |
| `esptool_mac.py` | esptool로 USB MAC 읽기 |
| `idf_bootstrap.py` | esp-idf submodule + `install.sh esp32s3` → `.espressif/` 마커 |
| `idf_env.py` / `idf_paths.py` / `idf_util.py` | `export.sh` 래핑·경로 상수·`idf.py` subprocess |

## 수동 플래시 (AP 파이프라인 — deprecated)

```bash
python scripts/tx_registry.py add --port /dev/cu.usbmodem101 --board-name TX1
python scripts/flash_tx.py -p /dev/cu.usbmodem101 --monitor

python scripts/device_registry.py add --port /dev/cu.usbmodem102 --board-name RX1
python scripts/flash_rx.py -p /dev/cu.usbmodem102
```

- 전역 `~/esp/esp-idf`만 쓰려면 `--skip-idf-bootstrap`
- 보드 전환 시 `--clean -y`
- USB 파이프라인 플래시는 CLI `[1]→[1]` ([usb-collection.md](../doc/pipeline/usb-collection.md))

## meshsense_config.json (deprecated — AP 경로 전용)

| 키 | 용도 |
|----|------|
| `ap.ssid` / `ap.pass` | TX SoftAP = RX STA 접속 Wi-Fi |
| `ap.channel` / `ap.max_conn` | SoftAP 설정 |
| `ap.beacon_interval_tu` / `ap.espnow_interval_ms` | 비콘·ESP-NOW 주기 |
| `collector.ip` / `collector.port` | RX → Mac 수집기 UDP 목적지 |
| `rx.espnow_only` | true면 ESP-NOW 프레임 CSI만 전송 (`CSI_ESPNOW_ONLY=1`) |

## Registry

| 대상 | 파일 | CLI |
|------|------|-----|
| RX | `mac_collector/device_registry.csv` | `python scripts/device_registry.py` |
| TX | `mac_collector/tx_registry.csv` | `python scripts/tx_registry.py` |

## TODO

- [ ] `mac_collector` ↔ `scripts` ↔ `model_train` 패키지화 (`sys.path` 조작 제거)
- [ ] 실시간 경로: ESP-NOW 업링크 + USB 싱크 보드
  ([sprint/2026-08-collection-hardening.md](../doc/sprint/2026-08-collection-hardening.md))
