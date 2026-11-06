"""Run the Python CLI and the Go CLI side by side against one registry and compare what each
printed, its exit status and the request it sent.

The Go binary comes from TREG_GO_BIN, else it is built from `cli/` when `go` is on PATH; without
either, the parity tests skip.
"""
from __future__ import annotations

import json
import os
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
    if given := os.environ.get("TREG_GO_BIN"):
        return Path(given)
    go = shutil.which("go")
    if not go:
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


def normalize(text: str) -> str:
    """The Go CLI writes a plain dash where the Python CLI wrote an em dash."""
    return text.replace(" — ", " - ").replace("—", "-")


def same_stdout(py: Run, go: Run) -> bool:
    if py.out == go.out:
        return True
    try:
        return json.loads(py.out) == json.loads(go.out)
    except ValueError:
        return normalize(py.text) == normalize(go.text)


def write_config(home: Path, **fields) -> None:
    path = home / ".treg" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields))
