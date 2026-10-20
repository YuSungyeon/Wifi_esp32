#!/usr/bin/env python3
"""학습 실행기 — 고른 세션으로 모델을 학습하고 진행률을 표준출력으로 흘린다 (GUI·CLI 공용).

    python scripts/_train_run.py --preset balanced --sessions 20260916_31 ... --eval 20260919

진행률은 학습기가 찍는 `seed N epoch i/total` 줄을 세어 계산한다. 남은 시간은 **첫 epoch 이
끝나면 실측으로 갈아탄다** — 기기·설정마다 속도가 달라 사전 추정만으로는 빗나간다.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
PY = str(ROOT / ".venv" / "bin" / "python") if (ROOT / ".venv" / "bin" / "python").is_file() \
    else sys.executable

#: 화면에 노출하는 설정은 검증된 것만 둔다. 실험에서 나빠진 조합(증강·가중치·차분·스펙트럼)은
#: 올리지 않는다. 수치는 model_train/phase60/README.md 의 실측값이다.
PRESETS = {
    "balanced": {
        "label": "균형", "trainer": "phase60",
        "detail": "위상 60초 10Hz · 봉인 0.959 · 전환 지연 31초",
        "args": ["--window", "6000", "--downsample", "10"],
        "steps": 600,
    },
    "accurate": {
        "label": "정확도 우선", "trainer": "phase60",
        "detail": "위상 90초 10Hz · 봉인 0.981 · 전환 지연 59초",
        "args": ["--window", "9000", "--downsample", "10"],
        "steps": 900,
    },
    "responsive": {
        "label": "짧은 사건 우선", "trainer": "phase60",
        "detail": "위상 60초 5Hz · 20초 사건 감지",
        "args": ["--window", "6000", "--downsample", "20"],
        "steps": 300,
    },
    "amplitude": {
        "label": "진폭 (담당자 구조)", "trainer": "amp60",
        "detail": "진폭 30초 · 배포 모델과 같은 입력 · 공식 산출물 아님",
        "args": ["--window", "3000"],
        "steps": 300,
    },
}
TRAINERS = {"phase60": ROOT / "model_train" / "phase60" / "train.py",
            "amp60": ROOT / "model_train" / "amp60" / "train.py"}
#: 학습기마다 `--train-stride` 기본값이 다르다. 하나로 뭉뚱그리면 창 수가 5배 틀린다.
#: 값은 각 train.py 의 add_argument 기본값이고, 바뀌면 여기 테스트가 깨진다.
TRAINER_STRIDE = {"phase60": 30, "amp60": 150}
TRAINER_WINDOW = {"phase60": 6000, "amp60": 3000}
#: 사전 추정용 계수 (초 / (창 × 스텝 × epoch × seed)). 위상 모델 실측에서 얻었다 —
#: v1 11789창×300스텝 1631초, v3 11789×600 2629초, v2 10189×900 3415초 → 1.24~1.54e-5.
#: 진폭 모델은 시계열이 1/3 이라 더 빠르다. 어차피 첫 epoch 뒤 실측으로 대체된다.
SECONDS_PER_UNIT = 1.24e-5
#: 학습기별 계수 (초 / (창 × 스텝 × epoch × seed)). **진폭이 위상보다 5배 빠르다** —
#: 위상은 156열을 들고 특징을 뽑지만 진폭은 51계열이다. 하나로 쓰면 버튼이 5배 부풀린다.
#: 실측: 위상 11789창×300스텝 1631초 / 진폭 2687창×300스텝 62초.
TRAINER_RATE = {"phase60": 1.24e-5, "amp60": 2.56e-6}
EPOCH_RE = re.compile(r"seed (\d+) epoch (\d+)/(\d+)")
#: 러너 기본값. 화면이 예상 시간을 낼 때 그대로 읽는다 — 양쪽에 박아 두면 한쪽만 바뀐다.
DEFAULT_EPOCHS, DEFAULT_SEEDS = 10, 3


def preset_shape(preset: dict) -> dict:
    """프리셋의 창·stride·스텝 — 화면이 버튼에 쓸 시간을 어림하는 데 쓴다.

    창은 `--window` 인자에서, stride 는 학습기 기본값(150)에서 온다. 인자로 명시되지 않은
    것은 학습기와 같은 기본값을 쓴다.
    """
    args, trainer = preset["args"], preset["trainer"]
    window = (int(args[args.index("--window") + 1]) if "--window" in args
              else TRAINER_WINDOW[trainer])
    stride = (int(args[args.index("--train-stride") + 1]) if "--train-stride" in args
              else TRAINER_STRIDE[trainer])
    return {"window": window, "train_stride": stride, "steps": preset["steps"],
            "rate": TRAINER_RATE[trainer], "epochs": DEFAULT_EPOCHS, "seeds": DEFAULT_SEEDS}


def predict_windows(sessions: list, window: int, stride: int) -> int:
    """학습기가 데이터를 읽기 **전에** 창 수를 어림한다 — 버튼에 예상 시간을 쓰려면 필요하다.

    실제 창 수는 누락이 낀 창을 빼므로 이보다 조금 적다. 그래서 화면에는 `약` 을 붙이고,
    첫 epoch 이 끝나면 실측 속도로 갈아탄다.
    """
    total = 0
    for s in sessions:
        frames = int((s.get("span_s") or 0) * 100)
        if frames >= window:
            total += (frames - window) // stride + 1
    return total


def estimate_seconds(windows: int, steps: int, epochs: int, seeds: int,
                     trainer: str = "phase60") -> float:
    return TRAINER_RATE.get(trainer, SECONDS_PER_UNIT) * windows * steps * epochs * seeds


def hms(seconds: float) -> str:
    s = int(max(0, seconds))
    return f"{s // 60}분 {s % 60:02d}초" if s >= 60 else f"{s}초"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--preset", choices=tuple(PRESETS), default="balanced")
    ap.add_argument("--name", default=None, help="체크포인트 이름 (기본: 날짜-프리셋)")
    ap.add_argument("--note", default="", help="화면에 보일 한 줄 설명 (model_train/models.json)")
    ap.add_argument("--sessions", nargs="+", required=True)
    ap.add_argument("--eval", nargs="+", default=[])
    ap.add_argument("--seeds", type=int, nargs="+", default=list(range(DEFAULT_SEEDS)))
    ap.add_argument("--epochs", type=int, default=DEFAULT_EPOCHS)
    args = ap.parse_args()

    preset = PRESETS[args.preset]
    name = args.name or f"{time.strftime('%Y%m%d-%H%M')}-{args.preset}"
    cmd = [PY, str(TRAINERS[preset["trainer"]]), "--name", name,
           "--sessions", *args.sessions, "--seeds", *map(str, args.seeds),
           "--epochs", str(args.epochs), *preset["args"]]
    if args.eval:
        cmd += ["--eval", *args.eval]

    print(f"[설정] {preset['label']} — {preset['detail']}", flush=True)
    print(f"[대상] 학습 {len(args.sessions)}세션 · 평가 {args.eval or '없음'} · "
          f"seed {len(args.seeds)}개 · epoch {args.epochs}", flush=True)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, cwd=str(ROOT))
    total_epochs = args.epochs * len(args.seeds)
    done = 0
    started = time.monotonic()
    first_epoch_at = None
    assert proc.stdout
    for line in proc.stdout:
        line = line.rstrip()
        m = EPOCH_RE.search(line)
        if not m:
            print(line, flush=True)
            if line.startswith("  학습 ") and "창 " in line:       # 창 수를 알면 사전 추정을 낸다
                try:
                    windows = int(line.split("창 ")[1].split("개")[0])
                    eta = estimate_seconds(windows, preset["steps"], args.epochs,
                                           len(args.seeds), preset["trainer"])
                    print(f"[예상] 약 {hms(eta)} (첫 epoch 뒤 실측으로 갱신)", flush=True)
                except (IndexError, ValueError):
                    pass
            continue
        done += 1
        if first_epoch_at is None:
            first_epoch_at = time.monotonic()
        elapsed = time.monotonic() - started
        # 첫 epoch 은 캐시 로딩이 섞여 느리다 — 두 번째부터의 속도로 남은 시간을 잡는다
        rate = ((time.monotonic() - first_epoch_at) / (done - 1)) if done > 1 else elapsed
        remaining = rate * (total_epochs - done)
        print(f"[진행] {done}/{total_epochs} ({100 * done / total_epochs:.0f}%) · "
              f"경과 {hms(elapsed)} · 남은 시간 약 {hms(remaining)} · "
              f"완료 예정 {time.strftime('%H:%M', time.localtime(time.time() + remaining))}",
              flush=True)
    rc = proc.wait()
    print(f"[{'완료' if rc == 0 else '실패'}] 총 {hms(time.monotonic() - started)} (코드 {rc})",
          flush=True)
    if rc == 0 and args.note:
        #: 설명은 체크포인트 밖에 둔다 — 고칠 때마다 가중치를 다시 쓰지 않는다
        import model_registry
        pt = TRAINERS[preset["trainer"]].parent / f"model_{name}.pt"
        if pt.is_file():
            model_registry.update(str(pt.relative_to(ROOT)), {"note": args.note})
            print(f"[설명] {args.note}", flush=True)
    if rc == 0:
        out = TRAINERS[preset["trainer"]].parent / f"model_{name}.metrics.json"
        if out.is_file():
            m = json.loads(out.read_text())
            if "sessions" in m:
                print(f"[결과] 세션 {m['sessions']} · 사람 있음/없음 {m['presence']} · "
                      f"빈방vs정지 {m['empty_vs_static']} · 창 정답률 {m['window_accuracy']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
