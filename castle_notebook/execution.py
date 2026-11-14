"""Isolated processes, streaming runs and provenance. No shell interpolation."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time

from .core import (Problem, apply_initialization, editable_document, now,
                   load_context_config, output_summary, pack_context, project_changes, scan_project,
                   short, snapshot_project, uid, validate_order_output)

OUTPUT_LIMIT = 2000000


def spawn_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_tree(process):
    if process is None:
        return
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdin and not process.stdin.closed:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass


def pipe_reader(stream, messages, channel="stdout"):
    try:
        for line in iter(stream.readline, ""):
            messages.put((channel, line))
    finally:
        stream.close()
        messages.put((channel, None))


def resolve_claude(override=None):
    candidate = override or shutil.which("claude")
    if not candidate:
        return None
    path = Path(candidate)
    # npm shims on Windows invoke cmd.exe; use the actual executable instead.
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        package = path.parent / "node_modules" / "@anthropic-ai" / "claude-code"
        binary = package / "bin" / "claude.exe"
        if binary.is_file():
            return [str(binary)]
        entry = package / "cli.js"
        node = shutil.which("node")
        if entry.is_file() and node:
            return [node, str(entry)]
        return None
    if path.suffix == ".js":
        node = shutil.which("node")
        return [node, str(path)] if node and path.is_file() else None
    return [str(path)] if path.is_file() else None


def session_details(run=None):
    name = run.get("session_name") if run else None
    command = run.get("command", []) if run else []
    if not name and "-n" in command and command.index("-n") + 1 < len(command):
        name = command[command.index("-n") + 1]
    session_id = (run.get("session_id") or run.get("provider_result", {}).get("session_id")) if run else None
    if not run or run["cell_type"] != "order":
        reason = "请先运行此 Order 单元，再打开对话。"
    elif run["status"] == "running":
        reason = "请等待或停止本次执行后再打开对话。"
    elif not name or "--no-session-persistence" in command:
        reason = "此历史运行未保存 Claude 会话，请重新运行 Order 单元。"
    elif not session_id:
        reason = "本次执行未生成可恢复的 Claude 会话，请重新运行 Order 单元。"
    else:
        reason = ""
    return {"name": name, "id": session_id, "available": not reason, "reason": reason}


def open_claude_terminal(command, project):
    if os.name != "nt":
        raise Problem("在新终端打开 Claude 对话目前仅支持 Windows。", 503)
    try:
        # A visible console is intentional: the user continues the conversation here.
        process = subprocess.Popen(command, cwd=project, creationflags=subprocess.CREATE_NEW_CONSOLE)
    except OSError as exc:
        raise Problem(f"无法打开 Claude 对话终端：{exc}", 503) from exc
    threading.Thread(target=process.wait, daemon=True).start()


class Kernel:
    def __init__(self, python, project):
        self.id = uid()
        self.variables = []
        self.messages = queue.Queue(maxsize=5000)
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "MPLBACKEND": "Agg"}
        self.process = subprocess.Popen([python, "-u", str(Path(__file__).with_name("kernel_worker.py")), str(project)],
                                        cwd=project, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                        env=env, **spawn_options())
        self.readers = []
        for name in ("stdout", "stderr"):
            reader = threading.Thread(target=pipe_reader, args=(getattr(self.process, name), self.messages, name), daemon=True)
            reader.start()
            self.readers.append(reader)

    def execute(self, run, cell, emit, cancel, timeout):
        request = {"id": run["id"], "cell_id": cell["id"], "code": cell["source"]}
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise Problem("Python 内核已退出，请重启内核。") from exc
        start = time.monotonic()
        while True:
            if cancel.is_set():
                raise Problem("执行已停止。", 499)
            if time.monotonic() - start > timeout:
                raise Problem(f"Python 单元超过 {timeout} 秒限制，内核已重置。", 408)
            try:
                channel, line = self.messages.get(timeout=0.1)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise Problem("Python 内核意外退出；共享变量已丢失，请从上游重新运行。")
                continue
            if line is None:
                if channel == "stdout":
                    raise Problem("Python 内核已退出；共享变量已丢失。")
                continue
            if channel == "stderr":
                emit("log", {"text": line[:8000]})
                continue
            try:
                message = json.loads(line)
            except ValueError as exc:
                raise Problem("Python 内核通信被破坏，请重启内核。") from exc
            if message.get("id") != run["id"]:
                continue
            if message["type"] == "done":
                self.variables = message["variables"]
                emit("variables", {"variables": self.variables, "kernel_id": self.id})
                return message["success"]
            if message["type"] == "display":
                emit("output", message["output"])
            elif message["type"] in {"stream", "error"}:
                emit("output", {"kind": "error" if message["type"] == "error" else message["channel"], "text": message["text"]})

    def close(self):
        terminate_tree(self.process)
        for reader in self.readers:
            reader.join(timeout=1)


class Engine:
    def __init__(self, store, python=None, claude_command=None, code_timeout=60, agent_timeout=240):
        self.store = store
        self.python = python or sys.executable
        self.claude_command = claude_command if claude_command is not None else resolve_claude()
        self.code_timeout = code_timeout
        self.agent_timeout = agent_timeout
        self.lock = threading.RLock()
        self.active = {}
        self.runs = {}
        self.kernels = {}
        self.pending_contexts = {}
        self.recover()

    def recover(self):
        # A persisted output is evidence, never proof a new process has its variables.
        for entry in self.store.list():
            doc = self.store.get(entry["id"])
            affected = False
            for cell in doc["cells"]:
                rejected = False
                if cell["type"] == "order":
                    for rid in cell["run_ids"]:
                        run = self.store.get_run(rid)
                        if self._revalidate_saved_order(run) and rid == cell.get("last_run_id", cell["run_ids"][-1]):
                            cell.update(status="failed", last_error=run["error"])
                            affected = True
                            rejected = True
                if cell["type"] == "code" and cell["run_ids"]:
                    affected = True
                if cell["status"] == "running":
                    cell["status"] = "failed"
                    for rid in cell["run_ids"][-1:]:
                        run = self.store.get_run(rid)
                        run.update(status="interrupted", finished_at=now(), error="服务退出中断了执行。")
                        self.store.put_run(run)
                    affected = True
                if affected and cell["run_ids"] and not rejected:
                    cell["stale"] = True
            if affected:
                doc["revision"] += 1
                self.store.put(doc)

    def _revalidate_saved_order(self, run):
        """Correct old protocol-only successes without discarding their evidence."""
        if run["status"] != "succeeded":
            return False
        try:
            answer = output_summary(run["outputs"], OUTPUT_LIMIT + 100000)
            validate_order_output(answer, run["output_format"])
        except Problem as exc:
            checked_at = now()
            run["validation"] = {"previous_status": run["status"], "previous_error": run.get("error"),
                                 "checked_at": checked_at, "error": str(exc)}
            run.update(status="failed", error=str(exc))
            self.store.put_run(run)
            path = self.store.run_path(run["id"]) / "events.jsonl"
            previous = self.events(run["id"])
            corrections = [
                ("validation", {"previous_status": "succeeded", "status": "failed", "message": str(exc)}),
                ("finished", {"status": "failed", "error": str(exc), "duration_ms": run.get("duration_ms"),
                              "stale": run.get("stale", False), "stale_reason": run.get("stale_reason")})]
            with path.open("a", encoding="utf-8") as stream:
                for offset, (kind, data) in enumerate(corrections, 1):
                    event = {"seq": len(previous) + offset, "type": kind, "data": data, "at": checked_at}
                    stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            return True
        return False

    def runtime(self, notebook_id):
        with self.lock:
            kernel = self.kernels.get(notebook_id)
            alive = bool(kernel and kernel.process.poll() is None)
            return {"state": "busy" if notebook_id in self.active else ("ready" if alive else "cold"),
                    "kernel_id": kernel.id if alive else None,
                    "variables": copy.deepcopy(kernel.variables) if alive else [],
                    "active_run_id": self.active.get(notebook_id), "python": self.python}

    def document(self, notebook_id):
        with self.lock:
            doc = self.store.get(notebook_id)
            for cell in doc["cells"]:
                if cell["type"] == "order":
                    run = self.get_run(cell["run_ids"][-1]) if cell["run_ids"] else None
                    cell["session"] = session_details(run)
            return doc | {"runtime": self.runtime(notebook_id)}

    def open_session(self, notebook_id, cell_id):
        with self.lock:
            doc = self.store.get(notebook_id)
            cell = next((c for c in doc["cells"] if c["id"] == cell_id), None)
            if not cell:
                raise Problem("单元不存在。", 404)
            if cell["type"] != "order":
                raise Problem("只有 Order 单元可以打开 Claude 对话。")
            run = self.get_run(cell["run_ids"][-1]) if cell["run_ids"] else None
            session = session_details(run)
            if not session["available"]:
                raise Problem(session["reason"], 409)
            if not self.claude_command:
                raise Problem("未发现 Claude CLI。请安装并登录 Claude Code 后重启服务。", 503)
            # Headless (-p) sessions are excluded from Claude's interactive name search.
            # Resume the exact saved UUID while keeping Session Name as the display label.
            command = [*self.claude_command, "--resume", session["id"]]
            open_claude_terminal(command, run["project_path"])
            return {"session_name": session["name"]}

    def save(self, notebook_id, payload):
        with self.lock:
            if notebook_id in self.active:
                raise Problem("请等待或停止当前执行后再编辑。", 409)
            doc = editable_document(self.store.get(notebook_id), payload)
            self.store.put(doc)
            return self.document(notebook_id)

    def save_global(self, notebook_id, payload):
        """Global remains editable while a run uses its immutable input snapshot."""
        with self.lock:
            doc = self.store.get(notebook_id)
            if not isinstance(payload, dict) or payload.get("revision") != doc["global"]["revision"]:
                raise Problem("Global 已在其他页面更新，请刷新后再编辑。", 409)
            incoming = {"revision": doc["revision"], "global": {
                "goal": payload.get("goal", doc["global"]["goal"]),
                "constraints": payload.get("constraints", doc["global"]["constraints"])}}
            updated = editable_document(doc, incoming)
            if updated["global"]["revision"] != doc["global"]["revision"]:
                self.store.put(updated)
            return self.document(notebook_id)

    def context(self, notebook_id, cell_id):
        with self.lock:
            doc = self.store.get(notebook_id)
            key = (notebook_id, cell_id)
            rid = self.pending_contexts.get(key) or uid()
            context = self._context(doc, cell_id, rid)
            self.pending_contexts[key] = rid
            return context

    def _context(self, doc, cell_id, rid):
        try:
            relative_folder = Path(os.path.relpath(self.store.run_path(rid), doc["project_path"])).as_posix()
        except ValueError as exc:
            raise Problem("项目与证据目录不在同一驱动器，无法生成相对输出路径；请调整 --data-dir。") from exc
        run_info = {"run_id": rid, "session_name": f"ID{rid}", "run_dir": relative_folder,
                    "output_file": f"{relative_folder}/output.md", "prompt_file": f".castle/prompts/input{rid}.md"}
        return pack_context(doc, cell_id, self.runtime(doc["id"])["variables"],
                            load_context_config(doc["project_path"]),
                            scan_project(doc["project_path"], excluded=(self.store.path,)), run_info)

    def start(self, notebook_id, cell_id):
        with self.lock:
            if notebook_id in self.active:
                raise Problem("同一 Notebook 同时只执行一个单元。", 409)
            doc = self.store.get(notebook_id)
            cell = next((c for c in doc["cells"] if c["id"] == cell_id), None)
            if not cell:
                raise Problem("单元不存在。", 404)
            if not cell["source"].strip():
                raise Problem("请先填写单元内容。")
            if cell["type"] == "order" and not cell["expected_output"].strip():
                raise Problem("Order 单元必须填写预期输出契约。")
            if cell["type"] == "order" and not self.claude_command:
                raise Problem("未发现 Claude CLI。请安装并登录 Claude Code，随后重启服务；Python 与 Markdown 单元仍可独立使用。", 503)
            key = (notebook_id, cell_id)
            rid = self.pending_contexts.get(key) or uid()
            context = self._context(doc, cell_id, rid)
            before = snapshot_project(doc["project_path"], excluded=(self.store.path,)) if cell["type"] != "markdown" else None
            folder = self.store.run_path(rid)
            folder.mkdir()
            self.pending_contexts.pop(key, None)
            artifact = str(folder / "output.md")
            # Preview and execution use the exact same prompt and fingerprint.
            prompt_bytes = context["prompt"].encode("utf-8")
            (folder / "context.md").write_bytes(prompt_bytes)
            if before is not None:
                self.store.atomic_json(folder / "project-before.json", before)
            run = {"id": rid, "notebook_id": notebook_id, "cell_id": cell_id,
                   "cell_type": cell["type"], "cell_title": cell["title"],
                   "cell_revision": cell["revision"], "global_revision": doc["global"]["revision"],
                   "status": "running", "started_at": now(), "finished_at": None,
                   "mode": cell["mode"], "output_format": cell["output_format"],
                   "context": context, "outputs": [], "initializer": cell.get("initializer", False),
                   "artifact": artifact, "error": None, "duration_ms": None,
                   "input_snapshot": copy.deepcopy(cell), "project_path": doc["project_path"],
                   "project_changes": None}
            if cell["type"] == "order":
                run["session_name"] = context["run"]["session_name"]
                run["run_dir"] = context["run"]["run_dir"]
                run["output_file"] = context["run"]["output_file"]
                relative_prompt = context["run"]["prompt_file"]
                prompt_file = Path(doc["project_path"]) / relative_prompt
                prompt_file.parent.mkdir(parents=True, exist_ok=True)
                prompt_file.write_bytes(prompt_bytes)
                run["prompt_file"] = str(prompt_file)
                invocation_template = context["config"]["templates"]["invocation"]
                run["invocation_prompt"] = invocation_template.format(**context["run"])
            control = {"run": run, "events": [], "cancel": threading.Event(), "condition": threading.Condition(),
                       "process": None, "output_characters": 0, "stream_events": 0, "project_before": before}
            self.runs[rid] = control
            self.active[notebook_id] = rid
            cell.update(status="running")
            doc["execution_count"] = doc.get("execution_count", 0) + 1
            cell["execution_count"] = doc["execution_count"]
            run["execution_count"] = doc["execution_count"]
            cell["run_ids"].append(rid)
            doc["revision"] += 1
            doc["updated_at"] = now()
            self.store.put(doc)
            self.store.put_run(run)
            self.emit(rid, "status", {"status": "running", "message": "输入快照已保存，开始执行。"})
            worker = threading.Thread(target=self._execute, args=(rid, copy.deepcopy(cell), doc), daemon=True)
            control["thread"] = worker
            worker.start()
            return copy.deepcopy(run)

    def emit(self, rid, kind, data):
        control = self.runs[rid]
        if kind == "output":
            size = len(json.dumps(data, ensure_ascii=False))
            control["output_characters"] += size
            control["stream_events"] += 1
            if control["output_characters"] > OUTPUT_LIMIT or control["stream_events"] > 12000:
                raise Problem("输出超过 2 MB 或 12000 个事件限制，执行已停止。")
            outputs = control["run"]["outputs"]
            if outputs and data.get("kind") in {"stdout", "stderr"} and outputs[-1]["kind"] == data["kind"]:
                outputs[-1]["text"] += data.get("text", "")
            else:
                outputs.append(copy.deepcopy(data))
        with control["condition"]:
            event = {"seq": len(control["events"]) + 1, "type": kind, "data": data, "at": now()}
            control["events"].append(event)
            with (self.store.run_path(rid) / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            control["condition"].notify_all()

    def _execute(self, rid, cell, doc):
        control = self.runs[rid]
        run = control["run"]
        began = time.monotonic()
        kernel = None
        reset = False
        try:
            emit = lambda kind, data: self.emit(rid, kind, data)
            if cell["type"] == "markdown":
                emit("output", {"kind": "markdown", "text": cell["source"]})
                succeeded = True
            elif cell["type"] == "code":
                with self.lock:
                    kernel = self.kernels.get(doc["id"])
                    if not kernel or kernel.process.poll() is not None:
                        kernel = Kernel(self.python, doc["project_path"])
                        self.kernels[doc["id"]] = kernel
                    control["process"] = kernel.process
                run["kernel_id"] = kernel.id
                succeeded = kernel.execute(run, cell, emit, control["cancel"], self.code_timeout)
                if not succeeded:
                    run["error"] = "Python 执行失败，查看 traceback。失败前的变量赋值仍可能存在。"
            else:
                succeeded = self._claude(control, cell, doc, emit)
            if control["cancel"].is_set():
                raise Problem("执行已停止。", 499)
            outcome = "succeeded" if succeeded else "failed"
        except BaseException as exc:
            outcome = "cancelled" if control["cancel"].is_set() else "failed"
            run["error"] = str(exc) or type(exc).__name__
            self.emit(rid, "log", {"text": run["error"]})
            if kernel:
                reset = True
                kernel.close()
            else:
                terminate_tree(control["process"])
        finally:
            if control["project_before"] is not None:
                try:
                    after = snapshot_project(doc["project_path"], excluded=(self.store.path,))
                    self.store.atomic_json(self.store.run_path(rid) / "project-after.json", after)
                    run["project_changes"] = project_changes(control["project_before"], after)
                except Exception as exc:
                    run["project_changes"] = {"files": [], "complete": False,
                                              "warnings": [f"无法收集项目文件变更：{exc}"], "policy": "文件变更收集失败。"}
                self.store.atomic_json(self.store.run_path(rid) / "changes.json", run["project_changes"])
                self.emit(rid, "project_changes", run["project_changes"])
            with self.lock:
                run["status"] = outcome
                run["finished_at"] = now()
                run["duration_ms"] = round((time.monotonic() - began) * 1000)
                if reset:
                    self.kernels.pop(doc["id"], None)
                output_text = output_summary(run["outputs"], OUTPUT_LIMIT + 100000)
                (self.store.run_path(rid) / "output.md").write_text(output_text, encoding="utf-8")
                run["output_sha256"] = hashlib.sha256(output_text.encode()).hexdigest()
                saved = self.store.get(doc["id"])
                global_changed = saved["global"]["revision"] != run["global_revision"]
                files_changed = bool(run["project_changes"] and run["project_changes"]["files"])
                run["stale"] = bool(reset or global_changed)
                run["stale_reason"] = "Global 在执行期间发生变化。" if global_changed else ("Python 内核已重置。" if reset else None)
                seen = reset
                for item in saved["cells"]:
                    if item["id"] == cell["id"]:
                        item.update(status=run["status"], outputs=copy.deepcopy(run["outputs"]),
                                    project_changes=copy.deepcopy(run["project_changes"]),
                                    stale=run["stale"], last_run_id=rid, last_error=run["error"],
                                    last_duration_ms=run["duration_ms"])
                        seen = True
                    elif (seen or (files_changed and item["type"] != "markdown")) and item["run_ids"]:
                        item["stale"] = True
                saved["updated_at"] = now()
                saved["revision"] += 1
                self.store.put_run(run)
                self.store.put(saved)
                self.active.pop(doc["id"], None)
                self.emit(rid, "finished", {"status": run["status"], "duration_ms": run["duration_ms"], "error": run["error"], "stale": run["stale"], "stale_reason": run["stale_reason"]})

    def _claude(self, control, cell, doc, emit):
        run = control["run"]
        args = [*self.claude_command, "-p", run["invocation_prompt"], "-n", run["session_name"],
                "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        run["command"] = args
        emit("log", {"text": f"Claude · 会话 ID{run['id']} · 使用本机配置"})
        process = subprocess.Popen(args, cwd=doc["project_path"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **spawn_options())
        control["process"] = process
        messages = queue.Queue(maxsize=5000)
        for name in ("stdout", "stderr"):
            threading.Thread(target=pipe_reader, args=(getattr(process, name), messages, name), daemon=True).start()
        started = time.monotonic()
        final = None
        ended = set()
        stderr = ""
        partial_seen = False
        pending_tools = set()
        last_stop_reason = None
        while len(ended) < 2:
            if control["cancel"].is_set():
                raise Problem("Claude 执行已停止。", 499)
            if time.monotonic() - started > self.agent_timeout:
                raise Problem(f"Claude 执行超过 {self.agent_timeout} 秒，已停止。", 408)
            try:
                channel, line = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if line is None:
                ended.add(channel)
                continue
            if channel == "stderr":
                stderr = (stderr + line)[-16000:]
                emit("log", {"text": short(line, 2000)})
                continue
            try:
                event = json.loads(line)
            except ValueError:
                emit("log", {"text": short(line, 2000)})
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                run["session_id"] = event.get("session_id")
                run["provider_model"] = event.get("model")
                run["provider_tools"] = event.get("tools", [])
                emit("log", {"text": f"模型：{event.get('model', '未知')} · 可用工具：{', '.join(event.get('tools', [])) or '无'}"})
            elif kind == "stream_event":
                nested = event.get("event", {})
                delta = nested.get("delta", {})
                if nested.get("type") == "message_start":
                    last_stop_reason = None
                elif nested.get("type") == "message_delta" and delta.get("stop_reason"):
                    last_stop_reason = delta["stop_reason"]
                if nested.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    partial_seen = True
                    emit("output", {"kind": "stdout", "text": delta.get("text", "")})
            elif kind == "assistant":
                last_stop_reason = event.get("message", {}).get("stop_reason") or last_stop_reason
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        emit("tool", {"name": block.get("name"), "input": short(json.dumps(block.get("input", {}), ensure_ascii=False), 1200)})
                        pending_tools.add(block.get("id"))
                    elif block.get("type") == "text" and not partial_seen:
                        emit("output", {"kind": "stdout", "text": block.get("text", "")})
            elif kind == "user":
                for block in event.get("message", {}).get("content", []):
                    if block.get("type") == "tool_result":
                        pending_tools.discard(block.get("tool_use_id"))
                        emit("tool_result", {"tool_use_id": block.get("tool_use_id"),
                                             "is_error": bool(block.get("is_error"))})
            elif kind == "result":
                final = event
        exit_code = process.wait(timeout=5)
        if not final:
            raise Problem(f"Claude 未返回最终结果（退出码 {exit_code}）。{short(stderr, 1000)} 请检查 claude auth status。")
        run["provider_result"] = {k: final[k] for k in ("session_id", "num_turns", "total_cost_usd", "usage", "subtype", "permission_denials", "stop_reason", "is_error") if k in final}
        run["session_id"] = final.get("session_id") or run.get("session_id")
        answer = final.get("result")
        if not isinstance(answer, str):
            answer = ""
        if len(answer) > OUTPUT_LIMIT:
            raise Problem("Claude 最终输出超过 2 MB 限制。")
        # Preserve the provider's actual result, including rejected final output.
        if answer.strip():
            run["outputs"] = [{"kind": cell["output_format"], "text": answer}]
            emit("canonical_output", run["outputs"][0])
        if exit_code or final.get("is_error") or final.get("subtype", "success") != "success":
            detail = final.get("errors") or answer or stderr or "Claude 执行失败。"
            raise Problem(f"Claude 未成功完成（{final.get('subtype', 'unknown')}，退出码 {exit_code}）：{short(detail, 2500)}")
        stop_reason = final.get("stop_reason") or last_stop_reason
        run["provider_result"]["stop_reason"] = stop_reason
        if pending_tools or stop_reason in {"tool_use", "max_tokens", "pause_turn"}:
            raise Problem(f"模型尚未完成最终回答（停止原因：{stop_reason or '工具调用未返回'}），原始输出已保留。")
        if final.get("permission_denials"):
            emit("log", {"text": "Claude 记录了工具权限拒绝；最终状态沿用 CLI 的结果，详情见运行元数据。"})
        validate_order_output(answer, cell["output_format"])
        return True

    def get_run(self, rid):
        with self.lock:
            return copy.deepcopy(self.runs[rid]["run"]) if rid in self.runs else self.store.get_run(rid)

    def events(self, rid, after=0):
        if rid in self.runs:
            control = self.runs[rid]
            with control["condition"]:
                return copy.deepcopy(control["events"][after:])
        path = self.store.run_path(rid) / "events.jsonl"
        self.store.get_run(rid)
        if not path.is_file():
            return []
        with path.open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()][after:]

    def cancel(self, rid):
        with self.lock:
            if rid not in self.runs or self.runs[rid]["run"]["status"] != "running":
                raise Problem("该运行已结束。", 409)
            control = self.runs[rid]
            control["cancel"].set()
            terminate_tree(control["process"])

    def reset(self, notebook_id):
        with self.lock:
            if notebook_id in self.active:
                raise Problem("请先停止当前运行。", 409)
            kernel = self.kernels.pop(notebook_id, None)
            if kernel:
                kernel.close()
            doc = self.store.get(notebook_id)
            for cell in doc["cells"]:
                if cell["run_ids"] and cell["type"] != "markdown":
                    cell["stale"] = True
            doc["revision"] += 1
            doc["updated_at"] = now()
            self.store.put(doc)
            return self.document(notebook_id)

    def apply_init(self, notebook_id, rid):
        with self.lock:
            if notebook_id in self.active:
                raise Problem("请先等待当前运行结束。", 409)
            run = self.get_run(rid)
            if run["notebook_id"] != notebook_id:
                raise Problem("运行不属于该 Notebook。")
            doc = apply_initialization(self.store.get(notebook_id), run)
            self.store.put(doc)
            return self.document(notebook_id)

    def close(self):
        for rid in list(self.active.values()):
            try:
                self.cancel(rid)
            except Problem:
                pass
        for control in list(self.runs.values()):
            worker = control.get("thread")
            if worker and worker is not threading.current_thread():
                worker.join(timeout=10)
        for kernel in list(self.kernels.values()):
            kernel.close()
        self.kernels.clear()
