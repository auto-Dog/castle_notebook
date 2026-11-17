"""Custom provider: the conversation lives in an external AI (e.g. Qoder).

No CLI is launched. A run prepares the usual input/context evidence and then
waits for the external AI to write output.md. Raw conversation files can only
be read from a directory the user explicitly binds in the UI; without a bound
directory nothing outside the project is touched.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re

from ..core import Problem, short
from .base import Provider, finalize

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
SEARCH_FILE_LIMIT = 1000
SEARCH_FILE_BYTES = 32 * 1024 * 1024
TRANSCRIPT_MESSAGE_LIMIT = 2000
TRANSCRIPT_CHARACTER_LIMIT = 400000
TRANSCRIPT_TEXT_LIMIT = 20000


def parse_timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def message_text(record):
    message = record.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts = [item.get("text", "") for item in content
             if isinstance(item, dict) and item.get("type") == "text"]
    return "\n".join(part for part in parts if part)


def iter_messages(path):
    """Yield (role, text, timestamp) for human-visible turns of a JSONL chat log."""
    with path.open(encoding="utf-8-sig", errors="replace") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict) or record.get("type") not in {"user", "assistant"}:
                continue
            if record.get("isSidechain"):
                continue
            text = message_text(record)
            if not text.strip():
                continue
            yield record["type"], text, parse_timestamp(record.get("timestamp"))


def session_file(directory, session_id):
    if not directory:
        raise Problem("尚未绑定对话目录。请先在弹窗中指定外部 AI 的会话文件夹。", 409)
    directory = Path(directory)
    if not directory.is_dir():
        raise Problem(f"对话目录不存在或不可读：{directory}", 409)
    if not isinstance(session_id, str) or not SESSION_ID_PATTERN.match(session_id):
        raise Problem("会话 ID 无效。")
    path = (directory / f"{session_id}.jsonl").resolve()
    if path.parent != directory.resolve():
        raise Problem("会话文件不在绑定的对话目录内。")
    if not path.is_file():
        raise Problem("找不到该会话文件，它可能已被移动或删除。", 404)
    return path


def session_title(messages):
    for role, text, _ in messages:
        if role == "user":
            return short(text.strip().replace("\n", " "), 80)
    return ""


def search_sessions(directory, keyword="", date_from=None, date_to=None, limit=50):
    """Match sessions whose messages contain the keyword within the date range."""
    if not directory:
        raise Problem("尚未绑定对话目录。请先在弹窗中指定外部 AI 的会话文件夹。", 409)
    directory = Path(directory)
    if not directory.is_dir():
        raise Problem(f"对话目录不存在或不可读：{directory}", 409)
    needle = keyword.casefold()
    results = []
    for path in sorted(directory.glob("*.jsonl"))[:SEARCH_FILE_LIMIT]:
        try:
            if path.stat().st_size > SEARCH_FILE_BYTES:
                continue
            messages = list(iter_messages(path))
        except OSError:
            continue
        if not messages:
            continue
        matches = 0
        snippet = ""
        for role, text, stamp in messages:
            if needle and needle not in text.casefold():
                continue
            if stamp is not None:
                day = stamp.astimezone().date()
                if date_from and day < date_from:
                    continue
                if date_to and day > date_to:
                    continue
            elif date_from or date_to:
                continue
            matches += 1
            if not snippet:
                if needle:
                    at = text.casefold().find(needle)
                    snippet = short(text[max(0, at - 40):at + len(keyword) + 80].replace("\n", " "), 160)
                else:
                    snippet = short(text.strip().replace("\n", " "), 160)
        if not matches:
            continue
        stamps = [stamp for _, _, stamp in messages if stamp is not None]
        results.append({"session_id": path.stem, "title": session_title(messages) or path.stem,
                        "started_at": min(stamps).isoformat() if stamps else None,
                        "updated_at": max(stamps).isoformat() if stamps else None,
                        "messages": len(messages), "matches": matches, "snippet": snippet})
    results.sort(key=lambda item: item["updated_at"] or "", reverse=True)
    return {"directory": str(directory), "sessions": results[:limit], "truncated": len(results) > limit}


def read_transcript(directory, session_id):
    path = session_file(directory, session_id)
    messages = []
    title = ""
    characters = 0
    truncated = False
    for role, text, stamp in iter_messages(path):
        if len(messages) >= TRANSCRIPT_MESSAGE_LIMIT or characters >= TRANSCRIPT_CHARACTER_LIMIT:
            truncated = True
            break
        if len(text) > TRANSCRIPT_TEXT_LIMIT:
            text = text[:TRANSCRIPT_TEXT_LIMIT] + "\n…（本条消息已截断）"
            truncated = True
        if not title and role == "user":
            title = short(text.strip().replace("\n", " "), 80)
        characters += len(text)
        messages.append({"role": role, "text": text, "at": stamp.isoformat() if stamp else None})
    return {"session_id": session_id, "title": title, "directory": str(Path(directory)),
            "messages": messages, "truncated": truncated}


class CustomProvider(Provider):
    name = "custom"
    label = "Custom"

    def __init__(self, command=None):
        self.command = None
        self.sessions_dir = None

    def resolve(self, override=None):
        return None

    @property
    def available(self):
        return True

    def execute(self, engine, control, cell, doc, emit, use_output_file=True):
        run = control["run"]
        run.update(launch_mode="external")
        emit("log", {"text": f"Custom · 会话 ID{run['id']} · 等待外部 AI 写入 {run['output_file']}"})
        emit("status", {"message": "等待外部 AI 对话并写入 output.md。可打开对话绑定会话；停止等待不会删除已写入的文件。"})
        while True:
            if control["cancel"].is_set():
                raise Problem("已停止等待外部 AI。", 499)
            info, value = engine.read_output_file(run["id"], doc["project_path"])
            if info["state"] == "unavailable":
                raise Problem(info["error"])
            if value is not None and value.strip():
                break
            control["cancel"].wait(0.5)
        final = {"session_id": run.get("session_id"), "result": value, "subtype": "success",
                 "stop_reason": "end_turn"}
        return finalize(engine, control, cell, doc, emit, final, 0, "", set(),
                        "end_turn", use_output_file, self.label)

    def session_details(self, run):
        if not run or run["cell_type"] != "order":
            reason = "请先运行此 Order 单元，再绑定外部对话。"
        else:
            reason = ""
        session_id = run.get("session_id") if run else None
        return {"name": run.get("session_name") if run else None, "id": session_id,
                "available": not reason, "reason": reason, "provider": self.name,
                "background_id": None, "permission_mode": None,
                "bound": bool(session_id),
                "sessions_dir": str(self.sessions_dir) if self.sessions_dir else None}

    def terminal_command(self, engine, session, run):
        raise Problem("Custom 对话在外部 AI 中进行，请在界面弹窗中查看。")