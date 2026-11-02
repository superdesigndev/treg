"""Allowances a provider meters on treg's app rather than on the connected account.

A connection made through treg's own OAuth client, or carrying treg's own second credential, spends
an allowance every team's connection shares (`OAuthProvider.shared_allowance`). When the provider
says it is spent, the call path's breaker (domain/capacity/marks.py) locks it under its own key, so
the next connection call that is not answered from the archive is refused before it reaches the
provider, and one probe per process per minute finds out when it serves again.
`TREG_SHARED_ALLOWANCE_MODE` decides whether a lock refuses (`enforce`), is only logged
(`observe`), or nothing is tracked at all (`off`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import cache
from urllib.parse import urlsplit

from ... import oauth_providers
from ...config import get_settings
from ...domain.capacity import events as capacity_events
from ...domain.capacity import marks as capacity_marks
from ...domain.capacity import signatures as capacity_signatures
from ...domain.capacity.view import view as capacity_view
from ...timeutil import utcnow_naive
from .types import ResolutionFailed

KEY_PREFIX = "allowance:"
_SCAN_BYTES = 16 * 1024
MAX_LOCK = timedelta(seconds=capacity_marks.LOCK_TTL_S)


@dataclass(frozen=True)
class Draw:
    """One call's claim on a shared allowance: the provider, the allowance's key (one per treg
    account of that credential), and the keys of the smaller scopes this call falls in. `admit`
    fills in the lock this call probes, or the reset it is refused until."""
    service: str
    key: str
    scope_keys: tuple[str, ...]
    probe_lock_id: str | None = None
    refused_until: datetime | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.key, *self.scope_keys)

    @property
    def refused(self) -> bool:
        return self.refused_until is not None


def mode() -> str:
    return get_settings().shared_allowance_mode


@cache
def _platform_extra_providers() -> dict[str, oauth_providers.OAuthProvider]:
    return {provider.extra_credential_setting: provider
            for provider in oauth_providers.REGISTRY.values()
            if provider.shared_allowance and provider.shared_allowance.drawn_by == "platform_extra"}


def draw_for(tool, secrets: dict, method: str, upstream_url: str) -> Draw | None:
    """The shared allowance this call spends, or None. Sync and I/O-free."""
    if mode() == "off":
        return None
    for secret in secrets.values():
        provider = oauth_providers.get(secret.provider) if secret.provider else None
        if (secret.kind == "oauth" and provider is not None and provider.shared_allowance
                and provider.shared_allowance.drawn_by == "app"):
            return _draw(provider, None, method, upstream_url)
    extra = _platform_extra_providers()
    for binding in tool.bindings:
        provider = extra.get(binding.get("platform_setting") or "")
        if provider is not None:
            return _draw(provider, binding.get("account"), method, upstream_url)
    return None


def _draw(provider, account: str | None, method: str, upstream_url: str) -> Draw:
    key = f"{KEY_PREFIX}{provider.service}" + (f"#{account}" if account else "")
    call = f"{method.upper()} {urlsplit(upstream_url).path}"
    scope_keys = tuple(f"{key}/{scope.name}" for scope in provider.shared_allowance.scopes
                       if re.search(scope.call_regex, call))
    return Draw(provider.service, key, scope_keys)


async def admit(draw: Draw | None) -> Draw | None:
    """Decide a drawing call against the locks on its allowance: let it through, let it through as
    a probe, or mark it refused until the lock that lasts longest. A call probes only a lock its
    answer can prove: the whole allowance's by a call in no scope, a scope's by a call in it once
    the whole allowance is open. Reads the cached capacity view; call it with no database
    connection held. A view that cannot be read refuses nothing."""
    if draw is None:
        return None
    try:
        await capacity_view.load()
    except Exception:  # noqa: BLE001 - the guard is advisory; an unread view lets the call through
        return draw
    locked = [lock for key in draw.keys
              if (lock := capacity_view.lock(key)) is not None and lock.is_active()]
    if not locked:
        return draw
    whole = next((lock for lock in locked if lock.key == draw.key), None)
    provable = whole if whole is not None and not draw.scope_keys else None
    if whole is None:
        provable = max(locked, key=lambda lock: lock.until)
    if provable is not None and capacity_marks.probe_due(provable.key):
        return replace(draw, probe_lock_id=provable.lock_id)
    return replace(draw, refused_until=max(lock.until for lock in locked))


def refuse(draw: Draw) -> None:
    """At the point a refused call would leave: raise the refusal (`enforce`), or log it (`observe`)."""
    if mode() == "observe":
        capacity_events.emit("allowance_would_refuse", provider=draw.service, key=draw.key,
                             until=draw.refused_until)
        return
    raise exhausted(draw)


def exhausted(draw: Draw) -> ResolutionFailed:
    provider = oauth_providers.get(draw.service)
    name = provider.display_name if provider else draw.service
    resets = draw.refused_until.isoformat() + "Z" if draw.refused_until else None
    return ResolutionFailed("provider_capacity", status_code=503, detail={
        "error": "shared_allowance_exhausted", "provider": draw.service, "resets_at": resets,
        "message": (
            f"{name} has used up today's allowance on treg's app, which every team's connection "
            f"shares; it serves again at {resets or 'its next reset'}. Not charged. A connection "
            f"made with your own {_own_credential(provider)} draws on its own allowance instead."),
    })


def _own_credential(provider) -> str:
    if provider is not None and provider.shared_allowance is not None \
            and provider.shared_allowance.drawn_by == "platform_extra":
        return provider.extra_credential_label.lower() or "credential"
    return "OAuth client"


async def note_answer(draw: Draw | None, status: int, headers, body: bytes) -> None:
    """After the answer: a spent-allowance signature strikes the breaker on the narrowest pool it
    proves spent (a scope its body names, else the call's own scope, else the whole allowance); a
    2xx clears pending strikes and the lock this call probed. Never raises."""
    if draw is None:
        return
    try:
        if status >= 400:
            await _strike(draw, status, headers, body)
        elif status < 300:
            await _recover(draw)
    except Exception:  # noqa: BLE001 - a mark is for the next caller, never this call's fate
        capacity_events.emit("allowance_mark_failed", provider=draw.service, status=status)


async def _strike(draw: Draw, status: int, headers, body: bytes) -> None:
    signal = capacity_signatures.classify_own(draw.service, status, headers, body[:_SCAN_BYTES])
    if not capacity_signatures.is_exhausting(signal):
        return
    provider = oauth_providers.get(draw.service)
    text = body[:_SCAN_BYTES].decode("utf-8", "replace")
    named = next((f"{draw.key}/{scope.name}" for scope in provider.shared_allowance.scopes
                  if scope.body_regex and re.search(scope.body_regex, text)), None)
    key = named or next(iter(draw.scope_keys), draw.key)
    lock = await capacity_marks.strike(
        key, endpoint_id=None, kind=signal.kind, resets_at=signal.resets_at, max_lock=MAX_LOCK,
        note=f"{signal.kind} signature on {draw.service}: {signal.detail[:80]}")
    capacity_view.invalidate()
    if lock is None:
        return
    where = {"provider": draw.service, "key": lock.key, "status": status, "shared_allowance": True}
    if lock.opened:
        capacity_events.emit("lock_opened", **where, lock_id=lock.lock_id, strikes=lock.strikes,
                             first_signal_at=lock.first_signal_at, opened_at=lock.opened_at,
                             until=lock.until)
    elif not lock.is_active():
        capacity_events.emit("strike", **where, strikes=lock.strikes)


async def _recover(draw: Draw) -> None:
    """A probe's 2xx proves every pool the call draws on serves, so it lifts each active lock among
    them; any 2xx clears pending strikes."""
    await capacity_view.load()
    for key in draw.keys:
        lock = capacity_view.lock(key)
        if lock is None or (lock.is_active() and draw.probe_lock_id is None):
            continue
        if await capacity_marks.clear(key, lock_id=lock.lock_id) and lock.is_active():
            capacity_view.invalidate()
            capacity_events.emit(
                "lock_cleared", provider=draw.service, key=key, lock_id=lock.lock_id, via="probe",
                locked_s=int((utcnow_naive() - (lock.opened_at or lock.first_signal_at))
                             .total_seconds()),
                shared_allowance=True)
