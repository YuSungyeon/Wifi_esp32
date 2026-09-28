# Public Wi-Fi CSI Dataset Review

> 상태: **SUPPORTING ANALYSIS — 2026-09-06 공개 자료 조사 기준**
>
> 목적: 현재 ESP32-S3 3-RX LSTM 프로젝트에 활용할 외부 데이터셋과 적용 조건을 선정한다.
> 저자 논문·저장소·데이터 카드·일부 배포 메타데이터를 확인했다.
> 전체 CSI 데이터 다운로드, 파싱 검증, 추가 학습·test는 수행하지 않았다.

## 1. Executive Summary

**첫 검증 후보는 OpenCSI이고, Hugging Face에서는 HomeOccupancy를 우선 추천한다.**
아래 순위는 공개 설명과 현재 코드의 요구사항을 비교한 판단이며, 성능 향상을
실험으로 확인한 순위가 아니다.

- **OpenCSI**: ESP32-S3 수집분과 `empty / occupied_static / occupied_moving`
  라벨이 있어 현재 문제와 가장 가깝다. 단, 20 Hz·mesh 구조이므로 별도 변환이
  필요하다. [데이터 설명](https://zenodo.org/records/19861032/files/README.md?download=1)
- **HomeOccupancy**: ESP32-C6의 `empty / sleep / work` 데이터다. 우선
  빈 공간과 저움직임 재실 상태를 구분하는 보조 실험에 적합하다. `work`를 현재
  `motion`으로 자동 대응시키지는 않는다. [데이터 카드](https://huggingface.co/datasets/gadgadgad/HomeOccupancy)
- **WiMANS**: 사람이 없는 샘플과 사람이 가만히 서 있는 샘플을 명확히 구분할
  수 있다. 장비 차이를 감수한 3-class 재구성·외부 벤치마크 후보로 적합하다.
  [논문 및 데이터 설명](https://arxiv.org/html/2402.09430v1)

**조사한 후보 중 현재 `preprocess_3rx.py → LSTM.py`에 수정 없이 넣을 수 있는
데이터셋은 확인되지 않았다.** 외부 데이터는 모델·특징 추출 방법을 검증하거나
사전학습에 활용하는 보조 자료이며, 실제 배치 환경에서 수집한 독립 평가 데이터를
대체하지 않는다.

## 2. Project Requirements

| 항목 | 현재 프로젝트 기준 |
|---|---|
| 목표 | `empty=0`, `static=1`, `motion=2` 분류 |
| 장비·무선 설정 | ESP32-S3, 1 TX + 공간적으로 분리된 3 RX, 2.4 GHz HT20, LLTF |
| 입력 특징 | RX당 CSI amplitude 64개, RX101·102·103 순서로 결합한 192개 |
| 시간 축 | 목표 100 Hz, 3초 window `(300, 192)`, stride 30 |
| 정렬·품질 | 공통 `tx_seq` 정렬, 세션 공통 길이 최소 27,000 frame |
| 평가 | 세션 단위 train/validation/test 분리, train 통계로 정규화 |

근거: [Firmware](../../../doc/firmware.md),
[Preprocessing Design](../preprocessing/design.md), [LSTM 구현](../../lstm/LSTM.py).

기존 학습은 29개 세션을 train 17 / validation 6 / test 6으로 나누었다.
선택된 설정의 seed 3개에서 validation macro-F1은 `0.9860 ± 0.0104`,
test window-level macro-F1은 `0.7051 ± 0.0066`이었다. 모두 같은 두 test
세션에서 `empty`와 `static`을 혼동했다. 따라서 이번 조사는 움직임 종류를
더 늘리는 것보다 **빈 공간·정지한 사람·수집 환경 변화**를 포함하는 자료에
우선순위를 두었다. 이 결과만으로 데이터셋이 유일한 원인이라고 단정할 수는 없다.
근거: [Baseline Report](lstm-baseline-report.md).

## 3. Candidate Comparison

용량은 배포 페이지 또는 파일 메타데이터 기준이며, 압축 해제·전처리 산출물의
추가 공간은 포함하지 않는다. `우선`도 즉시 학습 가능하다는 뜻은 아니다.

| 후보·배포처 | 공개 데이터의 특징 | 현재 프로젝트에서의 용도 | 판단 |
|---|---|---|---|
| [OpenCSI](https://zenodo.org/records/19861032) | S3·C3·C6, 4회 × 12분, 압축 JSONL, 약 724 MB | 동일 의미의 3-class 실험, 환경·장비 변화 및 정규화 비교 | **우선** |
| [HomeOccupancy](https://huggingface.co/datasets/gadgadgad/HomeOccupancy) | ESP32-C6 1-RX, `empty/sleep/work`, 약 150분, CSV | `empty` 대 저움직임 재실 구분, amplitude 특징 비교 | **HF 우선** |
| [HomeHAR](https://huggingface.co/datasets/gadgadgad/HomeHAR) | ESP32-C6 1-RX, 7종 활동, 약 465분, 2.53 GB | 시간 간격이 큰 수집분에서 특징·모델 일반화 확인 | 후속 |
| [WiMANS](https://github.com/huangshk/WiMANS) | Intel 5300, 11,286개 3초 샘플, 0~5명, raw MAT·amplitude NPY | 0~1명 부분집합의 3-class 재구성, 환경별 평가 | 후속 |
| [Exposing the CSI](https://github.com/ansresearch/exposing-the-csi) | 공간적으로 분리된 3 RX, 12종 활동·7시나리오, 시나리오당 약 11 GB | 3-RX 방식과 빈 공간·정지 자세의 외부 비교 | 변환 비용 큼 |
| [CSI-Bench](https://github.com/guozhen-jenn-zhu/CSI-Bench-Real-WiFi-Sensing-Benchmark) | 여러 sensing task, H5/MAT 및 환경·사용자·장비별 split | 본격적인 환경·장비 일반화 벤치마크 | 장기 후보 |
| [UT-HAR / NTU-Fi HAR — SenseFi](https://github.com/xyanchen/WiFi-CSI-Sensing-Benchmark) | 전처리 데이터와 PyTorch 모델·가중치 제공 | LSTM·CNN 등 모델 구현 비교, 사전학습 방법 검토 | 보조 후보 |

## 4. Recommended Datasets

### 4.1 OpenCSI: First Compatibility Audit

대상은 2026년 공개된 **OpenCSI LCN 2026 Dataset**이다. 초기 검증에는
`envA_S3`와 `envB_S3prime` 수집분을 우선 사용한다. 전체는 3개 방·4개 배치의
기록이며, 각 배치의 8개 노드가 56개 방향성 링크를 만든다.
S3·C3는 HT20의 52개 유효 subcarrier, C6는 HE20의 246개를 사용한다.
[논문 수집 조건](https://arxiv.org/html/2607.26665v1)

원본 설명에 `annotations.jsonl`, `raw/csi_raw.jsonl.zst`, `trial.json`이
명시되어 있어 라벨과 수집 조건을 함께 읽을 수 있다. 참가자는 익명 1명으로
설명되어 있으므로 사람 간 일반화 검증에는 제한이 있다. 라벨 대응안은 다음과 같다.
[배포 README](https://zenodo.org/records/19861032/files/README.md?download=1)

| 원본 라벨 | 프로젝트 라벨 |
|---|---|
| `empty` | `empty=0` |
| `occupied_static` | `static=1` |
| `occupied_moving` | `motion=2` |

적용 시 다음 조건이 필요하다.

- 원본 20 Hz에서 3초는 약 60 frame이다. 300 frame으로 보간해도 실제 관측
  정보가 늘지는 않는다. 우선 원래 시간 해상도의 별도 baseline을 만든다.
- mesh 전체를 현재 1-TX/3-RX와 동일시하지 않는다. 동일 TX의 3개 RX 링크를
  추출하려면 실제 패킷 ID·시각·동기화 정보·결측률을 먼저 확인해야 한다.
- 4개 기록만 있으므로 대규모 데이터 증강보다는 일반화 진단에 적합하다.
  같은 기록의 다른 링크나 중첩 window를 train/test로 나누면 안 된다.
- 기록은 빈 공간 → 정지 → 움직임 → 빈 공간 순서다. 전환부 window를 제외하고,
  시간 순서를 라벨 대신 학습하지 않는지 확인한다.

논문의 주요 평가는 `static`과 `moving`을 합친 **재실 여부 2-class 평가**다.
보고된 성능을 현재 3-class LSTM의 기대 성능으로 인용하지 않는다. 또한 빈 공간
초기 보정이 필요한 방법은 보정 구간을 평가 구간과 분리하고, 대상 환경 데이터를
사용했다는 조건을 명시해야 한다. [논문 평가 프로토콜](https://arxiv.org/html/2607.26665v1)

### 4.2 HomeOccupancy and HomeHAR: Hugging Face Candidates

두 데이터셋 모두 ESP32-C6 2대가 **1 TX + 1 RX**로 동작한다. 2대라는 설명은
2-RX라는 뜻이 아니다. CSV의 128개 정수에서 64개 복소수 값을 읽으며, 저자
전처리는 그중 52개 LLTF 값을 선택하고 불규칙한 수신 시각을 150 Hz로 재표본화한다.
현재 3-RX 입력과 별도의 1-RX 실험이 필요하다.
[저자 프로젝트의 수집·처리 구조](https://gadm21.github.io/WifiSensingESP32HAR/)

**HomeOccupancy**는 `empty`, `sleep`, `work` 각 3개, 총 9개 CSV를 제공한다.
배포 메타데이터는 `_1`, `_2`를 train, `_3`를 test로 지정한다. 우선
`empty` 대 `sleep`의 보조 2-class 실험, 또는 원래 3개 라벨의 별도 benchmark를
권한다. `work`의 타이핑·마우스 조작이 프로젝트의 `static` 또는 `motion` 중
어디에 해당하는지는 수집 정의를 대조해야 한다.
[데이터 카드](https://huggingface.co/datasets/gadgadgad/HomeOccupancy),
[파일별 split 메타데이터](https://huggingface.co/datasets/gadgadgad/HomeOccupancy/blob/6eda463a036aa3d8b471522fb3d8828a1c2866ee/dataset_metadata.json)

**HomeHAR**에는 `empty`, `sleep`, `watch`, `drink`, `eat`, `smoke`, `work`가
있다. `data1/`, `data2/`는 2025년 10월 train 수집분, `data3/`는 2026년 2월
test 수집분이다. 현재 문제와 관련된 장기간 수집 조건 변화 실험에 유용하지만,
정지와 미세 동작이 섞인 클래스들을 단순히 `static/motion`으로 합치면 라벨
의미가 달라질 수 있다. [데이터 카드](https://huggingface.co/datasets/gadgadgad/HomeHAR)

수집 설명에서 발견한 확인 사항도 있다.

- HomeOccupancy 카드·메타데이터는 파일 holdout을 설명하지만, 프로젝트 표는
  test를 `same-session`으로 적는다. **별도 파일이라는 이유만으로 독립 날짜·환경
  평가라고 주장하지 않는다.** [프로젝트 데이터 표](https://gadm21.github.io/WifiSensingESP32HAR/)
- HomeHAR 카드의 총 패킷 수 설명과 프로젝트의 train/test 합계가 일치하지 않는다.
  실제 사용량은 파일 파싱 후 다시 집계해야 한다.
  [카드](https://huggingface.co/datasets/gadgadgad/HomeHAR),
  [프로젝트 데이터 표](https://gadm21.github.io/WifiSensingESP32HAR/)
- 조사 시 HomeHAR의 HF viewer에 CSV `ParserError`가 표시되었다. 이는 원본
  전체가 사용할 수 없다는 증거는 아니지만, `load_dataset()` 한 줄로 정상 로딩된다고
  가정할 수는 없다. [HF viewer](https://huggingface.co/datasets/gadgadgad/HomeHAR)

같은 저자의 [OfficeHAR](https://huggingface.co/datasets/gadgadgad/OfficeHAR)도
`empty/watch/work/eat`를 포함하지만 파일 내부 80/20 시간 분할을 사용한다.
별도 세션 일반화의 근거보다는 추가 1-RX 특징 비교 자료로 취급한다.

### 4.3 WiMANS: Explicit Empty and Stationary Labels

가장 유용한 구분은 **사람 수 0명**과 **사람이 있으나 `Nothing`을 수행하는 상태**다.
논문은 `Nothing`을 지정 위치에 가만히 서 있는 행동으로 설명한다.
초기 실험은 2.4 GHz·0~1명 부분집합으로 제한하는 편이 라벨 해석이 단순하다.
[논문 Appendix A 및 수집 조건](https://arxiv.org/html/2402.09430v1)

| 선택 조건 | 대응안 |
|---|---|
| 사람 수 0명 | `empty` |
| 사람 수 1명 + `Nothing` | `static` |
| 사람 수 1명 + `Walking/Rotation/Jumping/Waving` 등 명확한 동작 | `motion` |
| 다중 사용자·경계가 모호한 샘플 | 첫 실험에서는 제외 |

`Empty Room`은 가구가 없는 **환경 이름**이므로 사람이 없다는 라벨이 아니다.
또한 `Sitting Down`, `Standing Up`, `Lying Down`은 자세를 바꾸는 동작이므로
정지 자세로 자동 분류하지 않는다. [활동 정의](https://arxiv.org/html/2402.09430v1)

원본의 목표 shape는 `(3000, 3, 3, 30)`이다. 이는 1,000 Hz의 시간 축과
TX/RX 안테나 조합이며, 한 시점당 270개 복소수 값이다. 안테나 3개는 공간적으로
분리된 ESP32 RX 3대와 다르다. 실제 패킷 누락을 검사한 뒤 amplitude 변환·시간축
처리·입력 차원 변경이 필요하다. [논문 입력 정의](https://arxiv.org/html/2402.09430v1)

저자 저장소에는 amplitude `.npy`와 `annotation.csv` 사용법이 있어 원시 CSI
추출부터 재구현할 필요는 없다. 데이터는 저자 저장소에서 연결한 Kaggle로 배포된다.
[저장소](https://github.com/huangshk/WiMANS),
[데이터 다운로드 페이지](https://www.kaggle.com/datasets/shuokanghuang/wimans)

### 4.4 Exposing the CSI: Multi-Receiver Reference

서로 다른 위치의 RX 3개가 같은 frame을 받는다는 점에서 프로젝트와 구조적으로
가깝다. `Empty room`은 `empty`, `Sitting/Standing`은 `static`,
`Walk/Run/Jump` 등은 `motion`의 재라벨링 후보가 된다. 다만 4-antenna·160 MHz
802.11ax 장비를 사용하므로 ESP32 HT20과의 신호 차이가 크다.
[저자 저장소](https://github.com/ansresearch/exposing-the-csi)

3명·3개 환경의 7개 시나리오가 있고, 일부는 날짜도 다르다. 시나리오별 원본
정렬을 보존하여 환경·날짜별 holdout을 구성하는 용도가 적합하다. 전체를 한 번에
받기보다 S1 한 묶음으로 포맷을 확인하는 것이 합리적이다. S1 ZIP은 약 11.1 GB이며
나머지 시나리오와 전처리 결과까지 저장하면 공간 요구량이 커진다.
[시나리오 목록](https://github.com/ansresearch/exposing-the-csi),
[S1 배포 메타데이터](https://zenodo.org/api/records/7732595)

## 5. Secondary and Deferred Candidates

### 5.1 CSI-Bench and SenseFi

**CSI-Bench**는 환경·사용자·장비 변화별 평가 구조를 참고하기 좋다. 저자 저장소는
데이터 문제 수정 후 **Kaggle Version 12**로 실험을 다시 수행했다고 명시한다.
도입 시 버전과 split 파일을 고정하고, 예전 fork의 결과와 섞지 않는다. 여러 task의
라벨이 현재 3-class와 같다고 가정해서는 안 된다.
[저자 저장소 및 수정 안내](https://github.com/guozhen-jenn-zhu/CSI-Bench-Real-WiFi-Sensing-Benchmark),
[데이터 배포처](https://www.kaggle.com/datasets/guozhenjennzhu/csi-bench)

**SenseFi의 UT-HAR / NTU-Fi HAR**는 전처리된 입력과 LSTM을 포함한 PyTorch
구현이 있어 외부 benchmark를 시작하기 편하다. 제공 입력은 각각
`(1, 250, 90)`, `(3, 114, 500)`이며, 공개 클래스 목록에 `empty`가 없다.
현재 empty/static 문제를 직접 보완하는 데이터로는 우선순위가 낮다.
배포된 사전학습 가중치도 현재 2-layer LSTM과 동일한 구조의 가중치가 아니다.
[SenseFi 데이터·모델 설명 및 다운로드 링크](https://github.com/xyanchen/WiFi-CSI-Sensing-Benchmark)

### 5.2 Datasets Not Selected for Immediate Use

| 후보 | 보류·제외 이유 |
|---|---|
| [Haron98/WIFI-CSI-Dataset](https://huggingface.co/datasets/Haron98/WIFI-CSI-Dataset) | 3-RX·64개 복소수 값으로 형태는 유사하지만 `label_00..03`의 클래스 이름이 모두 미기재다. 상단 MIT 표기와 본문의 명시 라이선스 없음 설명도 불일치한다. 라벨·이용 조건 확인 전 지도학습용 선정 보류 |
| [ilyakolosov/WIFI-CSI-Dataset](https://huggingface.co/datasets/ilyakolosov/WIFI-CSI-Dataset) | 위 카드와 구조·집계·라벨 설명이 유사하다. 별개 데이터 양으로 합산하지 말고 원본 출처와 파일 hash 중복 여부부터 확인 |
| [CEX52014/WiFi-CSI-Dataset](https://github.com/CEX52014/WiFi-CSI-Dataset) | 100 Hz·3초라는 조건은 비슷하지만 저자가 합성 데이터라고 명시한다. 실제 환경 일반화의 검증 자료에서는 제외 |
| [OfficeLocalization](https://huggingface.co/datasets/gadgadgad/OfficeLocalization) | 위치 구분이 목적이다. `one/two/five`는 사람 수가 아닌 zone 라벨이며 `static/motion`을 직접 제공하지 않음 |

Haron98 카드에는 첫 줄 손상 등 알려진 파싱 문제도 기록되어 있다. 향후 라벨
문제가 해결되더라도 `.data` 전용 parser와 RX 사이 시각·패킷 정렬 확인이 필요하다.
[데이터 품질·포맷 설명](https://huggingface.co/datasets/Haron98/WIFI-CSI-Dataset)

## 6. Access, Versions, and Licenses

다음은 배포자가 명시한 조건을 정리한 것이며, 코드 라이선스를 데이터 전체의
라이선스로 대신 해석하지 않았다. 재배포·상용 적용 시 사용할 버전의 원문 조건을
별도로 확인한다.

| 데이터 | 확인한 배포 조건·버전 | 다운로드 전 확인 사항 |
|---|---|---|
| OpenCSI | Zenodo `19861032`, `v1`; README·DATASHEET에 **CC BY 4.0** 명시 | 약 724 MB 압축 묶음, 내부 LICENSE·manifest 및 raw 스키마 검증 |
| HomeOccupancy | HF revision `6eda463a036aa3d8b471522fb3d8828a1c2866ee`; **CC BY 4.0**, API상 `gated=false` | CSV 9개 합계 754,141,217 bytes, 약 754 MB; 실제 레코드 품질 확인 |
| HomeHAR / OfficeHAR | 데이터 카드에 **CC BY 4.0** 명시 | 사용할 HF revision 고정; CSV 파싱·수집 단위 확인 |
| WiMANS | 논문 데이터 이용 조건에 **CC BY-NC-SA 4.0**, 학술 연구 목적 명시 | Kaggle 배포 버전·용량·현재 이용 조건 확인; 상용 적용 전 재검토 |
| Exposing the CSI | S1 Zenodo `7732595`, `1.0`, **CC BY 4.0**, 공개 접근 | 이 조사에서는 S1 메타데이터 확인; S2~S7은 각 레코드 조건도 확인 |
| CSI-Bench / SenseFi 수록 데이터 | 코드 저장소는 MIT이나 이것만으로 모든 데이터의 이용 조건을 확정하지 않음 | Kaggle·원본 데이터별 조건과 실제 다운로드 접근 여부 확인 |

라이선스·용량 근거:
[OpenCSI DATASHEET](https://zenodo.org/records/19861032/files/DATASHEET.md?download=1),
[OpenCSI 파일 목록](https://zenodo.org/records/19861032),
[HomeOccupancy API](https://huggingface.co/api/datasets/gadgadgad/HomeOccupancy),
[HomeOccupancy 고정 버전 파일 목록](https://huggingface.co/api/datasets/gadgadgad/HomeOccupancy/tree/6eda463a036aa3d8b471522fb3d8828a1c2866ee),
[HomeHAR 카드](https://huggingface.co/datasets/gadgadgad/HomeHAR),
[OfficeHAR 카드](https://huggingface.co/datasets/gadgadgad/OfficeHAR),
[WiMANS Appendix E.5/E.6](https://arxiv.org/html/2402.09430v1),
[Exposing S1 메타데이터](https://zenodo.org/api/records/7732595).

## 7. Integration Requirements

### 7.1 Preserve the Existing Baseline Contract

현재 [LSTM.py](../../lstm/LSTM.py)는 shape뿐 아니라 label map, RX 순서,
정규화 정보, 세션 중복을 검사한다. `test` 명령은 학습 때 저장한 dataset manifest와
normalization의 SHA-256까지 비교한다. 따라서 외부 파일의 shape만 맞춰도 기존
`best-model.pt`를 그대로 공식 test 명령으로 평가할 수 있는 구조가 아니다.

외부 데이터용 loader·명시적인 데이터 계약·별도 실험 실행 경로를 추가해야 한다.
기존 검사나 hash를 우회하지 말고, 기존 학습 산출물은 재현 가능한 기준선으로
보존한다. 공개 가중치 불러오기나 사전학습 후 fine-tuning도 현재 공식 CLI에
구현된 작업 흐름이 아니므로 추가 구현 대상이다.

### 7.2 Convert Meaning, Not Only Shape

- **원본 해석:** 데이터셋마다 I/Q 순서, subcarrier index, LTF 종류, 진폭 scaling을
  확인한다. 현재의 64개 전체 사용과 외부의 52개 선택을 동일 특징이라고 가정하지 않는다.
- **시간 축:** timestamp와 실제 수신률로 초 단위를 보존한다. 재표본화 시 결측·중복을
  먼저 처리하고, 고속 신호를 낮출 때 저역통과 처리를 검토한다.
- **RX 구조:** 1-RX를 세 번 복사하거나 NIC 안테나를 RX101·102·103으로 이름만
  바꾸지 않는다. 우선 source-native 차원의 별도 모델로 비교한다.
- **짧은 기록:** 3초짜리 외부 샘플을 현재 최소 27,000 frame 세션 조건에 맞추기 위해
  반복 연결하지 않는다. 원본 trial을 보존하는 전용 loader로 처리한다.
- **출처 추적:** dataset ID·revision·license·원본 파일 hash·trial·사용자·환경·RX·
  원본 라벨·변환 라벨·sampling rate를 외부 데이터 manifest에 남긴다.

### 7.3 Prevent Evaluation Leakage

먼저 원본 기록·사용자·환경 단위로 split을 정하고 **그다음 window를 생성한다.**
같은 시간대의 중첩 window, 같은 패킷을 받은 여러 RX, 재정리된 복사본이 서로 다른
split에 들어가지 않도록 한다. 정규화 통계와 class weight 값은 train에서만 계산한다.
특징·설정 및 class weight 사용 여부는 validation으로 선택하며, 교차검증 시
각 fold의 train으로 통계를 다시 계산한다.

OpenCSI처럼 한 세션 안에 여러 라벨이 있는 데이터는 현재의 “세션당 단일 라벨”
session-level 평가에 그대로 넣지 않는다. 구간별 라벨에 맞는 평가를 추가하고,
동일 trial의 구간을 독립 세션 수로 부풀리지 않는다.

외부 사전학습을 하더라도 외부 test를 학습에 합치지 않는다. 자체 test 결과를 보고
외부 데이터 선택·변환 규칙을 반복 조정하지도 않는다. 기존 자체 test는 이미 확인한
결과이므로 후속 최종 성능은 수집 담당자가 제공하는 **새로운 미사용 holdout**으로
평가한다. [기존 평가의 후속 원칙](training-results-summary.md)

## 8. Proposed Next Experiment

1. **OpenCSI 파일 검증:** S3 원본에서 라벨·타임스탬프·링크·I/Q 스키마를 확인한다.
   3-RX 동기화 부분집합을 만들 수 있는지 판정하고, 불가능하면 링크별 baseline으로 시작한다.
2. **작은 외부 baseline:** 별도 실험 경로에서 원본 시간 해상도로 동작하는 LSTM과
   단순 amplitude 통계 모델을 비교한다. 원본 기록 단위 평가를 지키고,
   정규화 방식의 효과와 모델 구조의 효과를 구분한다.
3. **HF 보조 검증:** HomeOccupancy의 `empty/sleep`로 정지 재실 구분을 확인한다.
   그다음 HomeHAR의 시간 holdout으로 같은 특징의 안정성을 확인한다.
4. **자체 데이터에서 가치 확인:** 입력 의미를 공유하는 encoder 등 전이 구조를
   설계한 뒤, 같은 자체 train/validation에서 무사전학습 대비 사전학습의 차이를
   비교한다. 개선이 검증된 설정만 seed 3개로 고정하여 새 holdout에 각각 한 번 평가한다.

최종 보고 지표는 macro-F1, 클래스별 precision/recall/F1, confusion matrix,
독립 기록별 결과, seed 평균·표준편차로 한다. 외부 데이터와 자체 데이터 결과는
별도로 보고한다. **외부 benchmark의 높은 accuracy를 현재 프로젝트의
macro-F1 개선으로 해석하지 않는다.**

데이터 수집 담당자는 계속 날짜·위치·배치가 다양한 `empty/static/motion` 독립
세션을 확보하고, 모델 담당자는 위 외부 데이터 검증을 수행하는 역할 분담을 권한다.
외부 데이터 도입은 수집을 중단할 근거가 아니라, 어떤 특징과 학습 방식이
수집 조건 변화에 견디는지 확인하는 추가 실험이다.
