"""Isolated processes, streaming runs and provenance. No shell interpolation."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time

from .core import (Problem, apply_initialization, editable_document, now,
                   load_context_config, output_summary, pack_context, project_changes, scan_project,
                   short, snapshot_project, text_field, uid, validate_order_output)
from .cell_operations import operation_cell, parse_cells, replace_cells, select_cells

OUTPUT_LIMIT = 2000000
INPUT_LIMIT = 32000

LINUX_TERMINALS = (
    ("x-terminal-emulator", ("-e",)),
    ("gnome-terminal", ("--",)),
    ("kgx", ("--",)),
    ("konsole", ("-e",)),
    ("xfce4-terminal", ("-x",)),
    ("alacritty", ("-e",)),
    ("kitty", ("--",)),
    ("foot", ("--",)),
    ("xterm", ("-e",)),
)


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
    if override:
        expanded = Path(override).expanduser()
        candidate = str(expanded) if expanded.is_file() else shutil.which(override)
    else:
        candidate = shutil.which("claude")
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


def linux_terminal_command(command):
    """Wrap an argv for a visible Linux terminal without invoking a shell."""
    candidates = []
    configured = os.environ.get("TERMINAL", "").strip()
    if configured:
        try:
            parts = shlex.split(configured)
        except ValueError as exc:
            raise Problem(f"环境变量 TERMINAL 无法解析：{exc}", 503) from exc
        if parts:
            candidates.append((parts[0], tuple(parts[1:])))
    candidates.extend((name, ()) for name, _ in LINUX_TERMINALS)

    seen = set()
    launch_flags = dict(LINUX_TERMINALS)
    for name, options in candidates:
        if name in seen:
            continue
        seen.add(name)
        executable = shutil.which(name)
        if not executable:
            path = Path(name).expanduser()
            executable = str(path) if path.is_file() else None
        if not executable:
            continue
        terminal_name = Path(name).name.lower()
        flags = launch_flags.get(terminal_name, ("-e",))
        return [executable, *options, *flags, *command]
    return None


def session_details(run=None):
    name = run.get("session_name") if run else None
    command = run.get("command", []) if run else []
    if not name and "-n" in command and command.index("-n") + 1 < len(command):
        name = command[command.index("-n") + 1]
    session_id = (run.get("session_id") or run.get("provider_result", {}).get("session_id")) if run else None
    if not run or run["cell_type"] != "order":
        reason = "请先运行此 Order 单元，再打开对话。"
    elif run.get("derived") and not session_id:
        reason = "此整理单元暂无 AI 对话；运行后可打开新对话。"
    elif run["status"] == "running" and not run.get("background_id"):
        reason = "请等待或停止本次执行后再打开对话。"
    elif not name or "--no-session-persistence" in command:
        reason = "此历史运行未保存 Claude 会话，请重新运行 Order 单元。"
    elif not session_id and not run.get("background_id"):
        reason = "本次执行未生成可恢复的 Claude 会话，请重新运行 Order 单元。"
    else:
        reason = ""
    return {"name": name, "id": session_id, "available": not reason, "reason": reason,
            "background_id": run.get("background_id") if run else None,
            "permission_mode": run.get("provider_permission_mode") if run else None}


def open_claude_terminal(command, project):
    if os.name == "nt":
        launcher = command
        options = {"creationflags": subprocess.CREATE_NEW_CONSOLE}
    elif sys.platform.startswith("linux"):
        launcher = linux_terminal_command(command)
        if not launcher:
            raise Problem(
                "找不到可用的 Linux 终端。请安装 x-terminal-emulator、GNOME Terminal、Konsole、"
                "Xfce Terminal、Alacritty、Kitty、Foot 或 XTerm，或设置 TERMINAL 环境变量。",
                503,
            )
        options = {"start_new_session": True}
    else:
        raise Problem("在新终端打开 Claude 对话目前支持 Windows 和 Linux。", 503)
    try:
        # A visible console is intentional: the user continues the conversation here.
        process = subprocess.Popen(launcher, cwd=project, **options)
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
    def __init__(self, store, python=None, claude_command=None, code_timeout=60, agent_timeout=240,
                 claude_launch_mode="background", claude_permission_mode="auto"):
        self.store = store
        self.python = python or sys.executable
        self.claude_command = claude_command if claude_command is not None else resolve_claude()
        self.code_timeout = code_timeout
        self.agent_timeout = agent_timeout
        if claude_launch_mode not in {"background", "print"}:
            raise Problem("Claude 启动模式必须为 background 或 print。")
        if claude_permission_mode not in {"bypassPermissions", "auto", "acceptEdits", "default", "plan", "dontAsk"}:
            raise Problem("Claude 权限模式无效。")
        self.claude_launch_mode = claude_launch_mode
        self.claude_permission_mode = claude_permission_mode
        self.lock = threading.RLock()
        self.active = {}
        self.runs = {}
        self.kernels = {}
        self.pending_contexts = {}
        self.recover()

    def recover(self):
        # A persisted output is evidence, never proof a new process has its variables.
        background = []
        for entry in self.store.list():
            doc = self.store.get(entry["id"])
            affected = False
            for rid in doc.get("operation_ids", []):
                operation = self.store.get_run(rid)
                if operation["status"] == "running":
                    applied = [cell["id"] for cell in doc["cells"] if cell.get("operation_run_id") == rid]
                    operation.update(status="succeeded" if applied else "interrupted", finished_at=now(),
                                     applied_cell_ids=applied,
                                     error=None if applied else "服务退出中断了整理；原单元已保留。")
                    self.store.put_run(operation)
                    for child_id in operation.get("staged_run_ids", []):
                        child = self.store.get_run(child_id)
                        if child["status"] == "running":
                            child.update(status="interrupted", finished_at=now(), error="服务退出中断了对话复制。")
                            self.store.put_run(child)
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
                    latest = self.store.get_run(cell["run_ids"][-1]) if cell["run_ids"] else None
                    if (latest and cell["type"] == "order" and latest["status"] == "running"
                            and latest.get("claude_launch_mode") == "background"):
                        background.append((latest, copy.deepcopy(cell), doc))
                        continue
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
        for run, cell, doc in background:
            rid = run["id"]
            before = self.store.run_path(rid) / "project-before.json"
            control = {"run": run, "events": self.events(rid), "cancel": threading.Event(),
                       "detach": threading.Event(), "condition": threading.Condition(), "process": None,
                       "output_characters": 0, "stream_events": 0, "resume_background": True,
                       "project_before": json.loads(before.read_text(encoding="utf-8")) if before.is_file() else None}
            self.runs[rid] = control
            self.active[doc["id"]] = rid
            run.pop("collection_state", None)
            worker = threading.Thread(target=self._execute, args=(rid, cell, doc), daemon=True)
            control["thread"] = worker
            worker.start()

    def _revalidate_saved_order(self, run):
        """Correct old protocol-only successes without discarding their evidence."""
        if run["status"] != "succeeded" or run.get("derived"):
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
            doc = self.sync_output_files(notebook_id)
            for cell in doc["cells"]:
                if cell["type"] == "order":
                    run = self.get_run(cell["run_ids"][-1]) if cell["run_ids"] else None
                    cell["session"] = session_details(run)
            return doc | {"runtime": self.runtime(notebook_id)}

    def read_output_file(self, rid, project):
        """Read only the output belonging to a known run, with a bounded payload."""
        return self._read_cell_file(rid, project, "output.md", OUTPUT_LIMIT, "输出", "2 MB 显示上限")

    def read_input_file(self, rid, project):
        return self._read_cell_file(rid, project, "input.md", INPUT_LIMIT * 4, "需求输入", "32000 字符上限", INPUT_LIMIT)

    def _read_cell_file(self, rid, project, filename, byte_limit, label_name, limit_name, character_limit=None):
        path = self.store.run_path(rid) / filename
        try:
            label = Path(os.path.relpath(path, project)).as_posix()
        except ValueError:
            label = str(path)
        info = {"run_id": rid, "path": label, "state": "missing", "sha256": None}
        try:
            if path.is_symlink():
                info.update(state="unavailable", error=f"{label_name}文件是符号链接，无法实时读取。")
                return info, None
            with path.open("rb") as stream:
                raw = stream.read(byte_limit + 1)
            if len(raw) > byte_limit:
                info.update(state="unavailable", error=f"{label_name}文件超过 {limit_name}。")
                return info, None
            value = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            if character_limit is not None and len(value) > character_limit:
                info.update(state="unavailable", error=f"{label_name}文件超过 {limit_name}。")
                return info, None
            info.update(state="ready", sha256=hashlib.sha256(value.encode("utf-8")).hexdigest())
            return info, value
        except FileNotFoundError:
            return info, None
        except OSError:
            info.update(state="unavailable", error=f"{label_name}文件暂时无法读取，正在等待文件恢复。")
            return info, None

    def _seed_input_file(self, doc, cell, run):
        info, value = self.read_input_file(run["id"], doc["project_path"])
        if (info["state"] == "missing" and not run.get("input_file") and
                cell.get("file_input", {}).get("run_id") != run["id"]):
            try:
                with (self.store.run_path(run["id"]) / "input.md").open("xb") as stream:
                    stream.write(cell["source"].encode("utf-8"))
            except (FileExistsError, OSError):
                pass
            info, value = self.read_input_file(run["id"], doc["project_path"])
        return info, value

    def sync_output_files(self, notebook_id):
        """Refresh live input/output caches; source writes are protected by file hashes."""
        with self.lock:
            doc = self.store.get(notebook_id)
            changed = False
            first_changed = len(doc["cells"])
            for index, cell in enumerate(doc["cells"]):
                if cell["type"] != "order" or not cell["run_ids"]:
                    continue
                rid = cell["run_ids"][-1]
                run = self.get_run(rid)
                input_info, source = self._seed_input_file(doc, cell, run)
                if source is not None and source != cell["source"]:
                    cell.update(source=source, revision=cell["revision"] + 1, updated_at=now())
                    first_changed = min(first_changed, index)
                    changed = True
                elif source is None:
                    input_info["cached"] = bool(cell["source"])
                    input_info["cached_sha256"] = hashlib.sha256(cell["source"].encode()).hexdigest()
                if input_info != cell.get("file_input"):
                    cell["file_input"] = input_info
                    changed = True
                info, value = self.read_output_file(rid, doc["project_path"])
                previous_info = cell.get("file_output") or {}
                cache_matches = previous_info.get("run_id") == rid or cell.get("last_run_id") == rid
                outputs = cell["outputs"] if cache_matches else []
                if value is not None:
                    outputs = [{"kind": run["output_format"], "text": value}]
                else:
                    info["cached"] = bool(outputs)
                    if outputs:
                        info["cached_sha256"] = hashlib.sha256(output_summary(outputs, OUTPUT_LIMIT + 100000).encode()).hexdigest()
                if outputs != cell["outputs"]:
                    # An external conversation can change the evidence of later cells.
                    if value is not None:
                        first_changed = min(first_changed, index)
                    cell["outputs"] = copy.deepcopy(outputs)
                    changed = True
                if info != previous_info:
                    cell["file_output"] = info
                    changed = True
            if first_changed < len(doc["cells"]):
                for cell in doc["cells"][first_changed + 1:]:
                    if cell["run_ids"]:
                        cell["stale"] = True
                active = self.runs.get(self.active.get(notebook_id))
                if active:
                    active_index = next((i for i, cell in enumerate(doc["cells"])
                                         if cell["id"] == active["run"]["cell_id"]), -1)
                    if active_index > first_changed:
                        active["upstream_changed"] = True
                doc["updated_at"] = now()
            if changed:
                self.store.put(doc)
            return doc

    def order_outputs(self, notebook_id, after=""):
        with self.lock:
            doc = self.sync_output_files(notebook_id)
            cells = [{"id": cell["id"], "source": cell["source"], "revision": cell["revision"],
                      "updated_at": cell["updated_at"], "file_input": cell["file_input"],
                      "outputs": cell["outputs"], "file_output": cell["file_output"]}
                     for cell in doc["cells"] if cell["type"] == "order" and cell["run_ids"]]
            stale_ids = [cell["id"] for cell in doc["cells"] if cell["stale"]]
            signature = [[cell["id"], cell["file_input"], cell["revision"], cell["file_output"]] for cell in cells]
            fingerprint = hashlib.sha256(json.dumps([signature, stale_ids], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            result = {"fingerprint": fingerprint, "changed": fingerprint != after}
            if result["changed"]:
                result.update(cells=cells, stale_cell_ids=stale_ids, updated_at=doc["updated_at"])
            return result

    def open_session(self, notebook_id, cell_id):
        with self.lock:
            doc = self.sync_output_files(notebook_id)
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
            command = ([*self.claude_command, "attach", session["background_id"]] if session["background_id"]
                       else [*self.claude_command, "--resume", session["id"],
                             "--permission-mode", self.claude_permission_mode])
            legacy_notice = not run.get("input_file") and cell.get("input_session_synced") != session["id"]
            if legacy_notice:
                command.append(
                    f"本会话新增需求摘要文件：{cell['file_input']['path']}；输出文件：{cell['file_output']['path']}，"
                    "路径均相对于当前项目目录。此条仅同步文件位置，保留现有文件内容并等待用户的新要求。"
                    "后续每轮对话结束前，结合新增或变更需求更新 input.md 为当前完整需求摘要，并更新 output.md 的结论。")
            open_claude_terminal(command, run["project_path"])
            if legacy_notice:
                cell["input_session_synced"] = session["id"]
                self.store.put(doc)
            return {"session_name": session["name"]}

    def save(self, notebook_id, payload):
        with self.lock:
            active = self.runs.get(self.active.get(notebook_id))
            if active and active["run"].get("operation"):
                raise Problem("请等待或停止当前单元整理后再编辑。", 409)
            current = self.sync_output_files(notebook_id)
            incoming = copy.deepcopy(payload)
            sources = {cell["id"]: cell for cell in current["cells"]}
            writes = []
            if isinstance(incoming, dict) and isinstance(incoming.get("cells"), list):
                for item in incoming["cells"]:
                    if not isinstance(item, dict):
                        continue
                    cell = sources.get(item.get("id"))
                    if not cell or cell["type"] != "order" or item.get("type") != "order" or not cell.get("file_input"):
                        continue
                    if "input_edited" in item and type(item["input_edited"]) is not bool:
                        raise Problem("input_edited 必须为 true 或 false。")
                    edited = item.get("input_edited", item.get("source", cell["source"]) != cell["source"])
                    if not edited:
                        item["source"] = cell["source"]
                    elif item.get("source", "") != cell["source"]:
                        old_info = item.get("file_input")
                        expected = item.get("input_file_sha256", old_info.get("sha256") if isinstance(old_info, dict) else None)
                        info = cell["file_input"]
                        if info["state"] == "unavailable" or expected != info["sha256"]:
                            raise Problem("需求文件已更新或暂时不可读，请选择使用文件版本或保留编辑后再保存。", 409)
                        writes.append((cell, item.get("source", ""), info["sha256"]))
            doc = editable_document(current, incoming)
            editable_fields = ("title", "source", "expected_output", "context_note", "mode", "output_format")
            if active:
                if [(cell["id"], cell["type"]) for cell in doc["cells"]] != [
                        (cell["id"], cell["type"]) for cell in current["cells"]]:
                    raise Problem("执行期间可以编辑其他单元的内容，但不能增删、移动或转换单元。", 409)
                active_id = active["run"]["cell_id"]
                before = next(cell for cell in current["cells"] if cell["id"] == active_id)
                after = next(cell for cell in doc["cells"] if cell["id"] == active_id)
                if any(before[field] != after[field] for field in editable_fields):
                    raise Problem("正在执行的单元使用固定输入快照；请编辑其他单元，或先停止当前执行。", 409)
                active_index = next(index for index, cell in enumerate(current["cells"])
                                    if cell["id"] == active_id)
                changed_indices = [index for index, (old, new) in enumerate(zip(current["cells"], doc["cells"]))
                                   if any(old[field] != new[field] for field in editable_fields)]
                if changed_indices and min(changed_indices) < active_index:
                    active["upstream_changed"] = True
            for cell, value, expected in writes:
                info, _ = self.read_input_file(cell["run_ids"][-1], current["project_path"])
                if info["state"] == "unavailable" or info["sha256"] != expected:
                    raise Problem("需求文件在保存期间发生变化，请重新检查后保存。", 409)
                path = self.store.run_path(cell["run_ids"][-1]) / "input.md"
                temporary = path.with_name(f".input-{uid()}.tmp")
                try:
                    temporary.write_bytes(value.encode("utf-8"))
                    info, _ = self.read_input_file(cell["run_ids"][-1], current["project_path"])
                    if info["state"] == "unavailable" or info["sha256"] != expected:
                        raise Problem("需求文件在保存期间发生变化，请重新检查后保存。", 409)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
                updated = next(item for item in doc["cells"] if item["id"] == cell["id"])
                updated["file_input"] = self.read_input_file(cell["run_ids"][-1], current["project_path"])[0]
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
            doc = self.sync_output_files(notebook_id)
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
                    "input_file": f"{relative_folder}/input.md", "output_file": f"{relative_folder}/output.md",
                    "prompt_file": f".castle/prompts/input{rid}.md"}
        return pack_context(doc, cell_id, self.runtime(doc["id"])["variables"],
                            load_context_config(doc["project_path"]),
                            scan_project(doc["project_path"], excluded=(self.store.path,)), run_info)

    def transform_cells(self, notebook_id, cell_id, operation, payload):
        with self.lock:
            if notebook_id in self.active:
                raise Problem("请等待或停止当前执行后再整理单元。", 409)
            doc = self.sync_output_files(notebook_id)
            if payload.get("revision") != doc["revision"]:
                raise Problem("文档已更新，请刷新后再整理单元。", 409)
            sources = select_cells(doc, cell_id, operation, payload.get("other_cell_id"))
            instructions = text_field(payload.get("instructions", ""), "整理要求", 4000)
            if not self.claude_command:
                raise Problem("合并和拆分需要 Claude CLI，请安装并登录后重启服务。", 503)
            cell = operation_cell(operation, sources, instructions)
            rid = uid()
            context = self._context(doc | {"cells": [cell]}, cell["id"], rid)
            # The restructuring contract must survive custom prompt feature switches.
            if cell["source"] not in context["prompt"]:
                context = self._replace_prompt(context, context["prompt"] + "\n" + cell["source"])
            details = {"kind": operation, "sources": sources, "revision": doc["revision"] + 1,
                       "instructions": instructions}
            if operation == "split" and sources[0]["run_ids"]:
                session = session_details(self.get_run(sources[0]["run_ids"][-1]))
                if session["available"]:
                    details["resume_session_id"] = session["id"]
            return self._start_run(doc, cell, rid, context, None, details)

    def _replace_prompt(self, context, prompt):
        if len(prompt) > context["budget"]:
            raise Problem("整理输入超过上下文预算，请精简内容或调整 max_characters。")
        return context | {"prompt": prompt, "characters": len(prompt),
                          "estimated_tokens": math.ceil(len(prompt) / 2.5),
                          "fingerprint": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}

    def start(self, notebook_id, cell_id):
        with self.lock:
            if notebook_id in self.active:
                raise Problem("同一 Notebook 同时只执行一个单元。", 409)
            doc = self.sync_output_files(notebook_id)
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
            return self._start_run(doc, cell, rid, context, before)

    def _prepare_run(self, doc, cell, rid, context, before=None):
        folder = self.store.run_path(rid)
        folder.mkdir()
        prompt_bytes = context["prompt"].encode("utf-8")
        (folder / "context.md").write_bytes(prompt_bytes)
        if before is not None:
            self.store.atomic_json(folder / "project-before.json", before)
        run = {"id": rid, "notebook_id": doc["id"], "cell_id": cell["id"],
               "cell_type": cell["type"], "cell_title": cell["title"],
               "cell_revision": cell["revision"], "global_revision": doc["global"]["revision"],
               "status": "running", "started_at": now(), "finished_at": None,
               "mode": cell["mode"], "output_format": cell["output_format"],
               "context": context, "outputs": [], "initializer": cell.get("initializer", False),
               "artifact": str(folder / "output.md"), "error": None, "duration_ms": None,
               "input_snapshot": copy.deepcopy(cell), "project_path": doc["project_path"],
               "project_changes": None}
        if cell["type"] == "order":
            run["session_name"] = context["run"]["session_name"]
            run["run_dir"] = context["run"]["run_dir"]
            run["output_file"] = context["run"]["output_file"]
            run["input_file"] = context["run"]["input_file"]
            source_bytes = cell["source"].encode("utf-8")
            (folder / "input.md").write_bytes(source_bytes)
            run["initial_input_sha256"] = hashlib.sha256(source_bytes).hexdigest()
            prompt_file = Path(doc["project_path"]) / context["run"]["prompt_file"]
            prompt_file.parent.mkdir(parents=True, exist_ok=True)
            prompt_file.write_bytes(prompt_bytes)
            run["prompt_file"] = str(prompt_file)
            run["invocation_prompt"] = context["config"]["templates"]["invocation"].format(**context["run"])
        return run

    def _start_run(self, doc, cell, rid, context, before, operation=None):
        run = self._prepare_run(doc, cell, rid, context, before)
        self.pending_contexts.pop((doc["id"], cell["id"]), None)
        control = {"run": run, "events": [], "cancel": threading.Event(), "detach": threading.Event(), "condition": threading.Condition(),
                   "process": None, "output_characters": 0, "stream_events": 0, "project_before": before}
        self.runs[rid] = control
        self.active[doc["id"]] = rid
        if operation:
            run["operation"] = operation
            doc.setdefault("operation_ids", []).append(rid)
        else:
            cell.update(status="running")
            doc["execution_count"] = doc.get("execution_count", 0) + 1
            cell["execution_count"] = doc["execution_count"]
            run["execution_count"] = doc["execution_count"]
            cell["run_ids"].append(rid)
        doc["revision"] += 1
        doc["updated_at"] = now()
        run["document_revision"] = doc["revision"]
        self.store.put_run(run)
        self.store.put(doc)
        self.emit(rid, "status", {"status": "running", "message": "输入快照已保存，开始执行。"})
        worker = threading.Thread(target=self._execute, args=(rid, copy.deepcopy(cell), doc), daemon=True)
        control["thread"] = worker
        worker.start()
        return copy.deepcopy(run)

    def _check_operation(self, run):
        doc = self.sync_output_files(run["notebook_id"])
        operation = run["operation"]
        if doc["revision"] != operation["revision"]:
            raise Problem("整理期间文档或 Global 已更新，原单元已保留，请重新整理。", 409)
        current = {cell["id"]: cell for cell in doc["cells"]}
        if any(current.get(cell["id"]) != cell for cell in operation["sources"]):
            raise Problem("整理期间原单元、input.md 或 output.md 已变化，原单元已保留，请重新整理。", 409)
        return doc

    def _apply_operation(self, control):
        run = control["run"]
        operation = run["operation"]
        sources = operation["sources"]
        cells = parse_cells(output_summary(run["outputs"], OUTPUT_LIMIT + 100000), operation["kind"], sources)
        output_path = Path(run["artifact"])
        if not output_path.exists():
            output_path.write_text(output_summary(run["outputs"], OUTPUT_LIMIT + 100000), encoding="utf-8")
        with self.lock:
            doc = self._check_operation(run)
        candidate = replace_cells(doc, sources, cells)
        staged = []
        session_ids = set()
        try:
            for index, cell in enumerate(cells):
                if control["cancel"].is_set():
                    raise Problem("整理已停止，原单元已保留。", 499)
                rid = uid()
                context = self._context(candidate, cell["id"], rid)
                resume = operation.get("resume_session_id")
                if resume:
                    notice = (
                        "\n\n# CastleNotebook / copy split conversation\n"
                        "这轮只建立拆分后的独立会话，不执行上述指令，不修改任何文件。请仅回复已确认。\n"
                        "本会话已从原 Order 拆分，完整历史仅供参考。以本文件中的当前单元指令和预期输出为准。\n"
                        f"本会话当前输入文件：{context['run']['prompt_file']}\n"
                        f"此后每轮对话更新本单元的需求摘要文件：{context['run']['input_file']}\n"
                        f"此后每轮对话更新本单元的输出文件：{context['run']['output_file']}\n"
                        "历史中的其他输入/输出路径已失效，不得继续更新原单元或另一个拆分单元的文件。\n"
                        f"当前已有结论（本轮保持不变）：\n{output_summary(cell['outputs'], OUTPUT_LIMIT + 100000)}\n"
                    )
                    context = self._replace_prompt(context, context["prompt"] + notice)
                child = self._prepare_run(candidate, cell, rid, context)
                child.update(derived=True, operation_run_id=run["id"],
                             source_cell_ids=[source["id"] for source in sources])
                child_control = {"run": child, "events": [], "cancel": control["cancel"], "detach": control["detach"],
                                 "condition": threading.Condition(), "process": None, "owner": control,
                                 "output_characters": 0, "stream_events": 0}
                with self.lock:
                    self.runs[rid] = child_control
                staged.append(child)
                self.store.put_run(child)
                run["staged_run_ids"] = [item["id"] for item in staged]
                self.store.put_run(run)
                output = copy.deepcopy(cell["outputs"])
                output_text = output_summary(output, OUTPUT_LIMIT + 100000)
                Path(child["artifact"]).write_text(output_text, encoding="utf-8")
                began = time.monotonic()
                if resume:
                    child["resume_session_id"] = resume
                    self.emit(run["id"], "status", {"message": f"正在复制第 {index + 1}/2 份 Claude 对话…"})

                    def emit_copy(kind, data):
                        self.emit(rid, kind, data)
                        if kind in {"log", "tool", "tool_result"}:
                            self.emit(run["id"], kind, data)

                    self._claude(child_control, {"output_format": "text"}, candidate, emit_copy, use_output_file=False)
                    session_id = child.get("session_id")
                    if not session_id or session_id == resume or session_id in session_ids:
                        raise Problem("Claude 未生成独立的复制会话，原单元已保留。")
                    session_ids.add(session_id)
                else:
                    child.pop("session_name", None)
                # Session setup must never replace the Agent's reorganized conclusion.
                Path(child["artifact"]).write_text(output_text, encoding="utf-8")
                (self.store.run_path(rid) / "input.md").write_bytes(cell["source"].encode("utf-8"))
                child.update(status="succeeded", outputs=output, finished_at=now(),
                             duration_ms=round((time.monotonic() - began) * 1000),
                             output_sha256=hashlib.sha256(output_text.encode("utf-8")).hexdigest(),
                             output_source="restructured", stale=cell["stale"])
                self.store.put_run(child)
                self.emit(rid, "finished", {"status": "succeeded", "duration_ms": child["duration_ms"]})
                history = sources[0]["run_ids"][:] if operation["kind"] == "split" else []
                cell.update(run_ids=history + [rid], last_run_id=rid, operation_run_id=run["id"])
            with self.lock:
                if control["cancel"].is_set():
                    raise Problem("整理已停止，原单元已保留。", 499)
                current = self._check_operation(run)
                self.store.put(replace_cells(current, sources, cells))
                run["applied_cell_ids"] = [cell["id"] for cell in cells]
                for source in sources:
                    self.pending_contexts.pop((doc["id"], source["id"]), None)
        except BaseException as exc:
            for child in staged:
                child_control = self.runs[child["id"]]
                terminate_tree(child_control["process"])
                if child["status"] == "running":
                    child.update(status="cancelled" if control["cancel"].is_set() else "failed",
                                 error=str(exc), finished_at=now())
                    self.store.put_run(child)
            raise

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
            if run.get("operation"):
                self._apply_operation(control)
            outcome = "succeeded" if succeeded else "failed"
        except BaseException as exc:
            outcome = "cancelled" if control["cancel"].is_set() else "failed"
            if not control["detach"].is_set():
                run["error"] = str(exc) or type(exc).__name__
                self.emit(rid, "log", {"text": run["error"]})
            if kernel:
                reset = True
                kernel.close()
            else:
                terminate_tree(control["process"])
        finally:
            if control["detach"].is_set() and run.get("claude_launch_mode") == "background" and not control["cancel"].is_set():
                with self.lock:
                    run["collection_state"] = "detached"
                    self.store.put_run(run)
                    self.active.pop(doc["id"], None)
                    self.emit(rid, "detached", {"message": "Notebook 已停止收集，Claude 后台对话继续保留；重启服务后继续收集。"})
                return
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
                output_path = self.store.run_path(rid) / "output.md"
                if cell["type"] == "order":
                    info, file_text = self.read_output_file(rid, doc["project_path"])
                    if info["state"] == "missing":
                        try:
                            # A model-created file wins even if it appears during finalization.
                            with output_path.open("xb") as stream:
                                stream.write(output_text.encode("utf-8"))
                        except FileExistsError:
                            pass
                        except OSError as exc:
                            run.update(status="failed", error=f"无法保存输出文件：{exc}")
                        info, file_text = self.read_output_file(rid, doc["project_path"])
                    if file_text is not None:
                        output_text = file_text
                        run["outputs"] = [{"kind": cell["output_format"], "text": file_text}]
                        run["output_source"] = "file"
                else:
                    output_path.write_text(output_text, encoding="utf-8")
                run["output_sha256"] = hashlib.sha256(output_text.encode()).hexdigest()
                if run.get("operation"):
                    self.store.put_run(run)
                    self.active.pop(doc["id"], None)
                    self.emit(rid, "finished", {"status": run["status"], "duration_ms": run["duration_ms"],
                                                "error": run["error"], "applied_cell_ids": run.get("applied_cell_ids", [])})
                    return
                saved = self.sync_output_files(doc["id"])
                global_changed = saved["global"]["revision"] != run["global_revision"]
                files_changed = bool(run["project_changes"] and run["project_changes"]["files"])
                upstream_changed = bool(control.get("upstream_changed"))
                run["stale"] = bool(reset or global_changed or upstream_changed)
                run["stale_reason"] = ("Global 在执行期间发生变化。" if global_changed else
                                       "上游单元或其 input.md / output.md 在执行期间发生变化。" if upstream_changed else
                                       "Python 内核已重置。" if reset else None)
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
                if cell["type"] == "order":
                    self.sync_output_files(doc["id"])
                self.emit(rid, "finished", {"status": run["status"], "duration_ms": run["duration_ms"], "error": run["error"], "stale": run["stale"], "stale_reason": run["stale_reason"]})

    def _claude(self, control, cell, doc, emit, use_output_file=True):
        if self.claude_launch_mode == "background" or control.get("resume_background"):
            from .claude_background import execute
            return execute(self, control, cell, doc, emit, use_output_file)
        run = control["run"]
        args = [*self.claude_command, "-p", run["invocation_prompt"], "-n", run["session_name"],
                "--permission-mode", self.claude_permission_mode,
                "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if run.get("resume_session_id"):
            args.extend(["--resume", run["resume_session_id"], "--fork-session"])
        run.update(command=args, claude_launch_mode="print", requested_permission_mode=self.claude_permission_mode)
        emit("log", {"text": f"Claude · 会话 ID{run['id']} · 权限 {self.claude_permission_mode}"})
        process = subprocess.Popen(args, cwd=doc["project_path"], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding="utf-8", errors="replace",
                                   env={**os.environ, "PYTHONIOENCODING": "utf-8"}, **spawn_options())
        control["process"] = process
        if control.get("owner"):
            control["owner"]["process"] = process
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
                run["provider_permission_mode"] = event.get("permissionMode")
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
        return self._finish_claude(control, cell, doc, emit, final, exit_code, stderr,
                                   pending_tools, last_stop_reason, use_output_file)

    def _finish_claude(self, control, cell, doc, emit, final, exit_code, stderr,
                       pending_tools, last_stop_reason, use_output_file):
        run = control["run"]
        run["provider_result"] = {k: final[k] for k in ("session_id", "num_turns", "total_cost_usd", "usage", "subtype", "permission_denials", "stop_reason", "is_error") if k in final}
        run["session_id"] = final.get("session_id") or run.get("session_id")
        answer = final.get("result")
        if not isinstance(answer, str):
            answer = ""
        run["cli_output"] = answer
        file_info, file_text = self.read_output_file(run["id"], doc["project_path"]) if use_output_file else ({}, None)
        if file_text is not None:
            answer = file_text
        elif file_info.get("state") == "unavailable":
            raise Problem(file_info["error"])
        if len(answer) > OUTPUT_LIMIT:
            raise Problem("Claude 最终输出超过 2 MB 限制。")
        # Preserve the provider's actual result, including rejected final output.
        if answer.strip():
            run["outputs"] = [{"kind": cell["output_format"], "text": answer}]
            emit("canonical_output", run["outputs"][0])
        if exit_code or final.get("is_error") or final.get("subtype", "success") != "success":
            detail = final.get("errors") or run["cli_output"] or answer or stderr or "Claude 执行失败。"
            raise Problem(f"Claude 未成功完成（{final.get('subtype', 'unknown')}，退出码 {exit_code}）：{short(detail, 2500)}")
        stop_reason = final.get("stop_reason") or last_stop_reason
        run["provider_result"]["stop_reason"] = stop_reason
        if pending_tools or stop_reason in {"tool_use", "max_tokens", "pause_turn"}:
            raise Problem(f"模型尚未完成最终回答（停止原因：{stop_reason or '工具调用未返回'}），原始输出已保留。")
        if final.get("permission_denials"):
            names = ", ".join(dict.fromkeys(item.get("tool_name", "未知") for item in final["permission_denials"]))
            raise Problem(f"Claude 工具权限被拒绝（{names}），本次任务未完整执行；原始输出已保留。请检查权限模式或打开对话处理。")
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
            current = self.sync_output_files(notebook_id)
            source = next((cell for cell in current["cells"] if cell["id"] == run["cell_id"]), None)
            if source and source.get("file_output", {}).get("run_id") == rid:
                run["outputs"] = copy.deepcopy(source["outputs"])
            doc = apply_initialization(current, run)
            self.store.put(doc)
            return self.document(notebook_id)

    def close(self):
        for rid in list(self.active.values()):
            try:
                control = self.runs[rid]
                if control["run"].get("claude_launch_mode") == "background" or self.claude_launch_mode == "background" and control["run"]["cell_type"] == "order":
                    control["detach"].set()
                else:
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
