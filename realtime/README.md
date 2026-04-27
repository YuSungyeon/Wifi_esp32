# 실시간 자세 판정 MVP (2클래스, 콘솔)

선택된 기준:
- 클래스: `0=정지`, `1=동작`
- 출력: 콘솔
- 초기 전략: 규칙기반 -> 이후 학습모델 교체

실행 스크립트: `realtime/realtime_pose_mvp.py`

## 동작 개요

1. RX UDP 패킷 수신 (collector 스키마와 동일)
2. `csi_amp`로 프레임 에너지 계산
3. 장치별 baseline(평균 에너지) 유지
4. `|현재에너지 - baseline|` 점수를 이동평균
5. threshold 이상이면 `1(동작)`, 아니면 `0(정지)`

## 실행 방법

```bash
python "realtime/realtime_pose_mvp.py" \
  --host 0.0.0.0 \
  --port 9999 \
  --baseline-frames 100 \
  --score-window 20 \
  --motion-threshold 0.02 \
  --print-every-sec 1
```

## 출력 해석

- `label=0`: 정지
- `label=1`: 동작
- `score`: 동작 점수(클수록 움직임 가능성 큼)
- `baseline`: 장치별 기준 에너지
- `drop_rate`: 장치별 시퀀스 누락 추정률

## 튜닝 가이드

- 오검출이 많으면: `motion-threshold`를 올림(예: 0.02 -> 0.04)
- 반응이 너무 느리면: `score-window`를 줄임(예: 20 -> 10)
- 초기 안정화가 느리면: `baseline-frames`를 줄임(예: 100 -> 50)

## 이후 교체 계획

- 현재 규칙기반 결정을 학습 모델 추론 결과로 대체
- 출력 인터페이스는 그대로 유지 가능(콘솔 label/score 포맷 호환)
