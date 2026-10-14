"""`/table/<tool id>`: one ordinary call, its answer read in full and returned as rows and columns.

The call itself runs on the call road unchanged (`routers.call.run_call_surface`): the same gates,
the same key injection, the same hold and settle, the same audit row, the same `Idempotency-Key`.
By the time `table_answer` runs, the call is finished and its money is final (the cost is already on
the answer as `X-Treg-Cost-Micro`), so nothing here can charge, refund or hold anything. A table is
a view of an answer that was bought: when the answer cannot be shaped, the view is `raw`, never an
error. `/call/` is not involved and never changes (AGENTS.md, non-negotiable 4).
"""

from __future__ import annotations

import gzip
import json
import zlib
from typing import Any

from ..domain import table as table_domain
from ..domain.catalog import store as catalog_store
from .call.types import CallContext, UpstreamResponse

MAX_TABLE_BYTES = 8 * 1024 * 1024   # the most of one answer /table/ reads (non-negotiable 4's cap)
BODY_EXCERPT_BYTES = 2048           # the part of an upstream error body the table error carries


def enabled_for(org_slug: str | None, email: str | None) -> bool:
    """`/table/` exists for this caller: the flag, then the team and person lists (either lets the
    caller in; both empty means every team). A caller outside them gets the 404 the flag off gives."""
    from ..config import get_settings
    s = get_settings()
    if not s.table_enabled:
        return False
    if not (s.table_team_set or s.table_user_set):
        return True
    return ((org_slug or "").lower() in s.table_team_set
            or (email or "").strip().lower() in s.table_user_set)


def plain_headers(raw_headers: tuple[tuple[bytes, bytes], ...], *,
                  drop_authorization: bool = False) -> list[tuple[bytes, bytes]]:
    """The caller's headers for the upstream call, asking for uncompressed bytes (a table must parse
    the body), the way the hub runner asks for its steps. With `drop_authorization` (the caller
    signed in with a "treg for Sheets" OAuth token) the `Authorization` header is removed: it is
    treg's own token, and the relay would otherwise pass it to the provider."""
    drop = {b"accept-encoding"} | ({b"authorization"} if drop_authorization else set())
    kept = [(k, v) for k, v in raw_headers if k.lower() not in drop]
    return kept + [(b"accept-encoding", b"identity")]


async def read_answer(upstream: UpstreamResponse) -> tuple[bytes, bool]:
    """The whole answer, at most MAX_TABLE_BYTES; (bytes, truncated). The stream is closed either way."""
    chunks: list[bytes] = []
    size = 0
    truncated = False
    try:
        async for chunk in upstream.body_stream:
            if size + len(chunk) > MAX_TABLE_BYTES:
                chunks.append(chunk[:MAX_TABLE_BYTES - size])
                truncated = True
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        await upstream.close()
    return b"".join(chunks), truncated


def _header(upstream: UpstreamResponse, name: str) -> str | None:
    wanted = name.lower().encode("latin-1")
    for k, v in upstream.raw_headers:
        if k.lower() == wanted:
            return v.decode("latin-1")
    return None


def _decoded(body: bytes, encoding: str | None) -> bytes:
    """A provider that ignored `Accept-Encoding: identity`: gzip and deflate are undone here; any other
    encoding stays as it is and reads as a `raw` table."""
    enc = (encoding or "").strip().lower()
    try:
        if enc == "gzip":
            return gzip.decompress(body)
        if enc == "deflate":
            return zlib.decompress(body)
    except (OSError, zlib.error):
        return body
    return body


def _tool_id(context: CallContext, rest: str) -> str:
    marketplace = context.marketplace
    if marketplace is not None and marketplace.endpoint_id:
        return marketplace.endpoint_id
    return rest.split("?", 1)[0]


async def _hub_fields(context: CallContext, rest: str) -> list[str] | None:
    """The output fields of the hub tool this call ran, in manifest order, or None when the id is not
    a hub tool. One short read, after the answer is fully read (non-negotiable 3)."""
    from . import hub as hub_app
    from ..infra.db import session_maker
    tool_ref = rest.split("?", 1)[0]
    if not hub_app.is_hub_id_shape(tool_ref):
        return None
    caller = context.input.caller
    async with session_maker() as db:
        row = await hub_app.tool_for(db, tool_ref, caller_org_id=caller.org_id,
                                     caller_slug=caller.org.slug, caller_email=caller.email)
    if row is None:
        return None
    output = (row.manifest or {}).get("output") or {}
    if isinstance(output, dict) and isinstance(output.get("fields"), list):
        return [str(f) for f in output["fields"]]
    return [str(k) for k in output] if isinstance(output, dict) else []


def _contract(tool_id: str) -> tuple[list[str], str | None] | None:
    """(output fields in contract order, the required list field or None) for a routed job."""
    cat = catalog_store.load()
    ep = cat.by_id.get(tool_id)
    if not ep or ep.get("kind") != "routed":
        return None
    contract = cat.contracts.get(ep.get("capability") or "")
    if contract is None:
        return None
    fields = list(contract.output)
    list_field = next((f for f, spec in contract.output.items()
                       if (spec or {}).get("type") == "list" and (spec or {}).get("required")), None)
    return fields, list_field


async def table_answer(context: CallContext, upstream: UpstreamResponse, rest: str) -> tuple[int, dict[str, Any], list[tuple[bytes, bytes]]]:
    """(status, JSON body, headers) for one finished call. The headers are the call's own `X-Treg-*`
    headers (call id, cost, served-by, idempotent replay), so a table answer carries the same
    receipts as the `/call/` answer would."""
    headers = [(k, v) for k, v in upstream.raw_headers if k.lower().startswith(b"x-treg-")]
    status = upstream.status
    raw_body, truncated = await read_answer(upstream)
    raw_body = _decoded(raw_body, _header(upstream, "content-encoding"))
    text = raw_body.decode("utf-8", "replace")
    meta = {"call_id": context.call_ref, "cost_micro": context.cost_micro}
    if not 200 <= status < 300:
        # The provider refused or failed: the same status as /call/ reports, with the start of its
        # body, so the add-on can show why. The call's money is already settled (or released).
        return status, {"error": "upstream_error", "upstream_status": status,
                        "body_excerpt": text[:BODY_EXCERPT_BYTES], "_treg": meta}, headers
    if truncated:
        table = table_domain.raw(text, truncated=True)
    else:
        try:
            body = json.loads(text) if text.strip() else None
        except ValueError:
            body = None
        if body is None:
            table = table_domain.raw(text)
        else:
            tool_id = _tool_id(context, rest)
            contract = _contract(tool_id)
            if contract is not None:
                table = table_domain.to_table(body, contract_output=contract[0], list_field=contract[1])
            elif tool_id not in catalog_store.load().by_id and (fields := await _hub_fields(context, rest)) is not None:
                table = table_domain.to_table(body, hub_fields=fields)
            else:
                table = table_domain.to_table(body)
    table["_treg"] = {**(table.get("_treg") or {}), **meta}
    return status, table, headers


def _example_body(ep: dict) -> Any:
    """The saved example answer of one catalog endpoint, or None. The file name comes from the
    loaded catalog row, never from the request."""
    name = ep.get("terminal_example_file") if ep.get("async") else ep.get("example_file")
    if not name:
        return None
    try:
        return json.loads((catalog_store.CATALOG_DIR / catalog_store.EXAMPLES_DIRNAME / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _coverage(tool_ref: str, fields: list[str]) -> dict[str, dict[str, int]]:
    """How many of a routed job's providers fill each output field: `{field: {filled_by, providers}}`.
    A provider fills a field when its adapter's `out` maps it. The cheapest provider answers first,
    so a field only some providers give can come back empty (email verify: trykitt has no score)."""
    cat = catalog_store.load()
    kids = [k for k in cat.by_id[tool_ref].get("routed_children") or [] if k in cat.adapters]
    return {f: {"filled_by": sum(1 for k in kids if f in cat.adapters[k].out_map), "providers": len(kids)}
            for f in fields}


async def preview(tool_ref: str, *, org_id: int, org_slug: str, email: str) -> dict[str, Any] | None:
    """The columns a `/table/` answer for this tool will have, with no provider call and no charge
    (`GET /table-columns/<tool id>`), so a client can show them before anyone pays. None when treg
    knows nothing about the tool's answer: no contract, no manifest, no saved example.

    - a routed job: its contract. A list job adds the columns of its first child's saved example
      items, after the mapped columns;
    - a hub tool: its manifest's output fields;
    - any other catalog endpoint: its saved example answer through the same converter, rows removed.
    """
    from . import hub as hub_app
    from ..infra.db import session_maker
    tool_ref = tool_ref.split("?", 1)[0]
    cat = catalog_store.load()
    contract = _contract(tool_ref)
    if contract is not None:
        fields, list_field = contract
        sample: list = []
        if list_field:
            for child in cat.by_id[tool_ref].get("routed_children") or []:
                child_ep = cat.by_id.get(child)
                sample = table_domain.sample_items(_example_body(child_ep)) if child_ep else []
                if sample:
                    break
        body = {"output": {list_field: sample} if list_field else {}, "_treg": {}}
        view = table_domain.columns_only(table_domain.to_table(body, contract_output=fields, list_field=list_field))
        view["coverage"] = _coverage(tool_ref, [list_field] if list_field else fields)
        return view
    ep = cat.by_id.get(tool_ref)
    if ep is not None:
        example = _example_body(ep)
        return table_domain.columns_only(table_domain.to_table(example)) if example is not None else None
    if not hub_app.is_hub_id_shape(tool_ref):
        return None
    async with session_maker() as db:
        row = await hub_app.tool_for(db, tool_ref, caller_org_id=org_id, caller_slug=org_slug, caller_email=email)
    if row is None:
        return None
    output = (row.manifest or {}).get("output") or {}
    fields = ([str(f) for f in output["fields"]] if isinstance(output, dict) and isinstance(output.get("fields"), list)
              else [str(k) for k in output] if isinstance(output, dict) else [])
    return {"shape": "flat", "columns": fields, "column_source": "hub"}


async def account_teams(db, *, user_id: int, only_org_id: int | None) -> list[dict[str, Any]]:
    """A person's teams with their role and balance (micro-USD), oldest membership first."""
    from sqlmodel import select
    from ..models import Membership, Org
    q = select(Membership, Org).join(Org, Org.id == Membership.org_id).where(Membership.user_id == user_id)
    if only_org_id is not None:
        q = q.where(Membership.org_id == only_org_id)
    rows = (await db.execute(q.order_by(Membership.id))).all()
    return [{"org_id": org.id, "slug": org.slug, "name": org.name, "role": m.role,
             "balance_micro": int(org.balance_micro or 0)}
            for m, org in rows if not org.suspended]

