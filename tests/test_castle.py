from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from castle_notebook.core import (MAX_CONTEXT, MAX_EMBEDDED_IMAGE_BYTES, Problem, Store,
                                 default_context_config, editable_document,
                                 embed_markdown_images, ensure_context_config, ensure_settings,
                                 load_context_config, load_settings, normalize_context_config,
                                 normalize_settings, initialization_notebook, new_cell, new_notebook,
                                 pack_context, parse_json_output, project_changes, read_project_asset,
                                 read_project_file, scan_project, settings_summary, snapshot_project,
                                 validate_order_output)
from castle_notebook.execution import (Engine, linux_terminal_command, open_claude_terminal,
                                       spawn_options)
from castle_notebook.providers.claude import resolve_claude
from castle_notebook.server import App, MathAssets, Server

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]


def wait_run(engine, rid, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = engine.get_run(rid)
        if result["status"] != "running":
            return result
        time.sleep(0.02)
    raise AssertionError(f"run {rid} did not finish")


class PlatformTests(unittest.TestCase):
    def test_resolve_claude_accepts_a_command_name_from_path(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / "claude"
            executable.write_text("fixture", encoding="utf-8")
            with patch("castle_notebook.providers.claude.shutil.which", return_value=str(executable)):
                self.assertEqual(resolve_claude("claude"), [str(executable)])

    def test_linux_terminal_command_finds_an_emulator_and_preserves_argv(self):
        command = ["/home/research/.local/bin/claude", "--resume", "session with spaces"]

        def locate(name):
            return "/usr/bin/gnome-terminal" if name == "gnome-terminal" else None

        with patch.dict("castle_notebook.execution.os.environ", {}, clear=True):
            with patch("castle_notebook.execution.shutil.which", side_effect=locate):
                launcher = linux_terminal_command(command)
        self.assertEqual(launcher, ["/usr/bin/gnome-terminal", "--", *command])

    def test_linux_terminal_command_honors_configured_terminal_options(self):
        command = ["claude", "--resume", "abc"]
        with patch.dict("castle_notebook.execution.os.environ", {"TERMINAL": "konsole --new-tab"}, clear=True):
            with patch("castle_notebook.execution.shutil.which", return_value="/usr/bin/konsole"):
                launcher = linux_terminal_command(command)
        self.assertEqual(launcher, ["/usr/bin/konsole", "--new-tab", "-e", *command])

    def test_open_claude_terminal_launches_linux_emulator_in_project(self):
        command = ["claude", "--resume", "abc"]
        launcher = ["/usr/bin/x-terminal-emulator", "-e", *command]
        with patch("castle_notebook.execution.os.name", "posix"), \
                patch("castle_notebook.execution.sys.platform", "linux"), \
                patch("castle_notebook.execution.linux_terminal_command", return_value=launcher), \
                patch("castle_notebook.execution.subprocess.Popen") as popen, \
                patch("castle_notebook.execution.threading.Thread") as thread:
            open_claude_terminal(command, "/work/project")
        popen.assert_called_once_with(launcher, cwd="/work/project", start_new_session=True)
        thread.assert_called_once_with(target=popen.return_value.wait, daemon=True)
        thread.return_value.start.assert_called_once_with()

    def test_open_claude_terminal_reports_missing_linux_emulator(self):
        with patch("castle_notebook.execution.os.name", "posix"), \
                patch("castle_notebook.execution.sys.platform", "linux"), \
                patch("castle_notebook.execution.linux_terminal_command", return_value=None):
            with self.assertRaisesRegex(Problem, "找不到可用的 Linux 终端") as error:
                open_claude_terminal(["claude", "--resume", "abc"], "/work/project")
        self.assertEqual(error.exception.status, 503)


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.doc = new_notebook(".", template="quant")

    def evidence(self):
        for cell in self.doc["cells"]:
            cell.update(status="succeeded", outputs=[{"kind": "text", "text": "EVIDENCE_" + cell["id"]}], run_ids=["a" * 12])

    def test_templates_have_timestamps_and_output_contracts(self):
        for template in ("blank", "quant", "algorithm"):
            doc = new_notebook(".", template=template)
            self.assertIn("+00:00", doc["created_at"])
            for cell in doc["cells"]:
                self.assertTrue(cell["created_at"])
                if cell["type"] == "order":
                    self.assertTrue(cell["expected_output"])

    def test_context_never_includes_future_cells(self):
        first = self.doc["cells"][1]
        self.doc["cells"][-1]["source"] = "FUTURE_SECRET"
        self.assertNotIn("FUTURE_SECRET", pack_context(self.doc, first["id"])["prompt"])

    def test_stale_and_failed_evidence_excluded(self):
        self.evidence()
        self.doc["cells"][0]["stale"] = True
        self.doc["cells"][1]["status"] = "failed"
        context = pack_context(self.doc, self.doc["cells"][-1]["id"])
        self.assertNotIn("EVIDENCE_" + self.doc["cells"][0]["id"], context["prompt"])
        self.assertNotIn("EVIDENCE_" + self.doc["cells"][1]["id"], context["prompt"])
        self.assertIn("EVIDENCE_" + self.doc["cells"][2]["id"], context["prompt"])

    def test_manual_summary_and_fingerprint_are_deterministic(self):
        self.doc["cells"][0]["context_note"] = "MANUAL SUMMARY"
        one = pack_context(self.doc, self.doc["cells"][-1]["id"])
        two = pack_context(self.doc, self.doc["cells"][-1]["id"])
        self.assertEqual(one, two)
        self.assertIn("MANUAL SUMMARY", one["prompt"])
        self.assertEqual(one["fingerprint"], hashlib.sha256(one["prompt"].encode()).hexdigest())

    def test_budget_preserves_global_and_current_input(self):
        self.doc["cells"] = [new_cell("code", f"step {i}", "a" * 30000) for i in range(70)] + [new_cell("order", "current", "CURRENT_END", "CONTRACT_END")]
        result = pack_context(self.doc, self.doc["cells"][-1]["id"], config={"features": {"compact_history": True}})
        self.assertLessEqual(len(result["prompt"]), MAX_CONTEXT)
        self.assertEqual(len(result["sections"]), 70)
        self.assertIn(self.doc["global"]["goal"], result["prompt"])
        self.assertIn("CURRENT_END", result["prompt"])
        self.assertIn("CONTRACT_END", result["prompt"])

    def test_over_budget_is_explicit(self):
        cell = self.doc["cells"][-1]
        cell["source"] = "x" * MAX_CONTEXT
        with self.assertRaises(Problem):
            pack_context(self.doc, cell["id"])

    def test_edit_marks_downstream_stale_preserving_evidence(self):
        self.evidence()
        incoming = copy.deepcopy(self.doc)
        incoming["cells"][1]["source"] += "\nx=5"
        changed = editable_document(self.doc, incoming)
        self.assertFalse(changed["cells"][0]["stale"])
        self.assertTrue(all(c["stale"] for c in changed["cells"][1:]))
        self.assertEqual(changed["cells"][2]["outputs"], self.doc["cells"][2]["outputs"])

    def test_global_edit_invalidates_all(self):
        self.evidence()
        incoming = copy.deepcopy(self.doc)
        incoming["global"]["constraints"] += "\nNew rule"
        changed = editable_document(self.doc, incoming)
        self.assertEqual(changed["global"]["revision"], 2)
        self.assertTrue(all(c["stale"] for c in changed["cells"]))

    def test_reorder_and_deletion_invalidate_from_changed_position(self):
        self.evidence()
        incoming = copy.deepcopy(self.doc)
        incoming["cells"].pop(1)
        changed = editable_document(self.doc, incoming)
        self.assertFalse(changed["cells"][0]["stale"])
        self.assertTrue(all(c["stale"] for c in changed["cells"][1:]))

    def test_client_cannot_forge_outputs_or_project(self):
        incoming = copy.deepcopy(self.doc)
        incoming["project_path"] = "elsewhere"
        incoming["cells"][0].update(outputs=[{"kind": "text", "text": "forged"}], status="succeeded", initializer=True)
        result = editable_document(self.doc, incoming)
        self.assertEqual(result["project_path"], self.doc["project_path"])
        self.assertEqual(result["cells"][0]["outputs"], [])
        self.assertNotIn("initializer", result["cells"][0])

    def test_conflicts_and_duplicate_ids_rejected(self):
        incoming = copy.deepcopy(self.doc)
        incoming["revision"] = 0
        with self.assertRaises(Problem) as error:
            editable_document(self.doc, incoming)
        self.assertEqual(error.exception.status, 409)
        incoming = copy.deepcopy(self.doc)
        incoming["cells"][1]["id"] = incoming["cells"][0]["id"]
        with self.assertRaises(Problem):
            editable_document(self.doc, incoming)

    def test_json_contract(self):
        self.assertEqual(parse_json_output('```json\n{"ok":true}\n```'), {"ok": True})
        with self.assertRaises(Problem):
            parse_json_output("prose is not json")
        for invalid in ('{"x": NaN}', '{"x": Infinity}', '{"x": 1e999}'):
            with self.assertRaises(Problem):
                parse_json_output(invalid)

    def test_context_uses_native_claude_without_legacy_mode_restrictions(self):
        cell = self.doc["cells"][-1]
        llm = pack_context(self.doc, cell["id"])
        self.assertIn("## Current step", llm["prompt"])
        cell["mode"] = "agent"
        agent = pack_context(self.doc, cell["id"])
        self.assertEqual(llm["fingerprint"], agent["fingerprint"])
        self.assertNotIn("禁用", agent["prompt"])

    def test_context_features_exclude_disabled_content(self):
        self.evidence()
        self.doc["global"].update(goal="GLOBAL_GOAL_MARKER", constraints="GLOBAL_CONSTRAINTS_MARKER")
        self.doc["cells"][0]["source"] = "PREVIOUS_SOURCE_MARKER"
        current = self.doc["cells"][-1]
        current.update(source="CURRENT_SOURCE_MARKER", expected_output="CONTRACT_MARKER")
        config = {"features": {name: False for name in default_context_config()["features"]}}
        context = pack_context(self.doc, current["id"], [{"name": "VARIABLE_MARKER"}], config)
        self.assertIn("CURRENT_SOURCE_MARKER", context["prompt"])
        for marker in ("GLOBAL_GOAL_MARKER", "GLOBAL_CONSTRAINTS_MARKER", "PREVIOUS_SOURCE_MARKER",
                       "CONTRACT_MARKER", "VARIABLE_MARKER", "EVIDENCE_", "输出格式：", "你是面向"):
            self.assertNotIn(marker, context["prompt"])
        self.assertEqual(context["sections"], [])

    def test_context_input_and_output_switches_are_independent(self):
        self.evidence()
        self.doc["cells"][0]["source"] = "PREVIOUS_SOURCE_MARKER"
        current = self.doc["cells"][-1]
        source_only = pack_context(self.doc, current["id"], config={"features": {"previous_outputs": False}})
        self.assertIn("PREVIOUS_SOURCE_MARKER", source_only["prompt"])
        self.assertNotIn("EVIDENCE_", source_only["prompt"])
        self.assertFalse(any(section["has_valid_output"] for section in source_only["sections"]))
        outputs_only = pack_context(self.doc, current["id"], config={"features": {"previous_inputs": False}})
        self.assertNotIn("PREVIOUS_SOURCE_MARKER", outputs_only["prompt"])
        self.assertIn("EVIDENCE_", outputs_only["prompt"])

    def test_context_global_switches_are_independent(self):
        self.doc["global"].update(goal="GLOBAL_GOAL_MARKER", constraints="GLOBAL_CONSTRAINTS_MARKER")
        current = self.doc["cells"][-1]
        for disabled, excluded, included in (("global_goal", "GLOBAL_GOAL_MARKER", "GLOBAL_CONSTRAINTS_MARKER"),
                                             ("global_constraints", "GLOBAL_CONSTRAINTS_MARKER", "GLOBAL_GOAL_MARKER")):
            with self.subTest(disabled=disabled):
                context = pack_context(self.doc, current["id"], config={"features": {disabled: False}})
                self.assertNotIn(excluded, context["prompt"])
                self.assertIn(included, context["prompt"])

    def test_context_manual_summaries_and_compaction_can_be_disabled(self):
        current = self.doc["cells"][-1]
        first = self.doc["cells"][0]
        first.update(source="a" * 5000 + "FULL_SOURCE_END", context_note="MANUAL_SUMMARY_MARKER")
        config = {"features": {"manual_summaries": False, "compact_history": True}}
        compact = pack_context(self.doc, current["id"], config=config)
        self.assertNotIn("MANUAL_SUMMARY_MARKER", compact["prompt"])
        self.assertNotIn("FULL_SOURCE_END", compact["prompt"])
        config["features"]["compact_history"] = False
        full = pack_context(self.doc, current["id"], config=config)
        self.assertIn(first["source"], full["prompt"])
        self.assertFalse(full["sections"][0]["manually_summarized"])
        config["limits"] = {"max_characters": 2000}
        with self.assertRaisesRegex(Problem, "预算"):
            pack_context(self.doc, current["id"], config=config)

    def test_context_templates_preserve_literal_input_and_support_reordering(self):
        cell = self.doc["cells"][-1]
        cell["source"] = 'Do not format {arbitrary_input}: "quotes" \\ paths\n中文'
        config = {"templates": {
            "instructions": 'CUSTOM HARNESS with literal JSON {"ok":true}\n',
            "prompt": "{current_step}\n{instructions}END",
            "current_step": "CUSTOM STEP {id}:\n{source}\nLiteral braces: {{example}}"}}
        context = pack_context(self.doc, cell["id"], config=config)
        self.assertLess(context["prompt"].index("CUSTOM STEP"), context["prompt"].index("CUSTOM HARNESS"))
        self.assertIn(cell["source"], context["prompt"])
        self.assertIn('{"ok":true}', context["prompt"])
        self.assertIn("Literal braces: {example}", context["prompt"])
        self.assertTrue(context["prompt"].endswith("END"))
        config["templates"]["instructions"] += "CHANGED"
        self.assertNotEqual(context["config_fingerprint"], pack_context(self.doc, cell["id"], config=config)["config_fingerprint"])

    def test_instruction_run_paths_expand_without_formatting_literal_json(self):
        cell = self.doc["cells"][-1]
        config = {"templates": {"instructions":
                  'Literal JSON {"ok":true}; literal {source}; escaped {{run_dir}}\n'
                  '最终回答写入 {output_file}，本次运行目录 {run_dir}。\n'}}
        runtime = {"run_id": "a" * 12, "session_name": "ID" + "a" * 12,
                   "run_dir": ".castle/runs/" + "a" * 12,
                   "output_file": ".castle/runs/" + "a" * 12 + "/output.md"}
        context = pack_context(self.doc, cell["id"], config=config, run_info=runtime)
        self.assertIn('Literal JSON {"ok":true}; literal {source}; escaped {{run_dir}}', context["prompt"])
        self.assertIn(f"最终回答写入 {runtime['output_file']}，本次运行目录 {runtime['run_dir']}。", context["prompt"])
        self.assertEqual(context["config"]["templates"]["instructions"], config["templates"]["instructions"])

    def test_invalid_context_config_is_explicit(self):
        for invalid in ([], {"unknown": True}, {"version": 2}, {"features": {"variables": "false"}},
                        {"features": {"typo": False}}, {"limits": {"max_characters": True}},
                        {"templates": {"current_step": "{source.__class__}"}},
                        {"templates": {"prompt": "forgot current input"}},
                        {"templates": {"invocation": "forgot file path"}},
                        {"templates": {"global": "{unknown}"}}):
            with self.subTest(invalid=invalid), self.assertRaises(Problem):
                normalize_context_config(invalid)

    def test_settings_default_to_confirmation_and_fail_safe(self):
        with tempfile.TemporaryDirectory() as folder:
            path = ensure_settings(folder)
            self.assertEqual(path, Path(folder).resolve() / ".castle" / "settings.json")
            self.assertEqual(load_settings(folder), {"version": 1, "confirm_order_run": True, "custom_sessions_dir": ""})
            path.write_text('{"confirm_order_run": false}', encoding="utf-8")
            self.assertFalse(load_settings(folder)["confirm_order_run"])
            overview = settings_summary(folder)
            self.assertFalse(overview["confirm_order_run"])
            self.assertIsNone(overview["error"])
            path.write_text('{"confirm_order_run": "false"}', encoding="utf-8")
            with self.assertRaisesRegex(Problem, "settings.json"):
                load_settings(folder)
            # A broken settings file must never disable the safety prompt.
            overview = settings_summary(folder)
            self.assertTrue(overview["confirm_order_run"])
            self.assertIn("confirm_order_run", overview["error"])

    def test_settings_reject_unknown_options_and_versions(self):
        for invalid in ([], {"unknown": True}, {"version": 2}, {"version": True},
                        {"confirm_order_run": 1}):
            with self.subTest(invalid=invalid), self.assertRaises(Problem):
                normalize_settings(invalid)

    def test_protocol_only_output_is_not_a_final_answer(self):
        for answer in ('<｜｜DSML｜｜ calls>bad</｜｜DSML｜｜ calls>',
                       '<||DSML|| invoke name="Task">bad</||DSML|| invoke>',
                       '<function_calls><invoke name="Read"/></function_calls>',
                       '```xml\n<tool_call>{"name":"Task"}</tool_call>\n```'):
            with self.subTest(answer=answer), self.assertRaisesRegex(Problem, "未执行的工具调用协议"):
                validate_order_output(answer, "markdown")
        validate_order_output("协议错误说明：<｜｜DSML｜｜ calls> 是未执行的标记。", "markdown")

    def test_project_scan_skips_secrets_and_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "README.md").write_text("project documentation", encoding="utf-8")
            (root / ".env").write_text("SECRET", encoding="utf-8")
            (root / "server.key").write_text("SECRET", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "large.js").write_text("x")
            result = scan_project(root)
            self.assertEqual(result["files"], ["README.md"])
            self.assertIn("project documentation", result["manifest_excerpt"])
            with self.assertRaises(Problem):
                scan_project(root, "..")

    def test_project_asset_serves_images_and_rejects_other_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            (root / "docs" / "figure.png").write_bytes(b"\x89PNG\r\n\x1a\n")
            (root / ".env").write_text("PRIVATE", encoding="utf-8")
            (root / "evidence").mkdir()
            (root / "evidence" / "plot.png").write_bytes(b"PNG")
            (root / "notes.md").write_text("# notes", encoding="utf-8")
            (root / "outside.png").write_bytes(b"PNG")
            data, content_type = read_project_asset(root, "docs/figure.png")
            self.assertEqual((data, content_type), (b"\x89PNG\r\n\x1a\n", "image/png"))
            for path in ("../outside.png", ".env", "notes.md", "evidence/plot.png", "docs/missing.png"):
                with self.subTest(path=path), self.assertRaises(Problem):
                    read_project_asset(root, path, excluded=(root / "evidence",))

    def test_markdown_images_are_embedded_as_notebook_attachments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            figure = b"\x89PNG\r\n\x1a\n"
            (root / "docs" / "figure.png").write_bytes(figure)
            cell = new_cell("markdown", "figure", "![预算图](docs/figure.png)")
            embed_markdown_images([cell], root)
            name = next(iter(cell["attachments"]))
            self.assertRegex(name, r"^image-[a-f0-9]{16}\.png$")
            self.assertEqual(cell["source"], f"![预算图](attachment:{name})")
            self.assertEqual(base64.b64decode(cell["attachments"][name]["image/png"]), figure)
            # Saving again is idempotent even if the client still holds the old source.
            again = copy.deepcopy(cell)
            again["source"] = "![预算图](docs/figure.png)"
            embed_markdown_images([again], root)
            self.assertEqual(again["source"], cell["source"])
            self.assertEqual(list(again["attachments"]), [name])
            # Removing the reference removes the stored image.
            cell["source"] = "没有图片"
            embed_markdown_images([cell], root)
            self.assertEqual(cell["attachments"], {})
            # Remote, data, fenced and escaping references stay literal.
            untouched = new_cell("markdown", "x", "![a](https://example.com/a.png)\n"
                                 "![b](data:image/png;base64,iVBORw0KGgo=)\n"
                                 "```\n![c](docs/figure.png)\n```\n![d](../figure.png)")
            embed_markdown_images([untouched], root)
            self.assertEqual(untouched["attachments"], {})
            self.assertIn("![c](docs/figure.png)", untouched["source"])
            # Oversized images are left as ordinary project references.
            (root / "docs" / "big.png").write_bytes(b"x" * (MAX_EMBEDDED_IMAGE_BYTES + 1))
            big = new_cell("markdown", "x", "![big](docs/big.png)")
            embed_markdown_images([big], root)
            self.assertEqual(big["attachments"], {})
            self.assertEqual(big["source"], "![big](docs/big.png)")

    def test_attachment_payloads_are_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            doc = new_notebook(folder)
            cell = new_cell("markdown", "m", "text")
            doc["cells"].append(cell)

            def payload(attachments):
                return {"revision": doc["revision"], "cells": [cell | {"attachments": attachments}]}

            valid = {"image-a.png": {"image/png": base64.b64encode(b"png").decode()}}
            self.assertEqual(editable_document(doc, payload(valid))["cells"][0]["attachments"], valid)
            for attachments in ({"../escape.png": {"image/png": "cG5n"}},
                                {"image-a.png": {"text/html": "cG5n"}},
                                {"image-a.png": {"image/png": "not base64!"}},
                                {f"image-{index}.png": {"image/png": "cG5n"} for index in range(21)}):
                with self.subTest(attachments=attachments), self.assertRaises(Problem):
                    editable_document(doc, payload(attachments))

    def test_workspace_contract_survives_legacy_templates_and_feature_switches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "project_model.py").write_text("value = 42", encoding="utf-8")
            doc = new_notebook(root)
            cell = new_cell("order", "Develop in project", "Implement the requested change", "Files and checks")
            doc["cells"].append(cell)
            config = {"features": {"instructions": False}, "templates": {"prompt": "LEGACY {current_step}"}}
            result = pack_context(doc, cell["id"], config=config)
            self.assertIn(str(root.resolve()), result["prompt"])
            self.assertIn("project_model.py", result["project"]["files"])
            self.assertIn("项目源码、测试和数据文件是实际工作对象", result["prompt"])
            self.assertIn("Python 单元与代理使用同一项目目录", result["prompt"])
            self.assertIn("LEGACY", result["prompt"])
            self.assertEqual(result["project"]["project_path"], str(root.resolve()))
            config["templates"]["prompt"] = "{project}CUSTOM {current_step}"
            explicit = pack_context(doc, cell["id"], config=config)
            self.assertEqual(explicit["prompt"].count("## Project workspace"), 1)

    def test_project_changes_are_content_based_and_exclude_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "model.py").write_text("value = 1", encoding="utf-8")
            (root / "old.py").write_text("old", encoding="utf-8")
            evidence = root / "evidence"
            evidence.mkdir()
            before = snapshot_project(root, excluded=(evidence,))
            (root / "model.py").write_text("value = 2", encoding="utf-8")
            (root / "old.py").unlink()
            (root / "new.py").write_text("new", encoding="utf-8")
            (evidence / "run.json").write_text("{}", encoding="utf-8")
            after = snapshot_project(root, excluded=(evidence,))
            changes = project_changes(before, after)
            self.assertTrue(changes["complete"])
            self.assertEqual({item["path"]: item["change"] for item in changes["files"]},
                             {"model.py": "modified", "new.py": "added", "old.py": "deleted"})
            self.assertEqual(project_changes(after, snapshot_project(root, excluded=(evidence,)))["files"], [])
            missing = snapshot_project(root / "missing")
            self.assertFalse(project_changes(after, missing)["complete"])
            self.assertEqual(project_changes(after, missing)["files"], [])

    def test_project_file_preview_rejects_escape_private_and_evidence_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "model.py").write_text("value = 42", encoding="utf-8")
            (root / ".env").write_text("PRIVATE", encoding="utf-8")
            (root / "evidence").mkdir()
            (root / "evidence" / "run.json").write_text("{}", encoding="utf-8")
            self.assertEqual(read_project_file(root, "model.py")["source"], "value = 42")
            for path in ("../model.py", ".env", "evidence/run.json"):
                with self.subTest(path=path), self.assertRaises(Problem):
                    read_project_file(root, path, excluded=(root / "evidence",))


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.store = Store(self.project / ".castle")
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        self.doc = new_notebook(self.project)
        self.store.put(self.doc)

    def tearDown(self):
        self.engine.close()
        # Cancellation finalizers must finish before the temporary directory is removed.
        for rid in list(self.engine.runs):
            wait_run(self.engine, rid)
        self.temp.cleanup()

    def add(self, kind, source, output_format="markdown", mode="llm"):
        self.doc = self.store.get(self.doc["id"])
        cell = new_cell(kind, "test step", source, "Explicit expected result")
        cell.update(output_format=output_format, mode=mode)
        self.doc["cells"].append(cell)
        self.store.put(self.doc)
        return cell

    def execute(self, cell):
        return wait_run(self.engine, self.engine.start(self.doc["id"], cell["id"])["id"])

    def test_agent_edits_project_then_python_imports_real_module(self):
        (self.project / "project_model.py").write_text("def answer():\n    return 1\n", encoding="utf-8")
        (self.project / "obsolete.txt").write_text("obsolete", encoding="utf-8")
        order = self.add("order", "FAKE_PROJECT_EDIT")
        preview = self.engine.context(self.doc["id"], order["id"])
        run = self.execute(order)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["context"], preview)
        self.assertEqual({item["path"]: item["change"] for item in run["project_changes"]["files"]},
                         {"project_model.py": "modified", "verification.txt": "added", "obsolete.txt": "deleted"})
        folder = self.store.run_path(run["id"])
        self.assertEqual(json.loads((folder / "changes.json").read_text(encoding="utf-8")), run["project_changes"])
        self.assertTrue((folder / "project-before.json").is_file())
        self.assertTrue((folder / "project-after.json").is_file())
        verification = self.execute(self.add("code", "from project_model import answer\nassert answer() == 42\nanswer()"))
        self.assertEqual(verification["status"], "succeeded")
        self.assertEqual(verification["outputs"][-1]["text"], "42")
        self.assertEqual(verification["project_changes"]["files"], [])
        self.assertEqual(self.engine.document(self.doc["id"])["cells"][0]["project_changes"], run["project_changes"])

    def test_failed_code_collects_partial_project_writes(self):
        run = self.execute(self.add("code", "from pathlib import Path\nPath('partial.py').write_text('value = 7')\nraise RuntimeError('incomplete')"))
        self.assertEqual(run["status"], "failed")
        self.assertEqual([(item["path"], item["change"]) for item in run["project_changes"]["files"]], [("partial.py", "added")])
        self.assertTrue(any(event["type"] == "project_changes" for event in self.engine.events(run["id"])))

    def test_project_edits_invalidate_earlier_results_without_resetting_kernel(self):
        code = self.add("code", "x = 1\nx")
        first = self.execute(code)
        analysis = self.add("order", "Inspect previous results")
        self.execute(analysis)
        changed = self.execute(self.add("code", "from pathlib import Path\nPath('model.py').write_text('value = 42')"))
        doc = self.engine.document(self.doc["id"])
        self.assertTrue(doc["cells"][0]["stale"])
        self.assertTrue(doc["cells"][1]["stale"])
        self.assertFalse(doc["cells"][-1]["stale"])
        self.assertEqual(doc["cells"][0]["last_run_id"], first["id"])
        self.assertEqual(doc["cells"][0]["outputs"], first["outputs"])
        self.assertEqual(doc["runtime"]["state"], "ready")
        self.assertEqual(self.engine.get_run(changed["id"])["project_changes"]["files"][0]["path"], "model.py")

    def test_cancelled_code_collects_project_writes(self):
        cell = self.add("code", "from pathlib import Path\nimport time\nPath('before_stop.py').write_text('value = 1')\ntime.sleep(30)")
        run = self.engine.start(self.doc["id"], cell["id"])
        deadline = time.monotonic() + 8
        while not (self.project / "before_stop.py").is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue((self.project / "before_stop.py").is_file())
        self.engine.cancel(run["id"])
        stopped = wait_run(self.engine, run["id"])
        self.assertEqual(stopped["status"], "cancelled")
        self.assertEqual([(item["path"], item["change"]) for item in stopped["project_changes"]["files"]], [("before_stop.py", "added")])

    def test_shared_kernel_last_expression_and_metadata(self):
        first = self.execute(self.add("code", "x=21\nprint('hello')\nx"))
        second = self.execute(self.add("code", "x * 2"))
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["outputs"][-1]["text"], "42")
        self.assertEqual(first["kernel_id"], second["kernel_id"])
        self.assertEqual(first["execution_count"], 1)
        self.assertEqual(second["execution_count"], 2)
        self.assertIn("x", [v["name"] for v in self.engine.runtime(self.doc["id"])["variables"]])

    def test_display_table_and_non_finite_numbers(self):
        run = self.execute(self.add("code", "display([{'x': 1}, {'x': float('nan')}])"))
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["outputs"][0]["kind"], "table")
        self.assertEqual(run["outputs"][0]["data"]["rows"], [[1], ["nan"]])

    def test_shell_commands_stream_unicode_and_mix_with_python(self):
        (self.project / "shell fixture.py").write_text(
            "import os, sys\n"
            "assert sys.stdin.read() == ''\n"
            "print(sys.argv[1], flush=True)\n"
            "print(os.getcwd(), flush=True)\n"
            "print('shell-stderr', file=sys.stderr, flush=True)\n", encoding="utf-8")
        run = self.execute(self.add("code", 'import sys\nlabel = "中文 path with spaces"\n'
                                    '!"{sys.executable}" "shell fixture.py" "{label}"\nx = 21\nx * 2'))
        self.assertEqual(run["status"], "succeeded", run["outputs"])
        stdout = "".join(item.get("text", "") for item in run["outputs"] if item["kind"] == "stdout")
        stderr = "".join(item.get("text", "") for item in run["outputs"] if item["kind"] == "stderr")
        self.assertIn("中文 path with spaces", stdout)
        self.assertIn(str(self.project), stdout)
        self.assertIn("shell-stderr", stderr)
        self.assertEqual(run["outputs"][-1]["text"], "42")
        names = {v["name"] for v in self.engine.runtime(self.doc["id"])["variables"]}
        self.assertIn("x", names)
        self.assertFalse(names & {"In", "Out", "exit", "quit", "open", "get_ipython"})

    def test_shell_output_capture_and_local_variable_expansion(self):
        run = self.execute(self.add("code", 'def collect():\n    label = "local_value"\n'
                                    '    lines = !echo $label\n    return lines\n'
                                    'lines = collect()\nassert lines == ["local_value"]\n'
                                    'assert lines.s == "local_value"\n'
                                    'assert lines.n == "local_value"\nlines'))
        self.assertEqual(run["status"], "succeeded", run["outputs"])
        self.assertEqual(json.loads(run["outputs"][-1]["text"]), ["local_value"])
        self.assertFalse(any(item["kind"] == "stdout" for item in run["outputs"]))
        direct = self.execute(self.add("code", "!!echo direct_capture"))
        self.assertEqual(direct["status"], "succeeded", direct["outputs"])
        self.assertEqual(json.loads(direct["outputs"][-1]["text"]), ["direct_capture"])

    def test_shell_native_encoding_and_line_endings(self):
        run = self.execute(self.add("code", '!echo 中文\nlines = !echo 中文\nassert lines == ["中文"]\nlines'))
        self.assertEqual(run["status"], "succeeded", run["outputs"])
        stdout = "".join(item.get("text", "") for item in run["outputs"] if item["kind"] == "stdout")
        self.assertEqual(stdout, "中文\n")
        self.assertEqual(json.loads(run["outputs"][-1]["text"]), ["中文"])

    def test_shell_nonzero_exit_code_does_not_fail_python_cell(self):
        run = self.execute(self.add("code", 'import sys\n!"{sys.executable}" -c "import sys; sys.exit(7)"\n_exit_code'))
        self.assertEqual(run["status"], "succeeded", run["outputs"])
        self.assertEqual(run["outputs"][-1]["text"], "7")
        missing = self.execute(self.add("code", "!castle_missing_command_84723\nassert _exit_code != 0\n21 * 2"))
        self.assertEqual(missing["status"], "succeeded", missing["outputs"])
        self.assertEqual(missing["outputs"][-1]["text"], "42")

    def test_ipython_leaves_strings_comments_and_inequality_unchanged(self):
        source = 'text = """!echo not_a_command\n%pwd\n%%time\n"""\n# !echo ignored\nassert 1 != 2\ntext'
        run = self.execute(self.add("code", source))
        self.assertEqual(run["status"], "succeeded", run["outputs"])
        self.assertEqual(run["outputs"], [{"kind": "text", "text": "!echo not_a_command\n%pwd\n%%time\n"}])

    def test_ipython_magics_share_namespace_and_directory(self):
        (self.project / "sub directory").mkdir()
        changed = self.execute(self.add("code", '%cd "sub directory"\n%pwd'))
        self.assertEqual(changed["status"], "succeeded", changed["outputs"])
        self.assertEqual(Path(changed["outputs"][-1]["text"]), self.project / "sub directory")
        timed = self.execute(self.add("code", "%%time\ntimed_value = 21 * 2"))
        self.assertEqual(timed["status"], "succeeded", timed["outputs"])
        self.assertIn("Wall time:", "".join(item.get("text", "") for item in timed["outputs"]))
        after = self.execute(self.add("code", 'import os, sys\n'
                                      '!"{sys.executable}" -c "import os; print(os.getcwd())"\ntimed_value'))
        self.assertEqual(after["status"], "succeeded", after["outputs"])
        self.assertIn(str(self.project / "sub directory"), "".join(item.get("text", "") for item in after["outputs"]))
        self.assertEqual(after["outputs"][-1]["text"], "42")
        self.assertEqual(after["kernel_id"], changed["kernel_id"])

    def test_ipython_capture_preserves_structured_outputs(self):
        captured = self.execute(self.add("code", '%%capture captured\nprint("captured-python")\n'
                                         '!echo captured-shell\ndisplay([{"x": 42}])\ndisplay_markdown("**captured**")'))
        self.assertEqual(captured["status"], "succeeded", captured["outputs"])
        self.assertEqual(captured["outputs"], [])
        shown = self.execute(self.add("code", "captured.show()"))
        self.assertEqual(shown["status"], "succeeded", shown["outputs"])
        text = "".join(item.get("text", "") for item in shown["outputs"])
        self.assertIn("captured-python", text)
        self.assertIn("captured-shell", text)
        self.assertIn({"kind": "table", "data": {"columns": ["x"], "rows": [[42]]}, "truncated": False}, shown["outputs"])
        self.assertIn({"kind": "markdown", "text": "**captured**"}, shown["outputs"])

    def test_ipython_pip_uses_the_kernel_interpreter(self):
        run = self.execute(self.add("code", "%pip --version\nassert _exit_code == 0"))
        self.assertEqual(run["status"], "succeeded", run["outputs"])
        self.assertIn("pip ", "".join(item.get("text", "") for item in run["outputs"]))

    def test_ipython_semicolon_history_and_top_level_await(self):
        quiet = self.execute(self.add("code", "21;"))
        self.assertEqual(quiet["status"], "succeeded", quiet["outputs"])
        self.assertEqual(quiet["outputs"], [])
        run = self.execute(self.add("code", "import asyncio\nawait asyncio.sleep(0)\n21 * 2"))
        self.assertEqual(run["status"], "succeeded", run["outputs"])
        self.assertEqual(run["outputs"][-1]["text"], "42")
        history = self.execute(self.add("code", "assert _ == 42\nOut[2]"))
        self.assertEqual(history["status"], "succeeded", history["outputs"])
        self.assertEqual(history["outputs"][-1]["text"], "42")

    def test_shell_cancel_and_timeout_kill_descendants_after_streaming(self):
        child = "import time; from pathlib import Path; time.sleep(1.5); Path('orphan.txt').write_text('orphan')"
        (self.project / "long shell.py").write_text(
            f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\n"
            "print('early-output', end='', flush=True)\ntime.sleep(30)\n", encoding="utf-8")
        for action in ("cancel", "timeout"):
            with self.subTest(action=action):
                self.engine.code_timeout = 60
                self.execute(self.add("code", "1 + 1"))  # Warm the kernel before timing the shell.
                self.engine.code_timeout = .6 if action == "timeout" else 60
                cell = self.add("code", 'import sys\n!"{sys.executable}" "long shell.py"')
                run = self.engine.start(self.doc["id"], cell["id"])
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    running = self.engine.get_run(run["id"])
                    if "early-output" in "".join(item.get("text", "") for item in running["outputs"]):
                        break
                    time.sleep(.02)
                else:
                    self.fail(f"No live output before completion: {running}")
                self.assertEqual(running["status"], "running")
                if action == "cancel":
                    self.engine.cancel(run["id"])
                stopped = wait_run(self.engine, run["id"])
                self.assertEqual(stopped["status"], "cancelled" if action == "cancel" else "failed")
                if action == "timeout":
                    self.assertIn("超过", stopped["error"])
                self.assertEqual(self.engine.runtime(self.doc["id"])["state"], "cold")
                time.sleep(1.6)
                self.assertFalse((self.project / "orphan.txt").exists(), "Shell descendant survived kernel termination")

    def test_missing_ipython_reports_install_command_for_selected_interpreter(self):
        worker = Path(__file__).resolve().parents[1] / "castle_notebook" / "kernel_worker.py"
        request = json.dumps({"id": "missing-ipython", "cell_id": "test", "code": "1 + 1"}) + "\n"
        process = subprocess.run([sys.executable, "-X", "utf8", "-S", "-u", str(worker), str(self.project)],
                                 input=request, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8", timeout=10, **spawn_options())
        self.assertEqual(process.returncode, 0, process.stderr)
        messages = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertFalse(messages[-1]["success"])
        error = next(message["text"] for message in messages if message["type"] == "error")
        self.assertIn("pip install", error)
        self.assertIn(sys.executable, error)
        self.assertIn("IPython", error)

    def test_failure_preserves_partial_variables_and_traceback(self):
        run = self.execute(self.add("code", "x=3\nraise ValueError('deliberate')"))
        self.assertEqual(run["status"], "failed")
        self.assertIn("ValueError: deliberate", run["outputs"][-1]["text"])
        after = self.execute(self.add("code", "x"))
        self.assertEqual(after["outputs"][-1]["text"], "3")

    def test_markdown_is_real_render_output_without_kernel(self):
        run = self.execute(self.add("markdown", "# Assumption"))
        self.assertEqual(run["outputs"], [{"kind": "markdown", "text": "# Assumption"}])
        self.assertEqual(self.engine.runtime(self.doc["id"])["state"], "cold")

    def test_claude_streaming_final_artifact_and_exact_context(self):
        cell = self.add("order", "Inspect evidence", mode="agent")
        preview = self.engine.context(self.doc["id"], cell["id"])
        run = self.execute(cell)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["context"], preview)
        self.assertEqual((self.store.run_path(run["id"]) / "context.md").read_text(encoding="utf-8"), preview["prompt"])
        answer = run["outputs"][0]["text"]
        self.assertNotIn("正在流式输出", answer)
        self.assertEqual(Path(run["artifact"]).read_text(encoding="utf-8"), answer)
        self.assertEqual(run["output_sha256"], hashlib.sha256(answer.encode()).hexdigest())
        events = self.engine.events(run["id"])
        self.assertIn("tool", [e["type"] for e in events])
        self.assertIn("canonical_output", [e["type"] for e in events])
        self.assertEqual(events[-1]["type"], "finished")
        self.assertEqual(self.engine.events(run["id"], 2), events[2:])
        self.assertEqual(run["command"][run["command"].index("-n") + 1], f"ID{run['id']}")
        self.assertIn("tool_result", [e["type"] for e in events])
        self.assertEqual(run["provider_model"], "fixture-model")
        self.assertEqual(run["command"][len(FAKE):], ["-p", run["invocation_prompt"], "-n", f"ID{run['id']}",
                         "--permission-mode", "auto", "--output-format", "stream-json", "--verbose", "--include-partial-messages"])
        self.assertNotIn("--dangerously-skip-permissions", run["command"])

    def test_model_written_output_file_is_canonical_and_never_overwritten(self):
        run = self.execute(self.add("order", "FAKE_WRITE_FILE"))
        self.assertEqual(run["status"], "succeeded")
        expected = "# 文件正文\n\n**第一版**"
        self.assertEqual(Path(run["artifact"]).read_text(encoding="utf-8"), expected)
        self.assertEqual(run["outputs"], [{"kind": "markdown", "text": expected}])
        self.assertIn("不应覆盖文件", run["cli_output"])
        self.assertEqual(run["output_source"], "file")
        self.assertEqual(run["output_sha256"], hashlib.sha256(expected.encode()).hexdigest())

    def test_provider_failure_keeps_written_file_and_reports_cli_error(self):
        run = self.execute(self.add("order", "FAKE_WRITE_FILE FAKE_FAILURE"))
        self.assertEqual(run["status"], "failed")
        self.assertIn("CLI 已完成写入", run["error"])
        self.assertIn("第一版", Path(run["artifact"]).read_text(encoding="utf-8"))
        self.assertIn("第一版", run["outputs"][0]["text"])

    def test_json_contract_checks_written_file_instead_of_cli_acknowledgement(self):
        good = self.execute(self.add("order", "FAKE_WRITE_JSON_FILE", "json"))
        self.assertEqual(good["status"], "succeeded")
        self.assertEqual(json.loads(good["outputs"][0]["text"]), {"from_file": True})
        bad = self.execute(self.add("order", "FAKE_WRITE_BAD_JSON_FILE", "json"))
        self.assertEqual(bad["status"], "failed")
        self.assertIn("JSON 契约", bad["error"])
        self.assertEqual(Path(bad["artifact"]).read_text(encoding="utf-8"), "not valid JSON in file")

    def test_output_file_updates_during_execution_and_after_completion(self):
        cell = self.add("order", "FAKE_FILE_WAIT")
        started = self.engine.start(self.doc["id"], cell["id"])
        path = Path(started["artifact"])
        deadline = time.monotonic() + 5
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(.02)
        first = self.engine.order_outputs(self.doc["id"])
        self.assertEqual(self.engine.get_run(started["id"])["status"], "running")
        self.assertIn("第一版", first["cells"][0]["outputs"][0]["text"])
        deadline = time.monotonic() + 4
        while "第二版" not in path.read_text(encoding="utf-8") and time.monotonic() < deadline:
            time.sleep(.02)
        second = self.engine.order_outputs(self.doc["id"], first["fingerprint"])
        self.assertTrue(second["changed"])
        self.assertIn("第二版", second["cells"][0]["outputs"][0]["text"])
        run = wait_run(self.engine, started["id"])
        self.assertIn("第二版", run["outputs"][0]["text"])
        path.write_text("# 外部对话更新\n\n第三版", encoding="utf-8")
        after = self.engine.order_outputs(self.doc["id"], second["fingerprint"])
        self.assertIn("第三版", after["cells"][0]["outputs"][0]["text"])
        saved = self.store.get(self.doc["id"])
        self.assertIn("第三版", saved["cells"][0]["outputs"][0]["text"])
        # The original run remains an immutable execution record.
        self.assertEqual(self.engine.get_run(run["id"])["outputs"], run["outputs"])

    def test_live_file_cache_does_not_conflict_with_input_save_and_handles_empty_delete_restore(self):
        cell = self.add("order", "Inspect evidence")
        run = self.execute(cell)
        payload = self.store.get(self.doc["id"])
        path = Path(run["artifact"])
        path.write_text("# 最新文件", encoding="utf-8")
        latest = self.engine.order_outputs(self.doc["id"])
        self.assertEqual(self.store.get(self.doc["id"])["revision"], payload["revision"])
        payload["title"] = "Input editing remains available"
        saved = self.engine.save(self.doc["id"], payload)
        self.assertEqual(saved["title"], payload["title"])
        self.assertEqual(saved["cells"][0]["outputs"][0]["text"], "# 最新文件")
        unchanged = self.engine.order_outputs(self.doc["id"], latest["fingerprint"])
        self.assertFalse(unchanged["changed"])
        self.assertNotIn("cells", unchanged)
        path.write_text("", encoding="utf-8")
        empty = self.engine.order_outputs(self.doc["id"])
        self.assertEqual(empty["cells"][0]["outputs"][0]["text"], "")
        self.assertEqual(empty["cells"][0]["file_output"]["state"], "ready")
        path.write_text("cached before deletion", encoding="utf-8")
        self.engine.order_outputs(self.doc["id"])
        path.unlink()
        missing = self.engine.order_outputs(self.doc["id"])
        self.assertEqual(missing["cells"][0]["file_output"]["state"], "missing")
        self.assertTrue(missing["cells"][0]["file_output"]["cached"])
        self.assertEqual(missing["cells"][0]["outputs"][0]["text"], "cached before deletion")
        path.write_bytes(b"\xef\xbb\xbf# restored\r\n")
        restored = self.engine.order_outputs(self.doc["id"])
        self.assertEqual(restored["cells"][0]["outputs"][0]["text"], "# restored\n")

    def test_live_output_is_bounded_and_follows_only_latest_run(self):
        cell = self.add("order", "Inspect evidence")
        first = self.execute(cell)
        latest = self.execute(cell)
        Path(first["artifact"]).write_text("historical file changed", encoding="utf-8")
        data = self.engine.order_outputs(self.doc["id"])
        self.assertEqual(data["cells"][0]["file_output"]["run_id"], latest["id"])
        self.assertNotIn("historical file changed", data["cells"][0]["outputs"][0]["text"])
        Path(latest["artifact"]).write_text("x" * 100, encoding="utf-8")
        with patch("castle_notebook.execution.OUTPUT_LIMIT", 32):
            oversized = self.engine.order_outputs(self.doc["id"])
        self.assertEqual(oversized["cells"][0]["file_output"]["state"], "unavailable")
        self.assertIn("上限", oversized["cells"][0]["file_output"]["error"])
        self.assertEqual(oversized["cells"][0]["outputs"], data["cells"][0]["outputs"])

    def test_external_output_changes_invalidate_downstream_and_refresh_context(self):
        upstream = self.execute(self.add("order", "Inspect evidence"))
        downstream = self.add("order", "FAKE_FILE_WAIT")
        started = self.engine.start(self.doc["id"], downstream["id"])
        Path(upstream["artifact"]).write_text("UPDATED_UPSTREAM_EVIDENCE", encoding="utf-8")
        self.engine.order_outputs(self.doc["id"])
        run = wait_run(self.engine, started["id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertTrue(run["stale"])
        self.assertIn("output.md", run["stale_reason"])
        next_cell = self.add("order", "Review latest evidence")
        context = self.engine.context(self.doc["id"], next_cell["id"])
        self.assertIn("UPDATED_UPSTREAM_EVIDENCE", context["prompt"])

    def test_prompt_file_preserves_full_input_without_passing_it_in_command(self):
        source = '中文研究输入："double quotes"、\'single quotes\'、反斜杠 \\、反引号 `、美元符号 $、多行\n第二行 & | < >'
        self.doc["global"]["constraints"] = "Global 中的引号与换行必须完整保留。\nDo not alter input."
        self.store.put(self.doc)
        cell = self.add("order", source)
        preview = self.engine.context(self.doc["id"], cell["id"])
        run = self.execute(cell)
        self.assertEqual(run["status"], "succeeded")
        prompt_file = self.project / ".castle" / "prompts" / f"input{run['id']}.md"
        self.assertEqual(Path(run["prompt_file"]), prompt_file)
        self.assertEqual(prompt_file.read_bytes(), preview["prompt"].encode("utf-8"))
        self.assertEqual(hashlib.sha256(prompt_file.read_bytes()).hexdigest(), preview["fingerprint"])
        self.assertEqual(prompt_file.read_bytes(), (self.store.run_path(run["id"]) / "context.md").read_bytes())
        self.assertIn(f".castle/prompts/input{run['id']}.md", run["invocation_prompt"])
        self.assertNotIn(source, " ".join(run["command"]))
        self.assertLess(len(run["invocation_prompt"]), 1000)
        tool = next(event for event in self.engine.events(run["id"]) if event["type"] == "tool")
        self.assertEqual(json.loads(tool["data"]["input"])["file_path"], str(prompt_file.relative_to(self.project)))

    def test_context_config_hot_reload_and_run_snapshot(self):
        cell = self.add("order", "FAKE_JSON", "json")
        path = ensure_context_config(self.project)
        config = load_context_config(self.project)
        config["features"]["variables"] = False
        config["templates"]["instructions"] = "CUSTOM_HARNESS_ONE\n"
        config["templates"]["invocation"] = "Read {prompt_file}; run {run_id}; session {session_name}."
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        preview = self.engine.context(self.doc["id"], cell["id"])
        self.assertIn("CUSTOM_HARNESS_ONE", preview["prompt"])
        self.assertNotIn("Live Python variables", preview["prompt"])
        run = self.execute(cell)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["context"], preview)
        self.assertTrue(run["invocation_prompt"].startswith(f"Read .castle/prompts/input{run['id']}.md; run {run['id']}; session ID{run['id']}."))
        self.assertNotIn(run["output_file"], run["invocation_prompt"])
        config["templates"]["instructions"] = "CUSTOM_HARNESS_TWO\n"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        latest = self.engine.context(self.doc["id"], cell["id"])
        self.assertIn("CUSTOM_HARNESS_TWO", latest["prompt"])
        self.assertNotEqual(latest["config_fingerprint"], run["context"]["config_fingerprint"])
        saved = self.store.get_run(run["id"])
        self.assertEqual(saved["context"]["config"]["templates"]["instructions"], "CUSTOM_HARNESS_ONE\n")
        self.assertEqual(Path(saved["prompt_file"]).read_text(encoding="utf-8"), preview["prompt"])

    def test_relative_output_paths_are_in_instructions_with_exact_preview(self):
        cell = self.add("order", "Inspect evidence")
        preview = self.engine.context(self.doc["id"], cell["id"])
        self.assertEqual(preview, self.engine.context(self.doc["id"], cell["id"]))
        run = self.execute(cell)
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["context"], preview)
        self.assertEqual(run["id"], preview["run"]["run_id"])
        self.assertEqual(run["run_dir"], f".castle/runs/{run['id']}")
        self.assertEqual(run["output_file"], f".castle/runs/{run['id']}/output.md")
        self.assertEqual((self.project / run["output_file"]).resolve(), Path(run["artifact"]))
        prompt = Path(run["prompt_file"]).read_text(encoding="utf-8")
        self.assertIn(f"写入本次运行目录 `{run['run_dir']}` 的 output.md（相对项目目录的完整路径：`{run['output_file']}`），格式：TLDR总结做了什么，后面跟着详细内容。注意：此后的每一轮对话", prompt)
        self.assertNotIn(run["artifact"], prompt)
        self.assertNotIn("{output_file}", prompt)
        self.assertNotIn(run["output_file"], run["invocation_prompt"])
        self.assertEqual(run["command"][run["command"].index("-p") + 1], run["invocation_prompt"])
        next_preview = self.engine.context(self.doc["id"], cell["id"])
        self.assertNotEqual(next_preview["run"]["run_id"], run["id"])
        self.assertNotEqual(next_preview["run"]["output_file"], run["output_file"])

    def test_custom_output_path_placeholders_use_custom_data_directory(self):
        self.engine.close()
        self.store = Store(self.project / "separate evidence with spaces")
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        self.store.put(self.doc)
        config = {"templates": {"invocation": "Read {prompt_file}\nRun: {run_dir}\nOutput: {output_file}"}}
        ensure_context_config(self.project).write_text(json.dumps(config), encoding="utf-8")
        run = self.execute(self.add("order", "Inspect evidence"))
        self.assertEqual(run["status"], "succeeded")
        self.assertIn(f"separate evidence with spaces/runs/{run['id']}", run["invocation_prompt"])
        self.assertFalse(Path(run["run_dir"]).is_absolute())
        self.assertEqual((self.project / run["output_file"]).resolve(), self.store.run_path(run["id"]) / "output.md")
        self.assertEqual(run["invocation_prompt"].count(run["output_file"]), 1)
        self.assertEqual(run["invocation_prompt"].count("\nRun: "), 1)
        self.assertNotIn("本次运行目录（绝对路径）", run["invocation_prompt"])

    def test_explicit_relative_run_id_paths_are_not_duplicated(self):
        folder = ".castle/runs/{run_id}"
        template = f"Read {{prompt_file}}\nRun: {folder}\nOutput: {folder}/output.md"
        ensure_context_config(self.project).write_text(json.dumps({"templates": {"invocation": template}}), encoding="utf-8")
        run = self.execute(self.add("order", "Inspect evidence"))
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["invocation_prompt"].count(run["output_file"]), 1)
        self.assertNotIn("本次运行目录（绝对路径）", run["invocation_prompt"])

    def test_output_paths_can_refer_to_evidence_outside_project(self):
        self.engine.close()
        workspace = self.project / "workspace"
        workspace.mkdir()
        self.store = Store(self.project / "evidence")
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        self.doc = new_notebook(workspace)
        self.store.put(self.doc)
        run = self.execute(self.add("order", "Inspect evidence"))
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["output_file"], f"../evidence/runs/{run['id']}/output.md")
        self.assertEqual((workspace / run["output_file"]).resolve(), Path(run["artifact"]))
        self.assertIn(run["output_file"], Path(run["prompt_file"]).read_text(encoding="utf-8"))

    def test_relative_path_failure_does_not_start_a_run(self):
        cell = self.add("order", "Inspect evidence")
        with patch("castle_notebook.execution.os.path.relpath", side_effect=ValueError("different drives")):
            with self.assertRaisesRegex(Problem, "同一驱动器"):
                self.engine.start(self.doc["id"], cell["id"])
        self.assertEqual(self.engine.active, {})
        self.assertEqual(list((self.store.path / "runs").iterdir()), [])

    def test_invalid_project_config_does_not_start_run_or_replace_file(self):
        cell = self.add("order", "Inspect evidence")
        path = ensure_context_config(self.project)
        original = '{"features": {"variables": "false"}}'
        path.write_text(original, encoding="utf-8")
        for action in (self.engine.context, self.engine.start):
            with self.assertRaisesRegex(Problem, "context.json"):
                action(self.doc["id"], cell["id"])
        self.assertEqual(self.engine.active, {})
        self.assertEqual(list((self.store.path / "runs").iterdir()), [])
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_partial_config_and_bom_are_supported_without_replacing_edits(self):
        path = ensure_context_config(self.project)
        content = '{"features": {"previous_outputs": false}}'
        path.write_text(content, encoding="utf-8-sig")
        ensure_context_config(self.project)
        config = load_context_config(self.project)
        self.assertFalse(config["features"]["previous_outputs"])
        self.assertFalse(config["features"]["variables"])
        self.assertEqual(path.read_text(encoding="utf-8-sig"), content)

    def test_legacy_modes_both_use_native_claude(self):
        for mode in ("llm", "agent"):
            with self.subTest(mode=mode):
                run = self.execute(self.add("order", "FAKE_JSON", "json", mode=mode))
                self.assertEqual(run["status"], "succeeded")
                for flag in ("--tools", "--strict-mcp-config", "--system-prompt",
                             "--mcp-config", "--disable-slash-commands", "--max-turns", "--no-session-persistence"):
                    self.assertNotIn(flag, run["command"])

    def test_open_session_resumes_latest_run_after_restart_without_changing_evidence(self):
        cell = self.add("order", "Inspect evidence")
        first = self.execute(cell)
        latest = self.execute(cell)
        self.assertNotEqual(first["session_name"], latest["session_name"])
        self.assertNotEqual(first["session_id"], latest["session_id"])
        self.assertEqual(latest["session_name"], f"ID{latest['id']}")
        before = self.store.get(self.doc["id"])
        restored = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        try:
            session = restored.document(self.doc["id"])["cells"][-1]["session"]
            self.assertTrue(session["available"])
            self.assertEqual(session["name"], latest["session_name"])
            self.assertEqual(session["id"], latest["session_id"])
            with patch("castle_notebook.execution.open_claude_terminal") as launch:
                result = restored.open_session(self.doc["id"], cell["id"])
            launch.assert_called_once_with([*FAKE, "--resume", latest["session_id"], "--permission-mode", "auto"], str(self.project))
            self.assertEqual(result, {"session_name": latest["session_name"]})
            self.assertEqual(self.store.get(self.doc["id"]), before)
            self.assertEqual(self.store.get_run(latest["id"]), latest)
        finally:
            restored.close()

    def test_open_session_rejects_unrun_running_and_missing_sessions(self):
        cell = self.add("order", "FAKE_WAIT")
        with patch("castle_notebook.execution.open_claude_terminal") as launch:
            with self.assertRaisesRegex(Problem, "请先运行"):
                self.engine.open_session(self.doc["id"], cell["id"])
            run = self.engine.start(self.doc["id"], cell["id"])
            with self.assertRaisesRegex(Problem, "等待或停止"):
                self.engine.open_session(self.doc["id"], cell["id"])
            self.engine.cancel(run["id"])
            wait_run(self.engine, run["id"])
            with self.assertRaisesRegex(Problem, "未生成可恢复"):
                self.engine.open_session(self.doc["id"], cell["id"])
            code = self.add("code", "42")
            with self.assertRaisesRegex(Problem, "只有 Order"):
                self.engine.open_session(self.doc["id"], code["id"])
            with self.assertRaises(Problem) as missing:
                self.engine.open_session(self.doc["id"], "f" * 12)
            self.assertEqual(missing.exception.status, 404)
            launch.assert_not_called()

    def test_cancelled_order_with_saved_session_can_be_opened(self):
        cell = self.add("order", "FAKE_SESSION_WAIT")
        run = self.engine.start(self.doc["id"], cell["id"])
        deadline = time.monotonic() + 5
        while not self.engine.get_run(run["id"]).get("session_id") and time.monotonic() < deadline:
            time.sleep(0.02)
        session_id = self.engine.get_run(run["id"]).get("session_id")
        self.assertTrue(session_id)
        self.assertNotEqual(session_id, run["session_name"])
        self.engine.cancel(run["id"])
        self.assertEqual(wait_run(self.engine, run["id"])["status"], "cancelled")
        self.assertTrue(self.engine.document(self.doc["id"])["cells"][-1]["session"]["available"])
        with patch("castle_notebook.execution.open_claude_terminal") as launch:
            self.engine.open_session(self.doc["id"], cell["id"])
        launch.assert_called_once_with([*FAKE, "--resume", session_id, "--permission-mode", "auto"], str(self.project))

    def test_old_session_metadata_uses_command_but_rejects_disabled_persistence(self):
        cell = self.add("order", "Inspect evidence")
        run = self.execute(cell)
        name = run.pop("session_name")
        session_id = run.pop("session_id")
        self.store.put_run(run)
        restored = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        try:
            with patch("castle_notebook.execution.open_claude_terminal") as launch:
                restored.open_session(self.doc["id"], cell["id"])
                launch.assert_called_once_with([*FAKE, "--resume", session_id, "--permission-mode", "auto"], str(self.project))
                self.assertEqual(restored.document(self.doc["id"])["cells"][-1]["session"]["name"], name)
                launch.reset_mock()
                run["command"].append("--no-session-persistence")
                self.store.put_run(run)
                self.assertFalse(restored.document(self.doc["id"])["cells"][-1]["session"]["available"])
                with self.assertRaisesRegex(Problem, "未保存 Claude 会话"):
                    restored.open_session(self.doc["id"], cell["id"])
                launch.assert_not_called()
        finally:
            restored.close()

    def test_invalid_json_fails_and_preserves_raw_answer(self):
        run = self.execute(self.add("order", "FAKE_BAD_JSON", "json"))
        self.assertEqual(run["status"], "failed")
        self.assertIn("JSON", run["error"])
        self.assertIn("this is not json", Path(run["artifact"]).read_text(encoding="utf-8"))

    def test_provider_errors_are_not_success(self):
        run = self.execute(self.add("order", "FAKE_FAILURE"))
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["error"])
        run = self.execute(self.add("order", "FAKE_NO_RESULT"))
        self.assertEqual(run["status"], "failed")
        self.assertIn("最终结果", run["error"])

    def test_protocol_leak_fails_preserving_raw_output_and_excluding_evidence(self):
        cell = self.add("order", "FAKE_PROTOCOL")
        run = self.execute(cell)
        self.assertEqual(run["status"], "failed")
        self.assertIn("工具协议兼容性", run["error"])
        raw = run["outputs"][0]["text"]
        self.assertIn("DSML", raw)
        self.assertEqual(Path(run["artifact"]).read_text(encoding="utf-8"), raw)
        self.assertEqual(run["output_sha256"], hashlib.sha256(raw.encode()).hexdigest())
        next_cell = self.add("order", "Review previous evidence")
        context = self.engine.context(self.doc["id"], next_cell["id"])
        self.assertNotIn("DSML", context["prompt"])
        self.assertFalse(context["sections"][-1]["has_valid_output"])
        self.assertEqual(self.engine.events(run["id"])[-1]["data"]["status"], "failed")

    def test_incomplete_provider_responses_are_not_success(self):
        for source, mode, error in (("FAKE_TRUNCATED", "llm", "尚未完成"),
                                    ("FAKE_PENDING_TOOL", "agent", "尚未完成"),
                                    ("FAKE_SUBTYPE", "llm", "error_max_turns"),
                                    ("FAKE_EMPTY", "llm", "空结论")):
            with self.subTest(source=source):
                run = self.execute(self.add("order", source, mode=mode))
                self.assertEqual(run["status"], "failed")
                self.assertIn(error, run["error"])

    def test_permission_denials_fail_and_preserve_the_original_answer(self):
        run = self.execute(self.add("order", "FAKE_DENIED_TOOL"))
        self.assertEqual(run["status"], "failed")
        self.assertIn("权限被拒绝", run["error"])
        self.assertTrue(run["outputs"])
        self.assertEqual(run["provider_result"]["permission_denials"], [{"tool_name": "Read"}])

    def test_recovery_corrects_old_protocol_success_with_audit_and_downstream_invalidation(self):
        failed = self.execute(self.add("order", "FAKE_PROTOCOL"))
        downstream = self.execute(self.add("markdown", "Historical downstream output"))
        self.engine.close()
        failed.update(status="succeeded", error=None)
        self.store.put_run(failed)
        doc = self.store.get(self.doc["id"])
        doc["cells"][0].update(status="succeeded", last_error=None)
        self.store.put(doc)
        raw_before = Path(failed["artifact"]).read_bytes()
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        corrected = self.engine.get_run(failed["id"])
        self.assertEqual(corrected["status"], "failed")
        self.assertEqual(corrected["validation"]["previous_status"], "succeeded")
        self.assertEqual(corrected["finished_at"], failed["finished_at"])
        self.assertEqual(Path(failed["artifact"]).read_bytes(), raw_before)
        saved = self.store.get(doc["id"])
        self.assertEqual(saved["cells"][0]["status"], "failed")
        self.assertFalse(saved["cells"][0]["stale"])
        self.assertTrue(saved["cells"][1]["stale"])
        self.assertEqual(saved["cells"][1]["last_run_id"], downstream["id"])
        events = self.engine.events(failed["id"])
        self.assertEqual(events[-2]["type"], "validation")
        self.assertEqual(events[-1]["data"]["status"], "failed")
        self.engine.close()
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        self.assertEqual(self.engine.events(failed["id"]), events)

    def test_contract_and_missing_provider_fail_before_run(self):
        cell = self.add("order", "task")
        doc = self.store.get(self.doc["id"])
        doc["cells"][-1]["expected_output"] = ""
        self.store.put(doc)
        with self.assertRaises(Problem):
            self.engine.start(doc["id"], cell["id"])
        self.engine.claude_command = []
        doc["cells"][-1]["expected_output"] = "Markdown"
        self.store.put(doc)
        with self.assertRaises(Problem) as error:
            self.engine.start(doc["id"], cell["id"])
        self.assertEqual(error.exception.status, 503)
        self.assertEqual(self.engine.active, {})

    def test_save_embeds_local_markdown_images_and_keeps_them_without_resending_bytes(self):
        figure = b"\x89PNG\r\n\x1a\n"
        (self.project / "figure.png").write_bytes(figure)
        cell = self.add("markdown", "![图](figure.png)")
        saved = self.engine.save(self.doc["id"], self.engine.document(self.doc["id"]))
        saved_cell = next(item for item in saved["cells"] if item["id"] == cell["id"])
        name = next(iter(saved_cell["attachments"]))
        self.assertEqual(saved_cell["source"], f"![图](attachment:{name})")
        self.assertEqual(base64.b64decode(saved_cell["attachments"][name]["image/png"]), figure)
        # Later saves keep the stored attachment; the client never sends image bytes.
        payload = self.engine.document(self.doc["id"])
        again = self.engine.save(self.doc["id"], payload)
        again_cell = next(item for item in again["cells"] if item["id"] == cell["id"])
        self.assertEqual(again_cell["source"], saved_cell["source"])
        self.assertEqual(again_cell["attachments"], saved_cell["attachments"])
        # Removing the image reference prunes the embedded bytes.
        payload = self.engine.document(self.doc["id"])
        next(item for item in payload["cells"] if item["id"] == cell["id"])["source"] = "没有图片"
        pruned = self.engine.save(self.doc["id"], payload)
        self.assertNotIn("attachments", next(item for item in pruned["cells"] if item["id"] == cell["id"]))

    def test_serial_execution_locks_running_cell_but_allows_other_cell_edits(self):
        upstream = self.add("code", "value = 1")
        cell = self.add("order", "FAKE_FILE_WAIT")
        downstream = self.add("code", "next_value = 1")
        run = self.engine.start(self.doc["id"], cell["id"])
        self.assertEqual(run["document_revision"], self.store.get(self.doc["id"])["revision"])
        with self.assertRaises(Problem):
            self.engine.start(self.doc["id"], cell["id"])

        payload = self.engine.document(self.doc["id"])
        payload["cells"][0]["source"] = "value = 2"
        payload["cells"][2]["source"] = "next_value = 2"
        saved = self.engine.save(self.doc["id"], payload)
        self.assertEqual(saved["cells"][0]["source"], "value = 2")
        self.assertEqual(saved["cells"][2]["source"], "next_value = 2")
        self.assertEqual(saved["runtime"]["active_run_id"], run["id"])

        protected = copy.deepcopy(saved)
        protected["cells"][1]["source"] = "change the running input"
        with self.assertRaises(Problem) as running_edit:
            self.engine.save(self.doc["id"], protected)
        self.assertEqual(running_edit.exception.status, 409)

        reordered = copy.deepcopy(saved)
        reordered["cells"].reverse()
        with self.assertRaises(Problem) as structural_edit:
            self.engine.save(self.doc["id"], reordered)
        self.assertEqual(structural_edit.exception.status, 409)

        result = wait_run(self.engine, run["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["stale"])
        self.assertIn("上游单元", result["stale_reason"])
        final = self.engine.document(self.doc["id"])
        self.assertEqual(final["cells"][0]["source"], "value = 2")
        self.assertEqual(final["cells"][1]["source"], cell["source"])
        self.assertEqual(final["cells"][2]["source"], "next_value = 2")

    def test_order_cells_run_in_parallel_and_allow_other_cell_edits(self):
        first = self.add("order", "FAKE_FILE_WAIT")
        second = self.add("order", "FAKE_FILE_WAIT")
        code = self.add("code", "value = 1")
        run_a = self.engine.start(self.doc["id"], first["id"])
        run_b = self.engine.start(self.doc["id"], second["id"])
        self.assertEqual(self.engine.get_run(run_a["id"])["status"], "running")
        self.assertEqual(self.engine.get_run(run_b["id"])["status"], "running")
        runtime = self.engine.runtime(self.doc["id"])
        self.assertEqual(runtime["state"], "busy")
        self.assertEqual({item["id"] for item in runtime["active_runs"]}, {run_a["id"], run_b["id"]})
        with self.assertRaises(Problem) as same_cell:
            self.engine.start(self.doc["id"], first["id"])
        self.assertEqual(same_cell.exception.status, 409)
        with self.assertRaises(Problem) as code_blocked:
            self.engine.start(self.doc["id"], code["id"])
        self.assertEqual(code_blocked.exception.status, 409)

        payload = self.engine.document(self.doc["id"])
        payload["cells"][2]["source"] = "value = 2"
        saved = self.engine.save(self.doc["id"], payload)
        self.assertEqual(saved["cells"][2]["source"], "value = 2")
        protected = copy.deepcopy(saved)
        protected["cells"][0]["source"] = "change the running input"
        with self.assertRaises(Problem) as running_edit:
            self.engine.save(self.doc["id"], protected)
        self.assertEqual(running_edit.exception.status, 409)

        self.assertEqual(wait_run(self.engine, run_a["id"])["status"], "succeeded")
        self.assertEqual(wait_run(self.engine, run_b["id"])["status"], "succeeded")
        self.assertEqual(self.engine.runtime(self.doc["id"])["active_runs"], [])
        final = self.engine.document(self.doc["id"])
        self.assertEqual(final["cells"][2]["source"], "value = 2")

    def test_code_execution_stays_exclusive_against_orders(self):
        code = self.add("code", "import time\ntime.sleep(1)\nx = 1")
        order = self.add("order", "FAKE_FILE_WAIT")
        run = self.engine.start(self.doc["id"], code["id"])
        with self.assertRaises(Problem) as order_blocked:
            self.engine.start(self.doc["id"], order["id"])
        self.assertEqual(order_blocked.exception.status, 409)
        another = self.add("code", "y = 2")
        with self.assertRaises(Problem) as code_blocked:
            self.engine.start(self.doc["id"], another["id"])
        self.assertEqual(code_blocked.exception.status, 409)
        self.assertEqual(wait_run(self.engine, run["id"])["status"], "succeeded")
        self.assertEqual(self.execute(order)["status"], "succeeded")

    def test_global_is_editable_during_run_without_changing_snapshot(self):
        cell = self.add("code", "import time\ntime.sleep(.5)\nx=42")
        initial = self.store.get(self.doc["id"])
        initial["global"]["goal"] = "ORIGINAL_GOAL"
        self.store.put(initial)
        run = self.engine.start(self.doc["id"], cell["id"])
        changed = self.engine.save_global(self.doc["id"], {"revision": 1, "goal": "LIVE_GOAL", "constraints": "New rules"})
        self.assertEqual(changed["global"]["goal"], "LIVE_GOAL")
        self.assertEqual(changed["runtime"]["active_run_id"], run["id"])
        result = wait_run(self.engine, run["id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertTrue(result["stale"])
        self.assertIn("ORIGINAL_GOAL", result["context"]["prompt"])
        self.assertNotIn("LIVE_GOAL", result["context"]["prompt"])
        saved = self.store.get(self.doc["id"])
        self.assertEqual(saved["global"]["goal"], "LIVE_GOAL")
        self.assertTrue(saved["cells"][0]["stale"])
        with self.assertRaises(Problem):
            self.engine.save_global(self.doc["id"], {"revision": 1, "goal": "conflict"})

    def test_unchanged_global_does_not_create_new_revision(self):
        doc = self.store.get(self.doc["id"])
        unchanged = self.engine.save_global(doc["id"], {"revision": doc["global"]["revision"], "goal": doc["global"]["goal"], "constraints": doc["global"]["constraints"]})
        self.assertEqual(unchanged["revision"], doc["revision"])


    def test_cancellation_resets_kernel_and_preserves_history(self):
        first = self.add("code", "x=7")
        self.execute(first)
        cell = self.add("code", "import time\nprint('started')\ntime.sleep(30)")
        run = self.engine.start(self.doc["id"], cell["id"])
        time.sleep(.15)
        self.engine.cancel(run["id"])
        run = wait_run(self.engine, run["id"])
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(self.engine.runtime(self.doc["id"])["state"], "cold")
        self.assertTrue(self.store.get(self.doc["id"])["cells"][0]["stale"])
        self.assertTrue(Path(run["artifact"]).is_file())

    def test_timeout_resets_and_allows_next_execution(self):
        self.engine.code_timeout = .25
        run = self.execute(self.add("code", "import time\ntime.sleep(10)"))
        self.assertEqual(run["status"], "failed")
        self.assertIn("超过", run["error"])
        self.engine.code_timeout = 5
        next_run = self.execute(self.add("code", "1+1"))
        self.assertEqual(next_run["status"], "succeeded")

    def test_agent_cancellation_and_timeout(self):
        cell = self.add("order", "FAKE_WAIT")
        run = self.engine.start(self.doc["id"], cell["id"])
        self.engine.cancel(run["id"])
        self.assertEqual(wait_run(self.engine, run["id"])["status"], "cancelled")
        self.engine.agent_timeout = .2
        run = self.execute(cell)
        self.assertEqual(run["status"], "failed")
        self.assertIn("超过", run["error"])

    def test_reset_and_service_restart_never_claim_warm_variables(self):
        cell = self.add("code", "x=1")
        run = self.execute(cell)
        doc = self.engine.reset(self.doc["id"])
        self.assertEqual(doc["runtime"]["variables"], [])
        self.assertTrue(doc["cells"][0]["stale"])
        self.execute(cell)
        self.engine.close()
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        self.assertEqual(self.engine.runtime(doc["id"])["state"], "cold")
        self.assertTrue(self.store.get(doc["id"])["cells"][0]["stale"])
        self.assertEqual(self.engine.get_run(run["id"])["status"], "succeeded")

    def test_initialization_builds_manual_order_with_project_history_prompt(self):
        doc = initialization_notebook(scan_project(self.project))
        self.assertEqual([c["type"] for c in doc["cells"]], ["markdown", "order"])
        order = doc["cells"][-1]
        self.assertEqual(order["mode"], "agent")
        self.assertEqual(order["output_format"], "markdown")
        self.assertNotIn("initializer", order)
        self.assertIn(doc["project_path"], order["source"])
        self.assertIn("~/.claude/projects/", order["source"])
        self.assertIn("≤5 个 AI Order 板块", order["source"])
        self.assertTrue(order["expected_output"].strip())


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.app = App(cls.temp.name, claude_command=FAKE, seed=False, claude_launch_mode="print")
        cls.server = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.app.close()
        cls.server.server_close()
        cls.temp.cleanup()

    def request(self, method, path, data=None, headers=None, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        effective = {"Content-Type": "application/json"}
        if token:
            effective["X-Castle-Token"] = self.app.token
        effective.update(headers or {})
        connection.request(method, path, None if data is None else json.dumps(data), effective)
        response = connection.getresponse()
        body = response.read()
        result = (response.status, dict(response.getheaders()), body)
        connection.close()
        return result

    def test_parallel_order_runs_over_http_with_live_edits(self):
        doc = new_notebook(self.temp.name)
        first = new_cell("order", "并行一", "FAKE_FILE_WAIT", "Markdown")
        second = new_cell("order", "并行二", "FAKE_FILE_WAIT", "Markdown")
        code = new_cell("code", "验证", "value = 1", "预期")
        doc["cells"] = [first, second, code]
        self.app.store.put(doc)
        base = f"/api/notebooks/{doc['id']}"
        status, _, body = self.request("POST", f"{base}/cells/{first['id']}/run", {})
        self.assertEqual(status, 202)
        run_a = json.loads(body)
        status, _, body = self.request("POST", f"{base}/cells/{second['id']}/run", {})
        self.assertEqual(status, 202)
        run_b = json.loads(body)
        status, _, body = self.request("GET", base)
        runtime = json.loads(body)["runtime"]
        self.assertEqual({item["id"] for item in runtime["active_runs"]}, {run_a["id"], run_b["id"]})
        status, _, body = self.request("POST", f"{base}/cells/{code['id']}/run", {})
        self.assertEqual(status, 409)
        status, _, body = self.request("GET", base)
        payload = json.loads(body)
        payload["cells"][2]["source"] = "value = 2"
        status, _, body = self.request("PUT", base, payload)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["cells"][2]["source"], "value = 2")
        for rid in (run_a["id"], run_b["id"]):
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                status, _, body = self.request("GET", f"/api/runs/{rid}")
                run = json.loads(body)
                if run["status"] != "running":
                    break
                time.sleep(0.05)
            self.assertEqual(run["status"], "succeeded")
        status, _, body = self.request("GET", base)
        final = json.loads(body)
        self.assertEqual(final["runtime"]["active_runs"], [])
        self.assertEqual(final["cells"][2]["source"], "value = 2")

    def test_project_initialize_creates_notebook_without_running(self):
        status, _, body = self.request("POST", "/api/project/initialize", {"relative_path": "."})
        self.assertEqual(status, 201)
        payload = json.loads(body)
        self.assertNotIn("run", payload)
        doc = payload["notebook"]
        order = doc["cells"][-1]
        self.assertEqual(order["type"], "order")
        self.assertEqual(order["status"], "idle")
        self.assertIn(doc["project_path"], order["source"])
        self.assertEqual(doc["runtime"]["active_runs"], [])

    def test_bootstrap_static_and_security_headers(self):
        status, headers, body = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["token"], self.app.token)
        self.assertEqual(payload["context_config_path"], str(self.app.context_config_path))
        self.assertEqual(payload["settings"], {"path": str(self.app.settings_path),
                                               "confirm_order_run": True, "custom_sessions_dir": "", "error": None})
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"app.js", body)
        self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        self.assertIn("img-src 'self' data: https: http:", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_markdown_images_are_served_from_the_notebook_project(self):
        doc = new_notebook(self.temp.name)
        self.app.store.put(doc)
        image = Path(self.temp.name) / "markdown-figure.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n")
        endpoint = f"/api/notebooks/{doc['id']}/asset"
        status, headers, body = self.request("GET", endpoint + "?path=markdown-figure.png")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(body, image.read_bytes())
        for path in ("../markdown-figure.png", ".castle/notebooks.json", "missing.png"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", endpoint + f"?path={path}")
                self.assertEqual(status, 404)

    def test_math_bundle_is_served_at_the_paths_the_page_requests(self):
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "fonts").mkdir()
            (Path(root) / "katex.min.js").write_text("window.katex = {};", encoding="utf-8")
            (Path(root) / "fonts" / "KaTeX_Main-Regular.woff2").write_bytes(b"wOF2")
            previous = self.server.math
            self.server.math = MathAssets(root=root, files={
                "katex.min.js": "unused", "fonts/KaTeX_Main-Regular.woff2": "unused"})
            try:
                status, headers, body = self.request("GET", "/api/math/katex.min.js")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "text/javascript; charset=utf-8")
                self.assertIn(b"window.katex", body)
                status, headers, _ = self.request("GET", "/api/math/fonts/KaTeX_Main-Regular.woff2")
                self.assertEqual(status, 200)
                self.assertEqual(headers["Content-Type"], "font/woff2")
                self.assertIsNone(self.server.math.path("../server.py"))
            finally:
                self.server.math = previous

    def test_context_preview_hot_loads_config_and_reports_invalid_edits(self):
        doc = new_notebook(self.temp.name)
        cell = new_cell("order", "Configured prompt", "Current input", "Markdown")
        doc["cells"].append(cell)
        self.app.store.put(doc)
        path = self.app.context_config_path
        original = path.read_bytes()
        endpoint = f"/api/notebooks/{doc['id']}/cells/{cell['id']}/context"
        try:
            path.write_text(json.dumps({"features": {"variables": False},
                                        "templates": {"instructions": "HTTP_CUSTOM_HARNESS\n"}}), encoding="utf-8")
            status, _, body = self.request("GET", endpoint)
            self.assertEqual(status, 200)
            context = json.loads(body)
            self.assertIn("HTTP_CUSTOM_HARNESS", context["prompt"])
            self.assertNotIn("Live Python variables", context["prompt"])
            self.assertFalse(context["config"]["features"]["variables"])
            path.write_text("{invalid JSON", encoding="utf-8")
            status, _, body = self.request("GET", endpoint)
            self.assertEqual(status, 400)
            self.assertIn("context.json", json.loads(body)["error"])
        finally:
            path.write_bytes(original)

    def test_output_file_polling_export_and_input_save_use_latest_file(self):
        doc = new_notebook(self.temp.name, "Live output integration")
        cell = new_cell("order", "File output", "FAKE_WRITE_FILE", "Markdown")
        doc["cells"].append(cell)
        self.app.store.put(doc)
        run = wait_run(self.app.engine, self.app.engine.start(doc["id"], cell["id"])["id"])
        endpoint = f"/api/notebooks/{doc['id']}"
        status, _, body = self.request("GET", endpoint + "/outputs")
        self.assertEqual(status, 200)
        first = json.loads(body)
        self.assertIn("第一版", first["cells"][0]["outputs"][0]["text"])
        self.assertEqual(first["cells"][0]["file_output"]["run_id"], run["id"])
        status, _, body = self.request("GET", endpoint + "/outputs?after=" + first["fingerprint"])
        self.assertFalse(json.loads(body)["changed"])
        _, _, body = self.request("GET", endpoint)
        editable = json.loads(body)
        path = Path(run["artifact"])
        replacement = path.with_suffix(".replacement")
        replacement.write_text("# HTTP live replacement\n\n| A | B |\n|---|---|\n| 1 | 2 |", encoding="utf-8")
        replacement.replace(path)
        status, _, body = self.request("GET", endpoint + "/outputs?after=" + first["fingerprint"])
        self.assertTrue(json.loads(body)["changed"])
        self.assertIn("HTTP live replacement", json.loads(body)["cells"][0]["outputs"][0]["text"])
        editable["title"] = "Edited while file updates"
        status, _, body = self.request("PUT", endpoint, editable)
        self.assertEqual(status, 200)
        self.assertIn("HTTP live replacement", json.loads(body)["cells"][0]["outputs"][0]["text"])
        path.write_text("# Export reads the file directly", encoding="utf-8")
        status, _, body = self.request("GET", endpoint + "/export")
        self.assertEqual(json.loads(body)["cells"][0]["outputs"][0]["text"], "# Export reads the file directly")
        status, _, body = self.request("GET", f"/api/runs/{run['id']}")
        self.assertIn("第一版", json.loads(body)["outputs"][0]["text"])
        self.assertEqual(self.request("GET", "/api/notebooks/invalid/outputs")[0], 404)

    def test_csrf_origin_and_dns_rebinding_rejected(self):
        self.assertEqual(self.request("POST", "/api/notebooks", {}, token=False)[0], 403)
        self.assertEqual(self.request("POST", "/api/notebooks", {}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(self.request("GET", "/api/bootstrap", headers={"Host": "evil.example"})[0], 403)

    def test_paths_and_non_object_body_rejected(self):
        self.assertEqual(self.request("GET", "/../pyproject.toml")[0], 404)
        self.assertEqual(self.request("GET", "/api/runs/../../artifact?name=context")[0], 404)
        self.assertEqual(self.request("POST", "/api/notebooks", [1, 2])[0], 400)

    def test_notebook_project_binding_file_preview_and_artifacts(self):
        project = Path(self.temp.name) / "bound_project"
        project.mkdir()
        (project / "project_model.py").write_text("value = 42", encoding="utf-8")
        (Path(self.temp.name) / "root_only.py").write_text("outside the bound project", encoding="utf-8")
        status, _, body = self.request("POST", "/api/notebooks", {"title": "Project interface", "relative_path": "bound_project"})
        self.assertEqual(status, 201)
        doc = json.loads(body)
        self.assertEqual(Path(doc["project_path"]), project)
        endpoint = f"/api/notebooks/{doc['id']}"
        status, _, body = self.request("GET", endpoint + "/project")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["files"], ["project_model.py"])
        status, _, body = self.request("GET", endpoint + "/project/file?path=project_model.py")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["source"], "value = 42")
        self.assertEqual(self.request("GET", endpoint + "/project/file?path=..%2Froot_only.py")[0], 404)
        self.assertEqual(self.request("GET", endpoint + "/project/file?path=.castle%2Fcontext.json")[0], 404)
        self.assertEqual(self.request("POST", "/api/notebooks", {"relative_path": ".."})[0], 400)
        cell = new_cell("code", "Verify project module", "from project_model import value\nassert value == 42\nvalue")
        doc["cells"].append(cell)
        self.assertEqual(self.request("PUT", endpoint, doc)[0], 200)
        run = wait_run(self.app.engine, self.app.engine.start(doc["id"], cell["id"])["id"])
        self.assertEqual(run["status"], "succeeded")
        self.assertEqual(run["outputs"][-1]["text"], "42")
        status, _, body = self.request("GET", f"/api/runs/{run['id']}/artifact?name=changes")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["files"], [])

    def test_open_session_requires_token_and_uses_saved_name_and_project(self):
        _, _, body = self.request("POST", "/api/notebooks", {"title": "Session integration"})
        doc = json.loads(body)
        cell = new_cell("order", "Saved session", "Inspect evidence", "Markdown conclusion")
        doc["cells"].append(cell)
        endpoint = f"/api/notebooks/{doc['id']}"
        self.assertEqual(self.request("PUT", endpoint, doc)[0], 200)
        run = wait_run(self.app.engine, self.app.engine.start(doc["id"], cell["id"])["id"])
        open_path = endpoint + f"/cells/{cell['id']}/open-session"
        with patch("castle_notebook.execution.open_claude_terminal") as launch:
            self.assertEqual(self.request("POST", open_path, {}, token=False)[0], 403)
            self.assertEqual(self.request("POST", open_path, {}, {"Origin": "https://evil.example"})[0], 403)
            self.assertEqual(self.request("GET", open_path)[0], 404)
            launch.assert_not_called()
            status, _, body = self.request("POST", open_path, {"session_name": "other", "session_id": "other", "project_path": "other"})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), {"session_name": run["session_name"]})
            launch.assert_called_once_with([*FAKE, "--resume", run["session_id"], "--permission-mode", "auto"], self.temp.name)
        _, _, body = self.request("GET", endpoint)
        self.assertTrue(json.loads(body)["cells"][-1]["session"]["available"])

    @unittest.skipUnless(sys.platform == "win32", "Windows console launcher")
    def test_open_session_terminal_failure_is_reported(self):
        doc = new_notebook(self.temp.name)
        cell = new_cell("order", "Launch failure", "Inspect evidence", "Markdown conclusion")
        doc["cells"].append(cell)
        self.app.store.put(doc)
        wait_run(self.app.engine, self.app.engine.start(doc["id"], cell["id"])["id"])
        with patch("castle_notebook.execution.subprocess.Popen", side_effect=FileNotFoundError("Claude unavailable")):
            status, _, body = self.request("POST", f"/api/notebooks/{doc['id']}/cells/{cell['id']}/open-session", {})
        self.assertEqual(status, 503)
        self.assertIn("无法打开 AI 对话终端", json.loads(body)["error"])

    def test_notebook_save_run_export_and_resumable_sse(self):
        status, _, body = self.request("POST", "/api/notebooks", {"title": "HTTP integration"})
        self.assertEqual(status, 201)
        doc = json.loads(body)
        cell = new_cell("code", "real code", "x=6\nx*7")
        doc["cells"].append(cell)
        endpoint = f"/api/notebooks/{doc['id']}"
        status, _, body = self.request("PUT", endpoint, doc)
        self.assertEqual(status, 200)
        self.assertEqual(self.request("PUT", endpoint, doc)[0], 409)
        status, _, body = self.request("POST", endpoint + f"/cells/{cell['id']}/run", {})
        self.assertEqual(status, 202)
        run = json.loads(body)
        status, _, body = self.request("GET", f"/api/runs/{run['id']}/events")
        self.assertEqual(status, 200)
        self.assertIn(b'"finished"', body)
        self.assertIn(b'"text": "42"', body)
        status, _, body = self.request("GET", f"/api/runs/{run['id']}/events", headers={"Last-Event-ID": "1"})
        self.assertNotIn(b"id: 1\n", body)
        self.assertIn(b'"finished"', body)
        status, _, body = self.request("GET", endpoint + "/export")
        self.assertEqual(json.loads(body)["cells"][0]["status"], "succeeded")
        status, _, body = self.request("GET", f"/api/runs/{run['id']}/artifact?name=output")
        self.assertEqual(body.decode(), "42")
        self.assertEqual(self.request("GET", f"/api/runs/{run['id']}/artifact?name=../../secret")[0], 404)


if __name__ == "__main__":
    unittest.main()
