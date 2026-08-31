"""Framework-neutral authorization for a resolved proxied call."""

from ... import sandbox as demo_sandbox
from ...infra.db import session_maker
from ...domain.governance import access as access_policy
from ...domain.governance import publicdemo as publicdemo_policy
from ...domain.governance import usage as usage_policy
from ...domain.identity.access import Caller, _role_at_least
from ...models import Tool
from .types import AuthorizationFailed


async def authorize_call(
    *, caller: Caller, tool: Tool, upstream_url: str, method: str, client_ip: str,
) -> None:
    """Run the no-money gates in their frozen ACL, deny, member-cap, public-demo order."""
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
            await access_policy.enforce_deny(
                caller, upstream_url, method, db, tool.project_id)
        except access_policy.AccessPolicyError as exc:
            raise AuthorizationFailed(
                "policy_denied", status_code=403, detail=exc.detail) from exc
        try:  # per-user daily cap (skips sandbox + unmetered members)
            await usage_policy.enforce_daily_cap(
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
