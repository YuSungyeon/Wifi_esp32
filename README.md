# MeshSense

ESP32-S3와 Wi-Fi CSI(Channel State Information)를 이용한 실내 행동 인식 프로젝트입니다.

현재 공식 수집 방식은 **ESP-NOW 송신 → CSI 수신 → USB-Serial-JTAG → Mac JSONL**입니다. SoftAP·UDP 기반 운영 펌웨어와 수집기는 제거되었습니다.

```text
ESP-NOW TX (100Hz, channel 11, HT20)
  → CSI RX × N
  → USB-Serial-JTAG binary frame
  → Mac serial reader
  → device별 JSONL + session metadata
```

## 시작하기

```bash
python3 scripts/meshsense_cli.py
```

처음 실행할 때는 메뉴 **[1] 전체 가이드**를 사용합니다. Mac을 별도 Wi-Fi AP에 연결하거나 UDP collector를 실행할 필요가 없습니다.

## 기준 문서

| 문서 | 내용 |
|---|---|
| [문서 인덱스](doc/README.md) | 문서 상태와 읽는 순서 |
| [아키텍처](doc/architecture.md) | 현재 코드 기준 전체 구조와 모듈 |
| [빠른 시작](doc/quickstart.md) | 환경 준비부터 수집까지 |
| [펌웨어](doc/firmware.md) | TX/RX 무선·CSI·USB 동작 |
| [binary/JSONL 계약](doc/data-schema.md) | 수집 데이터 형식 |
| [후처리·학습](doc/postprocessing.md) | 구현된 기능과 모델 문서 연결 |
| [모델 학습 문서](model_train/docs/%5B문서%5D-목록.md) | 전처리·모델 비교·설계·학습 |
| [문제 해결](doc/troubleshooting.md) | ESP-IDF·빌드·포트 문제 |
| [문서 주도 개발 규칙](doc/documentation-policy.md) | 문서 선행 변경 절차와 완료 조건 |

`esp32s3_csi_send_poc`, `esp32s3_csi_recv_poc`의 `poc` 이름은 역사적으로 유지하고 있지만 현재 프로젝트의 공식 펌웨어입니다.
