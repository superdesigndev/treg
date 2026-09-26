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
MAX_CHARGES = 20                   # ctx.charge lines a run may add (one per own-key step, at most)
MAX_PARALLEL = 4                   # ctx.calls in flight at once, the JSON road's width
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
    log: list[str], data: list[dict[str, Any]] | None = None,
    charges: list[dict[str, Any]] | None = None, max_charge_micro: int = 0,
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
        proc.stdin.write((json.dumps({"op": "run", "script": script, "inputs": inputs, "data": data,
                                      "memory_mb": MEMORY_MB, "wall_s": wall_s},
                                     ensure_ascii=False) + "\n").encode())
        await proc.stdin.drain()
        deadline = asyncio.get_running_loop().time() + wall_s + 2
        # Calls run as tasks, MAX_PARALLEL at a time (the JSON road's width), and each reply goes
        # back to the child by id the moment its task finishes: five calls in one Promise.all are
        # five in flight. The child's stdout is read by one reader task so a reply write and a
        # pending readline never contend. A refused call still ends the run, as before.
        sem = asyncio.Semaphore(MAX_PARALLEL)
        in_flight: set[asyncio.Task] = set()
        failure: SandboxError | None = None

        async def write(reply: dict) -> None:
            proc.stdin.write((json.dumps(reply, ensure_ascii=False) + "\n").encode())
            await proc.stdin.drain()

        async def one_call(msg: dict) -> None:
            nonlocal failure
            cid = msg.get("id")
            async with sem:
                try:
                    opts = msg.get("opts") or {}
                    if not isinstance(opts, dict):
                        raise SandboxError("refused", "ctx.call's second argument must be an object")
                    left = deadline - asyncio.get_running_loop().time()
                    if left <= 0:
                        raise SandboxError("timeout", f"the run passed its {wall_s} s wall clock")
                    # `timeout_s` on ctx.call: the script's own limit for THIS call. Passing it is
                    # not a run failure: the call answers status 0 with `timed_out: true` and the
                    # script decides (an engine that did not answer in 60 s is "did not answer",
                    # the other four still count). The cancelled child releases its hold.
                    own = opts.get("timeout_s")
                    own = float(own) if isinstance(own, (int, float)) and not isinstance(own, bool) and own > 0 else None
                    budget = min(left, own) if own is not None else left
                    try:
                        result = await asyncio.wait_for(
                            execute(CallRequest(target=str(msg.get("target", "")), opts=opts)), timeout=budget)
                    except asyncio.TimeoutError:
                        if own is not None and budget < left:
                            result = {"status": 0, "headers": {}, "json": None, "text": "",
                                      "timed_out": True, "cost_usd": 0}
                        else:
                            raise SandboxError("timeout", f"the run passed its {wall_s} s wall clock during a call") from None
                    await write({"op": "result", "id": cid, **result})
                except SandboxError as exc:
                    failure = failure or exc
                    try:
                        await write({"op": "refused", "id": cid, "error": exc.message})
                    except (BrokenPipeError, ConnectionResetError, RuntimeError):
                        pass

        reader = asyncio.ensure_future(proc.stdout.readline())
        while True:
            left = deadline - asyncio.get_running_loop().time()
            if left <= 0:
                raise SandboxError("timeout", f"the run passed its {wall_s} s wall clock")
            if failure is not None:
                raise failure
            done, _ = await asyncio.wait({reader, *in_flight}, timeout=left, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise SandboxError("timeout", f"the run passed its {wall_s} s wall clock")
            for task in list(in_flight):
                if task in done:
                    in_flight.discard(task)
                    task.result()          # SandboxError was captured into `failure`; anything else is a bug
            if reader not in done:
                continue
            try:
                line = reader.result()
            except ValueError:
                # readline's limit: one bridge line over MAX_LINE_BYTES (a call body or a log the
                # engine could build but the bridge will not carry)
                raise SandboxError("protocol", f"the sandbox wrote a line over {MAX_LINE_BYTES // (1024 * 1024)} MiB") from None
            if not line:
                err = (await proc.stderr.read(4000)).decode("utf-8", "replace").strip()
                if "Traceback" in err or "MemoryError" in err:
                    # the maker reads this line; a server traceback with paths is not theirs to read
                    err = "the sandbox stopped (out of memory, or an internal error)"
                raise SandboxError("script", err[-600:] or "the sandbox exited without an answer")
            reader = asyncio.ensure_future(proc.stdout.readline())
            try:
                msg = json.loads(line)
            except ValueError:
                raise SandboxError("protocol", "the sandbox wrote a line that is not JSON") from None
            op = msg.get("op")
            if op == "log":
                if len(log) < MAX_LOG_LINES:
                    log.append(str(msg.get("text", ""))[:MAX_LOG_CHARS])
            elif op == "charge":
                # ctx.charge: the script's price, one line at a time (a fee, per result, a margin, a
                # vendor's cost). Refused, and the run stops, when the manifest declares no cap, the
                # sum passes the cap, or the run has already charged 20 times: every line the caller
                # pays is bounded by the `max_price_usd` they saw before the run.
                usd = msg.get("usd")
                if charges is None or max_charge_micro <= 0:
                    raise SandboxError("refused", "ctx.charge needs `pricing.max_price_usd` in the manifest")
                if not isinstance(usd, (int, float)) or isinstance(usd, bool) or not (usd >= 0) or usd != usd:
                    raise SandboxError("refused", "ctx.charge: the amount must be a number of dollars, 0 or more")
                micro = int(round(float(usd) * 1_000_000))
                if sum(c["micro"] for c in charges) + micro > max_charge_micro:
                    raise SandboxError("refused", f"ctx.charge: {usd} would take this run's charges past pricing.max_price_usd ({max_charge_micro / 1_000_000})")
                if len(charges) >= MAX_CHARGES:
                    raise SandboxError("refused", f"the run passed its cap of {MAX_CHARGES} charges")
                if micro > 0:
                    charges.append({"micro": micro, "label": str(msg.get("label", ""))[:80]})
            elif op == "call":
                calls += 1
                if calls > MAX_CALLS:
                    exc = SandboxError("refused", f"the run passed its cap of {MAX_CALLS} calls")
                    await write({"op": "refused", "id": msg.get("id"), "error": exc.message})
                    raise exc
                in_flight.add(asyncio.ensure_future(one_call(msg)))
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
        for task in list(locals().get("in_flight") or []):
            task.cancel()
        r = locals().get("reader")
        if r is not None:
            r.cancel()
        _kill_group(pgid)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError, asyncio.CancelledError):
            pass
        shutil.rmtree(home, ignore_errors=True)
