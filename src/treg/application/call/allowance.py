"""Each team's daily share of an allowance a provider meters on treg's app.

A connection made through treg's own OAuth client, or carrying treg's own second credential, spends
an allowance every team's connection shares (`OAuthProvider.shared_allowance`). Where the operator
gives the service a share (`TREG_SHARED_ALLOWANCE_SHARES`), each call spends its units from the
team's share for the provider's day immediately before it is sent, and is refused once that is
spent. A connection on the team's own client or credential is never counted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import cache
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ... import oauth_providers
from ...config import get_settings
from ...domain.capacity import events as capacity_events
from ...infra import kv
from .types import CallFailure

SHARE_NS = "treg:allowance-share:v1:"
SHARE_TTL_S = 2 * 86400
# Google's daily quotas reset at midnight Pacific time.
# https://developers.google.com/youtube/v3/getting-started#quota
_PROVIDER_DAY = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class Draw:
    """One call's claim on a shared allowance: the provider and what the call costs in its units."""
    service: str
    units: int


@cache
def _platform_extra_providers() -> dict[str, oauth_providers.OAuthProvider]:
    return {provider.extra_credential_setting: provider
            for provider in oauth_providers.REGISTRY.values()
            if provider.shared_allowance and provider.shared_allowance.drawn_by == "platform_extra"}


def draw_for(tool, secrets: dict, method: str, upstream_url: str) -> Draw | None:
    """The shared allowance this call spends, or None. Sync and I/O-free."""
    if not get_settings().shared_allowance_shares:
        return None
    call = f"{method.upper()} {urlsplit(upstream_url).path}"
    for secret in secrets.values():
        provider = oauth_providers.get(secret.provider) if secret.provider else None
        if (secret.kind == "oauth" and provider is not None and provider.shared_allowance
                and provider.shared_allowance.drawn_by == "app"):
            return Draw(provider.service, provider.shared_allowance.units_for(call))
    extra = _platform_extra_providers()
    for binding in tool.bindings:
        provider = extra.get(binding.get("platform_setting") or "")
        if provider is not None:
            return Draw(provider.service, provider.shared_allowance.units_for(call))
    return None


async def spend_share(draw: Draw | None, org_id: int | None) -> None:
    """Immediately before a drawing call is sent: spend its units from the team's share of the
    provider's day, or refuse it once that is spent. A share the store cannot answer admits."""
    if draw is None or not org_id:
        return
    share = get_settings().allowance_share(draw.service, org_id)
    if share is None:
        return
    now = datetime.now(timezone.utc)
    day = now.astimezone(_PROVIDER_DAY).date()
    store = kv.store()
    taken = await store.take_units(f"{SHARE_NS}{draw.service}:{org_id}:{day.isoformat()}",
                                   draw.units, share, SHARE_TTL_S)
    if taken is None:
        return
    admitted, used = taken
    if admitted:
        return
    capacity_events.emit("allowance_share_refused", provider=draw.service, org_id=org_id,
                         share=share, used=used, units=draw.units)
    resets = datetime.combine(day + timedelta(days=1), datetime.min.time(), _PROVIDER_DAY)
    raise share_spent(draw, share, used, resets.astimezone(timezone.utc), now)


def share_spent(draw: Draw, share: int, used: int, resets: datetime,
                now: datetime) -> CallFailure:
    provider = oauth_providers.get(draw.service)
    name = provider.display_name if provider else draw.service
    own = ("developer token" if provider and provider.shared_allowance
           and provider.shared_allowance.drawn_by == "platform_extra" else "OAuth client")
    resets_at = resets.strftime("%Y-%m-%dT%H:%M:%SZ")
    message = (f"Your team has used {used} of its {share} daily units of {name}'s allowance on "
               f"treg's app, which every team's connection shares; this call needs {draw.units}. "
               f"It resets at {resets_at}. Not charged. A connection made with your own {own} "
               f"has no team share.")
    if draw.units > share:
        message = (f"This {name} call needs {draw.units} units, more than your team's whole daily "
                   f"share of {share} on treg's app; it can never fit. Not charged. A connection "
                   f"made with your own {own} has no team share.")
    return CallFailure("shared_allowance_share", status_code=429, detail={
        "error": "shared_allowance_share_exceeded", "provider": draw.service, "share": share,
        "used": used, "units": draw.units, "resets_at": resets_at, "message": message,
    }, retry_after_s=None if draw.units > share else max(1, int((resets - now).total_seconds())))
