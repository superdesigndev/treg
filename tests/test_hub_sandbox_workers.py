"""Workers engine: the loader is not a second call path, and a bad signature never runs.

The contract tests in test_hub_sandbox.py run against this engine too. What lives here is the
part QuickJS cannot show: the HMAC, the one-time bridge, the closed outbound, and a wall-clock
kill of a loop that blocks the whole workerd thread (so it gets its own process).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from treg.application.hub import sandbox, sandbox_workers
from treg.application.hub.sandbox import SandboxError, run_script
from treg.config import get_settings
from tests.hub_workers_runtime import running

SECRET = "hub-workers-test-secret"
LOADER = Path(__file__).resolve().parents[1] / "workers" / "hub-sandbox" / "src" / "index.js"


def _use_workers(monkeypatch, url: str) -> None:
    monkeypatch.setenv("TREG_HUB_SANDBOX", "workers")
    monkeypatch.setenv("TREG_HUB_WORKERS_URL", url)
    monkeypatch.setenv("TREG_HUB_WORKERS_AUTH_SECRET", SECRET)
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def workers_url():
    with running(SECRET) as url:
        yield url


@pytest.fixture(autouse=True)
def _drop_cached_settings(monkeypatch):
    yield
    get_settings.cache_clear()


def test_default_engine_is_quickjs(monkeypatch):
    monkeypatch.delenv("TREG_HUB_SANDBOX", raising=False)
    get_settings.cache_clear()
    assert get_settings().hub_sandbox == "quickjs"


def test_workers_without_a_url_or_secret_does_not_boot(monkeypatch):
    monkeypatch.setenv("TREG_HUB_SANDBOX", "workers")
    monkeypatch.delenv("TREG_HUB_WORKERS_URL", raising=False)
    monkeypatch.delenv("TREG_HUB_WORKERS_AUTH_SECRET", raising=False)
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="hub_workers_url"):
        get_settings()
    get_settings.cache_clear()


def test_the_dynamic_worker_is_not_given_the_auth_secret_or_an_open_network():
    text = LOADER.read_text()
    assert "globalOutbound: null" in text
    env_at = text.index("env: {")
    env_block = text[env_at:text.index("}", env_at)]
    assert "AUTH_SECRET" not in env_block
    assert "BRIDGE" in env_block


async def test_workers_mode_does_not_spawn_quickjs(workers_url, monkeypatch):
    _use_workers(monkeypatch, workers_url)
    spawned: list[tuple] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        spawned.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    out = await run_script(
        "export default async function run(ctx) { return { ok: 1 }; }",
        {}, wall_s=10, execute=None, log=[])
    assert out == {"ok": 1}
    assert not any("hub_sandbox" in str(arg) for args in spawned for arg in args)


async def test_a_script_fetch_does_not_leave_the_isolate(workers_url, monkeypatch):
    _use_workers(monkeypatch, workers_url)
    hits: list[str] = []

    async def handler(reader, writer):
        hits.append("hit")
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        out = await run_script(
            "export default async function run(ctx) {"
            f"  let detail = 'absent'; try {{ await fetch('http://127.0.0.1:{port}/'); detail = 'sent'; }}"
            "  catch (e) { detail = String(e && e.name); }"
            "  return { t: typeof fetch, detail }; }",
            {}, wall_s=10, execute=None, log=[])
    finally:
        server.close()
    assert out["t"] == "undefined"
    assert out["detail"] != "sent"
    assert hits == []


async def test_the_run_body_does_not_carry_the_server_environment(workers_url, monkeypatch):
    _use_workers(monkeypatch, workers_url)
    monkeypatch.setenv("TREG_SECRET_KEY", "must-not-leak-into-the-worker")
    seen: list[bytes] = []

    async def handler(reader, writer):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = await reader.read(4096)
            if not chunk:
                break
            buf += chunk
        if b"\r\n\r\n" in buf:
            head, rest = buf.split(b"\r\n\r\n", 1)
            length = 0
            for line in head.decode("latin-1").split("\r\n")[1:]:
                key, _, value = line.partition(":")
                if key.lower().strip() == "content-length":
                    length = int(value.strip())
            while len(rest) < length:
                rest += await reader.read(length - len(rest))
            seen.append(head + b"\r\n\r\n" + rest[:length])
        writer.write(b"HTTP/1.1 500 ERR\r\nContent-Length: 4\r\nConnection: close\r\n\r\nnope")
        await writer.drain()
        writer.close()

    # A stub loader: it must not be asked to hold a provider key, and the body it is given
    # must not be the server's environment. It still has to redeem the bridge or the run fails.
    stub = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = stub.sockets[0].getsockname()[1]
    monkeypatch.setenv("TREG_HUB_WORKERS_URL", f"http://127.0.0.1:{port}")
    get_settings.cache_clear()
    try:
        # The stub does not speak the bridge, so the run cannot succeed. What it received is the point.
        with pytest.raises(SandboxError):
            await run_script(
                "export default async function run(ctx) { return { ok: 1 }; }",
                {"name": "m"}, wall_s=5, execute=None, log=[])
    finally:
        stub.close()
    assert seen, "the workers client never called the loader"
    blob = b"".join(seen)
    assert b"must-not-leak-into-the-worker" not in blob
    assert b"TREG_SECRET_KEY" not in blob
    assert b"export default" in blob


async def _stub_loader(respond):
    """HTTP loader double. `respond(body, authorization, post)` returns (status, payload dict)."""
    state = {"begins": 0, "bodies": []}

    async def handler(reader, writer):
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += await reader.read(4096)
        head, rest = buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        headers = {}
        for line in lines[1:]:
            key, _, value = line.partition(":")
            headers[key.lower().strip()] = value.strip()
        length = int(headers.get("content-length", "0"))
        body = rest
        while len(body) < length:
            body += await reader.read(length - len(body))
        body = body[:length]
        state["bodies"].append(body)
        auth = headers.get("authorization", "")
        status, payload = await respond(body, auth, state)
        raw = json.dumps(payload).encode()
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\n"
            f"Connection: close\r\n\r\n".encode() + raw)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    return server, url, state


def _authed(body: bytes, auth: str, secret: str) -> bool:
    if not auth.startswith("Bearer "):
        return False
    token = auth.split(" ", 1)[1]
    exp, _, mac = token.partition(".")
    if not exp.isdigit() or len(mac) != 64:
        return False
    expect = hmac.new(secret.encode(), f"{exp}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expect, mac)


async def test_a_matching_signature_is_required_before_begin(monkeypatch):
    async def respond(body, auth, state):
        if not _authed(body, auth, SECRET):
            return 401, {"op": "error", "message": "unauthorized"}
        state["begins"] += 1
        return 200, {"op": "done", "output": {"ok": 1}}

    server, url, state = await _stub_loader(respond)
    _use_workers(monkeypatch, url)
    try:
        out = await run_script(
            "export default async function run(ctx) { return { ok: 1 }; }",
            {}, wall_s=5, execute=None, log=[])
        assert out == {"ok": 1}
        assert state["begins"] == 1
        monkeypatch.setenv("TREG_HUB_WORKERS_AUTH_SECRET", "other-secret")
        get_settings.cache_clear()
        state["begins"] = 0
        with pytest.raises(SandboxError) as e:
            await run_script(
                "export default async function run(ctx) { return { ok: 1 }; }",
                {}, wall_s=5, execute=None, log=[])
        assert e.value.kind == "protocol"
        assert state["begins"] == 0
    finally:
        server.close()


async def test_parent_rejects_an_output_the_worker_failed_to_cap(monkeypatch):
    fat = "a" * (sandbox.MAX_OUTPUT_BYTES + 1)

    async def respond(body, auth, state):
        if not _authed(body, auth, SECRET):
            return 401, {"op": "error"}
        return 200, {"op": "done", "output": {"x": fat}}

    server, url, _state = await _stub_loader(respond)
    _use_workers(monkeypatch, url)
    try:
        with pytest.raises(SandboxError) as e:
            await run_script(
                "export default async function run(ctx) { return { ok: 1 }; }",
                {}, wall_s=5, execute=None, log=[])
    finally:
        server.close()
    assert e.value.kind == "output"


async def test_a_platform_memory_error_is_a_memory_failure(monkeypatch):
    async def respond(body, auth, state):
        if not _authed(body, auth, SECRET):
            return 401, {"op": "error"}
        return 200, {"op": "error", "kind": "script", "message": "Memory limit would be exceeded before EOF"}

    server, url, _state = await _stub_loader(respond)
    _use_workers(monkeypatch, url)
    try:
        with pytest.raises(SandboxError) as e:
            await run_script(
                "export default async function run(ctx) { return { ok: 1 }; }",
                {}, wall_s=5, execute=None, log=[])
    finally:
        server.close()
    assert e.value.kind == "memory"


async def test_a_wrong_bridge_token_is_rejected(workers_url, monkeypatch):
    _use_workers(monkeypatch, workers_url)
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(req):
        started.set()
        await release.wait()
        return {"status": 200, "headers": {}, "json": None, "text": ""}

    task = asyncio.create_task(run_script(
        "export default async function run(ctx) { await ctx.call('hunter.people.email.find'); return { ok: 1 }; }",
        {}, wall_s=10, execute=execute, log=[]))
    await asyncio.wait_for(started.wait(), 10)
    url = sandbox_workers.listener.advertised()
    async with httpx.AsyncClient() as client:
        bad = await client.post(url, json={"op": "call", "id": 1, "target": "x", "opts": {}},
                                headers={"authorization": "Bearer not-the-token"})
    assert bad.status_code == 401
    release.set()
    assert (await task) == {"ok": 1}


async def test_a_replayed_run_does_not_execute_twice(workers_url, monkeypatch):
    _use_workers(monkeypatch, workers_url)
    await sandbox_workers.listener.ensure()
    token = secrets.token_urlsafe(32)
    jti = secrets.token_urlsafe(18)
    calls = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def execute(req):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"status": 200, "headers": {}, "json": {"n": calls}, "text": ""}

    host = sandbox._Host(execute, [], None, 0, 10)
    sandbox_workers.listener.open(token, jti, host)
    try:
        body = sandbox_workers.run_body(
            "export default async function run(ctx) { await ctx.call('hunter.people.email.find'); return { ok: 1 }; }",
            {}, None, wall_s=10, jti=jti,
            bridge_url=sandbox_workers.listener.advertised(), bridge_token=token)
        auth = sandbox_workers.sign_run(SECRET, body, ttl_s=60)

        async def once():
            async with httpx.AsyncClient(timeout=None) as client:
                return await client.post(
                    workers_url.rstrip("/") + "/run", content=body,
                    headers={"authorization": f"Bearer {auth}", "content-type": "application/json"})

        first = asyncio.create_task(once())
        await asyncio.wait_for(started.wait(), 10)
        second = await once()
        assert second.status_code == 401
        assert calls == 1
        release.set()
        first_resp = await first
        assert first_resp.status_code == 200
        assert first_resp.json()["op"] == "done"
    finally:
        sandbox_workers.listener.close(token)


async def test_an_endless_loop_on_the_workers_engine_dies_at_the_wall(monkeypatch):
    """A tight loop blocks workerd's thread, so this loader is not the shared one: killing it is
    the cleanup, and the parent's wait is what ends the run."""
    with running(SECRET) as upstream:
        parsed = urlsplit(upstream)
        hits = 0

        async def relay(reader, writer):
            nonlocal hits
            hits += 1
            remote_r, remote_w = await asyncio.open_connection(parsed.hostname, parsed.port)

            async def pipe(src, dst):
                try:
                    while True:
                        chunk = await src.read(65536)
                        if not chunk:
                            break
                        dst.write(chunk)
                        await dst.drain()
                except (ConnectionError, OSError, RuntimeError, asyncio.CancelledError):
                    pass
                finally:
                    dst.close()

            await asyncio.gather(pipe(reader, remote_w), pipe(remote_r, writer))

        proxy = await asyncio.start_server(relay, "127.0.0.1", 0)
        port = proxy.sockets[0].getsockname()[1]
        # Settings are read inside run_script. Set them without the monkeypatch fixture: this test
        # builds its own process and must not depend on the module-scoped loader.
        _use_workers(monkeypatch, f"http://127.0.0.1:{port}")
        try:
            with pytest.raises(SandboxError) as e:
                await run_script(
                    "export default async function run(ctx) { while (true) {} }",
                    {}, wall_s=2, execute=None, log=[])
            assert e.value.kind == "timeout"
            assert hits >= 1
        finally:
            proxy.close()
