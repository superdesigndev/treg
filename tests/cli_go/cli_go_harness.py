"""Run the Python CLI and the Go CLI side by side against one registry and compare what each
printed, its exit status and the request it sent.

The Go binary comes from TREG_GO_BIN, or is built from `cli/` with TREG_GO_BUILD=1; without either,
the parity tests skip (CI runs them in the cli-go job).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYTHON_CLI = [sys.executable, "-c", "from treg.cli import main; main()"]


def build_go(tmp: Path) -> Path | None:
    """The Go binary under test: TREG_GO_BIN, or one built from `cli/` when TREG_GO_BUILD=1."""
    if given := os.environ.get("TREG_GO_BIN"):
        return Path(given)
    go = shutil.which("go")
    if os.environ.get("TREG_GO_BUILD") != "1" or not go:
        return None
    out = tmp / ("treg-go.exe" if os.name == "nt" else "treg-go")
    subprocess.run([go, "build", "-o", str(out), "./cmd/treg"], cwd=ROOT / "cli", check=True)
    return out


@dataclass
class Run:
    code: int
    out: bytes
    err: str

    @property
    def text(self) -> str:
        return self.out.decode("utf-8", "replace")


def cli_env(home: Path, env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment a CLI run gets: this machine's, without any treg or agent variable."""
    base = {k: v for k, v in os.environ.items() if not k.startswith("TREG_") and k not in (
        "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_PID", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
        "CODEX_SANDBOX", "CODEX_CI", "CURSOR_AGENT", "CURSOR_TRACE_ID", "GEMINI_CLI", "NO_COLOR",
        "DO_NOT_TRACK")}
    base |= {"HOME": str(home), "TREG_CONFIG": str(home / ".treg" / "config.json"),
             "TREG_TELEMETRY": "0", "PYTHONIOENCODING": "utf-8"}
    return base | (env or {})


def run(argv: list[str], *, home: Path, env: dict[str, str] | None = None, stdin: bytes | None = None,
        timeout: float = 60) -> Run:
    proc = subprocess.run(argv, input=stdin, capture_output=True, env=cli_env(home, env),
                          timeout=timeout, cwd=home)
    return Run(proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace"))


@dataclass
class Canned:
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeRegistry:
    """Answers every request with the canned response for its path (the first path segment after
    `/call/` names the scenario) and records what was sent."""

    def __init__(self) -> None:
        self.routes: dict[str, list[Canned]] = {}
        self.seen: list[Seen] = []
        registry = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                registry.seen.append(Seen(self.command, self.path,
                                          {k.lower(): v for k, v in self.headers.items()}, body))
                key = self.path.split("?", 1)[0]
                queue = registry.routes.get(key) or registry.routes.get("*") or [Canned(404, {}, b'{"detail":"no route"}')]
                answer = queue.pop(0) if len(queue) > 1 else queue[0]
                self.send_response(answer.status)
                for k, v in answer.headers.items():
                    self.send_header(k, v)
                if not any(k.lower() == "content-type" for k in answer.headers):
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(answer.body)))
                self.end_headers()
                self.wfile.write(answer.body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def route(self, path: str, *answers: Canned) -> None:
        self.routes[path] = list(answers)

    def close(self) -> None:
        self.server.shutdown()


_ERRNO = re.compile(r"\[Errno \d+\] ([^:\n]+): '([^'\n]*)'")
_CRASH = re.compile(r"^treg: (?:FileNotFoundError|PermissionError|IsADirectoryError|OSError): ", re.M)
_LIST = re.compile(r"\[('(?:[^'\n]*)'(?:, '(?:[^'\n]*)')*)\]")
_REPR = re.compile(r"(?<![\w])'([^'\n]*)'(?![\w])")
_PY_JSON_ERROR = re.compile(r"(?:Expecting [^:\n]*|Extra data|Unterminated string starting at|Invalid [^:\n]*"
                            r"|Illegal trailing comma[^:\n]*): line \d+ column \d+ \(char \d+\)")
_GO_JSON_ERROR = re.compile(r"jsontext: [^\n]*? after offset \d+|jsontext: [^\n]*|invalid JSON: [a-z ]+")
_MONEY = re.compile(r"( *)\$(\d[\d,]*(?:\.\d+)?(?:e-\d+)?)")
_NO_TEAM = ("You're not in a team yet: create one with `treg org create \"Your Team\"` or accept an invite.")


_JSON_LINE = re.compile(r"^(\s*)([\[{].*[\]}])[ \t]*$", re.M)


def _json_line(m: re.Match) -> str:
    """A line holding one JSON value, in one spelling: the Python CLI wrote ", " and ": " and
    escaped non-ASCII, the Go CLI writes it compact."""
    try:
        value = json.loads(m.group(2))
    except ValueError:
        return m.group(0)
    return m.group(1) + json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _money(m: re.Match) -> str:
    """An amount to four decimals under a dollar and cents above, after one space: the Python CLI
    wrote `%g` or four decimals, the Go CLI cents and every micro-dollar under a dollar, so a
    column padded to the amount's width pads differently."""
    value = float(m.group(2).replace(",", ""))
    pad = " " if m.group(1) else ""
    return pad + (f"${value:,.2f}" if value >= 1 else f"${value:.4f}")


def normalize(text: str) -> str:
    """Each CLI's text in one spelling, so a comparison sees only real differences. These are the
    Go CLI's intentional ones: a plain dash for an em dash, a value quoted as Go quotes it ("x",
    where Python wrote 'x' and a list as ['a', 'b']), the system's reason for a file it cannot read
    instead of Python's errno text and exception name, its own JSON parser's errors, dollars with
    cents (every micro-dollar under a dollar), JSON in its own spelling (a line holding one value is
    compared as that value), and no "None" where the Python CLI printed an absent value."""
    text = text.replace(" — ", " - ").replace("—", "-")
    text = _ERRNO.sub(lambda m: f"{m.group(2)}: {m.group(1).lower()}", text)
    text = _CRASH.sub("treg: cannot read ", text)
    text = _LIST.sub(lambda m: ", ".join(item.strip("'") for item in m.group(1).split(", ")), text)
    text = _PY_JSON_ERROR.sub("<invalid JSON>", text)
    text = _GO_JSON_ERROR.sub("<invalid JSON>", text)
    text = _REPR.sub(lambda m: json.dumps(m.group(1), ensure_ascii=False), text)
    text = _MONEY.sub(_money, text)
    text = text.replace("Active org: None", _NO_TEAM).replace(" (None)", "")
    text = re.sub(r'"None (?=\S)', '"', text)
    return _JSON_LINE.sub(_json_line, text)


def as_json(data: bytes):
    """A body or file as the value it holds, so two spellings of one JSON value compare equal: the
    Go CLI writes JSON without Python's spacing, escapes and number spelling."""
    try:
        return json.loads(data)
    except ValueError:
        return data


def same_stdout(py: Run, go: Run) -> bool:
    if py.out == go.out:
        return True
    try:
        if json.loads(py.out) == json.loads(go.out):
            return True
    except ValueError:
        pass
    py_text, go_text = normalize(py.text), normalize(go.text)
    return py_text == go_text or as_json(py_text.encode()) == as_json(go_text.encode())


def write_config(home: Path, **fields) -> None:
    path = home / ".treg" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields))
