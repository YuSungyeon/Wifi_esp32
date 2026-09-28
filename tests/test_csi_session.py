import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from csi_session import next_session_id


class SessionNumberingTest(unittest.TestCase):
    def test_legacy_folders_do_not_advance_new_session_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = Path(tmp) / "raw"
            legacy = raw / "20260616" / "session_30"
            legacy.mkdir(parents=True)
            (legacy / "device_101.jsonl").touch()

            self.assertEqual(next_session_id(Path(tmp)), 1)

            current = raw / "20260919" / "150000_empty_s1"
            current.mkdir(parents=True)
            (current / "session.json").write_text("{}", encoding="utf-8")

            self.assertEqual(next_session_id(Path(tmp)), 2)


if __name__ == "__main__":
    unittest.main()
