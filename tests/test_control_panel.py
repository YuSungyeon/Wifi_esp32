"""제어판 규칙 — 품질 문장·준비 줄·수집 화면·세션 묶음·탭 구성.

화면이 편해질수록 "왜 이렇게 판정됐나" 를 사람이 못 따라가게 된다. 그 경계를 여기서 고정한다.
설계 정본은 `doc/control-panel.md` 다.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from csi_session import VERDICT, quality, verdict_line  # noqa: E402

#: 3-RX 300초, 흠 없는 세션
OK = [{"device_id": i, "frames": 30000, "span_s": 300.0, "crc_fail": 0,
       "boot_changes": 0, "tx_back": 0, "seq_gap": 0} for i in (101, 102, 103)]


class QualityTest(unittest.TestCase):
    def test_clean_three_rx_session_is_ok(self):
        self.assertEqual(quality(OK)[0], "ok")

    def test_missing_keys_are_ok(self):
        """옛 매니페스트에는 seq_gap·tx_back 키가 없다 — KeyError 로 죽지도, 흠으로 세지도 않는다."""
        old = {"device_id": 101, "frames": 30000, "span_s": 300.0}
        self.assertEqual(quality([old, *OK[1:]]), ("ok", ""))

    def test_tx_reboot_demands_recollection(self):
        level, text = quality([dict(OK[0], tx_back=1), *OK[1:]])
        self.assertEqual(level, "recollect")
        self.assertIn("TX", text)

    def test_crc_failure_is_only_a_check(self):
        self.assertEqual(quality([dict(OK[0], crc_fail=3), *OK[1:]])[0], "check")

    def test_loss_over_one_percent_is_a_check(self):
        self.assertEqual(quality([dict(OK[0], seq_gap=400), *OK[1:]])[0], "check")
        self.assertEqual(quality([dict(OK[0], seq_gap=100), *OK[1:]])[0], "ok")

    def test_fewer_than_three_rx_demands_recollection(self):
        level, text = quality(OK[:2])
        self.assertEqual(level, "recollect")
        self.assertIn("2대", text)

    def test_strongest_level_wins_and_sentences_are_joined(self):
        level, text = quality([dict(OK[0], crc_fail=1), dict(OK[1], boot_changes=1), OK[2]])
        self.assertEqual(level, "recollect")
        self.assertIn(" / ", text)


class VerdictLineTest(unittest.TestCase):
    """CLI 수집 직후 한 줄 — 제어판과 같은 문장을 쓴다."""

    def test_clean_session_says_normal_and_nothing_else(self):
        self.assertEqual(verdict_line(OK), "[판정] 정상")

    def test_problem_session_names_the_level_and_the_reason(self):
        line = verdict_line([dict(OK[0], crc_fail=3), *OK[1:]])
        self.assertTrue(line.startswith("[판정] 확인 — "))
        self.assertIn("CRC 실패 3건", line)

    def test_every_level_has_a_korean_word(self):
        self.assertEqual(set(VERDICT), {"ok", "check", "recollect"})


if __name__ == "__main__":
    unittest.main()
