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

### 다른 MacBook 에서 처음 셋업 (환경 차이)

Mac·Python 설치 방식이 달라도 동일하게 동작하도록 `scripts/idf_env.py` / `idf_paths.py` 가 다음을 처리합니다.

| 환경 | 대응 |
|------|------|
| Homebrew Apple Silicon (`/opt/homebrew`) / Intel (`/usr/local`) | `python3` 경로를 PATH 앞에 추가 |
| 시스템 `/usr/bin/python3` 만 있는 경우 | **IDF venv** (`~/.espressif/python_env/idf5.2_py*_env`) 를 PATH 최우선 → `export.sh` 가 3.9 venv 를 찾지 않게 함 |
| pyenv / conda | `~/.pyenv/shims`, `$CONDA_PREFIX/bin` 를 PATH 에 포함 |
| `python` 명령 없음 (`python3` 만) | `idf.py` 를 venv `python …/tools/idf.py` 로 직접 실행 (shebang 우회) |
| `IDF_TOOLS_PATH` 사용자 지정 | `~/.espressif` 대신 해당 경로에서 venv 탐색 |

**다른 Mac 에서 권장 순서**

```bash
git clone --recursive <repo>
cd Wifi_esp32
python3 scripts/idf_bootstrap.py -y    # meshsense_cli 플래시 전 1회
python3 scripts/meshsense_cli.py       # [5] 사전 점검 → ESP-IDF OK 확인 후 [2] 플래시
```

`python` 이 없으면 `python3` 를 사용하세요. meshsense_cli 가 호출하는 플래시·bootstrap 은 동일한 `idf_env` 를 씁니다.

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

- `ruamel.yaml==0.17.21` (필수)
- `ruamel.yaml.clib==0.2.7` (Python 3.12 이하 IDF venv)
- `ruamel.yaml.clib>=0.2.12` (Python 3.14+ — `0.2.7` 소스 빌드가 `ast.Str` 제거로 실패)

`clib` 설치가 실패해도 `ruamel.yaml`만 맞으면 `idf.py`는 동작할 수 있습니다. bootstrap은 `clib` 실패 시 경고만 내고 계속합니다.

### Python 3.14에서 `ruamel.yaml.clib` 빌드 실패

```text
ImportError: cannot import name 'Str' from 'ast'
ERROR: Failed to build 'ruamel.yaml.clib' when getting requirements to build wheel
```

→ 저장소 최신 `scripts/idf_bootstrap.py`로 재실행하거나, venv에 `ruamel.yaml==0.17.21`과 `ruamel.yaml.clib>=0.2.12`(미리 빌드된 wheel)를 설치합니다.

### 수동 조치

자동 pin 후에도 실패하면 venv Python을 찾아 동일하게 설치합니다:

```bash
# 예: 경로는 Mac·Python 버전마다 다름
ls ~/.espressif/python_env/idf5.2_py*_env/bin/python

# Python 3.12 예
~/.espressif/python_env/idf5.2_py3.12_env/bin/python -m pip install \
  'ruamel.yaml==0.17.21' 'ruamel.yaml.clib==0.2.7'

# Python 3.14 예
~/.espressif/python_env/idf5.2_py3.14_env/bin/python -m pip install \
  'ruamel.yaml==0.17.21' 'ruamel.yaml.clib>=0.2.12'

export IDF_PATH="$PWD/esp-idf"
source "$IDF_PATH/export.sh"
idf.py --version
```

정상:

```text
ESP-IDF v5.2.2
```

---

## 문제 3: `env: python: No such file or directory` (플래시·bootstrap 실패)

### 증상

`meshsense_cli` 플래시 또는 `ensure_idf_ready` 직후:

```text
env: python: No such file or directory
error: install finished but idf.py --version still fails.
```

### 원인

1. `idf.py` shebang이 `#!/usr/bin/env python` 인데, macOS 비대화형 셸 PATH에 `python` 이 없음 (Homebrew는 `python3` 만 제공하는 경우 많음).
2. `export.sh` 가 시스템 `python3`(예: 3.9)를 잡아 존재하지 않는 `idf5.2_py3.9_env` 를 찾으려다 PATH 설정이 끊김.
3. 예전 `idf_env.py` 가 `source export.sh` stderr 를 `/dev/null` 로 숨겨 위 오류가 가려짐.

### 조치 (저장소)

`scripts/idf_env.py` — 플래시 전 IDF venv·Homebrew `bin` 을 PATH 앞에 두고, `idf.py` 대신 `~/.espressif/.../bin/python tools/idf.py` 로 실행.

수동 확인:

```bash
export IDF_PATH="$PWD/esp-idf"
source "$IDF_PATH/export.sh"
idf.py --version
```

---

## 문제 4: pip 네트워크 실패 (환경 이슈)

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
→ [2] 플래시 (MAC → registry 자동 분기) 또는 [3] 보드 관리
```

registry·`meshsense_config.json`이 맞으면 빌드 시 CMake에 AP/수집기/device_id가 주입됩니다.  
MAC 미등록 시: `python scripts/device_registry.py add --port /dev/cu.usbmodem101`

---

## 관련 문서

- [quickstart.md](quickstart.md)
- [scripts/README.md](../../scripts/README.md)
