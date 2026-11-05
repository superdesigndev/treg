"""Workers engine: the isolate has no road out, the loader runs only a signed run, and the run
lives and dies with the socket treg opened.

The contract tests in test_hub_sandbox.py run against this engine too. What lives here is the
part QuickJS cannot show: the isolate's capabilities, the HMAC, the frames, and a wall-clock kill
of a loop that blocks the whole workerd thread (so it gets its own process).
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from treg.application.hub import sandbox, sandbox_workers
from treg.application.hub.sandbox import SandboxError, run_script
from treg.config import get_settings
from tests.hub_workers_runtime import running

SECRET = "hub-workers-test-secret"
NOOP = "export default async function run(ctx) { return { ok: 1 }; }"


def _use_workers(monkeypatch, url: str, secret: str = SECRET) -> None:
    monkeypatch.setenv("TREG_HUB_SANDBOX", "workers")
    monkeypatch.setenv("TREG_HUB_WORKERS_URL", url)
    monkeypatch.setenv("TREG_HUB_WORKERS_AUTH_SECRET", secret)
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


@pytest.mark.parametrize("url", ["http://sandbox.example.com", "ws://sandbox.example.com", "ftp://127.0.0.1"])
def test_a_loader_off_this_machine_must_be_https(monkeypatch, url):
    _use_workers(monkeypatch, url)
    with pytest.raises(ValueError, match="https"):
        get_settings()


@pytest.mark.parametrize("url", ["https://sandbox.example.com", "http://127.0.0.1:8787", "ws://localhost:1"])
def test_https_or_loopback_boots(monkeypatch, url):
    _use_workers(monkeypatch, url)
    assert get_settings().hub_workers_url == url


async def test_the_isolate_has_no_road_out_but_ctx_call(workers_url, monkeypatch):
    """Every way out of a dynamic worker this test knows of, tried from inside a script. A new
    binding or an opened outbound turns one of these into OPEN."""
    _use_workers(monkeypatch, workers_url)
    out = await run_script("""
import { env } from "cloudflare:workers";
export default async function run(ctx) {
  const r = {};
  const t = async (k, f) => { try { await f(); r[k] = "OPEN"; } catch (e) { r[k] = "closed"; } };
  await t("connect", async () => { const { connect } = await import("cloudflare:sockets");
    await connect({ hostname: "1.1.1.1", port: 80 }).opened; });
  await t("fetch", async () => { const f = Object.getPrototypeOf(globalThis).fetch;
    if (!f) throw new Error("absent"); await f("https://example.com"); });
  await t("caches", async () => { await caches.default.put("https://x.test/a", new Response("x")); });
  await t("eval", async () => eval("1"));
  await t("function", async () => new Function("return 1")());
  await t("wasm", async () => WebAssembly.compile(new Uint8Array([0, 97, 115, 109, 1, 0, 0, 0])));
  r.env = Object.keys(env);
  return r;
}""", {}, wall_s=10, execute=None, log=[])
    assert out == {"connect": "closed", "fetch": "closed", "caches": "closed", "eval": "closed",
                   "function": "closed", "wasm": "closed", "env": []}


async def test_workers_mode_does_not_spawn_quickjs(workers_url, monkeypatch):
    _use_workers(monkeypatch, workers_url)
    spawned: list[tuple] = []
    real = asyncio.create_subprocess_exec

    async def spy(*args, **kwargs):
        spawned.append(args)
        return await real(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    assert await run_script(NOOP, {}, wall_s=10, execute=None, log=[]) == {"ok": 1}
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


async def test_a_wrong_secret_never_runs(workers_url, monkeypatch):
    _use_workers(monkeypatch, workers_url, secret="other-secret")
    calls = 0

    async def execute(req):
        nonlocal calls
        calls += 1
        return {"status": 200, "headers": {}, "json": None, "text": ""}

    with pytest.raises(SandboxError) as e:
        await run_script("export default async function run(ctx) { await ctx.call('x'); return { ok: 1 }; }",
                         {}, wall_s=10, execute=execute, log=[])
    assert e.value.kind == "protocol" and "unauthorized" in e.value.message
    assert calls == 0


async def _first_frame_answer(workers_url: str, frame: str) -> dict:
    async with connect(sandbox_workers.run_url(workers_url)) as ws:
        await ws.send(frame)
        return json.loads(await asyncio.wait_for(ws.recv(), 10))


async def test_an_expired_or_altered_run_is_refused(workers_url):
    body = sandbox_workers.run_body(NOOP, {}, None, wall_s=5)
    expired = sandbox_workers.sign_run(SECRET, body, ttl_s=-5)
    answer = await _first_frame_answer(workers_url, json.dumps({"auth": expired, "run": body}))
    assert answer == {"t": "end", "result": {"op": "error", "kind": "protocol", "message": "unauthorized"}}

    good = sandbox_workers.sign_run(SECRET, body)
    altered = body.replace('"wall_s":5', '"wall_s":300')
    answer = await _first_frame_answer(workers_url, json.dumps({"auth": good, "run": altered}))
    assert answer["result"]["message"] == "unauthorized"

    answer = await _first_frame_answer(workers_url, json.dumps({"auth": good, "run": body}))
    assert answer == {"t": "end", "result": {"op": "done", "output": {"ok": 1}}}


async def test_the_server_environment_does_not_travel_to_the_loader(monkeypatch):
    monkeypatch.setenv("TREG_SECRET_KEY", "must-not-leak-into-the-worker")
    seen: list[str] = []

    async def loader(ws):
        seen.append(await ws.recv())
        await ws.send(json.dumps({"t": "end", "result": {"op": "done", "output": {"ok": 1}}}))

    async with serve(loader, "127.0.0.1", 0) as server:
        _use_workers(monkeypatch, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
        assert await run_script(NOOP, {"name": "m"}, wall_s=5, execute=None, log=[]) == {"ok": 1}
    blob = "".join(seen)
    assert "export default" in blob
    assert "must-not-leak-into-the-worker" not in blob and "TREG_SECRET_KEY" not in blob


async def _with_stub(monkeypatch, script):
    """Loader double: reads the run, then plays `script(ws)`."""
    async def loader(ws):
        await ws.recv()
        await script(ws)

    server = await serve(loader, "127.0.0.1", 0).__aenter__()
    _use_workers(monkeypatch, f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}")
    return server


async def test_parent_rejects_an_output_the_worker_failed_to_cap(monkeypatch):
    fat = "a" * (sandbox.MAX_OUTPUT_BYTES + 1)

    async def script(ws):
        await ws.send(json.dumps({"t": "end", "result": {"op": "done", "output": {"x": fat}}}))

    server = await _with_stub(monkeypatch, script)
    try:
        with pytest.raises(SandboxError) as e:
            await run_script(NOOP, {}, wall_s=5, execute=None, log=[])
    finally:
        server.close()
    assert e.value.kind == "output"


async def test_a_platform_memory_error_is_a_memory_failure(monkeypatch):
    async def script(ws):
        await ws.send(json.dumps({"t": "end", "result": {
            "op": "error", "kind": "script", "message": "Memory limit would be exceeded before EOF"}}))

    server = await _with_stub(monkeypatch, script)
    try:
        with pytest.raises(SandboxError) as e:
            await run_script(NOOP, {}, wall_s=5, execute=None, log=[])
    finally:
        server.close()
    assert e.value.kind == "memory"


async def test_an_unknown_op_is_refused_and_ends_the_run(monkeypatch):
    async def script(ws):
        await ws.send(json.dumps({"t": "op", "id": 1, "msg": {"op": "begin"}}))
        await asyncio.sleep(10)

    server = await _with_stub(monkeypatch, script)
    try:
        with pytest.raises(SandboxError) as e:
            await run_script(NOOP, {}, wall_s=5, execute=None, log=[])
    finally:
        server.close()
    assert e.value.kind == "protocol"


async def test_a_refused_charge_ends_the_run_while_a_call_is_still_out(workers_url, monkeypatch):
    """The script swallows the refusal and waits on a slow call. The run still stops at once, and
    the call it left behind is cancelled rather than finished for a run that already failed."""
    _use_workers(monkeypatch, workers_url)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def slow(req):
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"status": 200, "headers": {}, "json": None, "text": ""}

    loop = asyncio.get_running_loop()
    began = loop.time()
    with pytest.raises(SandboxError) as e:
        await run_script("""
export default async function run(ctx) {
  const out = ctx.call("hunter.people.email.find");
  try { await ctx.charge(5, "too much"); } catch (e) {}
  await out;
  return { ok: 1 };
}""", {}, wall_s=20, execute=slow, log=[], charges=[], max_charge_micro=1_000_000)
    assert e.value.kind == "refused" and "max_price_usd" in e.value.message
    assert started.is_set()
    assert cancelled.is_set()
    assert loop.time() - began < 10


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
        _use_workers(monkeypatch, f"http://127.0.0.1:{port}")
        loop = asyncio.get_running_loop()
        began = loop.time()
        try:
            with pytest.raises(SandboxError) as e:
                await run_script("export default async function run(ctx) { while (true) {} }",
                                 {}, wall_s=2, execute=None, log=[])
            assert e.value.kind == "timeout"
            assert hits >= 1
            assert loop.time() - began < 8
        finally:
            proxy.close()


async def test_an_unreachable_loader_is_a_protocol_failure_an_operator_can_see(monkeypatch, caplog):
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    _use_workers(monkeypatch, f"http://127.0.0.1:{port}")
    with caplog.at_level("WARNING", logger="treg.hub.sandbox"):
        with pytest.raises(SandboxError) as e:
            await run_script(NOOP, {}, wall_s=5, execute=None, log=[])
    assert e.value.kind == "protocol" and "unreachable" in e.value.message
    assert any("hub sandbox unreachable" in r.getMessage() for r in caplog.records)
