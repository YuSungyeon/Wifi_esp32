# MeshSense

WiFi CSI(Channel State Information) 기반 실내 행동 인식 시스템.

ESP32-S3 보드가 CSI를 수집하고, Mac 수집기가 UDP로 JSONL을 저장하며, 후처리 파이프라인이 ML 학습용 텐서를 생성합니다.

## 문서

전체 문서는 **[doc/](doc/README.md)** 에 계층적으로 정리되어 있습니다.

| 구분 | 문서 |
|------|------|
| 개요 | [빠른 시작](doc/overview/quickstart.md) · [아키텍처](doc/overview/architecture.md) |
| 펌웨어 | [TX/AP](doc/firmware/tx-ap-node.md) · [RX CSI](doc/firmware/rx-csi-sender.md) |
| 수집 | [Mac Collector](doc/mac-collector/collector.md) · [UDP 스키마](doc/mac-collector/udp-packet-schema.md) |
| 후처리 | [파이프라인](doc/postprocessing/pipeline.md) |
| 호스트 | [TX/RX 플래시·registry](scripts/README.md) |

## 한 줄 요약

```text
TX SoftAP → RX CSI(100Hz UDP) → Mac JSONL → add/ 텐서 (N, 3, 52, 200)
```

AI 어시스턴트용 프로젝트 요약: [CLAUDE.md](CLAUDE.md)
