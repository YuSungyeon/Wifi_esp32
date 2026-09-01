# MeshSense

WiFi CSI(Channel State Information) 기반 실내 행동 인식 시스템.

ESP32-S3 보드가 CSI를 수집하고, Mac이 raw I/Q 프레임으로 저장하며, 후처리 파이프라인이
LSTM 학습 텐서를 생성합니다. 수집 경로는 2가지입니다:

- **USB 수집** — RX 보드를 USB로 연결해 시리얼로 100Hz 수집 (**모델 학습 데이터 정본 경로**)
- ~~AP 실시간 수집 — TX SoftAP + RX UDP 무선 전송~~ (**deprecated**, 실측 0.18~22.8Hz)

```bash
python scripts/meshsense_cli.py   # 첫 화면에서 파이프라인 선택
```

## 문서

전체 문서는 **[doc/](doc/README.md)** 에 정리되어 있습니다.

| 구분 | 문서 |
|------|------|
| 개요 | [빠른 시작](doc/overview/quickstart.md) · [아키텍처](doc/overview/architecture.md) |
| 파이프라인 | [USB 수집](doc/pipeline/usb-collection.md) · ~~[AP 실시간](doc/pipeline/ap-realtime.md)~~ |
| 작업 기록 | [수집 환경 정비 스프린트](doc/sprint/2026-08-collection-hardening.md) |
| 후처리·학습 | [파이프라인](doc/postprocessing/pipeline.md) · [LSTM 설계](doc/postprocessing/lstm-design.md) |
| 호스트 | [스크립트 레퍼런스](scripts/README.md) |

## 한 줄 요약

```text
TX ESP-NOW 10ms → RX CSI 100Hz → Mac .csi(raw I/Q) → (N, 300, RX수×52) 텐서 → LSTM
```

AI 어시스턴트용 프로젝트 요약: [CLAUDE.md](CLAUDE.md)
