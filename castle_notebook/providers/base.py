"""Provider contract shared by every AI backend.

Each backend lives in its own module and implements command resolution,
streaming execution and session resume. Output finalization (file evidence,
limits, contract validation) is identical for all providers and lives here.
"""
from __future__ import annotations

from ..core import Problem, short, validate_order_output


class Provider:
    name = ""
    label = ""

    def __init__(self, command=None):
        self.command = self.resolve() if command is None else command

    def resolve(self, override=None):
        raise NotImplementedError

    @property
    def available(self):
        return bool(self.command)

    def execute(self, engine, control, cell, doc, emit, use_output_file=True):
        raise NotImplementedError

    def session_details(self, run):
        session_id = (run.get("session_id") or run.get("provider_result", {}).get("session_id")) if run else None
        if not run or run["cell_type"] != "order":
            reason = "请先运行此 Order 单元，再打开对话。"
        elif run.get("derived") and not session_id:
            reason = "此整理单元暂无 AI 对话；运行后可打开新对话。"
        elif run["status"] == "running" and not run.get("background_id"):
            reason = "请等待或停止本次执行后再打开对话。"
        elif not session_id and not run.get("background_id"):
            reason = f"本次执行未生成可恢复的 {self.label} 会话，请重新运行 Order 单元。"
        else:
            reason = ""
        return {"name": run.get("session_name") if run else None, "id": session_id,
                "available": not reason, "reason": reason, "provider": self.name,
                "background_id": run.get("background_id") if run else None,
                "permission_mode": run.get("provider_permission_mode") if run else None}

    def terminal_command(self, engine, session, run):
        raise NotImplementedError


def finalize(engine, control, cell, doc, emit, final, exit_code, stderr,
             pending_tools, last_stop_reason, use_output_file, label):
    """Turn a provider's final event into validated run evidence."""
    run = control["run"]
    run["provider_result"] = {k: final[k] for k in ("session_id", "num_turns", "total_cost_usd", "usage", "subtype", "permission_denials", "stop_reason", "is_error") if k in final}
    run["session_id"] = final.get("session_id") or run.get("session_id")
    answer = final.get("result")
    if not isinstance(answer, str):
        answer = ""
    run["cli_output"] = answer
    file_info, file_text = engine.read_output_file(run["id"], doc["project_path"]) if use_output_file else ({}, None)
    if file_text is not None:
        answer = file_text
    elif file_info.get("state") == "unavailable":
        raise Problem(file_info["error"])
    if len(answer) > engine.limits["output_characters"]:
        raise Problem(f"{label} 最终输出超过 {engine.limits['output_characters']} 字符限制。")
    # Preserve the provider's actual result, including rejected final output.
    if answer.strip():
        run["outputs"] = [{"kind": cell["output_format"], "text": answer}]
        emit("canonical_output", run["outputs"][0])
    if exit_code or final.get("is_error") or final.get("subtype", "success") != "success":
        detail = final.get("errors") or run["cli_output"] or answer or stderr or f"{label} 执行失败。"
        raise Problem(f"{label} 未成功完成（{final.get('subtype', 'unknown')}，退出码 {exit_code}）：{short(detail, 2500)}")
    stop_reason = final.get("stop_reason") or last_stop_reason
    run["provider_result"]["stop_reason"] = stop_reason
    if pending_tools or stop_reason in {"tool_use", "max_tokens", "pause_turn"}:
        raise Problem(f"模型尚未完成最终回答（停止原因：{stop_reason or '工具调用未返回'}），原始输出已保留。")
    if final.get("permission_denials"):
        names = ", ".join(dict.fromkeys(item.get("tool_name", "未知") for item in final["permission_denials"]))
        raise Problem(f"{label} 工具权限被拒绝（{names}），本次任务未完整执行；原始输出已保留。请检查权限模式或打开对话处理。")
    validate_order_output(answer, cell["output_format"])
    return True