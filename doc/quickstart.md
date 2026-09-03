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

[`mac_collector/session_meta.yaml`](../mac_collector/session_meta.yaml)에서 최소 다음을 확인한다.

- `session_id`: 같은 날짜에 재사용하지 않는 run ID
- `experiment.label_target`: 이번 session label
- `experiment.split_strategy`: train/validation/test 구분
- `devices.expected_device_ids`: 연결할 RX 목록을 기록하는 metadata. 현재 CLI filter는 아님
- `operator.notes`: 현장 조건
- `acquisition`: 현재 ESP-NOW/USB 설정과 일치

## 4. CLI 실행

```bash
python3 scripts/meshsense_cli.py
```

처음이면 **[1] 전체 가이드**를 선택한다.

### TX

1. TX 보드만 USB에 연결한다.
2. 보드 플래시에서 TX registry와 자동 매칭한다.
3. TX firmware를 flash한다.
4. TX가 ESP-NOW를 계속 송신하도록 전원을 유지한다.

TX monitor에서 다음 형태의 로그를 확인할 수 있다.

```text
csi_send: wifi_channel: 11, send_frequency: 100
```

### RX

1. RX를 한 대씩 USB에 연결해 receiver firmware를 flash한다.
2. 수집할 때는 모든 RX를 USB로 Mac에 연결한다.
3. RX의 `idf.py monitor`는 반드시 닫는다. Reader와 같은 포트를 동시에 열 수 없다.

### 수집

메뉴 **[4] USB 시리얼 수집**을 선택한다.

CLI가 수행하는 작업:

- USB port별 chip MAC 확인
- RX registry와 매칭된 보드만 선택
- `session_meta.yaml`의 session ID 읽기
- session metadata snapshot 저장
- RX별 serial reader 병렬 실행
- reader log 저장
- 종료 후 waterfall 생성

실제 수집 대상은 USB에 연결되어 있고 RX registry와 일치하는 모든 보드다. `devices.expected_device_ids`와 실제 연결 목록의 자동 대조는 아직 구현되지 않았다.

Mac은 TX가 만든 Wi-Fi network에 연결할 필요가 없다. UDP port나 collector IP 설정도 없다.

## 5. 결과 확인

```text
mac_collector_output/raw/YYYYMMDD/session_<id>/
├── device_<id>.jsonl
├── session_meta_snapshot.yaml
└── csi_waterfall.png
```

수집률:

```bash
python3 scripts/measure_csi_hz.py \
  mac_collector_output/raw/YYYYMMDD/session_<id>
```

수집률은 RX callback의 `timestamp_us`만을 기준으로 계산한다.

시각화 재실행:

```bash
.venv/bin/python scripts/visualize_csi.py \
  --session-dir mac_collector_output/raw/YYYYMMDD/session_<id>
```

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

ESP-IDF·port 오류는 [문제 해결](troubleshooting.md)을 참조한다.
