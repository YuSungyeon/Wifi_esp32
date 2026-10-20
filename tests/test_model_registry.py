"""모델 이름·설명·실시간 노출 — 체크포인트 **바깥**에 둔다.

이름을 고칠 때마다 체크포인트를 다시 쓰면 가중치를 건드릴 위험이 있다. 파일 하나에 경로를
키로 두고, 지우면 원래 이름으로 돌아간다.
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import model_registry as mr  # noqa: E402


class DefaultTest(unittest.TestCase):
    def test_unknown_model_falls_back_to_its_file_name(self):
        e = mr.entry("model_train/amp60/model_foo.pt", {}, )
        self.assertEqual(e["name"], "foo")
        self.assertEqual(e["note"], "")

    def test_new_models_appear_in_realtime(self):
        """사용자 결정(2026-10-20): 새 모델은 켜짐으로 시작한다."""
        self.assertTrue(mr.entry("model_train/amp60/model_foo.pt", {})["realtime"])

    def test_stored_values_win_over_the_file_name(self):
        store = {"model_train/amp60/model_foo.pt": {"name": "내 모델", "note": "설명",
                                                    "realtime": False}}
        e = mr.entry("model_train/amp60/model_foo.pt", store)
        self.assertEqual((e["name"], e["note"], e["realtime"]), ("내 모델", "설명", False))

    def test_partial_entry_fills_the_rest(self):
        store = {"model_train/amp60/model_foo.pt": {"note": "설명만 있다"}}
        e = mr.entry("model_train/amp60/model_foo.pt", store)
        self.assertEqual(e["name"], "foo")
        self.assertTrue(e["realtime"])


class PinnedTest(unittest.TestCase):
    """코드가 직접 가리키는 모델은 끌 수 없다 — 끄면 실시간이 안 돈다."""

    def test_default_and_gate_models_cannot_leave_realtime(self):
        for p in mr.PINNED:
            with self.subTest(p):
                store = {p: {"realtime": False}}
                self.assertTrue(mr.entry(p, store)["realtime"])
                self.assertTrue(mr.entry(p, store)["pinned"])

    def test_an_ordinary_model_is_not_pinned(self):
        self.assertFalse(mr.entry("model_train/amp60/model_foo.pt", {})["pinned"])

    def test_pinned_paths_match_the_engine(self):
        """엔진이 가리키는 파일과 어긋나면 끌 수 없어야 할 것이 꺼진다."""
        from realtime.engine import MODEL, STAGE1
        self.assertEqual(set(mr.PINNED),
                         {str(MODEL.relative_to(ROOT)), str(STAGE1.relative_to(ROOT))})


class IsolationTest(unittest.TestCase):
    """테스트가 진짜 레지스트리에 쓰면 안 된다 — 실제로 두 번 샜다.

    `def update(..., path=STORE)` 처럼 기본값을 import 시점에 묶으면
    `mock.patch.object(mr, "STORE", …)` 가 먹지 않는다.
    """

    def test_patching_the_store_actually_redirects_writes(self):
        with tempfile.TemporaryDirectory() as t:
            fake = Path(t) / "models.json"
            real = mr.STORE
            before = real.read_text(encoding="utf-8") if real.is_file() else None
            with unittest.mock.patch.object(mr, "STORE", fake):
                mr.update("model_train/amp60/model_zzz.pt", {"name": "샘"})
                mr.forget("model_train/amp60/model_zzz.pt")
            self.assertTrue(fake.is_file())
            after = real.read_text(encoding="utf-8") if real.is_file() else None
            self.assertEqual(after, before)


class StoreTest(unittest.TestCase):
    def test_missing_and_corrupt_file_start_empty(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "models.json"
            self.assertEqual(mr.load(p), {})
            p.write_text("{ not json")
            self.assertEqual(mr.load(p), {})

    def test_update_writes_only_what_changed(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "models.json"
            mr.update("model_train/amp60/model_foo.pt", {"name": "가"}, p)
            mr.update("model_train/amp60/model_foo.pt", {"note": "나"}, p)
            got = json.loads(p.read_text())["model_train/amp60/model_foo.pt"]
            self.assertEqual(got, {"name": "가", "note": "나"})

    def test_update_refuses_to_hide_a_pinned_model(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "models.json"
            with self.assertRaises(ValueError):
                mr.update(mr.PINNED[0], {"realtime": False}, p)

    def test_name_is_trimmed_and_cannot_be_blank(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "models.json"
            mr.update("model_train/amp60/model_foo.pt", {"name": "  띄어쓰기  "}, p)
            self.assertEqual(mr.load(p)["model_train/amp60/model_foo.pt"]["name"], "띄어쓰기")
            mr.update("model_train/amp60/model_foo.pt", {"name": "   "}, p)
            self.assertNotIn("name", mr.load(p)["model_train/amp60/model_foo.pt"])


if __name__ == "__main__":
    unittest.main()
