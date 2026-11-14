"""Subprocess protocol fixture. Never makes a network call."""
import json
from pathlib import Path
import re
import sys
import time
import uuid

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
invocation = sys.argv[sys.argv.index("-p") + 1]
prompt_file = Path(re.search(r"\.castle/prompts/input[a-f0-9]{12}\.md", invocation).group())
session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(prompt_file.resolve())))
prompt = prompt_file.read_text(encoding="utf-8").rsplit("## Current step / ", 1)[-1]


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


if "FAKE_WAIT" in prompt:
    time.sleep(30)
if "FAKE_NO_RESULT" in prompt:
    print("not a protocol result", flush=True)
    sys.exit(2)
if "FAKE_INIT" in prompt or "初始化 / 拆分项目" in prompt:
    answer = json.dumps({"goal": "拆解现有算法", "constraints": "只执行用户检查过的代码", "cells": [
        {"type": "markdown", "title": "项目说明", "source": "## 最小研究路径"},
        {"type": "code", "title": "独立断言", "source": "assert 2 + 2 == 4\nprint('verified')"},
        {"type": "order", "title": "下一步审阅", "source": "检查边界", "expected_output": "Markdown"}]}, ensure_ascii=False)
elif "FAKE_PROJECT_EDIT" in prompt:
    # The fixture performs actual writes relative to the subprocess cwd.
    model = Path("project_model.py")
    assert "return 1" in model.read_text(encoding="utf-8")
    model.write_text("def answer():\n    return 42\n", encoding="utf-8")
    Path("verification.txt").write_text("project artifact", encoding="utf-8")
    Path("obsolete.txt").unlink()
    answer = "已修改 project_model.py，新增 verification.txt，删除 obsolete.txt；等待 Python 单元验证。"
elif "FAKE_BAD_JSON" in prompt:
    answer = "this is not json"
elif "FAKE_JSON" in prompt:
    answer = '{"verified": true, "evidence": "上游结果"}'
elif "FAKE_PROTOCOL" in prompt:
    answer = '\n\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="Task">\n</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>'
elif "FAKE_EMPTY" in prompt:
    answer = ""
else:
    answer = "## 检查结论\n\n上游结果可供检查。下一步需要独立执行。"
tools = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Bash", "Task"]
emit({"type": "system", "subtype": "init", "session_id": session_id,
      "model": "fixture-model", "tools": list(filter(None, tools))})
if "FAKE_SESSION_WAIT" in prompt:
    time.sleep(30)
if "Read" in tools:
    emit({"type": "assistant", "message": {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "id": "read-1", "name": "Read", "input": {"file_path": str(prompt_file)}}]}})
    if "FAKE_PENDING_TOOL" not in prompt:
        emit({"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "read-1", "content": "Fixture project"}]}})
emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "正在流式输出"}}})
stop_reason = "max_tokens" if "FAKE_TRUNCATED" in prompt else "end_turn"
emit({"type": "assistant", "message": {"stop_reason": stop_reason, "content": [{"type": "text", "text": answer}]}})
emit({"type": "result", "is_error": "FAKE_FAILURE" in prompt, "result": answer,
      "subtype": "error_max_turns" if "FAKE_SUBTYPE" in prompt else "success",
      "permission_denials": [{"tool_name": "Read"}] if "FAKE_DENIED_TOOL" in prompt else [],
      "session_id": session_id, "num_turns": 1})
