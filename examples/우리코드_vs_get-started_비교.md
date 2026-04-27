# 우리 송수신 코드 vs `examples/get-started` 비교

이 문서는 현재 프로젝트의 송수신 코드와 `examples/get-started` 예제의 목적/구조/출력 차이를 빠르게 이해하기 위한 비교 문서입니다.

## 비교 대상

- 우리 코드
  - TX/AP: `esp32s3_tx_ap_node/main/tx_ap_main.c`
  - RX(sender): `esp32s3_csi_sender/main/csi_sender_main.c`
  - Mac 수신기: `mac_collector/udp_collector_mvp.py`
- 예제 코드
  - sender: `examples/get-started/csi_send/main/app_main.c`
  - recv: `examples/get-started/csi_recv/main/app_main.c`
  - recv_router: `examples/get-started/csi_recv_router/main/app_main.c`

---

## 1) 목표 차이

- `examples/get-started`
  - CSI 취득 원리/동작을 빠르게 확인하는 데모 성격
  - 수신된 CSI를 UART 로그로 출력하고 별도 툴에서 시각화
- 우리 코드
  - 실제 운영을 위한 수집 파이프라인 구축이 목적
  - TX/AP + RX + Mac 저장/모니터링 + 후속 학습(`.mat`)까지 연결

---

## 2) 송신 방식 차이

- 예제 `csi_send`
  - ESP-NOW 브로드캐스트 송신 (`esp_now_send`)
  - 수신기에서 해당 MAC 필터링
- 우리 TX/AP (`esp32s3_tx_ap_node`)
  - SoftAP 생성 + UDP broadcast heartbeat 송신
  - RX 노드가 Wi-Fi CSI를 안정적으로 얻도록 트래픽 공급

---

## 3) CSI 수신 처리 차이

- 예제 `csi_recv` / `csi_recv_router`
  - CSI raw 값을 콘솔 CSV 형태로 `ets_printf` 출력
  - 목적: 사람이 시리얼 로그/뷰어로 확인
- 우리 RX (`esp32s3_csi_sender`)
  - CSI(raw I/Q) -> amplitude 변환
  - 이동평균 + z-score + 클리핑(1차 전처리)
  - 바이너리 패킷으로 Mac에 UDP 전송

---

## 4) 수신(저장) 계층 차이

- 예제
  - PC 측 파이썬 툴은 시리얼 로그 파싱/시각화 중심
  - 실험 세션 저장/장치 상태 관리는 상대적으로 단순
- 우리 Mac 수집기
  - UDP 패킷 스키마 검증 (`magic/version/len`)
  - JSONL 저장 + session/device 디렉터리 구조화
  - `seq` 기반 유실률, `missing/stale` 상태 모니터링
  - `device_registry.csv`, `session_meta.yaml` 연동

---

## 5) 데이터 포맷 차이

- 예제
  - 텍스트 CSV 로그(콘솔 출력 문자열)
- 우리 코드
  - ESP->Mac: 고정 헤더 + `float32 csi_amp[]` 바이너리 UDP
  - Mac 저장: JSONL
  - 학습 입력: `data_tools/jsonl_to_mat.py`로 `.mat` 변환

---

## 6) 확장성과 운영성

- 예제
  - 단일/소수 장치 데모 중심
- 우리 코드
  - RX N대 확장 고려(`device_id`, registry, expected devices)
  - 세션 메타 스냅샷 보존으로 재현성 강화
  - 실시간 규칙기반 판정 MVP(`realtime/realtime_pose_mvp.py`) 제공

---

## 7) 한 줄 요약

- `examples/get-started`는 **CSI 취득 데모**에 강점
- 우리 코드는 **실제 실험 운영/저장/후처리/학습 연결**까지 확장한 구조
