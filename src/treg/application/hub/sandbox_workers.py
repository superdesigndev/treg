"""Cloudflare Workers engine for the hub script sandbox.

treg dials the loader and keeps one WebSocket open for the run. The script's `ctx.call`,
`ctx.charge` and logs arrive as frames on that socket and are answered on it, so the loader never
connects back: there is no listener, no callback address, and no per-process token to route.
The parent in `sandbox.py` still admits every call, charge and log through `_Host`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any
from urllib.parse import urlsplit

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.frames import CloseCode

from ...config import get_settings
from .sandbox import MAX_LINE_BYTES, MAX_OUTPUT_BYTES, SandboxError, _Host

_KINDS = {"script", "timeout", "memory", "stack", "output", "refused", "protocol"}
_MEMORY = ("memory limit", "exceeded memory", "out of memory")
AUTH_TTL_S = 60
log = logging.getLogger("treg.hub.sandbox")


def sign_run(secret: str, run: str, *, ttl_s: int = AUTH_TTL_S) -> str:
    """`exp.hex`: HMAC-SHA256 over `exp.` plus the run JSON. The loader checks it before it
    loads anything."""
    exp = int(time.time()) + ttl_s
    mac = hmac.new(secret.encode(), f"{exp}.{run}".encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def run_url(base: str) -> str:
    """The loader's `/run` socket. Settings already refused plain http off loopback."""
    parts = urlsplit(base.strip())
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme, parts.scheme)
    return f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}/run"


def run_body(script: str, inputs: dict[str, Any], data: Any, *, wall_s: int) -> str:
    return json.dumps({"script": script, "inputs": inputs, "data": data, "wall_s": wall_s},
                      separators=(",", ":"), ensure_ascii=False)


def _kind(kind: str, message: str) -> str:
    folded = message.lower()
    if any(part in folded for part in _MEMORY):
        return "memory"
    return kind if kind in _KINDS else "script"


async def _answer(host: _Host, msg: dict) -> dict:
    op = msg.get("op")
    try:
        if op == "logs":
            host.add_logs(msg.get("lines"))
            return {"op": "ok"}
        if op == "charge":
            host.on_charge(msg.get("usd"), msg.get("label", ""))
            return {"op": "ok"}
        if op == "call":
            host.admit_call(msg)
            return await host.perform_call(msg)
        raise SandboxError("protocol", f"unknown message {op!r}")
    except SandboxError as exc:
        host.failure = host.failure or exc
        return {"op": "refused", "error": exc.message}


def _result(host: _Host, result: object) -> dict[str, Any]:
    if not isinstance(result, dict) or "op" not in result:
        raise SandboxError("protocol", "the workers sandbox ended without an answer")
    if host.failure is not None:
        raise host.failure
    if result["op"] == "error":
        message = str(result.get("message", ""))[:600]
        raise SandboxError(_kind(str(result.get("kind", "script")), message), message)
    if result["op"] != "done":
        raise SandboxError("protocol", "the workers sandbox ended without an answer")
    out = host.take_output(result.get("output"))
    if len(json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode()) > MAX_OUTPUT_BYTES:
        raise SandboxError("output", f"the returned object must be JSON under {MAX_OUTPUT_BYTES} bytes")
    return out


async def _serve(ws: ClientConnection, host: _Host, in_flight: set[asyncio.Task]) -> dict[str, Any]:
    sending = asyncio.Lock()

    async def reply(frame_id: object, msg: dict) -> None:
        answer = await _answer(host, msg)
        async with sending:
            await ws.send(json.dumps({"t": "reply", "id": frame_id, "reply": answer}, separators=(",", ":")))

    receiver = asyncio.ensure_future(ws.recv())
    in_flight.add(receiver)
    while True:
        if host.failure is not None:
            raise host.failure
        left = host.left()
        if left <= 0:
            raise SandboxError("timeout", f"the run passed its {host.wall_s} s wall clock")
        done, _ = await asyncio.wait(in_flight, timeout=left, return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise SandboxError("timeout", f"the run passed its {host.wall_s} s wall clock")
        for task in done - {receiver}:
            in_flight.discard(task)
            task.result()
        if receiver not in done:
            continue
        in_flight.discard(receiver)
        raw = receiver.result()
        receiver = asyncio.ensure_future(ws.recv())
        in_flight.add(receiver)
        try:
            frame = json.loads(raw)
        except ValueError:
            raise SandboxError("protocol", "the workers sandbox wrote a frame that is not JSON") from None
        if not isinstance(frame, dict):
            raise SandboxError("protocol", "the workers sandbox wrote a frame that is not an object")
        if frame.get("t") == "end":
            return _result(host, frame.get("result"))
        if frame.get("t") != "op" or not isinstance(frame.get("msg"), dict):
            raise SandboxError("protocol", "the workers sandbox wrote an unknown frame")
        in_flight.add(asyncio.ensure_future(reply(frame.get("id"), frame["msg"])))


async def run(host: _Host, script: str, inputs: dict[str, Any], data: Any) -> dict[str, Any]:
    settings = get_settings()
    url = run_url(settings.hub_workers_url)
    body = run_body(script, inputs, data, wall_s=host.wall_s)
    host.deadline = asyncio.get_running_loop().time() + host.wall_s + 2
    try:
        async with asyncio.timeout(max(host.left(), 0.05)):
            ws = await connect(url, max_size=MAX_LINE_BYTES, open_timeout=None, ping_interval=20, ping_timeout=20,
                               close_timeout=1, compression=None)
        async with ws:
            in_flight: set[asyncio.Task] = set()
            try:
                await ws.send(json.dumps({"auth": sign_run(settings.hub_workers_auth_secret, body), "run": body},
                                         separators=(",", ":"), ensure_ascii=False))
                return await _serve(ws, host, in_flight)
            finally:
                for task in in_flight:
                    task.cancel()
                await asyncio.gather(*in_flight, return_exceptions=True)
    except TimeoutError:
        raise SandboxError("timeout", f"the run passed its {host.wall_s} s wall clock") from None
    except ConnectionClosed as exc:
        if host.failure is not None:
            raise host.failure from None
        if host.left() <= 0:
            raise SandboxError("timeout", f"the run passed its {host.wall_s} s wall clock") from None
        if exc.sent is not None and exc.sent.code == CloseCode.MESSAGE_TOO_BIG:
            raise SandboxError("protocol", f"the sandbox wrote a frame over {MAX_LINE_BYTES // (1024 * 1024)} MiB") from None
        log.warning("hub sandbox closed early sent=%s rcvd=%s left_s=%.1f",
                    exc.sent and exc.sent.code, exc.rcvd and exc.rcvd.code, host.left())
        raise SandboxError("protocol", "the workers sandbox closed the run without an answer") from exc
    except (InvalidHandshake, InvalidURI, OSError) as exc:
        log.warning("hub sandbox unreachable %s", type(exc).__name__)
        raise SandboxError("protocol", "the workers sandbox is unreachable") from exc
