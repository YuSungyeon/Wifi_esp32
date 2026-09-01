#!/usr/bin/env python3
"""MeshSense CSI 프레임 저장소 — 프레임 규격·검증·진폭 계산의 Python 단일 소스.

수집기(csi_serial_reader)·진단(measure_csi_hz)·시각화(visualize_csi)·후처리
(model_train/model/Preprocessing)가 모두 이 모듈을 통해 데이터를 읽는다.
C 쪽 정본은 esp32s3_csi_recv_poc/main/app_main.c 의 csi_frame_header_t.

세션 레이아웃::

    mac_collector_output/raw/<YYYYMMDD>/<HHMMSS>_<label>_s<session_id>/
        device_<id>.csi              # 40B 헤더 + raw I/Q 프레임을 그대로 이어붙인 바이너리
        session.json                 # 매니페스트 (라벨 SSOT)
        session_meta_snapshot.yaml   # 수집 시점 session_meta.yaml 스냅샷

`.csi` 파일은 시리얼에서 받은 바이트 그대로다 — 저장 단계에 변환이 없어 변환 버그가
낄 자리가 없고, 파서가 시리얼/파일 양쪽에 그대로 재사용된다.
"""
from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import numpy as np

# ── 프레임 규격 (v3) ────────────────────────────────────────────────────────────
FRAME_MAGIC = 0x4353          # 'CS' — LE 바이트로 b"\x53\x43"
MAGIC_BYTES = b"\x53\x43"
FRAME_VERSION = 4
HEADER_SIZE = 44

FRAME_TYPE_CSI = 0
FRAME_TYPE_IDENT = 1

#: HT20 LLTF 64 서브캐리어 × I/Q 2B. 이 값이 아닌 CSI 프레임은 설정이 다른 것이므로 거부한다.
HT20_LLTF_RAW_LEN = 128
IDENT_PAYLOAD_LEN = 32

HEADER_DTYPE = np.dtype(
    [
        ("magic", "<u2"),
        ("version", "u1"),
        ("frame_type", "u1"),
        ("total_len", "<u2"),
        ("raw_len", "<u2"),
        ("seq", "<u4"),
        ("timestamp_us", "<u8"),
        ("rssi", "i1"),
        ("channel", "u1"),
        ("noise_floor", "i1"),
        ("rate", "u1"),
        ("sig_len", "<u2"),
        ("boot_id", "<u2"),
        ("tx_seq", "<u4"),
        ("agc_gain", "u1"),
        ("fft_gain", "i1"),
        ("reserved", "<u2"),
        ("gain_comp", "<f4"),
        ("crc32", "<u4"),
    ]
)
assert HEADER_DTYPE.itemsize == HEADER_SIZE, HEADER_DTYPE.itemsize

#: 파일에 저장되는 CSI 프레임 1개 = 헤더 + HT20 raw I/Q.
CSI_FRAME_DTYPE = np.dtype([("hdr", HEADER_DTYPE), ("raw", "i1", (HT20_LLTF_RAW_LEN,))])
CSI_FRAME_SIZE = CSI_FRAME_DTYPE.itemsize  # 172

#: 유효 LLTF 데이터 톤 인덱스. 64 SC 중 0(DC)과 27~37(가드)은 상시 0이라 모델 입력에서 제외한다.
#: 실측 확인(raw/20260615/session_21): 평균 진폭이 0인 인덱스가 정확히 [0, 27..37].
LLTF_DATA_IDX = np.r_[1:27, 38:64]
N_SUB = len(LLTF_DATA_IDX)  # 52
assert N_SUB == 52

_CRC_OFFSET = HEADER_DTYPE.fields["crc32"][1]  # 40

#: 라벨 어휘는 학습 전처리(model_train/preprocessing/preprocess_3rx.py 의 LABEL_MAP)와
#: 반드시 같아야 한다. 수집이 producer, 전처리가 consumer다.
LABELS = ("empty", "static", "motion")
LABEL_MAP = {name: i for i, name in enumerate(LABELS)}


# ── 검증 ───────────────────────────────────────────────────────────────────────
@dataclass
class FrameStats:
    """프레임 스트림을 읽으며 누적하는 품질 지표."""

    frames: int = 0          # 저장된 CSI 프레임
    ident: int = 0           # IDENT 프레임
    crc_fail: int = 0        # CRC 불일치 (오탐 magic 포함)
    invalid: int = 0         # 필드 범위 위반
    resync: int = 0          # magic 재탐색 횟수
    seq_gap: int = 0         # seq 점프로 추정한 손실
    boot_changes: int = 0    # 세션 중 보드 재부팅

    def as_dict(self) -> dict:
        return {
            "frames": self.frames,
            "ident": self.ident,
            "crc_fail": self.crc_fail,
            "invalid": self.invalid,
            "resync": self.resync,
            "seq_gap": self.seq_gap,
            "boot_changes": self.boot_changes,
        }


def crc32_of(frame: bytes) -> int:
    """헤더의 crc32 필드를 0으로 둔 상태의 zlib CRC-32. 펌웨어 csi_crc32()와 동일 관례."""
    body = bytearray(frame)
    body[_CRC_OFFSET : _CRC_OFFSET + 4] = b"\x00\x00\x00\x00"
    return zlib.crc32(bytes(body)) & 0xFFFFFFFF


def header_of(frame: bytes) -> np.void:
    return np.frombuffer(frame, dtype=HEADER_DTYPE, count=1)[0]


def validate_frame(frame: bytes) -> str | None:
    """프레임 1개를 검증. 통과하면 None, 아니면 사유 문자열.

    이전 reader 는 magic + ``raw_len > 4096`` 만 봤고, 그 결과 raw CSI 안의 우연한
    0x53 0x43 을 프레임 헤더로 오인한 레코드가 실제로 저장됐다
    (channel=159, rssi=+11dBm 등). CRC 가 그 경로를 결정적으로 막는다.
    """
    if len(frame) < HEADER_SIZE:
        return "short"
    h = header_of(frame)
    if h["magic"] != FRAME_MAGIC:
        return "magic"
    if h["version"] != FRAME_VERSION:
        return f"version={int(h['version'])}"
    if int(h["total_len"]) != HEADER_SIZE + int(h["raw_len"]):
        return "total_len"
    if len(frame) != int(h["total_len"]):
        return "len"
    ftype = int(h["frame_type"])
    if ftype == FRAME_TYPE_CSI:
        if int(h["raw_len"]) != HT20_LLTF_RAW_LEN:
            return f"raw_len={int(h['raw_len'])}"
        if not 1 <= int(h["channel"]) <= 14:
            return f"channel={int(h['channel'])}"
        if not -100 <= int(h["rssi"]) <= 0:
            return f"rssi={int(h['rssi'])}"
    elif ftype == FRAME_TYPE_IDENT:
        if int(h["raw_len"]) != IDENT_PAYLOAD_LEN:
            return f"ident_len={int(h['raw_len'])}"
    else:
        return f"frame_type={ftype}"
    if crc32_of(frame) != int(h["crc32"]):
        return "crc"
    return None


def parse_ident(frame: bytes) -> tuple[str, str, dict]:
    """IDENT 프레임 → (eFuse base MAC, 펌웨어 문자열, 펌웨어 진단 카운터).

    카운터를 프레임에 실은 이유: 이 프로젝트의 RX 는 콘솔 primary 가 GPIO43 UART 라
    ESP_LOG 가 USB 로 나오지 않는다. 5초 로그만으로는 `ringbuf_drop`/`partial` 을
    호스트에서 볼 방법이 없었다.
    """
    payload = frame[HEADER_SIZE:]
    mac = ":".join(f"{b:02X}" for b in payload[:6])
    fw = payload[6:16].split(b"\x00", 1)[0].decode("ascii", "replace")
    # 3번째 값은 모드에 따라 의미가 다르다: USB=uart_sent, 업링크=uplink_ok.
    # 마지막도 USB=uart_partial, 업링크=uplink_fail.
    keys = ("fw_csi_cb", "fw_sent", "fw_ringbuf_drop", "fw_send_fail")
    counters = dict(zip(keys, np.frombuffer(payload[16:32], dtype="<u4").tolist()))
    return mac, fw, counters


# ── 스트림 파싱 ────────────────────────────────────────────────────────────────
class FrameSplitter:
    """바이트 스트림에서 검증된 프레임을 뽑아내는 재동기화 파서.

    시리얼 reader 와 파일 로더가 같은 로직을 쓴다. 정상 스트림에서는 버퍼 앞이 곧
    프레임 시작이라 magic 탐색 비용이 들지 않고, 깨진 구간에서만 재동기화한다.
    """

    def __init__(self, stats: FrameStats | None = None) -> None:
        self.buf = bytearray()
        self.stats = stats if stats is not None else FrameStats()

    def feed(self, chunk: bytes) -> Iterator[bytes]:
        self.buf.extend(chunk)
        while True:
            if len(self.buf) < HEADER_SIZE:
                return
            if bytes(self.buf[:2]) != MAGIC_BYTES:
                nxt = self.buf.find(MAGIC_BYTES, 1)
                if nxt < 0:
                    # magic 후보가 없다 — 마지막 1바이트만 남겨 경계 걸침을 보존
                    del self.buf[: max(0, len(self.buf) - 1)]
                    return
                del self.buf[:nxt]
                self.stats.resync += 1
                continue

            h = header_of(bytes(self.buf[:HEADER_SIZE]))
            total = int(h["total_len"])
            if not HEADER_SIZE <= total <= HEADER_SIZE + 512:
                del self.buf[:2]
                self.stats.resync += 1
                continue
            if len(self.buf) < total:
                return

            frame = bytes(self.buf[:total])
            why = validate_frame(frame)
            if why is None:
                del self.buf[:total]
                if int(h["frame_type"]) == FRAME_TYPE_IDENT:
                    self.stats.ident += 1
                else:
                    self.stats.frames += 1
                yield frame
            else:
                # 오탐 magic — 2바이트만 버리고 다시 찾는다
                del self.buf[:2]
                if why == "crc":
                    self.stats.crc_fail += 1
                else:
                    self.stats.invalid += 1
                self.stats.resync += 1


# ── 파일 I/O ───────────────────────────────────────────────────────────────────
def read_device_file(path: Path) -> np.ndarray:
    """`device_<id>.csi` → CSI_FRAME_DTYPE 배열.

    파일은 CSI 프레임만 담는다 (reader 가 IDENT 를 저장하지 않는다). 길이가 프레임
    크기의 배수가 아니면 마지막 잘린 프레임을 버린다 — 수집 중 강제 종료된 경우다.
    """
    buf = path.read_bytes()
    n = len(buf) // CSI_FRAME_SIZE
    if n == 0:
        return np.empty(0, dtype=CSI_FRAME_DTYPE)
    return np.frombuffer(buf, dtype=CSI_FRAME_DTYPE, count=n)


def device_id_from_path(path: Path) -> int:
    return int(path.stem.split("_", 1)[1])


def read_manifest(session_dir: Path) -> dict:
    p = Path(session_dir) / "session.json"
    if not p.is_file():
        raise FileNotFoundError(
            f"매니페스트 없음: {p}\n"
            "  구 JSONL 세션이면 라벨 정보가 없어 학습에 쓸 수 없습니다 — 재수집이 필요합니다."
        )
    return json.loads(p.read_text(encoding="utf-8"))


def read_session(session_dir: Path) -> dict[int, np.ndarray]:
    """세션 디렉터리 → {device_id: 프레임 배열}."""
    session_dir = Path(session_dir)
    out: dict[int, np.ndarray] = {}
    for p in sorted(session_dir.glob("device_*.csi")):
        out[device_id_from_path(p)] = read_device_file(p)
    if not out:
        raise FileNotFoundError(f"device_*.csi 없음: {session_dir}")
    return out


def find_sessions(raw_root: Path) -> list[Path]:
    """`raw/<YYYYMMDD>/<세션>/` 중 session.json 을 가진 것만, 이름 순으로."""
    raw_root = Path(raw_root)
    return sorted(p.parent for p in raw_root.glob("*/*/session.json"))


# ── 신호 변환 ──────────────────────────────────────────────────────────────────
def amplitude(frames: np.ndarray, *, valid_only: bool = True, compensate: bool = False) -> np.ndarray:
    """프레임 배열 → 진폭 (T, 52) 또는 (T, 64).

    raw 는 int8 I/Q 교차. 위상이 필요하면 :func:`complex_csi` 를 쓴다 — 저장 단계에서
    버리지 않으므로 나중에 살릴 수 있다.

    `compensate=True` 면 헤더의 gain 보정 배율을 곱한다. **기본은 끔** — 근거는
    :func:`gain_compensation` 의 실측 기록. 보정값 자체는 헤더에 늘 저장되므로 나중에
    다른 조건(수신기 이동, RSSI 급변)에서 재검토할 수 있다.
    """
    raw = frames["raw"].astype(np.int16)
    i = raw[:, 0::2].astype(np.float64)
    q = raw[:, 1::2].astype(np.float64)
    amp = np.sqrt(i * i + q * q)
    if compensate:
        amp = amp * gain_compensation(frames)[:, None]
    return amp[:, LLTF_DATA_IDX] if valid_only else amp


def complex_csi(frames: np.ndarray, *, valid_only: bool = True) -> np.ndarray:
    """프레임 배열 → 복소 CSI (T, 52). 위상 기반 확장용."""
    raw = frames["raw"].astype(np.int16)
    z = raw[:, 0::2].astype(np.float64) + 1j * raw[:, 1::2].astype(np.float64)
    return z[:, LLTF_DATA_IDX] if valid_only else z


def gain_compensation(frames: np.ndarray) -> np.ndarray:
    """프레임별 진폭 gain 보정 배율 (T,). 값이 없는 구간은 1.0.

    배율은 **보드가 esp_csi_gain_ctrl 로 계산해 헤더에 실어준 값**이다. 호스트에서 재현할 수
    없어서 그렇게 한다 — 이 컴포넌트는 소스 없이 정적 라이브러리(.a)로만 배포된다.

    직접 만든 근사식(`2^-((fft-fft0)+(agc-agc0)/4)`)을 써 봤다가 실보드 데이터에서
    시간축 std 가 0.63 → 8.36 으로 오히려 13배 악화되어 폐기했다 (2026-08-25).

    `gain_comp == 0` 은 부팅 직후 baseline 수집 구간(첫 100패킷 ≈ 1초)이라는 뜻이다.

    **기본적으로 적용하지 않는다.** 실보드 측정(RX103, 45초, 정지 장면, 2026-08-26):

    ========================================  ========  ========
    지표                                      보정 전   보정 후
    ========================================  ========  ========
    시간축 std 평균                             0.636     1.280
    변동계수(std/mean)                          0.0483    0.0966
    AGC 변화 483곳의 평균 진폭 점프              0.58      0.59
    ========================================  ========  ========

    AGC 는 45초 동안 483회 바뀌는데 그 지점의 진폭 점프가 0.58(평균 진폭 13의 4%)로
    일반 프레임간 변동과 구분되지 않는다 — 즉 **보정할 계단이 애초에 없다**
    (`manu_scale=false` 로 하드웨어가 이미 스케일을 맞추는 것으로 보인다).
    보정을 켜면 없는 계단을 만드느라 분산만 2배가 된다.
    """
    g = frames["hdr"]["gain_comp"].astype(np.float64)
    return np.where(g > 0, g, 1.0)


# ── 구 포맷 호환 ───────────────────────────────────────────────────────────────
def load_legacy_jsonl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """구 JSONL(`csi_amp` float 리스트) → (tx_seq, amp[:, 64]).

    구 세션은 라벨이 없고 같은 session_id 재수집으로 여러 런이 한 파일에 섞여 있어
    학습에 쓰면 안 된다. 비교·디버깅 용도로만 남긴다.
    """
    tx, amps = [], []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("tx_seq") is None:
                continue
            tx.append(int(rec["tx_seq"]))
            amps.append(rec["csi_amp"])
    if not tx:
        return np.empty(0, np.int64), np.empty((0, 64))
    return np.array(tx, np.int64), np.array(amps, np.float64)


if __name__ == "__main__":  # 셀프테스트: C 구조체와 크기가 어긋나면 즉시 드러난다
    print(f"HEADER_SIZE={HEADER_SIZE} CSI_FRAME_SIZE={CSI_FRAME_SIZE} N_SUB={N_SUB}")
    print(f"LLTF_DATA_IDX={LLTF_DATA_IDX.tolist()}")
    hdr = np.zeros(1, dtype=HEADER_DTYPE)
    hdr["magic"], hdr["version"] = FRAME_MAGIC, FRAME_VERSION
    hdr["frame_type"], hdr["raw_len"] = FRAME_TYPE_CSI, HT20_LLTF_RAW_LEN
    hdr["total_len"], hdr["channel"], hdr["rssi"] = HEADER_SIZE + HT20_LLTF_RAW_LEN, 11, -40
    hdr["gain_comp"] = 1.0
    frame = bytearray(hdr.tobytes() + bytes(HT20_LLTF_RAW_LEN))
    crc = crc32_of(bytes(frame))
    frame[_CRC_OFFSET : _CRC_OFFSET + 4] = int(crc).to_bytes(4, "little")
    assert validate_frame(bytes(frame)) is None, validate_frame(bytes(frame))
    sp = FrameSplitter()
    got = list(sp.feed(b"\x00\xff\x53" + bytes(frame) + b"\x53\x43"))
    assert len(got) == 1 and got[0] == bytes(frame)
    assert sp.stats.frames == 1 and sp.stats.resync == 1, sp.stats
    bad = bytearray(frame)
    bad[20] = 0x7F  # rssi=+127
    assert validate_frame(bytes(bad)) is not None
    print("selftest OK", sp.stats.as_dict())
