# RX Flash Troubleshooting

이 문서는 `python3 scripts/meshsense_cli.py`에서 `2. 플래시만 -> 2. RX 플래시`를 실행할 때 발견한 문제와 해결 방법을 정리한 기록입니다.

## 실행 흐름

실행한 메뉴 흐름:

```text
python3 scripts/meshsense_cli.py
2. 플래시만
2. RX CSI 노드
```

자동 선택된 포트:

```text
/dev/cu.usbmodem101
```

최종 플래시 결과:

```text
[ok] flashed device_id=103 (RX3) MAC=DC:B4:D9:03:47:00
[완료] 플래시 성공
```

## 문제 1: `.meshsense_tools_ready` 파일 생성 실패

### 증상

ESP-IDF 설치 과정은 끝났지만, 아래 파일을 만들지 못해서 CLI가 실패했습니다.

```text
/Users/jaehyeog/my/Wifi_esp32/.espressif/.meshsense_tools_ready
```

에러 의미:

```text
No such file or directory: '/Users/jaehyeog/my/Wifi_esp32/.espressif/.meshsense_tools_ready'
```

### 원인

`scripts/idf_bootstrap.py`에서 완료 표시 파일을 쓰기 전에 상위 디렉토리인 `.espressif`를 만들지 않았습니다.

기존 코드는 파일을 바로 쓰려고 했습니다.

```python
tools_ready_file(repo_root).write_text(...)
```

하지만 `.espressif` 폴더가 없으면 파일 생성이 실패합니다.

### 해결

`scripts/idf_bootstrap.py`에서 파일을 쓰기 전에 부모 폴더를 먼저 만들도록 수정했습니다.

```python
ready_file = tools_ready_file(repo_root)
ready_file.parent.mkdir(parents=True, exist_ok=True)
ready_file.write_text(f"idf={idf_path}\ntarget={IDF_TARGET}\n", encoding="utf-8")
```

## 문제 2: ESP-IDF v5.2.2 Python requirements 검사 실패

### 증상

`idf.py --version` 검증 중 아래와 같은 에러가 발생했습니다.

```text
The following Python requirements are not satisfied:
Error while checking requirement 'construct'. Package was not found and is required by the application: ruamel.yaml
```

또는 다음처럼 다른 패키지를 검사하다가 `ruamel.yaml`에서 실패했습니다.

```text
Error while checking requirement 'esp-coredump~=1.10'. Package was not found and is required by the application: ruamel.yaml
Error while checking requirement 'esp-idf-size<2.0.0,>=1.0.1'. Package was not found and is required by the application: ruamel.yaml
```

### 원인

실제로 `ruamel.yaml` import는 가능했지만, ESP-IDF v5.2.2의 오래된 dependency checker가 현재 설치된 `ruamel.yaml`의 metadata 이름을 제대로 찾지 못했습니다.

확인 결과:

```text
import ruamel.yaml 가능
pip show ruamel.yaml 가능
하지만 importlib.metadata.version("ruamel.yaml") 실패
```

즉 패키지가 아예 없는 문제가 아니라, ESP-IDF v5.2.2의 검사 방식과 설치된 `ruamel.yaml` 버전의 metadata 이름이 맞지 않는 문제였습니다.

### 해결

ESP-IDF v5.2.2 Python venv에서 `ruamel.yaml` 계열 패키지를 호환되는 버전으로 맞췄습니다.

```bash
/Users/jaehyeog/.espressif/python_env/idf5.2_py3.9_env/bin/python -m pip install 'ruamel.yaml==0.17.21'
/Users/jaehyeog/.espressif/python_env/idf5.2_py3.9_env/bin/python -m pip install 'ruamel.yaml.clib==0.2.7'
```

이후 아래 명령이 정상 동작했습니다.

```bash
export IDF_PATH="/Users/jaehyeog/my/Wifi_esp32/esp-idf"
source "$IDF_PATH/export.sh"
idf.py --version
```

정상 결과:

```text
ESP-IDF v5.2.2
```

## 문제 3: 샌드박스 환경에서 pip 네트워크 실패

### 증상

처음 실행할 때 pip가 패키지를 받으려고 했지만 네트워크 접근이 막혀 실패했습니다.

```text
Failed to establish a new connection: [Errno 8] nodename nor servname provided, or not known
```

### 원인

CLI 자체의 문제라기보다는 실행 환경에서 네트워크 접근이 제한되어 있었기 때문입니다.

### 해결

네트워크 접근이 가능한 실행으로 다시 돌렸고, ESP-IDF Python 패키지 설치가 진행됐습니다.

## 최종 확인

위 문제들을 해결한 뒤 다시 CLI를 실행했습니다.

```text
python3 scripts/meshsense_cli.py
2. 플래시만
2. RX CSI 노드
```

빌드에 들어간 주요 값:

```text
CSI_WIFI_SSID=MeshSense_TX_AP
CSI_WIFI_PASS=mstx1234
CSI_COLLECTOR_IP=192.168.4.2
CSI_COLLECTOR_PORT=9999
CSI_DEVICE_ID=103
```

최종적으로 `/dev/cu.usbmodem101`에 연결된 보드는 registry 기준으로 아래처럼 인식됐습니다.

```text
device_id=103
board_name=RX3
MAC=DC:B4:D9:03:47:00
```

플래시는 정상 완료됐습니다.

