import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from csi_session import LIVE_LABEL, next_session_id, session_dir_name


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


class LiveLabelTest(unittest.TestCase):
    def test_live_sessions_can_be_named_but_are_not_a_training_class(self):
        from csi_store import LABELS
        self.assertIn("_live_s7", session_dir_name(LIVE_LABEL, 7))
        self.assertNotIn(LIVE_LABEL, LABELS)                 # 전처리 LABEL_MAP 과 같아야 한다
        with self.assertRaises(ValueError):
            session_dir_name("walking", 7)


if __name__ == "__main__":
    unittest.main()
