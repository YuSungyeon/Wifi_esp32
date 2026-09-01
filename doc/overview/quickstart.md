# 빠른 시작

## 0. 호스트 설정 (최초 1회 — 이 블록이 셋업 명령의 유일한 정본)

```bash
git clone --recursive <repo-url>
cd Wifi_esp32
python scripts/idf_bootstrap.py -y   # esp-idf/ + ~/.espressif (최초만 10–30분)

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-viz.txt  # numpy·matplotlib·pyserial (수집·시각화·후처리)
```

- 이미 clone한 경우: `git submodule update --init esp-idf`
- `idf.py`/빌드 오류: [esp-idf-troubleshooting.md](esp-idf-troubleshooting.md)
- 수집 전 실험 조건(방·운영자·메모)은 **브라우저 폼**으로 입력합니다:
  `python scripts/session_form.py` (또는 CLI `[1] USB 수집 → [3] 세션 메타 편집`).
  `session_id` 순번은 자동이라 손댈 필요가 없습니다

## 1. 실행 방법 — 제어판 또는 CLI

```bash
python scripts/meshsense_gui.py    # 브라우저 제어판 (터미널이 익숙하지 않다면 이쪽)
python scripts/meshsense_cli.py    # 터미널 메뉴
```

제어판은 보드 확인·플래시·수집·세션 관리·분리 가능성 진단·데이터셋 생성을 한 화면에서
합니다. 127.0.0.1 에만 열리고 표준 라이브러리만 씁니다.

| 메뉴 | 파이프라인 | 언제 쓰나 |
|------|-----------|----------|
| **[1] USB 수집** | RX 보드를 USB로 연결, 시리얼로 100Hz 수집 | **모델 학습 데이터 수집 — 정본 경로.** 손실 0%, Wi-Fi 설정 불필요 |
| **[2] AP 실시간 수집** | TX SoftAP + RX UDP 무선 전송 | **deprecated** — 실측 0.18~22.8Hz ([ap-realtime.md](../pipeline/ap-realtime.md)) |

AP 경로 설정이 필요하면 `cp scripts/meshsense_config.example.json scripts/meshsense_config.json`.
USB 경로는 config 없이 동작합니다.

## 2-A. USB 수집 경로 (정본)

제어판 기준:

1. **보드** 탭 — 연결된 보드가 자동 식별됩니다. 미등록이면 registry 에 먼저 추가하고,
   `RX 플래시` / `TX 플래시` 로 펌웨어를 굽습니다 (RX 펌웨어는 보드마다 동일한 바이너리)
2. **실험 정보** 탭 — 방 크기·운영자·메모 기록 (`session_id` 순번은 자동)
3. **수집** 탭 — 라벨과 시간을 고르고 시작. 연결된 모든 포트에 reader 를 붙이고,
   RX 가 아닌 포트는 자동으로 빠집니다
4. **세션** 탭 — 수집 결과·품질·CSI 파형 확인
5. **진단·데이터셋** 탭 — 라벨당 2세션 이상 모이면 분리 가능성 진단 → 데이터셋 생성

CLI 로는 `[1] USB 수집` 아래 `[1] 보드 플래시` / `[2] 수집` / `[3] 세션 메타 편집` /
`[4] 보드 관리` 가 같은 일을 합니다.

수집 결과는 `mac_collector_output/raw/<날짜>/<시각>_<라벨>_s<id>/`에 쌓입니다.
품질 확인: `python scripts/measure_csi_hz.py <세션 디렉터리>`.
상세와 수동 명령: [usb-collection.md](../pipeline/usb-collection.md)

## 2-B. AP 실시간 수집 경로 (deprecated)

CLI `[2] AP 실시간 수집 → [1] 전체 가이드` 가 아래 순서를 단계별로 안내합니다
(`python scripts/meshsense_cli.py --guide` 로 바로 시작).

1. TX 등록·플래시: `tx_registry.py add` → `flash_tx.py`
2. Mac Wi-Fi를 TX SoftAP(`ap.ssid`)에 접속, IP 확인
3. 수집기 실행 (메뉴 `[3] 수집기 실행`)
4. RX 등록·플래시: `device_registry.py` → `flash_rx.py`

상세와 수동 명령: [ap-realtime.md](../pipeline/ap-realtime.md) · [collector.md](../mac-collector/collector.md)

## 3. 후처리·학습

```bash
python model_train/model/Preprocessing.py    # 최신 세션 자동 선택, X=(N, 300, RX수×52)
python model_train/model/LSTM.py --epochs 20 # 전처리 + LSTM 학습 (PyTorch 필요)
```

상세: [pipeline.md](../postprocessing/pipeline.md) · [lstm-design.md](../postprocessing/lstm-design.md)
