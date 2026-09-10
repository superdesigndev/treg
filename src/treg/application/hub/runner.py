"""The hub runner, JSON road: the graph, the waves, the steps (docs/HUB-DECISIONS.md).

A run is `POST /call/<team>.<name>` with the inputs as a JSON body. The runner resolves the
manifest's references into a graph, starts every ready step (four at a time), and runs each
step as an ordinary treg call through `execute_call` — the same gates, the same key injection,
the same reserve-and-settle, under a child hold `{run}:s{n}`. This module owns no money and no
key: it only decides what to call next and reads the answers back into scope.

Two teams meet in one run: the CALLER (identity and money) and the MAKER (the tools the recipe
names). A catalog step runs as the caller: their balance, their rules, their key if they have
one for the provider, else treg's. A step on one of the maker's own tools runs as the maker:
their registered key, unmetered, their tool. That is how a shared recipe can use a key the
caller never holds (HUB-DECISIONS round 2 q9).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.parse import urlencode

import httpx
from sqlalchemy import select

from ... import audit
from ...domain.catalog import store as catalog_store
from ...domain.hub import graph as hub_graph
from ...domain.hub import refs
from ...infra.db import session_maker
from ...models import HubRun, HubTool, Org
from ..call.types import CallContext, CallFailure, CallInput, ResolutionFailed, UpstreamResponse

RUN_MAX_COST_HEADER = "X-Treg-Run-Max-Cost"
DEFAULT_RUN_MAX_COST_MICRO = 1_000_000      # $1.00 for the whole run, seller price included later
MAX_PARALLEL = 4                            # steps in flight at once inside one run
_DROP_FROM_CHILD = frozenset({b"content-length", b"content-type", b"transfer-encoding",
                              b"idempotency-key", b"x-treg-run-max-cost", b"host",
                              # The RUNNER reads every step's bytes itself (a script gets `json`
                              # and `text`), so a step must never be answered compressed: the
                              # caller's accept-encoding is dropped and identity is asked below.
                              # Found live 2026-09-09: a 20-row Supabase answer came back gzip
                              # and the script saw an empty list; a 2-row one was fine.
                              b"accept-encoding"})
# Refusals that are the same for every step: no point starting anything else.
_GLOBAL_REFUSALS = frozenset({"insufficient_balance", "tag_spend_cap_reached",
                              "platform_daily_cap_reached", "daily_cap_reached"})


class _Bytes:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def stream(self):
        yield self._data

    async def read(self) -> bytes:
        return self._data


@dataclass
class _Step:
    """One unit against the caps: a plain step, or one item of a repeated step."""
    name: str
    spec: dict[str, Any]
    ref: str                    # the child call_ref
    wave: int
    item: int | None = None
    item_value: Any = None


class RunStopped(Exception):
    """The run must not start anything new: a step failed, a cap was hit, or a global refusal."""

    def __init__(self, kind: str, status: int, detail: dict) -> None:
        super().__init__(kind)
        self.kind, self.status, self.detail = kind, status, detail


# ---------------------------------------------------------------------------------------------
# Inputs

class InputError(ValueError):
    def __init__(self, field: str, rule: str) -> None:
        super().__init__(f"{field}: {rule}")
        self.field, self.rule = field, rule


def coerce_inputs(specs: dict[str, dict[str, Any]], given: dict[str, Any]) -> dict[str, Any]:
    """The manifest's contract applied once, in the runner (Crawl4AI lesson 6): defaults filled,
    types checked, ints clamped to min..max, unknown inputs and missing required ones refused."""
    for key in given:
        if key not in specs:
            raise InputError(key, "not an input of this tool")
    out: dict[str, Any] = {}
    for key, spec in specs.items():
        if key not in given or given[key] is None:
            if "default" in spec:
                out[key] = spec["default"]
                continue
            raise InputError(key, "required (it has no default)")
        v = given[key]
        typ = spec["type"]
        try:
            if typ == "string":
                v = v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)
            elif typ == "int":
                if isinstance(v, bool):
                    raise ValueError
                v = int(v)
                v = max(spec.get("min", -(2**31)), min(int(spec["max"]), v))
            elif typ == "float":
                if isinstance(v, bool):
                    raise ValueError
                v = float(v)
            elif typ == "bool":
                v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
            elif typ == "list":
                v = v if isinstance(v, list) else json.loads(v) if isinstance(v, str) else [v]
                if not isinstance(v, list):
                    raise ValueError
            elif typ == "object":
                v = v if isinstance(v, dict) else json.loads(v) if isinstance(v, str) else None
                if not isinstance(v, dict):
                    raise ValueError
        except (ValueError, TypeError):
            raise InputError(key, f"must be a {typ}") from None
        out[key] = v
    return out


def masked(specs: dict[str, dict[str, Any]], inputs: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if specs.get(k, {}).get("secret") else v) for k, v in inputs.items()}


# ---------------------------------------------------------------------------------------------
# The run

async def run_hub_tool(
    parent: CallContext, tool: HubTool, body_bytes: bytes, get_header: Callable[[str], str | None],
    upstream_client: httpx.AsyncClient, execute_child, *, audit_client: str = "",
) -> tuple[UpstreamResponse, int]:
    """Execute one run under `parent`. Returns (the JSON reply, total charged to the caller)."""
    manifest = tool.manifest
    run_id = parent.call_ref
    started = time.monotonic()
    try:
        given = json.loads(body_bytes or b"{}") if body_bytes.strip() else {}
    except ValueError:
        raise ResolutionFailed("hub_input_invalid", status_code=422, detail={
            "error": "hub_input_invalid", "field": "body", "rule": "a JSON object of inputs"})
    if not isinstance(given, dict):
        raise ResolutionFailed("hub_input_invalid", status_code=422, detail={
            "error": "hub_input_invalid", "field": "body", "rule": "a JSON object of inputs"})
    if not given and parent.input.query_items:
        given = dict(parent.input.query_items)
    try:
        inputs = coerce_inputs(manifest["inputs"], given)
    except InputError as exc:
        raise ResolutionFailed("hub_input_invalid", status_code=422, detail={
            "error": "hub_input_invalid", "field": exc.field, "rule": exc.rule})
    ceiling = _ceiling(get_header(RUN_MAX_COST_HEADER))
    maker = await _maker_snapshot(parent, tool)
    catalog = catalog_store.load()
    own_tools = {u for u in manifest["uses"] if "." not in u}
    price_held = await _reserve_price(parent, tool, run_id)
    if price_held > ceiling:
        await _close_price(tool, run_id, price_held, success=False, reason="hub_run_max_cost")
        raise ResolutionFailed("hub_run_failed", status_code=402, detail={
            "error": "hub_run_max_cost", "max_cost_micro": ceiling, "price_micro": price_held,
            "run_id": run_id, "charged_micro": 0, "trace": [],
            "message": f"the tool's own price ({price_held} µ$) already passes {RUN_MAX_COST_HEADER}"})
    if tool.kind == "script":
        return await _run_script_road(parent, tool, inputs, ceiling, maker, catalog, own_tools,
                                      upstream_client, execute_child, started, audit_client, price_held)
    g = hub_graph.build(manifest["steps"])

    scope: dict[str, Any] = {"input": inputs}
    trace: list[dict[str, Any]] = []
    spent = 0
    counted = 0
    stop: RunStopped | None = None
    done: set[str] = set()
    sem = asyncio.Semaphore(MAX_PARALLEL)
    running: dict[asyncio.Task, str] = {}

    async def one_call(step: _Step) -> tuple[_Step, dict[str, Any], Any]:
        """One child call. Returns (step, trace entry, answer)."""
        async with sem:
            spec = step.spec
            item_scope = dict(scope)
            if step.item is not None:
                item_scope[spec["as"]] = step.item_value
            if spec.get("skip_if_empty") and refs.is_empty(
                    refs.resolve(spec["skip_if_empty"], item_scope, g.positions)):
                entry = _entry(step, "skipped", None, 0, 0, key=None)
                return step, entry, None
            call = spec["call"]
            target = call.split("/", 1)[0]
            ep = None if target in own_tools else catalog.by_id.get(call)
            inp = refs.resolve(spec.get("input", {}), item_scope, g.positions)
            method = (spec.get("method") or (ep["method"] if ep else "GET")).upper()
            as_who = maker if target in own_tools else parent.input.caller
            child = CallContext(input=_child_input(parent, call, method, inp, as_who),
                                call_ref=step.ref, meta=parent.meta)
            t0 = time.monotonic()
            try:
                response = await execute_child(child, upstream_client)
            except CallFailure as exc:
                ms = int((time.monotonic() - t0) * 1000)
                entry = _entry(step, "failed", exc.status_code, ms, 0, key=None,
                               error=_short(exc.detail))
                if exc.kind in _GLOBAL_REFUSALS:
                    entry["global"] = exc.kind
                return step, entry, exc
            raw = await _read(response)
            ms = int((time.monotonic() - t0) * 1000)
            charged = int(_header(response, "X-Treg-Cost-Micro") or 0)
            key = "treg" if _header(response, "X-Treg-Cost-Micro") is not None else "team"
            ok = 200 <= response.status < 300
            try:
                answer = json.loads(raw) if raw else None
            except ValueError:
                answer = raw.decode("utf-8", "replace")
            entry = _entry(step, "ok" if ok else "failed", response.status, ms, charged, key=key,
                           error=None if ok else _short(answer))
            return step, entry, answer if ok else _StepFailed(response.status, answer)

    def estimate(spec: dict[str, Any]) -> int:
        target = spec["call"].split("/", 1)[0]
        if target in own_tools:
            return 0
        ep = catalog.by_id.get(spec["call"])
        cv = catalog.cost_view(ep.get("cost"), ep.get("provider")) if ep else None
        usd = (cv or {}).get("usd")
        return int(round(float(usd) * 1_000_000)) if usd else 0

    def units_for(name: str) -> list[_Step]:
        spec = g.steps[name]
        wave = g.wave[name]
        idx = g.order.index(name)
        if spec.get("for_each"):
            items = refs.resolve(spec["for_each"], scope, g.positions)
            items = items if isinstance(items, list) else ([] if items is None else [items])
            return [_Step(name, spec, f"{run_id}:s{idx}.{i}", wave, i, v) for i, v in enumerate(items)]
        return [_Step(name, spec, f"{run_id}:s{idx}", wave)]

    pending_units: dict[str, list[_Step]] = {}
    results: dict[str, list[Any]] = {}
    try:
        while True:
            if stop is None:
                for name in g.order:
                    if name in done or name in pending_units:
                        continue
                    if not g.parents[name] <= done:
                        continue
                    units = units_for(name)
                    pending_units[name] = []
                    results[name] = [None] * len(units)
                    if not units:                 # a repeat over nothing: the step is done, empty
                        done.add(name)
                        scope[name] = []
                        continue
                    for u in units:
                        if counted + 1 > manifest["limits"]["steps"]:
                            stop = RunStopped("hub_step_cap", 424, {
                                "error": "hub_step_cap", "step": name,
                                "message": f"the run would pass its step cap ({manifest['limits']['steps']})"})
                            break
                        est = estimate(u.spec)
                        if price_held + spent + _reserved(running, pending_units, estimate) + est > ceiling:
                            stop = RunStopped("hub_run_max_cost", 402, {
                                "error": "hub_run_max_cost", "step": name, "max_cost_micro": ceiling,
                                "message": f"the next step would pass {RUN_MAX_COST_HEADER}"})
                            break
                        counted += 1
                        task = asyncio.create_task(one_call(u))
                        running[task] = name
                        pending_units[name].append(u)
                    if stop is not None:
                        break
            if not running:
                break
            finished, _ = await asyncio.wait(set(running), return_when=asyncio.FIRST_COMPLETED)
            for task in finished:
                name = running.pop(task)
                step, entry, answer = task.result()
                trace.append(entry)
                spent += entry["cost_micro"]
                failed = isinstance(answer, (_StepFailed, CallFailure))
                if failed and not step.spec.get("allow_fail"):
                    if stop is None:
                        stop = RunStopped("hub_step_failed", 424, {
                            "error": "hub_step_failed", "step": step.name, "status": entry.get("status"),
                            **({"message": "a refusal that applies to every step"} if entry.get("global") else {})})
                    if isinstance(answer, CallFailure) and entry.get("global"):
                        stop = RunStopped(answer.kind, answer.status_code, {
                            "error": answer.kind, "step": step.name, "detail": answer.detail})
                    answer = None
                elif failed:
                    entry["outcome"] = "failed_allowed"
                    answer = None
                slot = step.item if step.item is not None else 0
                results[name][slot] = answer
                if len([t for t, n in running.items() if n == name]) == 0:
                    done.add(name)
                    scope[name] = results[name] if step.spec.get("for_each") else results[name][0]
    except asyncio.CancelledError:
        raise

    trace.sort(key=lambda e: (e["wave"], e["name"], e.get("item") or 0))
    ms_total = int((time.monotonic() - started) * 1000)
    if stop is not None:
        await _close_price(tool, run_id, price_held, success=False, reason=stop.kind)
        detail = {**stop.detail, "run_id": run_id, "recipe": f"{tool.tool_id}@{tool.version}",
                  "charged_micro": spent, "price_micro": 0, "trace": trace}
        await _record(tool, parent, run_id, "failed" if stop.kind == "hub_step_failed" else "stopped",
                      counted, spent, ms_total, masked(manifest["inputs"], inputs), trace, error=detail)
        _audit_parent(parent, tool, stop.status, spent, audit_client)
        raise ResolutionFailed("hub_run_failed", status_code=stop.status, detail=detail)

    output = refs.resolve(manifest["output"], scope, g.positions)
    earned = await _close_price(tool, run_id, price_held, success=True)
    body_out = {
        "run_id": run_id, "recipe": f"{tool.tool_id}@{tool.version}", "output": output,
        "usage": {"cost_micro": spent + earned, "steps_micro": spent, "price_micro": earned,
                  "steps": counted, "ms": ms_total},
        "trace": trace, "log": [],
    }
    await _record(tool, parent, run_id, "ok", counted, spent, ms_total,
                  masked(manifest["inputs"], inputs), trace, price=earned, output=output)
    _audit_parent(parent, tool, 200, spent + earned, audit_client)
    return _json(body_out, 200, {"X-Treg-Run-Id": run_id, "X-Treg-Steps": str(counted)}), spent + earned


# ---------------------------------------------------------------------------------------------
# The seller's price (docs/HUB-DECISIONS.md round 3): one extra hold `{run}:price` on the CALLER
# at run start, settled to the MAKER as `earned` credit on success, released on failure. Not
# charged when the caller IS the maker (their own tool, and the publish check run), so the check
# never costs the seller their own price.

async def _reserve_price(parent: CallContext, tool: HubTool, run_id: str) -> int:
    """Open the price hold. Returns the amount held (0 when nothing is owed). Raises
    ResolutionFailed 402 `hub_price_unaffordable` when the caller cannot afford it."""
    from ...domain import money as ledger
    caller = parent.input.caller
    if tool.price_micro <= 0 or caller.org_id == tool.org_id:
        return 0
    async with session_maker() as s:
        try:
            await ledger.reserve_in_transaction(
                s, caller.org_id, tool.tool_id, tool.price_micro, call_id=f"{run_id}:price",
                meta={"tier": "hub_price", "tool_id": tool.tool_id, "version": tool.version,
                      "maker_org_id": tool.org_id}, tags=dict(getattr(parent.meta, "tags", {}) or {}))
        except ledger.InsufficientBalance as exc:
            await s.rollback()
            raise ResolutionFailed("hub_price_unaffordable", status_code=402, detail={
                "error": "insufficient_balance", "tool_id": tool.tool_id,
                "balance_micro": exc.balance_micro, "price_micro": tool.price_micro,
                "message": f"this tool costs ${tool.price_micro / 1e6:.6g} per run from its maker, "
                           f"before its steps; your balance is ${exc.balance_micro / 1e6:.4f}"}) from None
        await s.commit()
    return tool.price_micro


async def _close_price(tool: HubTool, run_id: str, held: int, *, success: bool, reason: str = "") -> int:
    """Settle the price to the maker (success) or give it back to the caller (failure).
    Returns what the maker earned."""
    if held <= 0:
        return 0
    from ...domain import money as ledger
    async with session_maker() as s:
        if success:
            earned = await ledger.settle_to_in_transaction(
                s, f"{run_id}:price", tool.org_id,
                meta={"tool_id": tool.tool_id, "version": tool.version, "run_id": run_id})
        else:
            earned = 0
            await ledger.release_in_transaction(s, f"{run_id}:price", reason=reason or "hub_run_failed",
                                                meta={"tool_id": tool.tool_id, "run_id": run_id})
        await s.commit()
    return earned


class _StepFailed:
    def __init__(self, status: int, body: Any) -> None:
        self.status, self.body = status, body


def _reserved(running: dict, pending: dict, estimate) -> int:
    """Estimates of the units still in flight: the ceiling check must count what is already out."""
    names = list(running.values())
    return sum(estimate(pending[n][-1].spec) for n in names if pending.get(n)) if names else 0


def _ceiling(raw: str | None) -> int:
    if not raw:
        return DEFAULT_RUN_MAX_COST_MICRO
    try:
        return max(0, int(round(float(raw) * 1_000_000)))
    except ValueError:
        return DEFAULT_RUN_MAX_COST_MICRO


def _entry(step: _Step, outcome: str, status: int | None, ms: int, cost: int, *, key: str | None,
           error: Any = None) -> dict[str, Any]:
    e: dict[str, Any] = {"wave": step.wave, "name": step.name, "call": step.spec["call"],
                         "outcome": outcome, "status": status, "ms": ms, "cost_micro": cost, "key": key}
    if step.item is not None:
        e["item"] = step.item
    if error is not None:
        e["error"] = error
    return e


def _short(v: Any) -> Any:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s[:300]


_SCRIPT_HEADER_DENY = frozenset({"authorization", "cookie", "host", "content-length", "transfer-encoding",
                                 "connection", "idempotency-key", "apikey"})


def _child_input(parent: CallContext, call: str, method: str, inp: dict[str, Any], as_who,
                 extra_query: dict[str, Any] | None = None,
                 extra_headers: dict[str, str] | None = None) -> CallInput:
    has_body = method in ("POST", "PUT", "PATCH")
    payload = json.dumps(inp, ensure_ascii=False).encode() if has_body else b""
    drop = set(_DROP_FROM_CHILD) | {k.lower().encode("latin-1") for k in (extra_headers or {})}
    headers = [(k, v) for k, v in parent.input.raw_headers if k.lower() not in drop]
    headers += [(k.lower().encode("latin-1"), v.encode("latin-1", "replace")) for k, v in (extra_headers or {}).items()
                if k.lower() != "accept-encoding"]
    headers.append((b"accept-encoding", b"identity"))
    if has_body:
        headers += [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode())]
    q = (extra_query or {}) if has_body else inp
    items = tuple((k, v if isinstance(v, str) else json.dumps(v)) for k, v in q.items() if v is not None)
    return CallInput(method=method, raw_rest=call, raw_headers=tuple(headers), query_items=items,
                     raw_query=urlencode(items), body=_Bytes(payload), caller=as_who,
                     client_ip=parent.input.client_ip, catalog_only=False)


async def _maker_snapshot(parent: CallContext, tool: HubTool):
    """The caller's snapshot re-pointed at the maker's team for the maker's own tools: same
    person, same client, the MAKER's tools and keys (unmetered, so no money follows this)."""
    caller = parent.input.caller
    if tool.org_id == caller.org_id:
        return caller
    async with session_maker() as s:
        org = await s.get(Org, tool.org_id)
    if org is None:
        return caller
    return replace(caller,
                   membership=replace(caller.membership, org_id=tool.org_id, tool_access=None,
                                      project_access=None),
                   org=replace(caller.org, id=org.id, slug=org.slug, demo=org.demo,
                               public_demo=org.public_demo))


async def _read(response: UpstreamResponse) -> bytes:
    chunks = []
    async for chunk in response.body_stream:
        chunks.append(chunk)
    await response.close()
    return b"".join(chunks)


def _header(response: UpstreamResponse, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for k, v in response.raw_headers:
        if k.lower() == wanted:
            return v.decode("latin-1")
    return None


async def _record(tool: HubTool, parent: CallContext, run_id: str, status: str, steps: int,
                  cost: int, ms: int, inputs: dict, trace: list, error: dict | None = None,
                  log: list | None = None, price: int = 0, output: dict | None = None) -> None:
    from datetime import datetime, timezone
    caller = parent.input.caller
    async with session_maker() as s:
        s.add(HubRun(run_id=run_id, tool_id=tool.tool_id, version=tool.version,
                     caller_org_id=caller.org_id, maker_org_id=tool.org_id, caller_email=caller.email,
                     status=status, steps=steps, cost_micro=cost, price_micro=price, duration_ms=ms, inputs=inputs,
                     trace=trace, log=log or [], error=error, output=output,
                     finished_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        await s.commit()


def _audit_parent(parent: CallContext, tool: HubTool, status: int, charged: int, client: str) -> None:
    c = parent.input.caller
    audit.record_call(org_id=c.org_id, user_email=c.email, tool_name=tool.tool_id, method="POST",
                      path=f"/call/{tool.tool_id}", status_code=status, client=client,
                      telemetry={"call_ref": parent.call_ref, "endpoint_id": tool.tool_id,
                                 "provider": "hub", "credential_tier": "hub",
                                 "cost_charged_micro": charged})


def _json(value, status: int, headers: dict[str, str]) -> UpstreamResponse:
    body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()

    async def _one():
        yield body

    async def _closed():
        return None
    raw = [(b"content-length", str(len(body)).encode()), (b"content-type", b"application/json")]
    raw += [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
    return UpstreamResponse(status, tuple(raw), _one(), _closed)


# ---------------------------------------------------------------------------------------------
# The script road: the same child call, asked for by run.js through the sandbox bridge

async def _run_script_road(parent, tool, inputs, ceiling, maker, catalog, own_tools,
                           upstream_client, execute_child, started, audit_client, price_held=0):
    from . import sandbox

    manifest = tool.manifest
    run_id = parent.call_ref
    uses = set(manifest["uses"])
    trace: list[dict[str, Any]] = []
    log: list[str] = []
    spent = 0
    counted = 0
    fields = manifest["output"]["fields"]

    def estimate_for(call: str) -> int:
        target = call.split("/", 1)[0]
        if target in own_tools:
            return 0
        ep = catalog.by_id.get(call)
        cv = catalog.cost_view(ep.get("cost"), ep.get("provider")) if ep else None
        usd = (cv or {}).get("usd")
        return int(round(float(usd) * 1_000_000)) if usd else 0

    # The maker's own tools named in `uses`, with their base URLs: a full URL in ctx.call resolves
    # to `<tool>/<path>` when it starts with one of them (HUB-DECISIONS round 2 q2, the third
    # target shape), and is refused for any other host - the sandbox never reaches a host the
    # manifest did not name.
    bases: dict[str, str] = {}
    if own_tools:
        from ...models import Tool
        async with session_maker() as s:
            rows = (await s.execute(select(Tool.name, Tool.base_url).where(
                Tool.org_id == tool.org_id, Tool.name.in_(own_tools)))).all()
        bases = {name: (base or "").rstrip("/") for name, base in rows}

    def from_url(url: str) -> tuple[str, dict[str, str]]:
        from urllib.parse import parse_qsl, urlsplit
        for name, base in sorted(bases.items(), key=lambda kv: -len(kv[1])):
            if base and (url == base or url.startswith(base + "/") or url.startswith(base + "?")):
                rest = url[len(base):]
                u = urlsplit(rest if rest.startswith("/") else "/" + rest)
                return f"{name}{u.path}", dict(parse_qsl(u.query, keep_blank_values=True))
        raise sandbox.SandboxError("refused", f"{url!r} is not under a tool in `uses`; register the host as a tool and name it")

    async def execute(req: sandbox.CallRequest) -> dict[str, Any]:
        nonlocal spent, counted
        call = req.target
        url_query: dict[str, str] = {}
        if call.startswith("http://") or call.startswith("https://"):
            call, url_query = from_url(call)
        target = call.split("/", 1)[0]
        if not (call in uses or (target in own_tools and "/" in call) or target in uses and target in own_tools):
            raise sandbox.SandboxError("refused", f"{call!r} is not in the manifest's `uses`")
        if target not in own_tools and call not in catalog.by_id:
            raise sandbox.SandboxError("refused", f"{call!r} is not a catalog id")
        if counted + 1 > manifest["limits"]["steps"]:
            raise sandbox.SandboxError("refused", f"the run would pass its step cap ({manifest['limits']['steps']})")
        est = estimate_for(call)
        if price_held + spent + est > ceiling:
            raise sandbox.SandboxError("refused", f"the next call would pass {RUN_MAX_COST_HEADER}")
        counted += 1
        n = counted - 1
        opts = req.opts
        ep = None if target in own_tools else catalog.by_id.get(call)
        method = str(opts.get("method") or (ep["method"] if ep else "GET")).upper()
        query = {**url_query, **(opts.get("query") if isinstance(opts.get("query"), dict) else {})}
        body = opts.get("body")
        inp = body if (isinstance(body, dict) and method in ("POST", "PUT", "PATCH")) else query
        if method in ("POST", "PUT", "PATCH") and not isinstance(inp, dict):
            inp = {}
        as_who = maker if target in own_tools else parent.input.caller
        step = _Step(name=f"call{n + 1}", spec={"call": call}, ref=f"{run_id}:s{n}", wave=n)
        # Headers a script sets on ctx.call ride to the upstream (a PostgREST `Accept-Profile`, a
        # vendor's `Accept`), minus the ones that carry identity or framing: those are treg's.
        raw_headers = opts.get("headers") if isinstance(opts.get("headers"), dict) else {}
        extra_headers = {str(k): str(v) for k, v in raw_headers.items()
                         if str(k).lower() not in _SCRIPT_HEADER_DENY and not str(k).lower().startswith("x-treg-")}
        child = CallContext(input=_child_input(parent, call, method, inp, as_who,
                                               extra_query=query if inp is not query else None,
                                               extra_headers=extra_headers),
                            call_ref=step.ref, meta=parent.meta)
        t0 = time.monotonic()
        try:
            response = await execute_child(child, upstream_client)
        except CallFailure as exc:
            ms = int((time.monotonic() - t0) * 1000)
            trace.append(_entry(step, "failed", exc.status_code, ms, 0, key=None, error=_short(exc.detail)))
            if exc.kind in _GLOBAL_REFUSALS:
                raise sandbox.SandboxError("refused", f"{exc.kind}: a refusal that applies to every call")
            return {"status": exc.status_code, "headers": {}, "json": exc.detail if isinstance(exc.detail, dict) else None,
                    "text": str(exc.detail)[:4000]}
        raw = await _read(response)
        ms = int((time.monotonic() - t0) * 1000)
        charged = int(_header(response, "X-Treg-Cost-Micro") or 0)
        spent += charged
        key = "treg" if _header(response, "X-Treg-Cost-Micro") is not None else "team"
        ok = 200 <= response.status < 300
        try:
            doc = json.loads(raw) if raw else None
        except ValueError:
            doc = None
        trace.append(_entry(step, "ok" if ok else "failed", response.status, ms, charged, key=key,
                            error=None if ok else _short(doc if doc is not None else raw[:300].decode("utf-8", "replace"))))
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in response.raw_headers
                   if not k.lower().startswith(b"x-treg-")}
        return {"status": response.status, "headers": headers, "json": doc,
                "text": raw[:MAX_TEXT].decode("utf-8", "replace")}

    data_rows = _csv_rows(tool.data) if getattr(tool, "data", None) else None
    try:
        output = await sandbox.run_script(tool.script or "", inputs, wall_s=manifest["limits"]["wall_s"],
                                          execute=execute, log=log, data=data_rows)
    except sandbox.SandboxError as exc:
        ms_total = int((time.monotonic() - started) * 1000)
        await _close_price(tool, run_id, price_held, success=False, reason="hub_script_failed")
        detail = {"error": "hub_script_failed", "kind": exc.kind, "message": exc.message,
                  "run_id": run_id, "recipe": f"{tool.tool_id}@{tool.version}",
                  "charged_micro": spent, "price_micro": 0, "trace": trace, "log": log}
        await _record(tool, parent, run_id, "failed", counted, spent, ms_total,
                      masked(manifest["inputs"], inputs), trace, error=detail, log=log)
        _audit_parent(parent, tool, 424, spent, audit_client)
        raise ResolutionFailed("hub_run_failed", status_code=424, detail=detail)
    missing = [f for f in fields if f not in output]
    ms_total = int((time.monotonic() - started) * 1000)
    if missing:
        await _close_price(tool, run_id, price_held, success=False, reason="hub_output_invalid")
        detail = {"error": "hub_output_invalid", "missing": missing, "run_id": run_id,
                  "recipe": f"{tool.tool_id}@{tool.version}", "charged_micro": spent,
                  "price_micro": 0, "trace": trace, "log": log,
                  "message": "run(ctx) returned an object without the fields the manifest declares"}
        await _record(tool, parent, run_id, "failed", counted, spent, ms_total,
                      masked(manifest["inputs"], inputs), trace, error=detail, log=log)
        _audit_parent(parent, tool, 424, spent, audit_client)
        raise ResolutionFailed("hub_run_failed", status_code=424, detail=detail)
    earned = await _close_price(tool, run_id, price_held, success=True)
    body_out = {"run_id": run_id, "recipe": f"{tool.tool_id}@{tool.version}", "output": output,
                "usage": {"cost_micro": spent + earned, "steps_micro": spent, "price_micro": earned,
                          "steps": counted, "ms": ms_total}, "trace": trace, "log": log}
    await _record(tool, parent, run_id, "ok", counted, spent, ms_total,
                  masked(manifest["inputs"], inputs), trace, log=log, price=earned, output=output)
    _audit_parent(parent, tool, 200, spent + earned, audit_client)
    return _json(body_out, 200, {"X-Treg-Run-Id": run_id, "X-Treg-Steps": str(counted)}), spent + earned


MAX_TEXT = 1_000_000


def _csv_rows(text: str) -> list[dict[str, str]]:
    """The uploaded CSV as rows keyed by its header row, parsed once per run in the parent so the
    engine gets JSON, never a parser to write."""
    import csv
    import io
    reader = csv.DictReader(io.StringIO(text))
    return [dict(r) for r in reader]
