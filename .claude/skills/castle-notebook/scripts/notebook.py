#!/usr/bin/env python3
"""Dependency-free file adapter for the CastleNotebook Jupyter skill."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile
from datetime import datetime, timezone
import uuid


KEY = "castle_notebook"
INSTRUCTION = "<!-- castle:instruction -->"
EXPECTED = "<!-- castle:expected-output -->"
SPEC_FIELDS = ("title", "instruction", "expected_output")
TEMPLATE_FIELDS = ("project", "global", "history", "current", "expected_output", "writeback")
DEFAULT_CONFIG = {
    "history_characters": 1400,
    "template": (
        "# CastleNotebook / current project step\n\n"
        "## Project\n{project}\n\n## Global\n{global}\n\n"
        "## All preceding cells / compact context\n{history}\n\n"
        "## Current order\n{current}\n\n## Expected output\n{expected_output}\n\n"
        "## Writeback contract\n{writeback}\n"
    ),
}


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def token(prefix):
    return prefix + "-" + uuid.uuid4().hex[:12]


def read_text(path):
    return Path(path).read_text(encoding="utf-8-sig")


def read_json(path):
    return json.loads(read_text(path))


def atomic_text(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".castle-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def source(cell):
    value = cell.get("source", "")
    return "".join(value) if isinstance(value, list) else value


def meta(cell):
    return cell.get("metadata", {}).get(KEY, {})


def new_cell(kind, text, role=None, **fields):
    cell = {"cell_type": kind, "id": token("cell"), "metadata": {}, "source": text}
    if role:
        cell["metadata"][KEY] = {"role": role, "created_at": stamp(), "updated_at": stamp(), **fields}
    if kind == "code":
        cell.update(execution_count=None, outputs=[])
    return cell


def validate_spec(spec):
    if not isinstance(spec, dict):
        raise ValueError("Order spec must be an object")
    for field in SPEC_FIELDS:
        if not isinstance(spec.get(field), str) or not spec[field].strip():
            raise ValueError(f"Order requires nonempty {field}")
    if any(mark in spec[field] for mark in (INSTRUCTION, EXPECTED) for field in SPEC_FIELDS):
        raise ValueError("Order text must not contain reserved Castle markers")
    return {field: spec[field].strip() for field in SPEC_FIELDS}


def order_source(spec):
    return (f"## Order: {spec['title']}\n\n{INSTRUCTION}\n\n{spec['instruction']}\n\n"
            f"{EXPECTED}\n\n### 预期输出\n\n{spec['expected_output']}\n")


def parse_order(cell):
    text = source(cell)
    if text.count(INSTRUCTION) != 1 or text.count(EXPECTED) != 1:
        raise ValueError(f"Order {meta(cell).get('order_id')} has missing or repeated markers")
    title, rest = text.split(INSTRUCTION)
    instruction, expected = rest.split(EXPECTED)
    expected = expected.strip()
    if expected.startswith("### 预期输出"):
        expected = expected[len("### 预期输出"):].strip()
    title = title.strip().removeprefix("## Order:").strip()
    return validate_spec({"title": title, "instruction": instruction, "expected_output": expected})


def excerpt(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[摘录已截断；按文件路径查看完整内容]"


def shell_quote(value):
    value = str(value)
    if os.name == "nt":
        # IPython ! uses cmd.exe on Windows, not the caller's PowerShell.
        if any(char in value for char in ('"', '%', '!', '\n', '\r')):
            raise ValueError("Use argv instead of shell interpolation for this path")
        return '"' + value + '"'
    return shlex.quote(value)


def path_reference(path, base):
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:
        return str(path)  # A personal skill and project can be on different Windows drives.


def runner_source(notebook, project, helper, order_id):
    candidates = [notebook.name, path_reference(notebook, project)]
    relative_helper = path_reference(helper, notebook.parent)
    return f'''# 先保存 Notebook 并暂停自动保存；结构更新后 Reload from Disk。
import json as _castle_json, os as _castle_os, subprocess as _castle_sp, sys as _castle_sys
from pathlib import Path as _CastlePath
from IPython.display import Markdown as _CastleMarkdown, display as _castle_display
_castle_nb = next((p.resolve() for p in map(_CastlePath, {candidates!r}) if p.is_file()), None)
if _castle_nb is None:
    raise FileNotFoundError("请将内核工作目录切换到 Notebook 所在目录或项目根目录")
_castle_helper = (_castle_nb.parent / {relative_helper!r}).resolve()
_castle_run = _castle_json.loads(_castle_sp.check_output(
    [_castle_sys.executable, str(_castle_helper), "prepare", str(_castle_nb), "--cell", {order_id!r}],
    text=True, encoding="utf-8"))
_castle_old_cwd = _CastlePath.cwd()
try:
    _castle_os.chdir(_castle_run["project"])
    if _castle_run["shell_flags"] is not None:
        _castle_flags = _castle_run["shell_flags"]
        _castle_request = _castle_run["request_shell"]
        _castle_response = _castle_run["response_shell"]
        !claude {{_castle_flags}} < {{_castle_request}} > {{_castle_response}}
        _castle_exit = get_ipython().user_ns.get("_exit_code", 1)
    else:
        with open(_castle_run["response_file"], "w", encoding="utf-8") as _castle_response_file:
            _castle_process = _castle_sp.run(
                _castle_run["argv"], input=_CastlePath(_castle_run["request_file"]).read_text(encoding="utf-8"),
                stdout=_castle_response_file, text=True, encoding="utf-8")
        _castle_exit = _castle_process.returncode
finally:
    _castle_os.chdir(_castle_old_cwd)
_castle_finished = _castle_sp.run(
    [_castle_sys.executable, str(_castle_helper), "finish", str(_castle_nb),
     "--run", _castle_run["run_id"], "--exit-code", str(_castle_exit)],
    capture_output=True, text=True, encoding="utf-8")
if _CastlePath(_castle_run["output_file"]).is_file():
    _castle_display(_CastleMarkdown(_CastlePath(_castle_run["output_file"]).read_text(encoding="utf-8")))
print(_castle_finished.stdout or _castle_finished.stderr)
if _castle_finished.returncode:
    raise RuntimeError("Order 未成功；查看上面的错误和运行目录。")
'''


class Notebook:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.original = self.path.read_bytes()
        self.doc = json.loads(self.original.decode("utf-8-sig"))
        self.info = self.doc.get("metadata", {}).get(KEY)
        if not self.info or self.info.get("version") != 1:
            raise ValueError("Not a CastleNotebook v1 ipynb; initialize a new notebook first")
        self.project = (self.path.parent / self.info["project_root"]).resolve()
        self.state = (self.path.parent / self.info["state_dir"]).resolve()
        if not self.path.is_relative_to(self.project) or not self.state.is_relative_to(self.project):
            raise ValueError("Notebook and state directory must stay inside the project")
        self.helper = (self.path.parent / self.info["helper"]).resolve()

    @property
    def cells(self):
        return self.doc["cells"]

    def config(self):
        path = self.state / "context.json"
        return read_json(path) if path.exists() else copy.deepcopy(DEFAULT_CONFIG)

    def global_cell(self):
        cells = [cell for cell in self.cells if meta(cell).get("role") == "global"]
        if len(cells) != 1:
            raise ValueError("Notebook requires exactly one Global cell")
        return cells[0]

    def order(self, order_id):
        for cell in self.cells:
            if meta(cell).get("role") == "order" and meta(cell).get("order_id") == order_id:
                return cell
        raise ValueError(f"Unknown Order: {order_id}")

    def runner(self, order_id):
        matches = [cell for cell in self.cells if meta(cell).get("role") == "runner"
                   and meta(cell).get("order_id") == order_id]
        if len(matches) != 1:
            raise ValueError(f"Order {order_id} requires exactly one runner")
        return matches[0]

    def input_path(self, cell):
        path = (self.project / meta(cell)["input_file"]).resolve()
        if not path.is_relative_to(self.state / "inputs"):
            raise ValueError("Input file must stay in this Notebook's inputs directory")
        return path

    def save(self):
        self.info["updated_at"] = stamp()
        if self.path.read_bytes() != self.original:
            raise ValueError("Notebook changed on disk; reread it instead of overwriting")
        atomic_text(self.state / "backups" / (token("before") + ".ipynb"),
                    self.original.decode("utf-8-sig"))
        atomic_json(self.path, self.doc)
        self.original = self.path.read_bytes()

    def run_path(self, run_id):
        if not re.fullmatch(r"run-[a-f0-9]{12}", run_id):
            raise ValueError("Invalid run ID")
        return self.state / "runs" / run_id / "run.json"

    def run(self, run_id):
        return read_json(self.run_path(run_id))

    def unfinished(self, cell):
        run_id = meta(cell).get("latest_run")
        return bool(run_id and not self.run(run_id).get("finished_at"))

    def pair(self, spec, fork_from=None):
        spec = validate_spec(spec)
        order_id = token("order")
        input_file = path_reference(self.state / "inputs" / (order_id + ".md"), self.project)
        order = new_cell("markdown", order_source(spec), "order", order_id=order_id,
                         title=spec["title"], status="idle", latest_run=None, input_file=input_file,
                         conversation={"session_id": None, "fork_from": fork_from})
        runner = new_cell("code", runner_source(self.path, self.project, self.helper, order_id),
                          "runner", order_id=order_id)
        return [order, runner]

    def export_inputs(self):
        if not (self.state / "context.json").exists():
            atomic_json(self.state / "context.json", DEFAULT_CONFIG)
        atomic_text(self.state / "global.md", source(self.global_cell()))
        for cell in self.cells:
            if meta(cell).get("role") == "order":
                spec = parse_order(cell)
                path = self.input_path(cell)
                if not path.exists() or read_text(path) != source(cell):
                    meta(cell).update(title=spec["title"], updated_at=stamp())
                atomic_text(path, source(cell))

    def fingerprint(self, order_id):
        parts = []
        for cell in self.cells:
            role = meta(cell).get("role")
            if role == "global":
                continue
            if role == "runner":
                previous = self.order(meta(cell)["order_id"])
                run_id = meta(previous).get("latest_run")
                if run_id:
                    run = self.run(run_id)
                    output = Path(run["output_file"])
                    summary = Path(run["summary_file"])
                    parts.append([run_id, run["status"], meta(previous)["status"],
                                  digest(output.read_bytes()) if output.exists() else None,
                                  digest(summary.read_bytes()) if summary.exists() else None])
                continue
            parts.append([cell.get("id"), source(cell), cell.get("outputs", [])])
            if role == "order" and meta(cell)["order_id"] == order_id:
                break
        return digest([source(self.global_cell()), parts, self.config()])

    def stale_orders(self, start=0):
        for cell in self.cells[start:]:
            if meta(cell).get("role") == "order" and meta(cell).get("latest_run"):
                meta(cell)["status"] = "stale"
                self.render(cell)

    def render(self, cell):
        runner = self.runner(meta(cell)["order_id"])
        run_id = meta(cell).get("latest_run")
        if not run_id:
            return
        run = self.run(run_id)
        path = Path(run["output_file"])
        text = read_text(path) if path.is_file() else "尚无输出文件。"
        status = meta(cell).get("status", run["status"])
        banner = "" if status == "succeeded" else f"> Order 状态：**{status}**。此结果不可当作当前已验证结论。\n\n"
        body = banner + text
        runner["outputs"] = [{"output_type": "display_data", "metadata": {},
                              "data": {"text/markdown": body, "text/plain": body}}]
        meta(runner)["updated_at"] = stamp()

    def archive(self, orders, operation):
        ids = {meta(cell)["order_id"] for cell in orders}
        selected = [cell for cell in self.cells if meta(cell).get("order_id") in ids]
        path = self.state / "archive" / (token(operation) + ".json")
        atomic_json(path, {"operation": operation, "at": stamp(), "cells": selected})
        return path

    def history(self, order_id, limit):
        entries = []
        for index, cell in enumerate(self.cells):
            role = meta(cell).get("role")
            if role == "order" and meta(cell)["order_id"] == order_id:
                break
            if role in ("global", "runner"):
                continue
            if role == "order":
                spec = parse_order(cell)
                m = meta(cell)
                text = f"### {m['order_id']} / {spec['title']} / {m['status']}\n"
                text += f"Input file: {self.input_path(cell)}\nInput excerpt:\n{excerpt(source(cell), limit)}\n"
                if m.get("latest_run"):
                    run = self.run(m["latest_run"])
                    text += f"Output file: {run['output_file']}\n"
                    valid = (m["status"] == "succeeded" and run["input_fingerprint"] == self.fingerprint(m["order_id"]))
                    if valid:
                        summary, output = Path(run["summary_file"]), Path(run["output_file"])
                        fresh_output = output.is_file() and digest(output.read_bytes()) == run.get("output_hash")
                        if fresh_output and summary.is_file():
                            text += "Agent summary:\n" + excerpt(read_text(summary), limit)
                        elif output.is_file():
                            text += "Output excerpt (summary may be outdated):\n" + excerpt(read_text(output), limit)
                        else:
                            text += "Output file unavailable; cached result is not verified."
                    else:
                        text += "Result is stale or unsuccessful; it is not valid evidence."
                entries.append(text)
            else:
                text = f"### Native cell {cell.get('id', index)} / {cell['cell_type']}\n"
                text += "Source excerpt:\n" + excerpt(source(cell), limit)
                outputs = []
                for output in cell.get("outputs", []):
                    value = output.get("text") or output.get("data", {}).get("text/markdown") or output.get("data", {}).get("text/plain")
                    if value:
                        outputs.append("".join(value) if isinstance(value, list) else value)
                if outputs:
                    text += "\nSaved native output (execution freshness unknown):\n" + excerpt("\n".join(outputs), limit)
                entries.append(text)
        return "\n\n".join(entries) or "(No preceding steps)"


def initialize(args):
    path = Path(args.notebook).resolve()
    if path.exists():
        raise ValueError("Notebook already exists; choose a new path")
    project = Path(args.project).resolve() if args.project else path.parent
    if not path.is_relative_to(project):
        raise ValueError("Notebook must be inside the project")
    plan = read_json(args.plan) if args.plan else {"global": "# Global\n\n## 目的\n\n## 约束\n", "orders": []}
    if not isinstance(plan.get("global"), str) or not plan["global"].strip():
        raise ValueError("Plan requires Global Markdown")
    specs = [validate_spec(spec) for spec in plan.get("orders", [])]
    notebook_id = token("notebook")
    state = project / ".castle-notebook" / notebook_id
    helper = Path(__file__).resolve()
    info = {"version": 1, "notebook_id": notebook_id, "created_at": stamp(), "updated_at": stamp(),
            "project_root": path_reference(project, path.parent), "state_dir": path_reference(state, path.parent),
            "helper": path_reference(helper, path.parent)}
    doc = {"nbformat": 4, "nbformat_minor": 5,
           "metadata": {KEY: info, "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                        "language_info": {"name": "python"}},
           "cells": [new_cell("markdown", "# CastleNotebook\n\n保存 Notebook，暂停页面自动保存，然后运行 Order 的 Python 单元。"
                              "AI 修改磁盘文件后使用 Reload from Disk；不要用旧页面覆盖。\n\n"
                              "Global 和 Order 的 Markdown 可直接编辑；保留 Order 中的 HTML 注释。"
                              "输出来自运行目录的 output.md，摘要供后续步骤使用。"),
                     new_cell("markdown", plan["global"], "global")]}
    atomic_json(state / "context.json", DEFAULT_CONFIG)
    atomic_json(path, doc)
    nb = Notebook(path)
    for spec in specs:
        nb.cells.extend(nb.pair(spec))
    nb.export_inputs()
    nb.save()
    return {"notebook": str(path), "state_dir": str(state),
            "orders": [meta(c)["order_id"] for c in nb.cells if meta(c).get("role") == "order"]}


def prepare(nb, order_id):
    cell = nb.order(order_id)
    if any(nb.unfinished(c) for c in nb.cells if meta(c).get("role") == "order"):
        raise ValueError("An Order is already running; finish or abort that run first")
    spec = parse_order(cell)
    nb.export_inputs()
    config = nb.config()
    limit = config["history_characters"]
    if not isinstance(limit, int) or limit < 100:
        raise ValueError("history_characters must be an integer >= 100")
    if not all("{" + field + "}" in config["template"] for field in TEMPLATE_FIELDS):
        raise ValueError("Context template must include every required section")
    run_id = token("run")
    run_dir = nb.run_path(run_id).parent
    conversation = meta(cell).get("conversation", {})
    fork_from = conversation.get("fork_from")
    session_id = None if fork_from else str(uuid.uuid4())
    name = order_id + "-" + run_id
    run = {"run_id": run_id, "order_id": order_id, "notebook": str(nb.path), "project": str(nb.project),
           "started_at": stamp(), "status": "running", "input_fingerprint": nb.fingerprint(order_id),
           "output_file": str(run_dir / "output.md"), "summary_file": str(run_dir / "summary.md"),
           "context_file": str(run_dir / "context.md"), "request_file": str(run_dir / "request.md"),
           "response_file": str(run_dir / "response.json"), "session_id": session_id,
           "session_name": name, "fork_from": fork_from}
    writeback = (f"Only execute this order. Write final Markdown to {run['output_file']}.\n"
                 f"Write a compact factual summary to {run['summary_file']}.\n"
                 f"Then call the skill helper complete for run {run_id}.\n"
                 "Do not recursively invoke Claude or execute later orders. Report real changes and verification.")
    context = config["template"].format(project=nb.project, **{"global": source(nb.global_cell())},
                                         history=nb.history(order_id, limit), current=spec["instruction"],
                                         expected_output=spec["expected_output"], writeback=writeback)
    request = ("Use the castle-notebook skill in execute mode.\n"
               f"Read its full instructions at: {nb.helper.parent.parent / 'SKILL.md'}\n"
               f"Notebook: {nb.path}\nRun ID: {run_id}\n"
               f"Read run metadata: {nb.run_path(run_id)}\nRead full context: {run['context_file']}\n"
               "Complete this prepared run and call the helper's complete command. Do not start another Agent.\n")
    argv = [shutil.which("claude") or "claude", "-p", "--output-format", "json", "-n", name]
    argv += ["--resume", fork_from, "--fork-session"] if fork_from else ["--session-id", session_id]
    presentation = {"argv": argv, "shell_flags": None, "request_shell": None, "response_shell": None}
    try:
        presentation.update(shell_flags=" ".join(shell_quote(v) for v in argv[1:]),
                            request_shell=shell_quote(run["request_file"]), response_shell=shell_quote(run["response_file"]))
    except ValueError:
        pass  # The generated runner uses subprocess with argv for shell-sensitive paths.
    atomic_text(run["context_file"], context)
    atomic_text(run["request_file"], request)
    atomic_json(nb.run_path(run_id), run)
    meta(cell).update(latest_run=run_id, status="running", updated_at=stamp())
    nb.stale_orders(nb.cells.index(cell) + 1)
    nb.render(cell)
    nb.save()
    return {**run, **presentation}


def complete(nb, run_id):
    run = nb.run(run_id)
    cell = nb.order(run["order_id"])
    if meta(cell).get("latest_run") != run_id:
        raise ValueError("Run is no longer the latest run of this Order")
    for name in ("output_file", "summary_file"):
        path = Path(run[name])
        if not path.is_file() or not read_text(path).strip():
            raise ValueError(f"Agent must write nonempty {name}: {path}")
    fresh = run["input_fingerprint"] == nb.fingerprint(run["order_id"])
    status = ("succeeded" if run["status"] == "succeeded" else "written") if fresh else "stale"
    run.update(status=status, output_hash=digest(Path(run["output_file"]).read_bytes()), updated_at=stamp())
    atomic_json(nb.run_path(run_id), run)
    meta(cell).update(status=status, updated_at=stamp())
    nb.render(cell)
    nb.stale_orders(nb.cells.index(cell) + 1)
    nb.save()
    return {"run_id": run_id, "status": status, "output_file": run["output_file"]}


def finish(nb, run_id, exit_code):
    run = nb.run(run_id)
    cell = nb.order(run["order_id"])
    if meta(cell).get("latest_run") != run_id:
        raise ValueError("Cannot finish a superseded run")
    response = {}
    errors = []
    try:
        response = read_json(run["response_file"])
        if not isinstance(response, dict) or response.get("type") != "result":
            raise ValueError("CLI response is not a result object")
        if response.get("is_error"):
            errors.append(str(response.get("result") or response.get("errors") or "Claude reported an error"))
    except (OSError, ValueError) as error:
        errors.append(f"Invalid or missing CLI response: {error}")
        response = {}
    if exit_code:
        errors.append(f"Claude exit code: {exit_code}")
    if run["status"] not in ("written", "succeeded", "stale") or not run.get("output_hash"):
        errors.append("Agent did not complete the output writeback protocol")
    for key in ("output_file", "summary_file"):
        if not Path(run[key]).is_file() or not read_text(run[key]).strip():
            errors.append(f"Missing or empty {key}")
    if Path(run["output_file"]).is_file() and digest(Path(run["output_file"]).read_bytes()) != run.get("output_hash"):
        errors.append("Output changed after writeback; complete the protocol again")
    session_id = response.get("session_id")
    try:
        uuid.UUID(session_id)
        meta(cell)["conversation"] = {"session_id": session_id, "fork_from": None}
        run["session_id"] = session_id
    except (ValueError, TypeError, AttributeError):
        errors.append("Missing or invalid actual Claude session ID")
    fresh = run["input_fingerprint"] == nb.fingerprint(run["order_id"])
    status = "failed" if errors else ("succeeded" if fresh else "stale")
    run.update(status=status, finished_at=stamp(), error="; ".join(errors) or None)
    atomic_json(nb.run_path(run_id), run)
    meta(cell).update(status=status, updated_at=stamp())
    nb.render(cell)
    nb.save()
    return {"run_id": run_id, "status": status, "error": run["error"], "session_id": run["session_id"]}


def restructure(nb, order_ids, specs, operation):
    if len(set(order_ids)) != len(order_ids):
        raise ValueError("Select different Orders")
    orders = [nb.order(value) for value in order_ids]
    if any(nb.unfinished(c) for c in orders):
        raise ValueError("Finish the selected Orders before restructuring")
    specs = [validate_spec(value) for value in specs]
    parent = meta(orders[0]).get("conversation", {})
    fork_from = (parent.get("session_id") or parent.get("fork_from")) if operation == "split" else None
    position = min(nb.cells.index(cell) for cell in orders)
    archive = nb.archive(orders, operation)
    generated = [cell for spec in specs for cell in nb.pair(spec, fork_from=fork_from)]
    selected = set(order_ids)
    nb.doc["cells"] = [cell for cell in nb.cells if meta(cell).get("order_id") not in selected]
    nb.cells[position:position] = generated
    nb.stale_orders(position + len(generated))
    nb.export_inputs()
    nb.save()
    return {"orders": [meta(c)["order_id"] for c in generated if meta(c).get("role") == "order"],
            "archive": str(archive), "fork_from": fork_from}


def dispatch(args):
    if args.command == "init":
        return initialize(args)
    nb = Notebook(args.notebook)
    if args.command == "inspect":
        return {"notebook": str(nb.path), "state_dir": str(nb.state), "global": source(nb.global_cell()),
                "orders": [{**meta(c), **parse_order(c)} for c in nb.cells if meta(c).get("role") == "order"]}
    if args.command == "set-global":
        text = read_text(args.file)
        if not text.strip():
            raise ValueError("Global cannot be empty")
        cell = nb.global_cell()
        cell["source"] = text
        meta(cell)["updated_at"] = stamp()
        nb.stale_orders()
    elif args.command == "add-order":
        pair = nb.pair(read_json(args.spec))
        nb.cells.extend(pair)
        nb.export_inputs()
        nb.save()
        return {"order_id": meta(pair[0])["order_id"]}
    elif args.command == "update-order":
        cell = nb.order(args.cell)
        spec = validate_spec(read_json(args.spec))
        cell["source"] = order_source(spec)
        meta(cell).update(title=spec["title"], updated_at=stamp())
        nb.stale_orders(nb.cells.index(cell))
    elif args.command == "prepare":
        return prepare(nb, args.cell)
    elif args.command == "complete":
        return complete(nb, args.run)
    elif args.command == "finish":
        return finish(nb, args.run, args.exit_code)
    elif args.command == "merge":
        return restructure(nb, args.cells, [read_json(args.spec)], "merge")
    elif args.command == "split":
        specs = read_json(args.spec)
        if not isinstance(specs, list) or len(specs) != 2:
            raise ValueError("Split requires an array of exactly two Order specs")
        return restructure(nb, [args.cell], specs, "split")
    elif args.command == "conversation":
        conversation = meta(nb.order(args.cell)).get("conversation", {})
        session_id = conversation.get("session_id") or conversation.get("fork_from")
        if not session_id:
            raise ValueError("This Order has no saved Claude conversation")
        argv = ["claude", "--resume", session_id]
        if not conversation.get("session_id"):
            argv.append("--fork-session")
        return {"cwd": str(nb.project), "argv": argv, "command": " ".join(shell_quote(v) for v in argv)}
    elif args.command == "sync":
        for cell in nb.cells:
            if meta(cell).get("role") == "order" and meta(cell).get("latest_run"):
                run = nb.run(meta(cell)["latest_run"])
                if run["input_fingerprint"] != nb.fingerprint(meta(cell)["order_id"]):
                    meta(cell)["status"] = "stale"
                nb.render(cell)
    nb.export_inputs()
    nb.save()
    return {"notebook": str(nb.path), "status": "updated"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "inspect", "set-global", "add-order", "update-order", "prepare", "complete",
                 "finish", "merge", "split", "conversation", "sync"):
        command = commands.add_parser(name)
        command.add_argument("notebook")
        if name == "init":
            command.add_argument("--project")
            command.add_argument("--plan")
        if name == "set-global":
            command.add_argument("--file", required=True)
        if name in ("add-order", "update-order", "merge", "split"):
            command.add_argument("--spec", required=True)
        if name in ("update-order", "prepare", "split", "conversation"):
            command.add_argument("--cell", required=True)
        if name == "merge":
            command.add_argument("--cells", nargs=2, required=True)
        if name in ("complete", "finish"):
            command.add_argument("--run", required=True)
        if name == "finish":
            command.add_argument("--exit-code", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1 if result.get("status") in ("failed", "stale") else 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
