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
from ...domain.hub import ManifestError, validate, validate_check, validate_readme
from ...models import HubTool, Org, Tool

HUB_ID_MIN_PARTS = 2


def enabled() -> bool:
    return bool(get_settings().hub_enabled)


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


async def tool_for(db: AsyncSession, rest: str, *, live_only: bool = True) -> HubTool | None:
    """The version that serves `rest`: the newest `live` one, or `@N` pinned. A pinned version may
    also be the one UNDER CHECK (the check run pins it: HUB-DECISIONS round 2 q10), and a pinned
    old version stays callable for OLD_VERSION_DAYS after a newer live one exists (round 4 q8)."""
    if not enabled() or not is_hub_id_shape(rest):
        return None
    tool_id, pin = split_id(rest)
    if pin is None:
        q = (select(HubTool).where(HubTool.tool_id == tool_id, HubTool.status == "live")
             .order_by(HubTool.version.desc()).limit(1))
        return (await db.execute(q)).scalars().first()
    row = (await db.execute(select(HubTool).where(HubTool.tool_id == tool_id, HubTool.version == pin))).scalars().first()
    if row is None:
        return None
    if row.status == "checking":
        return row
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


MAX_DATA_BYTES = 50_000_000


def validate_data(data: Any) -> str | None:
    """The uploaded CSV: text, at most 50 MB, a header row and at least one data row, parseable."""
    import csv
    import io
    if data is None or data == "":
        return None
    if not isinstance(data, str):
        raise ManifestError("data", "the CSV as text (the contents of data.csv)")
    if len(data.encode("utf-8")) > MAX_DATA_BYTES:
        raise ManifestError("data", "at most 50 MB")
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
    v = validate(manifest, catalog_ids=set(catalog_store.load().by_id),
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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://treg.internal",
                                 headers=headers, timeout=200.0) as client:
        r = await client.post(f"/call/{row.tool_id}@{row.version}", json=row.check.get("inputs", {}))
    from .health import verdict_from
    try:
        body = r.json()
    except ValueError:
        body = {"text": r.text[:600]}
    verdict = verdict_from(row, r.status_code, body, dict(r.headers))
    row.status = "live" if verdict["status"] == "passed" else "failed"
    row.check_result = verdict
    db.add(row)
    return verdict


def view(row: HubTool) -> dict[str, Any]:
    """The maker-facing shape of one version. The script is the maker's own; it is returned to
    the maker here and to nobody else (the public page of phase 7 hides it)."""
    return {
        "tool_id": row.tool_id, "version": row.version, "kind": row.kind, "status": row.status,
        "summary": row.summary, "writes": row.writes, "price_usd": row.price_micro / 1_000_000,
        "uses": row.manifest.get("uses", []), "inputs": row.manifest.get("inputs", {}),
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
    v = validate(manifest, catalog_ids=set(catalog_store.load().by_id), own_tools=own_tools, hub_ids=hub_ids)
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
    every trace stamps the price it paid. Returns the row, or None when the team has no such
    live tool. Does not commit."""
    from ...domain.hub import manifest as hub_manifest
    micro = hub_manifest._validate_price(price_usd)
    row = (await db.execute(select(HubTool).where(
        HubTool.tool_id == tool_id, HubTool.org_id == org_id, HubTool.status == "live")
        .order_by(HubTool.version.desc()).limit(1))).scalars().first()
    if row is None:
        return None
    row.price_micro = micro
    row.manifest = {**row.manifest, "price_usd": micro / 1_000_000}
    db.add(row)
    return row
