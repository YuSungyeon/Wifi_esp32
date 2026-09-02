# 빠른 시작

> 상태: **CURRENT**
> 공식 경로: ESP-NOW TX → CSI RX → USB-Serial-JTAG → JSONL

## 1. 최초 준비

```bash
git submodule update --init esp-idf
python3 scripts/idf_bootstrap.py -y
python3 -m pip install pyserial
```

시각화까지 사용할 경우:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-viz.txt
```

## 2. Registry 확인

```bash
python3 scripts/tx_registry.py verify
python3 scripts/device_registry.py verify
```

미등록 보드는 USB로 한 대씩 연결해 등록한다.

```bash
python3 scripts/tx_registry.py add \
  --port /dev/cu.usbmodem101 --board-name TX1

python3 scripts/device_registry.py add \
  --port /dev/cu.usbmodem102 --board-name RX101
```

## 3. Session metadata 작성

브라우저 폼으로 입력한다 — YAML 을 손으로 고치면 들여쓰기·키 이름을 틀리기 쉽고, 틀려도
수집이 그냥 돌아간다.

```bash
python scripts/session_form.py        # 또는 제어판의 '실험 정보' 탭
```

기록 항목: 실험일, 방 크기·설명, 실험 목표, 참여 RX, 운영자·현장 메모, 그리고
**다음 수집의 기본 라벨**(`label_target`).

- **`session_id` 는 여기 없다.** 기존 세션의 `_s<N>` 최댓값+1 로 자동 부여된다
  (`csi_session.next_session_id`). run ID 를 사람이 관리하지 않는다.
- **라벨은 수집 시작 시 고른 값이 정본**이다. `label_target` 은 그 프롬프트의 기본 선택일 뿐이며,
  실제 값은 세션의 `session.json` 에 박힌다.

## 4. 수집 실행

### 제어판 (권장)

```bash
python scripts/meshsense_gui.py
```

127.0.0.1 에만 열리는 로컬 웹 UI다. 표준 라이브러리만 쓴다.

1. **보드** — 연결된 보드가 자동 식별된다. 보드가 2초마다 보내는 IDENT 로 확인하므로
   **확인만으로는 보드가 리셋되지 않는다.** TX 는 아무것도 보내지 않아 'RX 아님' 으로
   표시되는 것이 정상이다. 필요하면 `RX 플래시` / `TX 플래시`
2. **수집** — 라벨(`empty`/`static`/`motion`)과 시간을 고르고 시작. 연결된 모든 포트에
   reader 를 붙이고, RX 가 아닌 포트는 스스로 빠진다 (`rc=2`)
3. **세션** — 결과·품질·CSI 워터폴 확인
4. **진단·데이터셋** — 라벨당 2세션 이상 모이면 분리 가능성 진단

### 터미널

```bash
python3 scripts/meshsense_cli.py      # [1] USB 수집
```

`[1] 보드 플래시` / `[2] 수집` / `[3] 세션 메타 편집` / `[4] 보드 관리`.

TX 는 전원만 있으면 동작하므로 USB 포트가 모자라면 충전기에 꽂아두고 RX 만 연결한다
([firmware.md](firmware.md) §9).

## 5. 결과 확인

```text
mac_collector_output/raw/<YYYYMMDD>/<HHMMSS>_<label>_s<session_id>/
    device_<id>.csi   session.json   session_meta_snapshot.yaml   csi_waterfall.png
```

```bash
python scripts/measure_csi_hz.py mac_collector_output/raw/<날짜>/<세션>
```

정상 기준: `hz≈100(±3)`, `gaps>200ms=0`, `crc_fail=0`, `invalid=0`, `resync=0`,
`seq_gap=0`, `boot_changes=0`, `tx_back=0`, `tx_cov>0.99`.
RX 가 2대 이상이면 마지막 줄의 `cross-RX 공통 tx_seq` 가 98% 이상이어야 한다.

`tx_back>0` 이면 수집 중 TX 가 재부팅한 것이라 시간 격자가 깨졌다 — 재수집해야 한다.
`boot_changes>0` 은 RX 재부팅이며 그 구간만큼 구멍이 생긴다.

## 6. 수동 실행

CLI를 쓰지 않을 경우 TX/RX firmware를 각각 build/flash한 뒤 RX마다 reader를 실행한다.

```bash
python3 scripts/csi_serial_reader.py \
  --port /dev/cu.usbmodem102 \
  --device-id 101 \
  --session-id 31 \
  --output-dir mac_collector_output
```

수동 reader는 session metadata snapshot을 자동 복사하지 않는다. 재현 가능한 실험에는 CLI 사용을 권장한다.

## 7. 실패 시 확인 순서

1. TX 전원과 100Hz 송신 로그
2. RX firmware flash 여부
3. RX monitor 종료 여부
4. USB port와 registry MAC 일치
5. `pyserial` 설치
6. `session_meta.yaml` 존재 여부
7. reader log의 `invalid`, `seq_drop`, `last_tx_seq`

ESP-IDF·port 오류는 [문제 해결](troubleshooting/README.md)을 참조한다.
