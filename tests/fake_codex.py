"""Subprocess fixture for `codex exec --json`. Never makes a network call."""
import json
from pathlib import Path
import re
import sys
import time
import uuid

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")
argv = sys.argv[1:]
assert argv[0] == "exec"
assert "--json" in argv and "--skip-git-repo-check" in argv
invocation = argv[-1]
prompt_file = Path(re.search(r"\.castle/prompts/input[a-f0-9]{12}\.md", invocation).group())
forked = argv[1] == "fork"
resume_id = argv[2] if forked else None
session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, str(prompt_file.resolve()) + ("|fork|" + resume_id if forked else "")))
last_message = Path(argv[argv.index("-o") + 1])
full_prompt = prompt_file.read_text(encoding="utf-8")
prompt = full_prompt.rsplit("## Current step / ", 1)[-1]
output_match = re.search(r"相对项目目录的完整路径：`([^`]+)`", full_prompt)
output_file = Path(output_match.group(1)) if output_match else Path(".castle/runs") / prompt_file.stem.removeprefix("input") / "output.md"
input_match = re.search(r"需求摘要文件为 `([^`]+)`", full_prompt)
input_file = Path(input_match.group(1)) if input_match else output_file.with_name("input.md")


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
    assert forked, "split conversation copies must fork the source session"
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
    model = Path("project_model.py")
    assert "return 1" in model.read_text(encoding="utf-8")
    model.write_text("def answer():\n    return 42\n", encoding="utf-8")
    Path("verification.txt").write_text("project artifact", encoding="utf-8")
    Path("obsolete.txt").unlink()
    answer = "已修改 project_model.py，新增 verification.txt，删除 obsolete.txt；等待 Python 单元验证。"
elif "FAKE_UPDATE_INPUT" in prompt:
    input_file.write_text("## 当前需求\n\n- [x] 修复初始问题\n- [ ] 新增可编辑上下文模板", encoding="utf-8")
    output_file.write_text("初始问题已修复，正在处理新增需求。", encoding="utf-8")
    answer = "输入与输出文件均已更新。"
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

emit({"type": "thread.started", "thread_id": session_id})
emit({"type": "turn.started"})
if "FAKE_FILE_WAIT" in prompt:
    time.sleep(1.2)
    output_file.write_text("# 文件正文\n\n**第二版**", encoding="utf-8")
    time.sleep(1.2)
if "FAKE_SESSION_WAIT" in prompt:
    time.sleep(30)
if copying and "FAKE_COPY_WAIT" in prompt:
    time.sleep(30)
emit({"type": "item.started", "item": {"id": "item_0", "type": "command_execution",
                                       "command": f"cat {prompt_file}", "status": "in_progress"}})
if "FAKE_PENDING_TOOL" not in prompt:
    emit({"type": "item.completed", "item": {"id": "item_0", "type": "command_execution",
                                             "command": f"cat {prompt_file}", "aggregated_output": "Fixture project",
                                             "exit_code": 0, "status": "completed"}})
emit({"type": "item.completed", "item": {"id": "item_1", "type": "reasoning", "text": "检查上游证据"}})
failed = "FAKE_FAILURE" in prompt or (copying and "FAKE_COPY_FAILURE" in prompt and "标题：整理 / 2" in prompt)
if failed:
    emit({"type": "turn.failed", "error": {"message": "fixture turn failure"}})
    sys.exit(1)
emit({"type": "item.completed", "item": {"id": "item_2", "type": "agent_message", "text": answer}})
last_message.write_text(answer, encoding="utf-8")
emit({"type": "turn.completed", "usage": {"input_tokens": 120, "cached_input_tokens": 0, "output_tokens": 40}})