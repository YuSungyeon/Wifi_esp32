# ESP-IDF / 플래시 트러블슈팅

MeshSense는 **ESP-IDF v5.2.2** (`esp-idf/` 서브모듈)와 **호스트 툴체인** `~/.espressif/`를 사용합니다.  
프로젝트 루트 `.espressif/`는 **bootstrap 완료 마커**만 둡니다 (실제 컴파일러·venv는 `~/.espressif`).

온보딩 권장:

```bash
git clone --recursive <repo-url>
cd Wifi_esp32
cp scripts/meshsense_config.example.json scripts/meshsense_config.json
python scripts/idf_bootstrap.py -y   # 플래시 전 최초 1회 (10–30분)
```

`flash_rx.py` / `flash_tx.py` / CLI 플래시도 툴이 없으면 위 bootstrap을 자동 호출합니다.

---

## 문제 1: `.meshsense_tools_ready` 생성 실패 (해결됨)

### 증상

`install.sh`는 끝났는데 CLI가 실패:

```text
No such file or directory: '.../Wifi_esp32/.espressif/.meshsense_tools_ready'
```

### 원인

툴은 `~/.espressif`에 설치되는데, 완료 마커만 `프로젝트/.espressif/`에 씁니다. 예전 bootstrap은 부모 폴더 `mkdir` 없이 `write_text` 해서 실패했습니다.

### 조치 (저장소)

`scripts/idf_bootstrap.py`의 `run_install()`에서 마커 쓰기 전 `ready_file.parent.mkdir(parents=True, exist_ok=True)` — **현재 main에 포함**.

---

## 문제 2: `ruamel.yaml` / Python requirements 검사 실패

### 증상

`idf.py --version` 또는 플래시 직전 bootstrap에서:

```text
The following Python requirements are not satisfied:
Error while checking requirement '...' ... ruamel.yaml
```

### 원인

ESP-IDF v5.2.2의 requirements 검사기가, pip가 설치한 **최신 `ruamel.yaml` 메타데이터 이름**과 맞지 않는 경우가 있습니다 (`import`는 되지만 `importlib.metadata.version("ruamel.yaml")` 실패).

### 자동 조치 (저장소)

`install.sh` 직후 bootstrap이 IDF venv에 호환 버전을 pin 합니다:

- `ruamel.yaml==0.17.21`
- `ruamel.yaml.clib==0.2.7`

### 수동 조치

자동 pin 후에도 실패하면 venv Python을 찾아 동일하게 설치합니다:

```bash
# 예: 경로는 Mac·Python 버전마다 다름
ls ~/.espressif/python_env/idf5.2_py*_env/bin/python

~/.espressif/python_env/idf5.2_py3.12_env/bin/python -m pip install \
  'ruamel.yaml==0.17.21' 'ruamel.yaml.clib==0.2.7'

export IDF_PATH="$PWD/esp-idf"
source "$IDF_PATH/export.sh"
idf.py --version
```

정상:

```text
ESP-IDF v5.2.2
```

---

## 문제 3: pip 네트워크 실패 (환경 이슈)

### 증상

```text
Failed to establish a new connection: [Errno 8] nodename nor servname provided, or not known
```

### 원인

MeshSense CLI 버그가 아니라 **네트워크 차단** (Cursor 샌드박스, 오프라인, 회사 프록시 등).

### 해결

일반 터미널·인터넷 가능 환경에서 `python scripts/idf_bootstrap.py -y` 재실행.

---

## RX 플래시 메뉴 예시

```text
python3 scripts/meshsense_cli.py
→ 플래시만 → RX CSI 노드
```

registry·`meshsense_config.json`이 맞으면 빌드 시 CMake에 AP/수집기/device_id가 주입됩니다.  
MAC 미등록 시: `python scripts/device_registry.py add --port /dev/cu.usbmodem101`

---

## 관련 문서

- [quickstart.md](quickstart.md)
- [scripts/README.md](../../scripts/README.md)
