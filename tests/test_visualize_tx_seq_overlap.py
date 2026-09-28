import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from visualize_tx_seq_overlap import (  # noqa: E402
    RecordMeta,
    _trace_display_data,
    generate_overlap_visualization,
    load_session_traces,
    split_rx_segments,
)


class VisualizeTxSeqOverlapTest(unittest.TestCase):
    @staticmethod
    def record(seq: int, tx_seq: int, wall_us: int) -> RecordMeta:
        return RecordMeta(
            seq=seq,
            timestamp_us=seq * 10_000,
            tx_seq=tx_seq,
            received_at_unix_us=wall_us,
        )

    def trace(self, device_id: int, tx_values: list[int], wall_start: int = 1_000):
        records = [
            self.record(index, tx_seq, wall_start + index * 10)
            for index, tx_seq in enumerate(tx_values)
        ]
        return split_rx_segments(device_id, records)

    def test_tx_seq_decrease_creates_a_new_epoch(self):
        trace = self.trace(101, [100, 101, 0, 1])

        self.assertEqual(trace.tx_resets, 1)
        self.assertEqual(len(trace.segments), 2)
        self.assertEqual([record.tx_seq for record in trace.segments[0].records], [100, 101])
        self.assertEqual([record.tx_seq for record in trace.segments[1].records], [0, 1])

    def test_rx_reboot_boundary_record_is_excluded(self):
        records = [
            self.record(2_998, 100, 1_000),
            self.record(2_999, 101, 1_010),
            self.record(0, 102, 1_020),
            self.record(1, 103, 1_030),
        ]

        trace = split_rx_segments(101, records)

        self.assertEqual(trace.rx_resets, 1)
        self.assertEqual(len(trace.segments), 2)
        self.assertEqual([record.tx_seq for record in trace.segments[0].records], [100, 101])
        self.assertEqual([record.tx_seq for record in trace.segments[1].records], [103])

    def test_each_rx_range_is_kept_independently(self):
        traces = {
            101: self.trace(101, [200, 201, 202, 203, 204, 205]),
            102: self.trace(102, [201, 202, 204, 205]),
            103: self.trace(103, [199, 200, 201, 202, 203, 204]),
        }

        rgba, global_start, global_end, ranges = _trace_display_data(
            traces,
            (101, 102, 103),
            max_columns=100,
            max_grid_length=100,
        )

        self.assertEqual((global_start, global_end), (199, 205))
        self.assertEqual(ranges, [(200, 205), (201, 205), (199, 204)])
        np.testing.assert_array_equal(rgba[1, 203 - global_start, :3], [1.0, 1.0, 1.0])
        self.assertFalse(np.allclose(rgba[1, 204 - global_start, :3], [1.0, 1.0, 1.0]))

    def test_duplicate_tx_seq_keeps_first_record(self):
        trace = self.trace(101, [10, 11, 11, 12])

        self.assertEqual(len(trace.segments), 1)
        self.assertEqual(trace.segments[0].duplicate_count, 1)
        self.assertEqual([record.tx_seq for record in trace.segments[0].records], [10, 11, 12])

    def test_missing_rx_file_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory)
            (session_dir / "device_101.jsonl").write_text("", encoding="utf-8")

            with self.assertRaisesRegex(FileNotFoundError, "102, 103"):
                load_session_traces(session_dir, (101, 102, 103))

    def test_generates_png_for_a_small_session(self):
        try:
            import matplotlib  # noqa: F401
        except ImportError:
            self.skipTest("matplotlib is not installed")

        with tempfile.TemporaryDirectory() as directory:
            session_dir = Path(directory)
            for device_id, tx_values in {
                101: [10, 11, 12, 13],
                102: [11, 12, 13, 14],
                103: [9, 10, 11, 12, 13],
            }.items():
                path = session_dir / f"device_{device_id}.jsonl"
                with path.open("w", encoding="utf-8") as output:
                    for index, tx_seq in enumerate(tx_values):
                        output.write(
                            json.dumps(
                                {
                                    "device_id": device_id,
                                    "seq": index,
                                    "timestamp_us": index * 10_000,
                                    "tx_seq": tx_seq,
                                    "received_at_unix_us": 1_000_000 + index * 10_000,
                                }
                            )
                            + "\n"
                        )

            rendered, _ = generate_overlap_visualization(
                session_dir,
                out_name="overlap.png",
            )

            self.assertEqual(rendered, (session_dir / "overlap.png").resolve())
            self.assertTrue(rendered.is_file())
            self.assertGreater(rendered.stat().st_size, 0)

    def test_output_name_cannot_escape_session_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "without directory components"):
                generate_overlap_visualization(
                    Path(directory),
                    out_name="../outside.png",
                )


if __name__ == "__main__":
    unittest.main()
