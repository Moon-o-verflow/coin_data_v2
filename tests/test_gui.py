"""GUI (PRD 10.8, FR-8.8).

화면 없이 확인할 수 있는 부분을 자동 시험한다: 붙여넣기 해석, `plan_id` 부여, 작업 스레드, 명령 처리부.
화면 연기 시험은 tkinter와 디스플레이가 있을 때만 한다.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from coindata.cli import EXIT_CANNOT_RUN, main, service
from coindata.gui.paste import PasteError, assign_plan_ids, extract_json, prepare
from coindata.gui.worker import Worker
from tests.test_plans import PlanFlowTest
from tests.test_summary import SummaryTestCase


class ExtractJsonTest(unittest.TestCase):
    def test_fenced_block_inside_prose(self) -> None:
        text = '판단입니다.\n\n```json\n{"schema": "plan/1"}\n```\n\n이상.'
        self.assertEqual(extract_json(text), {"schema": "plan/1"})

    def test_plain_json(self) -> None:
        self.assertEqual(extract_json('  {"a": 1}\n'), {"a": 1})

    def test_errors(self) -> None:
        for text in ("```json\n{}\n```\n```json\n{}\n```", "", "[1, 2]", "```json\n{not json}\n```"):
            with self.subTest(text=text), self.assertRaises(PasteError):
                extract_json(text)


class AssignPlanIdTest(unittest.TestCase):
    def test_skips_used_and_present_ids(self) -> None:
        data = {"plans": [{"side": "long"}, {"plan_id": "p2"}, {"plan_id": ""}, {"plan_id": None, "side": "short"}]}
        filled, assigned = assign_plan_ids(data, {"p1"})
        self.assertEqual([p["plan_id"] for p in filled["plans"]], ["p3", "p2", "p4", "p5"])
        self.assertEqual(assigned, ((0, "p3"), (2, "p4"), (3, "p5")))
        self.assertEqual(filled["plans"][3]["side"], "short")
        self.assertNotIn("plan_id", data["plans"][0])  # 입력은 바꾸지 않는다

    def test_non_list_plans_left_for_validation(self) -> None:
        self.assertEqual(assign_plan_ids({"plans": "x"}, set()), ({"plans": "x"}, ()))

    def test_prepare_requires_source_summary_id(self) -> None:
        with self.assertRaises(PasteError):
            prepare('{"schema": "plan/1", "plans": [{}]}', lambda source: set())
        prepared = prepare('{"schema": "plan/1", "source_summary_id": "S", "plans": [{}]}', lambda source: {"p1"})
        self.assertEqual(prepared.source_summary_id, "S")
        self.assertEqual(json.loads(prepared.text)["plans"][0]["plan_id"], "p2")


class WorkerTest(unittest.TestCase):
    def _wait(self, worker: Worker) -> None:
        deadline = time.monotonic() + 5
        while worker.busy and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_one_job_at_a_time_and_results(self) -> None:
        worker = Worker()
        results: list[object] = []
        messages: list[str] = []

        def slow() -> int:
            worker.message("진행")
            time.sleep(0.1)
            return 7

        self.assertTrue(worker.submit("a", slow, results.append, results.append))
        self.assertFalse(worker.submit("b", lambda: 1, results.append, results.append))
        self._wait(worker)
        worker.drain(messages.append)
        self.assertEqual((results, messages), ([7], ["진행"]))

        def fail() -> None:
            raise service.CommandError("실행 불가: x")

        worker.submit("c", fail, results.append, results.append)
        self._wait(worker)
        worker.drain(messages.append)
        self.assertIsInstance(results[-1], service.CommandError)


class PathsTest(unittest.TestCase):
    def test_frozen_executable_uses_its_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "coindata.exe"
            (Path(tmp) / "coindata.toml").write_text('[data]\ndb_path = "db/x.sqlite3"\n', encoding="utf-8")
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(exe)):
                config, paths = service.load(None)
            self.assertEqual(paths.config_path, Path(tmp).resolve() / "coindata.toml")
            self.assertEqual(paths.db_path, Path(tmp).resolve() / "db" / "x.sqlite3")
            self.assertEqual(paths.output_dir, Path(tmp).resolve() / "summaries")

    def test_gui_command_needs_runner(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["gui"]), EXIT_CANNOT_RUN)


class ServiceTest(SummaryTestCase):
    """GUI가 쓰는 명령 처리부: 검증만 하기, 번호 조회, 최근 요약."""

    def setUp(self) -> None:
        super().setUp()
        self.cfg, self.paths = service.load(self.config)

    def test_dry_run_does_not_register(self) -> None:
        _, doc = self.summary()
        source = doc["meta"]["summary_id"]
        text = json.dumps({"schema": "plan/1", "source_summary_id": source, "plans": [PlanFlowTest.LONG, PlanFlowTest.LONG]})
        outcomes = service.plan_add(self.cfg, self.paths, self.clock, text, dry_run=True)
        self.assertEqual(outcomes[0].errors, ())
        self.assertIsNotNone(outcomes[0].at_registration)
        self.assertIn("같은 plan_key", outcomes[1].errors[0])  # 같은 입력 안의 중복
        self.assertEqual(self.query("SELECT COUNT(*) FROM plan")[0][0], 0)
        self.assertEqual(service.plan_ids_in_use(self.cfg, self.paths, source), set())

        one = json.dumps({"schema": "plan/1", "source_summary_id": source, "plans": [PlanFlowTest.LONG]})
        self.assertEqual(service.plan_add(self.cfg, self.paths, self.clock, one, dry_run=False)[0].errors, ())
        self.assertEqual(service.plan_ids_in_use(self.cfg, self.paths, source), {"p1"})
        self.assertEqual([r.spec.plan_key for r in service.stored_plans(self.cfg, self.paths, True)], [f"{source}/p1"])

    def test_recent_summaries_and_text(self) -> None:
        self.summary()
        _, doc = self.summary()
        entries = service.recent_summaries(self.cfg, self.paths)
        self.assertEqual(entries[0].record.summary_id, doc["meta"]["summary_id"])
        self.assertTrue(all(e.exists for e in entries))
        compact = service.summary_text(entries[0].path, compact=True)
        self.assertEqual(compact.count("\n"), 1)
        self.assertEqual(json.loads(compact), doc)
        entries[1].path.unlink()
        self.assertFalse(service.recent_summaries(self.cfg, self.paths)[1].exists)
        overview = service.overview(self.cfg, self.paths)
        self.assertEqual(len(overview.datasets), 3)


def _display_available() -> bool:
    if importlib.util.find_spec("tkinter") is None:
        return False
    return sys.platform == "win32" or bool(os.environ.get("DISPLAY"))


@unittest.skipUnless(_display_available(), "tkinter 또는 디스플레이가 없다 (FR-8.8)")
class GuiSmokeTest(SummaryTestCase):
    def test_window_opens_and_lists(self) -> None:
        import tkinter as tk

        from coindata.gui.app import App

        _, doc = self.summary()
        config, paths = service.load(self.config)
        root = tk.Tk()
        try:
            worker = Worker()
            app = App(root, config, paths, service.default_runtime(), worker)
            deadline = time.monotonic() + 10
            while (worker.busy or not app.summaries) and time.monotonic() < deadline:
                root.update()
                time.sleep(0.02)
            self.assertEqual(app.summaries[0].record.summary_id, doc["meta"]["summary_id"])
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
