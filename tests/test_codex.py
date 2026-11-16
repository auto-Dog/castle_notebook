from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from castle_notebook.core import Problem, Store, editable_document, new_cell, new_notebook
from castle_notebook.execution import Engine
from castle_notebook.providers.codex import resolve_codex, sandbox_policy

FAKE_CODEX = [sys.executable, str(Path(__file__).with_name("fake_codex.py"))]
FAKE_CLAUDE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]


def wait_run(engine, rid, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = engine.get_run(rid)
        if result["status"] != "running":
            return result
        time.sleep(0.02)
    raise AssertionError(f"run {rid} did not finish")


class CodexTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="castle codex $ '")
        self.project = Path(self.temp.name).resolve()
        self.store = Store(self.project / ".castle")
        self.engine = Engine(self.store, claude_command=FAKE_CLAUDE, codex_command=FAKE_CODEX,
                             claude_launch_mode="print", provider="codex", agent_timeout=15)
        self.doc = new_notebook(self.project)
        self.store.put(self.doc)

    def tearDown(self):
        self.engine.close()
        for rid in list(self.engine.runs):
            wait_run(self.engine, rid)
        self.temp.cleanup()

    def add(self, source, output_format="markdown", provider=None):
        self.doc = self.store.get(self.doc["id"])
        cell = new_cell("order", "test step", source, "Explicit expected result")
        cell["output_format"] = output_format
        if provider:
            cell["provider"] = provider
        self.doc["cells"].append(cell)
        self.store.put(self.doc)
        return cell

    def execute(self, cell):
        return wait_run(self.engine, self.engine.start(self.doc["id"], cell["id"])["id"])

    def transform(self, cell, kind, **payload):
        revision = self.store.get(self.doc["id"])["revision"]
        return self.engine.transform_cells(self.doc["id"], cell["id"], kind, {"revision": revision, **payload})

    def test_default_provider_runs_codex_exec_with_session_and_usage(self):
        run = self.execute(self.add("检查上游结果"))
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertEqual(run["provider"], "codex")
        self.assertEqual(run["launch_mode"], "exec")
        self.assertIn("exec", run["command"])
        self.assertIn("--json", run["command"])
        self.assertIn("--skip-git-repo-check", run["command"])
        self.assertTrue(run["session_id"])
        self.assertEqual(run["provider_result"]["usage"]["output_tokens"], 40)
        self.assertEqual(run["provider_result"]["stop_reason"], "end_turn")
        self.assertIn("检查结论", run["outputs"][0]["text"])
        kinds = [event["type"] for event in self.engine.events(run["id"])]
        self.assertIn("session", kinds)
        self.assertIn("tool", kinds)
        self.assertIn("tool_result", kinds)
        self.assertTrue((self.store.run_path(run["id"]) / "codex-last-message.md").is_file())
        session = self.engine.document(self.doc["id"])["cells"][-1]["session"]
        self.assertTrue(session["available"])
        self.assertEqual(session["provider"], "codex")
        self.assertEqual(session["id"], run["session_id"])

    def test_output_file_wins_over_cli_answer_and_tracks_live_writes(self):
        for source, expected in (("FAKE_WRITE_FILE", "**第一版**"), ("FAKE_FILE_WAIT", "**第二版**")):
            with self.subTest(source=source):
                run = self.execute(self.add(source))
                self.assertEqual(run["status"], "succeeded", run.get("error"))
                self.assertIn(expected, run["outputs"][0]["text"])
                self.assertEqual(run["output_source"], "file")
                self.assertNotIn("此文本不应覆盖文件", run["outputs"][0]["text"])

    def test_json_contract_validates_file_and_cli_answers(self):
        run = self.execute(self.add("FAKE_JSON", output_format="json"))
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertEqual(json.loads(run["outputs"][0]["text"])["verified"], True)
        run = self.execute(self.add("FAKE_WRITE_JSON_FILE", output_format="json"))
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertEqual(json.loads(run["outputs"][0]["text"]), {"from_file": True})
        for source in ("FAKE_BAD_JSON", "FAKE_WRITE_BAD_JSON_FILE"):
            with self.subTest(source=source):
                run = self.execute(self.add(source, output_format="json"))
                self.assertEqual(run["status"], "failed")
                self.assertIn("JSON", run["error"])

    def test_failure_empty_and_protocol_answers_are_not_success(self):
        for source, marker in (("FAKE_FAILURE", "Codex"), ("FAKE_EMPTY", "空结论"),
                               ("FAKE_PROTOCOL", "工具调用协议"), ("FAKE_NO_RESULT", "Codex"),
                               ("FAKE_PENDING_TOOL", "尚未完成")):
            with self.subTest(source=source):
                run = self.execute(self.add(source))
                self.assertEqual(run["status"], "failed")
                self.assertIn(marker, run["error"])

    def test_cancel_and_timeout_stop_codex(self):
        run = self.engine.start(self.doc["id"], self.add("FAKE_WAIT")["id"])
        deadline = time.monotonic() + 8
        while not self.engine.runs[run["id"]].get("process") and time.monotonic() < deadline:
            time.sleep(0.02)
        self.engine.cancel(run["id"])
        self.assertEqual(wait_run(self.engine, run["id"])["status"], "cancelled")
        self.engine.agent_timeout = 1
        run = self.execute(self.add("FAKE_SESSION_WAIT"))
        self.assertEqual(run["status"], "failed")
        self.assertIn("超过", run["error"])

    def test_missing_codex_cli_blocks_only_codex_cells(self):
        self.engine.providers["codex"].command = []
        with self.assertRaises(Problem) as caught:
            self.engine.start(self.doc["id"], self.add("需要 Codex")["id"])
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("Codex", str(caught.exception))
        run = self.execute(self.add("FAKE_JSON", provider="claude"))
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertEqual(run["provider"], "claude")

    def test_cell_provider_overrides_default_and_legacy_cells_follow_default(self):
        codex_run = self.execute(self.add("默认走 Codex"))
        self.assertEqual(codex_run["provider"], "codex")
        claude_run = self.execute(self.add("FAKE_JSON", provider="claude"))
        self.assertEqual(claude_run["provider"], "claude")
        self.assertEqual(claude_run["provider_model"], "fixture-model")
        legacy = self.add("旧单元没有 provider 字段")
        doc = self.store.get(self.doc["id"])
        doc["cells"][-1].pop("provider", None)
        self.store.put(doc)
        run = self.execute(legacy)
        self.assertEqual(run["provider"], "codex")

    def test_sandbox_policy_maps_permission_modes(self):
        self.assertEqual(sandbox_policy("plan")[0], "read-only")
        self.assertEqual(sandbox_policy("default")[0], "read-only")
        self.assertEqual(sandbox_policy("auto")[0], "workspace-write")
        self.assertEqual(sandbox_policy("acceptEdits")[0], "workspace-write")
        self.assertEqual(sandbox_policy("bypassPermissions")[0], "bypass")
        self.engine.claude_permission_mode = "plan"
        run = self.execute(self.add("只读审阅"))
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        self.assertIn("read-only", run["command"])
        self.assertEqual(run["provider_permission_mode"], "read-only")
        self.engine.claude_permission_mode = "bypassPermissions"
        run = self.execute(self.add("完全访问"))
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", run["command"])

    def test_open_session_resumes_codex_thread_in_terminal(self):
        run = self.execute(self.add("建立会话"))
        with patch("castle_notebook.execution.open_claude_terminal") as launch:
            self.engine.open_session(self.doc["id"], run["cell_id"])
        command = launch.call_args[0][0]
        self.assertEqual(command, [*FAKE_CODEX, "resume", run["session_id"]])
        self.assertEqual(launch.call_args[0][1], str(self.project))

    def test_split_forks_independent_codex_sessions(self):
        source = self.add("拆分前的完整需求", provider="codex")
        original = self.execute(source)
        result = wait_run(self.engine, self.transform(source, "split")["id"], timeout=30)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(len(result["staged_run_ids"]), 2)
        session_ids = set()
        for rid in result["staged_run_ids"]:
            child = self.engine.get_run(rid)
            self.assertEqual(child["status"], "succeeded", child.get("error"))
            self.assertEqual(child["provider"], "codex")
            self.assertIn("fork", child["command"])
            self.assertIn(original["session_id"], child["command"])
            self.assertNotEqual(child["session_id"], original["session_id"])
            session_ids.add(child["session_id"])
        self.assertEqual(len(session_ids), 2)
        doc = self.engine.document(self.doc["id"])
        orders = [cell for cell in doc["cells"] if cell["type"] == "order"]
        self.assertEqual(len(orders), 2)
        for cell in orders:
            self.assertEqual(cell["provider"], "codex")
            self.assertTrue(cell["session"]["available"])

    def test_merge_uses_codex_and_preserves_conclusions(self):
        first = self.add("实现功能", provider="codex")
        second = self.add("修复问题", provider="codex")
        self.execute(first)
        self.execute(second)
        result = wait_run(self.engine, self.transform(second, "merge", other_cell_id=first["id"])["id"], timeout=30)
        self.assertEqual(result["status"], "succeeded", result.get("error"))
        self.assertEqual(result["provider"], "codex")
        doc = self.engine.document(self.doc["id"])
        orders = [cell for cell in doc["cells"] if cell["type"] == "order"]
        self.assertEqual(len(orders), 1)
        self.assertIn("检查结论", orders[0]["outputs"][0]["text"])

    def test_initialization_runs_on_default_codex(self):
        cell = self.add("FAKE_INIT", output_format="json")
        cell_update = self.store.get(self.doc["id"])
        cell_update["cells"][-1]["initializer"] = True
        self.store.put(cell_update)
        run = self.execute(cell)
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        doc = self.engine.apply_init(self.doc["id"], run["id"])
        self.assertEqual(doc["global"]["goal"], "拆解现有算法")
        self.assertGreaterEqual(len(doc["cells"]), 4)

    def test_editable_document_validates_provider_field(self):
        doc = self.store.get(self.doc["id"])
        cell = new_cell("order", "选择提供商", "内容", "契约")
        doc["cells"].append(cell)
        self.store.put(doc)
        incoming = self.store.get(self.doc["id"])
        incoming["cells"][-1]["provider"] = "codex"
        updated = editable_document(self.store.get(self.doc["id"]), incoming)
        self.assertEqual(updated["cells"][-1]["provider"], "codex")
        incoming = updated
        incoming["cells"][-1]["provider"] = "gemini"
        with self.assertRaises(Problem):
            editable_document(updated, incoming)


class CodexResolutionTests(unittest.TestCase):
    def test_resolve_codex_makes_relative_file_override_absolute(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "codex"
            executable.write_text("fixture", encoding="utf-8")
            previous = Path.cwd()
            os.chdir(folder)
            try:
                self.assertEqual(resolve_codex("codex"), [str(executable.resolve())])
            finally:
                os.chdir(previous)

    def test_resolve_codex_accepts_command_name_from_path(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "codex"
            executable.write_text("fixture", encoding="utf-8")
            with patch("castle_notebook.providers.codex.shutil.which", return_value=str(executable)):
                self.assertEqual(resolve_codex("codex"), [str(executable)])

    def test_resolve_codex_prefers_native_binary_behind_npm_shim(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shim = root / "codex.cmd"
            shim.write_text("@echo off", encoding="utf-8")
            package = root / "node_modules" / "@openai" / "codex" / "bin"
            package.mkdir(parents=True)
            binary = package / "codex.exe"
            binary.write_text("fixture", encoding="utf-8")
            with patch("castle_notebook.providers.codex.shutil.which", return_value=str(shim)):
                self.assertEqual(resolve_codex("codex"), [str(binary)])
            binary.unlink()
            entry = package / "codex.js"
            entry.write_text("fixture", encoding="utf-8")
            with patch("castle_notebook.providers.codex.shutil.which",
                       side_effect=lambda name: str(shim) if name == "codex" else "/usr/bin/node"):
                self.assertEqual(resolve_codex("codex"), ["/usr/bin/node", str(entry)])


if __name__ == "__main__":
    unittest.main()