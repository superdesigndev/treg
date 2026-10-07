"""What the vibe-it page shows above and beside the chat (docs/context/architecture/vibe-it.md): the
published tool's state, and the warnings a maker should read before a test run.

Reads only. `warnings` asks the catalog's own access answer in-process as the maker, so the session
it is given must not hold a connection (the caller commits first).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.catalog import store as catalog_store
from ...domain.hub import price_label
from ...models import HubRun, HubTool, Org
from ...timeutil import utcnow_naive
from ..hub import apps as hub_apps
from ..hub import health as hub_health
from ..hub import listings_of


async def tool_status(db: AsyncSession, org: Org, tool_id: str | None) -> dict[str, Any] | None:
    """The conversation's published tool: newest version and its state, the version callers get,
    the app, runs by others in 30 days, and where to find it."""
    if not tool_id:
        return None
    rows = (await db.execute(select(HubTool).where(HubTool.tool_id == tool_id, HubTool.org_id == org.id)
                             .order_by(HubTool.version.desc()))).scalars().all()
    if not rows:
        return None
    newest = rows[0]
    live = next((r for r in rows if r.status == "live"), None)
    h = await hub_health.health_of(db, newest.tool_id, newest.version, newest.check_result)
    since = utcnow_naive() - timedelta(days=30)
    runs = (await db.execute(select(func.count(HubRun.id)).where(
        HubRun.tool_id == tool_id, HubRun.caller_org_id != org.id, HubRun.version > 0,
        HubRun.started_at >= since))).scalar() or 0
    listing = (await listings_of(db, [tool_id])).get(tool_id)
    app = await hub_apps.of_tool(db, org_id=org.id, tool_id=tool_id)
    return {"tool_id": tool_id, "version": newest.version, "status": newest.status,
            "live_version": live.version if live else None, "health": h.state,
            "listing": listing.state if listing else None, "runs_30d": int(runs),
            "price_label": price_label(newest.manifest), "share_url": f"/hub/{tool_id}",
            "call": f"treg call {tool_id} '{{...}}'",
            "app": hub_apps.view(app, org.slug) if app else None}


async def warnings(db: AsyncSession, org: Org, draft: dict[str, Any], access_of) -> list[dict[str, Any]]:
    """Before a test run: each catalog tool in `uses` this team cannot call (why, and the fix), and
    a balance under what one run of those tools costs. `access_of(endpoint_id)` is the catalog's
    access answer as the maker. Commits before asking it."""
    manifest = draft.get("manifest") if isinstance(draft.get("manifest"), dict) else {}
    cat = catalog_store.load()
    ids = [u for u in (manifest.get("uses") or []) if isinstance(u, str) and u in cat.by_id]
    balance = int((await db.execute(select(Org.balance_micro).where(Org.id == org.id))).scalar() or 0)
    await db.commit()
    out: list[dict[str, Any]] = []
    for eid, a in zip(ids, await asyncio.gather(*(access_of(i) for i in ids))):
        if a is not None and not a["callable"]:
            out.append({"kind": "access", "id": eid, "why_not": a.get("why_not"), "fix": a.get("fix")})
    need = 0
    for eid in ids:
        ep = cat.by_id[eid]
        usd = (cat.cost_view(ep.get("cost"), ep.get("provider")) or {}).get("usd")
        need += int(round(float(usd) * 1_000_000)) if usd else 0
    if need and balance < need:
        out.append({"kind": "balance", "balance_micro": balance, "need_micro": need})
    return out
