# 위상 60초 모델 (phase60)

> 상태: **EXPERIMENTAL** — 실행 코드와 체크포인트가 있으나 공식 pipeline 이 아니다.
> 실시간 경로(`realtime/`)에서 시험 후보로 선택할 수 있다(2026-10-10). 기본값은 기존 진폭 모델이다.

2026-10-13 **위상 60초 · 내 방 추가 학습** 후보를 추가했다. `model_room_20261012.pt`는
기존 모델의 CNN을 고정하고 10/12 방 자료로 head를 파인튜닝한 3클래스 모델이다.
GUI에서 별도로 선택하거나 CLI에 `--model-kind phase60_room`을 넣는다.
[학습 결과·실시간 시험 방법](../docs/model-training/20261013-phase60-room-finetuning.md)을 따른다.

60초 창의 **위상 잔차**로 `empty` / `static` / `motion` 을 가리는 3-class 모델이다. 배포
모델이 **진폭 30초**를 쓰는 것과 대비되는 구성이다. 선택 연결 자체는 기본값 교체나 성능 개선을 뜻하지 않는다.
근거 비교는 [1D-CNN 파일럿 교차검증 보고서](../docs/model-training/archive/cnn1d-pilot-cv-report.md)와
[실시간 상태 판단](../../doc/realtime-inference.md) §2 에 있다.

## 체크포인트

| 파일 | 학습 | 평가 (학습에 쓰지 않은 날짜) | 세션 | 사람 있음/없음 | 빈 방 vs 정지 | 창 정답률 |
|---|---|---|---:|---:|---:|---:|
| `model_15sessions.pt` | 9/16·17 **15세션** | 9/19·20 60세션 | **58/60** | **60/60** | 39/40 | 0.948 |
| `model_60sessions.pt` | 9/19·20 **60세션** | 9/16·17 15세션 | **14/15** | 14/15 | 9/10 | 0.877 |

두 모델은 **서로의 날짜로 평가**했다. 학습에 쓴 날짜가 평가에 섞이면 `train.py` 가 실행을
거부한다. 시험 세션 수가 다르므로 두 숫자를 직접 비교하지 않는다 — 각자 "다른 날에도
동작하는가"에 대한 답이다.

## 쓰는 법

```bash
# 체크포인트와 성능 보기
python model_train/phase60/predict.py --list

# 세션 하나 판단 (기본 모델 = model_60sessions.pt)
python model_train/phase60/predict.py --session-dir mac_collector_output/raw/20260919/153454_empty_s1

# 모델 지정, 창 단위 득표까지
python model_train/phase60/predict.py --session-dir <세션> \
  --model model_train/phase60/model_15sessions.pt --details
```

세션의 `label` 은 읽지 않는다. 보드 식별자만 쓰므로 평가에 정답이 새지 않는다.

GUI는 **실시간 탭 → 사용할 모델 → 위상 60초 (시험 후보)**로 선택한다.
CLI는 `--model-kind phase60`을 붙인다. 현재 SINK 모드의 예:

```bash
.venv/bin/python scripts/meshsense_cli.py --live --port <SINK포트> --model-kind phase60 --plain
```

60초 입력을 준비한 후 새 공통 입력 30프레임 기준으로 창을 옮겨 즉시 분류한다.
기본 이동은 0.3초이며 저장 모델의 오프라인 평가 간격 3초와 다르다.
다른 모델로 바꾸려면 현재 판단을 정지한 뒤 새 실행을 시작한다.

## 다시 학습하려면

```bash
python model_train/phase60/train.py --name 15sessions \
  --train 20260916 20260917 --eval 20260919 20260920
python model_train/phase60/train.py --name 60sessions \
  --train 20260919 20260920 --eval 20260916 20260917 --train-stride 60
```

학습이 끝나면 평가까지 자동으로 하고 그 숫자를 체크포인트 안에 넣는다. 15세션은 약 27분,
60세션은 약 49분 걸린다(Apple MPS, seed 3개).

`cache/` 에는 세션별 위상 잔차가 쌓인다(세션당 약 35MB). git 에 올리지 않으며 지워도 된다 —
다음 실행에서 다시 만든다.

## 입력 계약

아래는 저장 모델의 입력 규칙이다. `predict.py` 로더는 형식·특징 버전·모드·샘플링·창·다운샘플·
RX/톤·클래스 순서·seed 수/목록을 검사한다. `evaluate_raw.py`는 고정 평가의 3초 이동도 검사한다.

| 항목 | 값 |
|---|---|
| `feature_version` | `phase-residual-ls-v1` |
| `mode` | `P` (위상) |
| 창 / 오프라인 평가 이동 | 6000 frame (60초) / 300 frame (3초) |
| GUI·CLI 실시간 이동 기본값 | 30 frame (0.3초), 입력 준비는 60초 |
| 다운샘플 | 20 (5Hz, 300 step) |
| RX 순서 | `[101, 102, 103]` |
| raw 톤 순서 | `[38..63, 1..26]` — 52톤 × RX 3 = **156열** |
| 정규화 | 창 평균 차감만 |
| 앙상블 | seed 0·1·2 의 softmax 평균 |

위상 처리는 `features.py` 한 곳에 있고 학습·추론이 같은 함수를 쓴다. 주파수축으로 unwrap 한
뒤 직선 성분(상수 = CFO, 기울기 = 심볼 타이밍 오프셋)을 최소자승으로 뺀 잔차가 입력이다.

## 한계

- **새 실제 위상 시험·장시간 운영은 남아 있다.** 10/6 전환 원본 재생에서는 앉기 정지 일치율이 0%였다.
- **피험자 1명, 단일 방**이다. 방·사람 일반화는 주장하지 않는다.
- 15세션 모델은 학습 데이터가 적다. 60세션 모델이 더 많은 조건(배치 3주기, 서 있는 자세 등)을 봤다.
- softmax 는 확률 보정을 거치지 않았다. 0.9 가 "90% 확률로 맞다"는 뜻이 아니다.
- 공식 전처리(`preprocess_3rx.py`)와 JSONL 은 진폭만 다룬다. 이 모델은 `.csi` 의 raw I/Q 를
  직접 읽으므로 공식 pipeline 에 넣으려면 [data-schema.md](../../doc/data-schema.md) 변경이 선행되어야 한다.

## 2026-10-08 가져오기·재평가

main `01c9f1929b170d21fe9a570bd37c821127521aeb`에서 코드와 두 가중치를 가져왔다.
기존 `robust/` 진폭 학습 코드와 가중치는 그대로 보존했다. 60세션 모델을 9/16·17 원본에서
재평가해 **14/15세션**, 전체 창 **1,041/1,190**을 확인했다.
자세한 지표·제외 구간·재현 명령은 [위상 모델 재평가 보고서](../docs/model-training/20260916-17-phase60-evaluation.md)에 있다.
가져온 시점에는 GUI·CLI가 진폭만 사용했다. 2026-10-10에는 선택형으로 연결했다.
가중치·`features.py` 수식·학습 코드는 그대로이며 `predict.py`에 엄격한 검사와 한 창 추론 진입점을 추가했다.

이후 [10/6 전환 재생 비교](../docs/model-training/20261006-phase60-transition-replay.md)에서는
빈 방 구분은 개선됐지만 앉기 구간의 정지 일치율이 0%여서 기본 모델 교체를 보류했다.
실행·기록·검증 상세는 [실시간 상태 판단](../../doc/realtime-inference.md)과
[도입 작업 목록 3번](../../doc/plans/2026-10-08-phase60-introduction.md)에 있다.
