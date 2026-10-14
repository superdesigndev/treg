"""The hub's script sandbox, hostile suite: what a script cannot do, and how the run ends when it
tries. Runs the real child process (`python -m treg.hub_sandbox`) with a fake call executor —
no treg call path here; that is tests/callmatrix/test_hub_run.py."""

from __future__ import annotations

import asyncio

import pytest

from treg.application.hub import sandbox
from treg.application.hub.sandbox import SandboxError, run_script


async def _echo(req: sandbox.CallRequest) -> dict:
    return {"status": 200, "headers": {"content-type": "application/json"},
            "json": {"target": req.target, "opts": req.opts}, "text": ""}


async def _run(script: str, inputs=None, *, execute=_echo, wall_s: int = 10) -> tuple[dict, list[str]]:
    log: list[str] = []
    out = await run_script(script, inputs or {}, wall_s=wall_s, execute=execute, log=log)
    return out, log


async def test_a_script_returns_its_object_and_reads_inputs():
    out, log = await _run("""
export default async function run(ctx) {
  ctx.log("hello " + ctx.inputs.name);
  return { greeting: "hi " + ctx.inputs.name, n: ctx.inputs.n + 1 };
}""", {"name": "mitchel", "n": 41})
    assert out == {"greeting": "hi mitchel", "n": 42}
    assert log == ["hello mitchel"]


async def test_ctx_call_crosses_the_bridge_and_comes_back():
    out, _ = await _run("""
export default async function run(ctx) {
  const a = await ctx.call("supabase/rest/v1/leads", { query: { select: "email" } });
  const b = await ctx.call("hunter.people.email.find", { method: "POST", body: { domain: "x.com" } });
  return { a: a.json.target, aq: a.json.opts.query.select, b: b.json.opts.body.domain, status: a.status };
}""")
    assert out == {"a": "supabase/rest/v1/leads", "aq": "email", "b": "x.com", "status": 200}


@pytest.mark.parametrize("expr", ["fetch", "require", "process", "setTimeout", "XMLHttpRequest", "WebSocket", "os"])
async def test_no_road_out_but_ctx_call(expr):
    out, _ = await _run(f"export default async function run(ctx) {{ return {{ t: typeof {expr} }}; }}")
    assert out == {"t": "undefined"}


async def test_a_refused_call_stops_the_run():
    async def refuse(req):
        raise SandboxError("refused", f"{req.target!r} is not in the manifest's `uses`")
    with pytest.raises(SandboxError) as e:
        await _run("""
export default async function run(ctx) {
  try { await ctx.call("evil.tool"); } catch (err) { /* the script cannot swallow a refusal */ }
  return { reached: true };
}""", execute=refuse)
    assert e.value.kind == "refused" and "evil.tool" in e.value.message


async def test_a_memory_bomb_dies_inside_the_cap():
    with pytest.raises(SandboxError) as e:
        await _run("""
export default async function run(ctx) {
  const a = []; while (true) { a.push("x".repeat(1000000)); }
}""")
    assert e.value.kind == "memory"


async def test_an_endless_loop_dies_at_the_wall():
    with pytest.raises(SandboxError) as e:
        await _run("export default async function run(ctx) { while (true) {} }", wall_s=2)
    assert e.value.kind == "timeout"


async def test_a_thrown_error_is_reported_as_a_script_failure():
    with pytest.raises(SandboxError) as e:
        await _run("export default async function run(ctx) { throw new Error('boom'); }")
    assert e.value.kind == "script" and "boom" in e.value.message


async def test_a_script_without_the_export_is_refused():
    with pytest.raises(SandboxError) as e:
        await _run("function run(ctx) { return {}; }")
    assert e.value.kind == "script" and "export default" in e.value.message


async def test_a_non_object_return_is_refused():
    with pytest.raises(SandboxError) as e:
        await _run("export default async function run(ctx) { return [1, 2]; }")
    assert e.value.kind == "output"


async def test_the_log_is_capped_at_fifty_lines():
    _, log = await _run("""
export default async function run(ctx) {
  for (let i = 0; i < 80; i++) ctx.log("line " + i + " " + "y".repeat(5000));
  return { ok: true };
}""")
    assert len(log) == 50 and all(len(line) <= 2000 for line in log)


async def test_the_call_cap_is_enforced_by_the_parent():
    calls = 0

    async def count(req):
        nonlocal calls
        calls += 1
        return await _echo(req)
    with pytest.raises(SandboxError) as e:
        await _run("""
export default async function run(ctx) {
  for (let i = 0; i < 30; i++) await ctx.call("hunter.people.email.find");
  return { ok: true };
}""", execute=count)
    assert e.value.kind == "refused" and "cap of 20" in e.value.message
    assert calls == 20


async def test_the_child_sees_no_server_environment(monkeypatch):
    monkeypatch.setenv("TREG_SECRET_KEY", "must-not-leak")
    # the engine has no env API at all; prove the process env is scrubbed by the parent's builder
    env = sandbox._child_env("/tmp/x")
    assert "TREG_SECRET_KEY" not in env and env["HOME"] == "/tmp/x"


async def test_two_runs_in_parallel_do_not_mix():
    a, b = await asyncio.gather(
        _run("export default async function run(ctx) { return { who: ctx.inputs.who }; }", {"who": "a"}),
        _run("export default async function run(ctx) { return { who: ctx.inputs.who }; }", {"who": "b"}),
    )
    assert a[0] == {"who": "a"} and b[0] == {"who": "b"}


async def test_ctx_csv_parses_quotes_commas_and_newlines():
    out, _ = await _run(r'''
export default async function run(ctx) {
  const rows = ctx.csv('name,note,n\r\n"Doe, Jane","said ""hi""\nthen left",3\nBob,,4\n');
  return { rows, n: rows.length };
}''')
    assert out["n"] == 2
    assert out["rows"][0] == {"name": "Doe, Jane", "note": 'said "hi"\nthen left', "n": "3"}
    assert out["rows"][1] == {"name": "Bob", "note": "", "n": "4"}


async def test_ctx_data_carries_the_uploaded_rows():
    log: list[str] = []
    out = await run_script("export default async function run(ctx) { return { n: ctx.data.length, first: ctx.data[0] }; }",
                           {}, wall_s=10, execute=_echo, log=log, data=[{"a": "1", "b": "x"}, {"a": "2", "b": "y"}])
    assert out == {"n": 2, "first": {"a": "1", "b": "x"}}
    out2, _ = await _run("export default async function run(ctx) { return { d: ctx.data }; }")
    assert out2 == {"d": None}


async def test_five_calls_in_one_promise_all_run_at_the_same_time():
    """The AI visibility tool: five engines at ~30-47 s each cannot fit 120 s one after another.
    ctx.call no longer blocks the engine, so Promise.all overlaps them (the parent runs four at a
    time). Each call sleeps 0.3 s: five sequential would take 1.5 s; overlapped, well under 1 s."""
    import time
    started: list[float] = []

    async def execute(req):
        started.append(time.monotonic())
        await asyncio.sleep(0.3)
        return {"status": 200, "headers": {}, "json": {"n": len(started)}, "text": ""}

    t0 = time.monotonic()
    out = await run_script(
        "export default async function run(ctx) {"
        "  const rs = await Promise.all([1,2,3,4,5].map(i => ctx.call('hunter.people.email.find', {query: {i}})));"
        "  return { ok: rs.length, all200: rs.every(r => r.status === 200) };"
        "}",
        {}, wall_s=10, execute=execute, log=[])
    elapsed = time.monotonic() - t0
    assert out == {"ok": 5, "all200": True}
    assert len(started) == 5
    assert elapsed < 1.2, f"five 0.3 s calls took {elapsed:.2f} s: they ran one after another"
    # four at a time: the first four start together, the fifth waits for a slot
    assert started[3] - started[0] < 0.2 and started[4] - started[0] >= 0.25


async def test_a_refused_call_inside_promise_all_still_stops_the_run():
    async def execute(req):
        if req.opts.get("query", {}).get("i") == 2:
            raise SandboxError("refused", "no")
        await asyncio.sleep(0.05)
        return {"status": 200, "headers": {}, "json": None, "text": ""}

    with pytest.raises(SandboxError) as e:
        await run_script(
            "export default async function run(ctx) {"
            "  await Promise.all([1,2,3].map(i => ctx.call('hunter.people.email.find', {query: {i}})));"
            "  return { ok: true };"
            "}",
            {}, wall_s=10, execute=execute, log=[])
    assert e.value.kind == "refused"


async def test_a_call_with_its_own_timeout_answers_timed_out_instead_of_ending_the_run():
    """`timeout_s` on ctx.call: the slow engine is "did not answer", the fast one still counts."""
    async def execute(req):
        if req.opts.get("query", {}).get("slow"):
            await asyncio.sleep(5)
        return {"status": 200, "headers": {}, "json": {"ok": 1}, "text": ""}

    out = await run_script(
        "export default async function run(ctx) {"
        "  const [a, b] = await Promise.all(["
        "    ctx.call('hunter.people.email.find', {query: {slow: 1}, timeout_s: 0.3}),"
        "    ctx.call('hunter.people.email.find', {query: {slow: 0}, timeout_s: 0.3})]);"
        "  return { a_timed_out: a.timed_out, a_status: a.status, b_status: b.status, b_timed_out: b.timed_out };"
        "}",
        {}, wall_s=10, execute=execute, log=[])
    assert out == {"a_timed_out": True, "a_status": 0, "b_status": 200, "b_timed_out": False}


# ---------------------------------------------------------------------------------------------
# ctx.charge: the script prices itself, one line at a time, under pricing.max_price_usd (round 4, 2026-09-24)

async def test_ctx_charge_lines_reach_the_parent_and_zero_is_dropped():
    charges: list = []
    out = await run_script(
        "export default async function run(ctx) {"
        "  ctx.charge(0.03, 'vendor-b hit'); ctx.charge(0, 'free miss'); ctx.charge(0.001);"
        "  return { ok: 1 };"
        "}",
        {}, wall_s=10, execute=None, log=[], charges=charges, max_charge_micro=50_000)
    assert out == {"ok": 1}
    assert charges == [{"micro": 30_000, "label": "vendor-b hit"}, {"micro": 1_000, "label": ""}]


@pytest.mark.parametrize("expr, rule", [
    ("ctx.charge(0.6, 'too much')", "past pricing.max_price_usd"),
    ("ctx.charge(0.3, 'a'); ctx.charge(0.3, 'b')", "past pricing.max_price_usd"),
    ("ctx.charge('abc', 'x')", "a number of dollars"),
    ("ctx.charge(-1, 'x')", "a number of dollars"),
    ("for (let i = 0; i < 21; i++) ctx.charge(0.01, 'x')", "cap of 20 charges"),
])
async def test_a_bad_charge_is_refused_and_stops_the_run(expr, rule):
    with pytest.raises(SandboxError) as e:
        await run_script("export default async function run(ctx) { " + expr + "; return { ok: 1 }; }",
                         {}, wall_s=10, execute=None, log=[], charges=[], max_charge_micro=500_000)
    assert e.value.kind == "refused" and rule in e.value.message


async def test_ctx_charge_without_a_declared_cap_is_refused():
    with pytest.raises(SandboxError) as e:
        await run_script("export default async function run(ctx) { ctx.charge(0.01, 'x'); return { ok: 1 }; }",
                         {}, wall_s=10, execute=None, log=[])
    assert e.value.kind == "refused" and "max_price_usd" in e.value.message
