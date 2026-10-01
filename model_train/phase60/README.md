# 위상 60초 모델 (phase60)

> 상태: **EXPERIMENTAL** — 실행 코드와 체크포인트가 있으나 공식 pipeline 이 아니다.
> 실시간 경로(`realtime/`)는 모델 담당의 배포 체크포인트(`model_train/robust/model.pt`)를 쓴다.

60초 창의 **위상 잔차**로 `empty` / `static` / `motion` 을 가리는 3-class 모델이다. 배포
모델이 **진폭 30초**를 쓰는 것과 대비되는 구성이며, 둘 중 무엇을 쓸지는 모델 담당과 협의할
사안이다. 근거 비교는 [1D-CNN 파일럿 교차검증 보고서](../docs/model-training/cnn1d-pilot-cv-report.md)와
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

체크포인트를 열 때 아래가 다르면 **거부**한다. 잘못된 입력으로 돌려 조용히 틀리는 것을 막는다.

| 항목 | 값 |
|---|---|
| `feature_version` | `phase-residual-ls-v1` |
| `mode` | `P` (위상) |
| 창 / 판단 간격 | 6000 frame (60초) / 300 frame (3초) |
| 다운샘플 | 20 (5Hz, 300 step) |
| RX 순서 | `[101, 102, 103]` |
| raw 톤 순서 | `[38..63, 1..26]` — 52톤 × RX 3 = **156열** |
| 정규화 | 창 평균 차감만 |
| 앙상블 | seed 0·1·2 의 softmax 평균 |

위상 처리는 `features.py` 한 곳에 있고 학습·추론이 같은 함수를 쓴다. 주파수축으로 unwrap 한
뒤 직선 성분(상수 = CFO, 기울기 = 심볼 타이밍 오프셋)을 최소자승으로 뺀 잔차가 입력이다.

## 한계

- **전환 구간과 장시간 운영은 검증하지 않았다.** 모든 평가 세션이 5분 동안 상태 하나만 담고 있다.
- **피험자 1명, 단일 방**이다. 방·사람 일반화는 주장하지 않는다.
- 15세션 모델은 학습 데이터가 적다. 60세션 모델이 더 많은 조건(배치 3주기, 서 있는 자세 등)을 봤다.
- softmax 는 확률 보정을 거치지 않았다. 0.9 가 "90% 확률로 맞다"는 뜻이 아니다.
- 공식 전처리(`preprocess_3rx.py`)와 JSONL 은 진폭만 다룬다. 이 모델은 `.csi` 의 raw I/Q 를
  직접 읽으므로 공식 pipeline 에 넣으려면 [data-schema.md](../../doc/data-schema.md) 변경이 선행되어야 한다.
