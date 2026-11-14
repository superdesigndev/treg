"""Framework-neutral authorization for a resolved proxied call."""

import logging
from datetime import date

from ... import oauth_providers
from ... import sandbox as demo_sandbox
from ...infra.db import session_maker
from ...domain.governance import access as access_policy
from ...domain.governance import publicdemo as publicdemo_policy
from ...domain.governance import usage as usage_policy
from ...domain.identity.access import Caller, _role_at_least
from ...models import Tool
from .types import AuthorizationFailed

log = logging.getLogger("treg.call")


async def authorize_call(
    *, caller: Caller, tool: Tool, upstream_url: str, method: str, client_ip: str,
) -> date | None:
    """Run the no-money gates in their frozen ACL, deny, member-cap, public-demo order. Returns
    the UTC day the member's call slot was taken on, or None when no slot was taken."""
    async with session_maker() as db:
        # Per-member tool + project ACL (NULL access = all; admins exempt).
        try:
            access_policy._require_tool_use(caller, tool)
        except access_policy.AccessPolicyError as exc:
            raise AuthorizationFailed(
                "tool_access_denied", status_code=403, detail=exc.detail) from exc
        # Policy deny is evaluated on the RESOLVED upstream, so it sees the real host/path/method
        # whichever shape the caller used. The relay never follows redirects, so a blocked host
        # cannot be reached through a 3xx bounce.
        try:
            for url in [upstream_url, *oauth_providers.legacy_aliases(upstream_url)]:
                await access_policy.enforce_deny(caller, url, method, db, tool.project_id)
        except access_policy.AccessPolicyError as exc:
            raise AuthorizationFailed(
                "policy_denied", status_code=403, detail=exc.detail) from exc
        try:  # per-user daily cap (skips sandbox + unmetered members)
            slot_day = await usage_policy.enforce_daily_cap(
                caller, db, sandbox=demo_sandbox.is_sandbox(caller.org))
        except usage_policy.UsagePolicyError as exc:
            raise AuthorizationFailed(
                "daily_cap_reached", status_code=429, detail=exc.detail) from exc
        if caller.org.public_demo and not _role_at_least(caller.role, "admin"):
            # A shared token must be metered by client IP, not by its one synthetic user.
            try:
                await publicdemo_policy.enforce_public_demo_ip_cap(client_ip, db)
            except publicdemo_policy.PublicDemoLimitError as exc:
                await db.commit()
                raise AuthorizationFailed(
                    "public_demo_rate_limited", status_code=429, detail=exc.detail) from exc
        await db.commit()
    return slot_day


async def return_daily_slot(caller: Caller, slot_day: date | None) -> None:
    """Undo `authorize_call`'s call-count slot (taken on `slot_day`) for a call refused by the
    member's spend limit, so a member stopped by dollars does not also burn their call count.
    Best-effort."""
    if slot_day is None:
        return
    try:
        async with session_maker() as db:
            await usage_policy.return_daily_slot(db, caller.membership.id, slot_day)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - the refusal stands either way
        log.warning("daily-cap slot return failed for membership %s: %s", caller.membership.id, exc)


async def enforce_public_demo_limit(client_ip: str) -> None:
    """Persist one sandbox live-wire rate hit and translate its policy refusal."""
    async with session_maker() as db:
        try:
            await publicdemo_policy.enforce_public_demo_ip_cap(client_ip, db)
        except publicdemo_policy.PublicDemoLimitError as exc:
            await db.commit()
            raise AuthorizationFailed(
                "public_demo_rate_limited", status_code=429, detail=exc.detail) from exc
        await db.commit()
