"""Native Claude jobs, tracked through the public CLI and per-run hooks."""
from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess
import sys
import time

from .core import Problem, short


HOOKS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
         "PostToolUseFailure", "PermissionDenied", "Stop", "StopFailure")


def cli(engine, control, args, project, *, interruptible=True, timeout=20):
    # Import lazily to share the same cross-platform process lifecycle helpers.
    from .execution import spawn_options, terminate_tree
    process = subprocess.Popen([*engine.claude_command, *args], cwd=project,
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace", **spawn_options())
    if interruptible:
        control["process"] = process
        if control.get("owner"):
            control["owner"]["process"] = process
    began = time.monotonic()
    try:
        while True:
            if interruptible and control["cancel"].is_set():
                raise Problem("Claude 执行已停止。", 499)
            if interruptible and control.get("detach") and control["detach"].is_set():
                raise Problem("Notebook 已停止收集，Claude 后台对话继续保留。")
            if time.monotonic() - began > timeout:
                raise Problem("Claude 后台管理命令超时。", 408)
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                return process.returncode, stdout, stderr
            except subprocess.TimeoutExpired:
                pass
    finally:
        terminate_tree(process)
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def sessions(engine, control, project, **options):
    code, out, err = cli(engine, control, ["agents", "--json", "--all", "--cwd", project],
                         project, **options)
    if code:
        raise Problem(f"无法读取 Claude 后台状态：{short(err or out, 1500)}")
    try:
        entries = json.loads(out)
    except ValueError as exc:
        raise Problem(f"Claude 后台状态不是有效 JSON：{short(out or err, 1500)}") from exc
    if not isinstance(entries, list):
        raise Problem("Claude 后台状态必须是 JSON 数组。")
    return entries


def find_session(entries, run):
    return next((item for item in entries if isinstance(item, dict) and item.get("kind") == "background"
                 and ((run.get("background_id") and item.get("id") == run["background_id"])
                      or item.get("name") == run["session_name"])), None)


def stop(engine, control, project):
    run = control["run"]
    if not run.get("background_id") and control.get("background_launched"):
        entry = find_session(sessions(engine, control, project, interruptible=False, timeout=5), run)
        if entry:
            run["background_id"] = entry["id"]
    if run.get("background_id"):
        code, out, err = cli(engine, control, ["stop", run["background_id"]], project,
                             interruptible=False, timeout=5)
        if code:
            raise Problem(f"无法停止 Claude 后台会话 {run['background_id']}：{short(err or out, 1000)}")


def execute(engine, control, cell, doc, emit, use_output_file):
    run = control["run"]
    project = doc["project_path"]
    folder = engine.store.run_path(run["id"])
    events = folder / "claude-events"
    events.mkdir(exist_ok=True)
    # argv-form hooks avoid shell quoting and preserve paths containing spaces/Unicode.
    hook = {"type": "command", "command": sys.executable,
            "args": [str(Path(__file__).with_name("claude_hook.py")), str(events)], "timeout": 10}
    settings = folder / "claude-settings.json"
    engine.store.atomic_json(settings, {"hooks": {name: [{"hooks": [hook]}] for name in HOOKS}})
    args = ["--background", run["invocation_prompt"], "-n", run["session_name"],
            "--permission-mode", engine.claude_permission_mode, "--settings", str(settings)]
    if run.get("resume_session_id"):
        args.extend(["--resume", run["resume_session_id"], "--fork-session"])
    if not control.get("resume_background"):
        run.update(command=[*engine.claude_command, *args], claude_launch_mode="background",
                   requested_permission_mode=engine.claude_permission_mode)
        engine.store.put_run(run)
    emit("log", {"text": f"Claude · {run['session_name']} · 原生后台 · 权限 {run.get('requested_permission_mode')}"})
    seen = set()
    pending = set()
    denied = []
    answer = None
    failure = None
    previous_state = None
    previous_log = ""

    def read_hooks():
        nonlocal answer, failure
        for path in sorted(events.glob("*.json")):
            if path.name in seen:
                continue
            record = json.loads(path.read_text(encoding="utf-8"))
            seen.add(path.name)
            if record.get("session_id"):
                run["session_id"] = record["session_id"]
            if record.get("model"):
                run["provider_model"] = record["model"]
            mode = record.get("permission_mode")
            if mode and mode != run.get("provider_permission_mode"):
                run["provider_permission_mode"] = mode
                emit("log", {"text": f"Claude 实际权限模式：{mode}"})
            kind = record.get("hook_event_name")
            if kind == "PreToolUse":
                pending.add(record.get("tool_use_id"))
                emit("tool", {"name": record.get("tool_name"), "input": record.get("input", "")})
            elif kind in {"PostToolUse", "PostToolUseFailure", "PermissionDenied"}:
                pending.discard(record.get("tool_use_id"))
                emit("tool_result", {"tool_use_id": record.get("tool_use_id"),
                                     "is_error": kind != "PostToolUse"})
                if kind == "PermissionDenied":
                    denied.append({"tool_name": record.get("tool_name"), "reason": record.get("reason")})
                    emit("log", {"text": f"工具权限被拒绝：{record.get('tool_name')} · {record.get('reason', '')}"})
            elif kind == "Stop":
                answer = record.get("last_assistant_message", "")
            elif kind == "StopFailure":
                failure = record.get("error_details") or record.get("error") or record.get("last_assistant_message")

    try:
        control["background_launched"] = True
        out = err = ""
        if not control.get("resume_background"):
            code, out, err = cli(engine, control, args, project)
            if code:
                setup = ("claude --dangerously-skip-permissions" if engine.claude_permission_mode == "bypassPermissions"
                         else "claude")
                raise Problem(f"Claude 后台启动失败：{short(err or out, 2500)}。请先在项目目录运行 {setup} 完成首次权限与信任确认，或用 --claude-launch-mode print。")
            match = re.search(r"claude attach\s+([\w-]+)", out)
            if match:
                run["background_id"] = match[1]
        missing_since = None
        while True:
            if control["cancel"].is_set():
                raise Problem("Claude 执行已停止。", 499)
            if control.get("detach") and control["detach"].is_set():
                raise Problem("Notebook 已停止收集，Claude 后台对话继续保留。")
            entry = find_session(sessions(engine, control, project), run)
            read_hooks()
            if entry:
                missing_since = None
                run["background_id"] = entry["id"]
                run["session_id"] = entry.get("sessionId") or run.get("session_id")
                current = (entry.get("state"), entry.get("status"), entry.get("waitingFor"),
                           run.get("provider_permission_mode"), run.get("session_id"))
                run["background_state"] = entry.get("state")
                if current != previous_state:
                    previous_state = current
                    engine.store.put_run(run)
                    emit("session", {"name": run["session_name"], "id": run.get("session_id"),
                                     "background_id": run["background_id"], "state": current[0],
                                     "waiting_for": current[2], "permission_mode": run.get("provider_permission_mode"),
                                     "available": True})
                    emit("status", {"message": f"Claude 后台：{current[0]}" + (f" · 等待 {current[2]}，可打开对话处理" if current[2] else "")})
                code, logs, _ = cli(engine, control, ["logs", run["background_id"]], project, timeout=10)
                if not code and logs != previous_log:
                    previous_log = logs
                    logs = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))", "", logs)
                    emit("log", {"text": logs[-4000:]})
                if current[0] in {"failed", "stopped"}:
                    raise Problem(f"Claude 后台执行{current[0]}：{short(failure or logs, 2500)}")
                if current[0] == "done":
                    # Stop hooks complete before a turn becomes done; reread after the status query.
                    read_hooks()
                    if failure:
                        raise Problem(f"Claude 后台执行失败：{short(failure, 2500)}")
                    if answer is None:
                        raise Problem("Claude 后台已结束但未收到最终结果（Stop hook）。请检查 hooks 配置或用 --claude-launch-mode print。")
                    final = {"session_id": run.get("session_id"), "result": answer, "subtype": "success",
                             "stop_reason": "tool_use" if pending else "end_turn", "permission_denials": denied}
                    result = engine._finish_claude(control, cell, doc, emit, final, 0, "", pending,
                                                   final["stop_reason"], use_output_file)
                    return result
            else:
                missing_since = missing_since or time.monotonic()
                if time.monotonic() - missing_since > 10:
                    raise Problem(f"找不到 Claude 后台会话 {run['session_name']}，启动输出：{short(out or err, 1500)}")
            control["cancel"].wait(0.5)
    finally:
        # The notebook observes a user-owned session. Only an explicit Stop owns termination.
        if control["cancel"].is_set():
            try:
                stop(engine, control, project)
            except Exception as exc:
                emit("log", {"text": f"停止 Claude 后台会话失败：{exc}"})
