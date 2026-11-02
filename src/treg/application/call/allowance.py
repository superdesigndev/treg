"""Allowances a provider meters on treg's app rather than on the connected account.

A connection made through treg's own OAuth client, or carrying treg's own second credential, spends
an allowance every team's connection shares (`OAuthProvider.shared_allowance`). When the provider
says it is spent, the call path's breaker (domain/capacity/marks.py) locks it under its own key, so
the next connection call is refused before it reaches the provider, and one probe per process per
minute finds out when it serves again. `TREG_SHARED_ALLOWANCE_MODE` decides whether a lock refuses
(`enforce`), is only logged (`observe`), or nothing is tracked at all (`off`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
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
# A daily allowance resets within a day; the stored row outlives no longer than that either.
MAX_LOCK = timedelta(seconds=capacity_marks.LOCK_TTL_S)


@dataclass(frozen=True)
class Draw:
    """One call's claim on a shared allowance: the provider, the allowance's key (one per treg
    account of that credential), and the keys of the smaller scopes this call also falls in."""
    service: str
    key: str
    scope_keys: tuple[str, ...]
    probe_lock_id: str | None = None

    @property
    def keys(self) -> tuple[str, ...]:
        return (self.key, *self.scope_keys)


def mode() -> str:
    return get_settings().shared_allowance_mode


def draw_for(tool, secrets: dict, upstream_url: str) -> Draw | None:
    """The shared allowance this call spends, or None. Sync and I/O-free."""
    if mode() == "off":
        return None
    for secret in secrets.values():
        provider = oauth_providers.get(secret.provider) if secret.provider else None
        if (secret.kind == "oauth" and provider is not None and provider.shared_allowance
                and provider.shared_allowance.drawn_by == "app"):
            return _draw(provider, None, upstream_url)
    for binding in tool.bindings:
        setting = binding.get("platform_setting")
        if not setting:
            continue
        for provider in oauth_providers.REGISTRY.values():
            if (provider.shared_allowance and provider.shared_allowance.drawn_by == "platform_extra"
                    and provider.extra_credential_setting == setting):
                return _draw(provider, binding.get("account"), upstream_url)
    return None


def _draw(provider, account: str | None, upstream_url: str) -> Draw:
    key = f"{KEY_PREFIX}{provider.service}" + (f"#{account}" if account else "")
    path = urlsplit(upstream_url).path
    scope_keys = tuple(f"{key}/{scope.name}" for scope in provider.shared_allowance.scopes
                       if re.search(scope.path_regex, path))
    return Draw(provider.service, key, scope_keys)


async def admit(draw: Draw | None) -> Draw | None:
    """Refuse a call whose allowance is locked (`enforce`), or let one through as the probe. Returns
    the draw to carry to `note_answer`, with the lock it probes. Reads the cached capacity view;
    call it with no database connection held."""
    if draw is None:
        return None
    await capacity_view.load()
    locked = [lock for key in draw.keys
              if (lock := capacity_view.lock(key)) is not None and lock.is_active()]
    if not locked:
        return draw
    lock = max(locked, key=lambda lock: lock.until)
    if capacity_marks.probe_due(lock.key):
        return Draw(draw.service, draw.key, draw.scope_keys, probe_lock_id=lock.lock_id)
    if mode() == "observe":
        capacity_events.emit("allowance_would_refuse", provider=draw.service, key=lock.key,
                             until=lock.until)
        return draw
    raise exhausted(draw.service, lock.until)


def exhausted(service: str, until) -> ResolutionFailed:
    provider = oauth_providers.get(service)
    name = provider.display_name if provider else service
    resets = until.isoformat() + "Z" if until else None
    return ResolutionFailed("provider_capacity", status_code=503, detail={
        "error": "shared_allowance_exhausted", "provider": service, "resets_at": resets,
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
    """After the answer: a spent-allowance signature strikes the breaker on its allowance (or the
    scope it names); a 2xx clears pending strikes and the lock this call probed. Never raises."""
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
    signal = capacity_signatures.classify(draw.service, status, headers, body[:4096])
    if not capacity_signatures.is_exhausting(signal):
        return
    provider = oauth_providers.get(draw.service)
    text = body[:4096].decode("utf-8", "replace")
    key = next((f"{draw.key}/{scope.name}" for scope in provider.shared_allowance.scopes
                if re.search(scope.body_regex, text)), draw.key)
    lock = await capacity_marks.strike(
        key, endpoint_id=None, kind=signal.kind, resets_at=signal.resets_at, max_lock=MAX_LOCK,
        note=f"{signal.kind} signature on {draw.service}: {signal.detail[:80]}")
    capacity_view.invalidate()
    if lock is not None and lock.opened:
        capacity_events.emit("lock_opened", provider=draw.service, key=lock.key,
                             lock_id=lock.lock_id, strikes=lock.strikes,
                             first_signal_at=lock.first_signal_at, opened_at=lock.opened_at,
                             until=lock.until, shared_allowance=True)


async def _recover(draw: Draw) -> None:
    for key in draw.keys:
        lock = capacity_view.lock(key)
        if lock is None or (lock.is_active() and lock.lock_id != draw.probe_lock_id):
            continue
        if await capacity_marks.clear(key, lock_id=draw.probe_lock_id) and lock.is_active():
            capacity_view.invalidate()
            capacity_events.emit(
                "lock_cleared", provider=draw.service, key=key, lock_id=lock.lock_id, via="probe",
                locked_s=int((utcnow_naive() - (lock.opened_at or lock.first_signal_at))
                             .total_seconds()),
                shared_allowance=True)
