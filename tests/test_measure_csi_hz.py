import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "scripts"))

from measure_csi_hz import analyze_jsonl, main, result_headers  # noqa: E402


class MeasureCsiHzTest(unittest.TestCase):
    @staticmethod
    def record(seq, timestamp_us):
        return {"seq": seq, "timestamp_us": timestamp_us}

    def analyze(self, records):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device_101.jsonl"
            with path.open("w", encoding="utf-8") as output:
                for record in records:
                    output.write(json.dumps(record) + "\n")
            return analyze_jsonl(path)

    def test_normal_100_hz(self):
        result = self.analyze([self.record(seq, seq * 10_000) for seq in range(5)])

        self.assertEqual(result["records"], 5)
        self.assertAlmostEqual(result["rx_hz"], 100.0)
        self.assertAlmostEqual(result["rx_seq_hz"], 100.0)
        self.assertAlmostEqual(result["median_dt_ms"], 10.0)
        self.assertEqual(result["gaps"], 0)

    def test_sequence_gap_changes_estimated_rate(self):
        result = self.analyze(
            [self.record(0, 0), self.record(1, 10_000), self.record(4, 40_000)]
        )

        self.assertAlmostEqual(result["rx_hz"], 50.0)
        self.assertAlmostEqual(result["rx_seq_hz"], 100.0)
        self.assertEqual(result["seq_gap"], 2)

    def test_only_records_after_latest_reboot_are_used(self):
        result = self.analyze(
            [
                self.record(29_958, 300_390_000),
                self.record(29_959, 300_400_000),
                self.record(0, 315_000),
                self.record(1, 325_000),
                self.record(2, 335_000),
            ]
        )

        self.assertEqual(result["resets"], 1)
        self.assertEqual(result["records"], 3)
        self.assertAlmostEqual(result["rx_hz"], 100.0)
        self.assertEqual(result["seq_gap"], 0)

    def test_duplicate_and_ordering_anomaly(self):
        duplicate = self.analyze(
            [self.record(0, 0), self.record(1, 10_000), self.record(1, 20_000)]
        )
        anomaly = self.analyze([self.record(10, 10_000), self.record(9, 20_000)])

        self.assertEqual(duplicate["duplicates"], 1)
        self.assertEqual(anomaly["anomalies"], 1)

    def test_single_record_rate_is_unavailable(self):
        result = self.analyze([self.record(0, 0)])

        self.assertIsNone(result["rx_hz"])
        self.assertIsNone(result["rx_seq_hz"])
        self.assertIsNone(result["median_dt_ms"])

    def test_table_headers_match_current_output(self):
        headers = result_headers()

        self.assertIn("RX_HZ", headers)
        self.assertIn("RX_SEQ_HZ", headers)
        self.assertIn("SEQ_GAP", headers)
        self.assertNotIn("DURATION_S", headers)

    def test_cli_prints_current_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "device_101.jsonl"
            with path.open("w", encoding="utf-8") as output:
                output.write(json.dumps(self.record(0, 0)) + "\n")
                output.write(json.dumps(self.record(1, 10_000)) + "\n")
            captured = io.StringIO()
            with patch.object(sys, "argv", ["measure_csi_hz.py", directory]), redirect_stdout(
                captured
            ):
                exit_code = main()

        text = captured.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("large gap: >200ms", text)
        self.assertIn("RX_SEQ_HZ", text)
        self.assertIn("SEQ_GAP", text)
        self.assertNotIn("DURATION_S", text)


if __name__ == "__main__":
    unittest.main()
