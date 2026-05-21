# 후처리 파이프라인

Mac 수집기 JSONL → 다중 RX 시간 정렬 → 슬라이딩 윈도 → 학습 텐서 `(N, 3, 52, 200)`.

현재 구현: [`add/main.py`](../../add/main.py) (단일 스크립트, 상단 경로·상수 수정 후 실행).

## 입력

수집기 출력 예:

```text
mac_collector_output/raw/YYYYMMDD/session_<id>/device_<device_id>.jsonl
```

JSONL 레코드 주요 필드: `received_at_unix_us`, `device_id`, `csi_amp` (float 배열, 펌웨어당 최대 64개).

## 상수 (`add/main.py`)

| 상수 | 값 | 의미 |
|------|-----|------|
| `RX_IDS` | `[101, 102, 103]` | 사용할 RX `device_id` (환경에 맞게 수정) |
| `F_S` | 100 | 목표 샘플링 (Hz), 펌웨어 10ms 전송과 맞춤 |
| `WINDOW` | 200 | 윈도 길이 (2초 @ 100Hz) |
| `STRIDE` | 100 | stride (1초) |
| `N_SUB` | 52 | 모델 입력 서브캐리어 수 |

## 실행 전 수정

1. **`SESSION_DIR`** — 수집 결과 디렉터리로 변경:

```python
SESSION_DIR = Path("mac_collector_output/raw/20260513/session_1")
```

2. **`RX_IDS`** — 해당 세션에 켜 둔 `device_id` 목록과 일치
3. **`csi_amp` 길이** — 펌웨어는 최대 64 bin을 보냅니다. `main.py`는 `N_SUB=52`로 보간·스택하므로, bin 수가 52가 아니면 **슬라이스/인덱스 매핑**을 스크립트에 추가해야 합니다 (유효 톤 매핑은 실험별로 `add/main.py`에 반영).

## 처리 단계

1. **로드** — `SESSION_DIR` 아래 `*.jsonl`을 `device_id`별 버퍼 `(received_at_unix_us, csi_amp)`에 적재
2. **시간 정렬** — RX별 타임스탬프를 100Hz `t_grid`에 선형 보간 → `aligned` shape `(3, T, 52)`
3. **윈도잉** — `WINDOW`/`STRIDE`로 `(N, 3, 52, 200)` 텐서 `X` 생성 (RX 축 순서: `RX_IDS` 순)

## 실행

```bash
source .venv/bin/activate   # numpy 필요
# add/main.py 상단 SESSION_DIR, RX_IDS 수정 후
python add/main.py
```

## Python 환경

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-viz.txt
```

`meshsense_cli` 사전 점검·수집 종료 후 PNG 생성 시 `.venv` 없으면 위 설치를 안내·선택 실행합니다.

ESP-IDF 빌드용 Python venv와는 별도입니다. 개요: [architecture.md](../overview/architecture.md).
