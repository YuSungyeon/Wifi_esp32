# 실시간 상태 판단 구현 계획

> **작업자 안내:** 이 계획은 task 단위로 실행한다. 각 step 은 체크박스로 추적하며,
> 모든 코드 변경은 **실패하는 테스트 → 최소 구현 → 통과 확인 → 커밋** 순서를 지킨다.

> **2026-09-30 기록 — 이 계획대로 구현하지 않았다.** 작성 직후 모델 담당의 60세션과 배포
> 체크포인트(3-class 단일 모델)가 합류해 전제가 바뀌었다. Task 1~4(2단계 모델 학습)는
> 건너뛰고 Task 5~10(스트림·상태 기계·엔진·CLI·제어판)만 단일 모델 기준으로 구현했다.
> 현재 설계와 구현 현황은 [realtime-inference.md](../realtime-inference.md) 가 정본이다.
> 이 문서는 2단계 구성을 어떻게 쪼개려 했는지에 대한 기록으로 남긴다.

**Goal:** 수집 중인 `.csi` 를 따라 읽어 방 상태(`empty`/`static`/`motion`/`deciding`)를
실시간으로 판단하고, CLI 와 제어판에 표시하며 상태 전환을 파일로 기록한다.

**Architecture:** 2단계 모델 — 1단계는 진폭 3초 창으로 움직임 여부, 2단계는 위상 W초 창으로
`empty`/`static`. 상태 기계가 둘을 합쳐 최종 상태를 낸다. 수집 코드는 건드리지 않고 저장 중인
`.csi` 를 tail 로 읽는다. 같은 엔진이 녹화 파일 재생에도 쓰여 오프라인 평가가 된다.

**Tech Stack:** Python 3.14, numpy, PyTorch 2.14 (MPS/CPU), 표준 라이브러리 `http.server`
(제어판), pytest. 새 서드파티 의존성 없음.

**Spec:** [doc/realtime-inference.md](../realtime-inference.md)

## Global Constraints

- 모든 Python 실행은 저장소 루트의 `.venv` 로 한다: `.venv/bin/python`.
- 테스트는 `tests/` 에 `unittest` 스타일로 두고 `.venv/bin/python -m pytest tests/... -v` 로 돌린다.
  기존 테스트와 같이 `sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))`
  로 `scripts/` 모듈을 import 한다.
- **수집 경로(`csi_serial_reader.py`, 펌웨어, `.csi` 포맷)는 수정하지 않는다.** CLAUDE.md
  "손대면 안 되는 것" 참조.
- 라벨 어휘는 `csi_store.LABELS = ("empty", "static", "motion")` 를 단일 소스로 쓴다.
  실시간 전용 상태 `deciding` 은 라벨이 아니라 화면·이벤트 표기값이다.
- 유효 톤은 `csi_store.LLTF_DATA_IDX` (52개), 3-RX 이므로 입력 시계열은 156개.
- 위상은 `csi_store.complex_csi` 를 쓴다. **2026-09-24 에 I/Q 순서 버그가 고쳐졌으므로
  그 이전에 만든 `runs/pilot_cv/cache/s*_phase.npz` 는 전부 지우고 다시 만든다.**
- 사용자 실험 데이터(`mac_collector_output/`)와 registry 값은 명시적 요청 없이 바꾸지 않는다.
- 커밋 메시지 끝에 `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` 를 붙인다.
- 상태 색·크기·공간 배분은 spec §8 을 그대로 따른다. 새 색을 만들지 않는다.

## 파일 구조

| 파일 | 책임 |
|---|---|
| `model_train/cnn1d/shared_cnn.py` (신규) | `SharedTemporalCNN` 정의 — 실험(cv_pilot)과 실시간이 공유 |
| `model_train/cnn1d/cv_pilot.py` (수정) | 모델 정의를 import 로 교체, `--rx-subset` 추가 |
| `model_train/cnn1d/summarize_runs.py` (신규) | 여러 run 의 results.json 을 표로 + W 선택 규칙 적용 |
| `realtime/train.py` (신규) | 1·2단계 최종 모델 학습·저장 (가중치 + 메타데이터) |
| `realtime/stream.py` (신규) | `.csi` tail 읽기와 tx_seq 격자 정렬 링버퍼 |
| `realtime/features.py` (신규) | 링버퍼 → 모델 입력 텐서 (진폭 3초 / 위상 W초) |
| `realtime/state.py` (신규) | 상태 기계 (spec §4 규칙 ①~⑤) |
| `realtime/engine.py` (신규) | 루프: stream → features → 모델 → 상태 기계 → 이벤트/상태 파일 |
| `realtime/ui.py` (신규) | CLI 렌더링(전체 화면 / `--plain`)과 실행 진입점 |
| `scripts/meshsense_gui.py` (수정) | `실시간` 탭과 `/api/realtime` |
| `tests/test_realtime_*.py` (신규) | 위 모듈별 테스트 |

`realtime/` 는 모델과 수집 어디에도 속하지 않는 소비자라 최상위에 둔다. 모델 구조 정의만
`model_train/` 에 남겨 학습 코드와 공유한다.

---

### Task 1: `SharedTemporalCNN` 을 공용 모듈로 빼고 RX 일부 입력을 지원한다

실시간 엔진과 학습 코드가 같은 모델 정의를 써야 한다. 동시에 RX 한 대가 빠졌을 때(저하 모드)
입력 시계열이 104개가 되는데, 현재 구조가 실제로 그대로 도는지 테스트로 못 박는다.

**Files:**
- Create: `model_train/cnn1d/shared_cnn.py`
- Modify: `model_train/cnn1d/cv_pilot.py` (클래스 정의 삭제 → import, `--rx-subset` 추가)
- Test: `tests/test_realtime_model.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `SharedTemporalCNN(dropout: float = 0.2, groups: tuple[int, ...] = (156,), n_classes: int = 3)`
  - `forward(x: Tensor[B, T, F]) -> Tensor[B, n_classes]`, `F == sum(groups)`
  - `RX_ORDER: tuple[int, ...] = (101, 102, 103)`
  - `rx_columns(rx_ids: Sequence[int]) -> np.ndarray` — 주어진 RX 의 156열 중 열 인덱스

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_realtime_model.py
import sys, unittest
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from model_train.cnn1d.shared_cnn import RX_ORDER, SharedTemporalCNN, rx_columns


class SharedCnnTest(unittest.TestCase):
    def test_forward_shape_for_three_rx(self):
        model = SharedTemporalCNN(groups=(156,), n_classes=3).eval()
        with torch.no_grad():
            out = model(torch.zeros(2, 100, 156))
        self.assertEqual(tuple(out.shape), (2, 3))

    def test_two_rx_input_runs_with_matching_groups(self):
        """RX 한 대가 빠져도 구조상 계산이 성립한다 (저하 모드의 전제)."""
        model = SharedTemporalCNN(groups=(104,), n_classes=2).eval()
        with torch.no_grad():
            out = model(torch.zeros(1, 100, 104))
        self.assertEqual(tuple(out.shape), (1, 2))

    def test_rx_columns_selects_contiguous_blocks_in_rx_order(self):
        cols = rx_columns([101, 103])
        self.assertEqual(len(cols), 104)
        np.testing.assert_array_equal(cols[:52], np.arange(0, 52))
        np.testing.assert_array_equal(cols[52:], np.arange(104, 156))

    def test_rx_columns_rejects_unknown_rx(self):
        with self.assertRaises(ValueError):
            rx_columns([101, 999])

    def test_cv_pilot_reuses_the_shared_definition(self):
        from model_train.cnn1d import cv_pilot
        self.assertIs(cv_pilot.SharedTemporalCNN, SharedTemporalCNN)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_model.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'model_train.cnn1d.shared_cnn'`

- [ ] **Step 3: 모듈을 만든다**

`cv_pilot.py` 의 `SharedTemporalCNN` 을 **그대로** 옮긴다(동작 변경 금지). 주석도 함께 옮긴다.

```python
#!/usr/bin/env python3
"""서브캐리어 공유 시간 인코더 — 실험(cv_pilot)과 실시간 추론이 함께 쓴다."""
from __future__ import annotations

from typing import Sequence

import numpy as np
import torch
from torch import nn

#: 3-RX 입력의 열 순서. 156열 = RX 3대 × 유효 톤 52개.
RX_ORDER: tuple[int, ...] = (101, 102, 103)
TONES_PER_RX = 52


def rx_columns(rx_ids: Sequence[int]) -> np.ndarray:
    """주어진 RX 들이 차지하는 열 인덱스. RX_ORDER 순서를 유지한다."""
    unknown = [r for r in rx_ids if r not in RX_ORDER]
    if unknown:
        raise ValueError(f"알 수 없는 RX: {unknown} (가능: {list(RX_ORDER)})")
    keep = [i for i, rx in enumerate(RX_ORDER) if rx in set(rx_ids)]
    return np.concatenate([np.arange(i * TONES_PER_RX, (i + 1) * TONES_PER_RX) for i in keep])


class SharedTemporalCNN(nn.Module):
    """모든 서브캐리어 시계열에 같은 가중치의 시간축 Conv를 적용하고 통계로 풀링한다.

    특정 서브캐리어 조합(=배치별 다중경로 지문)을 외울 수 없게 하는 구조다.
    """

    def __init__(self, dropout: float = 0.2, groups: tuple = (156,), n_classes: int = 3) -> None:
        super().__init__()

        def block(cin, cout, k):
            return [nn.Conv1d(cin, cout, k, padding=k // 2, bias=False),
                    nn.BatchNorm1d(cout), nn.ReLU()]

        self.groups = list(groups)
        self.encoders = nn.ModuleList(
            nn.Sequential(*block(1, 16, 5), nn.MaxPool1d(2),
                          *block(16, 32, 5), nn.MaxPool1d(2), *block(32, 32, 3))
            for _ in self.groups)
        self.head = nn.Sequential(nn.Linear(128 * len(self.groups), 64), nn.ReLU(),
                                  nn.Dropout(dropout), nn.Linear(64, n_classes))

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # (B, T, F)
        pooled = []
        for enc, part in zip(self.encoders, torch.split(x, self.groups, dim=2)):
            b, t, f = part.shape
            h = enc(part.permute(0, 2, 1).reshape(b * f, 1, t))
            per_series = torch.cat([h.mean(2), h.std(2)], 1).view(b, f, -1)
            pooled += [per_series.mean(1), per_series.std(1)]
        return self.head(torch.cat(pooled, 1))
```

- [ ] **Step 4: `cv_pilot.py` 가 이 정의를 쓰게 바꾼다**

클래스 정의를 지우고 import 를 추가한다. `VALID_FEATURES` 아래에 둔다.

```python
from model_train.cnn1d.shared_cnn import RX_ORDER, SharedTemporalCNN, rx_columns  # noqa: E402
```

`cmd_cv` 의 세션 로딩 직후에 RX 부분 선택을 넣는다(§6-⑤ 실험용).

```python
    if args.rx_subset:
        cols = rx_columns(args.rx_subset)
        for v in sessions.values():
            v["x"] = np.ascontiguousarray(v["x"].reshape(len(v["x"]), 3, 64)[:, [
                RX_ORDER.index(r) for r in args.rx_subset], :].reshape(len(v["x"]), -1))
            if "phase" in v:
                v["phase"] = np.ascontiguousarray(v["phase"][:, cols])
```

`run_fold` 의 group 크기 계산도 열 수를 따라가게 한다.

```python
        n_series = len(VALID_FEATURES) if "amp" in args.features else sessions[
            next(iter(sessions))]["phase"].shape[1]
        n_groups = len(args.features.split("+")) * (2 if args.pool == "mean+std" else 1)
        groups = tuple(n_series for _ in range(n_groups))
```

인자를 추가한다.

```python
    cv.add_argument("--rx-subset", type=int, nargs="+", default=None,
                    help="이 RX 들만 입력으로 쓴다 (예: --rx-subset 101 103). 저하 모드 실험용")
```

- [ ] **Step 5: 테스트 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_model.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: 기존 실험이 그대로 도는지 확인한다**

Run: `.venv/bin/python model_train/cnn1d/cv_pilot.py cv --name t1-smoke --model shared --norm window --features phase --window 3000 --downsample 20 --seeds 0 --epochs 1`
Expected: 첫 fold 가 오류 없이 끝난다. 확인 후 `rm -rf model_train/cnn1d/runs/pilot_cv/t1-smoke`

- [ ] **Step 7: 커밋**

```bash
git add model_train/cnn1d/shared_cnn.py model_train/cnn1d/cv_pilot.py tests/test_realtime_model.py
git commit -m "refactor : SharedTemporalCNN 공용 모듈 분리 + --rx-subset (저하 모드 실험)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: run 요약기 — W 선택 규칙을 코드로 고정한다

spec §5 의 선택 규칙("최고 정답률과 0.02 이내인 후보 중 가장 짧은 W")을 사람이 눈으로
고르면 사후 편향이 생긴다. 규칙을 실행 코드로 만들어 결과를 보기 전에 박아 둔다.

**Files:**
- Create: `model_train/cnn1d/summarize_runs.py`
- Test: `tests/test_summarize_runs.py`

**Interfaces:**
- Consumes: `runs/pilot_cv/<name>/{config.json,results.json}` (기존 형식)
- Produces:
  - `load_run(run_dir: Path) -> dict` — `{"name", "window", "macro_f1", "std", "session_acc", "es_window_acc"}`
  - `choose_window(rows: list[dict], tolerance: float = 0.02) -> dict` — 선택된 행
  - `format_table(rows: list[dict]) -> str`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_summarize_runs.py
import json, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model_train.cnn1d.summarize_runs import choose_window, format_table, load_run


def _run(tmp, name, window, f1s, labels_preds):
    d = tmp / name
    d.mkdir()
    (d / "config.json").write_text(json.dumps({"name": name, "window": window}))
    results = [{"seed": 0, "test_block": b,
                "metrics": {"macro_f1": f1, "confusion_matrix": [[9, 1, 0], [1, 9, 0], [0, 0, 10]]},
                "sessions": {str(i): {"label": lab, "pred": pred, "window_acc": 1.0}
                             for i, (lab, pred) in enumerate(labels_preds)}}
               for b, f1 in zip("ABCD", f1s)]
    (d / "results.json").write_text(json.dumps(results))
    return d


class SummarizeRunsTest(unittest.TestCase):
    def test_load_run_reports_window_seconds_and_means(self):
        with tempfile.TemporaryDirectory() as t:
            tmp = Path(t)
            d = _run(tmp, "c-30s", 3000, [0.8, 0.8, 0.9, 0.9], [("empty", "empty")])
            row = load_run(d)
            self.assertEqual(row["window_s"], 30.0)
            self.assertAlmostEqual(row["macro_f1"], 0.85)
            self.assertAlmostEqual(row["session_acc"], 1.0)

    def test_choose_window_prefers_the_shortest_within_tolerance(self):
        rows = [{"name": "a", "window_s": 20.0, "macro_f1": 0.80},
                {"name": "b", "window_s": 45.0, "macro_f1": 0.84},
                {"name": "c", "window_s": 60.0, "macro_f1": 0.85}]
        self.assertEqual(choose_window(rows)["window_s"], 45.0)

    def test_choose_window_falls_back_to_the_best_when_nothing_is_close(self):
        rows = [{"name": "a", "window_s": 20.0, "macro_f1": 0.50},
                {"name": "b", "window_s": 60.0, "macro_f1": 0.85}]
        self.assertEqual(choose_window(rows)["window_s"], 60.0)

    def test_format_table_marks_the_chosen_row(self):
        rows = [{"name": "a", "window_s": 20.0, "macro_f1": 0.80, "std": 0.1,
                 "session_acc": 0.8, "wait_s": 23.0}]
        out = format_table(rows)
        self.assertIn("20", out)
        self.assertIn("0.800", out)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_summarize_runs.py -v`
Expected: FAIL — `ModuleNotFoundError: model_train.cnn1d.summarize_runs`

- [ ] **Step 3: 구현한다**

```python
#!/usr/bin/env python3
"""pilot_cv run 들을 한 표로 모으고 spec §5 의 W 선택 규칙을 적용한다.

    python model_train/cnn1d/summarize_runs.py runs/pilot_cv/w-*
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

#: 움직임이 멈춘 뒤 판단 확정까지의 고정 대기(규칙 ②). 대기 시간 = STOP_CONFIRM_S + W
STOP_CONFIRM_S = 3.0


def load_run(run_dir: Path) -> dict:
    cfg = json.loads((run_dir / "config.json").read_text())
    results = json.loads((run_dir / "results.json").read_text())
    f1 = np.array([r["metrics"]["macro_f1"] for r in results])
    sess = [v["label"] == v["pred"] for r in results for v in r["sessions"].values()]
    window_s = cfg["window"] / 100.0
    return {"name": cfg["name"], "window_s": window_s, "macro_f1": float(f1.mean()),
            "std": float(f1.std()), "session_acc": float(np.mean(sess)),
            "wait_s": STOP_CONFIRM_S + window_s}


def choose_window(rows: list[dict], tolerance: float = 0.02) -> dict:
    """최고 점수와 tolerance 이내인 후보 중 가장 짧은 W. 결과를 보기 전에 고정한 규칙이다."""
    best = max(r["macro_f1"] for r in rows)
    close = [r for r in rows if r["macro_f1"] >= best - tolerance]
    return min(close, key=lambda r: r["window_s"])


def format_table(rows: list[dict], chosen: dict | None = None) -> str:
    head = f"{'W(초)':>7} {'macro-F1':>10} {'±':>6} {'세션':>6} {'대기(초)':>9}  run"
    lines = [head, "-" * len(head)]
    for r in sorted(rows, key=lambda x: x["window_s"]):
        mark = " ←선택" if chosen and r["name"] == chosen["name"] else ""
        lines.append(f"{r['window_s']:7.0f} {r['macro_f1']:10.3f} {r.get('std', 0):6.3f} "
                     f"{r.get('session_acc', 0):6.2f} {r.get('wait_s', 0):9.0f}  {r['name']}{mark}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("runs", type=Path, nargs="+")
    ap.add_argument("--tolerance", type=float, default=0.02)
    args = ap.parse_args()
    rows = [load_run(d) for d in args.runs if (d / "results.json").is_file()]
    if not rows:
        print("results.json 이 있는 run 이 없습니다")
        return 1
    chosen = choose_window(rows, args.tolerance)
    print(format_table(rows, chosen))
    print(f"\n선택: {chosen['name']} (W={chosen['window_s']:.0f}초, "
          f"멈춘 뒤 확정까지 약 {chosen['wait_s']:.0f}초)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_summarize_runs.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 커밋**

```bash
git add model_train/cnn1d/summarize_runs.py tests/test_summarize_runs.py
git commit -m "feat : run 요약기 — W 선택 규칙(최고 대비 0.02 이내 중 최단)을 코드로 고정

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: W 스윕과 RX 손실 실험을 돌려 W 를 확정한다

코드가 아니라 **실험 실행** task 다. 결과가 이후 모든 task 의 상수(W)를 정한다.

**Files:**
- Modify: `model_train/docs/model-training/cnn1d-pilot-cv-report.md` (사이클 14~ 결과 추가)
- Modify: `doc/realtime-inference.md` (§5 에 확정된 W 기입)

- [ ] **Step 1: 위상 캐시를 다시 만든다**

`complex_csi` 의 I/Q 순서가 2026-09-24 에 고쳐졌다. 이전 캐시는 쓰면 안 된다.

```bash
rm -f model_train/cnn1d/runs/pilot_cv/cache/s*_phase.npz
.venv/bin/python model_train/cnn1d/cv_pilot.py cache-phase
```

- [ ] **Step 2: W 후보 6개를 seed 1개로 돌린다**

발열 때문에 순차 실행한다. 한 run 당 20~60분을 예상한다.

```bash
for W in 2000 3000 4500 6000 9000 12000; do
  .venv/bin/python model_train/cnn1d/cv_pilot.py cv --name w-$W \
    --model shared --norm window --features phase --window $W --downsample 20 --seeds 0
done
```

- [ ] **Step 3: 표를 뽑고 규칙대로 W 를 고른다**

```bash
.venv/bin/python model_train/cnn1d/summarize_runs.py model_train/cnn1d/runs/pilot_cv/w-*
```

세션당 유효 구간이 부족한 후보(90·120초)는 표에 그대로 두되, 창 수가 다른 후보의 1/3 미만이면
보고서에 "불안정" 으로 표시한다.

- [ ] **Step 4: 상위 2개를 seed 3개로 재확인한다**

```bash
.venv/bin/python model_train/cnn1d/cv_pilot.py cv --name w-<A>-s3 --model shared --norm window \
  --features phase --window <A> --downsample 20 --seeds 0 1 2
```

- [ ] **Step 5: RX 손실 실험 3가지를 돌린다 (spec §6-⑤)**

확정된 W 로, RX 를 한 대씩 뺀다.

```bash
for PAIR in "102 103" "101 103" "101 102"; do
  .venv/bin/python model_train/cnn1d/cv_pilot.py cv --name rx-${PAIR// /-} \
    --model shared --norm window --features phase --window <확정W> --downsample 20 \
    --seeds 0 --rx-subset $PAIR
done
```

- [ ] **Step 6: 결과를 문서에 적는다**

`cnn1d-pilot-cv-report.md` 에 사이클 표를 이어 쓰고, `doc/realtime-inference.md` §5 에
확정된 W 와 근거 수치를, §8 저하 모드 표에 RX 손실 실측 저하 폭을 기입한다.

- [ ] **Step 7: 커밋**

```bash
git add model_train/docs/model-training/cnn1d-pilot-cv-report.md doc/realtime-inference.md
git commit -m "experiment : 2단계 윈도 W 확정과 RX 손실 저하 폭 실측

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: 최종 모델 학습·저장

**Files:**
- Create: `realtime/__init__.py` (빈 파일), `realtime/train.py`
- Create: `realtime/models/.gitkeep`
- Test: `tests/test_realtime_train.py`

**Interfaces:**
- Consumes: Task 1 의 `SharedTemporalCNN`, `cv_pilot` 의 캐시(`runs/pilot_cv/cache/s*.npz`)
- Produces:
  - `ModelSpec` — dataclass: `kind: str`("motion"|"es"), `window: int`, `downsample: int`,
    `feature: str`("amp"|"phase"), `classes: tuple[str, ...]`, `rx: tuple[int, ...]`
  - `save_model(path: Path, model: nn.Module, spec: ModelSpec) -> None`
  - `load_model(path: Path, device: str = "cpu") -> tuple[nn.Module, ModelSpec]`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_realtime_train.py
import sys, tempfile, unittest
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from model_train.cnn1d.shared_cnn import SharedTemporalCNN
from realtime.train import ModelSpec, load_model, save_model


class SaveLoadTest(unittest.TestCase):
    def test_round_trip_preserves_spec_and_weights(self):
        spec = ModelSpec(kind="motion", window=300, downsample=3, feature="amp",
                         classes=("rest", "motion"), rx=(101, 102, 103))
        model = SharedTemporalCNN(groups=(156,), n_classes=2).eval()
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "stage1.pt"
            save_model(p, model, spec)
            loaded, got = load_model(p)
        self.assertEqual(got, spec)
        x = torch.zeros(1, 100, 156)
        with torch.no_grad():
            torch.testing.assert_close(loaded.eval()(x), model(x))

    def test_load_rejects_a_file_without_spec(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "bad.pt"
            torch.save({"state_dict": {}}, p)
            with self.assertRaises(KeyError):
                load_model(p)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_train.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'realtime'`

- [ ] **Step 3: 저장·로딩을 구현한다**

```python
#!/usr/bin/env python3
"""실시간용 1·2단계 모델 학습과 저장.

    python realtime/train.py --stage motion
    python realtime/train.py --stage es --window 6000

파일럿 15세션 전부로 학습한다(교차검증이 아니라 배포용). 평가는 새 날짜 holdout 으로만 한다.
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import torch
from torch import nn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from model_train.cnn1d.shared_cnn import SharedTemporalCNN  # noqa: E402

MODELS = Path(__file__).resolve().parent / "models"


@dataclass(frozen=True)
class ModelSpec:
    kind: str                  # "motion" (1단계) | "es" (2단계)
    window: int                # frame 수 (100Hz)
    downsample: int
    feature: str               # "amp" | "phase"
    classes: tuple[str, ...]
    rx: tuple[int, ...]

    @property
    def n_series(self) -> int:
        return 52 * len(self.rx)


def save_model(path: Path, model: nn.Module, spec: ModelSpec) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"state_dict": model.state_dict(), "spec": asdict(spec)}, path)


def load_model(path: Path, device: str = "cpu") -> tuple[nn.Module, ModelSpec]:
    blob = torch.load(path, map_location=device, weights_only=False)
    raw = blob["spec"]                       # 없으면 KeyError — 규격 없는 가중치는 쓰지 않는다
    spec = ModelSpec(kind=raw["kind"], window=raw["window"], downsample=raw["downsample"],
                     feature=raw["feature"], classes=tuple(raw["classes"]), rx=tuple(raw["rx"]))
    model = SharedTemporalCNN(groups=(spec.n_series,), n_classes=len(spec.classes))
    model.load_state_dict(blob["state_dict"])
    model.to(device).eval()
    return model, spec
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_train.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: 학습 진입점을 붙인다**

`cv_pilot` 의 세션 로딩·윈도·정규화를 재사용한다. 검증 fold 가 없으므로 epoch 은 실험과 같은
값으로 고정한다(1단계 10, 2단계 10).

```python
def train(stage: str, window: int, downsample: int, seeds: tuple[int, ...] = (0, 1, 2),
          epochs: int = 10, lr: float = 1e-3, device: str = "cpu") -> None:
    from model_train.cnn1d import cv_pilot as cv

    sessions = cv.load_sessions()
    feature = "amp" if stage == "motion" else "phase"
    remap = {0: 0, 1: 0, 2: 1} if stage == "motion" else {0: 0, 1: 1}
    classes = ("rest", "motion") if stage == "motion" else ("empty", "static")
    sessions = {s: v for s, v in sessions.items() if v["label"] in remap}
    for v in sessions.values():
        v["label"] = remap[v["label"]]
        source = v["x"][:, cv.VALID_FEATURES] if feature == "amp" else v["phase"]
        v["series"] = np.ascontiguousarray(source)
        v["starts"] = cv.valid_starts(v["series"], window, 30)

    spec = ModelSpec(kind=stage, window=window, downsample=downsample, feature=feature,
                     classes=classes, rx=(101, 102, 103))
    for seed in seeds:
        torch.manual_seed(seed)
        model = SharedTemporalCNN(groups=(spec.n_series,), n_classes=len(classes)).to(device)
        opt = torch.optim.Adam(model.parameters(), lr=lr)
        loss_fn = nn.CrossEntropyLoss()
        index = np.array([(s, st) for s in sessions for st in sessions[s]["starts"]])
        rng = np.random.default_rng(seed)
        for _ in range(epochs):
            model.train()
            for b in range(0, len(index), 32):
                rows = index[rng.permutation(len(index))[b:b + 32]]
                xb = np.concatenate([_featurize(sessions[s], np.array([st]), spec) for s, st in rows])
                yb = np.array([sessions[s]["label"] for s, _ in rows])
                opt.zero_grad()
                loss_fn(model(torch.from_numpy(xb).to(device)),
                        torch.from_numpy(yb).to(device)).backward()
                opt.step()
        save_model(MODELS / f"{stage}_seed{seed}.pt", model.cpu().eval(), spec)
        print(f"저장: {MODELS / f'{stage}_seed{seed}.pt'}")


def _featurize(session: dict, starts: np.ndarray, spec: ModelSpec) -> np.ndarray:
    from model_train.cnn1d import cv_pilot as cv

    w = cv.windows(session["series"], starts, spec.window, spec.downsample, "mean")
    if spec.feature == "amp":
        return cv.window_norm(w)
    return (w - w.mean(axis=1, keepdims=True)).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("motion", "es"), required=True)
    ap.add_argument("--window", type=int, default=None, help="기본: motion 300, es 6000")
    ap.add_argument("--downsample", type=int, default=None, help="기본: motion 3, es 20")
    ap.add_argument("--seeds", type=int, nargs="+", default=[0, 1, 2])
    ap.add_argument("--epochs", type=int, default=10)
    ap.add_argument("--device", default="cpu")
    args = ap.parse_args()
    window = args.window or (300 if args.stage == "motion" else 6000)
    downsample = args.downsample or (3 if args.stage == "motion" else 20)
    train(args.stage, window, downsample, tuple(args.seeds), args.epochs, device=args.device)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: 실제로 학습해 가중치를 만든다**

```bash
.venv/bin/python realtime/train.py --stage motion
.venv/bin/python realtime/train.py --stage es --window <Task 3 에서 확정한 W>
```

Expected: `realtime/models/motion_seed{0,1,2}.pt`, `es_seed{0,1,2}.pt` 생성. 각 파일은 수백 KB다.

- [ ] **Step 7: 커밋**

```bash
git add realtime/__init__.py realtime/train.py realtime/models tests/test_realtime_train.py
git commit -m "feat : 실시간용 1·2단계 모델 학습·저장 (seed 3개)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: `.csi` tail 읽기와 tx_seq 정렬 링버퍼

**Files:**
- Create: `realtime/stream.py`
- Test: `tests/test_realtime_stream.py`

**Interfaces:**
- Consumes: `csi_store` (`CSI_FRAME_DTYPE`, `CSI_FRAME_SIZE`, `complex_csi`, `amplitude`, `crc32_of`)
- Produces:
  - `Buffers` — dataclass: `amp: np.ndarray[T,156]`, `phase: np.ndarray[T,156]`,
    `present: np.ndarray[3,T] bool`, `tx_seq0: int`
  - `class CsiStream(session_dir: Path, capacity: int = 24000, rx_order=(101,102,103))`
    - `poll() -> int` — 새 프레임을 읽어 넣고 개수를 돌려준다
    - `latest(width: int) -> Buffers | None` — 최근 width frame. 채워지지 않았으면 None
    - `rx_alive(within_s: float = 2.0) -> tuple[int, ...]` — 최근에 프레임이 온 RX 들
  - `phase_residual(z: np.ndarray[..., 52]) -> np.ndarray[..., 52]` — unwrap 후 직선 성분 제거

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_realtime_stream.py
import sys, tempfile, unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import csi_store as cs
from realtime.stream import CsiStream, phase_residual


def frame_bytes(tx_seq: int, seq: int, value: int = 5) -> bytes:
    hdr = np.zeros(1, dtype=cs.HEADER_DTYPE)
    hdr["magic"], hdr["version"] = cs.FRAME_MAGIC, cs.FRAME_VERSION
    hdr["frame_type"], hdr["raw_len"] = cs.FRAME_TYPE_CSI, cs.HT20_LLTF_RAW_LEN
    hdr["total_len"] = cs.HEADER_SIZE + cs.HT20_LLTF_RAW_LEN
    hdr["channel"], hdr["rssi"], hdr["gain_comp"] = 11, -40, 1.0
    hdr["seq"], hdr["tx_seq"], hdr["timestamp_us"] = seq, tx_seq, seq * 10_000
    raw = np.full(cs.HT20_LLTF_RAW_LEN, value, dtype=np.int8)
    frame = bytearray(hdr.tobytes() + raw.tobytes())
    crc = cs.crc32_of(bytes(frame))
    frame[cs._CRC_OFFSET:cs._CRC_OFFSET + 4] = int(crc).to_bytes(4, "little")
    return bytes(frame)


def write_session(tmp: Path, n: int = 400, skip_rx: int | None = None) -> Path:
    d = tmp / "120000_empty_s99"
    d.mkdir(parents=True)
    (d / "session.json").write_text('{"label": "empty", "session_id": 99}')
    for rx in (101, 102, 103):
        count = 0 if rx == skip_rx else n
        (d / f"device_{rx}.csi").write_bytes(
            b"".join(frame_bytes(1000 + i, i, value=rx - 100) for i in range(count)))
    return d


class StreamTest(unittest.TestCase):
    def test_poll_reads_all_three_rx_and_aligns_on_tx_seq(self):
        with tempfile.TemporaryDirectory() as t:
            d = write_session(Path(t), n=400)
            st = CsiStream(d)
            self.assertEqual(st.poll(), 1200)          # 3 RX × 400
            buf = st.latest(300)
            self.assertEqual(buf.amp.shape, (300, 156))
            self.assertTrue(np.isfinite(buf.amp).all())

    def test_latest_returns_none_before_the_window_is_full(self):
        with tempfile.TemporaryDirectory() as t:
            st = CsiStream(write_session(Path(t), n=100))
            st.poll()
            self.assertIsNone(st.latest(300))

    def test_poll_picks_up_frames_appended_after_the_first_read(self):
        with tempfile.TemporaryDirectory() as t:
            d = write_session(Path(t), n=100)
            st = CsiStream(d)
            st.poll()
            with (d / "device_101.csi").open("ab") as fp:
                fp.write(b"".join(frame_bytes(1100 + i, 100 + i) for i in range(50)))
            self.assertEqual(st.poll(), 50)

    def test_partial_trailing_frame_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as t:
            d = write_session(Path(t), n=10)
            with (d / "device_101.csi").open("ab") as fp:
                fp.write(frame_bytes(2000, 10)[:100])   # 잘린 프레임
            st = CsiStream(d)
            st.poll()
            with (d / "device_101.csi").open("ab") as fp:
                fp.write(frame_bytes(2000, 10)[100:])   # 나머지가 나중에 도착
            self.assertEqual(st.poll(), 1)

    def test_missing_rx_leaves_nan_and_is_reported_as_not_alive(self):
        with tempfile.TemporaryDirectory() as t:
            st = CsiStream(write_session(Path(t), n=400, skip_rx=102))
            st.poll()
            buf = st.latest(300)
            self.assertTrue(np.isnan(buf.amp[:, 52:104]).all())
            self.assertEqual(st.rx_alive(), (101, 103))

    def test_phase_residual_removes_constant_and_linear_terms(self):
        freq = np.r_[-26:0, 1:27].astype(float)
        z = np.exp(1j * (0.7 + 0.03 * freq))            # CFO 상수 + 타이밍 기울기만 있는 신호
        np.testing.assert_allclose(phase_residual(z), np.zeros(52), atol=1e-9)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_stream.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'realtime.stream'`

- [ ] **Step 3: 구현한다**

```python
#!/usr/bin/env python3
"""저장 중인 `.csi` 를 따라 읽어 tx_seq 격자에 정렬한 링버퍼로 유지한다.

수집 프로세스는 건드리지 않는다. 파일 끝에 붙는 프레임만 읽으며, 잘린 프레임은 다음 poll 로
미룬다. 정렬 규칙은 공식 전처리와 같다 — tx_seq 가 격자, 없는 칸은 NaN.
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import csi_store as cs  # noqa: E402

#: ESP32 LLTF 버퍼 인덱스 → 주파수 순서 (38~63 = -26~-1, 1~26 = +1~+26)
ORDER = np.r_[38:64, 1:27]
FREQ = np.r_[-26:0, 1:27].astype(np.float64)
_DESIGN = np.vstack([FREQ, np.ones_like(FREQ)]).T
_PROJ = _DESIGN @ np.linalg.pinv(_DESIGN)
TONES = 52
RX_ORDER = (101, 102, 103)


def phase_residual(z: np.ndarray) -> np.ndarray:
    """복소 CSI → 주파수축 직선 성분(CFO·타이밍 오프셋)을 뺀 위상 잔차."""
    phi = np.unwrap(np.angle(z), axis=-1)
    return phi - phi @ _PROJ.T


@dataclass
class Buffers:
    amp: np.ndarray        # (T, 156)
    phase: np.ndarray      # (T, 156)
    present: np.ndarray    # (3, T) bool
    tx_seq0: int


class CsiStream:
    def __init__(self, session_dir: Path, capacity: int = 24000,
                 rx_order: tuple[int, ...] = RX_ORDER) -> None:
        self.dir = Path(session_dir)
        self.rx_order = rx_order
        self.capacity = capacity
        self._offset = {rx: 0 for rx in rx_order}      # 파일에서 읽은 바이트 수
        self._last_seen = {rx: 0.0 for rx in rx_order}
        self._amp = np.full((capacity, TONES * len(rx_order)), np.nan, np.float32)
        self._phase = np.full((capacity, TONES * len(rx_order)), np.nan, np.float32)
        self._present = np.zeros((len(rx_order), capacity), bool)
        self._base: int | None = None                  # 버퍼 0번 칸의 tx_seq
        self._end = 0                                  # 버퍼에 채워진 칸 수

    def poll(self) -> int:
        added = 0
        for ri, rx in enumerate(self.rx_order):
            path = self.dir / f"device_{rx}.csi"
            if not path.is_file():
                continue
            size = path.stat().st_size
            whole = (size - self._offset[rx]) // cs.CSI_FRAME_SIZE
            if whole <= 0:
                continue
            with path.open("rb") as fp:
                fp.seek(self._offset[rx])
                blob = fp.read(whole * cs.CSI_FRAME_SIZE)
            self._offset[rx] += len(blob)
            frames = np.frombuffer(blob, dtype=cs.CSI_FRAME_DTYPE)
            self._ingest(ri, frames)
            self._last_seen[rx] = time.monotonic()
            added += len(frames)
        return added

    def _ingest(self, ri: int, frames: np.ndarray) -> None:
        tx = frames["hdr"]["tx_seq"].astype(np.int64)
        if self._base is None:
            self._base = int(tx[0])
        idx = tx - self._base
        if idx.max() >= self.capacity:                 # 링버퍼를 넘기면 앞을 버리고 당긴다
            shift = int(idx.max()) - self.capacity + 1
            self._roll(shift)
            idx -= shift
        keep = idx >= 0
        idx, frames = idx[keep], frames[keep]
        if len(idx) == 0:
            return
        idx, first = np.unique(idx, return_index=True)  # 중복 tx_seq 는 첫 프레임
        frames = frames[first]
        cols = slice(ri * TONES, (ri + 1) * TONES)
        z = cs.complex_csi(frames, valid_only=False)[:, ORDER]
        self._amp[idx, cols] = np.abs(z).astype(np.float32)
        self._phase[idx, cols] = phase_residual(z).astype(np.float32)
        self._present[ri, idx] = True
        self._end = max(self._end, int(idx.max()) + 1)

    def _roll(self, shift: int) -> None:
        self._amp[:-shift] = self._amp[shift:]
        self._phase[:-shift] = self._phase[shift:]
        self._present[:, :-shift] = self._present[:, shift:]
        self._amp[-shift:] = np.nan
        self._phase[-shift:] = np.nan
        self._present[:, -shift:] = False
        self._base += shift
        self._end = max(0, self._end - shift)

    def latest(self, width: int) -> Buffers | None:
        if self._base is None or self._end < width:
            return None
        s = self._end - width
        return Buffers(amp=self._amp[s:self._end], phase=self._phase[s:self._end],
                       present=self._present[:, s:self._end], tx_seq0=self._base + s)

    def rx_alive(self, within_s: float = 2.0) -> tuple[int, ...]:
        now = time.monotonic()
        return tuple(rx for rx in self.rx_order
                     if self._last_seen[rx] and now - self._last_seen[rx] <= within_s)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_stream.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: 실제 세션으로 확인한다**

```bash
.venv/bin/python -c "
from pathlib import Path; import sys; sys.path.insert(0, '.')
from realtime.stream import CsiStream
d = sorted(Path('mac_collector_output/raw/20260917').glob('*_s45'))[0]
st = CsiStream(d); print('frames', st.poll()); b = st.latest(6000)
print(b.amp.shape, 'NaN 비율', float(__import__('numpy').isnan(b.amp).mean()))"
```
Expected: `frames` 가 8만 이상, NaN 비율이 0.05 미만

- [ ] **Step 6: 커밋**

```bash
git add realtime/stream.py tests/test_realtime_stream.py
git commit -m "feat : .csi tail 읽기와 tx_seq 정렬 링버퍼 (위상 잔차 포함)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: 모델 입력 변환

**Files:**
- Create: `realtime/features.py`
- Test: `tests/test_realtime_features.py`

**Interfaces:**
- Consumes: Task 4 의 `ModelSpec`, Task 5 의 `Buffers`
- Produces:
  - `make_input(buf: Buffers, spec: ModelSpec, rx: tuple[int, ...]) -> np.ndarray | None`
    — `(1, T', 52*len(rx))` float32. NaN 이 낀 칸이 있으면 None

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_realtime_features.py
import sys, unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from realtime.features import make_input
from realtime.stream import Buffers
from realtime.train import ModelSpec

AMP_SPEC = ModelSpec("motion", 300, 3, "amp", ("rest", "motion"), (101, 102, 103))
PHASE_SPEC = ModelSpec("es", 600, 20, "phase", ("empty", "static"), (101, 102, 103))


def buffers(t=600, nan_at=None):
    rng = np.random.default_rng(0)
    amp = rng.uniform(5, 50, (t, 156)).astype(np.float32)
    phase = rng.normal(0, .05, (t, 156)).astype(np.float32)
    if nan_at is not None:
        amp[nan_at] = np.nan
        phase[nan_at] = np.nan
    return Buffers(amp=amp, phase=phase, present=np.ones((3, t), bool), tx_seq0=0)


class FeaturesTest(unittest.TestCase):
    def test_amp_input_is_downsampled_and_window_normalised(self):
        x = make_input(buffers(), AMP_SPEC, (101, 102, 103))
        self.assertEqual(x.shape, (1, 100, 156))          # 300 frame / 3
        self.assertLess(abs(float(x.mean())), 0.05)       # 윈도 평균 제거 → 0 근처

    def test_phase_input_has_zero_mean_per_series(self):
        x = make_input(buffers(), PHASE_SPEC, (101, 102, 103))
        self.assertEqual(x.shape, (1, 30, 156))           # 600 frame / 20
        np.testing.assert_allclose(x.mean(axis=1), 0, atol=1e-5)

    def test_missing_frames_make_it_refuse(self):
        self.assertIsNone(make_input(buffers(nan_at=-5), AMP_SPEC, (101, 102, 103)))

    def test_rx_subset_selects_only_those_columns(self):
        x = make_input(buffers(), AMP_SPEC, (101, 103))
        self.assertEqual(x.shape, (1, 100, 104))

    def test_short_buffer_returns_none(self):
        self.assertIsNone(make_input(buffers(t=100), PHASE_SPEC, (101, 102, 103)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_features.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'realtime.features'`

- [ ] **Step 3: 구현한다**

```python
#!/usr/bin/env python3
"""링버퍼 → 모델 입력. 학습 때와 **같은** 변환이어야 한다 (cv_pilot 의 windows/window_norm).""" 
from __future__ import annotations

import numpy as np

from model_train.cnn1d.shared_cnn import rx_columns
from realtime.stream import Buffers
from realtime.train import ModelSpec


def make_input(buf: Buffers, spec: ModelSpec, rx: tuple[int, ...]) -> np.ndarray | None:
    source = buf.amp if spec.feature == "amp" else buf.phase
    if len(source) < spec.window:
        return None
    w = source[-spec.window:][:, rx_columns(rx)]
    if not np.isfinite(w).all():                 # 보간되지 않은 누락이 낀 윈도는 쓰지 않는다
        return None
    t = spec.window - spec.window % spec.downsample
    w = w[:t].reshape(t // spec.downsample, spec.downsample, w.shape[1]).mean(axis=1)
    w = w[None, ...]
    if spec.feature == "amp":
        m = w.mean(axis=1, keepdims=True)        # 학습과 동일: (x-평균)/(평균+1)
        return ((w - m) / (m + 1.0)).astype(np.float32)
    return (w - w.mean(axis=1, keepdims=True)).astype(np.float32)
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_features.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: 학습 경로와 같은 값이 나오는지 교차 확인한다**

```python
# tests/test_realtime_features.py 에 추가
    def test_matches_the_training_path_on_the_same_window(self):
        """cv_pilot 의 windows()+window_norm() 과 값이 같아야 한다."""
        from model_train.cnn1d import cv_pilot as cv

        b = buffers()
        mine = make_input(b, AMP_SPEC, (101, 102, 103))
        series = np.ascontiguousarray(b.amp[-300:])
        theirs = cv.window_norm(cv.windows(series, np.array([0]), 300, 3, "mean"))
        np.testing.assert_allclose(mine, theirs, rtol=1e-5)
```

Run: `.venv/bin/python -m pytest tests/test_realtime_features.py -v`
Expected: PASS (6 tests). 값이 다르면 **features.py 를 학습 경로에 맞춰 고친다** — 학습이 기준이다.

- [ ] **Step 6: 커밋**

```bash
git add realtime/features.py tests/test_realtime_features.py
git commit -m "feat : 실시간 모델 입력 변환 (학습 경로와 동일함을 테스트로 고정)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: 상태 기계

spec §4 규칙 ①~⑤ 를 그대로 옮긴다. 판정의 모든 시간 상수는 생성자 인자로 두고, 기본값은
spec 의 초기값으로 한다.

**Files:**
- Create: `realtime/state.py`
- Test: `tests/test_realtime_state.py`

**Interfaces:**
- Consumes: 없음 (순수 로직)
- Produces:
  - `Reading(t: float, motion_p: float, es_p: tuple[float, float] | None, n_rx: int)`
  - `Event(t: float, frm: str, to: str, confidence: float)`
  - `class StateMachine(w_seconds: float, conf: float = 0.7, degraded_conf: float = 0.85,
    motion_enter: int = 2, motion_window: int = 3, quiet_s: float = 3.0)`
    - `update(r: Reading) -> Event | None`
    - 속성: `state: str`, `since: float`, `confidence: float`, `note: str`, `remaining_s: float`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_realtime_state.py
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime.state import Reading, StateMachine


def feed(sm, t0, n, step=0.3, **kw):
    """n 번 판단을 먹인다. 마지막 이벤트를 돌려준다."""
    last = None
    for i in range(n):
        ev = sm.update(Reading(t=t0 + i * step, **kw))
        last = ev or last
    return last


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.sm = StateMachine(w_seconds=60.0)

    def test_starts_in_deciding(self):
        self.assertEqual(self.sm.state, "deciding")

    def test_two_of_three_motion_readings_enter_motion(self):
        self.sm.update(Reading(0.0, motion_p=.9, es_p=None, n_rx=3))
        ev = self.sm.update(Reading(0.3, motion_p=.9, es_p=None, n_rx=3))
        self.assertEqual(self.sm.state, "motion")
        self.assertEqual((ev.frm, ev.to), ("deciding", "motion"))

    def test_a_single_motion_blip_does_not_enter_motion(self):
        self.sm.update(Reading(0.0, motion_p=.9, es_p=None, n_rx=3))
        self.sm.update(Reading(0.3, motion_p=.1, es_p=None, n_rx=3))
        self.sm.update(Reading(0.6, motion_p=.1, es_p=None, n_rx=3))
        self.assertEqual(self.sm.state, "deciding")

    def test_motion_ends_after_the_quiet_period(self):
        feed(self.sm, 0.0, 4, motion_p=.9, es_p=None, n_rx=3)
        feed(self.sm, 1.2, 9, motion_p=.05, es_p=None, n_rx=3)   # 2.7초 — 아직 3초 미만
        self.assertEqual(self.sm.state, "motion")
        ev = self.sm.update(Reading(4.3, motion_p=.05, es_p=None, n_rx=3))
        self.assertEqual((ev.frm, ev.to), ("motion", "deciding"))

    def test_es_decision_waits_for_a_full_motion_free_window(self):
        feed(self.sm, 0.0, 4, motion_p=.9, es_p=None, n_rx=3)
        feed(self.sm, 1.2, 20, motion_p=.05, es_p=(.05, .95), n_rx=3)
        self.assertEqual(self.sm.state, "deciding")               # 60초가 아직 안 참
        ev = self.sm.update(Reading(70.0, motion_p=.05, es_p=(.05, .95), n_rx=3))
        self.assertEqual((ev.frm, ev.to), ("deciding", "static"))

    def test_low_confidence_keeps_deciding(self):
        feed(self.sm, 0.0, 4, motion_p=.9, es_p=None, n_rx=3)
        self.sm.update(Reading(70.0, motion_p=.05, es_p=(.45, .55), n_rx=3))
        self.assertEqual(self.sm.state, "deciding")

    def test_degraded_mode_raises_the_threshold(self):
        sm = StateMachine(w_seconds=60.0, conf=.7, degraded_conf=.85)
        feed(sm, 0.0, 4, motion_p=.9, es_p=None, n_rx=2)
        sm.update(Reading(70.0, motion_p=.05, es_p=(.2, .8), n_rx=2))   # 0.8 < 0.85
        self.assertEqual(sm.state, "deciding")
        sm.update(Reading(70.3, motion_p=.05, es_p=(.1, .9), n_rx=2))
        self.assertEqual(sm.state, "static")
        self.assertIn("RX 2대", sm.note)

    def test_empty_static_does_not_flip_without_motion_before_two_windows(self):
        feed(self.sm, 0.0, 4, motion_p=.9, es_p=None, n_rx=3)
        self.sm.update(Reading(70.0, motion_p=.05, es_p=(.05, .95), n_rx=3))
        self.assertEqual(self.sm.state, "static")
        feed(self.sm, 71.0, 100, motion_p=.05, es_p=(.95, .05), n_rx=3)   # 30초간 반대 판단
        self.assertEqual(self.sm.state, "static")
        ev = self.sm.update(Reading(200.0, motion_p=.05, es_p=(.95, .05), n_rx=3))
        self.assertEqual((ev.frm, ev.to), ("static", "empty"))            # 2W 초과 후 교정

    def test_motion_interrupts_any_state_immediately(self):
        feed(self.sm, 0.0, 4, motion_p=.9, es_p=None, n_rx=3)
        self.sm.update(Reading(70.0, motion_p=.05, es_p=(.95, .05), n_rx=3))
        self.assertEqual(self.sm.state, "empty")
        feed(self.sm, 80.0, 2, motion_p=.95, es_p=None, n_rx=3)
        self.assertEqual(self.sm.state, "motion")

    def test_one_rx_suspends_judgement_and_keeps_the_last_state(self):
        feed(self.sm, 0.0, 4, motion_p=.9, es_p=None, n_rx=3)
        self.sm.update(Reading(70.0, motion_p=.05, es_p=(.05, .95), n_rx=3))
        ev = self.sm.update(Reading(71.0, motion_p=.0, es_p=None, n_rx=1))
        self.assertEqual(self.sm.state, "suspended")
        self.assertEqual(ev.frm, "static")
        self.assertIn("static", self.sm.note)          # 마지막 판단을 알려준다

    def test_remaining_seconds_counts_down_while_deciding(self):
        feed(self.sm, 0.0, 4, motion_p=.9, es_p=None, n_rx=3)
        self.sm.update(Reading(10.0, motion_p=.05, es_p=None, n_rx=3))
        self.assertAlmostEqual(self.sm.remaining_s, 60.0 - (10.0 - 1.2), places=1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'realtime.state'`

- [ ] **Step 3: 구현한다**

```python
#!/usr/bin/env python3
"""상태 기계 — doc/realtime-inference.md §4 규칙 ①~⑤.

핵심 제약: empty ↔ static 전환은 물리적으로 반드시 움직임을 거친다. 그래서 움직임 없이 도는
동안에는 판정을 뒤집지 않는다(규칙 ⑤ 의 교정 경로만 예외). 장시간 오탐을 막는 장치다.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    t: float                                   # 초 단위 단조 시각
    motion_p: float                            # 1단계: 움직임 확률
    es_p: tuple[float, float] | None           # 2단계: (empty, static) 확률. 없으면 None
    n_rx: int                                  # 살아 있는 RX 수


@dataclass(frozen=True)
class Event:
    t: float
    frm: str
    to: str
    confidence: float


class StateMachine:
    def __init__(self, w_seconds: float, conf: float = 0.7, degraded_conf: float = 0.85,
                 motion_enter: int = 2, motion_window: int = 3, quiet_s: float = 3.0) -> None:
        self.w = w_seconds
        self.conf, self.degraded_conf = conf, degraded_conf
        self.motion_enter, self.quiet_s = motion_enter, quiet_s
        self._recent: deque[bool] = deque(maxlen=motion_window)
        self.state, self.since, self.confidence, self.note = "deciding", 0.0, 0.0, ""
        self._last_motion_t: float | None = None     # 마지막으로 움직임을 본 시각
        self._quiet_from: float | None = None        # 조용해지기 시작한 시각
        self._disagree_from: float | None = None     # 반대 판단이 이어진 시작 시각
        self._last_decided = ""                      # 정지 상태에서 마지막으로 확정한 값
        self._t = 0.0

    # ── 내부 ────────────────────────────────────────────────────────────────
    def _to(self, t: float, new: str, confidence: float) -> Event:
        ev = Event(t=t, frm=self.state, to=new, confidence=confidence)
        self.state, self.since, self.confidence = new, t, confidence
        if new in ("empty", "static"):
            self._last_decided = new
        self._disagree_from = None
        return ev

    @property
    def threshold(self) -> float:
        return self.degraded_conf if self._degraded else self.conf

    @property
    def remaining_s(self) -> float:
        if self.state != "deciding" or self._quiet_from is None:
            return 0.0
        return max(0.0, self.w - (self._t - self._quiet_from))

    # ── 본체 ────────────────────────────────────────────────────────────────
    def update(self, r: Reading) -> Event | None:
        self._t = r.t
        self._degraded = r.n_rx == 2
        self.note = ""
        if r.n_rx <= 1:
            self.note = (f"RX {r.n_rx}대 — 판단 정지. 마지막 판단 "
                         f"{self._last_decided or self.state}")
            return self._to(r.t, "suspended", 0.0) if self.state != "suspended" else None
        if self._degraded:
            self.note = f"RX 2대 — 확신 {self.degraded_conf:.0%} 이상일 때만 판단"

        moving = r.motion_p >= 0.5
        self._recent.append(moving)
        if moving:
            self._last_motion_t, self._quiet_from = r.t, None

        # ① 움직임 진입
        if sum(self._recent) >= self.motion_enter and self.state != "motion":
            return self._to(r.t, "motion", float(r.motion_p))

        # ② 움직임 종료 → 판단 중
        if self.state == "motion":
            if not moving and self._last_motion_t is not None:
                if r.t - self._last_motion_t >= self.quiet_s:
                    self._quiet_from = self._last_motion_t
                    return self._to(r.t, "deciding", 0.0)
            return None

        if self._quiet_from is None:
            self._quiet_from = r.t if self._last_motion_t is None else self._last_motion_t

        if r.es_p is None:
            return None
        p_empty, p_static = r.es_p
        guess, p = ("empty", p_empty) if p_empty >= p_static else ("static", p_static)

        # ③④ 빈 방·정지 결정 — 창이 움직임 없이 다 찼고 확신이 기준 이상일 때만
        if self.state in ("deciding", "suspended"):
            if r.t - self._quiet_from >= self.w and p >= self.threshold:
                return self._to(r.t, guess, float(p))
            return None

        # ⑤ 움직임 없는 직접 전환 — 2W 이상 반대 판단이 이어질 때만 교정
        if guess != self.state:
            self._disagree_from = self._disagree_from or r.t
            if r.t - self._disagree_from >= 2 * self.w and p >= self.threshold:
                return self._to(r.t, guess, float(p))
        else:
            self._disagree_from = None
            self.confidence = float(p)
        return None
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_state.py -v`
Expected: PASS (11 tests). 실패하면 **테스트가 아니라 구현을 고친다** — 테스트가 spec §4 다.

- [ ] **Step 5: 커밋**

```bash
git add realtime/state.py tests/test_realtime_state.py
git commit -m "feat : 실시간 상태 기계 (spec §4 규칙 ①~⑤, 저하 모드 포함)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: 엔진 — 재생과 실시간을 같은 코드로

**Files:**
- Create: `realtime/engine.py`
- Test: `tests/test_realtime_engine.py`

**Interfaces:**
- Consumes: Task 4~7 전부
- Produces:
  - `class Engine(session_dir, models_dir, w_seconds, stride=30, seeds=(0,1,2), device="cpu")`
    - `step() -> Event | None` — 새 프레임을 읽고 한 번 판단한다
    - `view() -> dict` — 화면용 현재 상태
      (`state, since, held_s, confidence, note, remaining_s, n_rx, hz, events[]`)
    - `run(follow: bool) -> Iterator[Event]`
  - `mark(note: str = "") -> dict` — 운영자 전환 표시를 tx_seq 와 함께 기록
  - `write_status(path: Path, view: dict) -> None` — 임시 파일 후 rename (부분 읽기 방지)
  - 파일: `<session>/realtime_events.jsonl`, `realtime_status.json`, `realtime_marks.jsonl`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_realtime_engine.py
import json, sys, tempfile, unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from realtime.engine import Engine, write_status


class StubModel:
    """항상 같은 확률을 내는 가짜 모델. 엔진 배선만 시험한다."""

    def __init__(self, probs):
        self.probs = probs

    def __call__(self, x):
        import torch
        return torch.log(torch.tensor([self.probs], dtype=torch.float32))

    def eval(self):
        return self


class EngineTest(unittest.TestCase):
    def test_write_status_is_atomic_and_readable(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "realtime_status.json"
            write_status(p, {"state": "motion", "confidence": 0.9})
            self.assertEqual(json.loads(p.read_text())["state"], "motion")
            self.assertFalse(list(Path(t).glob("*.tmp")))

    def test_replay_over_a_recorded_session_emits_events_and_a_log(self):
        from tests.test_realtime_stream import write_session  # 프레임 생성 헬퍼 재사용

        with tempfile.TemporaryDirectory() as t:
            d = write_session(Path(t), n=9000)
            eng = Engine(d, models_dir=None, w_seconds=30.0, stride=300)
            eng.stage1 = (StubModel([0.02, 0.98]),)        # 항상 movement
            eng.stage2 = (StubModel([0.9, 0.1]),)
            events = list(eng.run(follow=False))
            self.assertTrue(any(e.to == "motion" for e in events))
            log = (d / "realtime_events.jsonl").read_text().strip().splitlines()
            self.assertEqual(len(log), len(events))
            self.assertEqual(json.loads(log[0])["to"], events[0].to)

    def test_mark_records_the_current_tx_seq_for_latency_measurement(self):
        from tests.test_realtime_stream import write_session

        with tempfile.TemporaryDirectory() as t:
            d = write_session(Path(t), n=500)
            eng = Engine(d, models_dir=None, w_seconds=30.0)
            eng.step()
            row = eng.mark("앉음")
            self.assertIsNotNone(row["tx_seq"])
            logged = json.loads((d / "realtime_marks.jsonl").read_text().strip())
            self.assertEqual(logged["note"], "앉음")

    def test_view_reports_the_current_state_for_the_ui(self):
        from tests.test_realtime_stream import write_session

        with tempfile.TemporaryDirectory() as t:
            d = write_session(Path(t), n=9000)
            eng = Engine(d, models_dir=None, w_seconds=30.0, stride=300)
            eng.stage1 = (StubModel([0.98, 0.02]),)
            eng.stage2 = (StubModel([0.95, 0.05]),)
            list(eng.run(follow=False))
            v = eng.view()
            self.assertIn(v["state"], ("empty", "deciding"))
            self.assertEqual(v["n_rx"], 3)
            self.assertIn("events", v)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'realtime.engine'`

- [ ] **Step 3: 구현한다**

```python
#!/usr/bin/env python3
"""실시간 판단 엔진. 녹화 파일 재생과 실시간 tail 이 같은 코드를 쓴다.

    python realtime/engine.py --session <세션 디렉터리>            # 재생
    python realtime/engine.py --session <세션 디렉터리> --follow   # 실시간
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Iterator

import numpy as np
import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime.features import make_input          # noqa: E402
from realtime.state import Event, Reading, StateMachine  # noqa: E402
from realtime.stream import CsiStream             # noqa: E402
from realtime.train import MODELS, load_model     # noqa: E402

STATUS_NAME = "realtime_status.json"
EVENTS_NAME = "realtime_events.jsonl"
MARKS_NAME = "realtime_marks.jsonl"


def write_status(path: Path, view: dict) -> None:
    """부분적으로 쓰인 파일을 GUI 가 읽지 않도록 임시 파일에 쓰고 rename 한다."""
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(view, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Engine:
    def __init__(self, session_dir: Path, models_dir: Path | None = MODELS,
                 w_seconds: float = 60.0, stride: int = 30, seeds: tuple[int, ...] = (0, 1, 2),
                 device: str = "cpu") -> None:
        self.dir = Path(session_dir)
        self.stream = CsiStream(self.dir)
        self.sm = StateMachine(w_seconds=w_seconds)
        self.stride = stride
        self.device = device
        self.events: list[Event] = []
        self.stage1, self.stage2 = (), ()
        self.spec1 = self.spec2 = None
        if models_dir is not None:
            loaded1 = [load_model(models_dir / f"motion_seed{s}.pt", device) for s in seeds]
            loaded2 = [load_model(models_dir / f"es_seed{s}.pt", device) for s in seeds]
            self.stage1, self.spec1 = tuple(m for m, _ in loaded1), loaded1[0][1]
            self.stage2, self.spec2 = tuple(m for m, _ in loaded2), loaded2[0][1]
        self._t0 = time.monotonic()
        self._frames = 0

    def _predict(self, models, spec, rx) -> np.ndarray | None:
        buf = self.stream.latest(spec.window if spec else 300)
        if buf is None:
            return None
        x = make_input(buf, spec, rx)
        if x is None:
            return None
        with torch.no_grad():
            probs = [torch.softmax(m(torch.from_numpy(x).to(self.device)), 1).cpu().numpy()[0]
                     for m in models]
        return np.mean(probs, axis=0)          # seed 앙상블

    def step(self, now: float | None = None) -> Event | None:
        self._frames += self.stream.poll()
        rx = self.stream.rx_alive() or (101, 102, 103)
        t = now if now is not None else time.monotonic() - self._t0
        p1 = self._predict(self.stage1, self.spec1, rx) if self.stage1 else None
        p2 = self._predict(self.stage2, self.spec2, rx) if self.stage2 else None
        reading = Reading(t=t, motion_p=float(p1[1]) if p1 is not None else 0.0,
                          es_p=(float(p2[0]), float(p2[1])) if p2 is not None else None,
                          n_rx=len(rx))
        ev = self.sm.update(reading)
        if ev:
            self.events.append(ev)
            with (self.dir / EVENTS_NAME).open("a", encoding="utf-8") as fp:
                fp.write(json.dumps({"t": ev.t, "from": ev.frm, "to": ev.to,
                                     "confidence": round(ev.confidence, 4)},
                                    ensure_ascii=False) + "\n")
        write_status(self.dir / STATUS_NAME, self.view())
        return ev

    def view(self) -> dict:
        t = self.sm._t
        span = max(1e-6, time.monotonic() - self._t0)
        n_rx = len(self.stream.rx_alive() or (101, 102, 103))
        return {"state": self.sm.state, "since": self.sm.since, "held_s": t - self.sm.since,
                "confidence": self.sm.confidence, "note": self.sm.note, "t": t,
                "remaining_s": self.sm.remaining_s, "n_rx": n_rx,
                "hz": self._frames / span / 3.0, "threshold": self.sm.threshold,
                "events": [{"t": e.t, "from": e.frm, "to": e.to, "confidence": e.confidence}
                           for e in self.events[-20:]]}

    def mark(self, note: str = "") -> dict:
        """운영자가 상태를 바꾼 시각을 정답으로 남긴다 (spec §6-② 반응 지연 측정용).

        tx_seq 로 남겨야 나중에 프레임 위치로 환산된다 — 호스트 시각만으로는 맞출 수 없다.
        """
        buf = self.stream.latest(1)
        row = {"t": self.sm._t, "wall": time.time(), "note": note,
               "tx_seq": int(buf.tx_seq0) if buf else None, "state": self.sm.state}
        with (self.dir / MARKS_NAME).open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    def run(self, follow: bool) -> Iterator[Event]:
        """follow=False 면 파일 끝까지 재생하고 끝난다 (오프라인 평가)."""
        if follow:
            while True:
                ev = self.step()
                if ev:
                    yield ev
                time.sleep(self.stride / 100.0)
            return
        self.stream.poll()
        t = 0.0
        while True:
            buf = self.stream.latest(self.spec2.window if self.spec2 else 3000)
            if buf is None:
                break
            ev = self.step(now=t)
            if ev:
                yield ev
            t += self.stride / 100.0
            if not self._advance():
                break

    def _advance(self) -> bool:
        """재생에서 다음 stride 만큼 창을 민다. 더 밀 수 없으면 False."""
        return self.stream.advance(self.stride) if hasattr(self.stream, "advance") else False
```

- [ ] **Step 4: 재생을 위해 `CsiStream` 에 커서를 추가한다**

재생은 "파일 전체가 이미 있는 상태"라 `latest()` 만으로는 시간이 흐르지 않는다. 커서를 둔다.

```python
# realtime/stream.py — CsiStream 에 추가
    def __init__(...):
        ...
        self._cursor: int | None = None       # 재생용 창 끝 위치. None 이면 항상 최신

    def advance(self, stride: int) -> bool:
        """재생 커서를 stride 만큼 민다. 끝까지 갔으면 False."""
        if self._cursor is None:
            self._cursor = min(self._end, stride)
        if self._cursor + stride > self._end:
            return False
        self._cursor += stride
        return True

    def latest(self, width: int) -> Buffers | None:
        end = self._end if self._cursor is None else self._cursor
        if self._base is None or end < width:
            return None
        s = end - width
        return Buffers(amp=self._amp[s:end], phase=self._phase[s:end],
                       present=self._present[:, s:end], tx_seq0=self._base + s)
```

`tests/test_realtime_stream.py` 에 커서 테스트를 추가한다.

```python
    def test_advance_moves_the_replay_cursor_until_the_end(self):
        with tempfile.TemporaryDirectory() as t:
            st = CsiStream(write_session(Path(t), n=400))
            st.poll()
            first = st.latest(300).tx_seq0
            self.assertTrue(st.advance(50))
            self.assertEqual(st.latest(300).tx_seq0, first + 50)
            for _ in range(20):
                if not st.advance(50):
                    break
            self.assertFalse(st.advance(50))
```

- [ ] **Step 5: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_engine.py tests/test_realtime_stream.py -v`
Expected: PASS (엔진 4 + 스트림 7)

- [ ] **Step 6: 실제 녹화 세션으로 재생해 본다**

```bash
.venv/bin/python realtime/engine.py --session mac_collector_output/raw/20260917/213701_static_s42
```
Expected: 이벤트가 출력되고 세션 폴더에 `realtime_events.jsonl` 이 생긴다. `static` 세션이므로
`deciding → static` 이 나오는 것이 정상이고, `motion` 이 반복되면 1단계 임계나 입력 변환을 의심한다.

- [ ] **Step 7: 커밋**

```bash
git add realtime/engine.py realtime/stream.py tests/test_realtime_engine.py tests/test_realtime_stream.py
git commit -m "feat : 실시간 판단 엔진 — 재생·실시간 공용, 이벤트/상태 파일 기록

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: CLI 화면

spec §8 의 공간 배분(24행 기준: 제목 1 · 상태 블록 9 · 이력 4 · 상태줄 1)을 그대로 구현한다.
렌더링은 **순수 함수**로 두고 터미널 제어는 얇게 감싼다 — 테스트가 문자열만 보면 되게.

**Files:**
- Create: `realtime/ui.py`
- Test: `tests/test_realtime_ui.py`

**Interfaces:**
- Consumes: Task 8 의 `Engine.view()` dict
- Produces:
  - `render(view: dict, width: int = 62, height: int = 24) -> str`
  - `render_plain(view: dict) -> str` — 한 줄 갱신 모드
  - `read_key() -> str | None` — 블로킹 없이 키 하나 (없으면 None)
  - `main()` — `--session`, `--follow`, `--plain`, `--w`

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_realtime_ui.py
import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime.ui import render, render_plain

VIEW = {"state": "motion", "since": 100.0, "held_s": 134.0, "confidence": 0.87, "note": "",
        "remaining_s": 0.0, "n_rx": 3, "hz": 99.8, "threshold": 0.7,
        "events": [{"t": 100.0, "from": "empty", "to": "motion", "confidence": .9},
                   {"t": 40.0, "from": "static", "to": "empty", "confidence": .8}]}


class RenderTest(unittest.TestCase):
    def test_state_block_dominates_the_screen(self):
        out = render(VIEW, width=62, height=24).splitlines()
        self.assertEqual(len(out), 24)
        block = "\n".join(out[1:10])
        self.assertIn("움직임", block)
        blank = sum(1 for line in out[1:10] if not line.strip("│ "))
        self.assertGreaterEqual(blank, 4)          # 여백이 강조 장치다

    def test_held_time_and_confidence_are_shown(self):
        out = render(VIEW)
        self.assertIn("2분 14초", out)
        self.assertIn("87%", out)

    def test_recent_changes_are_listed_newest_first(self):
        out = render(VIEW)
        self.assertLess(out.index("움직임"), out.index("빈 방"))

    def test_deciding_shows_the_remaining_seconds(self):
        view = dict(VIEW, state="deciding", remaining_s=22.0, confidence=0.0, held_s=38.0)
        self.assertIn("22초", render(view))

    def test_degraded_note_is_rendered(self):
        view = dict(VIEW, state="static", n_rx=2, note="RX 2대 — 확신 85% 이상일 때만 판단")
        self.assertIn("RX 2대", render(view))

    def test_short_terminal_drops_history_but_keeps_the_state_block(self):
        out = render(VIEW, height=13).splitlines()
        self.assertEqual(len(out), 13)
        self.assertIn("움직임", "\n".join(out))
        self.assertNotIn("빈 방", "\n".join(out))

    def test_plain_mode_is_one_line(self):
        line = render_plain(VIEW)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("MOTION", line.upper())
        self.assertIn("99.8", line)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_ui.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'realtime.ui'`

- [ ] **Step 3: 구현한다**

```python
#!/usr/bin/env python3
"""CLI 화면. 터미널에서는 글자 크기가 없으므로 **공간 배분**으로 서열을 만든다.

    python realtime/ui.py --session <세션> --follow
    python realtime/ui.py --session <세션> --plain     # 한 줄 갱신 (원격·로그용)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from realtime.engine import Engine  # noqa: E402

LABEL = {"empty": "빈 방", "static": "정지", "motion": "움직임",
         "deciding": "판단 중", "suspended": "판단 정지"}
COLOR = {"empty": "\x1b[90m", "static": "\x1b[34m", "motion": "\x1b[33m",
         "deciding": "\x1b[2m", "suspended": "\x1b[31m"}
RESET = "\x1b[0m"


def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}분 {s % 60:02d}초" if s >= 60 else f"{s}초"


def bar(fraction: float, width: int = 25) -> str:
    filled = int(round(max(0.0, min(1.0, fraction)) * width))
    return "█" * filled + "░" * (width - filled)


def render(view: dict, width: int = 62, height: int = 24, color: bool = False) -> str:
    inner = width - 2
    state = view["state"]
    name = LABEL.get(state, state)
    if color:
        name = f"{COLOR.get(state, '')}{name}{RESET}"

    rows: list[str] = [f"┌─ MeshSense 실시간 {'─' * max(0, inner - 28)} {time.strftime('%H:%M:%S')} ─┐"]

    block = ["", f"     {name}", ""]
    if state == "deciding":
        block += [f"     {hms(view['held_s'])}째",
                  f"     남음  {bar(1 - view['remaining_s'] / max(1e-6, view['remaining_s'] + view['held_s']))}"
                  f"  {int(view['remaining_s'])}초"]
    elif state == "suspended":
        block += [f"     {view['note']}"]
    else:
        block += [f"     {hms(view['held_s'])}째",
                  f"     확신  {bar(view['confidence'])}  {view['confidence']:.0%}"]
    if view.get("note") and state != "suspended":
        block += ["", f"     {view['note']}"]

    history_rows = max(0, height - 12)
    block += [""] * max(0, 9 - len(block))
    rows += [f"│{line[:inner]:<{inner}}│" for line in block[:9]]

    if history_rows > 0:
        rows.append("├" + "─" * inner + "┤")
        for e in view["events"][::-1][:history_rows]:
            line = (f"  {time.strftime('%H:%M:%S', time.localtime(e.get('wall', time.time())))}"
                    f"   {LABEL.get(e['from'], e['from'])} → {LABEL.get(e['to'], e['to'])}")
            rows.append(f"│{line[:inner]:<{inner}}│")

    rows.append("├" + "─" * inner + "┤")
    foot = (f"  {view['hz']:.1f}Hz  RX{view['n_rx']}  "
            f"{'저하' if view['n_rx'] == 2 else '정상'}        [m]전환 [q]종료")
    rows.append(f"│{foot[:inner]:<{inner}}│")
    rows.append("└" + "─" * inner + "┘")
    return "\n".join(rows[:height])


def render_plain(view: dict) -> str:
    return (f"[{time.strftime('%H:%M:%S')}] {view['state'].upper():9s} "
            f"{view['confidence']:.0%}  {hms(view['held_s'])}  "
            f"{view['hz']:.1f}Hz RX{view['n_rx']}")


def read_key() -> str | None:
    """대기 없이 키 하나를 읽는다. 터미널이 아니면(파이프·테스트) None."""
    import select

    if not sys.stdin.isatty():
        return None
    ready, _, _ = select.select([sys.stdin], [], [], 0)
    return sys.stdin.read(1) if ready else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session", type=Path, required=True)
    ap.add_argument("--follow", action="store_true", help="실시간(파일 tail). 없으면 재생")
    ap.add_argument("--plain", action="store_true", help="한 줄 갱신 모드")
    ap.add_argument("--w", type=float, default=60.0, help="2단계 윈도 (초)")
    args = ap.parse_args()

    eng = Engine(args.session, w_seconds=args.w)
    try:
        while True:
            key = read_key()
            if key == "q":
                break
            if key == "m":
                eng.mark()              # 운영자가 상태를 바꾼 시각 (정답 기록)
            eng.step()
            view = eng.view()
            if args.plain:
                print(render_plain(view), flush=True)
            else:
                print("\x1b[H\x1b[J" + render(view, color=True), flush=True)
            if not args.follow:
                if not eng._advance():
                    break
            else:
                time.sleep(0.3)
    except KeyboardInterrupt:
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_ui.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: 눈으로 확인한다**

```bash
.venv/bin/python realtime/ui.py --session mac_collector_output/raw/20260917/213701_static_s42 | head -30
```
Expected: 상태 블록이 화면 위쪽을 넓게 차지하고 이력이 아래에 쌓인다

- [ ] **Step 6: 커밋**

```bash
git add realtime/ui.py tests/test_realtime_ui.py
git commit -m "feat : 실시간 CLI 화면 (공간 배분 기반, --plain 한 줄 모드)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 10: 제어판 실시간 탭

spec §8 의 GUI 시안을 구현한다. 탭 순서는 맨 앞, 색 체계는 spec 표 그대로.

**Files:**
- Modify: `scripts/meshsense_gui.py` (탭 추가, `/api/realtime`, CSS)
- Test: `tests/test_realtime_gui.py`

**Interfaces:**
- Consumes: `<세션>/realtime_status.json`, `<세션>/realtime_events.jsonl`
- Produces:
  - `realtime_view() -> dict | None` — 가장 최근 세션의 상태. 없으면 None
  - `timeline_spans(events: list[dict], now: float, minutes: int = 60) -> list[dict]`
    — `[{"state": str, "pct": float}]`, 60분 띠용. 1분 미만 구간도 최소 폭으로 남긴다

- [ ] **Step 1: 실패하는 테스트를 쓴다**

```python
# tests/test_realtime_gui.py
import json, sys, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import meshsense_gui as gui


class TimelineTest(unittest.TestCase):
    def test_spans_cover_the_whole_window(self):
        events = [{"t": 0.0, "from": "deciding", "to": "empty"},
                  {"t": 1800.0, "from": "empty", "to": "motion"}]
        spans = gui.timeline_spans(events, now=3600.0, minutes=60)
        self.assertAlmostEqual(sum(s["pct"] for s in spans), 100.0, places=3)
        self.assertEqual(spans[-1]["state"], "motion")

    def test_short_span_is_kept_with_a_minimum_width(self):
        events = [{"t": 0.0, "from": "deciding", "to": "empty"},
                  {"t": 3595.0, "from": "empty", "to": "motion"},
                  {"t": 3597.0, "from": "motion", "to": "empty"}]
        spans = gui.timeline_spans(events, now=3600.0, minutes=60)
        motion = [s for s in spans if s["state"] == "motion"]
        self.assertEqual(len(motion), 1)
        self.assertGreaterEqual(motion[0]["pct"], 0.5)

    def test_realtime_view_reads_the_status_file(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "raw" / "20260930" / "120000_empty_s1"
            d.mkdir(parents=True)
            (d / "session.json").write_text('{"label": "empty", "session_id": 1}')
            (d / "realtime_status.json").write_text(json.dumps(
                {"state": "static", "confidence": .91, "held_s": 65.0, "n_rx": 2,
                 "note": "RX 2대", "hz": 99.5, "remaining_s": 0, "events": []}))
            view = gui.realtime_view(raw_root=d.parents[1])
            self.assertEqual(view["state"], "static")
            self.assertEqual(view["n_rx"], 2)

    def test_realtime_view_is_none_without_a_status_file(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertIsNone(gui.realtime_view(raw_root=Path(t)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_gui.py -v`
Expected: FAIL — `AttributeError: module 'meshsense_gui' has no attribute 'timeline_spans'`

- [ ] **Step 3: 서버 쪽을 구현한다**

```python
# scripts/meshsense_gui.py — session_rows() 위에 추가
STATE_ORDER = ("empty", "static", "motion", "deciding", "suspended")


def realtime_view(raw_root: Path = RAW_ROOT) -> Optional[dict]:
    """가장 최근 세션의 realtime_status.json. 실시간 판단이 안 돌면 None."""
    for d in sorted(Path(raw_root).glob("*/*"), reverse=True):
        p = d / "realtime_status.json"
        if p.is_file():
            try:
                view = json.loads(p.read_text(encoding="utf-8"))
            except json.JSONDecodeError:      # rename 직전의 부분 파일
                continue
            view["session"] = d.name
            view["spans"] = timeline_spans(view.get("events", []), view.get("t", 0.0))
            return view
    return None


def timeline_spans(events: list, now: float, minutes: int = 60) -> list[dict]:
    """최근 `minutes` 분을 상태 구간으로 자른다. 폭 0인 구간도 최소 0.5% 로 남긴다."""
    span_s = minutes * 60.0
    start = now - span_s
    marks = [(max(e["t"], start), e["to"]) for e in events if e["t"] >= start]
    first = next((e["from"] for e in events if e["t"] >= start), "deciding")
    marks.insert(0, (start, first))
    out = []
    for (t0, state), (t1, _) in zip(marks, marks[1:] + [(now, None)]):
        out.append({"state": state, "pct": max(0.5, 100.0 * (t1 - t0) / span_s)})
    total = sum(s["pct"] for s in out) or 1.0
    for s in out:
        s["pct"] = round(100.0 * s["pct"] / total, 3)
    return out
```

`/api/state` 응답에 `"realtime": realtime_view()` 를 추가하고, `dispatch()` 에 전환 표시를 넣는다.

```python
    if path == "/api/mark":
        view = realtime_view()
        if not view:
            return {"error": "실시간 판단이 실행 중이 아닙니다"}
        target = next((d for d in sorted(RAW_ROOT.glob("*/*"), reverse=True)
                       if d.name == view["session"]), None)
        if target is None:
            return {"error": "세션을 찾지 못했습니다"}
        row = {"wall": time.time(), "note": str(body.get("note", "")), "via": "gui"}
        with (target / "realtime_marks.jsonl").open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(row, ensure_ascii=False) + "\n")
        return {"ok": True}
```

`import time` 을 파일 상단에 추가한다. 화면의 `전환 표시` 버튼은 `post('/api/mark')` 를 부른다.
엔진이 남기는 마크와 같은 파일에 붙는다. GUI 마크에는 `tx_seq` 가 없으므로 지연 측정은
엔진 마크를 우선 쓰고 GUI 마크는 보조로 쓴다.

- [ ] **Step 4: 통과를 확인한다**

Run: `.venv/bin/python -m pytest tests/test_realtime_gui.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: 화면을 붙인다**

`PAGE` 의 `nav` 맨 앞에 `<button data-t="realtime" class="on">실시간</button>` 을 넣고
(다른 탭의 `class="on"` 은 제거), `<section id="realtime" class="on"></section>` 을 추가한다.
`CSS` 에는 spec §8 의 색 토큰과 hero/타임라인 규칙을 넣는다 — 시안 파일
(`gui_mock_*.html`) 의 `실시간 탭` 블록을 그대로 옮긴다. `render()` 에 탭 렌더러를 추가한다.

```javascript
  if (tab === 'realtime' && changed('realtime', [S.realtime])) {
    const r = S.realtime;
    document.getElementById('realtime').innerHTML = !r
      ? `<div class="card"><h2>실시간 판단이 실행 중이 아닙니다</h2>
         <p class="sub">터미널에서: <code>python realtime/ui.py --session &lt;세션&gt; --follow</code></p></div>`
      : `<div class="card hero s-${esc(r.state)}">
           <p class="state">${esc(LABEL[r.state] || r.state)}</p>
           <p class="sub-state"><b>${fmtHeld(r.held_s)}째</b><span>· ${esc(r.session)}</span></p>
           <div class="meter"><span class="lab">${r.state === 'deciding' ? '남음' : '확신'}</span>
             <span class="track"><span class="fill" style="width:${r.state === 'deciding'
               ? 100 - Math.min(100, r.remaining_s) : Math.round(r.confidence * 100)}%"></span>
               <span class="mark" style="left:${Math.round((r.threshold || .7) * 100)}%"></span></span>
             <span class="num">${r.state === 'deciding' ? Math.round(r.remaining_s) + '초'
               : Math.round(r.confidence * 100) + '%'}</span></div>
           ${r.note ? `<p class="note">${esc(r.note)}</p>` : ''}
         </div>
         <div class="card"><h2>최근 60분</h2>
           <div class="tl">${r.spans.map(s => `<i class="${s.state[0]}" style="width:${s.pct}%"></i>`).join('')}</div>
           <div class="axis"><span>60분 전</span><span>30분 전</span><span>지금</span></div></div>
         <div class="card"><h2>상태 변화</h2><table class="chg">${r.events.slice().reverse().map(e =>
           `<tr><td class="t">${fmtClock(e.t)}</td><td>${esc(LABEL[e.from] || e.from)}
            <span class="arrow">→</span><b>${esc(LABEL[e.to] || e.to)}</b></td>
            <td class="d">${Math.round(e.confidence * 100)}%</td></tr>`).join('')}</table></div>`;
  }
```

- [ ] **Step 6: 브라우저로 확인한다**

```bash
.venv/bin/python scripts/meshsense_gui.py --no-open --port 8791
```
다른 터미널에서 녹화 세션을 재생해 상태 파일을 만들고(`realtime/ui.py --session ... --plain`),
`http://127.0.0.1:8791/` 의 실시간 탭이 갱신되는지 본다. 판단이 안 돌 때는 안내 문구만 보여야 한다.

- [ ] **Step 7: 커밋**

```bash
git add scripts/meshsense_gui.py tests/test_realtime_gui.py
git commit -m "feat : 제어판 실시간 탭 — 상태·60분 띠·상태 변화

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 11: 문서 갱신과 전체 점검

**Files:**
- Modify: `doc/realtime-inference.md` (상태 PLANNED → CURRENT, 실행법 추가)
- Modify: `CLAUDE.md` (Authoritative Modules 에 `realtime/` 추가)
- Modify: `scripts/README.md` (실시간 실행 항목)
- Modify: `doc/sprint/2026-08-collection-hardening.md` (구현 결과 기록)

- [ ] **Step 1: 전체 테스트를 돌린다**

Run: `.venv/bin/python -m pytest tests/ -q --ignore=tests/test_robust_three_class.py --ignore=tests/test_phase_experiment.py`
Expected: 모두 통과 (scipy 를 설치했다면 무시 옵션 없이 전부)

- [ ] **Step 2: 문서를 고친다**

- `doc/realtime-inference.md`: 상태 줄을 `**CURRENT** — 구현 완료(실시간·재생), 성능 검증은 §6 미완료`
  로 바꾸고, §9 구현 순서 표에 완료 표시와 실행 명령을 적는다.
- `CLAUDE.md` 의 Authoritative Modules 표에 한 줄 추가:
  `| realtime/ | 실시간 상태 판단 — .csi tail, 2단계 모델, 상태 기계, CLI |`
  그리고 Current Architecture 그림의 `.csi` 아래에 `realtime/engine.py` 가지를 추가한다.
- `scripts/README.md` 에 실행법을 적는다.

- [ ] **Step 3: 커밋**

```bash
git add doc/realtime-inference.md CLAUDE.md scripts/README.md doc/sprint/2026-08-collection-hardening.md
git commit -m "docs : 실시간 판단 구현 반영 (모듈 표·실행법·스프린트 기록)

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 이 계획에 없는 것 (사람이 해야 하는 일)

spec §6·§7 의 검증과 수집은 보드와 사람이 필요해 계획에서 제외했다. 구현이 끝난 뒤 순서는:

1. 전환 세션 수집(첫 묶음) → 규칙 ①~⑤ 상수 조정 → 재생으로 반복 확인
2. 규칙 고정 → 새 날짜 holdout 수집 → **한 번만** 평가
3. 장시간 오탐 측정(빈 방 야간, 책상 1시간), 다른 피험자 수집

2번의 holdout 은 1번 조정에 쓰면 안 된다. 한 번 본 데이터는 시험지가 아니다.
