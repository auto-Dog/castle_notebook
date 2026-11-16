from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from castle_notebook.core import Store, new_cell, new_notebook
from castle_notebook.execution import Engine

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude_background.py"))]


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="castle 后台 $ '")
        self.project = Path(self.temp.name).resolve()
        self.store = Store(self.project / ".castle")
        self.engine = Engine(self.store, claude_command=FAKE, agent_timeout=15)
        self.doc = new_notebook(self.project)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def start(self, source="BG_OUTPUT_FILE"):
        cell = new_cell("order", source=source, expected_output="Markdown conclusion")
        self.doc["cells"] = [cell]
        self.store.put(self.doc)
        return self.engine.start(self.doc["id"], cell["id"])

    def wait(self, rid, field="status", value=None):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            run = self.engine.get_run(rid)
            if (run.get(field) == value if value else run.get(field) and (field != "status" or run[field] != "running")):
                return run
            time.sleep(0.02)
        self.fail(f"Run did not reach expected {field}: {self.engine.get_run(rid)}")

    def test_native_job_uses_explicit_permissions_hooks_and_canonical_file(self):
        run = self.wait(self.start()["id"])
        self.assertEqual(run["status"], "succeeded", run["error"])
        self.assertIn("--background", run["command"])
        self.assertNotIn("-p", run["command"])
        self.assertNotIn("--output-format", run["command"])
        self.assertEqual(run["provider_permission_mode"], "auto")
        self.assertEqual(run["provider_model"], "background-fixture")
        self.assertEqual(run["outputs"][0]["text"], "# Native output file")
        self.assertEqual(run["cli_output"], "Background final answer")
        events = self.engine.events(run["id"])
        self.assertIn("tool", [event["type"] for event in events])
        self.assertIn("tool_result", [event["type"] for event in events])
        session = next(event["data"] for event in events if event["type"] == "session")
        self.assertTrue(session["available"])
        self.assertEqual(session["background_id"], run["background_id"])
        settings = json.loads((self.store.run_path(run["id"]) / "claude-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["hooks"]["Stop"][0]["hooks"][0]["command"], sys.executable)
        self.assertEqual(events[-1]["type"], "finished")

    def test_other_cell_edits_are_saved_while_native_background_job_runs(self):
        running = new_cell("order", source="BG_WAIT", expected_output="Markdown")
        others = [new_cell(kind, source="Original draft", expected_output="Markdown")
                  for kind in ("code", "markdown", "order")]
        self.doc["cells"] = [running, *others]
        self.store.put(self.doc)
        run = self.engine.start(self.doc["id"], running["id"])
        self.wait(run["id"], "background_id")
        payload = self.engine.document(self.doc["id"])
        for cell in payload["cells"][1:]:
            cell.update(source=f"Updated {cell['type']} draft", context_note="New context")
        saved = self.engine.save(self.doc["id"], payload)
        self.assertEqual(saved["runtime"]["active_run_id"], run["id"])
        self.assertEqual(saved["cells"][0]["status"], "running")
        self.assertEqual(self.engine.get_run(run["id"])["input_snapshot"]["source"], "BG_WAIT")
        self.engine.cancel(run["id"])
        final = self.wait(run["id"])
        self.assertFalse(final["stale"], "Downstream edits do not invalidate the running input")
        saved = self.engine.document(self.doc["id"])
        for cell in saved["cells"][1:]:
            self.assertEqual(cell["source"], f"Updated {cell['type']} draft")
            self.assertEqual(cell["context_note"], "New context")

    def test_blocked_running_job_can_be_opened_live_and_after_restart(self):
        run = self.start("BG_BLOCKED BG_WAIT")
        active = self.wait(run["id"], "background_state", "blocked")
        cell = self.engine.document(self.doc["id"])["cells"][0]
        self.assertTrue(cell["session"]["available"])
        with patch("castle_notebook.execution.open_claude_terminal") as launch:
            self.engine.open_session(self.doc["id"], cell["id"])
        launch.assert_called_once_with([*FAKE, "attach", active["background_id"]], str(self.project))
        self.engine.cancel(run["id"])
        self.wait(run["id"])
        self.engine.close()
        self.engine = Engine(self.store, claude_command=FAKE)
        with patch("castle_notebook.execution.open_claude_terminal") as launch:
            self.engine.open_session(self.doc["id"], cell["id"])
        launch.assert_called_once_with([*FAKE, "attach", active["background_id"]], str(self.project))

    def test_explicit_stop_terminates_the_native_worker(self):
        run = self.start("BG_WAIT")
        active = self.wait(run["id"], "background_id")
        self.engine.cancel(run["id"])
        self.assertEqual(self.wait(run["id"])["status"], "cancelled")
        job = json.loads((self.project / ".castle/fake-jobs" / f"{active['background_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(job["state"], "stopped")

    def test_shutdown_preserves_session_and_restart_collects_same_run(self):
        self.engine.agent_timeout = 0.1  # Native jobs have no print-mode execution deadline.
        run = self.start("BG_WAIT")
        active = self.wait(run["id"], "session_id")
        self.engine.close()
        saved = self.store.get_run(run["id"])
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["collection_state"], "detached")
        job_path = self.project / ".castle/fake-jobs" / f"{active['background_id']}.json"
        job = json.loads(job_path.read_text(encoding="utf-8"))
        self.assertEqual(job["state"], "working")
        job["prompt"] = job["prompt"].replace("BG_WAIT", "BG_OUTPUT_FILE")
        job_path.write_text(json.dumps(job), encoding="utf-8")
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        finished = self.wait(run["id"])
        self.assertEqual(finished["status"], "succeeded", finished["error"])
        self.assertEqual(finished["background_id"], active["background_id"])
        self.assertEqual(finished["session_id"], active["session_id"])
        self.assertEqual(len(list(job_path.parent.glob("*.json"))), 1)

    def test_collection_error_does_not_stop_user_owned_session(self):
        run = self.wait(self.start("BG_WAIT BG_QUERY_FAIL")["id"])
        self.assertEqual(run["status"], "failed")
        job = json.loads((self.project / ".castle/fake-jobs" / f"{run['background_id']}.json").read_text(encoding="utf-8"))
        self.assertEqual(job["state"], "working")

    def test_denied_failed_and_missing_final_hook_are_never_success(self):
        for source, message in (("BG_DENIED", "权限被拒绝"), ("BG_FAILURE", "fixture API failure"),
                                ("BG_NO_HOOK", "最终结果"), ("BG_LAUNCH_FAIL", "Workspace not trusted")):
            with self.subTest(source=source):
                run = self.wait(self.start(source)["id"])
                self.assertEqual(run["status"], "failed")
                self.assertIn(message, run["error"])
                if source == "BG_LAUNCH_FAIL":
                    self.assertNotIn("--dangerously-skip-permissions", run["error"])
                self.doc = self.engine.document(self.doc["id"])

    def test_hook_ignores_subagent_completion(self):
        folder = self.project / "hook events"
        result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "castle_notebook/providers/claude_hook.py"), str(folder)],
                                input=json.dumps({"hook_event_name": "Stop", "agent_id": "child", "last_assistant_message": "Wrong answer"}),
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(folder.exists())

    def test_hook_preserves_utf8_chinese_stop_message(self):
        folder = self.project / "hook events"
        result = subprocess.run([sys.executable, str(Path(__file__).parents[1] / "castle_notebook/providers/claude_hook.py"), str(folder)],
                                input=json.dumps({"hook_event_name": "Stop", "last_assistant_message": "写入和读回均成功"}, ensure_ascii=False).encode("utf-8"),
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        event = json.loads(next(folder.glob("*.json")).read_text(encoding="utf-8"))
        self.assertEqual(event["last_assistant_message"], "写入和读回均成功")


if __name__ == "__main__":
    unittest.main()
