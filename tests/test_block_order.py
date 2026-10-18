"""블록 순서 진행 상태 — 다음에 무엇을 찍을지 정하는 규칙.

이 상태가 수집 버튼의 라벨을 정한다(제어판 설계 §3.2). 브라우저에만 두면 새로고침에
사라지고 CLI 로 찍은 세션이 반영되지 않아 서버 파일 하나로 둔다.
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import block_order as bo  # noqa: E402
from csi_store import LABELS  # noqa: E402


class DrawTest(unittest.TestCase):
    def test_draw_never_repeats_the_previous_order(self):
        for _ in range(20):
            prev = ["empty", "static", "motion"]
            self.assertNotEqual(bo.draw(prev), prev)

    def test_draw_uses_every_label_once(self):
        self.assertEqual(sorted(bo.draw(None)), sorted(LABELS))


class AdvanceTest(unittest.TestCase):
    def setUp(self):
        self.s = {"order": ["empty", "static", "motion"], "done": [],
                  "date": "20261018", "placement": "P2", "subject": "S1"}

    def test_next_label_follows_the_order(self):
        self.assertEqual(bo.next_label(self.s), "empty")
        self.assertEqual(bo.next_label(bo.advance(self.s, "empty", "ok")), "static")

    def test_check_counts_as_done(self):
        self.assertIn("empty", bo.advance(self.s, "empty", "check")["done"])

    def test_recollect_puts_the_cell_back(self):
        done = bo.advance(self.s, "empty", "ok")
        self.assertEqual(bo.advance(done, "empty", "recollect")["done"], [])

    def test_all_done_means_no_next_label(self):
        s = dict(self.s, done=["empty", "static", "motion"])
        self.assertIsNone(bo.next_label(s))


class RedrawTest(unittest.TestCase):
    BASE = {"order": ["empty", "static", "motion"], "done": ["empty"],
            "date": "20261018", "placement": "P2", "subject": "S1"}

    def test_same_condition_keeps_the_block(self):
        self.assertIsNone(bo.redraw_reason(self.BASE, "20261018", "P2", "S1"))

    def test_new_date_placement_or_subject_redraws_with_a_reason(self):
        for date, place, subj, word in [("20261019", "P2", "S1", "날짜"),
                                        ("20261018", "P3", "S1", "배치"),
                                        ("20261018", "P2", "S2", "피험자")]:
            with self.subTest(word):
                self.assertIn(word, bo.redraw_reason(self.BASE, date, place, subj))

    def test_block_finished_redraws(self):
        s = dict(self.BASE, done=["empty", "static", "motion"])
        self.assertIsNotNone(bo.redraw_reason(s, "20261018", "P2", "S1"))


class StoreTest(unittest.TestCase):
    def test_missing_and_corrupt_file_start_fresh(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "block_order.json"
            self.assertEqual(bo.load(p)["order"], [])
            p.write_text("{ not json")
            self.assertEqual(bo.load(p)["order"], [])

    def test_save_then_load_round_trips(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "block_order.json"
            bo.save({"order": ["motion", "empty", "static"], "done": ["motion"],
                     "date": "20261018", "placement": "P2", "subject": "S1"}, p)
            self.assertEqual(bo.load(p)["done"], ["motion"])


if __name__ == "__main__":
    unittest.main()
