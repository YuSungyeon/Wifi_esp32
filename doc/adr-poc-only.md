# ADR-0001: ESP-NOW/USB 단일 수집 아키텍처

- 상태: **ACCEPTED**
- 결정일: 2026-07-22

## Context

저장소에는 두 수집 경로가 동시에 존재했다.

1. SoftAP + ESP-NOW 자극 + RX UDP 전송
2. association 없는 ESP-NOW broadcast + RX USB-Serial-JTAG 전송

실제 프로젝트는 두 번째 경로로 진행 중이지만 최상위 문서와 CLI는 첫 번째 경로를 기본 운영 방식으로 노출했다. Firmware, 수집기, 후처리 전제도 서로 달라 유지보수와 실험 재현에 혼선을 만들었다.

## Decision

ESP-NOW/USB 경로만 공식 지원한다.

```text
ESP-NOW TX → CSI RX → USB-Serial-JTAG → Mac JSONL
```

다음을 제거한다.

- `esp32s3_tx_ap_node`
- `esp32s3_csi_sender`
- `mac_collector/udp_collector_mvp.py`
- production `flash_tx.py`, `flash_rx.py`
- `meshsense_config.py`와 SoftAP/collector 설정
- production 전용 문서와 과거 troubleshooting 문서
- `add/main.py` 시간 기반 후처리 prototype

PoC firmware 디렉터리의 이름은 path 변경 위험을 피하기 위해 유지하되, 문서에서는 현재 공식 firmware로 정의한다.

Registry와 session metadata는 ESP-NOW/USB CLI에서도 사용하므로 유지한다. `mac_collector/` 디렉터리 이름도 경로 호환을 위해 유지한다.

## Consequences

긍정적 결과:

- firmware 선택지가 역할별 한 개가 된다.
- Mac Wi-Fi, collector IP, UDP port 설정이 필요 없다.
- `tx_seq`를 multi-RX 공통 동기화 키로 사용할 수 있다.
- 문서와 CLI가 실제 실험 절차를 직접 설명한다.

제약:

- RX마다 Mac과 USB 연결이 필요하다.
- USB port 수와 bandwidth가 확장 한계가 된다.
- 기존 UDP data와 새 USB data를 같은 공식 pipeline에서 지원하지 않는다.
- 현재 model preprocessing은 아직 experimental이며 별도 완성이 필요하다.

## Compliance

이 결정 이후 새 문서와 코드에서 SoftAP/UDP 경로를 현재 기능으로 다시 언급하지 않는다. 필요하면 새 ADR로 이 결정을 supersede한다.
