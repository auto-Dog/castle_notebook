"""Claude Code CLI integration: print streaming and native background sessions."""
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


def resolve_claude(override=None):
    if override:
        expanded = Path(override).expanduser()
        candidate = str(expanded.resolve()) if expanded.is_file() else shutil.which(override)
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


class ClaudeProvider(Provider):
    name = "claude"
    label = "Claude"

    def resolve(self, override=None):
        return resolve_claude(override)

    def execute(self, engine, control, cell, doc, emit, use_output_file=True):
        from ..execution import pipe_reader, spawn_options
        if engine.claude_launch_mode == "background" or control.get("resume_background"):
            from .claude_background import execute
            return execute(engine, control, cell, doc, emit, use_output_file)
        run = control["run"]
        args = [*self.command, "-p", run["invocation_prompt"], "-n", run["session_name"],
                "--permission-mode", engine.claude_permission_mode,
                "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if run.get("resume_session_id"):
            args.extend(["--resume", run["resume_session_id"], "--fork-session"])
        run.update(command=args, claude_launch_mode="print", requested_permission_mode=engine.claude_permission_mode)
        emit("log", {"text": f"Claude · 会话 ID{run['id']} · 权限 {engine.claude_permission_mode}"})
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
            if time.monotonic() - started > engine.agent_timeout:
                raise Problem(f"Claude 执行超过 {engine.agent_timeout} 秒，已停止。", 408)
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
        return finalize(engine, control, cell, doc, emit, final, exit_code, stderr,
                        pending_tools, last_stop_reason, use_output_file, self.label)

    def session_details(self, run):
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
                "provider": self.name,
                "background_id": run.get("background_id") if run else None,
                "permission_mode": run.get("provider_permission_mode") if run else None}

    def terminal_command(self, engine, session, run):
        # Headless (-p) sessions are excluded from Claude's interactive name search.
        # Resume the exact saved UUID while keeping Session Name as the display label.
        if session["background_id"]:
            return [*self.command, "attach", session["background_id"]]
        return [*self.command, "--resume", session["id"],
                "--permission-mode", engine.claude_permission_mode]