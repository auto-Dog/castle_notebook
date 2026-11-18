"""Mutable conversation requirements stay separate from immutable run inputs."""
from __future__ import annotations

import copy
import hashlib
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from castle_notebook.core import Problem, Store, new_cell, new_notebook
from castle_notebook.execution import Engine
from castle_notebook.server import App, Server

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]


def wait_run(engine, rid):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        run = engine.get_run(rid)
        if run["status"] != "running":
            return run
        time.sleep(.02)
    raise AssertionError(f"Run did not finish: {rid}")


class OrderInputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.store = Store(self.project / ".castle")
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        self.doc = new_notebook(self.project)
        self.store.put(self.doc)

    def tearDown(self):
        self.engine.close()
        for rid in list(self.engine.runs):
            wait_run(self.engine, rid)
        self.temp.cleanup()

    def add(self, source="检查现有问题"):
        cell = new_cell("order", "当前需求", source, "Markdown")
        doc = self.store.get(self.doc["id"])
        doc["cells"].append(cell)
        self.store.put(doc)
        return cell

    def execute(self, cell):
        run = wait_run(self.engine, self.engine.start(self.doc["id"], cell["id"])["id"])
        self.assertEqual(run["status"], "succeeded", run.get("error"))
        return run

    def input_path(self, run):
        return self.project / run["input_file"]

    def test_seed_and_model_replacement_preserve_original_prompt_and_input_snapshot(self):
        cell = self.add('FAKE_UPDATE_INPUT\n含有 "引号"、{JSON} 和换行')
        preview = self.engine.context(self.doc["id"], cell["id"])
        self.assertEqual(preview["run"]["input_file"], f".castle/runs/{preview['run']['run_id']}/input.md")
        self.assertIn(f"需求摘要文件为 `{preview['run']['input_file']}`", preview["prompt"])
        run = self.execute(cell)
        self.assertEqual(run["context"], preview)
        self.assertEqual(run["input_snapshot"]["source"], cell["source"])
        self.assertEqual(run["initial_input_sha256"], hashlib.sha256(cell["source"].encode()).hexdigest())
        current = self.engine.document(self.doc["id"])["cells"][0]
        self.assertIn("新增可编辑上下文模板", current["source"])
        self.assertNotIn("FAKE_UPDATE_INPUT", current["source"])
        self.assertEqual(self.input_path(run).read_text(encoding="utf-8"), current["source"])
        self.assertEqual(current["revision"], cell["revision"] + 1)
        for path in (Path(run["prompt_file"]), self.store.run_path(run["id"]) / "context.md"):
            self.assertEqual(path.read_bytes(), preview["prompt"].encode())
        self.input_path(run).write_text("下一轮已调整需求", encoding="utf-8")
        self.engine.document(self.doc["id"])
        self.assertEqual(self.engine.get_run(run["id"])["input_snapshot"], run["input_snapshot"])
        self.assertEqual(Path(run["prompt_file"]).read_bytes(), preview["prompt"].encode())

    def test_external_replacement_refreshes_context_downstream_cache_and_poll_fingerprint(self):
        first = self.execute(self.add())
        later_cell = self.add("根据上游需求做后续验证")
        self.execute(later_cell)
        before = self.engine.document(self.doc["id"])
        poll = self.engine.order_outputs(self.doc["id"])
        self.input_path(first).write_bytes(b"\xef\xbb\xbf# Revised requirements\r\n\r\n- [x] Done\r\n- [ ] Pending")
        after = self.engine.order_outputs(self.doc["id"], poll["fingerprint"])
        self.assertTrue(after["changed"])
        item = after["cells"][0]
        self.assertEqual(item["source"], "# Revised requirements\n\n- [x] Done\n- [ ] Pending")
        self.assertEqual(item["file_input"]["state"], "ready")
        self.assertEqual(item["revision"], before["cells"][0]["revision"] + 1)
        saved = self.store.get(self.doc["id"])
        self.assertEqual(saved["revision"], before["revision"])
        self.assertEqual(saved["cells"][0]["source"], item["source"])
        self.assertIn(later_cell["id"], after["stale_cell_ids"])
        self.assertIn("Revised requirements", self.engine.context(self.doc["id"], later_cell["id"])["prompt"])
        self.assertFalse(self.engine.order_outputs(self.doc["id"], after["fingerprint"])["changed"])

    def test_input_changes_during_downstream_execution_invalidate_result(self):
        first = self.execute(self.add())
        later = self.add("FAKE_FILE_WAIT")
        started = self.engine.start(self.doc["id"], later["id"])
        self.input_path(first).write_text("执行期间修改上游需求", encoding="utf-8")
        self.engine.order_outputs(self.doc["id"])
        run = wait_run(self.engine, started["id"])
        self.assertTrue(run["stale"])
        self.assertIn("input.md", run["stale_reason"])

    def test_rejected_upstream_input_save_does_not_invalidate_the_running_snapshot(self):
        upstream = self.execute(self.add("Original upstream requirements"))
        started = self.engine.start(self.doc["id"], self.add("FAKE_FILE_WAIT")["id"])
        payload = self.engine.document(self.doc["id"])
        payload["cells"][0].update(source="Unsaved upstream draft", input_edited=True)
        read_input = self.engine.read_input_file
        upstream_reads = 0

        def become_unavailable(rid, project):
            nonlocal upstream_reads
            if rid == upstream["id"]:
                upstream_reads += 1
                if upstream_reads > 1:
                    return {"state": "unavailable"}, None
            return read_input(rid, project)

        with patch.object(self.engine, "read_input_file", side_effect=become_unavailable):
            with self.assertRaises(Problem) as conflict:
                self.engine.save(self.doc["id"], payload)
        self.assertEqual(conflict.exception.status, 409)
        final = wait_run(self.engine, started["id"])
        self.assertFalse(final["stale"])
        self.assertEqual(self.input_path(upstream).read_text(encoding="utf-8"), "Original upstream requirements")

    def test_editor_saves_update_file_and_unrelated_autosave_keeps_external_input(self):
        run = self.execute(self.add())
        payload = self.engine.document(self.doc["id"])
        payload["cells"][0].update(source="## 手动编辑的新需求", input_edited=True)
        saved = self.engine.save(self.doc["id"], payload)
        self.assertEqual(self.input_path(run).read_text(encoding="utf-8"), "## 手动编辑的新需求")
        self.assertEqual(saved["cells"][0]["source"], "## 手动编辑的新需求")
        stale_payload = copy.deepcopy(saved)
        stale_payload["title"] = "无关的标题修改"
        stale_payload["cells"][0]["input_edited"] = False
        self.input_path(run).write_text("模型续聊替换后的完整需求", encoding="utf-8")
        result = self.engine.save(self.doc["id"], stale_payload)
        self.assertEqual(result["title"], stale_payload["title"])
        self.assertEqual(result["cells"][0]["source"], "模型续聊替换后的完整需求")
        self.assertEqual(self.input_path(run).read_text(encoding="utf-8"), result["cells"][0]["source"])

    def test_file_hash_conflict_keeps_both_versions_and_explicit_rebase_can_replace(self):
        run = self.execute(self.add())
        draft = self.engine.document(self.doc["id"])
        draft["cells"][0].update(source="用户编辑草稿", input_edited=True)
        self.input_path(run).write_text("模型新增需求", encoding="utf-8")
        with self.assertRaises(Problem) as conflict:
            self.engine.save(self.doc["id"], draft)
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(draft["cells"][0]["source"], "用户编辑草稿")
        self.assertEqual(self.input_path(run).read_text(encoding="utf-8"), "模型新增需求")
        latest = self.engine.document(self.doc["id"])
        draft["cells"][0]["input_file_sha256"] = latest["cells"][0]["file_input"]["sha256"]
        result = self.engine.save(self.doc["id"], draft)
        self.assertEqual(result["cells"][0]["source"], "用户编辑草稿")
        self.assertEqual(self.input_path(run).read_text(encoding="utf-8"), "用户编辑草稿")
        # Invalid documents are rejected before any requirement file is written.
        invalid = copy.deepcopy(result)
        invalid["cells"][0].update(source="不可写入", input_edited=True, output_format="invalid")
        with self.assertRaises(Problem):
            self.engine.save(self.doc["id"], invalid)
        self.assertEqual(self.input_path(run).read_text(encoding="utf-8"), "用户编辑草稿")

    def test_empty_deleted_oversized_and_restored_input_preserve_explicit_cache(self):
        run = self.execute(self.add())
        path = self.input_path(run)
        path.write_text("", encoding="utf-8")
        self.assertEqual(self.engine.document(self.doc["id"])["cells"][0]["source"], "")
        path.write_text("最后可读的需求", encoding="utf-8")
        self.engine.document(self.doc["id"])
        path.unlink()
        missing = self.engine.document(self.doc["id"])["cells"][0]
        self.assertEqual(missing["source"], "最后可读的需求")
        self.assertTrue(missing["file_input"]["cached"])
        self.assertEqual(missing["file_input"]["state"], "missing")
        self.assertFalse(path.exists())
        path.write_text("大" * 33, encoding="utf-8")
        with patch("castle_notebook.execution.INPUT_LIMIT", 32):
            oversized = self.engine.document(self.doc["id"])["cells"][0]
        self.assertEqual(oversized["file_input"]["state"], "unavailable")
        self.assertEqual(oversized["source"], "最后可读的需求")
        path.write_text("恢复后的需求", encoding="utf-8")
        self.assertEqual(self.engine.document(self.doc["id"])["cells"][0]["source"], "恢复后的需求")

    def test_latest_run_has_independent_input_and_old_session_files_cannot_replace_it(self):
        cell = self.add()
        old = self.execute(cell)
        self.input_path(old).write_text("续聊中演变的需求", encoding="utf-8")
        latest = self.execute(cell)
        self.assertEqual(latest["input_snapshot"]["source"], "续聊中演变的需求")
        self.assertEqual(self.input_path(latest).read_text(encoding="utf-8"), "续聊中演变的需求")
        self.input_path(old).write_text("旧会话后来的修改", encoding="utf-8")
        current = self.engine.document(self.doc["id"])["cells"][0]
        self.assertEqual(current["file_input"]["run_id"], latest["id"])
        self.assertEqual(current["source"], "续聊中演变的需求")

    def test_unrun_order_board_displays_external_trial_files_and_polls_changes(self):
        cell = self.add("运行前的原始需求")
        downstream = self.execute(self.add("下游验证"))
        board = self.store.board_path(cell["id"])
        board.mkdir(parents=True)
        (board / "input.md").write_text("# 外部试跑后的完整需求", encoding="utf-8")
        (board / "output.md").write_text("# 试跑结论\n\n第一版", encoding="utf-8")
        current = self.engine.document(self.doc["id"])["cells"][0]
        self.assertEqual(current["source"], "# 外部试跑后的完整需求")
        self.assertEqual(current["file_input"]["run_id"], cell["id"])
        self.assertEqual(current["file_input"]["state"], "ready")
        self.assertEqual(current["outputs"][0]["text"], "# 试跑结论\n\n第一版")
        self.assertEqual(current["file_output"]["run_id"], cell["id"])
        poll = self.engine.order_outputs(self.doc["id"])
        self.assertEqual(poll["cells"][0]["id"], cell["id"])
        (board / "output.md").write_text("# 试跑结论\n\n第二版", encoding="utf-8")
        after = self.engine.order_outputs(self.doc["id"], poll["fingerprint"])
        self.assertTrue(after["changed"])
        self.assertEqual(after["cells"][0]["outputs"][0]["text"], "# 试跑结论\n\n第二版")
        self.assertIn(downstream["cell_id"], after["stale_cell_ids"])
        self.assertFalse(self.engine.order_outputs(self.doc["id"], after["fingerprint"])["changed"])

    def test_unrun_board_directory_is_the_first_run_directory(self):
        cell = self.add("导入后的需求")
        board = self.store.board_path(cell["id"])
        board.mkdir(parents=True)
        (board / "input.md").write_text("导入后的需求", encoding="utf-8")
        (board / "output.md").write_text("旧证据", encoding="utf-8")
        preview = self.engine.context(self.doc["id"], cell["id"])
        self.assertEqual(preview["run"]["run_id"], cell["id"])
        self.assertEqual(preview["run"]["input_file"], f".castle/runs/{cell['id']}/input.md")
        self.assertEqual(preview["run"]["output_file"], f".castle/runs/{cell['id']}/output.md")
        run = self.execute(cell)
        self.assertEqual(run["id"], cell["id"])
        self.assertEqual(run["context"], preview)
        self.assertEqual((board / "output.prior.md").read_text(encoding="utf-8"), "旧证据")
        self.assertNotEqual(run["outputs"][0]["text"], "旧证据")
        self.assertEqual((board / "output.md").read_text(encoding="utf-8"), run["outputs"][0]["text"])
        rerun_preview = self.engine.context(self.doc["id"], cell["id"])
        self.assertNotEqual(rerun_preview["run"]["run_id"], cell["id"])

    def test_legacy_cells_board_migrates_into_runs_namespace(self):
        cell = self.add("迁移前的外部试跑需求")
        legacy = self.store.path / "cells" / cell["id"]
        legacy.mkdir(parents=True)
        (legacy / "input.md").write_text("# 旧展板需求", encoding="utf-8")
        (legacy / "output.md").write_text("# 旧展板结论", encoding="utf-8")
        reopened = Store(self.store.path)
        self.assertFalse((self.store.path / "cells").exists())
        board = reopened.board_path(cell["id"])
        self.assertEqual((board / "input.md").read_text(encoding="utf-8"), "# 旧展板需求")
        current = self.engine.document(self.doc["id"])["cells"][0]
        self.assertEqual(current["source"], "# 旧展板需求")
        self.assertEqual(current["outputs"][0]["text"], "# 旧展板结论")

    def test_unrun_order_editor_save_writes_board_input_file(self):
        cell = self.add("初始需求")
        payload = self.engine.document(self.doc["id"])
        payload["cells"][0].update(source="## 手写需求摘要", input_edited=True)
        saved = self.engine.save(self.doc["id"], payload)
        board = self.store.board_path(cell["id"])
        self.assertEqual((board / "input.md").read_text(encoding="utf-8"), "## 手写需求摘要")
        self.assertEqual(saved["cells"][0]["file_input"]["run_id"], cell["id"])
        self.assertEqual(saved["cells"][0]["file_input"]["state"], "ready")
        (board / "output.md").write_text("外部试跑结论", encoding="utf-8")
        current = self.engine.document(self.doc["id"])["cells"][0]
        self.assertEqual(current["outputs"][0]["text"], "外部试跑结论")
        self.assertEqual(current["source"], "## 手写需求摘要")

    def test_finished_run_mirrors_files_to_board_and_run_files_stay_canonical(self):
        cell = self.add("检查现有问题")
        # The first run reuses the board directory; a rerun gets its own folder,
        # which is what makes the board a mirror instead of the canonical file.
        self.execute(cell)
        run = self.execute(cell)
        board = self.store.board_path(cell["id"])
        self.assertEqual((board / "input.md").read_bytes(), self.input_path(run).read_bytes())
        self.assertEqual((board / "output.md").read_bytes(),
                         (self.store.run_path(run["id"]) / "output.md").read_bytes())
        (board / "output.md").write_text("展板上的外部改动", encoding="utf-8")
        current = self.engine.document(self.doc["id"])["cells"][0]
        self.assertEqual(current["file_output"]["run_id"], run["id"])
        self.assertNotIn("展板上的外部改动", current["outputs"][0]["text"])
        (self.store.run_path(run["id"]) / "output.md").write_text("运行目录的更新", encoding="utf-8")
        current = self.engine.document(self.doc["id"])["cells"][0]
        self.assertEqual(current["outputs"][0]["text"], "运行目录的更新")

    def test_legacy_run_is_seeded_once_and_resume_informs_only_its_session(self):
        cell = self.add()
        run = self.execute(cell)
        self.input_path(run).unlink()
        run.pop("input_file")
        self.store.put_run(run)
        self.engine.close()
        self.engine = Engine(self.store, claude_command=FAKE, claude_launch_mode="print")
        doc = self.store.get(self.doc["id"])
        doc["cells"][0].pop("file_input", None)
        self.store.put(doc)
        latest = self.engine.document(self.doc["id"])["cells"][0]
        path = self.store.run_path(run["id"]) / "input.md"
        self.assertEqual(path.read_text(encoding="utf-8"), latest["source"])
        with patch("castle_notebook.execution.open_claude_terminal") as launch:
            self.engine.open_session(self.doc["id"], cell["id"])
            command, project = launch.call_args.args
            self.assertEqual(command[:len(FAKE) + 4], [*FAKE, "--resume", run["session_id"], "--permission-mode", "auto"])
            self.assertIn(latest["file_input"]["path"], command[-1])
            self.assertNotIn(str(self.project), command[-1])
            self.assertIn("保留现有文件内容", command[-1])
            self.assertEqual(project, str(self.project))
            self.engine.open_session(self.doc["id"], cell["id"])
            self.assertEqual(launch.call_args.args[0], [*FAKE, "--resume", run["session_id"], "--permission-mode", "auto"])
        path.unlink()
        self.engine.document(self.doc["id"])
        self.assertFalse(path.exists(), "A deleted file must not be seeded repeatedly")


class OrderInputHTTPTests(unittest.TestCase):
    def test_poll_export_artifact_and_hash_conflict(self):
        with tempfile.TemporaryDirectory() as project:
            app = App(project, claude_command=FAKE, seed=False, claude_launch_mode="print")
            doc = new_notebook(project)
            cell = new_cell("order", "初始需求", "初始需求", "Markdown")
            doc["cells"].append(cell)
            app.store.put(doc)
            run = wait_run(app.engine, app.engine.start(doc["id"], cell["id"])["id"])
            server = Server(("127.0.0.1", 0), app)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()

            def request(method, path, data=None):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                connection.request(method, path, None if data is None else json.dumps(data),
                                   {"Content-Type": "application/json", "X-Castle-Token": app.token})
                response = connection.getresponse()
                result = response.status, response.read()
                connection.close()
                return result

            try:
                endpoint = f"/api/notebooks/{doc['id']}"
                payload = app.engine.document(doc["id"])
                path = Path(project) / run["input_file"]
                path.write_text("# 随对话更新的需求\n\n- [ ] 新功能", encoding="utf-8")
                status, body = request("GET", endpoint + "/outputs")
                self.assertEqual(status, 200)
                poll = json.loads(body)
                self.assertIn("新功能", poll["cells"][0]["source"])
                self.assertEqual(poll["cells"][0]["file_input"]["path"], run["input_file"])
                status, body = request("GET", endpoint + "/outputs?after=" + poll["fingerprint"])
                self.assertEqual(json.loads(body), {"changed": False, "fingerprint": poll["fingerprint"]})
                status, body = request("GET", endpoint + "/export")
                self.assertIn("新功能", json.loads(body)["cells"][0]["source"])
                status, body = request("GET", f"/api/runs/{run['id']}/artifact?name=input")
                self.assertEqual(body, path.read_bytes())
                payload["cells"][0].update(source="浏览器中的旧草稿", input_edited=True)
                status, _ = request("PUT", endpoint, payload)
                self.assertEqual(status, 409)
                self.assertIn("新功能", path.read_text(encoding="utf-8"))
            finally:
                server.shutdown()
                app.close()
                server.server_close()
                thread.join(timeout=5)
