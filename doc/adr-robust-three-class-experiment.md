# 30초 3분류 실험의 입력 규격 결정

> 상태: **ACCEPTED — 별도 실험 경로, 2026-09-24**

## Context

3초 CNN의 진폭+위상 비교는 다른 날짜에서 session F1 0.377에 머물렀다.
사용자가 안정적인 3분류 학습을 요청했다. 긴 문맥과 세션 지문을 줄이는 표현,
위상 결측에 의해 움직임 윈도가 과도하게 탈락하는 문제를 검증해야 한다.

## Decision

공식 192차원 pipeline 및 이전 phase51 실험을 보존하고 `model_train/robust/`에
변동 특징·실험 runner·추론을 구현한다. 검증된 phase51 캐시를 읽기 전용으로
재사용한다. 새로운 특징/분할/판정은 [실험 설계](../model_train/docs/model-training/robust-three-class-design.md)
및 [전처리 계약](../model_train/docs/preprocessing/robust-window-features.md)에 따른다.

## Consequences

기존 점수와 표본·모델이 다르므로 단순 동일조건 ablation으로 해석하지 않는다.
긴 관측 지연과 실제 제외율을 함께 보고한다. 실험 성공 여부와 무관하게 공식
모델을 자동 교체하지 않으며, checkpoint에 안정성 판정과 사용 범위를 명시한다.
