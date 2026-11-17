"""OpenAI Codex CLI integration: `codex exec --json` streaming runs.

Codex has no native background job API, so every run is a tracked subprocess
(the equivalent of Claude's print mode). Sessions resume interactively with
`codex resume <id>` and split copies fork with `codex exec fork <id>`.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time

from ..core import Problem, short
from .base import Provider, finalize

TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


def resolve_codex(override=None):
    if override:
        expanded = Path(override).expanduser()
        candidate = str(expanded.resolve()) if expanded.is_file() else shutil.which(override)
    else:
        candidate = shutil.which("codex")
    if not candidate:
        return None
    path = Path(candidate)
    # npm shims on Windows invoke cmd.exe; use the actual executable instead.
    if path.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        package = path.parent / "node_modules" / "@openai" / "codex"
        binary = package / "bin" / "codex.exe"
        if binary.is_file():
            return [str(binary)]
        entry = package / "bin" / "codex.js"
        node = shutil.which("node")
        if entry.is_file() and node:
            return [node, str(entry)]
        return None
    if path.suffix == ".js":
        node = shutil.which("node")
        return [node, str(path)] if node and path.is_file() else None
    return [str(path)] if path.is_file() else None


def sandbox_policy(permission_mode):
    """Map the shared permission setting onto Codex sandbox + approval policy."""
    if permission_mode == "bypassPermissions":
        return "bypass", ["--dangerously-bypass-approvals-and-sandbox"]
    sandbox = "read-only" if permission_mode in {"default", "plan"} else "workspace-write"
    return sandbox, ["-s", sandbox, "-c", 'approval_policy="never"']


class CodexProvider(Provider):
    name = "codex"
    label = "Codex"

    def resolve(self, override=None):
        return resolve_codex(override)

    def execute(self, engine, control, cell, doc, emit, use_output_file=True):
        from ..execution import pipe_reader, spawn_options
        run = control["run"]
        folder = engine.store.run_path(run["id"])
        last_message = folder / "codex-last-message.md"
        sandbox, policy = sandbox_policy(engine.claude_permission_mode)
        args = [*self.command, "exec"]
        if run.get("resume_session_id"):
            args.extend(["fork", run["resume_session_id"]])
        args.extend(["--json", "--skip-git-repo-check", "-o", str(last_message), *policy,
                     run["invocation_prompt"]])
        run.update(command=args, launch_mode="exec", provider_permission_mode=sandbox,
                   requested_permission_mode=engine.claude_permission_mode)
        emit("log", {"text": f"Codex · 会话 ID{run['id']} · 沙箱 {sandbox}"})
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
        ended = set()
        stderr = ""
        pending_tools = set()
        answer = ""
        usage = None
        turns = 0
        failure = None
        completed = False
        while len(ended) < 2:
            if control["cancel"].is_set():
                raise Problem("Codex 执行已停止。", 499)
            if time.monotonic() - started > engine.agent_timeout:
                raise Problem(f"Codex 执行超过 {engine.agent_timeout} 秒，已停止。", 408)
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
            if kind == "thread.started":
                run["session_id"] = event.get("thread_id")
                emit("session", {"name": run["session_name"], "id": run.get("session_id"),
                                 "background_id": None, "state": "running", "waiting_for": None,
                                 "permission_mode": sandbox, "available": True})
            elif kind in {"item.started", "item.completed"}:
                item = event.get("item", {})
                item_type = item.get("type")
                item_id = item.get("id")
                if item_type in TOOL_ITEMS:
                    if kind == "item.started":
                        pending_tools.add(item_id)
                        emit("tool", {"name": self._tool_name(item), "input": self._tool_input(item)})
                    else:
                        if item_id not in pending_tools:
                            emit("tool", {"name": self._tool_name(item), "input": self._tool_input(item)})
                        pending_tools.discard(item_id)
                        emit("tool_result", {"tool_use_id": item_id, "is_error": self._tool_failed(item)})
                elif item_type == "agent_message" and kind == "item.completed":
                    answer = item.get("text", "")
                    emit("output", {"kind": "stdout", "text": answer + "\n"})
                elif item_type == "reasoning" and kind == "item.completed":
                    emit("log", {"text": short(item.get("text", ""), 2000)})
                elif item_type == "todo_list" and kind == "item.completed":
                    emit("log", {"text": short(json.dumps(item.get("items", []), ensure_ascii=False), 2000)})
            elif kind == "turn.completed":
                completed = True
                turns += 1
                usage = event.get("usage")
            elif kind == "turn.failed":
                failure = (event.get("error") or {}).get("message") or "Codex turn 失败。"
            elif kind == "error":
                emit("log", {"text": short(event.get("message", ""), 2000)})
        exit_code = process.wait(timeout=5)
        if last_message.is_file():
            try:
                answer = last_message.read_text(encoding="utf-8-sig").replace("\r\n", "\n")
            except OSError:
                pass
        if failure or not completed:
            detail = failure or stderr or answer or "Codex 未返回最终结果。"
            raise Problem(f"Codex 未成功完成（退出码 {exit_code}）：{short(detail, 2500)} 请检查 codex login status。")
        final = {"session_id": run.get("session_id"), "result": answer, "subtype": "success",
                 "stop_reason": "end_turn", "num_turns": turns, "usage": usage}
        return finalize(engine, control, cell, doc, emit, final, exit_code, stderr,
                        pending_tools, "end_turn", use_output_file, self.label)

    @staticmethod
    def _tool_name(item):
        kind = item.get("type")
        if kind == "command_execution":
            return "shell"
        if kind == "mcp_tool_call":
            return f"{item.get('server', 'mcp')}/{item.get('tool', 'tool')}"
        return kind

    @staticmethod
    def _tool_input(item):
        kind = item.get("type")
        if kind == "command_execution":
            return short(item.get("command", ""), 1200)
        if kind == "file_change":
            return short(json.dumps(item.get("changes", []), ensure_ascii=False), 1200)
        if kind == "mcp_tool_call":
            return short(json.dumps(item.get("arguments", {}), ensure_ascii=False), 1200)
        if kind == "web_search":
            return short(item.get("query", ""), 1200)
        return ""

    @staticmethod
    def _tool_failed(item):
        if item.get("type") == "command_execution":
            return bool(item.get("exit_code")) or item.get("status") == "failed"
        return item.get("status") == "failed"

    def terminal_command(self, engine, session, run):
        return [*self.command, "resume", session["id"]]