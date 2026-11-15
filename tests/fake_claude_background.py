"""Deterministic native-job CLI fixture. No worker process or network requests."""
import json
from pathlib import Path
import re
import subprocess
import sys
import uuid

sys.stdout.reconfigure(encoding="utf-8")
root = Path(".castle/fake-jobs")
root.mkdir(parents=True, exist_ok=True)
args = sys.argv[1:]


def save(job):
    path = root / f"{job['id']}.json"
    path.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")


def hook(job, kind, **data):
    settings = json.loads(Path(job["settings"]).read_text(encoding="utf-8"))
    command = settings["hooks"][kind][0]["hooks"][0]
    event = {"hook_event_name": kind, "session_id": job["sessionId"],
             "permission_mode": job["mode"], **data}
    result = subprocess.run([command["command"], *command["args"]],
                            input=json.dumps(event), capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr


if "--background" in args:
    invocation = args[args.index("--background") + 1]
    prompt_file = Path(re.search(r"\.castle/prompts/input[a-f0-9]{12}\.md", invocation).group())
    prompt = prompt_file.read_text(encoding="utf-8")
    if "BG_LAUNCH_FAIL" in prompt:
        print("Workspace not trusted", file=sys.stderr)
        sys.exit(1)
    sid = str(uuid.uuid5(uuid.NAMESPACE_URL, str(prompt_file.resolve())))
    job = {"id": sid[:8], "sessionId": sid, "name": args[args.index("-n") + 1],
           "kind": "background", "state": "working", "status": "busy", "cwd": str(Path.cwd()),
           "settings": args[args.index("--settings") + 1], "mode": args[args.index("--permission-mode") + 1],
           "polls": 0, "prompt": prompt}
    hook(job, "SessionStart", model="background-fixture")
    save(job)
    print(f"Backgrounded session {job['id']}. Run claude attach {job['id']}")
elif args[0] == "agents":
    result = []
    for path in root.glob("*.json"):
        job = json.loads(path.read_text(encoding="utf-8"))
        if job["state"] in {"working", "blocked"}:
            job["polls"] += 1
            if "BG_QUERY_FAIL" in job["prompt"] and job["polls"] > 1:
                print("fixture status query failed", file=sys.stderr)
                sys.exit(1)
            if job["polls"] == 1:
                hook(job, "PreToolUse", tool_name="Write", tool_use_id="write-1", tool_input={"file_path": "result.txt"})
            if "BG_BLOCKED" in job["prompt"] and job["polls"] < 3:
                job.update(state="blocked", status="waiting", waitingFor="input needed")
            elif "BG_WAIT" not in job["prompt"] and job["polls"] >= 2:
                denied = "BG_DENIED" in job["prompt"]
                hook(job, "PermissionDenied" if denied else "PostToolUse", tool_name="Write",
                     tool_use_id="write-1", reason="fixture deny")
                if "BG_FAILURE" in job["prompt"]:
                    hook(job, "StopFailure", error="api_error", error_details="fixture API failure")
                    job.update(state="failed", status="idle")
                else:
                    if "BG_NO_HOOK" not in job["prompt"]:
                        hook(job, "Stop", last_assistant_message="Background final answer")
                    if "BG_OUTPUT_FILE" in job["prompt"]:
                        target = Path(re.search(r"相对项目目录的完整路径：`([^`]+)`", job["prompt"]).group(1))
                        target.write_text("# Native output file", encoding="utf-8")
                    job.update(state="done", status="idle")
                job.pop("waitingFor", None)
            save(job)
        result.append({k: v for k, v in job.items() if k not in {"prompt", "settings", "polls", "mode"}})
    print(json.dumps(result))
elif args[0] == "logs":
    print("Claude is writing the result…")
elif args[0] == "stop":
    path = root / f"{args[1]}.json"
    job = json.loads(path.read_text(encoding="utf-8"))
    job.update(state="stopped", status="idle")
    save(job)
else:
    raise AssertionError(args)
