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


async def tool_for(db: AsyncSession, rest: str, *, live_only: bool = True) -> HubTool | None:
    """The version that serves `rest`: a pinned version when the id carries `@N`, else the newest.
    `live_only` keeps unchecked and retired versions off the call road."""
    if not enabled() or not is_hub_id_shape(rest):
        return None
    tool_id, pin = split_id(rest)
    q = select(HubTool).where(HubTool.tool_id == tool_id)
    if pin is not None:
        q = q.where(HubTool.version == pin)
    if live_only:
        q = q.where(HubTool.status == "live")
    q = q.order_by(HubTool.version.desc()).limit(1)
    return (await db.execute(q)).scalars().first()


@dataclass(frozen=True)
class Published:
    tool_id: str
    version: int
    status: str
    kind: str


async def publish(
    db: AsyncSession, *, org: Org, maker_email: str,
    manifest: Any, script: str | None, check: Any, readme: Any,
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

    tool_id = f"{org.slug}.{v.name}"
    newest = (await db.execute(
        select(HubTool.version).where(HubTool.tool_id == tool_id)
        .order_by(HubTool.version.desc()).limit(1))).scalar_one_or_none()
    version = 1 if newest is None else newest + 1
    row = HubTool(
        org_id=org.id, tool_id=tool_id, name=v.name, version=version, kind=v.kind,
        status="unchecked", summary=v.summary, writes=v.writes, price_micro=v.price_micro,
        manifest={**v.manifest, "version": version}, script=script if v.kind == "script" else None,
        check=check_v, readme=readme_v, created_by=maker_email,
    )
    db.add(row)
    await db.flush()
    return Published(tool_id=tool_id, version=version, status=row.status, kind=v.kind)


def view(row: HubTool) -> dict[str, Any]:
    """The maker-facing shape of one version. The script is the maker's own; it is returned to
    the maker here and to nobody else (the public page of phase 7 hides it)."""
    return {
        "tool_id": row.tool_id, "version": row.version, "kind": row.kind, "status": row.status,
        "summary": row.summary, "writes": row.writes, "price_usd": row.price_micro / 1_000_000,
        "uses": row.manifest.get("uses", []), "inputs": row.manifest.get("inputs", {}),
        "output": row.manifest.get("output", {}), "limits": row.manifest.get("limits", {}),
        "created_by": row.created_by, "created_at": row.created_at.isoformat(),
    }
