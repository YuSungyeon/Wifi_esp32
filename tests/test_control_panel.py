"""제어판 규칙 — 품질 문장·준비 줄·수집 화면·세션 묶음·탭 구성.

화면이 편해질수록 "왜 이렇게 판정됐나" 를 사람이 못 따라가게 된다. 그 경계를 여기서 고정한다.
설계 정본은 `doc/control-panel.md` 다.
"""
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from csi_session import VERDICT, quality, verdict_line  # noqa: E402
import block_order as bo  # noqa: E402
import meshsense_gui as gui  # noqa: E402

#: 등록된 RX 3대가 보이는 상태 (SINK 없이 직결)
THREE = [{"port": f"/dev/cu.usbmodem{i}", "registered": True, "device_id": i,
          "sta_mac": f"1a:00:00:00:00:{i:02x}", "board_name": f"RX{i}", "via": None}
         for i in (101, 102, 103)]
#: 저장소 상태(커밋 여부·좌표)에 테스트가 흔들리지 않게 바깥 입력을 전부 고정한다
CLEAN = {"packages": {}, "pyserial": True, "last_hz": {}, "notes": []}

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


class ReadyLineTest(unittest.TestCase):
    """수집 화면 맨 위 한 줄 — 옛 보드 탭이 여기로 들어온다 (설계 §3.1)."""

    def test_three_boards_and_pyserial_is_ok(self):
        line = gui.ready_line(boards=THREE, job_running=False, **CLEAN)
        self.assertEqual(line["level"], "ok")
        self.assertIn("RX 3대", line["text"])

    def test_no_boards_blocks_with_reason(self):
        line = gui.ready_line(boards=[], job_running=False)
        self.assertEqual(line["level"], "block")
        self.assertIn("3대", line["text"])

    def test_missing_torch_warns_but_does_not_block(self):
        """수집은 pyserial 만 있으면 된다 — torch 없는 수집용 노트북을 막지 않는다."""
        line = gui.ready_line(boards=THREE, job_running=False,
                              **{**CLEAN, "packages": {"torch": False}})
        self.assertEqual(line["level"], "warn")

    def test_missing_pyserial_blocks(self):
        line = gui.ready_line(boards=THREE, job_running=False, **{**CLEAN, "pyserial": False})
        self.assertEqual(line["level"], "block")

    def test_does_not_probe_while_job_running(self):
        """수집 중 포트 접근은 금지다 (CLAUDE.md '손대면 안 되는 것')."""
        with mock.patch.object(gui, "identify", side_effect=AssertionError("probed")), \
             mock.patch.object(gui, "list_ports", side_effect=AssertionError("probed")):
            line = gui.ready_line(boards=THREE, job_running=True)
        self.assertIn("수집 중", line["text"])

    def test_hz_is_labelled_with_its_source(self):
        """실측이 아니라 지난 세션 값이다 — 출처를 함께 적는다."""
        line = gui.ready_line(boards=THREE, job_running=False,
                              **{**CLEAN, "last_hz": {101: 99.8}})
        row = next(r for r in line["rows"] if r["key"] == "rx101")
        self.assertIn("지난 세션", row["text"])
        self.assertIn("99.8", row["text"])

    def test_unregistered_board_offers_registration(self):
        boards = THREE[:2] + [{"port": "/dev/cu.usbmodemX", "registered": False,
                               "device_id": None, "sta_mac": "1a:00:00:00:00:ff",
                               "board_name": "", "via": None}]
        line = gui.ready_line(boards=boards, job_running=False, **CLEAN)
        self.assertEqual(line["level"], "block")          # RX 3대가 아니다
        self.assertTrue(any(r["action"] == "register" for r in line["rows"]))


class CollectPayloadTest(unittest.TestCase):
    """수집 버튼이 무엇을 찍을지 말한다 (설계 §2 공통 규칙 1, §3.2)."""

    def test_collect_button_label_comes_from_the_block(self):
        self.assertEqual(gui.collect_label({"order": ["empty", "static", "motion"],
                                            "done": ["empty"]}), "static")

    def test_block_finished_asks_for_a_new_draw(self):
        self.assertIsNone(gui.collect_label({"order": ["empty"], "done": ["empty"]}))

    def test_recollect_verdict_puts_the_label_back(self):
        """§4.2 가 '같은 라벨로 다시 찍으세요' 라고 하면 화면도 같은 말을 해야 한다."""
        s = bo.advance({"order": ["empty", "static", "motion"], "done": []}, "empty", "ok")
        self.assertEqual(gui.collect_label(bo.advance(s, "empty", "recollect")), "empty")

    def test_folded_line_shows_the_values_without_opening(self):
        self.assertEqual(gui.settings_summary({"condition.placement_id": "P2",
                                               "condition.subject_id": "S1",
                                               "operator.notes": ""}),
                         "P2 · S1 · 메모 없음")

    def test_folded_line_marks_what_is_missing(self):
        self.assertEqual(gui.settings_summary({}), "배치 없음 · 피험자 없음 · 메모 없음")

    def test_folded_line_shows_that_a_note_exists_not_its_text(self):
        """접힌 줄은 한 줄이다 — 메모 본문은 펼쳐서 읽는다."""
        out = gui.settings_summary({"condition.placement_id": "P2",
                                    "condition.subject_id": "S1",
                                    "operator.notes": "피험자가 RX102 시야를 가림"})
        self.assertEqual(out, "P2 · S1 · 메모 있음")


class CollectDoneTest(unittest.TestCase):
    """수집이 끝나면 품질 판정이 블록을 진행시킨다 — 사람이 기억하지 않는다."""

    OK = [{"device_id": i, "frames": 30000, "span_s": 300.0, "crc_fail": 0,
           "boot_changes": 0, "tx_back": 0, "seq_gap": 0} for i in (101, 102, 103)]

    def _after(self, rc, devices, done=()):
        state = {"order": ["empty", "static", "motion"], "done": list(done),
                 "date": "20261018", "placement": "P2", "subject": "S1"}
        with mock.patch.object(gui.block_order, "load", return_value=state), \
             mock.patch.object(gui.block_order, "save") as save, \
             mock.patch.object(gui, "latest_session_dir",
                               return_value=Path("/x")), \
             mock.patch.object(gui, "read_manifest",
                               return_value={"label": "empty", "devices": devices}):
            note = gui.collect_done(rc)
        return (save.call_args[0][0] if save.call_args else None), note

    def test_clean_session_advances_the_block(self):
        state, note = self._after(0, self.OK)
        self.assertEqual(state["done"], ["empty"])
        self.assertIn("정상", note)

    def test_recollect_leaves_the_label_for_another_try(self):
        state, note = self._after(0, self.OK[:2])
        self.assertEqual(state["done"], [])
        self.assertIn("재수집", note)

    def test_failed_run_does_not_touch_the_block(self):
        state, note = self._after(1, self.OK)
        self.assertIsNone(state)


def _row(date, label, sid, level="ok", place="P3"):
    return {"path": f"mac_collector_output/raw/{date}/x_{label}_s{sid}", "date": date,
            "name": f"x_{label}_s{sid}", "label": label, "session_id": sid,
            "span_s": 300.0, "devices": [], "png": False, "note": "", "placement": place,
            "quality": {"level": level, "text": "" if level == "ok" else "흠"}}


ROWS = ([_row("20260920", lab, i) for i, lab in enumerate(["empty", "static", "motion"], 1)]
        + [_row("20260919", lab, i, place="P2") for i, lab in enumerate(["empty", "motion"], 4)])
LIVE_ROW = _row("20260920", "live", 61)


class SessionGroupTest(unittest.TestCase):
    """세션이 80개를 넘었다 — 평소 궁금한 건 '어느 날 몇 개' 지 세션 하나하나가 아니다."""

    def test_empty_sessions(self):
        self.assertEqual(gui.session_groups([]), [])

    def test_groups_are_newest_first_and_keep_every_session(self):
        g = gui.session_groups(ROWS)
        self.assertEqual([x["date"] for x in g], ["20260920", "20260919"])
        self.assertEqual(sum(x["count"] for x in g), len(ROWS))

    def test_live_session_is_listed_but_not_counted(self):
        """live 는 여러 상태가 섞여 클래스가 아니다 — 전체에는 세고 라벨 칩에는 안 센다."""
        g = gui.session_groups(ROWS + [LIVE_ROW])[0]
        self.assertEqual(g["live"], 1)
        self.assertNotIn("live", g["labels"])
        self.assertEqual(g["count"], sum(g["labels"].values()) + g["live"])

    def test_worst_quality_reaches_the_folded_row(self):
        rows = ROWS[:2] + [_row("20260920", "motion", 3, level="check")]
        self.assertEqual(gui.session_groups(rows)[0]["worst"], "check")
        self.assertEqual(gui.session_groups(ROWS)[0]["worst"], "ok")

    def test_excluded_session_is_listed_with_its_reason(self):
        rows = ROWS + [_row("20260919", "static", 12, place="P2")]
        g = next(x for x in gui.session_groups(rows) if x["date"] == "20260919")
        self.assertEqual(g["excluded"], ["20260919_12"])

    def test_placements_are_collected_and_deduplicated(self):
        rows = ROWS + [_row("20260920", "static", 9, place="P1")]
        self.assertEqual(gui.session_groups(rows)[0]["placements"], ["P1", "P3"])


class TrainScreenTest(unittest.TestCase):
    """학습 탭 — 고민하는 건 '어떤 데이터로' 와 '어떤 설정으로' 둘뿐이다 (설계 §5)."""

    @classmethod
    def setUpClass(cls):
        cls.state = gui.training_state()

    def test_nothing_is_selected_when_the_screen_opens(self):
        """미리 고르면 평가 날짜 기본값(학습에 안 쓴 날짜)이 갈 곳이 없어진다."""
        self.assertEqual(self.state["selected"], [])

    def test_each_date_reports_how_many_models_used_it(self):
        """'봉인' 이라고 적을 수 없다 — 네 날짜 모두 이미 학습에 쓰였다. 사실만 적는다."""
        row = next(d for d in self.state["dates"] if d["date"] == "20260920")
        self.assertGreater(row["trained_models"], 0)

    def test_presets_carry_what_the_screen_needs_to_estimate_time(self):
        """버튼의 '약 N분' 은 창 길이와 stride 로 센다 — 화면이 그 값을 알아야 한다."""
        for name, p in self.state["presets"].items():
            with self.subTest(name):
                self.assertGreater(p["window"], 0)
                self.assertGreater(p["train_stride"], 0)
                self.assertGreater(p["steps"], 0)

    def test_a_date_no_model_used_reports_zero(self):
        row = next(d for d in self.state["dates"] if d["date"] == "20260910")
        self.assertEqual(row["trained_models"], 0)


class RealtimePauseTest(unittest.TestCase):
    """`판단 정지` 는 화면만 끈다 — 수집은 계속 돈다 (설계 §6)."""

    def test_pause_stops_judgement_but_not_collection(self):
        with mock.patch.object(gui.RT_JOB, "stop") as rt, \
             mock.patch.object(gui.JOB, "stop") as job:
            gui.dispatch("/api/rt/pause", {})
        rt.assert_called_once()
        job.assert_not_called()

    def test_stop_ends_both(self):
        with mock.patch.object(gui.RT_JOB, "stop") as rt, \
             mock.patch.object(gui.JOB, "stop") as job, \
             mock.patch.object(type(gui.JOB), "running", property(lambda s: True)), \
             mock.patch.object(gui.JOB, "name", gui.LIVE_JOB):
            gui.dispatch("/api/live/stop", {})
        rt.assert_called_once()
        job.assert_called_once()

    def test_operator_pause_and_rx_loss_do_not_share_wording(self):
        """둘 다 멈춤이지만 하나는 정상이고 하나는 이상이다."""
        self.assertNotEqual(gui.pause_note(paused_by_operator=True),
                            gui.pause_note(paused_by_operator=False))
        self.assertIn("운영자", gui.pause_note(paused_by_operator=True))


class MarkTest(unittest.TestCase):
    """전환 표시는 전환 지연 측정의 유일한 정답 입력이다 — 눌렀는지 보여야 한다."""

    def test_last_mark_time_is_reported(self):
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "20261018" / "x_live_s1"
            d.mkdir(parents=True)
            (d / "realtime_status.json").write_text(_json.dumps({"state": "motion", "t": 120.0}))
            (d / "realtime_marks.jsonl").write_text(
                _json.dumps({"t": 30.0, "wall": 1.0, "note": "", "state": "empty"}) + "\n"
                + _json.dumps({"t": 95.5, "wall": 2.0, "note": "", "state": "motion"}) + "\n")
            view = gui.realtime_view(Path(t))
        self.assertEqual(view["last_mark_s"], 95.5)

    def test_no_marks_reports_none_not_a_crash(self):
        import tempfile, json as _json
        with tempfile.TemporaryDirectory() as t:
            d = Path(t) / "20261018" / "x_live_s1"
            d.mkdir(parents=True)
            (d / "realtime_status.json").write_text(_json.dumps({"state": "motion", "t": 120.0}))
            self.assertIsNone(gui.realtime_view(Path(t))["last_mark_s"])


class FileNameTest(unittest.TestCase):
    def test_marks_file_name_matches_the_engine(self):
        """제어판이 베껴 둔 이름이 엔진과 어긋나면 전환 표시가 조용히 사라진다."""
        from realtime.engine import MARKS_NAME
        self.assertEqual(gui.MARKS_NAME, MARKS_NAME)


class TabTest(unittest.TestCase):
    """탭 이름이 곧 할 일이고, 순서가 곧 작업 순서다 (설계 §2)."""

    def test_exactly_four_tabs_in_work_order(self):
        import re
        self.assertEqual(re.findall(r'data-t="(\w+)"', gui.PAGE),
                         ["collect", "sessions", "train", "realtime"])

    def test_collect_is_the_default_tab(self):
        self.assertIn('data-t="collect" class="on"', gui.PAGE)
        self.assertIn("<section id=\"collect\" class=\"on\">", gui.PAGE)
        self.assertIn("let S = null, tab = 'collect'", gui.JS)

    def test_every_server_action_is_still_reachable_from_the_screen(self):
        """탭을 지우면 그 탭에만 있던 버튼이 조용히 사라진다 — 플래시가 그랬다."""
        import re
        served = set(re.findall(r'path == "(/api/[\w/]+)"',
                                Path(gui.__file__).read_text(encoding="utf-8")))
        called = set(re.findall(r"post\('(/api/[\w/]+)'", gui.JS + gui.PAGE))
        # 화면이 아니라 다른 경로로 불리는 것들
        internal = {"/api/state", "/api/img", "/api/live/start"}
        self.assertEqual(served - called - internal, set())

    def test_every_state_variable_the_tabs_use_is_declared(self):
        """탭을 옮기다 선언 줄을 통째로 지운 적이 있다 — 학습 탭이 통째로 죽었다.
        pytest 는 JS 를 실행하지 않으므로 선언 유무만이라도 묶는다."""
        import re
        declared = set()
        for m in re.finditer(r"^(?:let|const|var)\s+(.+?);", gui.JS, re.M):
            declared |= {x.split("=")[0].strip() for x in m.group(1).split(",")}
        for name in ("trainPick", "trainEval", "trainPreset", "trainName", "trainOpen",
                     "readyOpen", "timeOpen", "setOpen", "projOpen", "openDates",
                     "toolOpen", "durS", "delayS", "rtModel", "shownPng", "tab", "sig"):
            with self.subTest(name):
                self.assertIn(name, declared)

    def test_removed_tabs_leave_no_dead_sections(self):
        for gone in ("boards", "meta", "analyze"):
            with self.subTest(gone):
                self.assertNotIn(f'id="{gone}"', gui.PAGE)
                self.assertNotIn(f"tab === '{gone}'", gui.JS)


class CliMenuTest(unittest.TestCase):
    """CLI 는 개발자용이라 번호 메뉴를 유지하고 빠진 두 단계만 넣는다 (설계 §7)."""

    def test_menu_has_session_view_and_realtime(self):
        import meshsense_cli as cli
        items = " ".join(cli._usb_menu_items())
        self.assertIn("세션 보기", items)
        self.assertIn("실시간", items)

    def test_menu_items_match_the_handlers(self):
        """항목과 분기가 어긋나면 엉뚱한 기능이 돈다 — 번호 메뉴의 고전적 실수다."""
        import inspect, re
        import meshsense_cli as cli
        src = inspect.getsource(cli._menu_usb_pipeline)
        branches = {int(m) for m in re.findall(r"idx == (\d+)", src)}
        self.assertEqual(branches, set(range(len(cli._usb_menu_items()) - 1)))


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
