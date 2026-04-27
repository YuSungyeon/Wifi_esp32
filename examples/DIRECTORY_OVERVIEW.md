# `examples/` 디렉토리 정리

`esp-csi/examples/`는 ESP CSI 기능을 빠르게 실험할 수 있도록 예제들을 목적별로 나눠 둔 폴더입니다.

## 최상위 구조

- `README.md`: 예제 전체 인덱스(프로젝트별 간단 설명)
- `README_cn.md`: 중국어 인덱스 문서
- `get-started/`: 가장 기본적인 CSI 송수신/수집 시작용 예제
- `esp-radar/`: 레이더/감지 응용(콘솔 툴, RainMaker 연동, Wi-Fi sensing 데모)
- `esp-crab/`: `esp-crab` 하드웨어 기반 위상 동기 CSI 수신/분석 예제

## `get-started/`

초기 테스트용 그룹입니다. 두 보드로 CSI를 송수신하고 시각화하는 기본 흐름을 제공합니다.

- `README.md`: get-started 전체 사용법(보드 준비, 플래시, 파이썬 시각화 도구)
- `csi_send/`: CSI 생성용 패킷을 주기적으로 송신하는 송신 펌웨어
- `csi_recv/`: `csi_send`가 보낸 패킷에서 CSI를 추출해 UART로 출력하는 수신 펌웨어
- `csi_recv_router/`: 라우터와 통신(핑)하면서 응답 패킷 CSI를 수집하는 수신 예제
- `tools/`: PC 측 파서/시각화 유틸(`csi_data_read_parse.py`, `csi_viewer.html` 등)

## `esp-radar/`

CSI를 이용한 사람 움직임/존재 감지 등 응용 레벨 예제 그룹입니다.

- `console_test/`: 콘솔 기반 CSI 수집/분석/시각화 테스트 플랫폼
  - 장치 로그/파형/RSSI/상태를 GUI 도구(`tools/esp_csi_tool.py`)와 함께 확인 가능
  - 데이터 라벨링/수집 기능 포함(머신러닝 실험용)
- `connect_rainmaker/`: ESP RainMaker 클라우드 연동 예제
  - `someone_status`, `move_status`, `move_threshold` 등 감지 파라미터를 앱에서 확인/제어
- `wifi_sensing_demo/`: `esp_wifi_sensing` 컴포넌트 기반 데모
  - FSM 시작/정지, LED 상태 피드백, 브라우저 Web Serial 모니터(`tools/web_serial_monitor.html`) 제공

## `esp-crab/`

`esp-crab` 보드(듀얼 ESP32-C5, 공진기 동기 기반)를 사용하는 고정밀 CSI 수신 예제입니다.

- `README.md`: 모드 설명 및 하드웨어 사용 가이드
- `master_recv/`: 마스터 수신 노드
  - 송신/슬레이브에서 온 CIR을 받아 CSI 진폭/위상을 계산·표시
- `slave_recv/`: 슬레이브 수신 노드
  - 송신 패킷 CSI/CIR 수집 후 마스터로 전달
- `slave_send/`: 송신 노드
  - 수신 노드들이 CSI를 추출할 수 있도록 기준 패킷 송신

## 빠른 선택 가이드

- 가장 빠르게 시작: `get-started/csi_send` + `get-started/csi_recv`
- 라우터 기반 수집: `get-started/csi_recv_router`
- 분석/시각화 중심 실험: `esp-radar/console_test`
- 클라우드 연동: `esp-radar/connect_rainmaker`
- 최신 sensing 데모: `esp-radar/wifi_sensing_demo`
- 하드웨어 동기 기반 고정밀 실험: `esp-crab/*`
