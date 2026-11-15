"""Behavioral tests for the standalone Jupyter skill, without model calls."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid


HELPER = Path(__file__).resolve().parents[1] / ".claude/skills/castle-notebook/scripts/notebook.py"
SPEC = importlib.util.spec_from_file_location("notebook_skill", HELPER)
skill = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(skill)


def order_spec(title):
    return {"title": title, "instruction": "Inspect " + title, "expected_output": "A factual report for " + title}


class NotebookSkillTests(unittest.TestCase):
    def setUp(self):
        scratch = HELPER.parents[4] / "tmp"
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="castle-skill-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / "项目 with spaces"
        self.project.mkdir()
        self.path = self.project / "research notes.ipynb"
        plan = self.project / "plan.json"
        skill.atomic_json(plan, {"global": "# Global\n目的：检查实际项目。\n约束：只读。", "orders": [order_spec("first"), order_spec("second")]})
        result = self.command("init", project=str(self.project), plan=str(plan))
        self.ids = result["orders"]

    def command(self, name, **kwargs):
        return skill.dispatch(argparse.Namespace(command=name, notebook=str(self.path), **kwargs))

    def notebook(self):
        return skill.Notebook(self.path)

    def prepare(self, order_id=None):
        return self.command("prepare", cell=order_id or self.ids[0])

    def write_result(self, run, content="# Actual result\n已检查真实文件。", summary="Checked files; no project changes."):
        skill.atomic_text(run["output_file"], content)
        skill.atomic_text(run["summary_file"], summary)
        return self.command("complete", run=run["run_id"])

    def finish(self, run, code=0, session=None, is_error=False):
        session = session or str(uuid.uuid4())
        skill.atomic_json(run["response_file"], {"type": "result", "session_id": session, "is_error": is_error, "result": "written"})
        return self.command("finish", run=run["run_id"], exit_code=code)

    def test_native_notebook_and_required_expected_output(self):
        import nbformat

        nbformat.validate(nbformat.read(self.path, as_version=4))
        nb = self.notebook()
        self.assertEqual([skill.meta(c).get("role") for c in nb.cells], [None, "global", "order", "runner", "order", "runner"])
        before = self.path.read_bytes()
        invalid = self.project / "invalid.json"
        skill.atomic_json(invalid, {"title": "empty", "instruction": "Do something", "expected_output": " "})
        with self.assertRaisesRegex(ValueError, "expected_output"):
            self.command("add-order", spec=str(invalid))
        self.assertEqual(self.path.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.command("init", project=str(self.project), plan=None)

    def test_agent_writeback_uses_native_outputs_and_real_session(self):
        import nbformat

        run = self.prepare()
        self.assertEqual(self.write_result(run)["status"], "written")
        session = str(uuid.uuid4())
        self.assertEqual(self.finish(run, session=session)["status"], "succeeded")
        nb = self.notebook()
        output = nb.runner(self.ids[0])["outputs"][0]
        self.assertEqual(output["output_type"], "display_data")
        self.assertIn("已检查真实文件", output["data"]["text/markdown"])
        self.assertEqual(skill.meta(nb.order(self.ids[0]))["conversation"]["session_id"], session)
        command = self.command("conversation", cell=self.ids[0])
        self.assertEqual(command["argv"], ["claude", "--resume", session])
        self.assertTrue(list((nb.state / "backups").glob("*.ipynb")))
        nbformat.validate(nbformat.read(self.path, as_version=4))

    def test_context_includes_every_previous_input_output_path_and_summary(self):
        first = self.prepare()
        self.write_result(first, summary="DECISION: keep the public API stable.")
        self.finish(first)
        nb = self.notebook()
        native = skill.new_cell("code", ["print('native')\n"])
        native["outputs"] = [{"output_type": "stream", "name": "stdout", "text": ["native result\n"]}]
        nb.cells.insert(4, native)
        nb.save()
        second = self.prepare(self.ids[1])
        context = skill.read_text(second["context_file"])
        for value in (self.ids[0], first["output_file"], "DECISION: keep the public API stable.", "native result", "Inspect second", "A factual report for second", "约束：只读"):
            self.assertIn(value, context)
        self.assertNotIn(self.ids[1], context)  # Current ID lives in run metadata, not history.

    def test_successful_cli_without_writeback_is_failure(self):
        run = self.prepare()
        result = self.finish(run)
        self.assertEqual(result["status"], "failed")
        self.assertIn("writeback protocol", result["error"])
        self.assertIsNotNone(self.prepare()["run_id"])

    def test_cli_failure_preserves_output_but_not_success(self):
        run = self.prepare()
        self.write_result(run)
        self.assertEqual(self.finish(run, code=1, is_error=True)["status"], "failed")
        nb = self.notebook()
        body = nb.runner(self.ids[0])["outputs"][0]["data"]["text/markdown"]
        self.assertIn("failed", body)
        self.assertIn("Actual result", body)

    def test_saved_global_change_during_execution_marks_result_stale(self):
        run = self.prepare()
        nb = self.notebook()
        nb.global_cell()["source"] += "\n新增约束：保留数据文件。"
        nb.save()
        self.assertEqual(self.write_result(run)["status"], "stale")
        self.assertEqual(self.finish(run)["status"], "stale")
        next_run = self.prepare(self.ids[1])
        context = skill.read_text(next_run["context_file"])
        self.assertIn("not valid evidence", context)
        self.assertNotIn("Checked files; no project changes.", context)

    def test_merging_archives_dialogues_and_preserves_unrelated_cells(self):
        run = self.prepare()
        self.write_result(run)
        self.finish(run)
        nb = self.notebook()
        native = skill.new_cell("code", "original_value = 42")
        native["metadata"]["custom"] = {"keep": True}
        native["execution_count"] = 9
        nb.cells.insert(4, native)
        nb.save()
        spec_file = self.project / "merged.json"
        skill.atomic_json(spec_file, order_spec("combined"))
        result = self.command("merge", cells=list(reversed(self.ids)), spec=str(spec_file))
        nb = self.notebook()
        self.assertIn(native, nb.cells)
        self.assertEqual(len(result["orders"]), 1)
        new = nb.order(result["orders"][0])
        self.assertEqual(skill.meta(new)["conversation"], {"session_id": None, "fork_from": None})
        self.assertEqual(nb.runner(result["orders"][0])["outputs"], [])
        archived = skill.read_json(result["archive"])["cells"]
        self.assertEqual(len(archived), 4)
        self.assertTrue(any(skill.meta(c).get("conversation", {}).get("session_id") for c in archived))
        self.assertTrue(Path(run["output_file"]).exists())
        self.assertTrue(nb.input_path(new).is_file())

    def test_split_forks_two_independent_conversations_lazily(self):
        run = self.prepare()
        self.write_result(run)
        parent = str(uuid.uuid4())
        self.finish(run, session=parent)
        spec_file = self.project / "split.json"
        skill.atomic_json(spec_file, [order_spec("implement"), order_spec("verify")])
        result = self.command("split", cell=self.ids[0], spec=str(spec_file))
        self.assertEqual(result["fork_from"], parent)
        new_ids = result["orders"]
        for order_id in new_ids:
            conversation = skill.meta(self.notebook().order(order_id))["conversation"]
            self.assertEqual(conversation, {"session_id": None, "fork_from": parent})
        sessions = []
        for order_id in new_ids:
            branch = self.prepare(order_id)
            self.assertIn("--fork-session", branch["argv"])
            self.assertEqual(branch["argv"][branch["argv"].index("--resume") + 1], parent)
            self.write_result(branch)
            actual = str(uuid.uuid4())
            self.finish(branch, session=actual)
            sessions.append(skill.meta(self.notebook().order(order_id))["conversation"]["session_id"])
        self.assertNotEqual(sessions[0], sessions[1])
        self.assertNotIn(parent, sessions)

    def test_optimistic_version_check_does_not_overwrite_new_disk_content(self):
        old = self.notebook()
        current = self.notebook()
        current.global_cell()["source"] += "\nNew disk content"
        current.save()
        expected = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "changed on disk"):
            old.save()
        self.assertEqual(self.path.read_bytes(), expected)

    def test_direct_input_edits_and_external_output_updates_are_detected(self):
        run = self.prepare()
        self.write_result(run)
        self.finish(run)
        skill.atomic_text(run["output_file"], "# New external content")
        nb = self.notebook()
        context = nb.history(self.ids[1], 1400)
        self.assertIn("New external content", context)
        self.assertNotIn("Agent summary:", context)
        nb.order(self.ids[0])["source"] = skill.order_source(order_spec("revised"))
        nb.save()
        self.command("sync")
        self.assertEqual(skill.meta(self.notebook().order(self.ids[0]))["status"], "stale")

    def test_generated_ipython_runner_with_fake_claude_and_unicode_paths(self):
        self.run_fake_runner(expect_shell=True)

    def test_generated_runner_falls_back_for_shell_sensitive_paths(self):
        plan = self.project / "plan.json"
        self.project = self.project / "a ! special % path"
        self.project.mkdir()
        self.path = self.project / "special.ipynb"
        self.ids = self.command("init", project=str(self.project), plan=str(plan))["orders"]
        self.run_fake_runner(expect_shell=os.name != "nt")

    def run_fake_runner(self, expect_shell):
        from IPython.core.interactiveshell import InteractiveShell
        from traitlets.config import Config

        fake_bin = self.project / "fake bin"
        fake_bin.mkdir()
        fake_script = fake_bin / "fake_claude.py"
        fake_script.write_text('''import json, os, pathlib, re, subprocess, sys, uuid
request = sys.stdin.read()
run_path = re.search(r"^Read run metadata: (.+)$", request, re.M).group(1)
run = json.loads(pathlib.Path(run_path).read_text(encoding="utf-8"))
pathlib.Path(run["output_file"]).write_text("# REAL FAKE RESULT\\nUnicode: 结果", encoding="utf-8")
pathlib.Path(run["summary_file"]).write_text("Fake agent verified the plumbing.", encoding="utf-8")
completed = subprocess.run([sys.executable, os.environ["CASTLE_TEST_HELPER"], "complete", run["notebook"], "--run", run["run_id"]], capture_output=True)
if completed.returncode:
    sys.stderr.buffer.write(completed.stderr)
    sys.exit(completed.returncode)
print(json.dumps({"type": "result", "is_error": False, "session_id": run["session_id"] or str(uuid.uuid4()), "result": "written"}))
''', encoding="utf-8")
        if os.name == "nt":
            launcher = fake_bin / "claude.cmd"
            launcher.write_text('@echo off\n"%CASTLE_TEST_PYTHON%" "%CASTLE_TEST_FAKE%" %*\nexit /b %errorlevel%\n', encoding="utf-8")
        else:
            launcher = fake_bin / "claude"
            launcher.write_text('#!/bin/sh\nexec "' + sys.executable + '" "' + str(fake_script) + '" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        old_cwd = Path.cwd()
        config = Config()
        config.HistoryManager.hist_file = ":memory:"
        shell = InteractiveShell(config=config)
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            os.chdir(self.project)
            environment = {"PATH": str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                           "CASTLE_TEST_HELPER": str(HELPER), "PYTHONUTF8": "1",
                           "CASTLE_TEST_PYTHON": sys.executable, "CASTLE_TEST_FAKE": str(fake_script)}
            with patch.dict(os.environ, environment), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(Path(shutil.which("claude")).resolve(), launcher.resolve())
                result = shell.run_cell(skill.source(self.notebook().runner(self.ids[0])))
            self.assertTrue(result.success, stdout.getvalue() + stderr.getvalue())
            self.assertEqual(shell.user_ns["_castle_run"]["shell_flags"] is not None, expect_shell)
        finally:
            os.chdir(old_cwd)
            shell.clear_instance()
        nb = self.notebook()
        self.assertEqual(skill.meta(nb.order(self.ids[0]))["status"], "succeeded")
        self.assertIn("REAL FAKE RESULT", nb.runner(self.ids[0])["outputs"][0]["data"]["text/markdown"])


if __name__ == "__main__":
    unittest.main()
