"""Record native Claude background events. Executed directly by command hooks."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
import uuid


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    event = json.load(sys.stdin)
    # Subagent events must not finish the parent Order or replace its session ID.
    if event.get("agent_id"):
        return
    fields = ("hook_event_name", "session_id", "permission_mode", "model", "tool_name",
              "tool_use_id", "error", "error_details", "reason", "last_assistant_message")
    record = {key: event[key] for key in fields if key in event}
    if "tool_input" in event:
        record["input"] = json.dumps(event["tool_input"], ensure_ascii=False)[:1200]
    if isinstance(record.get("last_assistant_message"), str):
        record["last_assistant_message"] = record["last_assistant_message"][:2000001]
    folder = Path(sys.argv[1])
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{time.time_ns():020d}-{uuid.uuid4().hex}.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    main()
