#!/usr/bin/env python3
"""모델의 **보이는 이름·설명·실시간 노출** — 체크포인트 바깥에 둔다.

체크포인트가 늘면서 이름만으로는 뜻이 서지 않게 됐다. `amp1500_all74`(실시간 기본)와
`amp1500_60sessions`(날짜 교차 대조군)은 창 길이까지 같아 실시간 목록에서 구분되지 않는다.

**체크포인트 안에 쓰지 않는다.** 이름 한 번 고치자고 `torch.load`/`save` 를 돌리면 가중치를
건드릴 위험이 있다. `model_train/models.json` 한 파일에 경로를 키로 둔다 — 사람이 열어 고칠
수 있고, 다시 학습해도 살아남고, 지우면 파일 이름으로 돌아간다.

**파일 이름은 바뀌지 않는다.** 보이는 이름만 바뀌고 화면은 경로를 함께 보인다.
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STORE = ROOT / "model_train" / "models.json"

#: 코드가 직접 가리키는 모델. 실시간 목록에서 뺄 수 없다 — 빼면 실시간이 안 돈다.
#: `realtime/engine.py` 의 MODEL·STAGE1 과 같아야 하고, 테스트가 둘을 묶는다.
PINNED = ("model_train/amp60/model_amp1500_all74.pt",
          "model_train/amp60/model_motion3s.pt")

FIELDS = ("name", "note", "realtime")


def load(path: Path | None = None) -> dict:
    """저장된 값. 파일이 없거나 깨졌으면 빈 dict — 화면은 파일 이름으로 뜬다."""
    path = STORE if path is None else path
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(store: dict, path: Path | None = None) -> None:
    path = Path(STORE if path is None else path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")


def entry(model_path: str, store: dict | None = None) -> dict:
    """한 모델의 `{name, note, realtime, pinned}`. 저장된 값이 없으면 파일 이름으로 채운다."""
    store = load() if store is None else store
    key = str(model_path)
    saved = store.get(key) or {}
    pinned = key in PINNED
    return {
        "name": saved.get("name") or Path(key).stem.replace("model_", ""),
        "note": saved.get("note", ""),
        #: 고정 모델은 저장값이 무엇이든 켜져 있다
        "realtime": True if pinned else bool(saved.get("realtime", True)),
        "pinned": pinned,
    }


def update(model_path: str, changes: dict, path: Path | None = None) -> dict:
    """한 모델의 값을 고친다. 준 칸만 바꾸고 나머지는 둔다."""
    key = str(model_path)
    if key in PINNED and changes.get("realtime") is False:
        raise ValueError("코드가 쓰는 모델은 실시간 목록에서 뺄 수 없습니다")
    store = load(path)
    cur = dict(store.get(key) or {})
    for f in FIELDS:
        if f not in changes:
            continue
        if f == "realtime":
            cur[f] = bool(changes[f])
        elif (v := str(changes[f]).strip()):
            cur[f] = v
        else:
            cur.pop(f, None)                           # 빈 값은 지운다 = 기본으로 돌아간다
    store[key] = cur
    save(store, path)
    return entry(key, store)


def forget(model_path: str, path: Path | None = None) -> None:
    """레지스트리에서 지운다. 체크포인트를 지울 때 같이 부른다 — 남겨 두면 같은 이름으로
    다시 학습했을 때 옛 설명이 되살아나 틀린 설명이 붙는다."""
    store = load(path)
    if store.pop(str(model_path), None) is not None:
        save(store, path)


if __name__ == "__main__":  # 셀프체크: 실제 체크포인트에 붙여 본다
    store = load()
    for p in sorted(ROOT.glob("model_train/*/model*.pt")):
        rel = str(p.relative_to(ROOT))
        e = entry(rel, store)
        mark = " (고정)" if e["pinned"] else ("" if e["realtime"] else " [실시간 끔]")
        print(f"  {e['name']:28s}{mark}")
        if e["note"]:
            print(f"      {e['note']}")
