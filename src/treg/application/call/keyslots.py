"""Which treg-owned account serves one platform call.

A provider can sit behind several treg-owned accounts (`TREG_PLATFORM_ACCOUNTS`, one
`config.PlatformSlot` each). The rules, in order:

1. A call on something an account owns (an async task, its result, a managed resource) uses that
   account: the object exists nowhere else. An owner row without a slot predates pools and lives on
   the primary account.
2. A call carrying an Idempotency-Key uses the account the key hashes to, so a retry that reaches
   the provider again lands where the provider can recognise it. Adding or removing an account can
   move a key; a retry across that change is a new request to the provider.
3. Otherwise an account not out of capacity whose rate bucket would make the call wait least; ties
   rotate per provider. This one
   is chosen right before the relay, not during resolution, so the choice and the token it takes
   happen in one step.

The primary account's credentials are the deployment's own fields, so a call on it carries no
account and smooths in the provider's own bucket: a provider with one account behaves as before.
Capacity is per account: each further account has its own locks (`capacity_slot`); the sweep
reads only the primary. Sync and I/O-free: resolution calls `pinned` while it holds its session.
"""

from __future__ import annotations

import hashlib
import itertools
import random

from ... import oauth_providers
from ...config import PRIMARY_SLOT, get_settings, platform_setting_name
from ...domain.capacity.fair_share import fair_share
from ...domain.capacity.view import view as capacity_view
from ...infra.upstream.limiter import limiter
from .types import ResolutionFailed

# Per provider, so one provider's calls never skew another's. Each process starts at a random
# point: every web worker counting from 0 sent its first call on a provider to the primary, so at
# low traffic across several workers nearly every call landed there.
_rotation: dict[str, itertools.count] = {}


def check_accounts() -> None:
    """Refuse to start on an account the call path could not use: one behind a provider with no
    primary key (the primary is the serving switch, so the provider is off), a provider without a
    pasted platform key, or an account missing its provider's paired credential (pairing it with
    the primary's half is a pair the provider rejects). Every process that serves or polls platform
    calls runs this: the API at app creation, `treg-worker` before any command."""
    settings = get_settings()
    for service, accounts in settings.platform_extra_accounts().items():
        provider = oauth_providers.get(service)
        if provider is None or not provider.uses_pasted_secret:
            raise RuntimeError(f"TREG_PLATFORM_ACCOUNTS names {service!r}, which has no platform key")
        if not settings.platform_slots(service):
            raise RuntimeError(f"TREG_PLATFORM_ACCOUNTS adds {service} accounts, but "
                               f"TREG_{platform_setting_name(service).upper()} is not set")
        paired = provider.platform_extra_setting if provider.needs_extra_credential else ""
        for slot in accounts:
            if paired and not slot.value(paired):
                raise RuntimeError(f"TREG_PLATFORM_ACCOUNTS {service}/{slot.slot} must name {paired}")


def _labels(service: str) -> list[str]:
    """The provider's account labels, the primary first. A provider with no key at all answers
    `primary`, and the relay refuses the injection."""
    return [s.slot for s in get_settings().platform_slots(service)] or [PRIMARY_SLOT]


def pinned(service: str, *, owners: tuple[str | None, ...] = (), idempotency_key: str = "") -> str | None:
    """The account this call must use, or None when any account may serve it (`assign` picks one
    right before the relay). `owners` are the key slots of the rows the call acts on."""
    slots = _labels(service)
    if owners:
        wanted = {owner or PRIMARY_SLOT for owner in owners}
        if len(wanted) > 1:
            raise ResolutionFailed("platform_account_split", status_code=409, detail={
                "error": "platform_account_split",
                "message": (f"this call names {service} objects created on different treg accounts, "
                            "and no one account holds them all; nothing was charged"),
            })
        if not wanted <= set(slots):
            settings = get_settings()
            if settings.platform_slots(service) and service in settings.platform_extra_accounts():
                raise ResolutionFailed("platform_account_retired", status_code=410, detail={
                    "error": "platform_account_retired",
                    "message": (f"this {service} object was created on a treg account that can no "
                                "longer serve it; nothing was charged"),
                })
            # This process holds no further accounts for the provider at all, or not even its
            # primary: more likely an environment that lost them than an account an operator
            # retired, so it is not final (the async worker retries rather than releasing).
            raise ResolutionFailed("platform_account_unavailable", status_code=503, detail={
                "error": "platform_account_unavailable",
                "message": (f"the treg account that holds this {service} object is not configured "
                            "here right now; nothing was charged, retry later"),
            })
        return wanted.pop()
    if len(slots) == 1:
        return slots[0]
    if idempotency_key:
        slots.sort()
        return slots[int(hashlib.sha256(idempotency_key.encode()).hexdigest()[:8], 16) % len(slots)]
    return None


def capacity_slot(slot: str | None) -> str | None:
    """How the capacity domain names an account: the primary is the provider's own account (its
    buckets, locks and sweep reading carry no suffix), a further account is its label."""
    return None if slot in (None, PRIMARY_SLOT) else slot


def capacity_key(service: str, slot: str | None) -> str:
    """The provider-wide capacity key of this account: the provider's own name for the primary."""
    cap = capacity_slot(slot)
    return f"{service}#{cap}" if cap else service


def exhausted(service: str, endpoint_id: str, slot: str | None) -> bool:
    return capacity_view.is_exhausted(service, endpoint_id, capacity_slot(slot))


def all_exhausted(service: str, endpoint_id: str) -> bool:
    """No account of the provider can serve this endpoint now."""
    return all(exhausted(service, endpoint_id, slot) for slot in _labels(service))


def assign(mk) -> None:
    """Give a platform call that resolution left open (`mk.key_slot is None`) its account: the
    one not out of capacity with a free concurrency place (`fair_share`) whose smoothing bucket
    would wait least, ties rotating per provider.
    The caller takes that bucket's token before it next awaits, so a burst arriving together
    spreads instead of every call reading the same idle account."""
    if mk.key_slot is not None:
        return
    slots = [slot for slot in _labels(mk.provider) if not exhausted(mk.provider, mk.endpoint_id, slot)]
    slots = slots or _labels(mk.provider)
    start = next(_rotation.setdefault(mk.provider, itertools.count(random.randrange(1 << 16)))) % len(slots)
    mk.key_slot = min(slots[start:] + slots[:start],
                      key=lambda slot: (fair_share.queued(capacity_key(mk.provider, slot)),
                                        _wait_s(mk.provider, mk.endpoint_id, slot)))
    mk.tool.bindings = oauth_providers.platform_bindings(oauth_providers.get(mk.provider), mk.key_slot)


def rate_bucket(service: str, endpoint_id: str, slot: str | None) -> tuple[str, int, float] | None:
    """The smoothing bucket of this account."""
    return capacity_view.rate_bucket(service, endpoint_id, capacity_slot(slot))


def _wait_s(service: str, endpoint_id: str, slot: str) -> float:
    bucket = rate_bucket(service, endpoint_id, slot)
    return limiter.pending_wait_s(*bucket) if bucket else 0.0
