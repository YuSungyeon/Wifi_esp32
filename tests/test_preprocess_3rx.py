import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "model_train" / "preprocessing"))

import preprocess_3rx as pp  # noqa: E402


def rec(line_no, seq, tx_seq, amp_value=None):
    amp = None
    if amp_value is not None:
        amp = np.full(pp.DEFAULT_CONFIG.features_per_rx, amp_value, dtype=np.float32)
    return pp.Record(line_no, seq, tx_seq, amp)


def make_segment(tx_values, seq_start=0, amp_value=1.0):
    return [
        rec(i + 1, seq_start + i, tx, amp_value) for i, tx in enumerate(tx_values)
    ]


def jsonl_line(rx, seq, tx_seq, amp_base, received=0):
    return {
        "device_id": rx,
        "seq": seq,
        "timestamp_us": seq * 10_000,
        "tx_seq": tx_seq,
        "received_at_unix_us": received,
        "csi_amp": [amp_base + k * 0.01 for k in range(64)],
    }


def write_jsonl(path, lines):
    with open(path, "w", encoding="utf-8") as fp:
        for line in lines:
            fp.write((line if isinstance(line, str) else json.dumps(line)) + "\n")


def write_session(raw_dir, session_id, rx_lines):
    session_dir = raw_dir / f"session_{session_id}"
    session_dir.mkdir(parents=True, exist_ok=True)
    for rx, lines in rx_lines.items():
        write_jsonl(session_dir / f"device_{rx}.jsonl", lines)
    return session_dir


def normal_session_lines(session_id, tx_start=1000, count=200, received=0):
    return {
        rx: [
            jsonl_line(rx, seq, tx_start + seq - 1, session_id + rx, received)
            for seq in range(1, count + 1)
        ]
        for rx in (101, 102, 103)
    }


class ParseJsonlTest(unittest.TestCase):
    def test_errors_are_recorded_and_order_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device_101.jsonl"
            write_jsonl(
                path,
                [
                    jsonl_line(101, 1, 1000, 1.0),
                    "{broken json",
                    json.dumps({"device_id": 101, "seq": 2, "tx_seq": 1001}),
                    json.dumps(
                        {
                            "device_id": 102,
                            "seq": 3,
                            "timestamp_us": 0,
                            "tx_seq": 1002,
                            "csi_amp": [0.0] * 64,
                        }
                    ),
                    json.dumps(
                        {
                            "device_id": 101,
                            "seq": 4,
                            "timestamp_us": 0,
                            "tx_seq": 1003,
                            "csi_amp": [0.0] * 10,
                        }
                    ),
                    "",
                    jsonl_line(101, 5, 1004, 2.0),
                ],
            )
            records, errors = pp.parse_jsonl(path, 101, pp.DEFAULT_CONFIG)

        self.assertEqual([r.seq for r in records], [1, 5])
        self.assertEqual([r.line_no for r in records], [1, 7])
        self.assertEqual([e["line"] for e in errors], [2, 3, 4, 5, 6])
        self.assertIn("JSON 파싱 실패", errors[0]["reason"])
        self.assertIn("필수 field 누락", errors[1]["reason"])
        self.assertIn("device_id 불일치", errors[2]["reason"])
        self.assertIn("csi_amp 길이 이상", errors[3]["reason"])


class RemoveSingleCorruptTest(unittest.TestCase):
    def test_low_dip_removed_and_segment_kept(self):
        # 설계 5.2의 session 11 실측 예: 한 record만 아래로 튄 뒤 복귀
        records = [
            rec(1, 17455, 617287),
            rec(2, 17456, 617288),
            rec(3, 3288334404, 2411),
            rec(4, 17459, 617291),
            rec(5, 17460, 617292),
        ]
        kept, removed, ambiguous = pp.remove_single_corrupt(
            records, pp.DEFAULT_CONFIG
        )

        self.assertEqual([r.seq for r, _ in kept], [17455, 17456, 17459, 17460])
        self.assertEqual(len(removed), 1)
        self.assertEqual(removed[0]["line"], 3)
        self.assertEqual(ambiguous, [])
        segments, boundary_count, _ = pp.split_boot_segments(kept, pp.DEFAULT_CONFIG)
        self.assertEqual(len(segments), 1)
        self.assertEqual(boundary_count, 0)

    def test_high_spike_removed_even_if_seq_is_normal(self):
        # tx_seq만 위로 튀는 경우 (5000→999999→5001)
        records = [
            rec(1, 10, 5000),
            rec(2, 11, 999999),
            rec(3, 12, 5001),
            rec(4, 13, 5002),
        ]
        kept, removed, _ = pp.remove_single_corrupt(records, pp.DEFAULT_CONFIG)

        self.assertEqual([r.tx_seq for r, _ in kept], [5000, 5001, 5002])
        self.assertEqual(removed[0]["line"], 2)

    def test_unrecoverable_decrease_is_not_removed(self):
        # 설계 5.2: 5000→4950→4951은 가운데를 빼도 감소가 남으므로 자동 제거 금지
        records = [rec(1, 10, 5000), rec(2, 11, 4950), rec(3, 12, 4951)]
        kept, removed, _ = pp.remove_single_corrupt(records, pp.DEFAULT_CONFIG)

        self.assertEqual(len(kept), 3)
        self.assertEqual(removed, [])
        segments, _, _ = pp.split_boot_segments(kept, pp.DEFAULT_CONFIG)
        self.assertEqual(pp.count_tx_seq_decreases(segments[0]), 1)
        self.assertEqual(pp.candidate_segments(segments), [])

    def test_edge_records_are_never_removed(self):
        records = [rec(1, 999999999, 2), rec(2, 10, 5000), rec(3, 11, 5001)]
        kept, removed, _ = pp.remove_single_corrupt(records, pp.DEFAULT_CONFIG)
        self.assertEqual(len(kept), 3)
        self.assertEqual(removed, [])


class SplitBootSegmentsTest(unittest.TestCase):
    def test_normal_reboot_drops_boundary_record(self):
        records = [
            rec(1, 100, 5000),
            rec(2, 101, 5001),
            rec(3, 102, 5002),
            rec(4, 0, 5003),
            rec(5, 1, 5004),
            rec(6, 2, 5005),
        ]
        kept = [(r, False) for r in records]
        segments, boundary_count, dropped = pp.split_boot_segments(
            kept, pp.DEFAULT_CONFIG
        )

        self.assertEqual(len(segments), 2)
        self.assertEqual([r.seq for r in segments[0]], [100, 101, 102])
        self.assertEqual([r.seq for r in segments[1]], [1, 2])  # 경계 record 제외
        self.assertEqual(boundary_count, 1)
        self.assertEqual(dropped, [4])

    def test_large_seq_drop_is_also_reboot(self):
        records = [rec(1, 5000, 100), rec(2, 5001, 101), rec(3, 4000, 102), rec(4, 4001, 103)]
        kept = [(r, False) for r in records]
        segments, boundary_count, _ = pp.split_boot_segments(kept, pp.DEFAULT_CONFIG)

        self.assertEqual(len(segments), 2)
        self.assertEqual(boundary_count, 1)
        self.assertEqual([r.seq for r in segments[1]], [4001])

    def test_small_backstep_is_not_reboot(self):
        # 감소 폭 < 100 이고 seq > 10 이면 재부팅으로 보지 않는다
        records = [rec(1, 5000, 100), rec(2, 4950, 101), rec(3, 5001, 102)]
        kept = [(r, False) for r in records]
        segments, boundary_count, _ = pp.split_boot_segments(kept, pp.DEFAULT_CONFIG)

        self.assertEqual(len(segments), 1)
        self.assertEqual(boundary_count, 0)

    def test_boot_start_after_corrupt_keeps_first_record(self):
        # 재부팅 경계 record 자체가 손상된 경우: 손상 제거 후 다음 record는
        # 제외하지 않고 새 segment의 첫 record로 사용한다 (설계 5.2 규칙 3, 5.3)
        records = [
            rec(1, 100, 5000),
            rec(2, 101, 5001),
            rec(3, 4000000000, 99999999),
            rec(4, 3, 5003),
            rec(5, 4, 5004),
        ]
        kept, removed, _ = pp.remove_single_corrupt(records, pp.DEFAULT_CONFIG)
        self.assertEqual(removed[0]["line"], 3)
        self.assertTrue(kept[2][1])  # seq=3 record에 boot_start_after_corrupt 표시

        segments, boundary_count, dropped = pp.split_boot_segments(
            kept, pp.DEFAULT_CONFIG
        )
        self.assertEqual(len(segments), 2)
        self.assertEqual([r.seq for r in segments[1]], [3, 4])  # 첫 record 유지
        self.assertEqual(boundary_count, 1)
        self.assertEqual(dropped, [])


class ChooseCombinationTest(unittest.TestCase):
    def test_intersection_is_max_start_min_end(self):
        cfg = pp.PreprocessConfig(min_common_length=10)
        candidates = {
            101: pp.candidate_segments([make_segment(range(113500, 143537))]),
            102: pp.candidate_segments([make_segment(range(113480, 143537))]),
            103: pp.candidate_segments([make_segment(range(113510, 143537))]),
        }
        chosen = pp.choose_combination(candidates, cfg)

        self.assertEqual(chosen["common_start"], 113510)
        self.assertEqual(chosen["common_end"], 143536)
        self.assertEqual(chosen["common_length"], 30027)

    def test_longest_combination_wins_over_segment_number(self):
        cfg = pp.PreprocessConfig(min_common_length=10)
        candidates = {
            101: pp.candidate_segments(
                [make_segment(range(0, 9)), make_segment(range(100, 120))]
            ),
            102: pp.candidate_segments([make_segment(range(0, 120))]),
            103: pp.candidate_segments([make_segment(range(0, 120))]),
        }
        chosen = pp.choose_combination(candidates, cfg)

        self.assertEqual(chosen["segments"][101], 1)
        self.assertEqual(chosen["common_start"], 100)
        self.assertEqual(chosen["common_length"], 20)

    def test_no_overlap_returns_none(self):
        cfg = pp.PreprocessConfig(min_common_length=10)
        candidates = {
            101: pp.candidate_segments([make_segment(range(0, 10))]),
            102: pp.candidate_segments([make_segment(range(100, 110))]),
            103: pp.candidate_segments([make_segment(range(0, 10))]),
        }
        self.assertIsNone(pp.choose_combination(candidates, cfg))


class GridAndMaskTest(unittest.TestCase):
    def build_chosen(self, seg101):
        return {
            "segment_records": {
                101: seg101,
                102: make_segment(range(10, 13), amp_value=2.0),
                103: make_segment(range(10, 13), amp_value=3.0),
            },
            "common_start": 10,
            "common_end": 12,
            "common_length": 3,
        }

    def test_duplicate_tx_seq_uses_first_record(self):
        seg101 = [
            rec(1, 0, 10, 1.0),
            rec(2, 1, 11, 5.0),
            rec(3, 2, 11, 7.0),  # 중복 — 첫 record만 사용
            rec(4, 3, 12, 1.0),
        ]
        aligned, present, duplicates = pp.build_grid(
            self.build_chosen(seg101), pp.DEFAULT_CONFIG
        )

        self.assertEqual(duplicates[101], 1)
        self.assertTrue(present.all())
        self.assertAlmostEqual(float(aligned[0, 1, 0]), 5.0)

    def test_missing_tx_seq_leaves_mask_false(self):
        seg101 = [rec(1, 0, 10, 1.0), rec(2, 1, 12, 1.0)]  # tx 11 누락
        aligned, present, _ = pp.build_grid(self.build_chosen(seg101), pp.DEFAULT_CONFIG)

        self.assertFalse(present[0, 1])
        self.assertTrue(np.isnan(aligned[0, 1]).all())
        ratios = pp.observed_ratios(self.build_chosen(seg101), pp.DEFAULT_CONFIG)
        self.assertAlmostEqual(ratios[101], 2 / 3)
        self.assertAlmostEqual(ratios[102], 1.0)


class InterpolationTest(unittest.TestCase):
    def test_short_interior_gap_is_linear_and_edges_stay_invalid(self):
        cfg = pp.DEFAULT_CONFIG
        T = 20
        aligned = np.full((1, T, cfg.features_per_rx), np.nan, dtype=np.float32)
        present = np.zeros((1, T), dtype=bool)
        for i in list(range(0, 5)) + list(range(8, 13)) + [14]:
            aligned[0, i] = float(i)
            present[0, i] = True
        # index 5~7: 3-frame 내부 gap → 보간, index 13: 1-frame gap → 보간
        # index 15~19: 뒤쪽 가장자리 → 보간 금지

        interpolated = pp.interpolate_short_gaps(aligned, present, cfg)

        self.assertTrue(interpolated[0, 5:8].all())
        self.assertTrue(interpolated[0, 13])
        np.testing.assert_allclose(aligned[0, 5:8, 0], [5.0, 6.0, 7.0], rtol=1e-6)
        self.assertAlmostEqual(float(aligned[0, 13, 0]), 13.0, places=5)
        self.assertFalse(interpolated[0, 15:].any())
        self.assertTrue(np.isnan(aligned[0, 15:]).all())

    def test_gap_longer_than_limit_is_not_interpolated(self):
        cfg = pp.DEFAULT_CONFIG
        T = 10
        aligned = np.full((1, T, cfg.features_per_rx), np.nan, dtype=np.float32)
        present = np.zeros((1, T), dtype=bool)
        for i in (0, 7, 8, 9):  # index 1~6: 6-frame gap > 5
            aligned[0, i] = float(i)
            present[0, i] = True

        interpolated = pp.interpolate_short_gaps(aligned, present, cfg)

        self.assertFalse(interpolated.any())
        self.assertTrue(np.isnan(aligned[0, 1:7]).all())


class WindowSelectionTest(unittest.TestCase):
    def test_windows_overlapping_long_gap_are_excluded(self):
        cfg = pp.PreprocessConfig(window=10, stride=5)
        T = 40
        present = np.ones((3, T), dtype=bool)
        interpolated = np.zeros((3, T), dtype=bool)
        present[1, 22] = False  # RX 하나의 보간되지 않은 누락

        valid, candidates, excluded = pp.select_window_starts(
            present, interpolated, cfg
        )

        self.assertEqual(candidates, 7)  # 시작 0,5,...,30
        self.assertEqual(excluded, 2)  # 시작 15, 20 window가 index 22를 포함
        self.assertEqual(valid, [0, 5, 10, 25, 30])

    def test_interpolated_frames_do_not_exclude_windows(self):
        cfg = pp.PreprocessConfig(window=10, stride=5)
        present = np.ones((3, 20), dtype=bool)
        interpolated = np.zeros((3, 20), dtype=bool)
        present[0, 4] = False
        interpolated[0, 4] = True  # 보간된 frame은 window 제외 사유가 아님

        valid, candidates, excluded = pp.select_window_starts(
            present, interpolated, cfg
        )

        self.assertEqual(excluded, 0)
        self.assertEqual(len(valid), candidates)


class EndToEndTest(unittest.TestCase):
    CFG = pp.PreprocessConfig(
        window=50, stride=25, min_common_length=100, min_observed_ratio=0.8
    )
    SPLITS = {"train": [1], "validation": [11], "test": [21]}

    def build_raw(self, raw_dir, received=0):
        for session_id in (1, 11, 21):
            write_session(
                raw_dir, session_id, normal_session_lines(session_id, received=received)
            )

    def run_pipeline(self, base, received=0):
        raw_dir = base / f"raw_{received}"
        out_dir = base / f"out_{received}"
        raw_dir.mkdir()
        self.build_raw(raw_dir, received=received)
        manifest = pp.run(raw_dir, out_dir, cfg=self.CFG, splits=self.SPLITS)
        return manifest, out_dir

    def test_shapes_labels_metadata_and_normalization(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest, out_dir = self.run_pipeline(Path(directory))

            for split, session_id, label_id in (
                ("train", 1, 0),
                ("validation", 11, 1),
                ("test", 21, 2),
            ):
                X = np.load(out_dir / split / "X.npy")
                y = np.load(out_dir / split / "y.npy")
                self.assertEqual(X.shape, (7, 50, 192))  # (200-50)/25+1 = 7
                self.assertEqual(X.dtype, np.float32)
                self.assertEqual(y.dtype, np.int64)
                self.assertTrue((y == label_id).all())
                self.assertFalse(np.isnan(X).any())

                # feature 결합 순서: index = RX순서*64 + subcarrier (설계 5.12)
                for r, rx in enumerate((101, 102, 103)):
                    expected = np.float32(session_id + rx)  # subcarrier 0
                    self.assertAlmostEqual(
                        float(X[0, 0, r * 64]), float(expected), places=3
                    )

                with open(out_dir / split / "windows.jsonl", encoding="utf-8") as fp:
                    windows = [json.loads(line) for line in fp]
                self.assertEqual(len(windows), 7)
                self.assertEqual(windows[0]["session_id"], session_id)
                self.assertEqual(windows[0]["window_start_tx_seq"], 1000)
                self.assertEqual(windows[2]["window_start_tx_seq"], 1050)
                self.assertEqual(windows[0]["rx_order"], [101, 102, 103])

            # normalization은 train에서만 계산 (설계 5.14)
            with np.load(out_dir / "normalization.npz") as norm:
                X_train = np.load(out_dir / "train" / "X.npy")
                expected_mean = (
                    X_train.reshape(-1, 192).astype(np.float64).mean(axis=0)
                )
                np.testing.assert_allclose(norm["mean"], expected_mean, rtol=1e-6)
                # 시간에 대해 일정한 feature라 std ≈ 0 → std_safe = 1.0
                np.testing.assert_allclose(norm["std_safe"], np.ones(192))

            for entry in manifest["sessions"]:
                self.assertTrue(entry["used"])
                self.assertEqual(entry["common_length"], 200)
                self.assertEqual(entry["window_count"], 7)
            self.assertTrue((out_dir / "manifest.json").exists())

    def test_received_at_unix_us_does_not_change_results(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            manifest_a, out_a = self.run_pipeline(base, received=0)
            manifest_b, out_b = self.run_pipeline(base, received=987654321)

            for split in ("train", "validation", "test"):
                np.testing.assert_array_equal(
                    np.load(out_a / split / "X.npy"), np.load(out_b / split / "X.npy")
                )
                np.testing.assert_array_equal(
                    np.load(out_a / split / "y.npy"), np.load(out_b / split / "y.npy")
                )
            self.assertEqual(
                json.dumps(manifest_a["sessions"], sort_keys=True),
                json.dumps(manifest_b["sessions"], sort_keys=True),
            )


class SessionGateTest(unittest.TestCase):
    CFG = EndToEndTest.CFG

    def test_short_common_range_and_missing_file_exclude_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            out_dir = Path(directory) / "out"
            raw_dir.mkdir()
            write_session(raw_dir, 1, normal_session_lines(1))
            # session 22 유형: RX 102가 3 record뿐 → 공통 구간 3 frame
            lines_2 = normal_session_lines(2)
            lines_2[102] = [
                jsonl_line(102, seq, 1100 + seq - 1, 104.0) for seq in range(1, 4)
            ]
            write_session(raw_dir, 2, lines_2)
            # RX 파일 자체가 없는 세션
            lines_3 = normal_session_lines(3)
            del lines_3[103]
            write_session(raw_dir, 3, lines_3)

            manifest = pp.run(
                raw_dir, out_dir, cfg=self.CFG, splits={"train": [1]}
            )

            by_id = {s["session_id"]: s for s in manifest["sessions"]}
            self.assertTrue(by_id[1]["used"])
            self.assertFalse(by_id[2]["used"])
            self.assertEqual(by_id[2]["common_length"], 3)
            self.assertTrue(
                any("공통 길이" in r for r in by_id[2]["exclusion_reasons"])
            )
            self.assertFalse(by_id[3]["used"])
            self.assertIn("RX 103 파일 없음", by_id[3]["exclusion_reasons"])
            self.assertEqual(manifest["unassigned_sessions"], [2, 3])

    def test_usable_session_without_split_assignment_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            out_dir = Path(directory) / "out"
            raw_dir.mkdir()
            write_session(raw_dir, 1, normal_session_lines(1))

            with self.assertRaises(RuntimeError):
                pp.run(raw_dir, out_dir, cfg=self.CFG, splits={"train": []})

    def test_persistent_tx_seq_decrease_excludes_session(self):
        # TX 재부팅 형태 (설계 5.4): 감소한 값이 계속 증가 → session 제외
        with tempfile.TemporaryDirectory() as directory:
            raw_dir = Path(directory) / "raw"
            out_dir = Path(directory) / "out"
            raw_dir.mkdir()
            lines = normal_session_lines(1)
            lines[101] = [
                jsonl_line(101, seq, 50000 + seq if seq <= 100 else seq - 100, 102.0)
                for seq in range(1, 201)
            ]
            write_session(raw_dir, 1, lines)

            manifest = pp.run(
                raw_dir, out_dir, cfg=self.CFG, splits={"train": []}, dry_run=True
            )

            entry = manifest["sessions"][0]
            self.assertFalse(entry["used"])
            self.assertIn(
                "단일 손상 제거 후에도 tx_seq 감소가 지속됨",
                entry["exclusion_reasons"],
            )


class LabelContractTest(unittest.TestCase):
    def test_label_map_is_fixed(self):
        self.assertEqual(pp.LABEL_MAP, {"empty": 0, "static": 1, "motion": 2})

    def test_default_split_draft_matches_design(self):
        self.assertEqual(len(pp.DEFAULT_SPLITS["train"]), 17)
        self.assertEqual(len(pp.DEFAULT_SPLITS["validation"]), 6)
        self.assertEqual(len(pp.DEFAULT_SPLITS["test"]), 6)
        assigned = sorted(
            sid for ids in pp.DEFAULT_SPLITS.values() for sid in ids
        )
        self.assertNotIn(22, assigned)
        self.assertEqual(len(assigned), len(set(assigned)))

    def test_session_label_ranges(self):
        self.assertEqual(pp.label_name_for_session(1), "empty")
        self.assertEqual(pp.label_name_for_session(10), "empty")
        self.assertEqual(pp.label_name_for_session(11), "static")
        self.assertEqual(pp.label_name_for_session(20), "static")
        self.assertEqual(pp.label_name_for_session(21), "motion")
        self.assertEqual(pp.label_name_for_session(30), "motion")
        with self.assertRaises(ValueError):
            pp.label_name_for_session(31)


if __name__ == "__main__":
    unittest.main()
