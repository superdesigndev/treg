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
