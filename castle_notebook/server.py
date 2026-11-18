"""Loopback-only HTTP application, JSON API and resumable server-sent events."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from .core import (Problem, Store, VERSION, ensure_context_config, ensure_settings,
                   initialization_notebook, load_settings, new_notebook,
                   project_path, read_project_asset, read_project_file, scan_project,
                   settings_summary, text_field)
from .execution import Engine
from .providers.claude import resolve_claude
from .providers.codex import resolve_codex
from .providers.custom import read_transcript, search_sessions

STATIC = Path(__file__).with_name("static")
# Formula rendering (KaTeX) is vendored on first use. The server fetches it in a
# background thread the first time a page asks, so the first paint never waits
# on the network: the frontend loads KaTeX only once /api/math/status says ready.
# Domestic mirror first (the audience is in mainland China), then public CDNs.
MATH_VERSION = "0.16.11"
MATH_MIRRORS = (
    f"https://registry.npmmirror.com/katex/{MATH_VERSION}/files/dist/",
    f"https://cdn.jsdelivr.net/npm/katex@{MATH_VERSION}/dist/",
    f"https://unpkg.com/katex@{MATH_VERSION}/dist/",
)
MATH_ASSETS = (
    "katex.min.js", "katex.min.css",
    "fonts/KaTeX_AMS-Regular.woff2",
    "fonts/KaTeX_Caligraphic-Bold.woff2", "fonts/KaTeX_Caligraphic-Regular.woff2",
    "fonts/KaTeX_Fraktur-Bold.woff2", "fonts/KaTeX_Fraktur-Regular.woff2",
    "fonts/KaTeX_Main-Bold.woff2", "fonts/KaTeX_Main-BoldItalic.woff2",
    "fonts/KaTeX_Main-Italic.woff2", "fonts/KaTeX_Main-Regular.woff2",
    "fonts/KaTeX_Math-BoldItalic.woff2", "fonts/KaTeX_Math-Italic.woff2",
    "fonts/KaTeX_SansSerif-Bold.woff2", "fonts/KaTeX_SansSerif-Italic.woff2",
    "fonts/KaTeX_SansSerif-Regular.woff2", "fonts/KaTeX_Script-Regular.woff2",
    "fonts/KaTeX_Size1-Regular.woff2", "fonts/KaTeX_Size2-Regular.woff2",
    "fonts/KaTeX_Size3-Regular.woff2", "fonts/KaTeX_Size4-Regular.woff2",
    "fonts/KaTeX_Typewriter-Regular.woff2",
)
MATH_FILES = {name: tuple(base + name for base in MATH_MIRRORS) for name in MATH_ASSETS}
MATH_ROOT = STATIC / "math"
MATH_TIMEOUT = 8
MATH_RETRY = 60.0


class App:
    def __init__(self, project, data_dir=None, python=None, claude_command=None,
                 seed=True, code_timeout=60, agent_timeout=240,
                 claude_launch_mode="background", claude_permission_mode="auto",
                 provider="claude", codex_command=None):
        self.project = Path(project).resolve()
        if not self.project.is_dir():
            raise Problem("项目目录不存在。")
        self.token = secrets.token_urlsafe(32)
        self.store = Store(data_dir or self.project / ".castle")
        self.context_config_path = ensure_context_config(self.project)
        self.settings_path = ensure_settings(self.project)
        self.engine = Engine(self.store, python, claude_command, code_timeout, agent_timeout,
                             claude_launch_mode, claude_permission_mode, provider, codex_command)
        custom_dir = settings_summary(self.project)["custom_sessions_dir"]
        self.engine.providers["custom"].sessions_dir = Path(custom_dir) if custom_dir else None
        if seed and not self.store.list():
            self.store.put(new_notebook(self.project, template="quant"))

    def close(self):
        self.engine.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        super().__init__(address, Handler)
        self.app = app
        self.math = MathAssets()


class MathAssets:
    """Serve KaTeX locally, fetching it once in the background on first request.

    No page ever blocks on the network: the first /api/math/status call only
    schedules the download and answers immediately, and the frontend retries.
    """

    def __init__(self, root=MATH_ROOT, files=None, fetch=None):
        self.root = Path(root)
        self.files = dict(files if files is not None else MATH_FILES)
        self.fetch = fetch
        self.lock = threading.Lock()
        self.status = {name: self._present(name) for name in self.files}
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="castle-math")
        self.job = None
        self.started_at = 0.0
        self.done = False
        self.changed = threading.Event()

    def _present(self, name):
        path = self.root / name
        return path.is_file() and path.stat().st_size > 0

    def path(self, name):
        """Resolve a request path inside the asset directory, or None."""
        target = (self.root / name).resolve()
        root = self.root.resolve()
        return target if target == root or root in target.parents else None

    def snapshot(self):
        with self.lock:
            primary = "katex.min.js" if "katex.min.js" in self.status else next(iter(self.status), None)
            return {"ready": all(self.status.values()), "downloading": self.job is not None and not self.done,
                    "offline": self.done and not self.status.get(primary, True),
                    "missing": [name for name, ok in self.status.items() if not ok]}

    def start(self):
        """Kick off the background download; retry coolly after a failed attempt."""
        with self.lock:
            if all(self.status.values()):
                return
            if self.job is not None and not self.done:
                return
            if self.job is not None and time.monotonic() - self.started_at < MATH_RETRY:
                return
            self.started_at = time.monotonic()
            self.done = False
            self.job = self.pool.submit(self._download)

    def request(self):
        """Bundle the download trigger with the status read to avoid a poll race."""
        self.start()
        return self.snapshot()

    def _download(self):
        for name, sources in self.files.items():
            if self.status.get(name):
                continue
            data = self._fetch(name, sources if isinstance(sources, (list, tuple)) else (sources,))
            if not data:
                continue
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(path.name + ".part")
            temporary.write_bytes(data)
            temporary.replace(path)
            with self.lock:
                self.status[name] = True
        with self.lock:
            self.done = True
        self.changed.set()

    def _fetch(self, name, sources):
        for url in sources:
            if self.fetch:
                try:
                    data = self.fetch(url, name)
                except Exception:
                    data = None
                if data:
                    return data
            try:
                request = Request(url, headers={"User-Agent": f"CastleNotebook/{VERSION}"})
                with urlopen(request, timeout=MATH_TIMEOUT) as response:
                    return response.read()
            except Exception:
                continue
        return None

    def wait(self, timeout=10.0):
        """Block up to timeout for the current download to finish (tests)."""
        deadline = time.monotonic() + timeout
        while not self.done and time.monotonic() < deadline:
            self.changed.wait(0.05)
            self.changed.clear()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CastleNotebook/0.9"

    @property
    def app(self):
        return self.server.app

    def log_message(self, format, *args):
        # Do not log cell source, context, tokens or local project contents.
        if len(args) > 1 and str(args[1]).startswith("5"):
            sys.stderr.write(f"CastleNotebook HTTP {args[1]}\n")

    def security_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data: https: http:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

    def send_bytes(self, data, content_type, status=200, filename=None, cache="no-store"):
        self.send_response(status)
        self.security_headers()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def json(self, data, status=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8"), "application/json; charset=utf-8", status)

    def body(self, limit=4500000):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise Problem("Content-Length 无效。") from exc
        if not 0 <= length <= limit:
            self.close_connection = True
            raise Problem(f"请求体超过 {limit / 1000000:g} MB 限制。", 413)
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise Problem("请求需要 application/json。", 415)
        try:
            data = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError) as exc:
            raise Problem("请求不是有效 JSON。") from exc
        if not isinstance(data, dict):
            raise Problem("请求必须是 JSON 对象。")
        return data

    def guard(self, mutation=False):
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        if host not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
            self.close_connection = True
            raise Problem("服务只接受本机地址。", 403)
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{host}":
            self.close_connection = True
            raise Problem("拒绝跨站请求。", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            self.close_connection = True
            raise Problem("拒绝跨站请求。", 403)
        if mutation and not secrets.compare_digest(self.headers.get("X-Castle-Token", ""), self.app.token):
            self.close_connection = True
            raise Problem("页面会话已过期，请刷新后重试。", 403)

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_OPTIONS(self):
        self.json({"error": "不支持跨站访问。"}, 403)

    def dispatch(self, method):
        try:
            self.guard(method != "GET")
            self.route(method)
        except Problem as exc:
            self.json({"error": str(exc)}, exc.status)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception:
            self.json({"error": "本地服务发生异常。请检查终端日志。"}, 500)
            import traceback
            traceback.print_exc()

    def route(self, method):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        query = parse_qs(parsed.query)
        if method == "GET" and parts == ["api", "health"]:
            return self.json({"ok": True, "version": VERSION})
        if method == "GET" and parts == ["api", "math", "status"]:
            return self.json(self.server.math.request())
        if method == "GET" and len(parts) >= 3 and parts[:2] == ["api", "math"] and parts[2] != "status":
            return self.math_asset("/".join(parts[2:]))
        if method == "GET" and parts == ["api", "bootstrap"]:
            return self.json({"token": self.app.token, "version": VERSION,
                              "project_path": str(self.app.project), "notebooks": self.app.store.list(),
                              "context_config_path": str(self.app.context_config_path),
                              "settings": settings_summary(self.app.project),
                              "capabilities": {"claude": bool(self.app.engine.providers["claude"].command),
                                               "codex": bool(self.app.engine.providers["codex"].command),
                                               "custom": True,
                                               "default_provider": self.app.engine.default_provider,
                                               "python": self.app.engine.python,
                                               "claude_launch_mode": self.app.engine.claude_launch_mode,
                                               "claude_permission_mode": self.app.engine.claude_permission_mode,
                                               "code_timeout": self.app.engine.code_timeout,
                                               "agent_timeout": self.app.engine.agent_timeout}})
        if parts == ["api", "notebooks"]:
            if method == "GET":
                return self.json(self.app.store.list())
            if method == "POST":
                body = self.body()
                template = body.get("template", "blank")
                if template not in {"blank", "quant", "algorithm"}:
                    raise Problem("不支持的模板。")
                project = project_path(self.app.project, body.get("relative_path", "."))
                ensure_context_config(project)
                doc = new_notebook(project, text_field(body.get("title", "未命名 Notebook"), "标题", 200), template)
                self.app.store.put(doc)
                return self.json(self.app.engine.document(doc["id"]), 201)
        if parts == ["api", "notebooks", "import"] and method == "POST":
            # Exported notebooks carry embedded images, so the import payload
            # may exceed the regular edit limit (100 MB of images plus JSON).
            body = self.body(120000000)
            project = project_path(self.app.project, body.get("relative_path", "."))
            ensure_context_config(project)
            return self.json(self.app.engine.import_notebook(body.get("notebook"), project), 201)
        if parts == ["api", "project", "scan"] and method == "POST":
            return self.json(scan_project(self.app.project, self.body().get("relative_path", "."), excluded=(self.app.store.path,)))
        if parts == ["api", "project", "initialize"] and method == "POST":
            body = self.body()
            scan = scan_project(self.app.project, body.get("relative_path", "."), excluded=(self.app.store.path,))
            doc = initialization_notebook(scan)
            self.app.store.put(doc)
            return self.json({"notebook": self.app.engine.document(doc["id"])}, 201)
        if parts == ["api", "custom", "directory"] and method == "PUT":
            value = text_field(self.body().get("path", ""), "对话目录", 4000).strip()
            if value:
                directory = Path(value).expanduser()
                if not directory.is_dir():
                    raise Problem("对话目录不存在或不是文件夹。")
                value = str(directory.resolve())
            settings = load_settings(self.app.project)
            settings["custom_sessions_dir"] = value
            self.app.store.atomic_json(self.app.settings_path, settings)
            self.app.engine.providers["custom"].sessions_dir = Path(value) if value else None
            return self.json(settings_summary(self.app.project))
        if parts == ["api", "custom", "sessions", "search"] and method == "POST":
            body = self.body()
            keyword = text_field(body.get("keyword", ""), "关键词", 200).strip()
            bounds = []
            for field in ("date_from", "date_to"):
                raw = text_field(body.get(field, "") or "", field, 10).strip()
                try:
                    bounds.append(date.fromisoformat(raw) if raw else None)
                except ValueError as exc:
                    raise Problem("日期必须为 YYYY-MM-DD 格式。") from exc
            if bounds[0] and bounds[1] and bounds[0] > bounds[1]:
                raise Problem("开始日期不能晚于结束日期。")
            return self.json(search_sessions(self.app.engine.providers["custom"].sessions_dir,
                                             keyword, bounds[0], bounds[1]))
        if len(parts) == 5 and parts[:3] == ["api", "custom", "sessions"] and parts[4] == "transcript" and method == "GET":
            return self.json(read_transcript(self.app.engine.providers["custom"].sessions_dir, parts[3]))
        if len(parts) >= 3 and parts[:2] == ["api", "notebooks"]:
            nid = parts[2]
            if len(parts) == 3:
                if method == "GET":
                    return self.json(self.app.engine.document(nid))
                if method == "PUT":
                    return self.json(self.app.engine.save(nid, self.body()))
            if parts[3:] == ["project"] and method == "GET":
                doc = self.app.store.get(nid)
                return self.json(scan_project(doc["project_path"], excluded=(self.app.store.path,)))
            if parts[3:] == ["project", "file"] and method == "GET":
                doc = self.app.store.get(nid)
                return self.json(read_project_file(doc["project_path"], query.get("path", [""])[0],
                                                   excluded=(self.app.store.path,)))
            if parts[3:] == ["asset"] and method == "GET":
                doc = self.app.store.get(nid)
                data, content_type = read_project_asset(doc["project_path"], query.get("path", [""])[0],
                                                        excluded=(self.app.store.path,))
                return self.send_bytes(data, content_type)
            if len(parts) == 4 and parts[3] == "export" and method == "GET":
                doc = self.app.engine.sync_output_files(nid)
                return self.send_bytes(json.dumps(doc, ensure_ascii=False, indent=2).encode("utf-8"), "application/json; charset=utf-8", filename=f"castle-{doc['id']}.json")
            if parts[3:] == ["outputs"] and method == "GET":
                return self.json(self.app.engine.order_outputs(nid, query.get("after", [""])[0]))
            if len(parts) == 4 and parts[3] == "global" and method == "PUT":
                return self.json(self.app.engine.save_global(nid, self.body()))
            if parts[3:] == ["kernel", "reset"] and method == "POST":
                self.body()
                return self.json(self.app.engine.reset(nid))
            if parts[3:] == ["apply-init"] and method == "POST":
                return self.json(self.app.engine.apply_init(nid, self.body().get("run_id")))
            if len(parts) == 6 and parts[3] == "cells":
                cid = parts[4]
                if parts[5] == "context" and method == "GET":
                    return self.json(self.app.engine.context(nid, cid))
                if parts[5] == "run" and method == "POST":
                    self.body()
                    return self.json(self.app.engine.start(nid, cid), 202)
                if parts[5] in {"merge", "split"} and method == "POST":
                    return self.json(self.app.engine.transform_cells(nid, cid, parts[5], self.body()), 202)
                if parts[5] == "open-session" and method == "POST":
                    self.body()
                    return self.json(self.app.engine.open_session(nid, cid))
                if parts[5] == "custom-session" and method == "POST":
                    session_id = text_field(self.body().get("session_id", ""), "会话 ID", 128)
                    return self.json(self.app.engine.bind_custom_session(nid, cid, session_id))
        if len(parts) >= 3 and parts[:2] == ["api", "runs"]:
            rid = parts[2]
            if len(parts) == 3 and method == "GET":
                return self.json(self.app.engine.get_run(rid))
            if len(parts) == 4:
                if parts[3] == "cancel" and method == "POST":
                    self.body()
                    self.app.engine.cancel(rid)
                    return self.json({"ok": True})
                if parts[3] == "events" and method == "GET":
                    try:
                        after = max(0, int(self.headers.get("Last-Event-ID") or query.get("after", ["0"])[0]))
                    except ValueError as exc:
                        raise Problem("无效的事件游标。") from exc
                    return self.stream_events(rid, after)
                if parts[3] == "artifact" and method == "GET":
                    name = query.get("name", ["output"])[0]
                    filenames = {"input": "input.md", "output": "output.md", "context": "context.md", "run": "run.json", "events": "events.jsonl",
                                 "changes": "changes.json", "project-before": "project-before.json", "project-after": "project-after.json"}
                    if name not in filenames:
                        raise Problem("找不到该产物。", 404)
                    run = self.app.engine.get_run(rid)
                    path = self.app.store.run_path(run["id"]) / filenames[name]
                    if not path.is_file():
                        raise Problem("产物尚未生成。", 404)
                    return self.send_bytes(path.read_bytes(), "text/plain; charset=utf-8", filename=f"{rid}-{filenames[name]}")
        if method == "GET" and parsed.path in {"/", "/index.html", "/app.js", "/styles.css", "/logo.svg", "/favicon.ico"}:
            name = "index.html" if parsed.path == "/" else parsed.path[1:]
            if name == "favicon.ico":
                name = "logo.svg"
            path = STATIC / name
            if path.is_file():
                content_type = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml"}.get(path.suffix, mimetypes.guess_type(str(path))[0])
                return self.send_bytes(path.read_bytes(), content_type + "; charset=utf-8")
        raise Problem("未找到该资源。", 404)

    def math_asset(self, name):
        math = self.server.math
        path = math.path(name)
        if path is None:
            raise Problem("公式渲染资源路径无效。", 404)
        if not math.status.get(name):
            math.start()
            raise Problem("公式渲染资源正在后台下载，请稍后自动重试。", 404)
        if not path.is_file():
            raise Problem("公式渲染资源尚未就绪。", 404)
        content_type = {"js": "text/javascript", "css": "text/css", "woff2": "font/woff2",
                        "woff": "font/woff", "ttf": "font/ttf"}.get(path.suffix.lstrip("."), "application/octet-stream")
        charset = "; charset=utf-8" if content_type.startswith("text/") else ""
        return self.send_bytes(path.read_bytes(), content_type + charset, cache="public, max-age=86400")

    def stream_events(self, rid, after):
        self.app.engine.get_run(rid)
        self.send_response(200)
        self.security_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        heartbeat = time.monotonic()
        while True:
            events = self.app.engine.events(rid, after)
            for event in events:
                encoded = f"id: {event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
                self.wfile.write(encoded)
                after = event["seq"]
            if events:
                self.wfile.flush()
            if self.app.engine.get_run(rid)["status"] != "running":
                # Synchronize with the terminal event appended under the engine lock.
                if not self.app.engine.events(rid, after):
                    break
                continue
            if time.monotonic() - heartbeat > 10:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                heartbeat = time.monotonic()
            control = self.app.engine.runs.get(rid)
            if control:
                with control["condition"]:
                    control["condition"].wait(timeout=0.5)
            else:
                time.sleep(0.1)


def main():
    parser = argparse.ArgumentParser(description="CastleNotebook 0.9 · 本地 AI 辅助实时调试工作台")
    parser.add_argument("--project", default=".", help="项目根目录，也是执行代码与 Agent 的工作目录")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", help="Notebook 与运行证据的存储目录，默认 <project>/.castle")
    parser.add_argument("--python", default=sys.executable, help="Python 内核解释器，可指定研究环境")
    parser.add_argument("--claude", help="Claude 可执行文件或 npm cli.js 路径")
    parser.add_argument("--codex", help="Codex 可执行文件或 npm codex.js 路径")
    parser.add_argument("--provider", choices=("claude", "codex", "custom"), default="claude",
                        help="Order 单元的默认 AI 提供商；单元内可单独切换。custom 不启动 CLI，等待外部 AI 写入 output.md，并在界面绑定其会话目录")
    parser.add_argument("--claude-launch-mode", choices=("background", "print"), default="background",
                        help="Claude 启动模式：原生后台（默认，可在 claude agents 查看）或非交互流式；Codex 始终为流式子进程")
    parser.add_argument("--claude-permission-mode", default="auto",
                        choices=("bypassPermissions", "auto", "acceptEdits", "default", "plan", "dontAsk"),
                        help="AI 权限模式，默认 auto 自动审批；Codex 映射为沙箱策略（default/plan 只读，bypassPermissions 跳过沙箱）")
    parser.add_argument("--empty", action="store_true", help="不创建首次启动示例")
    parser.add_argument("--code-timeout", type=int, default=60)
    parser.add_argument("--agent-timeout", type=int, default=240, help="Claude print 模式与 Codex 执行秒数上限；Claude 原生后台会话不设执行期限")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.code_timeout <= 3600 or not 1 <= args.agent_timeout <= 3600:
        parser.error("端口必须为 1–65535，超时必须为 1–3600 秒。")
    if args.claude and not resolve_claude(args.claude):
        parser.error("无法解析 Claude 可执行文件；请直接指定 .exe、Unix executable 或 cli.js。")
    if args.codex and not resolve_codex(args.codex):
        parser.error("无法解析 Codex 可执行文件；请直接指定 .exe、Unix executable 或 codex.js。")
    app = App(args.project, args.data_dir, args.python, resolve_claude(args.claude),
              not args.empty, args.code_timeout, args.agent_timeout,
              args.claude_launch_mode, args.claude_permission_mode,
              args.provider, resolve_codex(args.codex))
    try:
        server = Server(("127.0.0.1", args.port), app)
    except OSError as exc:
        app.close()
        parser.exit(1, f"无法启动端口 {args.port}：{exc}\n")
    claude_state = "available" if app.engine.providers["claude"].command else "not found"
    codex_state = "available" if app.engine.providers["codex"].command else "not found"
    custom_dir = app.engine.providers["custom"].sessions_dir
    custom_state = str(custom_dir) if custom_dir else "no sessions directory bound"
    print(f"\n  CastleNotebook {VERSION}\n  Local:    http://127.0.0.1:{args.port}\n  Project:  {app.project}\n  Python:   {app.engine.python}\n  Claude:   {claude_state}\n  Codex:    {codex_state}\n  Custom:   {custom_state}\n  Provider: {app.engine.default_provider} (default)\n", flush=True)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        server.server_close()


if __name__ == "__main__":
    main()
