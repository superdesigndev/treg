"""The hub's script sandbox — the PARENT side (docs/HUB-DECISIONS.md round 2).

Spawns `python -m treg.hub_sandbox` as a separate short-lived process with the discipline
`treg/runner.py` already has for vendor CLIs: a scrubbed environment (never the server's), a
private temporary HOME, its own process group, POSIX rlimits, a wall-clock kill, and the whole
group killed on every exit. Inside, QuickJS has no network, no file system, no `require`, no
`process`; the only road out is `ctx.call`, which arrives here as one JSON line and is run
through the same step executor the JSON road uses — the same gates, the same key injection, the
same reserve-and-settle, under `{run}:s{n}`.

What this file enforces that the engine cannot: `uses` (a call to a tool outside the manifest's
list is refused and the run stops), the 20-call cap, the run ceiling, the output's declared
fields, and the log caps.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

try:
    import resource
except ImportError:  # pragma: no cover — Windows
    resource = None  # type: ignore[assignment]

MEMORY_MB = 64                 # the engine's own heap cap
PROCESS_RSS_MB = 512           # the whole child process: interpreter + engine + buffers
MAX_CALLS = 20
MAX_LOG_LINES = 50
MAX_LOG_CHARS = 2000
MAX_OUTPUT_BYTES = 2_000_000
MAX_LINE_BYTES = 8 * 1024 * 1024   # one bridge line (a step's answer travels here)


class SandboxError(Exception):
    """The run ended without an output: `kind` is script | timeout | memory | stack | output |
    refused | protocol; `message` is what the maker reads in the run log."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(f"{kind}: {message}")
        self.kind, self.message = kind, message


@dataclass
class CallRequest:
    target: str
    opts: dict[str, Any]


# One ctx.call, executed by the runner. Returns the reply dict the script sees, or raises
# SandboxError("refused", ...) to stop the run.
CallExecutor = Callable[[CallRequest], Awaitable[dict[str, Any]]]


def _child_env(home: str) -> dict[str, str]:
    """NOT the server's env (it holds TREG_SECRET_KEY and every other secret): PATH, a private
    HOME, and what the interpreter needs to find its own packages."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home, "TMPDIR": home,
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def _preexec() -> None:
    if resource is None:
        return
    for res, want in ((resource.RLIMIT_CPU, 130), (resource.RLIMIT_FSIZE, 1_000_000),
                      (resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 32)):
        try:
            soft, hard = resource.getrlimit(res)
            target = want if hard == resource.RLIM_INFINITY else min(want, hard)
            resource.setrlimit(res, (target, hard))
        except (ValueError, OSError):
            pass
    # RLIMIT_AS is the memory wall for the whole process. Darwin ignores it; Linux (prod) honours it.
    try:
        resource.setrlimit(resource.RLIMIT_AS, (PROCESS_RSS_MB * 1024 * 1024, PROCESS_RSS_MB * 1024 * 1024))
    except (ValueError, OSError):
        pass


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def run_script(
    script: str, inputs: dict[str, Any], *, wall_s: int, execute: CallExecutor,
    log: list[str],
) -> dict[str, Any]:
    """Run one script to its output. `execute` runs each ctx.call; `log` receives the script's
    lines. Raises SandboxError when the run cannot produce an output."""
    home = tempfile.mkdtemp(prefix="treg-hub-")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "treg.hub_sandbox",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=_child_env(home), cwd=home, start_new_session=True, preexec_fn=_preexec,
        limit=MAX_LINE_BYTES,
    )
    pgid = proc.pid
    assert proc.stdin and proc.stdout and proc.stderr
    calls = 0
    try:
        proc.stdin.write((json.dumps({"op": "run", "script": script, "inputs": inputs,
                                      "memory_mb": MEMORY_MB, "wall_s": wall_s},
                                     ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()
        deadline = asyncio.get_running_loop().time() + wall_s + 2
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise SandboxError("timeout", f"the run passed its {wall_s} s wall clock")
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=left)
            except asyncio.TimeoutError:
                raise SandboxError("timeout", f"the run passed its {wall_s} s wall clock") from None
            if not line:
                err = (await proc.stderr.read(4000)).decode("utf-8", "replace").strip()
                raise SandboxError("script", err[-600:] or "the sandbox exited without an answer")
            try:
                msg = json.loads(line)
            except ValueError:
                raise SandboxError("protocol", "the sandbox wrote a line that is not JSON") from None
            op = msg.get("op")
            if op == "log":
                if len(log) < MAX_LOG_LINES:
                    log.append(str(msg.get("text", ""))[:MAX_LOG_CHARS])
            elif op == "call":
                calls += 1
                req = CallRequest(target=str(msg.get("target", "")), opts=dict(msg.get("opts") or {}))
                if calls > MAX_CALLS:
                    reply = {"op": "refused", "id": msg["id"],
                             "error": f"the run passed its cap of {MAX_CALLS} calls"}
                    proc.stdin.write((json.dumps(reply) + "\n").encode())
                    await proc.stdin.drain()
                    raise SandboxError("refused", reply["error"])
                try:
                    result = await execute(req)
                    reply = {"op": "result", "id": msg["id"], **result}
                except SandboxError as exc:
                    reply = {"op": "refused", "id": msg["id"], "error": exc.message}
                    proc.stdin.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                    await proc.stdin.drain()
                    raise
                proc.stdin.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
                await proc.stdin.drain()
            elif op == "done":
                out = msg.get("output")
                if not isinstance(out, dict):
                    raise SandboxError("output", "run(ctx) must return one JSON object")
                return out
            elif op == "error":
                raise SandboxError(str(msg.get("kind", "script")), str(msg.get("message", ""))[:600])
            else:
                raise SandboxError("protocol", f"unknown message {op!r} from the sandbox")
    finally:
        _kill_group(pgid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError, asyncio.CancelledError):
            pass
        shutil.rmtree(home, ignore_errors=True)
