#!/usr/bin/env python3
"""3-RX CSI 전처리 공식 구현.

`model_train/docs/[전처리]-설계.md` (OFFICIAL DESIGN)의 처리 순서를 그대로 구현한다.

    1. RX별 JSONL을 파일 순서대로 읽기
    2. 이전·현재·다음의 seq, tx_seq로 단일 손상 record 제거
    3. seq로 RX 재부팅 segment 분리
    4. 손상 제거 후 tx_seq의 지속 감소가 없는지 검증
    5. 세 RX의 안정 segment 조합 만들기
    6. 각 조합의 tx_seq 교집합 계산
    7. 가장 긴 유효 교집합 선택
    8. 최소 길이·관측률 검사
    9. 공통 tx_seq grid와 실제 수신 mask 생성
    10. 5 frame 이하의 내부 누락만 선형 보간
    11. 긴 누락이 포함된 3초 window 제외
    12. 세 RX의 64개 feature 결합
    13. session 단위 split에 window 저장
    14. train 데이터 통계로만 normalization

`received_at_unix_us`는 정렬·segment·제외 판정 어디에도 사용하지 않는다.

실행 예:

    python3 model_train/preprocessing/preprocess_3rx.py \
      --raw-dir mac_collector_output/raw/20260616

출력 (기본 `model_train/preprocessing/output/<raw 폴더명>/`):

    train|validation|test/X.npy   (N, 300, 192) float32, raw amplitude
    train|validation|test/y.npy   (N,) int64
    train|validation|test/windows.jsonl  window별 출처 metadata
    normalization.npz             train 통계 (저장된 X에는 적용하지 않음)
    manifest.json                 세션별 선택·제외 근거와 사용한 임계값
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path

import numpy as np

# Label 이름과 번호는 dataset metadata 기준으로 고정한다 (설계 5.13).
LABEL_MAP = {"empty": 0, "static": 1, "motion": 2}

# 20260616 데이터의 session -> class 배정 (설계 3절).
LABEL_SESSION_RANGES = (
    ("empty", range(1, 11)),
    ("static", range(11, 21)),
    ("motion", range(21, 31)),
)

# 20260616 split 초안 (설계 5.13). session 22는 품질 gate 제외 대상이라 미배정.
DEFAULT_SPLITS = {
    "train": [1, 2, 3, 4, 5, 6, 11, 12, 13, 14, 15, 16, 21, 23, 24, 25, 26],
    "validation": [7, 8, 17, 18, 27, 28],
    "test": [9, 10, 19, 20, 29, 30],
}

SPLIT_ORDER = ("train", "validation", "test")

# manifest에 남기는 상세 항목(파싱 오류, 제거 record 등)의 최대 개수.
MANIFEST_DETAIL_LIMIT = 50


@dataclass(frozen=True)
class PreprocessConfig:
    """설계 2절의 공식 기준값. test 결과를 본 뒤 바꾸지 않도록 manifest에 기록한다."""

    rx_order: tuple = (101, 102, 103)
    features_per_rx: int = 64
    window: int = 300  # 3초
    stride: int = 30  # 0.3초
    min_common_length: int = 27000  # 30000의 90%
    min_observed_ratio: float = 0.85
    max_interp_gap: int = 5  # 50ms
    reboot_small_seq: int = 10  # seq가 이 값 이하로 돌아가면 재부팅 후보
    reboot_min_drop: int = 100  # 또는 seq가 이만큼 이상 감소
    zero_std_epsilon: float = 1e-6


DEFAULT_CONFIG = PreprocessConfig()


class Record:
    """JSONL 한 줄의 전처리 필수 입력값 (설계 5.1)."""

    __slots__ = ("line_no", "seq", "tx_seq", "amp")

    def __init__(self, line_no, seq, tx_seq, amp):
        self.line_no = line_no
        self.seq = seq
        self.tx_seq = tx_seq
        self.amp = amp


# raw-dir 옆의 labels.json 이 있으면 그것이 정본이다. scripts/export_jsonl.py 가
# 각 세션의 session.json(수집 시점에 박힌 라벨)에서 만들어 준다.
# 없으면 아래 LABEL_SESSION_RANGES 로 떨어진다 — 구 데이터 호환용.
_SESSION_LABELS: dict[int, str] = {}


def load_session_labels(raw_dir):
    """raw-dir 의 labels.json 을 읽어 session_id → label 을 채운다."""
    _SESSION_LABELS.clear()
    path = Path(raw_dir) / "labels.json"
    if not path.is_file():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    _SESSION_LABELS.update({int(k): v for k, v in data.items()})
    return True


def label_name_for_session(session_id):
    if session_id in _SESSION_LABELS:
        return _SESSION_LABELS[session_id]
    for name, ids in LABEL_SESSION_RANGES:
        if session_id in ids:
            return name
    raise ValueError(
        f"session {session_id}의 label 배정이 없다. "
        "scripts/export_jsonl.py 로 labels.json 을 만들거나 "
        "LABEL_SESSION_RANGES 를 갱신해야 한다."
    )


# ---------------------------------------------------------------------------
# 1단계 — RX별 JSONL을 파일 순서대로 읽기 (설계 5.1)
# ---------------------------------------------------------------------------

def parse_jsonl(path, rx_id, cfg):
    """파일 순서를 유지한 record 목록과 오류 목록을 돌려준다.

    오류 줄은 조용히 건너뛰지 않고 위치와 이유를 남긴다 (manifest 기록용).
    """
    records = []
    errors = []
    with open(path, encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, start=1):
            line = line.strip()
            if not line:
                errors.append({"line": line_no, "reason": "빈 줄"})
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                errors.append({"line": line_no, "reason": f"JSON 파싱 실패: {exc.msg}"})
                continue
            missing = [
                key
                for key in ("device_id", "seq", "timestamp_us", "tx_seq", "csi_amp")
                if key not in obj
            ]
            if missing:
                errors.append(
                    {"line": line_no, "reason": f"필수 field 누락: {','.join(missing)}"}
                )
                continue
            if obj["device_id"] != rx_id:
                errors.append(
                    {
                        "line": line_no,
                        "reason": f"device_id 불일치: {obj['device_id']} != {rx_id}",
                    }
                )
                continue
            seq, tx_seq = obj["seq"], obj["tx_seq"]
            if not isinstance(seq, int) or not isinstance(tx_seq, int) or isinstance(
                seq, bool
            ) or isinstance(tx_seq, bool):
                errors.append({"line": line_no, "reason": "seq/tx_seq가 정수가 아님"})
                continue
            amp = obj["csi_amp"]
            if not isinstance(amp, list) or len(amp) != cfg.features_per_rx:
                errors.append(
                    {
                        "line": line_no,
                        "reason": f"csi_amp 길이 이상: {len(amp) if isinstance(amp, list) else type(amp).__name__}",
                    }
                )
                continue
            try:
                amp_arr = np.asarray(amp, dtype=np.float32)
            except (TypeError, ValueError):
                errors.append({"line": line_no, "reason": "csi_amp에 숫자가 아닌 값"})
                continue
            records.append(Record(line_no, seq, tx_seq, amp_arr))
    return records, errors


# ---------------------------------------------------------------------------
# 2단계 — 단일 손상 record 제거 (설계 5.2)
# ---------------------------------------------------------------------------

def remove_single_corrupt(records, cfg):
    """이전(A)–현재(B)–다음(C) 비교로 단일 손상 record를 제거한다.

    반환: (kept, removed, ambiguous)
      kept: (Record, boot_start_after_corrupt 여부) 목록
      removed: 제거한 record 정보 목록
      ambiguous: 손상 후보였지만 연속이라 자동 제거하지 않은 record 정보 목록

    첫 record와 마지막 record는 이웃이 없으므로 자동 제거하지 않는다.
    "사이에 있다"는 두 경계보다 엄격하게 크고 작은 경우만 뜻한다.
    """
    n = len(records)
    marks = {}
    for i in range(1, n - 1):
        a, b, c = records[i - 1], records[i], records[i + 1]
        # 규칙 1: B를 건너뛴 A→C가 TX 흐름을 복구해야 한다.
        if c.tx_seq <= a.tx_seq:
            continue
        tx_between = a.tx_seq < b.tx_seq < c.tx_seq
        if c.seq > a.seq:
            # 규칙 2: 같은 RX boot 구간 — 두 순번이 모두 A와 C 사이여야 정상.
            seq_between = a.seq < b.seq < c.seq
            if not (tx_between and seq_between):
                marks[i] = {"boot": False, "reason": "같은 boot 구간의 단일 손상"}
        elif c.seq <= cfg.reboot_small_seq:
            # 규칙 3: RX 재부팅 경계의 단일 손상 — C가 새 segment의 시작.
            if not tx_between:
                marks[i] = {"boot": True, "reason": "RX 재부팅 경계의 단일 손상"}
        # 그 외(규칙 4)는 모호하므로 자동 제거하지 않는다.

    removed_idx, ambiguous_idx = set(), set()
    for i in marks:
        # 손상 후보가 연속이면 "단일" 손상이 아니므로 자동 제거하지 않는다.
        if (i - 1) in marks or (i + 1) in marks:
            ambiguous_idx.add(i)
        else:
            removed_idx.add(i)

    boot_start_idx = {i + 1 for i in removed_idx if marks[i]["boot"]}

    kept = [
        (rec, i in boot_start_idx)
        for i, rec in enumerate(records)
        if i not in removed_idx
    ]
    removed = [
        {
            "line": records[i].line_no,
            "seq": records[i].seq,
            "tx_seq": records[i].tx_seq,
            "reason": marks[i]["reason"],
        }
        for i in sorted(removed_idx)
    ]
    ambiguous = [
        {
            "line": records[i].line_no,
            "seq": records[i].seq,
            "tx_seq": records[i].tx_seq,
            "reason": "연속 손상 후보 — 자동 제거 안 함",
        }
        for i in sorted(ambiguous_idx)
    ]
    return kept, removed, ambiguous


# ---------------------------------------------------------------------------
# 3단계 — RX 재부팅 segment 분리 (설계 5.3)
# ---------------------------------------------------------------------------

def split_boot_segments(kept, cfg):
    """`seq`만 사용해 RX 재부팅 경계에서 segment를 나눈다.

    kept: remove_single_corrupt가 돌려준 (Record, boot_start 표시) 목록.
    반환: (segments, boundary_count, dropped_boundary_lines)

    일반 재부팅 경계 record는 제외하고 다음 record부터 새 segment를 시작한다.
    `boot_start_after_corrupt` 표시 record는 제외하지 않고 새 segment의 첫
    record로 사용한다. `timestamp_us`는 판정에 사용하지 않는다.
    """
    segments = []
    current = []
    pending_new = False
    boundary_count = 0
    dropped_boundary_lines = []

    for rec, boot_flag in kept:
        if boot_flag:
            if current:
                segments.append(current)
            current = [rec]
            boundary_count += 1
            pending_new = False
            continue
        if pending_new:
            current = [rec]
            pending_new = False
            continue
        if current:
            prev = current[-1]
            if rec.seq < prev.seq and (
                rec.seq <= cfg.reboot_small_seq
                or prev.seq - rec.seq >= cfg.reboot_min_drop
            ):
                segments.append(current)
                current = []
                dropped_boundary_lines.append(rec.line_no)
                boundary_count += 1
                pending_new = True
                continue
        current.append(rec)
    if current:
        segments.append(current)
    return segments, boundary_count, dropped_boundary_lines


# ---------------------------------------------------------------------------
# 4단계 — tx_seq 지속 감소 검증 (설계 5.4)
# ---------------------------------------------------------------------------

def count_tx_seq_decreases(segment):
    """segment 내부에서 tx_seq가 감소하는 지점 수 (중복 == 는 감소가 아님)."""
    return sum(
        1
        for prev, cur in zip(segment, segment[1:])
        if cur.tx_seq < prev.tx_seq
    )


# ---------------------------------------------------------------------------
# 5~7단계 — 안정 segment 조합과 교집합 선택 (설계 5.5~5.7)
# ---------------------------------------------------------------------------

def candidate_segments(segments):
    """단일 손상 제거 후 tx_seq가 계속 증가하는 segment만 조합 후보로 남긴다."""
    return [
        (index, segment)
        for index, segment in enumerate(segments)
        if count_tx_seq_decreases(segment) == 0
    ]


def choose_combination(candidates_by_rx, cfg):
    """세 RX 안정 segment 조합 중 가장 긴 유효 교집합을 고른다.

    candidates_by_rx: {rx_id: [(segment_index, segment), ...]}
    반환: 없으면 None, 있으면
      {"segments": {rx: index}, "common_start", "common_end", "common_length"}

    `min_common_length` 이상인 조합을 우선하고, 그 안에서 공통 길이가 가장 긴
    것을 고른다. 동률이면 파일에서 먼저 나온 segment 조합(제일 앞 순서)을
    선택해 결과를 재현할 수 있게 한다.
    """
    rx_ids = list(candidates_by_rx)
    if any(not candidates_by_rx[rx] for rx in rx_ids):
        return None
    best = None
    best_key = None
    for combo in product(*(candidates_by_rx[rx] for rx in rx_ids)):
        start = max(seg[0].tx_seq for _, seg in combo)
        end = min(seg[-1].tx_seq for _, seg in combo)
        length = end - start + 1
        if length < 1:
            continue
        key = (length >= cfg.min_common_length, length)
        if best_key is None or key > best_key:  # 동률은 앞 조합 유지
            best_key = key
            best = {
                "segments": {rx: idx for rx, (idx, _) in zip(rx_ids, combo)},
                "segment_records": {rx: seg for rx, (_, seg) in zip(rx_ids, combo)},
                "common_start": start,
                "common_end": end,
                "common_length": length,
            }
    return best


# ---------------------------------------------------------------------------
# 9단계 — grid와 수신 mask (설계 5.9)
# ---------------------------------------------------------------------------

def build_grid(chosen, cfg):
    """공통 tx_seq 정수 grid에 세 RX record를 배치한다.

    반환: (aligned (3,T,64) float32 — 누락은 NaN,
           present (3,T) bool,
           duplicates {rx: 중복 수})
    같은 segment 안의 중복 tx_seq는 파일 순서상 첫 record만 쓴다.
    """
    start, end = chosen["common_start"], chosen["common_end"]
    T = chosen["common_length"]
    n_rx = len(cfg.rx_order)
    aligned = np.full((n_rx, T, cfg.features_per_rx), np.nan, dtype=np.float32)
    present = np.zeros((n_rx, T), dtype=bool)
    duplicates = {}
    for r, rx in enumerate(cfg.rx_order):
        dup = 0
        for rec in chosen["segment_records"][rx]:
            if not (start <= rec.tx_seq <= end):
                continue
            idx = rec.tx_seq - start
            if present[r, idx]:
                dup += 1
                continue
            aligned[r, idx] = rec.amp
            present[r, idx] = True
        duplicates[rx] = dup
    return aligned, present, duplicates


def observed_ratios(chosen, cfg):
    """공통 범위 안에서 RX별로 실제 수신한 고유 tx_seq 비율 (설계 5.8)."""
    start, end = chosen["common_start"], chosen["common_end"]
    length = chosen["common_length"]
    ratios = {}
    for rx in cfg.rx_order:
        unique = {
            rec.tx_seq
            for rec in chosen["segment_records"][rx]
            if start <= rec.tx_seq <= end
        }
        ratios[rx] = len(unique) / length
    return ratios


# ---------------------------------------------------------------------------
# 10단계 — 짧은 내부 누락만 선형 보간 (설계 5.10)
# ---------------------------------------------------------------------------

def interpolate_short_gaps(aligned, present, cfg):
    """1~max_interp_gap frame의 내부 누락만 subcarrier별 선형 보간한다.

    aligned를 제자리에서 채우고 interpolated mask (3,T) bool을 돌려준다.
    공통 범위 가장자리의 누락(앞뒤 실제 수신이 없는 구간)은 보간하지 않는다.
    present는 원본 수신 여부를 그대로 유지한다.
    """
    interpolated = np.zeros_like(present)
    for r in range(present.shape[0]):
        received = np.flatnonzero(present[r])
        if received.size < 2:
            continue
        for left, right in zip(received, received[1:]):
            gap = right - left - 1
            if gap < 1 or gap > cfg.max_interp_gap:
                continue
            weights = (
                np.arange(1, gap + 1, dtype=np.float32) / np.float32(right - left)
            )[:, None]
            aligned[r, left + 1 : right] = (
                (1.0 - weights) * aligned[r, left] + weights * aligned[r, right]
            )
            interpolated[r, left + 1 : right] = True
    return interpolated


# ---------------------------------------------------------------------------
# 11단계 — 긴 누락이 낀 window 제외 (설계 5.11)
# ---------------------------------------------------------------------------

def select_window_starts(present, interpolated, cfg):
    """유효 window 시작 index 목록과 (전체 후보 수, 제외 수)를 돌려준다.

    세 RX 중 하나라도 보간되지 않은 누락 frame이 window 안에 있으면 제외한다.
    """
    invalid_any = np.logical_not(present | interpolated).any(axis=0)
    T = invalid_any.shape[0]
    prefix = np.concatenate(([0], np.cumsum(invalid_any)))
    starts = list(range(0, T - cfg.window + 1, cfg.stride))
    valid = [s for s in starts if prefix[s + cfg.window] - prefix[s] == 0]
    return valid, len(starts), len(starts) - len(valid)


# ---------------------------------------------------------------------------
# 세션 단위 처리 (설계 4절 전체 순서 중 1~12단계)
# ---------------------------------------------------------------------------

def _truncate(items):
    return items[:MANIFEST_DETAIL_LIMIT]


def process_session(session_dir, session_id, label_name, split, cfg):
    """한 세션의 세 RX JSONL을 정렬·검사하고 window 시작 목록까지 만든다.

    반환 dict:
      "manifest": manifest.json에 그대로 들어가는 세션 항목
      "combined": (T, 192) float32 (제외 세션은 None)
      "valid_starts": window 시작 index 목록
      "label": class 번호
    """
    label = LABEL_MAP[label_name]
    reasons = []
    rx_entries = {}
    rx_results = {}

    for rx in cfg.rx_order:
        path = session_dir / f"device_{rx}.jsonl"
        if not path.exists():
            reasons.append(f"RX {rx} 파일 없음")
            rx_entries[rx] = {"file": path.name, "exists": False}
            continue
        records, parse_errors = parse_jsonl(path, rx, cfg)
        kept, removed, ambiguous = remove_single_corrupt(records, cfg)
        segments, boundary_count, dropped_lines = split_boot_segments(kept, cfg)
        residual = sum(count_tx_seq_decreases(seg) for seg in segments)
        candidates = candidate_segments(segments)
        rx_results[rx] = {"segments": segments, "candidates": candidates}
        rx_entries[rx] = {
            "file": path.name,
            "exists": True,
            "record_count": len(records),
            "parse_error_count": len(parse_errors),
            "parse_errors": _truncate(parse_errors),
            "removed_single_corrupt_count": len(removed),
            "removed_single_corrupt": _truncate(removed),
            "ambiguous_corrupt_count": len(ambiguous),
            "ambiguous_corrupt": _truncate(ambiguous),
            "reboot_boundary_count": boundary_count,
            "dropped_boundary_lines": _truncate(dropped_lines),
            "segment_count": len(segments),
            "residual_tx_seq_decrease_count": residual,
        }

    residual_total = sum(
        entry.get("residual_tx_seq_decrease_count", 0) for entry in rx_entries.values()
    )
    if residual_total > 0:
        reasons.append("단일 손상 제거 후에도 tx_seq 감소가 지속됨")

    chosen = None
    ratios = None
    if len(rx_results) == len(cfg.rx_order):
        chosen = choose_combination(
            {rx: rx_results[rx]["candidates"] for rx in cfg.rx_order}, cfg
        )
        if chosen is None:
            reasons.append("정상 안정 segment 조합이 없음")
        else:
            if chosen["common_length"] < cfg.min_common_length:
                reasons.append(
                    f"공통 길이 {chosen['common_length']} < {cfg.min_common_length}"
                )
            ratios = observed_ratios(chosen, cfg)
            for rx, ratio in ratios.items():
                if ratio < cfg.min_observed_ratio:
                    reasons.append(
                        f"RX {rx} 관측률 {ratio:.4f} < {cfg.min_observed_ratio}"
                    )

    used = not reasons
    combined = None
    valid_starts = []
    duplicates = None
    interpolated_counts = None
    window_candidates = 0
    windows_excluded = 0
    window_start_tx_seqs = []

    if used:
        aligned, present, duplicates = build_grid(chosen, cfg)
        interpolated = interpolate_short_gaps(aligned, present, cfg)
        interpolated_counts = {
            rx: int(interpolated[r].sum()) for r, rx in enumerate(cfg.rx_order)
        }
        valid_starts, window_candidates, windows_excluded = select_window_starts(
            present, interpolated, cfg
        )
        # (3,T,64) -> (T,3,64) -> (T,192)  (설계 5.12, RX 순서 고정)
        T = aligned.shape[1]
        combined = aligned.transpose(1, 0, 2).reshape(
            T, len(cfg.rx_order) * cfg.features_per_rx
        )
        window_start_tx_seqs = [chosen["common_start"] + s for s in valid_starts]

    manifest_entry = {
        "session_id": session_id,
        "label": label_name,
        "label_id": label,
        "split": split,
        "rx": {str(rx): rx_entries.get(rx) for rx in cfg.rx_order},
        "chosen_segments": (
            {str(rx): chosen["segments"][rx] for rx in cfg.rx_order} if chosen else None
        ),
        "common_start": chosen["common_start"] if chosen else None,
        "common_end": chosen["common_end"] if chosen else None,
        "common_length": chosen["common_length"] if chosen else None,
        "observed_ratio": (
            {str(rx): ratios[rx] for rx in cfg.rx_order} if ratios else None
        ),
        "duplicate_count": (
            {str(rx): duplicates[rx] for rx in cfg.rx_order} if duplicates else None
        ),
        "interpolated_frame_count": (
            {str(rx): interpolated_counts[rx] for rx in cfg.rx_order}
            if interpolated_counts
            else None
        ),
        "window_candidate_count": window_candidates,
        "windows_excluded_by_gap": windows_excluded,
        "window_count": len(valid_starts),
        "used": used,
        "exclusion_reasons": reasons,
    }
    return {
        "session_id": session_id,
        "label": label,
        "label_name": label_name,
        "split": split,
        "manifest": manifest_entry,
        "combined": combined,
        "valid_starts": valid_starts,
        "window_start_tx_seqs": window_start_tx_seqs,
        "observed_ratio": ratios,
        "used": used,
    }


# ---------------------------------------------------------------------------
# 13~14단계 — split 저장과 train normalization (설계 5.13~5.14)
# ---------------------------------------------------------------------------

def discover_sessions(raw_dir):
    """raw_dir 아래 session_<id> 디렉터리를 {id: 경로}로 돌려준다."""
    sessions = {}
    for path in raw_dir.iterdir():
        if not path.is_dir() or not path.name.startswith("session_"):
            continue
        suffix = path.name[len("session_") :]
        if suffix.isdigit():
            sessions[int(suffix)] = path
    return sessions


def run(raw_dir, output_dir, cfg=DEFAULT_CONFIG, splits=None, dry_run=False):
    """전처리 전체 실행. manifest dict를 돌려준다."""
    raw_dir = Path(raw_dir)
    load_session_labels(raw_dir)
    output_dir = Path(output_dir)
    splits = splits if splits is not None else DEFAULT_SPLITS

    split_of = {}
    for split, ids in splits.items():
        for sid in ids:
            if sid in split_of:
                raise ValueError(f"session {sid}가 여러 split에 배정됨")
            split_of[sid] = split

    session_dirs = discover_sessions(raw_dir)
    if not session_dirs:
        raise FileNotFoundError(f"session_* 디렉터리가 없음: {raw_dir}")

    results = {}
    for sid in sorted(session_dirs):
        label_name = label_name_for_session(sid)
        result = process_session(
            session_dirs[sid], sid, label_name, split_of.get(sid), cfg
        )
        results[sid] = result
        if result["used"] and sid not in split_of:
            # split 배정 없는 세션이 품질 gate를 통과하면 조용히 버리지 않는다.
            raise RuntimeError(
                f"session {sid}가 품질 gate를 통과했지만 split 배정이 없다. "
                "설계 문서 5.13의 배정표와 DEFAULT_SPLITS를 갱신해야 한다."
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    n_features = len(cfg.rx_order) * cfg.features_per_rx

    norm_sum = np.zeros(n_features, dtype=np.float64)
    norm_sumsq = np.zeros(n_features, dtype=np.float64)
    norm_frames = 0

    split_summary = {}
    for split in SPLIT_ORDER:
        session_ids = [sid for sid in splits.get(split, []) if sid in results]
        used_sessions = [results[sid] for sid in session_ids if results[sid]["used"]]
        total_windows = sum(len(r["valid_starts"]) for r in used_sessions)
        class_counts = {name: 0 for name in LABEL_MAP}
        for r in used_sessions:
            class_counts[r["label_name"]] += len(r["valid_starts"])
        split_summary[split] = {
            "sessions": session_ids,
            "used_sessions": [r["session_id"] for r in used_sessions],
            "missing_sessions": [
                sid for sid in splits.get(split, []) if sid not in results
            ],
            "window_count": total_windows,
            "class_window_counts": class_counts,
        }
        if dry_run:
            continue

        split_dir = output_dir / split
        split_dir.mkdir(parents=True, exist_ok=True)
        y = np.empty(total_windows, dtype=np.int64)
        if total_windows > 0:
            X = np.lib.format.open_memmap(
                split_dir / "X.npy",
                mode="w+",
                dtype=np.float32,
                shape=(total_windows, cfg.window, n_features),
            )
        else:
            X = np.empty((0, cfg.window, n_features), dtype=np.float32)
            np.save(split_dir / "X.npy", X)

        position = 0
        with open(split_dir / "windows.jsonl", "w", encoding="utf-8") as meta_fp:
            for r in used_sessions:
                combined = r["combined"]
                for start, start_tx in zip(
                    r["valid_starts"], r["window_start_tx_seqs"]
                ):
                    window = combined[start : start + cfg.window]
                    if np.isnan(window).any():
                        raise RuntimeError(
                            f"session {r['session_id']} window(tx_seq {start_tx})에 "
                            "NaN이 남아 있음 — 보간·window 제외 로직 오류"
                        )
                    X[position] = window
                    y[position] = r["label"]
                    meta_fp.write(
                        json.dumps(
                            {
                                "index": position,
                                "session_id": r["session_id"],
                                "label": r["label_name"],
                                "label_id": r["label"],
                                "window_start_tx_seq": start_tx,
                                "rx_order": list(cfg.rx_order),
                                "observed_ratio": {
                                    str(rx): r["observed_ratio"][rx]
                                    for rx in cfg.rx_order
                                },
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    if split == "train":
                        w64 = window.astype(np.float64)
                        norm_sum += w64.sum(axis=0)
                        norm_sumsq += (w64 * w64).sum(axis=0)
                        norm_frames += cfg.window
                    position += 1
        if total_windows > 0:
            X.flush()
        np.save(split_dir / "y.npy", y)

    normalization = None
    if not dry_run:
        if norm_frames == 0:
            raise RuntimeError("train window가 0개라 normalization을 계산할 수 없다")
        mean = norm_sum / norm_frames
        variance = np.maximum(norm_sumsq / norm_frames - mean * mean, 0.0)
        std = np.sqrt(variance)
        std_safe = np.where(std < cfg.zero_std_epsilon, 1.0, std)
        np.savez(
            output_dir / "normalization.npz",
            mean=mean,
            std=std,
            std_safe=std_safe,
            zero_std_epsilon=cfg.zero_std_epsilon,
            zero_std_replacement=1.0,
            train_frame_count=norm_frames,
        )
        normalization = {
            "file": "normalization.npz",
            "computed_from": "train",
            "train_frame_count": norm_frames,
            "zero_std_epsilon": cfg.zero_std_epsilon,
            "zero_std_replacement": 1.0,
            "zero_std_feature_count": int((std < cfg.zero_std_epsilon).sum()),
            "mean": mean.tolist(),
            "std": std.tolist(),
        }

    manifest = {
        "generated_by": "model_train/preprocessing/preprocess_3rx.py",
        "design_doc": "model_train/docs/[전처리]-설계.md",
        "raw_dir": str(raw_dir),
        "dry_run": dry_run,
        "config": asdict(cfg),
        "label_map": LABEL_MAP,
        "rx_order": list(cfg.rx_order),
        "splits": {split: list(ids) for split, ids in splits.items()},
        "unassigned_sessions": [sid for sid in sorted(results) if sid not in split_of],
        "split_summary": split_summary,
        "normalization": normalization,
        "note_normalization": (
            "저장된 X는 raw amplitude다. 학습 시 train 통계(mean, std_safe)로 "
            "정규화해 사용한다."
        ),
        "sessions": [results[sid]["manifest"] for sid in sorted(results)],
    }
    with open(output_dir / "manifest.json", "w", encoding="utf-8") as fp:
        json.dump(manifest, fp, ensure_ascii=False, indent=2)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="3-RX CSI 전처리 (model_train/docs/[전처리]-설계.md 공식 구현)"
    )
    parser.add_argument(
        "--raw-dir",
        required=True,
        help="세션 디렉터리 모음 (예: mac_collector_output/raw/20260616)",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="출력 위치 (기본: model_train/preprocessing/output/<raw 폴더명>)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="X/y/normalization 저장 없이 manifest와 요약만 생성",
    )
    args = parser.parse_args(argv)

    raw_dir = Path(args.raw_dir)
    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        output_dir = Path(__file__).resolve().parent / "output" / raw_dir.name

    manifest = run(raw_dir, output_dir, dry_run=args.dry_run)

    print(f"raw: {raw_dir}")
    print(f"out: {output_dir}{'  (dry-run)' if args.dry_run else ''}")
    for split in SPLIT_ORDER:
        summary = manifest["split_summary"][split]
        print(
            f"  {split:<10} sessions={len(summary['used_sessions'])}"
            f" windows={summary['window_count']}"
            f" {summary['class_window_counts']}"
        )
    excluded = [s for s in manifest["sessions"] if not s["used"]]
    for s in excluded:
        print(f"  제외: session {s['session_id']} — {'; '.join(s['exclusion_reasons'])}")
    if not excluded:
        print("  제외된 세션 없음")
    return 0


if __name__ == "__main__":
    sys.exit(main())
