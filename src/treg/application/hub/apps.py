"""A hub tool's web page, the maker's side (docs/context/architecture/hub-apps.md).

The only writer of `HubApp`. An app belongs to one live hub tool of the maker's team and lives at
`/apps/<team slug>/<name>`. Turning it off keeps the row; the password, when set, guards the page
and the tool only while the app is on. Functions here never commit: the route does.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import get_settings
from ...domain.hub import ManifestError
from ...domain.hub.apps import (PASSWORD_MAX, default_app_name, hash_password, validate_app_name, validate_password,
                                verify_password)
from ...infra import kv
from ...models import HubApp, HubRun, HubTool, Org
from ...timeutil import utcnow_naive
from . import enabled_for as hub_enabled_for
from . import split_id, tool_for


def enabled_for(org_slug: str | None, email: str | None = None) -> bool:
    """The hub is on for this reader AND apps are on."""
    return bool(get_settings().hub_apps_enabled) and hub_enabled_for(org_slug, email)


def locked(app: HubApp | None) -> bool:
    """The password guards the page and the tool only while the app is on."""
    return app is not None and bool(app.enabled) and bool(app.password_hash)


def url(team_slug: str, name: str) -> str:
    return f"{get_settings().public_url.rstrip('/')}/apps/{team_slug}/{name}"


def view(app: HubApp | None, team_slug: str) -> dict[str, Any]:
    """The maker's view. Never the hash."""
    if app is None:
        return {"enabled": False, "name": None, "url": None, "password": False, "locked": False}
    return {"tool_id": app.tool_id, "enabled": bool(app.enabled), "name": app.name,
            "url": url(team_slug, app.name), "password": bool(app.password_hash), "locked": locked(app)}


async def of_tool(db: AsyncSession, *, org_id: int, tool_id: str) -> HubApp | None:
    app = await db.get(HubApp, tool_id)
    return app if app is not None and app.org_id == org_id else None


async def _has_live_version(db: AsyncSession, *, org_id: int, tool_id: str) -> bool:
    return (await db.execute(select(HubTool.id).where(
        HubTool.tool_id == tool_id, HubTool.org_id == org_id, HubTool.status == "live").limit(1))
    ).scalar_one_or_none() is not None


async def enable(db: AsyncSession, *, org: Org, tool_id: str, name: str | None, maker_email: str) -> HubApp | None:
    """Turn the app on, creating it on first use, or change its name. None when the team has no live
    version of the tool. A name another app of the team holds is refused."""
    if not await _has_live_version(db, org_id=org.id, tool_id=tool_id):
        return None
    app = await of_tool(db, org_id=org.id, tool_id=tool_id)
    new_name = validate_app_name(name) if name is not None else (app.name if app else validate_app_name(default_app_name(tool_id)))
    taken = (await db.execute(select(HubApp.tool_id).where(
        HubApp.org_id == org.id, HubApp.name == new_name, HubApp.tool_id != tool_id).limit(1))).scalar_one_or_none()
    if taken is not None:
        raise ManifestError("name", f"your team's app for {taken} already uses {new_name!r}; pick another name")
    now = utcnow_naive()
    if app is None:
        app = HubApp(tool_id=tool_id, org_id=org.id, name=new_name, enabled=True, created_by=maker_email,
                     created_at=now, updated_at=now)
        db.add(app)
    else:
        if new_name != app.name:
            # remember the old name, so links to it redirect (`moved`); a name taken back leaves the list
            app.old_names = [app.name, *[n for n in (app.old_names or []) if n not in (new_name, app.name)]][:MAX_OLD_NAMES]
        app.name, app.enabled, app.updated_at = new_name, True, now
    await db.flush()
    return app


async def moved(db: AsyncSession, team_slug: str, name: str) -> str | None:
    """Where an app moved: the path of the app a link to `/apps/<team>/<name>` meant, when the team
    has since changed its slug (`Org.previous_slug`) or the app its name (`HubApp.old_names`). None
    when the path is current or meant nothing. A current name always wins over an old one."""
    org = (await db.execute(select(Org).where(Org.slug == team_slug))).scalars().first()
    if org is None:
        org = (await db.execute(select(Org).where(Org.previous_slug == team_slug))).scalars().first()
    if org is None:
        return None
    apps = (await db.execute(select(HubApp).where(HubApp.org_id == org.id))).scalars().all()
    current = next((a for a in apps if a.name == name), None)
    if current is not None:
        return f"/apps/{org.slug}/{name}" if org.slug != team_slug else None
    old = next((a for a in apps if name in (a.old_names or [])), None)
    return f"/apps/{org.slug}/{old.name}" if old is not None and old.enabled else None


async def disable(db: AsyncSession, *, org_id: int, tool_id: str) -> HubApp | None:
    """Take the page down and lift the lock; the row, its name and its password stay."""
    app = await of_tool(db, org_id=org_id, tool_id=tool_id)
    if app is not None:
        app.enabled, app.updated_at = False, utcnow_naive()
        await db.flush()
    return app


async def set_password(db: AsyncSession, *, org_id: int, tool_id: str, password: str | None) -> HubApp | None:
    """Set (validated, hashed) or clear the password. Either way every unlock remembered under the
    old one stops working. None when the tool has no app yet."""
    app = await of_tool(db, org_id=org_id, tool_id=tool_id)
    if app is None:
        return None
    app.password_hash = hash_password(validate_password(password)) if password is not None else None
    app.lock_version += 1
    app.updated_at = utcnow_naive()
    await db.flush()
    return app


# ---------------------------------------------------------------------------------------------
# The visitor's side: find the app, unlock it, run it, read your own runs.

UNLOCK_TTL_S = 8 * 3600
MAX_OLD_NAMES = 10           # earlier names an app answers with a redirect
# Password tries, counted before the hash is checked: per (app, client) and per app, per window.
TRIES_PER_CLIENT = 10
TRIES_PER_APP = 200
TRIES_WINDOW_S = 300


@dataclass
class Found:
    app: HubApp
    tool: HubTool
    team_slug: str


async def by_path(db: AsyncSession, team_slug: str, name: str, *, reader_slug: str | None = None,
                  reader_email: str | None = None) -> Found | None:
    """The app at `/apps/<team>/<name>` and the version it runs, or None: no such app, turned off,
    no live version, or a tool treg's review rejected (gone dark for everyone but its maker)."""
    org = (await db.execute(select(Org).where(Org.slug == team_slug))).scalars().first()
    if org is None:
        return None
    app = (await db.execute(select(HubApp).where(HubApp.org_id == org.id, HubApp.name == name))).scalars().first()
    if app is None or not app.enabled:
        return None
    caller_org_id = None
    if reader_slug:
        reader_org = (await db.execute(select(Org.id).where(Org.slug == reader_slug))).scalar_one_or_none()
        caller_org_id = reader_org
    tool = await tool_for(db, app.tool_id, caller_org_id=caller_org_id, caller_slug=reader_slug, caller_email=reader_email)
    if tool is None:
        return None
    return Found(app=app, tool=tool, team_slug=org.slug)


_EPHEMERAL = secrets.token_bytes(32)


def _key() -> bytes:
    s = get_settings()
    base = (s.session_secret or s.secret_key or "").encode() or _EPHEMERAL
    return hmac.new(base, b"treg hub app unlock", hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def cookie_name(app: HubApp) -> str:
    return "treg_app_" + hashlib.sha256(app.tool_id.encode()).hexdigest()[:16]


def make_unlock(app: HubApp) -> str:
    """A signed, expiring proof that this browser typed the app's CURRENT password."""
    raw = json.dumps({"t": app.tool_id, "v": app.lock_version, "exp": int(time.time()) + UNLOCK_TTL_S},
                     separators=(",", ":")).encode()
    return f"{_b64(raw)}.{_b64(hmac.new(_key(), raw, hashlib.sha256).digest())}"


def read_unlock(app: HubApp, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    try:
        p, sig = token.split(".", 1)
        raw = _unb64(p)
        if not hmac.compare_digest(_unb64(sig), hmac.new(_key(), raw, hashlib.sha256).digest()):
            return False
        data = json.loads(raw)
        return (data.get("t") == app.tool_id and data.get("v") == app.lock_version
                and int(data.get("exp", 0)) > time.time())
    except (ValueError, TypeError):
        return False


# Passwords this process has verified, as (tool, password version, a digest of the password) and
# when the memory ends. A caller sending the right password on every call is checked by the slow
# hash once, not each time, and never spends a try: only an unremembered password counts.
_VERIFIED: dict[tuple[str, int, str], float] = {}
VERIFIED_TTL_S = 600
VERIFIED_MAX = 10_000


def _verified_key(app: HubApp, password: str) -> tuple[str, int, str]:
    """A memory key for a password, never the password: a light scrypt (about a millisecond) salted
    with the server's secret and the tool, so even this process's memory holds nothing a fast hash
    could reverse."""
    digest = hashlib.scrypt(password.encode(), salt=_key() + app.tool_id.encode(), n=2**10, r=8, p=1, dklen=32).hex()
    return (app.tool_id, int(app.lock_version), digest)


async def try_password(app: HubApp, password: str | None, client: str) -> str:
    """`ok`, `wrong` or `busy` (too many tries). A password this process verified lately passes
    without counting; any other try counts, against this client and against the app, before the
    hash is checked (so guessing is bounded and the hash cannot be made to run at will). The hash
    runs off the event loop."""
    if not isinstance(password, str) or not password or len(password) > PASSWORD_MAX:
        return "wrong"
    key = _verified_key(app, password)
    now = time.monotonic()
    if _VERIFIED.get(key, 0) > now:
        return "ok"
    if not await kv.store().take(f"hubapp:pw:{app.tool_id}:{client}", TRIES_PER_CLIENT, TRIES_WINDOW_S):
        return "busy"
    if not await kv.store().take(f"hubapp:pw:{app.tool_id}", TRIES_PER_APP, TRIES_WINDOW_S):
        return "busy"
    if not await asyncio.to_thread(verify_password, password, app.password_hash):
        return "wrong"
    if len(_VERIFIED) >= VERIFIED_MAX:
        for k in [k for k, exp in _VERIFIED.items() if exp <= now] or list(_VERIFIED)[: VERIFIED_MAX // 10]:
            _VERIFIED.pop(k, None)
    _VERIFIED[key] = now + VERIFIED_TTL_S
    return "ok"


async def app_of(db: AsyncSession, tool: HubTool) -> HubApp | None:
    return await db.get(HubApp, tool.tool_id)


async def call_lock(app: HubApp | None, tool: HubTool, *, caller_org_id: int, password: str | None,
                    client: str, unlocked: bool = False) -> str | None:
    """The lock on `/call/` of a hub tool: None to go on, else the refusal kind
    (`hub_tool_locked`, `hub_tool_password_busy`). The maker's own team never needs the password;
    an app run that already checked its unlock passes `unlocked`. Takes the app row already read
    (`app_of`), so the caller can release its database connection before the tries and the hash."""
    if unlocked or tool.org_id == caller_org_id or not locked(app):
        return None
    if not password:
        return "hub_tool_locked"
    verdict = await try_password(app, password, client)
    return None if verdict == "ok" else ("hub_tool_password_busy" if verdict == "busy" else "hub_tool_locked")


async def contract(db: AsyncSession, found: Found) -> dict[str, Any]:
    """What the app page renders: the public contract of the version it runs. Never the script,
    the maker's tools, a key, or the check's sample inputs."""
    from ...domain.hub import PAY_NOTE, fees_label, price_label, stored_pricing
    from . import price_ranges, with_range
    from .health import health_of
    t, m = found.tool, found.tool.manifest
    out = m.get("output", {})
    fields = out.get("fields") if isinstance(out, dict) and "fields" in out else list(out)
    rng = (await price_ranges(db, {t.tool_id: m})).get(t.tool_id)
    pricing = stored_pricing({"price_usd": t.price_micro / 1_000_000, **m})
    if pricing["mode"] == "charge":
        seller = "seller " + price_label(m)
    else:
        seller = f"seller ${pricing['price_usd']:.6g} a run" if pricing["price_usd"] else "no seller price"
    if pricing["mode"] == "charge" and not pricing.get("max_price_usd"):
        seller = "no seller price"
    inputs = {k: {kk: v.get(kk) for kk in ("type", "default", "example", "note", "min", "max", "secret") if kk in v}
              for k, v in (m.get("inputs") or {}).items()}
    health = (await health_of(db, t.tool_id, t.version, t.check_result)).state
    return {"tool_id": t.tool_id, "version": t.version, "name": t.name, "summary": t.summary,
            "readme": t.readme, "maker": found.team_slug, "app": found.app.name,
            "inputs": inputs, "output": fields, "writes": bool(t.writes),
            "price": {**with_range(m, rng), "seller": seller, "fees": fees_label(m, rng).strip(" +"),
                      "note": PAY_NOTE,
                      # nothing to pay at all: no seller price and no catalog step behind it
                      "free": seller == "no seller price" and not any("." in u for u in m.get("uses", []))},
            "limits": {"wall_s": (m.get("limits") or {}).get("wall_s", 120)},
            "health": health, "share_page": f"{get_settings().public_url.rstrip('/')}/hub/{t.tool_id}",
            "password": locked(found.app)}


def _run_row(r: HubRun, *, full: bool) -> dict[str, Any]:
    out = {"run_id": r.run_id, "version": r.version, "status": r.status,
           "started_at": r.started_at.isoformat(), "duration_ms": r.duration_ms, "steps": r.steps,
           "cost_micro": r.cost_micro + r.price_micro}
    if full:
        out.update({"inputs": r.inputs, "output": r.output,
                    "error": ({k: v for k, v in r.error.items() if k in ("error", "step", "status", "message")}
                              if r.error else None)})
    return out


async def runs_of(db: AsyncSession, *, tool_id: str, org_id: int, email: str, limit: int = 50) -> list[dict[str, Any]]:
    """The visitor's OWN runs of this tool (their team AND their sign-in), newest first. HubRun keeps
    30 days, so that is how far history reaches."""
    rows = (await db.execute(select(HubRun).where(
        HubRun.tool_id == split_id(tool_id)[0], HubRun.caller_org_id == org_id, HubRun.caller_email == email,
        HubRun.version > 0).order_by(HubRun.started_at.desc()).limit(limit))).scalars().all()
    return [_run_row(r, full=False) for r in rows]


async def run_of(db: AsyncSession, *, tool_id: str, run_id: str, org_id: int, email: str) -> dict[str, Any] | None:
    r = (await db.execute(select(HubRun).where(HubRun.run_id == run_id))).scalars().first()
    if r is None or r.tool_id != tool_id or r.caller_org_id != org_id or r.caller_email != email:
        return None
    return _run_row(r, full=True)
