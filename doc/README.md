# MeshSense 문서

WiFi CSI(Channel State Information) 기반 실내 행동 인식 시스템 문서입니다. 구성요소별로 디렉터리를 나누어 두었습니다.

## 문서 목록

| 경로 | 내용 |
|------|------|
| [overview/quickstart.md](overview/quickstart.md) | TX → RX → 수집기 → 후처리 빠른 시작 |
| [overview/esp-idf-troubleshooting.md](overview/esp-idf-troubleshooting.md) | ESP-IDF bootstrap·`idf.py`·플래시 오류 |
| [overview/csi-rate-troubleshooting.md](overview/csi-rate-troubleshooting.md) | CSI 수집률(Hz) 100Hz 미달 디버깅 기록 + collector IP 변동 이슈 |
| [overview/architecture.md](overview/architecture.md) | 전체 파이프라인·상수·Python 환경 |
| [firmware/tx-ap-node.md](firmware/tx-ap-node.md) | TX SoftAP, UDP 브로드캐스트, 빌드 |
| [firmware/rx-csi-sender.md](firmware/rx-csi-sender.md) | RX CSI 수집, `device_id`, 플래시 |
| [firmware/csi-poc.md](firmware/csi-poc.md) | esp-csi 베이스 PoC (100Hz 검증용, MeshSense 통합 전) |
| [mac-collector/collector.md](mac-collector/collector.md) | UDP 수집기 실행·등록표·세션 메타 |
| [mac-collector/udp-packet-schema.md](mac-collector/udp-packet-schema.md) | ESP → Mac 바이너리 UDP 규격 |
| [postprocessing/pipeline.md](postprocessing/pipeline.md) | JSONL → 52 서브캐리어 → 학습 텐서 |
| [scripts/README.md](../scripts/README.md) | TX/RX 플래시·registry CRUD (호스트) |

## 저장소 레이아웃

```text
Wifi_esp32/
├── doc/                    ← 이 문서 트리
├── esp-idf/                ESP-IDF (git submodule)
├── .espressif/             bootstrap 마커만 (gitignore; 툴체인은 ~/.espressif)
├── scripts/                플래시·bootstrap·meshsense_config·registry
├── esp32s3_tx_ap_node/     TX/AP 펌웨어
├── esp32s3_csi_sender/     RX CSI 펌웨어
├── mac_collector/          수집기·device_registry.csv (RX)·tx_registry.csv (TX)
├── add/                    후처리 스크립트
└── mac_collector_output/   수집 데이터 (git 제외)
```

AI 코딩 어시스턴트용 요약은 루트 [CLAUDE.md](../CLAUDE.md)를 참조하세요.
