"""The tool hub use cases: publish a tool, find the version that serves an id.

Phase 1 of the hub (docs/HUB-DECISIONS.md, `.arcterm` plan): a tool is validated and STORED here,
nothing runs yet. The check run that turns `unchecked` into `live` arrives with the runner, so
every row this phase writes stays `unchecked` and the call road answers 501 for it — the honest
shape until the runner exists (CLAUDE.md: never document, or serve, what is not built).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...domain.catalog import store as catalog_store
from ...domain.hub import (ManifestError, fees_label, price_label, seeded_observed, stored_pricing, validate, validate_check,
                           validate_readme)
from ...models import HubListing, HubTool, Org, Tool

HUB_ID_MIN_PARTS = 2


def enabled() -> bool:
    """The plain flag: the hub exists on this registry. Pages with no caller (the share page, the
    agent-facing files) use this one."""
    return bool(get_settings().hub_enabled)


def enabled_for(org_slug: str | None) -> bool:
    """The hub exists AND this team may use it: the flag, then `TREG_HUB_TEAMS` when it is set
    (empty = every team). Every gate that has a caller uses this one, so a team outside the list
    sees exactly what it sees with the flag off: 404 on the routes, no hub rows in search, the
    hub ids unknown on /call/ and in MCP."""
    s = get_settings()
    if not s.hub_enabled:
        return False
    teams = s.hub_team_set
    return not teams or (org_slug or "").lower() in teams


def visible_to(org_slug: str | None) -> bool:
    """May this reader see the hub at all? A reader with a team is judged by `enabled_for`. A reader
    with no team (no token, or a public page) sees it only when the hub is open to every team: while
    `TREG_HUB_TEAMS` limits it, search, catalog get, the share pages and the agent files show no
    trace of it to anyone outside the list."""
    if org_slug:
        return enabled_for(org_slug)
    return enabled() and not get_settings().hub_team_set


def is_hub_id_shape(rest: str) -> bool:
    """`<team-slug>.<name>[@N]`: dotted, slash-free, not a URL. Whether it IS a hub tool is the
    database's answer (`tool_for`); this only says the call road may ask."""
    if "/" in rest or rest.startswith("http") or "." not in rest:
        return False
    base = rest.split("@", 1)[0]
    return base.count(".") >= HUB_ID_MIN_PARTS - 1


def split_id(rest: str) -> tuple[str, int | None]:
    """`acme.leads-db@3` → (`acme.leads-db`, 3); no pin → (`acme.leads-db`, None)."""
    base, _, pin = rest.partition("@")
    if not pin:
        return base, None
    try:
        return base, int(pin)
    except ValueError:
        return base, None


OLD_VERSION_DAYS = 30   # a pinned old version stays callable this long after a newer live one


async def tool_for(db: AsyncSession, rest: str, *, live_only: bool = True,
                   caller_org_id: int | None = None, caller_slug: str | None = None) -> HubTool | None:
    """`_tool_for`, except that a tool whose listing treg REJECTED serves only its maker's team:
    another team's call and the public views get nothing (hub simulation run 3: a rejected
    "Official Hunter.io" tool stayed callable by id and share link). A tool never reviewed stays
    callable by id, so a maker can build and share before asking for search."""
    row = await _tool_for(db, rest, live_only=live_only, caller_org_id=caller_org_id, caller_slug=caller_slug)
    if row is None or (caller_org_id is not None and row.org_id == caller_org_id):
        return row
    return None if await is_rejected(db, row.tool_id) else row


async def is_rejected(db: AsyncSession, tool_id: str) -> bool:
    lst = await db.get(HubListing, tool_id)
    return lst is not None and lst.state == "rejected"


async def _tool_for(db: AsyncSession, rest: str, *, live_only: bool = True,
                    caller_org_id: int | None = None, caller_slug: str | None = None) -> HubTool | None:
    """The version that serves `rest`: the newest `live` one, or `@N` pinned. A pinned version may
    also be the one UNDER CHECK (the check run pins it: HUB-DECISIONS round 2 q10), and a pinned
    old version stays callable for OLD_VERSION_DAYS after a newer live one exists (round 4 q8)."""
    # The public views (catalog get, the share page) pass no caller and get the plain flag: a
    # contract is readable. A CALL names its caller's team and goes through the allow-list.
    if not visible_to(caller_slug) or not is_hub_id_shape(rest):
        return None
    tool_id, pin = split_id(rest)
    if pin is None:
        q = (select(HubTool).where(HubTool.tool_id == tool_id, HubTool.status == "live")
             .order_by(HubTool.version.desc()).limit(1))
        return (await db.execute(q)).scalars().first()
    row = (await db.execute(select(HubTool).where(HubTool.tool_id == tool_id, HubTool.version == pin))).scalars().first()
    if row is None:
        return None
    if row.status in ("checking", "review"):
        # the check run pins it (round 2 q10); a version waiting for review (round 4) is the maker's to
        # try by @N and nobody else's; anyone else who guesses @N gets nothing (8.1 review). The public
        # views (no caller) never show it: its contract is not reviewed yet (hub simulation run 1).
        return row if caller_org_id is not None and row.org_id == caller_org_id else None
    if live_only and row.status != "live":
        return None
    newer = (await db.execute(
        select(HubTool.created_at).where(HubTool.tool_id == tool_id, HubTool.status == "live",
                                         HubTool.version > pin)
        .order_by(HubTool.version.asc()).limit(1))).scalar_one_or_none()
    if newer is not None and (_utcnow() - newer).days >= OLD_VERSION_DAYS:
        return None
    return row


def _utcnow():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Published:
    tool_id: str
    version: int
    status: str
    kind: str


MAX_DATA_BYTES = 5_000_000   # the engine holds 64 MB; the rows travel into it on every run (8.1 review)


def validate_data(data: Any) -> str | None:
    """The uploaded CSV: text, at most 50 MB, a header row and at least one data row, parseable."""
    import csv
    import io
    if data is None or data == "":
        return None
    if not isinstance(data, str):
        raise ManifestError("data", "the CSV as text (the contents of data.csv)")
    if len(data.encode("utf-8")) > MAX_DATA_BYTES:
        raise ManifestError("data", "at most 5 MB")
    try:
        reader = csv.reader(io.StringIO(data))
        header = next(reader, None)
        first = next(reader, None)
    except csv.Error as exc:
        raise ManifestError("data", f"not valid CSV: {exc}") from None
    if not header or not any(h.strip() for h in header):
        raise ManifestError("data", "the first row must be the header (column names)")
    if first is None:
        raise ManifestError("data", "at least one data row under the header")
    return data


async def publish(
    db: AsyncSession, *, org: Org, maker_email: str,
    manifest: Any, script: str | None, check: Any, readme: Any, data: Any = None,
) -> Published:
    """Validate the four files against this team's world and store one version. Raises
    ManifestError with field + rule; the HTTP layer turns it into a 422 the maker's agent can act
    on. Does NOT commit: the caller's transaction owns it."""
    own_tools = {
        name for (name,) in (await db.execute(
            select(Tool.name).where(Tool.org_id == org.id))).all()
    }
    hub_ids = {
        tid for (tid,) in (await db.execute(select(HubTool.tool_id).distinct())).all()
    }
    v = validate(manifest, catalog_ids=set(catalog_store.load().by_id), capabilities=set(catalog_store.load().capabilities),
                 own_tools=own_tools, hub_ids=hub_ids)
    if v.kind == "script":
        if not isinstance(script, str) or not script.strip():
            raise ManifestError("script", "the manifest names run.js; send its contents as `script`")
        if len(script) > 200_000:
            raise ManifestError("script", "at most 200,000 characters")
    elif script:
        raise ManifestError("script", "a steps recipe carries no script; remove it or switch to \"script\": \"run.js\"")
    output_fields = (v.output["fields"] if v.kind == "script" else list(v.output))
    check_v = validate_check(check, v.inputs, output_fields)
    readme_v = validate_readme(readme)
    data_v = validate_data(data)
    if data_v is not None and v.kind != "script":
        raise ManifestError("data", "an uploaded CSV is read by a script (ctx.data); a steps recipe has no reader for it")

    tool_id = f"{org.slug}.{v.name}"
    newest = (await db.execute(
        select(HubTool.version).where(HubTool.tool_id == tool_id)
        .order_by(HubTool.version.desc()).limit(1))).scalar_one_or_none()
    version = 1 if newest is None else newest + 1
    row = HubTool(
        org_id=org.id, tool_id=tool_id, name=v.name, version=version, kind=v.kind,
        status="checking", summary=v.summary, writes=v.writes, price_micro=v.price_micro,
        manifest={**v.manifest, "version": version}, script=script if v.kind == "script" else None,
        check=check_v, readme=readme_v, created_by=maker_email, data=data_v,
    )
    db.add(row)
    await db.flush()
    return Published(tool_id=tool_id, version=version, status=row.status, kind=v.kind)


async def run_check(db: AsyncSession, row: HubTool, *, maker_headers: dict[str, str], app: Any) -> dict[str, Any]:
    """The check run (HUB-DECISIONS round 2 q10, round 3 q6): `check.json`'s sample inputs, run
    ONCE for real as the maker over the real call road (`POST /call/<id>@<version>` in-process
    with the maker's own identity headers), charged to the maker's balance at the normal step
    prices, seller price not charged. Pass ⇒ `live`; fail ⇒ `failed`, with the reason on the row.
    `app` is the running application (the router passes `request.app`), so this layer never
    imports the HTTP entry point. Returns the verdict dict, also stored as `check_result`. Does not commit."""
    import httpx

    from ...application.hub import runner as hub_runner

    headers = {k: v for k, v in maker_headers.items() if k.lower() in ("x-treg-token", "x-treg-org", "cookie")}
    headers["X-Treg-Client"] = "hub-check"
    headers[hub_runner.RUN_MAX_COST_HEADER] = "5.00"
    try:
        from .health import verdict_from
        cases = row.check.get("cases") or [row.check]
        verdict: dict[str, Any] = {}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://treg.internal",
                                     headers=headers, timeout=200.0) as client:
            for i, case in enumerate(cases):
                r = await client.post(f"/call/{row.tool_id}@{row.version}", json=case.get("inputs", {}))
                try:
                    body = r.json()
                except ValueError:
                    body = {"text": r.text[:600]}
                v = verdict_from(row, r.status_code, body, dict(r.headers), check=case)
                if i == 0:
                    verdict = v                   # the first case's run is the one the reviewer compares
                if v["status"] != "passed":
                    verdict = {**v, **({"case": i + 1} if len(cases) > 1 else {})}
                    break
        if verdict.get("status") == "passed" and len(cases) > 1:
            verdict["cases"] = len(cases)
        row.status = "live" if verdict["status"] == "passed" else "failed"
        if row.status == "live":
            await _hold_for_review(db, row)
        row.check_result = verdict
        db.add(row)
        return verdict
    except Exception as exc:  # noqa: BLE001 - a crashed check must not leave the version `checking`
        verdict = {"status": "failed", "checked_at": _utcnow().isoformat(),
                   "error": {"error": "check_crashed", "kind": type(exc).__name__,
                             "message": "the check could not finish; publish again"}}
        row.status = "failed"
        row.check_result = verdict
        db.add(row)
        return verdict


async def price_ranges(db: AsyncSession, tools: dict[str, dict[str, Any]], *, days: int = 30) -> dict[str, dict[str, Any]]:
    """Per tool id: the lowest and highest total a successful run cost in the last `days` (steps plus
    the seller's part, `seller_part_micro`) and the sample count, over EVERY successful run of that
    tool, the maker's own and the scheduled checks included: the steps cost the same whoever calls,
    and a new tool would otherwise show no number until a stranger pays. `tools` maps a tool id to
    the manifest that prices it (the newest). A tool with no run is absent from the answer."""
    from datetime import timedelta
    from ...domain.hub import seller_part_micro
    from ...models import HubRun
    from ...timeutil import utcnow_naive
    if not tools:
        return {}
    since = utcnow_naive() - timedelta(days=days)
    recent = (HubRun.tool_id.in_(list(tools)), HubRun.version > 0, HubRun.status == "ok", HubRun.started_at >= since)
    own = HubRun.caller_org_id == HubRun.maker_org_id
    # The trace is read only where the seller part is derived from it: the maker's own runs of a
    # script, whose ctx.charge lines say what a caller would have paid.
    scripts = [tid for tid, m in tools.items() if stored_pricing(m)["mode"] == "charge"]
    charged: dict[int, int] = {}
    if scripts:
        for rid, trace in (await db.execute(select(HubRun.id, HubRun.trace).where(
                *recent, own, HubRun.tool_id.in_(scripts)))).all():
            charged[rid] = sum(int(e.get("cost_micro") or 0) for e in (trace or [])
                               if isinstance(e, dict) and e.get("outcome") == "charged")
    # Runs by other teams decide the number once there are any; before that the maker's own runs and
    # checks stand in, marked `from_tests` (hub simulation run 2: free own runs shaped the figure).
    # Only runs of the version the manifest belongs to: an unapproved version's check run moved the
    # serving version's public price (hub simulation run 3).
    buckets: dict[str, dict[bool, dict[str, Any]]] = {}
    for rid, tid, ver, cost, price, is_own in (await db.execute(
            select(HubRun.id, HubRun.tool_id, HubRun.version, HubRun.cost_micro, HubRun.price_micro, own)
            .where(*recent))).all():
        if tools[tid].get("version") and ver != tools[tid]["version"]:
            continue
        total = int(cost or 0) + seller_part_micro(tools[tid], None if is_own else int(price or 0),
                                                   charged.get(rid, 0))
        fee = int(cost or 0)
        r = buckets.setdefault(tid, {}).setdefault(bool(is_own), {"low_micro": total, "high_micro": total, "samples": 0,
                                                                  "fees_low_micro": fee, "fees_high_micro": fee})
        r["low_micro"], r["high_micro"] = min(r["low_micro"], total), max(r["high_micro"], total)
        r["fees_low_micro"], r["fees_high_micro"] = min(r["fees_low_micro"], fee), max(r["fees_high_micro"], fee)
        r["samples"] += 1
    out: dict[str, dict[str, Any]] = {}
    for tid, b in buckets.items():
        out[tid] = b[False] if False in b else {**b[True], "from_tests": True}
    return out


def with_range(manifest: dict[str, Any], rng: dict[str, Any] | None) -> dict[str, Any]:
    """The keys every price surface carries: `price_range` (the headline, `range_label`),
    `price_samples` (how many runs it rests on; 0 means the declared worst case) and the observed
    `price_low_micro`/`price_high_micro` (None before any run) for a surface that shows the numbers
    alone."""
    from ...domain.hub import range_label
    rng = rng or {}
    label = range_label(manifest, rng.get("low_micro"), rng.get("high_micro"))
    if rng.get("from_tests"):
        label += " (from the maker's own tests)"
    return {"price_range": label, "price_from_tests": bool(rng.get("from_tests")),
            "price_samples": int(rng.get("samples", 0)),
            "price_low_micro": rng.get("low_micro"), "price_high_micro": rng.get("high_micro")}


def worst_usd(manifest: dict[str, Any], price_micro: int, rng: dict[str, Any] | None) -> float | None:
    """`cost.usd` for a hub row: the most a run has cost recently (steps and seller part) when runs
    exist, else the declared price: a recipe's fixed price or a script's cap."""
    if rng and rng.get("samples"):
        return rng["high_micro"] / 1_000_000
    p = stored_pricing({"price_usd": price_micro / 1_000_000, **manifest})
    return p["max_price_usd"] if p["mode"] == "charge" else p["price_usd"]


def listing_view(listing: HubListing | None) -> dict[str, Any]:
    """The tool's place in search, for the maker: `listed` is true only once treg approved it;
    `listing` says where the request stands (none | requested | approved | rejected | unlisted).
    `reason` is the admin's last rejection reason; it stays while the maker asks again."""
    if listing is None:
        return {"listed": False, "listing": {"state": "none"}}
    update = None
    if listing.pending_version or listing.pending_pricing or listing.update_reason:
        update = {"state": "pending" if (listing.pending_version or listing.pending_pricing) else "rejected",
                  "version": listing.pending_version or None, "pricing": listing.pending_pricing,
                  "reason": listing.update_reason}
    return {"listed": listing.state == "approved",
            "listing": {"state": listing.state, "reason": listing.reason, "capability": listing.capability,
                        "update": update, "reviewed": bool(listing.reviewed),
                        "requested_at": listing.requested_at.isoformat(),
                        "decided_at": listing.decided_at.isoformat() if listing.decided_at else None}}


async def listings_of(db: AsyncSession, tool_ids: list[str]) -> dict[str, HubListing]:
    if not tool_ids:
        return {}
    return {r.tool_id: r for r in (await db.execute(
        select(HubListing).where(HubListing.tool_id.in_(tool_ids)))).scalars().all()}


def view(row: HubTool, listing: HubListing | None = None) -> dict[str, Any]:
    """The maker-facing shape of one version. The script is the maker's own; it is returned to
    the maker here and to nobody else (the public page of phase 7 hides it)."""
    return {
        "tool_id": row.tool_id, "version": row.version, "kind": row.kind, "status": row.status,
        "summary": row.summary, "writes": row.writes, "price_usd": row.price_micro / 1_000_000,
        "pricing": stored_pricing({"price_usd": row.price_micro / 1_000_000, **row.manifest}),
        **listing_view(listing), "public_log": bool(row.public_log),
        "price_label": price_label(row.manifest),
        "uses": row.manifest.get("uses", []), "inputs": row.manifest.get("inputs", {}),
        "capability": row.manifest.get("capability"),
        "output": row.manifest.get("output", {}), "limits": row.manifest.get("limits", {}),
        "created_by": row.created_by, "created_at": row.created_at.isoformat(),
        "check_result": row.check_result,
        "data": ({"rows": max(0, (row.data.count("\n") + (0 if row.data.endswith("\n") else 1)) - 1),
                  "bytes": len(row.data.encode("utf-8"))} if row.data else None),
    }


async def transient(db: AsyncSession, *, org: Org, maker_email: str,
                    manifest: Any, script: str | None, check: Any, readme: Any, data: Any = None) -> HubTool:
    """The same validation as publish(), but the row is NOT added to the session: a dry run of a
    folder from the maker's machine. Version 0 marks it in every trace."""
    own_tools = {name for (name,) in (await db.execute(select(Tool.name).where(Tool.org_id == org.id))).all()}
    hub_ids = {tid for (tid,) in (await db.execute(select(HubTool.tool_id).distinct())).all()}
    v = validate(manifest, catalog_ids=set(catalog_store.load().by_id), capabilities=set(catalog_store.load().capabilities), own_tools=own_tools, hub_ids=hub_ids)
    if v.kind == "script" and not (isinstance(script, str) and script.strip()):
        raise ManifestError("script", "the manifest names run.js; send its contents as `script`")
    output_fields = (v.output["fields"] if v.kind == "script" else list(v.output))
    validate_check(check, v.inputs, output_fields)
    validate_readme(readme)
    data_v = validate_data(data)
    return HubTool(org_id=org.id, tool_id=f"{org.slug}.{v.name}", name=v.name, version=0, kind=v.kind,
                   status="dry-run", summary=v.summary, writes=v.writes, price_micro=v.price_micro,
                   manifest={**v.manifest, "version": 0}, script=script if v.kind == "script" else None,
                   check=check, readme=readme, created_by=maker_email, data=data_v)


async def retire(db: AsyncSession, *, org_id: int, tool_id: str) -> int:
    """Every version of a team's tool leaves the call road (round 7 of the case study: the owner
    asked for it). Returns how many versions changed. Does not commit."""
    from sqlalchemy import update
    result = await db.execute(update(HubTool).where(
        HubTool.tool_id == tool_id, HubTool.org_id == org_id, HubTool.status != "retired")
        .values(status="retired"))
    return int(result.rowcount or 0)


async def set_price(db: AsyncSession, *, org_id: int, tool_id: str, price_usd: float) -> HubTool | None:
    """The price applies to later runs of the newest live version (round 3 q8): no version bump,
    every trace stamps the price it paid. A JSON recipe's `price_usd`; a script's `max_price_usd`,
    the cap on its ctx.charge lines (above 0: a script's amounts live in run.js). Returns the row,
    or None when the team has no such live tool. Does not commit."""
    from ...domain.hub import manifest as hub_manifest
    row = (await db.execute(select(HubTool).where(
        HubTool.tool_id == tool_id, HubTool.org_id == org_id, HubTool.status == "live")
        .order_by(HubTool.version.desc()).limit(1))).scalars().first()
    if row is None:
        return None
    lst = await db.get(HubListing, tool_id)
    if lst is not None and lst.reviewed:
        # A reviewed tool's price is public: the new one waits for review (round 4), also after the
        # maker unlisted it (run 2: unlisting was a way round the review); check it now so
        # the maker hears a bad number at once, not from the reviewer.
        if row.kind == "script":
            hub_manifest._money_micro("max_price_usd", price_usd, allow_zero=False)
        else:
            hub_manifest._validate_price(price_usd)
        lst.pending_pricing, lst.update_reason = {"price_usd": float(price_usd)}, ""
        db.add(lst)
        return row
    return await _apply_price(db, row, price_usd)


async def _apply_price(db: AsyncSession, row: HubTool, price_usd: float) -> HubTool:
    from ...domain.hub import manifest as hub_manifest
    if row.kind == "script":
        micro = hub_manifest._money_micro("max_price_usd", price_usd, allow_zero=False)
        row.manifest = {**{k: v for k, v in row.manifest.items() if k != "price_usd"},
                        "pricing": {"mode": "charge", "max_price_usd": micro / 1_000_000}}
        db.add(row)
        return row
    micro = hub_manifest._validate_price(price_usd)
    row.price_micro = micro
    row.manifest = {**row.manifest, "price_usd": micro / 1_000_000,
                    "pricing": {"mode": "per_call", "price_usd": micro / 1_000_000}}
    db.add(row)
    return row


async def search_listed(db: AsyncSession, query: str, cat: Any, *,
                        org_slug: str | None = None) -> tuple[list[tuple[dict, float]], dict[str, dict]]:
    """The listed live hub tools that match `query` (docs/hub-listing-decisions.md, decision 2):
    the newest live version of every tool with `listed` on, scored by `catalog_store.score_extra`
    (the catalog's own tokens, idf and gate, no boost). Returns `([(row, score)], stats)`; `stats`
    is keyed by id with the 30-day ok rate and sample count of runs by OTHERS, the same shape the
    evidence rerank reads for a catalog row. The row is the public contract: never the script, the
    maker's tools or a key."""
    if not visible_to(org_slug) or not query.strip():
        return [], {}
    from datetime import timedelta
    from ...domain.catalog import store as catalog_store
    from ...domain.hub import price_label
    from ...models import HubRun
    from ...timeutil import utcnow_naive
    rows = (await db.execute(
        select(HubTool).join(HubListing, HubListing.tool_id == HubTool.tool_id)
        .where(HubListing.state == "approved", HubTool.status == "live")
        .order_by(HubTool.tool_id, HubTool.version.desc()))).scalars().all()
    caps = {r.tool_id: r.capability for r in (await db.execute(
        select(HubListing).where(HubListing.state == "approved"))).scalars().all()}
    newest: dict[str, HubTool] = {}
    for r in rows:
        newest.setdefault(r.tool_id, r)
    if not newest:
        return [], {}
    org_ids = {r.org_id for r in newest.values()}
    slugs = {o.id: o.slug for o in (await db.execute(select(Org).where(Org.id.in_(org_ids)))).scalars().all()}
    since = utcnow_naive() - timedelta(days=30)
    agg: dict[str, list[int]] = {}
    for tid, status in (await db.execute(
            select(HubRun.tool_id, HubRun.status)
            .where(HubRun.tool_id.in_(list(newest)), HubRun.version > 0, HubRun.started_at >= since,
                   HubRun.caller_org_id != HubRun.maker_org_id))).all():
        a = agg.setdefault(tid, [0, 0])
        a[0] += 1
        a[1] += 1 if status == "ok" else 0
    ranges = await price_ranges(db, {tid: r.manifest for tid, r in newest.items()})
    extra = []
    for tid, r in newest.items():
        m = r.manifest
        slug = slugs.get(r.org_id, "")
        worst = worst_usd(m, r.price_micro, ranges.get(tid))
        ep = {
            "id": tid, "kind": "hub", "hub": True, "version": r.version,
            "name": r.name, "summary": r.summary, "provider": slug, "provider_display": slug,
            "capability": caps.get(tid) or None, "capability_description": cat.capabilities.get(caps.get(tid) or "", ""),
            "platform": "", "tier": "core", "verified": True,
            "method": "POST", "path": f"/call/{tid}", "writes": r.writes,
            "cost": {"type": "per_success", "usd": worst, "currency": "USD", "unit": "run"},
            "price_line": "seller " + price_label(m) + fees_label(m, ranges.get(tid)), "price_label": price_label(m),
            **with_range(m, ranges.get(tid)),
            "made_of": len(m.get("uses", [])),
        }
        fields = [(catalog_store.W_SUMMARY, f"{r.name} {r.summary}".lower()),
                  (catalog_store.W_PATH, f"{tid} {slug}".lower())]
        if caps.get(tid):          # an approved job speaks the catalog's own words, like a provider's row
            fields.append((catalog_store.W_CAPABILITY, f"{caps[tid]} {cat.capabilities.get(caps[tid], '')}".lower()))
        extra.append((ep, fields))
    scored = catalog_store.score_extra(query, cat, extra)
    stats = {tid: {"ok_rate": (a[1] / a[0]) if a[0] else None, "samples": a[0]} for tid, a in agg.items()}
    return scored, stats


async def set_flags(db: AsyncSession, *, org_id: int, tool_id: str, maker_email: str = "",
                    listed: bool | None = None, public_log: bool | None = None) -> HubTool | None:
    """The maker's two distribution switches (docs/hub-listing-decisions.md), no version bump.
    `public_log` is on the newest live version. `listed` is the tool's HubListing row: True asks for
    a place in search (a new request, or a rejected one asked again; an approved or pending one is
    left alone), False takes the tool out and withdraws any request. A switch given as None is left
    alone. Returns the newest live row, or None when the team has no such live tool. Does not commit."""
    row = (await db.execute(select(HubTool).where(
        HubTool.tool_id == tool_id, HubTool.org_id == org_id, HubTool.status == "live")
        .order_by(HubTool.version.desc()).limit(1))).scalars().first()
    if row is None:
        return None
    if listed is not None:
        from ...timeutil import utcnow_naive
        current = await db.get(HubListing, tool_id)
        if not listed:
            if current is not None and current.reviewed:
                # Hidden from search, still reviewed: callers by id keep the approved version and
                # price, and every later change still waits (hub simulation run 2).
                if current.state == "approved":
                    current.state = "unlisted"
                    db.add(current)
            elif current is not None:
                await db.delete(current)                 # a request never approved: withdrawn
        elif current is None:
            db.add(HubListing(tool_id=tool_id, org_id=org_id, state="requested", requested_by=maker_email))
        elif current.state == "unlisted":
            current.state = "approved"                   # the tool did not change unreviewed: back at once
            db.add(current)
        elif current.state == "rejected":
            # Asking again keeps the last reason, so the maker can still read what to fix.
            current.state, current.requested_by = "requested", maker_email
            current.requested_at, current.decided_by, current.decided_at = utcnow_naive(), "", None
            db.add(current)
    if public_log is not None:
        row.public_log = bool(public_log)
        db.add(row)
    return row


async def pending_listings(db: AsyncSession, state: str = "requested") -> list[dict[str, Any]]:
    """The listing requests treg reviews (a superadmin's queue), oldest first, each with what the
    reviewer reads: the newest live version's summary, price, uses, health of its check."""
    rows = (await db.execute(select(HubListing).where(HubListing.state == state)
                             .order_by(HubListing.requested_at))).scalars().all()
    out = []
    for lst in rows:
        tool = (await db.execute(select(HubTool).where(HubTool.tool_id == lst.tool_id, HubTool.status == "live")
                                 .order_by(HubTool.version.desc()).limit(1))).scalars().first()
        proposed = (tool.manifest.get("capability") or "") if tool else ""
        cat = catalog_store.load()
        out.append({"tool_id": lst.tool_id, "state": lst.state, "reason": lst.reason,
                    "capability": lst.capability, "proposed_capability": proposed,
                    "proposed_capability_description": cat.capabilities.get(proposed, ""),
                    "proposed_capability_providers": len(cat.for_capability(proposed)) if proposed else 0,
                    "requested_by": lst.requested_by, "requested_at": lst.requested_at.isoformat(),
                    "decided_by": lst.decided_by,
                    "decided_at": lst.decided_at.isoformat() if lst.decided_at else None,
                    "live": tool is not None,
                    **({"version": tool.version, "kind": tool.kind, "summary": tool.summary,
                        "price_label": price_label(tool.manifest), "uses": tool.manifest.get("uses", []),
                        "check": (tool.check_result or {}).get("status"),
                        # What the reviewer approves is the code, not the summary (hub simulation run 3:
                        # a date switch in run.js was invisible to the queue).
                        "script": tool.script, "steps": tool.manifest.get("steps"),
                        "own_tools": await own_tool_urls(db, tool)} if tool else {})})
    return out


async def decide_listing(db: AsyncSession, *, tool_id: str, approve: bool, reason: str,
                         admin_email: str, capability: str | None = None) -> HubListing | None:
    """A superadmin's decision on a tool's listing: approve puts it in search, reject takes it out
    (a first answer, or an approval taken back) with a reason the maker reads. An approval also
    sets the catalog job the tool sits beside: `capability` when given ("" = none), else the one the
    newest live manifest proposes. None when the tool has no listing row (it never asked, or the
    maker unlisted it). Raises ManifestError for a capability the catalog does not have. Does not
    commit."""
    from ...timeutil import utcnow_naive
    lst = await db.get(HubListing, tool_id)
    if lst is None:
        return None
    if approve:
        if capability is None:
            tool = (await db.execute(select(HubTool).where(HubTool.tool_id == tool_id, HubTool.status == "live")
                                     .order_by(HubTool.version.desc()).limit(1))).scalars().first()
            capability = (tool.manifest.get("capability") or "") if tool else ""
        if capability and capability not in catalog_store.load().capabilities:
            raise ManifestError("capability", f"{capability!r} is not a catalog capability")
        lst.capability, lst.reviewed = capability, True
    else:
        lst.capability = ""                              # a reviewed tool's waiting update stays waiting
    lst.state, lst.reason = ("approved", "") if approve else ("rejected", reason.strip()[:500])
    lst.decided_by, lst.decided_at = admin_email, utcnow_naive()
    db.add(lst)
    return lst


async def capability_siblings(db: AsyncSession, capability: str, *, exclude: str = "", org_slug: str | None = None) -> list[dict[str, Any]]:
    """The approved, live hub tools that do `capability`, as the sibling rows catalog_get shows
    beside that job's providers (docs/hub-listing-decisions.md round 3). Each carries the public
    contract's price and a seeded `observed` (`seeded_observed`), from runs by other teams in the
    last 30 days. Never routed to: an agent compares and picks (AGENTS.md non-negotiable 4)."""
    if not visible_to(org_slug) or not capability:
        return []
    from datetime import timedelta
    from ...models import HubRun
    from ...timeutil import utcnow_naive
    ids = [t for (t,) in (await db.execute(select(HubListing.tool_id).where(
        HubListing.state == "approved", HubListing.capability == capability))).all() if t != exclude]
    if not ids:
        return []
    rows = (await db.execute(select(HubTool).where(HubTool.tool_id.in_(ids), HubTool.status == "live")
                             .order_by(HubTool.tool_id, HubTool.version.desc()))).scalars().all()
    newest: dict[str, HubTool] = {}
    for r in rows:
        newest.setdefault(r.tool_id, r)
    if not newest:
        return []
    slugs = {o.id: o.slug for o in (await db.execute(
        select(Org).where(Org.id.in_({r.org_id for r in newest.values()})))).scalars().all()}
    agg: dict[str, list[int]] = {}
    for tid, status in (await db.execute(
            select(HubRun.tool_id, HubRun.status).where(
                HubRun.tool_id.in_(list(newest)), HubRun.version > 0,
                HubRun.started_at >= utcnow_naive() - timedelta(days=30),
                HubRun.caller_org_id != HubRun.maker_org_id))).all():
        a = agg.setdefault(tid, [0, 0])
        a[0] += 1
        a[1] += 1 if status == "ok" else 0
    ranges = await price_ranges(db, {tid: r.manifest for tid, r in newest.items()})
    out = []
    for tid, r in newest.items():
        slug = slugs.get(r.org_id, "")
        runs, ok = agg.get(tid, [0, 0])
        out.append({"id": tid, "kind": "hub", "hub": True, "provider": slug, "provider_display": slug,
                    "name": r.name, "summary": r.summary, "capability": capability, "method": "POST",
                    "path": f"/call/{tid}",
                    "cost": {"type": "per_success", "usd": worst_usd(r.manifest, r.price_micro, ranges.get(tid)),
                             "currency": "USD", "unit": "run"},
                    "price_line": "seller " + price_label(r.manifest) + fees_label(r.manifest, ranges.get(tid)),
                    **with_range(r.manifest, ranges.get(tid)),
                    "observed": seeded_observed(ok, runs)})
    return out


async def approved_capability(db: AsyncSession, tool_id: str) -> str:
    lst = await db.get(HubListing, tool_id)
    return lst.capability if lst is not None and lst.state == "approved" else ""


# ---------------------------------------------------------------------------------------------
# Updates to an approved tool wait for review (docs/hub-listing-decisions.md round 4): a new version
# and a price change. The approved version and price keep serving until treg decides.

async def _hold_for_review(db: AsyncSession, row: HubTool) -> None:
    """A version that passed its check becomes `review` instead of `live` when treg has ever approved
    its tool (listed, unlisted or taken back). A newer one replaces an older one still waiting (`superseded`)."""
    lst = await db.get(HubListing, row.tool_id)
    if lst is None or not lst.reviewed:
        return
    if lst.pending_version and lst.pending_version != row.version:
        old = (await db.execute(select(HubTool).where(HubTool.tool_id == row.tool_id,
                                                      HubTool.version == lst.pending_version))).scalars().first()
        if old is not None and old.status == "review":
            old.status = "superseded"
            db.add(old)
    row.status = "review"
    lst.pending_version, lst.update_reason = row.version, ""
    db.add(lst)


async def _release_update(db: AsyncSession, lst: HubListing) -> None:
    """An approved update: the waiting version goes live and the waiting price applies."""
    if lst.pending_version:
        row = (await db.execute(select(HubTool).where(HubTool.tool_id == lst.tool_id,
                                                      HubTool.version == lst.pending_version))).scalars().first()
        if row is not None and row.status == "review":
            row.status = "live"
            db.add(row)
            await db.flush()
    if lst.pending_pricing:
        row = (await db.execute(select(HubTool).where(HubTool.tool_id == lst.tool_id, HubTool.status == "live")
                                .order_by(HubTool.version.desc()).limit(1))).scalars().first()
        if row is not None:
            await _apply_price(db, row, lst.pending_pricing["price_usd"])
    lst.pending_version, lst.pending_pricing, lst.update_reason = 0, None, ""
    db.add(lst)


async def pending_updates(db: AsyncSession) -> list[dict[str, Any]]:
    """The review queue of updates: every approved tool with a version or a price waiting, each with
    what serves now beside what would replace it, so the reviewer reads the change."""
    rows = (await db.execute(select(HubListing).where(HubListing.reviewed == True)  # noqa: E712
                             .order_by(HubListing.tool_id))).scalars().all()

    def side(t: HubTool | None) -> dict[str, Any] | None:
        if t is None:
            return None
        return {"version": t.version, "kind": t.kind, "summary": t.summary, "price_label": price_label(t.manifest),
                "uses": t.manifest.get("uses", []), "capability": t.manifest.get("capability"),
                "check": (t.check_result or {}).get("status")}
    out = []
    for lst in rows:
        if not (lst.pending_version or lst.pending_pricing):
            continue
        now = (await db.execute(select(HubTool).where(HubTool.tool_id == lst.tool_id, HubTool.status == "live")
                                .order_by(HubTool.version.desc()).limit(1))).scalars().first()
        new = None
        if lst.pending_version:
            new = (await db.execute(select(HubTool).where(HubTool.tool_id == lst.tool_id,
                                                          HubTool.version == lst.pending_version))).scalars().first()
        out.append({"tool_id": lst.tool_id, "now": side(now), "new": side(new),
                    "new_price_usd": (lst.pending_pricing or {}).get("price_usd"),
                    "code_diff": _code_diff(now, new),
                    "own_tools": await own_tool_urls(db, new) if new is not None else [],
                    "fields_lost": await _fields_lost(db, now, new)})
    return out


async def _fields_lost(db: AsyncSession, now: HubTool | None, new: HubTool | None) -> list[str]:
    """Output fields the approved version's check run filled that the new version's check run left
    empty. A maker writes its own check.json, so a check can prove little (hub simulation run 2:
    v4 never returned the industry and passed a check that asked only for the domain); the reviewer
    sees what the new version stopped returning."""
    from ...models import HubRun
    if now is None or new is None:
        return []

    async def output_of(t: HubTool) -> dict[str, Any]:
        rid = (t.check_result or {}).get("run_id")
        if not rid:
            return {}
        o = (await db.execute(select(HubRun.output).where(HubRun.run_id == rid))).scalar_one_or_none()
        return o if isinstance(o, dict) else {}
    empty = (None, "", [], {})
    before, after = await output_of(now), await output_of(new)
    return sorted(f for f, v in before.items() if v not in empty and after.get(f) in empty)


async def decide_update(db: AsyncSession, *, tool_id: str, approve: bool, reason: str,
                        admin_email: str) -> HubListing | None:
    """Approve: the waiting version goes live and serves everyone; the waiting price applies to the
    version that serves. Reject: the waiting version is `rejected` (kept, never served), the waiting
    price dropped, and the maker reads the reason. None when nothing waits. Does not commit."""
    lst = await db.get(HubListing, tool_id)
    if lst is None or not (lst.pending_version or lst.pending_pricing):
        return None
    if approve:
        await _release_update(db, lst)
    else:
        if lst.pending_version:
            row = (await db.execute(select(HubTool).where(HubTool.tool_id == tool_id,
                                                          HubTool.version == lst.pending_version))).scalars().first()
            if row is not None and row.status == "review":
                row.status = "rejected"
                db.add(row)
        lst.pending_version, lst.pending_pricing, lst.update_reason = 0, None, reason.strip()[:500]
        db.add(lst)
    return lst


async def own_hosts(db: AsyncSession, row: HubTool) -> list[str]:
    """The hosts of the maker's own tools this version calls: a caller's inputs can reach them, and
    the maker can change what answers there without a new version (hub simulation run 2). Shown on
    the public contract, host only."""
    names = [u for u in row.manifest.get("uses", []) if "." not in u]
    if not names:
        return []
    hosts = (await db.execute(select(Tool.host).where(Tool.org_id == row.org_id, Tool.name.in_(names)))).scalars().all()
    return sorted({h for h in hosts if h})


def treg_hosts() -> set[str]:
    """treg's own hosts: this deployment's public URL and the hosted service's names. A team's own tool
    pointed at one of them turns a hub tool into a relay to another hub tool, which the depth-one rule
    and the review exist to stop (hub simulation run 3: `treg-relay` -> https://<treg>/call)."""
    from urllib.parse import urlsplit
    from ...config import PUBLIC_HOST_ALIASES
    return {h for h in {(urlsplit(get_settings().public_url).hostname or "").lower(), *PUBLIC_HOST_ALIASES} if h}


def points_at_treg(base_url: str) -> bool:
    from urllib.parse import urlsplit
    return (urlsplit(base_url or "").hostname or "").lower() in treg_hosts()


def _code(t: HubTool | None) -> str:
    if t is None:
        return ""
    if t.kind == "script":
        return t.script or ""
    import json as _json
    return _json.dumps(t.manifest.get("steps") or [], indent=2)


def _code_diff(now: HubTool | None, new: HubTool | None) -> str:
    """A unified diff of the code a reviewer approves (run.js, or the steps), approved -> new. With
    no approved version to compare, the new code in full."""
    import difflib
    if new is None:
        return ""
    if now is None:
        return _code(new)
    return "".join(difflib.unified_diff(_code(now).splitlines(True), _code(new).splitlines(True),
                                        fromfile=f"v{now.version}", tofile=f"v{new.version}"))


async def own_tool_urls(db: AsyncSession, row: HubTool) -> list[dict[str, str]]:
    """The maker's own tools a version calls, with their base URLs, for the reviewer (not public)."""
    names = [u for u in row.manifest.get("uses", []) if "." not in u]
    if not names:
        return []
    rows = (await db.execute(select(Tool.name, Tool.base_url).where(
        Tool.org_id == row.org_id, Tool.name.in_(names)))).all()
    return [{"name": n, "base_url": b} for n, b in rows]

