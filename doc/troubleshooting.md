# ESP-IDF와 USB 문제 해결

> 상태: **CURRENT** — ESP-NOW TX/RX firmware 기준

## 1. 표준 환경

- ESP-IDF source: `esp-idf/` submodule, v5.2.2
- toolchain/Python env: 기본 `~/.espressif/`
- target: `esp32s3`
- firmware: `esp32s3_csi_send_poc`, `esp32s3_csi_recv_poc`

최초 한 번:

```bash
git submodule update --init esp-idf
python3 scripts/idf_bootstrap.py -y
```

확인:

```bash
python3 scripts/meshsense_cli.py
# [5] 사전 점검
```

## 2. `IDF_PATH` 또는 `idf.py`를 찾지 못함

프로젝트 script는 `esp-idf/export.sh`와 ESP-IDF Python env를 자동으로 사용한다. 수동 shell에서는:

```bash
source esp-idf/export.sh
idf.py --version
```

계속 실패하면:

```bash
python3 scripts/idf_bootstrap.py -y
```

다른 ESP-IDF source를 사용할 때만 `MESHESENSE_IDF_PATH`를 설정한다.

```bash
MESHESENSE_IDF_PATH=/path/to/esp-idf python3 scripts/meshsense_cli.py
```

## 3. target 또는 build cache 문제

프로젝트별로 실행한다.

```bash
cd esp32s3_csi_send_poc
idf.py fullclean
idf.py set-target esp32s3
idf.py build
```

RX도 같은 순서로 `esp32s3_csi_recv_poc`에서 실행한다. TX build directory를 RX에 복사하거나 반대로 사용하지 않는다.

## 4. USB port가 없음

```bash
ls /dev/cu.usbmodem*
```

없으면 다음을 확인한다.

- data 통신이 가능한 USB cable인지
- 보드 전원과 USB connector
- 다른 USB hub/port
- macOS System Information의 USB 장치

## 5. Port busy

RX reader와 `idf.py monitor`는 같은 port를 동시에 열 수 없다.

```bash
lsof /dev/cu.usbmodemXXXX
ps aux | grep csi_serial_reader
```

monitor는 해당 terminal에서 `Ctrl+]`로 종료한다. 남은 process가 있으면 정상 종료 후 다시 시도한다.

## 6. MAC 읽기 실패

CLI와 registry script는 `esptool`로 chip MAC을 읽는다.

```bash
python3 scripts/device_registry.py add \
  --port /dev/cu.usbmodemXXXX --board-name RX101
```

실패하면 port busy를 먼저 확인한다. 그래도 실패하면 BOOT/RESET 상태와 ESP-IDF Python env의 `esptool` 설치를 확인한다.

## 7. RX stream이 깨진 문자로 보임

정상이다. RX는 사람이 읽는 text가 아니라 binary v2 frame을 USB-Serial-JTAG로 보낸다. `idf.py monitor` 대신 reader를 사용한다.

```bash
python3 scripts/csi_serial_reader.py \
  --port /dev/cu.usbmodemXXXX \
  --device-id 101 \
  --session-id 31
```

## 8. Reader가 시작되지 않음

`pyserial` 확인:

```bash
python3 -c "import serial; print(serial.__version__)"
python3 -m pip install pyserial
```

여러 Python을 설치했다면 CLI를 실행한 Python과 `pyserial`을 설치한 Python이 같은지 확인한다.

## 9. `invalid` frame 증가

Reader는 magic, version, raw length, total length를 검사한다. Invalid frame의 흔한 원인:

- RX ESP_LOG가 binary stream 사이에 삽입됨
- TX/RX firmware version 불일치
- USB stream 일부 손실
- 오래된 RX firmware

먼저 RX를 current source로 다시 flash한다. 지속되면 reader log와 RX의 5초 `ringbuf_drop`을 함께 확인한다.

## 10. 수집 Hz가 낮음

확인 순서:

1. TX log의 `send_frequency: 100`
2. TX/RX 모두 channel 11, HT20인지
3. RX 5초 callback Hz
4. RX USB frame Hz와 `ringbuf_drop`
5. Reader의 `seq_drop`과 평균 Hz
6. USB hub/케이블 병목

```bash
python3 scripts/measure_csi_hz.py \
  mac_collector_output/raw/YYYYMMDD/session_<id>
```

## 11. 도움 요청 시 첨부할 정보

- 사용한 TX/RX source commit
- `idf.py --version`
- 보드 종류와 USB port
- TX 부팅 로그
- RX 5초 통계
- reader log의 `invalid`, `seq_drop`, `last_tx_seq`, `last_raw_len`
- session metadata snapshot
