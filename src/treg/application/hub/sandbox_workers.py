"""Cloudflare Workers engine for the hub script sandbox.

The parent in `sandbox.py` still admits every call, charge and log. This module only carries
those messages: it POSTs the script to the loader, and the loader's isolate calls back to a
bridge that has no provider key. The bridge token is for this run, in this process.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from ...config import get_settings
from .sandbox import MAX_LINE_BYTES, MAX_OUTPUT_BYTES, SandboxError, _Host

_KINDS = {"script", "timeout", "memory", "stack", "output", "refused", "protocol"}
_MEMORY = ("memory limit", "exceeded memory", "out of memory")


def sign_run(secret: str, body: bytes, *, ttl_s: int) -> str:
    """`exp.hex`, HMAC-SHA256 over `exp.` plus the raw body. The loader checks this before it
    runs anything, so a captured body cannot be pointed at a different bridge."""
    exp = int(time.time()) + ttl_s
    mac = hmac.new(secret.encode(), f"{exp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"{exp}.{mac}"


def _kind(kind: str, message: str) -> str:
    folded = message.lower()
    if any(part in folded for part in _MEMORY):
        return "memory"
    return kind if kind in _KINDS else "script"


def _same(got: str, want: str) -> bool:
    if len(got) != len(want):
        return False
    return hmac.compare_digest(got, want)


def _bearer(header: str) -> str:
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "bearer":
        return ""
    return rest.strip()


@dataclass
class _Live:
    host: _Host
    jti: str
    begun: bool = False
    lock: asyncio.Lock | None = None

    def __post_init__(self) -> None:
        self.lock = asyncio.Lock()


class _Listener:
    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self._runs: dict[str, _Live] = {}
        self._starting: asyncio.Lock | None = None
        self.local_url = ""

    async def ensure(self) -> None:
        if self._server is not None:
            return
        if self._starting is None:
            self._starting = asyncio.Lock()
        async with self._starting:
            if self._server is not None:
                return
            settings = get_settings()
            self._server = await asyncio.start_server(
                self._on_conn, settings.hub_workers_callback_host, settings.hub_workers_callback_port)
            sock = self._server.sockets[0].getsockname()
            host = sock[0]
            if host in {"0.0.0.0", "::"}:
                host = "127.0.0.1"
            self.local_url = f"http://{host}:{sock[1]}/bridge"

    def advertised(self) -> str:
        configured = get_settings().hub_workers_callback_url.strip()
        return configured or self.local_url

    def open(self, token: str, jti: str, host: _Host) -> None:
        self._runs[token] = _Live(host, jti)

    def close(self, token: str) -> None:
        self._runs.pop(token, None)

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            status, payload = await self._serve(reader)
        except (asyncio.IncompleteReadError, ValueError, UnicodeError, SandboxError):
            status, payload = 400, {"op": "error", "kind": "protocol", "message": "bad bridge request"}
        except (ConnectionError, OSError, RuntimeError, asyncio.CancelledError):
            status, payload = 400, {"op": "error", "kind": "protocol", "message": "bad bridge request"}
        body = json.dumps(payload, separators=(",", ":")).encode()
        reason = {200: "OK", 400: "Bad Request", 401: "Unauthorized", 413: "Payload Too Large"}.get(status, "Error")
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
        try:
            writer.write(head + body)
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        finally:
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader) -> tuple[int, dict]:
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                return 400, {"op": "error", "kind": "protocol", "message": "bad bridge request"}
            buf += chunk
            if len(buf) > 65536 and b"\r\n\r\n" not in buf:
                return 413, {"op": "error", "kind": "protocol", "message": "bridge header is over 64 KiB"}
        head, rest = buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            return 400, {"op": "error", "kind": "protocol", "message": "bad bridge request"}
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.lower().strip()] = value.strip()
        length = int(headers.get("content-length", "0"))
        if length < 0 or length > MAX_LINE_BYTES:
            return 413, {"op": "error", "kind": "protocol", "message": "bridge body is over the line cap"}
        body = rest
        if len(body) < length:
            body += await reader.readexactly(length - len(body))
        body = body[:length]
        path = urlsplit(target).path
        if method != "POST" or path != "/bridge":
            return 404, {"op": "error", "kind": "protocol", "message": "bad bridge request"}
        token = _bearer(headers.get("authorization", ""))
        try:
            msg = json.loads(body) if body else {}
        except ValueError:
            return 400, {"op": "error", "kind": "protocol", "message": "bridge body is not JSON"}
        if not isinstance(msg, dict):
            return 400, {"op": "error", "kind": "protocol", "message": "bridge body is not JSON"}
        return await self._dispatch(token, msg)

    async def _dispatch(self, token: str, msg: dict) -> tuple[int, dict]:
        live = self._runs.get(token)
        if live is None or not token:
            return 401, {"op": "error", "kind": "protocol", "message": "bridge rejected the run"}
        op = msg.get("op")
        if op == "begin":
            assert live.lock is not None
            async with live.lock:
                if live.begun or not _same(str(msg.get("jti", "")), live.jti):
                    return 401, {"op": "error", "kind": "protocol", "message": "bridge rejected the run"}
                live.begun = True
            return 200, {"op": "ok"}
        if not live.begun:
            return 401, {"op": "error", "kind": "protocol", "message": "bridge rejected the run"}
        host = live.host
        try:
            if op == "logs":
                host.add_logs(msg.get("lines"))
                return 200, {"op": "ok"}
            if op == "charge":
                host.on_charge(msg.get("usd"), msg.get("label", ""))
                return 200, {"op": "ok"}
            if op == "call":
                try:
                    host.admit_call(msg)
                except SandboxError as exc:
                    return 200, {"op": "refused", "id": msg.get("id"), "error": exc.message}
                return 200, await host.perform_call(msg)
        except SandboxError as exc:
            host.failure = host.failure or exc
            return 200, {"op": "refused", "id": msg.get("id"), "error": exc.message}
        return 400, {"op": "error", "kind": "protocol", "message": f"unknown message {op!r}"}


listener = _Listener()


def run_body(script: str, inputs: dict[str, Any], data: Any, *, wall_s: int, jti: str,
             bridge_url: str, bridge_token: str) -> bytes:
    return json.dumps({
        "script": script, "inputs": inputs, "data": data, "wall_s": wall_s, "jti": jti,
        "bridge_url": bridge_url, "bridge_token": bridge_token,
    }, separators=(",", ":"), ensure_ascii=False).encode()


async def _post(url: str, body: bytes, auth: str, host: _Host) -> tuple[int, bytes]:
    async with httpx.AsyncClient(timeout=None) as client:
        async def go() -> tuple[int, bytes]:
            async with client.stream("POST", url, content=body, headers={
                "authorization": f"Bearer {auth}", "content-type": "application/json",
            }) as resp:
                length = resp.headers.get("content-length")
                if length is not None and length.isdigit() and int(length) > MAX_OUTPUT_BYTES + 65_536:
                    raise SandboxError("output", f"the returned object must be JSON under {MAX_OUTPUT_BYTES} bytes")
                return resp.status_code, await resp.aread()
        return await asyncio.wait_for(go(), timeout=max(host.left(), 0.05))


async def run(host: _Host, script: str, inputs: dict[str, Any], data: Any) -> dict[str, Any]:
    settings = get_settings()
    await listener.ensure()
    token = secrets.token_urlsafe(32)
    jti = secrets.token_urlsafe(18)
    bridge_url = listener.advertised()
    listener.open(token, jti, host)
    body = run_body(script, inputs, data, wall_s=host.wall_s, jti=jti, bridge_url=bridge_url, bridge_token=token)
    auth = sign_run(settings.hub_workers_auth_secret, body, ttl_s=host.wall_s + 120)
    host.deadline = asyncio.get_running_loop().time() + host.wall_s + 2
    try:
        try:
            status, raw = await _post(settings.hub_workers_url.rstrip("/") + "/run", body, auth, host)
        except asyncio.TimeoutError:
            raise SandboxError("timeout", f"the run passed its {host.wall_s} s wall clock") from None
        except SandboxError:
            raise
        except (httpx.HTTPError, OSError) as exc:
            if host.left() <= 0:
                raise SandboxError("timeout", f"the run passed its {host.wall_s} s wall clock") from None
            raise SandboxError("protocol", "the workers sandbox is unreachable") from exc
        if host.failure is not None:
            raise host.failure
        if status == 401:
            raise SandboxError("protocol", "the workers sandbox rejected the run")
        try:
            msg = json.loads(raw)
        except ValueError:
            raise SandboxError("protocol", "the workers sandbox wrote a response that is not JSON") from None
        if not isinstance(msg, dict) or "op" not in msg:
            raise SandboxError("protocol", "the workers sandbox ended without an answer")
        if msg.get("op") == "error":
            message = str(msg.get("message", ""))[:600]
            raise SandboxError(_kind(str(msg.get("kind", "script")), message), message)
        if msg.get("op") == "done":
            out = host.take_output(msg.get("output"))
            encoded = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode()
            if len(encoded) > MAX_OUTPUT_BYTES:
                raise SandboxError("output", f"the returned object must be JSON under {MAX_OUTPUT_BYTES} bytes")
            return out
        raise SandboxError("protocol", "the workers sandbox ended without an answer")
    finally:
        listener.close(token)
