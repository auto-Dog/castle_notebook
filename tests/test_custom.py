from __future__ import annotations

from datetime import date, datetime, timezone
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from castle_notebook.core import Problem, Store, new_cell, new_notebook, normalize_settings
from castle_notebook.execution import Engine
from castle_notebook.providers.custom import read_transcript, search_sessions
from castle_notebook.server import App, Server


def wait_run(engine, rid, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = engine.get_run(rid)
        if result["status"] != "running":
            return result
        time.sleep(0.02)
    raise AssertionError(f"run {rid} did not finish")


def stamp(day, hour=12):
    return datetime(2026, 11, day, hour, 0, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def write_session(folder, session_id, turns):
    path = Path(folder) / f"{session_id}.jsonl"
    lines = [{"type": "workspace-directories", "sessionId": session_id, "directories": ["D:\\proj"]}]
    for role, text, day in turns:
        lines.append({"type": role, "sessionId": session_id, "timestamp": stamp(day),
                      "message": {"role": role, "content": [{"type": "text", "text": text}]}})
    path.write_text("\n".join(json.dumps(line, ensure_ascii=False) for line in lines), encoding="utf-8")
    return path


class SessionLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="castle custom ")
        self.folder = Path(self.temp.name)
        write_session(self.folder, "alpha-session", [
            ("user", "帮我写一个量化回测框架", 10),
            ("assistant", "好的，先定义数据接口。", 10),
            ("user", "再加上手续费模型", 10),
        ])
        write_session(self.folder, "beta-session", [
            ("user", "讨论一下卫星通信的调度算法", 15),
            ("assistant", "可以从图着色开始。", 15),
        ])

    def tearDown(self):
        self.temp.cleanup()

    def test_search_matches_keyword_and_date_range(self):
        result = search_sessions(self.folder, "量化")
        self.assertEqual([item["session_id"] for item in result["sessions"]], ["alpha-session"])
        entry = result["sessions"][0]
        self.assertEqual(entry["title"], "帮我写一个量化回测框架")
        self.assertEqual(entry["matches"], 1)
        self.assertIn("量化", entry["snippet"])
        result = search_sessions(self.folder, "量化", date(2026, 11, 12), date(2026, 11, 20))
        self.assertEqual(result["sessions"], [])
        result = search_sessions(self.folder, "", date(2026, 11, 14), date(2026, 11, 16))
        self.assertEqual([item["session_id"] for item in result["sessions"]], ["beta-session"])
        result = search_sessions(self.folder, "调度", date(2026, 11, 15), date(2026, 11, 15))
        self.assertEqual([item["session_id"] for item in result["sessions"]], ["beta-session"])

    def test_search_without_filters_lists_recent_sessions_first(self):
        result = search_sessions(self.folder)
        self.assertEqual([item["session_id"] for item in result["sessions"]],
                         ["beta-session", "alpha-session"])

    def test_unbound_directory_reads_nothing(self):
        with self.assertRaisesRegex(Problem, "尚未绑定对话目录"):
            search_sessions(None, "量化")
        with self.assertRaisesRegex(Problem, "尚未绑定对话目录"):
            read_transcript(None, "alpha-session")

    def test_transcript_returns_roles_and_timestamps(self):
        transcript = read_transcript(self.folder, "alpha-session")
        self.assertEqual(transcript["title"], "帮我写一个量化回测框架")
        self.assertEqual([m["role"] for m in transcript["messages"]], ["user", "assistant", "user"])
        self.assertTrue(transcript["messages"][0]["at"].startswith("2026-11-10"))
        self.assertFalse(transcript["truncated"])

    def test_transcript_rejects_invalid_or_foreign_ids(self):
        for bad in ("../alpha-session", "a/b", "..", "missing-session"):
            with self.assertRaises(Problem):
                read_transcript(self.folder, bad)


class CustomEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="castle custom engine ")
        self.project = Path(self.temp.name).resolve()
        self.store = Store(self.project / ".castle")
        self.engine = Engine(self.store, provider="custom")
        self.doc = new_notebook(self.project)
        self.store.put(self.doc)
        self.sessions = self.project / "chat-logs"
        self.sessions.mkdir()
        write_session(self.sessions, "external-one", [("user", "外部 AI 的第一条需求", 17)])

    def tearDown(self):
        self.engine.close()
        for rid in list(self.engine.runs):
            wait_run(self.engine, rid)
        self.temp.cleanup()

    def add(self, source="请外部 AI 完成这一步"):
        self.doc = self.store.get(self.doc["id"])
        cell = new_cell("order", "custom step", source, "Explicit expected result")
        cell["provider"] = "custom"
        self.doc["cells"].append(cell)
        self.store.put(self.doc)
        return cell

    def test_run_waits_for_external_output_file(self):
        cell = self.add()
        run = self.engine.start(self.doc["id"], cell["id"])
        self.assertEqual(run["provider"], "custom")
        self.assertEqual(run["launch_mode"], "external")
        self.assertEqual(self.engine.get_run(run["id"])["status"], "running")
        self.assertTrue((self.store.run_path(run["id"]) / "input.md").is_file())
        self.assertTrue((self.store.run_path(run["id"]) / "context.md").is_file())
        session = self.engine.document(self.doc["id"])["cells"][-1]["session"]
        self.assertTrue(session["available"])
        self.assertFalse(session["bound"])
        (self.store.run_path(run["id"]) / "output.md").write_text("**外部 AI 的结论**", encoding="utf-8")
        finished = wait_run(self.engine, run["id"])
        self.assertEqual(finished["status"], "succeeded", finished.get("error"))
        self.assertIn("外部 AI 的结论", finished["outputs"][0]["text"])
        self.assertEqual(finished["output_source"], "file")

    def test_stop_waiting_keeps_later_file_sync(self):
        cell = self.add()
        run = self.engine.start(self.doc["id"], cell["id"])
        self.engine.cancel(run["id"])
        finished = wait_run(self.engine, run["id"])
        self.assertEqual(finished["status"], "cancelled")
        (self.store.run_path(run["id"]) / "output.md").write_text("停止后写入的结论", encoding="utf-8")
        outputs = self.engine.order_outputs(self.doc["id"])
        cell_output = next(item for item in outputs["cells"] if item["id"] == cell["id"])
        self.assertIn("停止后写入的结论", cell_output["outputs"][0]["text"])

    def test_bind_session_while_running_and_read_transcript(self):
        cell = self.add()
        run = self.engine.start(self.doc["id"], cell["id"])
        with self.assertRaisesRegex(Problem, "尚未绑定对话目录"):
            self.engine.bind_custom_session(self.doc["id"], cell["id"], "external-one")
        self.engine.providers["custom"].sessions_dir = self.sessions
        doc = self.engine.bind_custom_session(self.doc["id"], cell["id"], "external-one")
        session = doc["cells"][-1]["session"]
        self.assertTrue(session["bound"])
        self.assertEqual(session["id"], "external-one")
        self.assertEqual(session["name"], "外部 AI 的第一条需求")
        self.assertEqual(self.engine.get_run(run["id"])["session_id"], "external-one")
        (self.store.run_path(run["id"]) / "output.md").write_text("结论", encoding="utf-8")
        finished = wait_run(self.engine, run["id"])
        self.assertEqual(finished["status"], "succeeded", finished.get("error"))
        self.assertEqual(finished["session_id"], "external-one")
        self.assertEqual(read_transcript(self.sessions, "external-one")["messages"][0]["text"],
                         "外部 AI 的第一条需求")

    def test_bind_requires_order_cell_with_run(self):
        cell = self.add()
        with self.assertRaisesRegex(Problem, "请先运行"):
            self.engine.bind_custom_session(self.doc["id"], cell["id"], "external-one")
        self.doc = self.store.get(self.doc["id"])
        code_cell = new_cell("code", "code step", "x = 1", "")
        self.doc["cells"].append(code_cell)
        self.store.put(self.doc)
        with self.assertRaisesRegex(Problem, "只有 Order 单元"):
            self.engine.bind_custom_session(self.doc["id"], code_cell["id"], "external-one")

    def test_merge_and_split_rejected_for_custom(self):
        self.add()
        self.add("第二步")
        revision = self.store.get(self.doc["id"])["revision"]
        with self.assertRaisesRegex(Problem, "暂不支持合并与拆分"):
            self.engine.transform_cells(self.doc["id"], self.doc["cells"][-1]["id"], "split",
                                        {"revision": revision})

    def test_settings_accept_sessions_directory(self):
        self.assertEqual(normalize_settings({})["custom_sessions_dir"], "")
        self.assertEqual(normalize_settings({"custom_sessions_dir": "C:\\chats"})["custom_sessions_dir"],
                         "C:\\chats")
        with self.assertRaises(Problem):
            normalize_settings({"custom_sessions_dir": 5})


class CustomHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="castle custom http ")
        cls.app = App(cls.temp.name, seed=False, provider="custom")
        cls.server = Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.sessions = Path(cls.temp.name) / "chats"
        cls.sessions.mkdir()
        write_session(cls.sessions, "http-session", [("user", "HTTP 绑定测试会话", 17)])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.app.close()
        cls.server.server_close()
        cls.temp.cleanup()

    def request(self, method, path, data=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json", "X-Castle-Token": self.app.token}
        connection.request(method, path, None if data is None else json.dumps(data), headers)
        response = connection.getresponse()
        body = response.read()
        result = response.status, json.loads(body) if body else {}
        connection.close()
        return result

    def test_directory_binding_search_and_transcript_flow(self):
        self.request("PUT", "/api/custom/directory", {"path": ""})
        status, payload = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(payload["capabilities"]["custom"])
        self.assertEqual(payload["capabilities"]["default_provider"], "custom")
        self.assertEqual(payload["settings"]["custom_sessions_dir"], "")
        status, payload = self.request("POST", "/api/custom/sessions/search", {"keyword": "HTTP"})
        self.assertEqual(status, 409)
        self.assertIn("尚未绑定", payload["error"])
        status, payload = self.request("PUT", "/api/custom/directory", {"path": str(self.sessions)})
        self.assertEqual(status, 200)
        self.assertEqual(payload["custom_sessions_dir"], str(self.sessions))
        status, payload = self.request("POST", "/api/custom/sessions/search",
                                       {"keyword": "HTTP", "date_from": "2026-11-17", "date_to": "2026-11-17"})
        self.assertEqual(status, 200)
        self.assertEqual([item["session_id"] for item in payload["sessions"]], ["http-session"])
        status, payload = self.request("POST", "/api/custom/sessions/search",
                                       {"keyword": "", "date_from": "2026-11-18", "date_to": ""})
        self.assertEqual(status, 200)
        self.assertEqual(payload["sessions"], [])
        status, payload = self.request("POST", "/api/custom/sessions/search",
                                       {"date_from": "bad-date"})
        self.assertEqual(status, 400)
        status, payload = self.request("GET", "/api/custom/sessions/http-session/transcript")
        self.assertEqual(status, 200)
        self.assertEqual(payload["messages"][0]["text"], "HTTP 绑定测试会话")
        status, payload = self.request("GET", "/api/custom/sessions/..%2Fsettings/transcript")
        self.assertIn(status, (400, 404))
        status, payload = self.request("PUT", "/api/custom/directory", {"path": str(self.sessions / "missing")})
        self.assertEqual(status, 400)
        status, payload = self.request("PUT", "/api/custom/directory", {"path": ""})
        self.assertEqual(status, 200)
        self.assertEqual(payload["custom_sessions_dir"], "")
        self.assertIsNone(self.app.engine.providers["custom"].sessions_dir)

    def test_cell_binding_endpoint(self):
        self.request("PUT", "/api/custom/directory", {"path": str(self.sessions)})
        status, doc = self.request("POST", "/api/notebooks", {"template": "blank", "title": "custom http"})
        self.assertEqual(status, 201)
        cell = new_cell("order", "http step", "请外部 AI 处理", "Expected result")
        cell["provider"] = "custom"
        doc["cells"].append(cell)
        status, doc = self.request("PUT", f"/api/notebooks/{doc['id']}", doc)
        self.assertEqual(status, 200, doc)
        status, payload = self.request("POST", f"/api/notebooks/{doc['id']}/cells/{cell['id']}/custom-session",
                                       {"session_id": "http-session"})
        self.assertEqual(status, 409)
        status, run = self.request("POST", f"/api/notebooks/{doc['id']}/cells/{cell['id']}/run", {})
        self.assertEqual(status, 202)
        status, payload = self.request("POST", f"/api/notebooks/{doc['id']}/cells/{cell['id']}/custom-session",
                                       {"session_id": "http-session"})
        self.assertEqual(status, 200)
        session = payload["cells"][-1]["session"]
        self.assertEqual(session["id"], "http-session")
        self.assertTrue(session["bound"])
        (self.app.store.run_path(run["id"]) / "output.md").write_text("外部结论", encoding="utf-8")
        wait_run(self.app.engine, run["id"])
        status, payload = self.request("POST", f"/api/notebooks/{doc['id']}/cells/{cell['id']}/open-session", {})
        self.assertEqual(status, 400)
        self.assertIn("弹窗", payload["error"])


if __name__ == "__main__":
    unittest.main()