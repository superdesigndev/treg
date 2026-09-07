"""Managed API-key storage and lifecycle rules.

The membership remains the authorization source. This module controls only the credential that
resolves to that membership and the safe metadata retained after membership removal.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timedelta

from sqlalchemy import or_, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ... import crypto
from ...infra.db import background_session_maker
from ...models import ApiKey, ApiKeyEvent, Membership, User
from ...timeutil import utcnow_naive

DEFAULT_KIND = "default_human"
ADDITIONAL_KIND = "additional_human"
LEGACY_KIND = "legacy_human"
AGENT_KIND = "agent"
ACTIVE = "active"
DISABLED = "disabled"
REVOKED = "revoked"

# Last-used time is display metadata, not an authentication authority. Never update the key row in
# the authentication transaction. One process schedules at most one best-effort write for a key in
# this window, and the database predicate protects the same bound across processes.
_LAST_USED_INTERVAL = timedelta(minutes=5)
_LAST_USED_CACHE_MAX = 4096
_LAST_USED_WRITERS = 1
_last_used_claims: OrderedDict[int, datetime] = OrderedDict()
_last_used_tasks: set[asyncio.Task] = set()
_last_used_gate: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


class RotationConflict(Exception):
    """Another request already claimed the key that this request tried to rotate."""


def is_agent_email(email: str) -> bool:
    return email.strip().lower().endswith("@agents.treg.local")


def is_machine_email(email: str) -> bool:
    normalized = email.strip().lower()
    return is_agent_email(normalized) or normalized.endswith("@public-demo.treg.local")


def mint_secret() -> tuple[str, str, str]:
    """Return plaintext, safe prefix, and SHA-256 hash for a new managed credential."""
    secret = f"treg_{crypto.new_token()}"
    return secret, secret[:12], crypto.hash_token(secret)


def audit_event(db: AsyncSession, key: ApiKey, actor_email: str, action: str) -> None:
    db.add(ApiKeyEvent(
        org_id=key.org_id,
        key_id=key.id,
        actor_email=actor_email,
        identity_label=key.identity_label,
        action=action,
    ))


async def ensure_default_key(
    db: AsyncSession,
    membership: Membership,
    user: User,
    *,
    created_by: str | None = None,
) -> ApiKey | None:
    """Create the stable signed-key control for a real human membership if it is absent."""
    if user.demo or is_machine_email(user.email):
        return None
    row = (await db.execute(select(ApiKey).where(
        ApiKey.membership_id == membership.id,
        ApiKey.kind == DEFAULT_KIND,
    ))).scalar_one_or_none()
    if row is not None:
        return row
    row = ApiKey(
        org_id=membership.org_id,
        membership_id=membership.id,
        identity_label=user.email,
        kind=DEFAULT_KIND,
        name="Default key",
        state=ACTIVE,
        created_by=created_by or user.email,
        created_at=membership.created_at,
    )
    db.add(row)
    await db.flush()
    return row


async def register_membership_token(
    db: AsyncSession,
    membership: Membership,
    user: User,
    token: str,
    *,
    created_by: str | None = None,
    name: str | None = None,
) -> ApiKey:
    """Record a plaintext membership token in the managed table without retaining the secret."""
    await ensure_default_key(db, membership, user, created_by=created_by)
    key_hash = crypto.hash_token(token)
    existing = (await db.execute(select(ApiKey).where(ApiKey.key_hash == key_hash))).scalar_one_or_none()
    if existing is not None:
        return existing
    agent = is_agent_email(user.email)
    row = ApiKey(
        org_id=membership.org_id,
        membership_id=membership.id,
        identity_label=user.email,
        kind=AGENT_KIND if agent else LEGACY_KIND,
        name=name or ("Agent key" if agent else "Legacy key"),
        key_hash=key_hash,
        state=ACTIVE,
        created_by=created_by or membership.created_by or user.email,
        created_at=membership.created_at,
    )
    db.add(row)
    await db.flush()
    return row


async def active_agent_key(db: AsyncSession, membership_id: int) -> ApiKey | None:
    return (await db.execute(select(ApiKey).where(
        ApiKey.membership_id == membership_id,
        ApiKey.kind == AGENT_KIND,
        ApiKey.state.in_((ACTIVE, DISABLED)),
    ))).scalar_one_or_none()


async def _claim_rotation(db: AsyncSession, old: ApiKey, now: datetime) -> None:
    """Atomically revoke and hide one exact row before a replacement is inserted.

    The conditional UPDATE is the cross-process lock. PostgreSQL waits for the winning transaction
    and then returns rowcount 0. SQLite can instead report a busy write; that is also a controlled
    conflict for this operation.
    """
    try:
        result = await db.execute(
            update(ApiKey).where(
                ApiKey.id == old.id,
                ApiKey.state.in_((ACTIVE, DISABLED)),
                ApiKey.replacement_key_id.is_(None),
            ).values(state=REVOKED, revoked_at=now, deleted_at=now)
            .execution_options(synchronize_session=False)
        )
    except OperationalError as exc:
        detail = str(exc).lower()
        if "locked" in detail or "busy" in detail:
            raise RotationConflict from exc
        raise
    if result.rowcount != 1:
        raise RotationConflict
    old.state = REVOKED
    old.revoked_at = now
    old.deleted_at = now


async def rotate_agent_key(
    db: AsyncSession,
    membership: Membership,
    user: User,
    *,
    actor_email: str,
    expected_key_id: int | None = None,
) -> tuple[str, ApiKey]:
    """Revoke the current agent key and create its one active replacement."""
    old = await active_agent_key(db, membership.id)
    if expected_key_id is not None and (old is None or old.id != expected_key_id):
        raise RotationConflict
    now = utcnow_naive()
    if old is not None:
        await _claim_rotation(db, old, now)
        audit_event(db, old, actor_email, "rotated")
    secret, prefix, key_hash = mint_secret()
    new = ApiKey(
        org_id=membership.org_id,
        membership_id=membership.id,
        identity_label=user.email,
        kind=AGENT_KIND,
        name=(old.name if old is not None else "Agent key"),
        safe_prefix=prefix,
        key_hash=key_hash,
        state=ACTIVE,
        created_by=actor_email,
    )
    db.add(new)
    await db.flush()
    if old is not None:
        old.replacement_key_id = new.id
    audit_event(db, new, actor_email, "created" if old is None else "rotation_replacement_created")
    membership.token_hash = key_hash  # compatibility column during the staged migration
    return secret, new


async def rotate_human_key(
    db: AsyncSession,
    old: ApiKey,
    *,
    actor_email: str,
) -> tuple[str, ApiKey]:
    """Atomically rotate one additional human key and return its one-time replacement secret."""
    if old.kind != ADDITIONAL_KIND:
        raise RotationConflict
    await _claim_rotation(db, old, utcnow_naive())
    audit_event(db, old, actor_email, "rotated")
    secret, prefix, key_hash = mint_secret()
    new = ApiKey(
        org_id=old.org_id,
        membership_id=old.membership_id,
        identity_label=old.identity_label,
        kind=ADDITIONAL_KIND,
        name=old.name,
        safe_prefix=prefix,
        key_hash=key_hash,
        state=ACTIVE,
        created_by=actor_email,
    )
    db.add(new)
    await db.flush()
    old.replacement_key_id = new.id
    audit_event(db, new, actor_email, "rotation_replacement_created")
    return secret, new


async def rotate_default_generation(db: AsyncSession, key: ApiKey, *, actor_email: str) -> int:
    """Atomically invalidate one signed Default token without creating another key row."""
    if key.kind != DEFAULT_KIND or key.state != ACTIVE:
        raise RotationConflict
    generation = key.default_generation
    try:
        result = await db.execute(
            update(ApiKey).where(
                ApiKey.id == key.id,
                ApiKey.kind == DEFAULT_KIND,
                ApiKey.state == ACTIVE,
                ApiKey.default_generation == generation,
            ).values(default_generation=generation + 1)
            .execution_options(synchronize_session=False)
        )
    except OperationalError as exc:
        detail = str(exc).lower()
        if "locked" in detail or "busy" in detail:
            raise RotationConflict from exc
        raise
    if result.rowcount != 1:
        raise RotationConflict
    key.default_generation = generation + 1
    audit_event(db, key, actor_email, "default_key_rotated")
    return key.default_generation


async def replace_membership_token(
    db: AsyncSession,
    membership: Membership,
    user: User,
    token: str,
    *,
    actor_email: str,
    name: str = "Legacy key",
) -> ApiKey:
    """Replace a compatibility membership token without leaving its old managed row active."""
    old = None
    if membership.token_hash:
        old = (await db.execute(select(ApiKey).where(
            ApiKey.key_hash == membership.token_hash,
        ))).scalar_one_or_none()
    now = utcnow_naive()
    if old is not None and old.state != REVOKED:
        old.state = REVOKED
        old.revoked_at = now
        audit_event(db, old, actor_email, "rotated")
    membership.token_hash = crypto.hash_token(token)
    new = await register_membership_token(
        db, membership, user, token, created_by=actor_email, name=name,
    )
    if old is not None:
        old.replacement_key_id = new.id
    audit_event(db, new, actor_email, "created" if old is None else "rotation_replacement_created")
    return new


async def revoke_membership_keys(
    db: AsyncSession,
    membership: Membership,
    *,
    actor_email: str,
) -> None:
    """Revoke and detach every key before its membership is removed."""
    now = utcnow_naive()
    rows = (await db.execute(select(ApiKey).where(
        ApiKey.membership_id == membership.id,
    ))).scalars().all()
    for row in rows:
        if row.state != REVOKED:
            row.state = REVOKED
            row.revoked_at = now
            audit_event(db, row, actor_email, "membership_revoked")
        row.membership_id = None


def key_snapshot(key: ApiKey | None) -> dict:
    return {
        "api_key_id": key.id if key else None,
        "api_key_name": key.name if key else None,
        "api_key_prefix": key.safe_prefix if key else None,
    }


def _writer_gate() -> asyncio.Semaphore:
    """Return a loop-local gate; pytest and embedded apps can use more than one event loop."""
    global _last_used_gate
    loop = asyncio.get_running_loop()
    if _last_used_gate is None or _last_used_gate[0] is not loop:
        _last_used_gate = (loop, asyncio.Semaphore(_LAST_USED_WRITERS))
    return _last_used_gate[1]


async def _write_last_used(key_id: int, seen_at: datetime) -> bool:
    """Conditionally persist display metadata on the background pool.

    A failed best-effort write must not change an authentication result. Returning ``False`` lets
    the scheduler release its in-process throttle claim so a later request can retry.
    """
    try:
        async with _writer_gate():
            async with background_session_maker() as db:
                result = await db.execute(
                    update(ApiKey).where(
                        ApiKey.id == key_id,
                        or_(
                            ApiKey.last_used_at.is_(None),
                            ApiKey.last_used_at < seen_at - _LAST_USED_INTERVAL,
                        ),
                    ).values(last_used_at=seen_at)
                )
                await db.commit()
                return True
    except Exception as exc:  # noqa: BLE001 - optional metadata must not fail authentication
        logging.getLogger("treg").warning(
            "managed-key last-used update failed for key %s: %s", key_id, exc,
        )
        return False


async def _run_last_used(key_id: int, seen_at: datetime) -> None:
    await _write_last_used(key_id, seen_at)


def touch(key: ApiKey | None, now: datetime | None = None) -> None:
    """Schedule a bounded, throttled last-used update after authentication has committed."""
    if key is None or key.id is None:
        return
    seen_at = now or utcnow_naive()
    cutoff = seen_at - _LAST_USED_INTERVAL
    if key.last_used_at is not None and key.last_used_at >= cutoff:
        return
    claimed_at = _last_used_claims.get(key.id)
    if claimed_at is not None and claimed_at >= cutoff:
        return
    _last_used_claims[key.id] = seen_at
    _last_used_claims.move_to_end(key.id)
    while len(_last_used_claims) > _LAST_USED_CACHE_MAX:
        _last_used_claims.popitem(last=False)
    try:
        task = asyncio.create_task(_run_last_used(key.id, seen_at))
    except RuntimeError:
        _last_used_claims.pop(key.id, None)
        return
    _last_used_tasks.add(task)
    task.add_done_callback(_last_used_tasks.discard)


async def drain_last_used() -> None:
    """Wait for scheduled writes. Tests and graceful shutdown paths may use this boundary."""
    while _last_used_tasks:
        await asyncio.gather(*tuple(_last_used_tasks), return_exceptions=True)
