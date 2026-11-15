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

from castle_notebook.cell_operations import parse_cells
from castle_notebook.core import Problem, Store, new_cell, new_notebook
from castle_notebook.execution import Engine
from castle_notebook.server import App, Server

FAKE = [sys.executable, str(Path(__file__).with_name("fake_claude.py"))]


def wait_run(engine, rid, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = engine.get_run(rid)
        if result["status"] != "running":
            return result
        time.sleep(.02)
    raise AssertionError(f"run {rid} did not finish")


class CellOperationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        self.store = Store(self.project / ".castle")
        self.engine = Engine(self.store, claude_command=FAKE)
        self.doc = new_notebook(self.project)
        self.store.put(self.doc)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def add(self, source="整理现有需求", kind="order", execute=False):
        cell = new_cell(kind, source[:30], source, "Markdown，保留验证结论")
        doc = self.store.get(self.doc["id"])
        doc["cells"].append(cell)
        self.store.put(doc)
        if execute:
            result = wait_run(self.engine, self.engine.start(doc["id"], cell["id"])["id"])
            self.assertEqual(result["status"], "succeeded", result.get("error"))
        return cell

    def transform(self, cell, kind, **payload):
        revision = self.store.get(self.doc["id"])["revision"]
        return self.engine.transform_cells(self.doc["id"], cell["id"], kind, {"revision": revision, **payload})

    def test_merge_rewrites_input_and_output_discards_old_chats_and_preserves_other_cells(self):
        before = self.add("保持上文", kind="markdown", execute=True)
        first = self.add("实现 Notebook 功能 FAKE_TRANSFORM_FILE", execute=True)
        middle = self.add("保留项目运行代码", kind="code")
        second = self.add("修复运行问题并接入上下文配置", execute=True)
        after = self.add("独立验证", execute=True)
        doc = self.engine.document(self.doc["id"])
        sources = [doc["cells"][1], doc["cells"][3]]
        originals = [self.engine.get_run(cell["run_ids"][-1]) for cell in sources]
        for index, run in enumerate(originals):
            Path(run["artifact"]).write_text(f"结论 {index}: 已验证功能，仍需检查边界。", encoding="utf-8")
        old_inputs = {run["prompt_file"]: Path(run["prompt_file"]).read_bytes() for run in originals}
        result = wait_run(self.engine, self.transform(second, "merge", other_cell_id=first["id"])["id"])
        self.assertEqual(result["status"], "succeeded", result["error"])
        self.assertEqual(result["cli_output"], "整理结果已写入文件。")
        doc = self.engine.document(doc["id"])
        self.assertEqual([cell["id"] for cell in doc["cells"] if cell["type"] != "order"], [before["id"], middle["id"]])
        merged = doc["cells"][1]
        self.assertEqual(result["applied_cell_ids"], [merged["id"]])
        self.assertIn(first["source"], merged["source"])
        self.assertIn(second["source"], merged["source"])
        self.assertIn("结论 0", merged["outputs"][0]["text"])
        self.assertIn("结论 1", merged["outputs"][0]["text"])
        self.assertFalse(merged["session"]["available"])
        self.assertTrue(doc["cells"][-1]["stale"])
        self.assertEqual(doc["cells"][-1]["id"], after["id"])
        self.assertFalse(doc["cells"][0]["stale"])
        self.assertTrue(set(merged["run_ids"]).isdisjoint({run["id"] for run in originals}))
        derived = self.engine.get_run(merged["last_run_id"])
        prompt = Path(derived["prompt_file"]).read_bytes()
        self.assertIn(merged["source"], prompt.decode("utf-8"))
        self.assertEqual(prompt, (self.store.run_path(derived["id"]) / "context.md").read_bytes())
        self.assertEqual(hashlib.sha256(prompt).hexdigest(), derived["context"]["fingerprint"])
        for path, data in old_inputs.items():
            self.assertEqual(Path(path).read_bytes(), data)
        for index, run in enumerate(originals):
            self.assertIn(f"结论 {index}", Path(run["artifact"]).read_text(encoding="utf-8"))

    def test_split_copies_history_into_two_independent_sessions_and_files_after_restart(self):
        source = self.add("先实现功能，再做独立验证", execute=True)
        after = self.add("后续步骤", execute=True)
        original_doc = self.engine.document(self.doc["id"])
        original = original_doc["cells"][0]
        original_run = self.engine.get_run(original["last_run_id"])
        original_output = Path(original_run["artifact"]).read_bytes()
        result = wait_run(self.engine, self.transform(source, "split")["id"])
        self.assertEqual(result["status"], "succeeded", result["error"])
        doc = self.engine.document(self.doc["id"])
        children = doc["cells"][:2]
        self.assertEqual(result["applied_cell_ids"], [cell["id"] for cell in children])
        self.assertEqual(doc["cells"][-1]["id"], after["id"])
        self.assertTrue(doc["cells"][-1]["stale"])
        self.assertNotEqual(children[0]["source"], children[1]["source"])
        self.assertEqual(len({cell["session"]["id"] for cell in children}), 2)
        self.assertNotIn(original["session"]["id"], {cell["session"]["id"] for cell in children})
        runs = [self.engine.get_run(cell["last_run_id"]) for cell in children]
        self.assertNotEqual(runs[0]["artifact"], runs[1]["artifact"])
        self.assertNotEqual(runs[0]["prompt_file"], runs[1]["prompt_file"])
        for child, run in zip(children, runs):
            self.assertEqual(child["run_ids"][:-1], original["run_ids"])
            self.assertEqual(run["command"][run["command"].index("--resume") + 1], original["session"]["id"])
            self.assertIn("--fork-session", run["command"])
            self.assertIn(run["output_file"], Path(run["prompt_file"]).read_text(encoding="utf-8"))
            self.assertIn(child["source"], Path(run["prompt_file"]).read_text(encoding="utf-8"))
            self.assertEqual(child["outputs"], original["outputs"])
        Path(runs[0]["artifact"]).write_text("只更新第一份对话的结论", encoding="utf-8")
        doc = self.engine.document(self.doc["id"])
        self.assertEqual(doc["cells"][0]["outputs"][0]["text"], "只更新第一份对话的结论")
        self.assertEqual(doc["cells"][1]["outputs"], original["outputs"])
        self.assertEqual(Path(original_run["artifact"]).read_bytes(), original_output)
        self.engine.close()
        self.engine = Engine(self.store, claude_command=FAKE)
        doc = self.engine.document(doc["id"])
        with patch("castle_notebook.execution.open_claude_terminal") as terminal:
            for child, run in zip(doc["cells"][:2], runs):
                self.assertTrue(child["session"]["available"])
                self.engine.open_session(doc["id"], child["id"])
                self.assertEqual(terminal.call_args.args, ([*FAKE, "--resume", run["session_id"]], str(self.project)))

    def test_split_unrun_cell_has_two_saved_inputs_without_inventing_a_dialogue_or_output(self):
        source = self.add()
        result = wait_run(self.engine, self.transform(source, "split")["id"])
        self.assertEqual(result["status"], "succeeded", result["error"])
        self.engine.close()
        self.engine = Engine(self.store, claude_command=FAKE)
        doc = self.engine.document(self.doc["id"])
        self.assertEqual(len(doc["cells"]), 2)
        for cell in doc["cells"]:
            self.assertFalse(cell["session"]["available"])
            self.assertIsNone(cell["session"]["name"])
            self.assertEqual(cell["status"], "idle")
            self.assertEqual(cell["outputs"], [{"kind": "markdown", "text": ""}])
            run = self.engine.get_run(cell["last_run_id"])
            self.assertTrue(Path(run["prompt_file"]).is_file())
            self.assertEqual(run["status"], "succeeded")

    def test_input_validation_and_revision_conflicts_do_not_launch_jobs(self):
        source = self.add()
        code = self.add("print(42)", kind="code")
        for kind, payload, status in [("merge", {"other_cell_id": source["id"]}, 400),
                                      ("merge", {"other_cell_id": code["id"]}, 400),
                                      ("merge", {"other_cell_id": "a" * 12}, 404),
                                      ("split", {"instructions": 3}, 400)]:
            with self.subTest(kind=kind, payload=payload), self.assertRaises(Problem) as rejected:
                self.transform(source, kind, **payload)
            self.assertEqual(rejected.exception.status, status)
        with self.assertRaises(Problem) as rejected:
            self.engine.transform_cells(self.doc["id"], source["id"], "split", {"revision": -1})
        self.assertEqual(rejected.exception.status, 409)
        self.engine.claude_command = []
        with self.assertRaises(Problem) as rejected:
            self.transform(source, "split")
        self.assertEqual(rejected.exception.status, 503)
        self.assertFalse(self.engine.runs)
        self.assertNotIn("operation_ids", self.store.get(self.doc["id"]))

    def test_malformed_agent_result_keeps_original_cells(self):
        source = self.add("FAKE_INVALID_TRANSFORM", execute=True)
        original = self.store.get(self.doc["id"])["cells"]
        result = wait_run(self.engine, self.transform(source, "split")["id"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("不能为空", result["error"])
        self.assertEqual(self.store.get(self.doc["id"])["cells"], original)
        self.assertNotIn("applied_cell_ids", result)
        self.assertFalse(self.engine.active)

    def test_cancel_during_session_copy_keeps_original_and_serializes_notebook_mutations(self):
        source = self.add("FAKE_COPY_WAIT", execute=True)
        original = self.store.get(self.doc["id"])["cells"]
        run = self.transform(source, "split")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not self.engine.get_run(run["id"]).get("staged_run_ids"):
            time.sleep(.02)
        self.assertTrue(self.engine.get_run(run["id"]).get("staged_run_ids"))
        with self.assertRaises(Problem):
            self.engine.start(self.doc["id"], source["id"])
        with self.assertRaises(Problem):
            self.engine.save(self.doc["id"], self.store.get(self.doc["id"]))
        self.engine.cancel(run["id"])
        result = wait_run(self.engine, run["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.store.get(self.doc["id"])["cells"], original)
        self.assertFalse(self.engine.active)

    def test_failure_of_second_session_copy_rolls_back_both_new_cells(self):
        source = self.add("FAKE_COPY_FAILURE", execute=True)
        original = self.store.get(self.doc["id"])["cells"]
        result = wait_run(self.engine, self.transform(source, "split")["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(len(result["staged_run_ids"]), 2)
        self.assertEqual(self.engine.get_run(result["staged_run_ids"][0])["status"], "succeeded")
        self.assertEqual(self.engine.get_run(result["staged_run_ids"][1])["status"], "failed")
        self.assertEqual(self.store.get(self.doc["id"])["cells"], original)

    def test_global_or_output_changes_during_agent_call_prevent_stale_replacement(self):
        for change in ("global", "output"):
            with self.subTest(change=change):
                source = self.add("整理时应检查原内容", execute=True)
                initial = self.store.get(self.doc["id"])
                began, release = threading.Event(), threading.Event()
                invoke = self.engine._claude

                def paused(*args, **kwargs):
                    began.set()
                    release.wait(5)
                    return invoke(*args, **kwargs)

                with patch.object(self.engine, "_claude", side_effect=paused):
                    run = self.transform(source, "split")
                    self.assertTrue(began.wait(5))
                    if change == "global":
                        self.engine.save_global(initial["id"], {"revision": initial["global"]["revision"], "goal": "新的全局目的"})
                    else:
                        rid = initial["cells"][-1]["last_run_id"]
                        (self.store.run_path(rid) / "output.md").write_text("外部续聊后的新结论", encoding="utf-8")
                    release.set()
                    result = wait_run(self.engine, run["id"])
                self.assertEqual(result["status"], "failed")
                self.assertIn("已保留", result["error"])
                self.assertEqual([cell["id"] for cell in self.store.get(initial["id"])["cells"]], [cell["id"] for cell in initial["cells"]])

    def test_recovery_recognizes_committed_operation_even_if_final_run_write_was_interrupted(self):
        source = self.add()
        result = wait_run(self.engine, self.transform(source, "split")["id"])
        self.assertEqual(result["status"], "succeeded", result["error"])
        self.engine.close()
        result.update(status="running", finished_at=None)
        result.pop("applied_cell_ids")
        self.store.put_run(result)
        self.engine = Engine(self.store, claude_command=FAKE)
        recovered = self.engine.get_run(result["id"])
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(len(recovered["applied_cell_ids"]), 2)

    def test_result_validation_rejects_wrong_count_forged_metadata_and_invalid_output(self):
        source = new_cell("order", "source", "指令", "Markdown")
        item = {"source": "整理指令", "expected_output": "Markdown", "output": ""}
        for cells in ([], [item], [item, item, item],
                      [item | {"session_id": "forged"}, item],
                      [item | {"source": ""}, item],
                      [item | {"output_format": "xml"}, item],
                      [item | {"output": "编造的已执行结果"}, item]):
            with self.subTest(cells=cells), self.assertRaises(Problem):
                parse_cells(json.dumps({"cells": cells}), "split", [source])
        source["outputs"] = [{"kind": "text", "text": "真实输出"}]
        with self.assertRaises(Problem):
            parse_cells(json.dumps({"cells": [item | {"output_format": "json", "output": "not JSON"}]}), "merge", [source])


class CellOperationHTTPTests(unittest.TestCase):
    def test_http_operations_require_token_and_revision_and_publish_completion_events(self):
        with tempfile.TemporaryDirectory() as folder:
            app = App(folder, claude_command=FAKE, seed=False)
            server = Server(("127.0.0.1", 0), app)
            threading.Thread(target=server.serve_forever, daemon=True).start()

            def request(method, path, data=None, token=True):
                connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["X-Castle-Token"] = app.token
                connection.request(method, path, json.dumps(data) if data is not None else None, headers)
                response = connection.getresponse()
                status, body = response.status, response.read()
                connection.close()
                return status, body

            try:
                doc = new_notebook(folder)
                doc["cells"] = [new_cell("order", "实现", "实现功能", "Markdown"),
                                new_cell("order", "验证", "验证功能", "Markdown")]
                app.store.put(doc)
                path = f"/api/notebooks/{doc['id']}/cells/{doc['cells'][0]['id']}"
                payload = {"revision": doc["revision"], "other_cell_id": doc["cells"][1]["id"]}
                self.assertEqual(request("POST", path + "/merge", payload, token=False)[0], 403)
                self.assertEqual(request("POST", path + "/merge", payload | {"revision": 0})[0], 409)
                status, body = request("POST", path + "/merge", payload)
                self.assertEqual(status, 202, body)
                result = wait_run(app.engine, json.loads(body)["id"])
                self.assertEqual(result["status"], "succeeded", result["error"])
                status, events = request("GET", f"/api/runs/{result['id']}/events")
                self.assertEqual(status, 200)
                self.assertIn(b"applied_cell_ids", events)
                self.assertIn(b'"status": "succeeded"', events)
                status, body = request("GET", f"/api/notebooks/{doc['id']}")
                merged = json.loads(body)
                self.assertEqual(len(merged["cells"]), 1)
                path = f"/api/notebooks/{doc['id']}/cells/{merged['cells'][0]['id']}/split"
                status, body = request("POST", path, {"revision": merged["revision"]})
                self.assertEqual(status, 202, body)
                result = wait_run(app.engine, json.loads(body)["id"])
                self.assertEqual(result["status"], "succeeded", result["error"])
                self.assertEqual(len(app.engine.document(doc["id"])["cells"]), 2)
            finally:
                server.shutdown()
                app.close()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
