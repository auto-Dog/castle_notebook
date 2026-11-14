"""Persistent Python worker. Protocol uses a private stream, never user stdout."""
from __future__ import annotations

import base64
import codecs
import io
import json
import locale
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import traceback
import types

CONTROL_IN, CONTROL_OUT = sys.stdin, sys.stdout
EXECUTION_ID = None
SHELL = None
OUTPUT_LOCK = threading.Lock()
OUTPUT_MIME = "application/vnd.castle.output+json"


def emit(kind, **payload):
    with OUTPUT_LOCK:
        CONTROL_OUT.write(json.dumps({"id": EXECUTION_ID, "type": kind, **payload},
                                     ensure_ascii=False, allow_nan=False) + "\n")
        CONTROL_OUT.flush()


def publish_output(output):
    # Publishing through IPython also lets %%capture collect Castle outputs.
    SHELL.display_pub.publish({OUTPUT_MIME: output})


class OutputStream(io.TextIOBase):
    def __init__(self, channel):
        self.channel = channel

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def isatty(self):
        return False

    def write(self, text):
        if text:
            for offset in range(0, len(text), 8000):
                emit("stream", channel=self.channel, text=text[offset:offset + 8000])
        return len(text)

    def flush(self):
        pass


class ShellDecoder(codecs.IncrementalDecoder):
    """Python children use UTF-8; Windows shell builtins use the native code page."""
    def __init__(self):
        super().__init__()
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")

    def decode(self, data, final=False):
        pending, _ = self.decoder.getstate()
        try:
            return self.decoder.decode(data, final=final)
        except UnicodeDecodeError:
            self.decoder = codecs.getincrementaldecoder(locale.getpreferredencoding(False))(errors="replace")
            return self.decoder.decode(pending + data, final=final)

    def getstate(self):
        return self.decoder.getstate()

    def setstate(self, state):
        self.decoder.setstate(state)

    def reset(self):
        self.decoder.reset()


def shell_decoder():
    return io.IncrementalNewlineDecoder(ShellDecoder(), translate=True)


def shell_command(command, capture=False):
    """Keep shell output off the JSON protocol and children in the kernel tree."""
    if command.rstrip().endswith("&"):
        raise OSError("Background processes not supported.")
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    with subprocess.Popen(command, shell=True, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT if capture else subprocess.PIPE,
                          **options) as process:
        if capture:
            output, _ = process.communicate()
            return shell_decoder().decode(output, final=True), process.returncode

        def forward(pipe, stream):
            decoder = shell_decoder()
            with pipe:
                while data := pipe.read1(8000):
                    stream.write(decoder.decode(data))
                stream.write(decoder.decode(b"", final=True))

        readers = [threading.Thread(target=forward, args=(pipe, stream), daemon=True)
                   for pipe, stream in ((process.stdout, sys.stdout), (process.stderr, sys.stderr))]
        for reader in readers:
            reader.start()
        try:
            code = process.wait()
        finally:
            if process.poll() is None:
                process.kill()
            for reader in readers:
                reader.join()
        return None, code


def create_shell(project, namespace):
    from IPython.core.displayhook import DisplayHook
    from IPython.core.displaypub import DisplayPublisher
    from IPython.core.interactiveshell import InteractiveShell
    from IPython.utils.text import LSString, SList
    from traitlets.config import Config

    class NotebookDisplayHook(DisplayHook):
        def __call__(self, result=None):
            self.check_for_underscore()
            if result is not None and not self.quiet():
                self.update_user_ns(result)
                self.fill_exec_result(result)
                display(result)

    class NotebookDisplayPublisher(DisplayPublisher):
        def publish(self, data, metadata=None, source=None, *, transient=None, update=False, **kwargs):
            if OUTPUT_MIME in data:
                output = data[OUTPUT_MIME]
            elif "image/png" in data or "image/jpeg" in data:
                mime = "image/png" if "image/png" in data else "image/jpeg"
                encoded = data[mime]
                if isinstance(encoded, bytes):
                    encoded = base64.b64encode(encoded).decode("ascii")
                output = {"kind": "image", "data": f"data:{mime};base64,{encoded}"}
            elif "text/markdown" in data:
                output = {"kind": "markdown", "text": str(data["text/markdown"])[:100000]}
            elif "application/json" in data:
                output = {"kind": "json", "text": json.dumps(clean(data["application/json"]), ensure_ascii=False)}
            else:
                output = {"kind": "text", "text": str(data.get("text/plain", ""))[:100000]}
            emit("display", output=output)

    class NotebookShell(InteractiveShell):
        def system_piped(self, command):
            _, code = shell_command(self.var_expand(command, depth=1))
            self.user_ns["_exit_code"] = code

        system = system_piped

        def getoutput(self, command, split=True, depth=0):
            text, _ = shell_command(self.var_expand(command, depth=depth + 1), capture=True)
            return SList(text.splitlines()) if split else LSString(text)

        def _showtraceback(self, etype, evalue, stb):
            emit("error", text=self.InteractiveTB.stb2text(stb))

        def show_usage_error(self, exc):
            emit("error", text=f"UsageError: {exc}\n")

    return NotebookShell.instance(user_ns=namespace, colors="nocolor", xmode="Plain",
                                  ipython_dir=str(project / ".castle" / "ipython"),
                                  displayhook_class=NotebookDisplayHook,
                                  display_pub_class=NotebookDisplayPublisher,
                                  config=Config({"HistoryManager": {"hist_file": ":memory:"}}))


def clean(value, depth=0):
    if depth > 7:
        return "…"
    if value is None or isinstance(value, (str, bool, int)):
        return value if not isinstance(value, str) else value[:16000]
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, dict):
        return {str(k)[:100]: clean(v, depth + 1) for k, v in list(value.items())[:100]}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in value[:100]]
    if type(value).__module__.startswith("numpy") and hasattr(value, "item"):
        try:
            return clean(value.item(), depth + 1)
        except ValueError:
            return clean(value.tolist(), depth + 1)
    return str(value)[:1000]


def figure_image(figure):
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png", dpi=110, bbox_inches="tight")
    data = buffer.getvalue()
    if len(data) > 1500000:
        raise ValueError("图像大于 1.5 MB，请降低尺寸或 dpi。")
    publish_output({"kind": "image", "data": "data:image/png;base64," + base64.b64encode(data).decode("ascii")})


def display(value):
    module = type(value).__module__
    if module.startswith("matplotlib.figure"):
        figure_image(value)
        if "matplotlib.pyplot" in sys.modules:
            sys.modules["matplotlib.pyplot"].close(value)
    elif module.startswith("pandas") and hasattr(value, "columns"):
        columns = [str(c) for c in value.columns[:30]]
        rows = clean(value.iloc[:100, :30].values.tolist())
        publish_output({"kind": "table", "data": {"columns": columns, "rows": rows}, "truncated": len(value) > 100 or len(value.columns) > 30})
    elif isinstance(value, list) and value and all(isinstance(v, dict) for v in value[:100]):
        columns = list(dict.fromkeys(str(k) for row in value[:100] for k in row))[:30]
        publish_output({"kind": "table", "data": {"columns": columns,
              "rows": [[clean(row.get(k)) for k in columns] for row in value[:100]]}, "truncated": len(value) > 100})
    elif isinstance(value, (dict, list, tuple, bool, int, float)) or value is None:
        publish_output({"kind": "json", "text": json.dumps(clean(value), ensure_ascii=False, indent=2, allow_nan=False)})
    else:
        publish_output({"kind": "text", "text": str(value)[:100000]})


def display_markdown(text):
    publish_output({"kind": "markdown", "text": str(text)[:100000]})


def variables(namespace, hidden=()):
    result = []
    for name, value in list(namespace.items()):
        if name.startswith("_") or name in hidden or name in {"display", "display_markdown"} or isinstance(value, (types.ModuleType, types.FunctionType, type)):
            continue
        kind = type(value).__name__
        shape = None
        if value is None or type(value) in {str, int, float, bool}:
            preview = str(value)[:160]
        elif type(value) in {list, tuple}:
            preview = json.dumps(clean(value[:4]), ensure_ascii=False)[:160]
            shape = str(len(value))
        elif type(value) in {dict, set}:
            preview = f"{len(value)} items"
            shape = str(len(value))
        elif type(value).__module__.startswith(("numpy", "pandas")):
            shape = str(getattr(value, "shape", ""))
            preview = f"{kind} {shape}"
        else:
            preview = f"<{kind}>"
        result.append({"name": name, "type": kind, "preview": preview, "shape": shape})
        if len(result) >= 80:
            break
    return result


def main():
    global EXECUTION_ID, SHELL
    project = Path(sys.argv[1]).resolve()
    os.chdir(project)
    sys.path.insert(0, str(project))
    namespace = {"__name__": "__main__", "display": display, "display_markdown": display_markdown}
    sys.stdin = io.StringIO("")
    sys.stdout, sys.stderr = OutputStream("stdout"), OutputStream("stderr")
    startup_error = None
    try:
        SHELL = create_shell(project, namespace)
    except ImportError as exc:
        startup_error = (f"无法加载 IPython：{exc}。请在内核解释器环境安装依赖：\n"
                         f'"{sys.executable}" -m pip install "ipython>=8.18"\n随后重启内核。')
    emit("ready")
    for line in CONTROL_IN:
        command = json.loads(line)
        EXECUTION_ID = command["id"]
        success = True
        try:
            if startup_error:
                raise RuntimeError(startup_error)
            result = SHELL.run_cell(command["code"], store_history=True, cell_id=command["cell_id"])
            success = result.success
            if "matplotlib.pyplot" in sys.modules:
                pyplot = sys.modules["matplotlib.pyplot"]
                for number in pyplot.get_fignums():
                    figure_image(pyplot.figure(number))
                    pyplot.close(number)
        except BaseException:
            success = False
            emit("error", text=traceback.format_exc(limit=12))
        emit("done", success=success, variables=variables(namespace, SHELL.user_ns_hidden if SHELL else ()))


if __name__ == "__main__":
    main()
