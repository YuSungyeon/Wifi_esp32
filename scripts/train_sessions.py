#!/usr/bin/env python3
"""학습용 세션 목록과 선택 규칙 — CLI 와 제어판이 같은 것을 쓴다.

화면이 편해질수록 "학습에 쓴 데이터로 평가하는" 실수가 쉬워진다. 그래서 규칙을 화면이 아니라
여기에 두고, 학습기도 실행 직전에 같은 검사를 한 번 더 한다(이중 방어).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from csi_session import read_manifest  # noqa: E402
from csi_store import LABELS  # noqa: E402

RAW_ROOT = ROOT / "mac_collector_output" / "raw"
MIN_PER_CLASS = 2      # 클래스마다 최소 세션 수 — 한 세션만으로는 학습도 평가도 의미가 없다

# 라벨 정의를 벗어난 세션 — 학습·평가 목록에서 뺀다. 데이터·session.json 은 건드리지 않는다.
# 근거는 doc/troubleshooting/04-session-labels.md 의 해당 날짜 항목. 새 항목은 평가 전에 넣는다.
EXCLUDED_SESSIONS = {
    "20260919_12": "static 라벨이지만 1단계 모델로 300초 내내 산발적 움직임(0.7 초과 창 5.1%) — "
                   "프로토콜 §2 'static = 호흡 외 움직임 없음' 위반 (2026-10-12 ② 채택)",
}


def is_excluded(key: str) -> bool:
    return key in EXCLUDED_SESSIONS


def session_key(date: str, dir_name: str) -> str:
    return f"{date}_{dir_name.rsplit('_s', 1)[1]}"


def list_sessions(raw_root: Path = RAW_ROOT) -> list[dict]:
    """학습에 쓸 수 있는 세션 전부. 날짜·이름 순."""
    out = []
    for d in sorted(Path(raw_root).glob("*/*")):
        if not (d / "session.json").is_file():
            continue
        label = d.name.split("_")[1]
        if label not in LABELS or is_excluded(session_key(d.parent.name, d.name)):
            continue
        try:
            m = read_manifest(d)
        except (OSError, ValueError):
            continue
        devices = m.get("devices", [])
        span = max((x.get("span_s") or 0) for x in devices) if devices else 0
        # 수집 품질 판정과 같은 눈높이: 스트림 손상·재부팅이 있으면 학습에서 빼는 쪽이 기본
        broken = any(x.get("crc_fail") or x.get("boot_changes") or x.get("tx_back")
                     for x in devices)
        out.append({
            "key": session_key(d.parent.name, d.name),
            "date": d.parent.name, "name": d.name, "label": label,
            "span_s": round(span, 1), "rx": len(devices),
            "hz": round(sum(x["frames"] for x in devices) / len(devices) / span, 1)
            if devices and span else 0.0,
            "ok": bool(devices) and not broken and len(devices) == 3,
            "path": str(d.relative_to(ROOT)),
        })
    return out


def group_by_date(sessions: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for s in sessions:
        out.setdefault(s["date"], []).append(s)
    return out


def dates_of(keys) -> list[str]:
    return sorted({k.split("_")[0] for k in keys})


def check_selection(keys, eval_dates, sessions: list[dict] | None = None) -> list[str]:
    """선택이 학습에 쓸 수 있는지. 문제를 한국어 문장 목록으로 돌려준다(없으면 빈 목록)."""
    sessions = sessions if sessions is not None else list_sessions()
    by_key = {s["key"]: s for s in sessions}
    problems = []

    unknown = [k for k in keys if k not in by_key]
    if unknown:
        problems.append(f"없는 세션: {', '.join(sorted(unknown))}")
    chosen = [by_key[k] for k in keys if k in by_key]
    if not chosen:
        problems.append("세션을 하나도 고르지 않았습니다")
        return problems

    counts = {label: sum(1 for s in chosen if s["label"] == label) for label in LABELS}
    short = [f"{label} {counts[label]}개" for label in LABELS if counts[label] < MIN_PER_CLASS]
    if short:
        problems.append(f"클래스마다 {MIN_PER_CLASS}세션 이상이어야 합니다 — 지금 {', '.join(short)}")

    overlap = set(dates_of(keys)) & set(eval_dates or [])
    if overlap:
        problems.append(f"학습에 쓴 날짜는 평가에 쓸 수 없습니다: {', '.join(sorted(overlap))}")

    bad = [s["key"] for s in chosen if not s["ok"]]
    if bad:
        problems.append(f"품질 미달 세션이 있습니다(학습에서 빼기를 권합니다): {', '.join(bad)}")
    return problems


def available_eval_dates(keys, sessions: list[dict] | None = None) -> list[str]:
    """고른 세션의 날짜를 뺀 나머지 날짜. 평가용으로 고를 수 있는 것들이다."""
    sessions = sessions if sessions is not None else list_sessions()
    locked = set(dates_of(keys))
    return [d for d in sorted({s["date"] for s in sessions}) if d not in locked]


if __name__ == "__main__":  # 셀프체크: 실제 수집 폴더로 규칙이 도는지
    rows = list_sessions()
    print(f"세션 {len(rows)}개 · 날짜 {len(group_by_date(rows))}개")
    for date, items in group_by_date(rows).items():
        ok = sum(1 for s in items if s["ok"])
        print(f"  {date}  {len(items):>2}세션 (정상 {ok})  "
              + " ".join(f"{s['label'][0]}{s['key'].split('_')[1]}" for s in items[:6])
              + (" ..." if len(items) > 6 else ""))
    sample = [s["key"] for s in rows if s["date"] in ("20260916", "20260917")]
    print("평가 가능 날짜:", available_eval_dates(sample))
    print("검사:", check_selection(sample, ["20260919"]) or "통과")
    print("겹치게 하면:", check_selection(sample, ["20260916"]))
