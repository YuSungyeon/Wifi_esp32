# 안정적인 3클래스 모델 후속 실험

> 상태: **EXPERIMENTAL — 구현·학습·검증 완료, 2026-09-24**
> 대상: 9/19·20의 60세션. 기존 위상 비교의 원본·산출물은 보존한다.
> 실행 전 기준은 아래에 보존하며 실측 결과는 [결과 보고서](robust-three-class-report.md)를 따른다.

처음 읽는 사람은 [쉬운 설계 설명](robust-three-class-design-easy.md)부터 볼 수 있다.

## 목적과 판정

사용자 요청은 안정적인 empty/static/motion 학습이다. 학습 손실 수렴과 일반화
성공을 구분한다. 날짜·배치가 달라져도 어느 클래스도 붕괴하지 않는 모델을 찾는다.
탐색 전에 다음 잠정 통과 기준을 둔다.

- 양방향 날짜 제외 평가 각각 session macro F1 >= 0.80, 모든 클래스 recall >= 0.70.
- 배치 제외 평가 각각 session macro F1 >= 0.70.
- 날짜 제외 평가의 window macro F1 >= 0.75.
- 같은 설정의 seed 0/1/2 session F1 표본 표준편차 <= 0.05.
- 예측·라벨·정규화·분할 재검증 통과.

기준을 충족해도 두 날짜는 이미 앞선 실험에 쓰인 자료다. 새 날짜·새 환경에 대한
실사용 안정성은 독립 수집 자료가 있어야 확인된다. 기준을 못 맞추면 최선의 연구용
checkpoint와 실패 내역을 남기고 stable 상태를 부여하지 않는다.

## 후보와 선택

1. 윈도 3/5/10/20/30초를 비교한다. 길이별 절반 stride, 학습 세션당 최대 48개를
   균등 추출해 짧은 윈도/긴 세션이 학습을 지배하지 않게 한다.
2. 절대 진폭·서브캐리어 위치에 대한 의존을 줄이는 시간 통계, 주파수 대역 에너지,
   상대 진폭과 원형 위상 변동을 사용한다. 진폭 프로파일은 별도 대조군이다.
3. 규제된 선형·RBF 분류기와 얕은 트리 앙상블을 먼저 비교한다. 공유 시간 인코더도
   별도로 비교한다. 전처리·표준화는 fold의 학습 자료에만 맞춘다.
4. 우선 9/19의 세션을 클래스별 시간 순위 두 개씩 묶은 5개 validation fold로
   비교한다. 하나의 세션 및 그 중첩 윈도는 fold를 넘지 않는다. 이 fold는 독립적인
   환경 5개를 뜻하지 않는다. 후보 선택은 이 development 결과만 사용한다.
5. 선택 이후 seed 0/1/2로 9/19 -> 9/20을 평가한다. 역방향 날짜 및 배치 P1/P2/P3
   제외 평가도 기록한다. 고정 후보의 후속 강건성 진단이며 완전히 새로운 test가 아니다.
   외부 결과를 본 뒤 후보를 수정하면 별도 탐색 단계로 표시한다.

세션 예측은 유효 윈도 확률 평균이다. window 성능과 세션 성능을 함께 기록하고
같은 세션의 수백 윈도를 독립 표본으로 간주하지 않는다. bootstrap은 세션 단위다.
filename, session ID, 날짜, 배치, 품질 마스크/누락률, 사진, 라벨을 모델 입력으로
사용하지 않는다. 날짜·배치는 분할과 오류 분석에만 사용한다.

공유 인코더는 10/30초 × A/P/AP의 6개 고정 후보를 같은 development fold로
비교한다. 각 RX 51톤 중 3개 간격의 17톤(총 51개)을 동일 가중치의 시간 Conv로
처리하고 시간·톤 통계로 집계한다. 10Hz bin, 윈도 내부 중심화, AdamW 0.001,
weight decay 0.01, label smoothing 0.1, dropout 0.3, batch 32, 15 epoch 고정이다.
훈련 중 톤 1/4 무작위 제거와 RX 공통 0.8~1.2 gain 증강을 적용한다. validation
epoch 최댓값을 다시 고르는 대신 15번째 epoch를 사용한다. 후보별 seed 0으로
development를 비교하고 선택 후보의 외부 진단을 seed 0/1/2로 반복한다.

### 추가 검증: 설정 선택까지 분리

고정 후보의 역방향 평가에는 최초 development 날짜가 들어간다. 이 선택 영향을
분리하기 위해 최종 보고 전 신경망 6개 후보의 중첩 검증을 추가한다. 각 outer 날짜·
배치 제외 fold의 **학습 세션만** 클래스별 시간순으로 3등분해 inner validation을
만든다. inner 점수로 길이·A/P/AP를 선택한 뒤 outer 학습 전체로 seed 0/1/2를
학습한다. 5개 outer fold의 설정 선택을 모두 저장한 후 outer 예측을 수행한다.
전처리 및 15 epoch 학습 조건은 변경하지 않는다. 이 절차도 이미 탐색한 60세션의
재검증이며 새로운 날짜에 대한 독립 시험을 대신하지 않는다.

## 재현과 산출물

새 경로 `model_train/analysis/output/20260924-robust-three-class/`에 특징 캐시,
입력 hash, candidate별 development 점수, 고정 선택 기록, OOF 확률, checkpoint,
seed별 결과, 최종 판정을 보존한다. 원본 및 이전 모델은 변경하지 않는다.
추론도 학습과 같은 특징 함수를 사용하고 파일을 다시 읽어 예측을 재현한다.

전처리 계약: [robust-window-features.md](../preprocessing/robust-window-features.md).
공식 입력과의 구분: [ADR](../../../doc/adr-robust-three-class-experiment.md).

분할·전처리 누출 방지 원칙은 [scikit-learn 공식 문서](https://scikit-learn.org/1.6/common_pitfalls.html)
및 [그룹 교차검증](https://scikit-learn.org/1.6/modules/cross_validation.html)에 따른다.
