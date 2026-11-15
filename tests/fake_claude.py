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
full_prompt = prompt_file.read_text(encoding="utf-8")
prompt = full_prompt.rsplit("## Current step / ", 1)[-1]
output_match = re.search(r"相对项目目录的完整路径：`([^`]+)`", full_prompt)
output_file = Path(output_match.group(1)) if output_match else Path(".castle/runs") / prompt_file.stem.removeprefix("input") / "output.md"


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


if "FAKE_WAIT" in prompt:
    time.sleep(30)
if "FAKE_NO_RESULT" in prompt:
    print("not a protocol result", flush=True)
    sys.exit(2)
copying = "# CastleNotebook / copy split conversation" in full_prompt
operation = re.search(r"# CastleNotebook / (merge|split) AI Cells\n", prompt)
if copying:
    assert "--resume" in sys.argv and "--fork-session" in sys.argv
    answer = "已确认，后续对话使用本单元的新输入和输出文件。"
elif operation:
    raw_sources = prompt.split("## 原单元（按文档顺序）\n", 1)[1].lstrip()
    sources, _ = json.JSONDecoder().raw_decode(raw_sources)
    output = "\n\n".join(item.get("text", "") for source in sources for item in source["outputs"])
    source = "\n\n".join(source["source"] for source in sources)
    count = 1 if operation[1] == "merge" else 2
    cells = [{"title": f"整理 / {index + 1}", "source": source + (f"\n第 {index + 1} 部分" if count == 2 else ""),
              "expected_output": "整理后的输出契约", "output_format": "markdown", "context_note": "保留已有验证结论",
              "output": output} for index in range(count)]
    if "FAKE_INVALID_TRANSFORM" in prompt:
        cells[0]["source"] = ""
    answer = json.dumps({"cells": cells}, ensure_ascii=False)
    if "FAKE_TRANSFORM_FILE" in prompt:
        output_file.write_text(answer, encoding="utf-8")
        answer = "整理结果已写入文件。"
elif "FAKE_INIT" in prompt or "初始化 / 拆分项目" in prompt:
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
elif "FAKE_WRITE_JSON_FILE" in prompt:
    output_file.write_text('{"from_file": true}', encoding="utf-8")
    answer = "JSON 已写入文件。"
elif "FAKE_WRITE_BAD_JSON_FILE" in prompt:
    output_file.write_text("not valid JSON in file", encoding="utf-8")
    answer = '{"cli_answer": true}'
elif "FAKE_WRITE_FILE" in prompt or "FAKE_FILE_WAIT" in prompt:
    output_file.write_text("# 文件正文\n\n**第一版**", encoding="utf-8")
    answer = "CLI 已完成写入，此文本不应覆盖文件。"
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
if "FAKE_FILE_WAIT" in prompt:
    time.sleep(1.2)
    output_file.write_text("# 文件正文\n\n**第二版**", encoding="utf-8")
    time.sleep(1.2)
if "FAKE_SESSION_WAIT" in prompt:
    time.sleep(30)
if copying and "FAKE_COPY_WAIT" in prompt:
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
emit({"type": "result", "is_error": "FAKE_FAILURE" in prompt or (copying and "FAKE_COPY_FAILURE" in prompt and "标题：整理 / 2" in prompt), "result": answer,
      "subtype": "error_max_turns" if "FAKE_SUBTYPE" in prompt else "success",
      "permission_denials": [{"tool_name": "Read"}] if "FAKE_DENIED_TOOL" in prompt else [],
      "session_id": session_id, "num_turns": 1})
