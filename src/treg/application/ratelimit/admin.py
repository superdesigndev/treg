"""Writing the rate-limit policy: Postgres (the audited source of truth), then the Redis cache.

The only writer of `RateLimitConfigVersion`. Every write is range-checked before anything is
stored, inserts one row (a new, higher version: `revert-to-shadow` too), commits, and only then
copies it to `rl:config` with a compare-and-set that keeps the newer of the two. Serving
processes read Postgres (`config_sync`), never the cache, so a failed, late or stale publish can
never change what they serve. `republish` copies the newest row again.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...config import get_settings
from ...domain.ratelimit import RateLimitConfig
from ...infra import kv
from ...models import RateLimitConfigVersion
from ...timeutil import utcnow_naive
from .config_state import InvalidConfig, encode_document, validate

log = logging.getLogger("treg.ratelimit")
_HISTORY = 20


class VersionConflict(Exception):
    def __init__(self, current: int):
        super().__init__(f"current version is {current}")
        self.current = current


class NothingToPublish(Exception):
    pass


def _row_out(row: RateLimitConfigVersion) -> dict:
    return {"version": row.id, "created_at": row.created_at.isoformat(), "actor": row.actor,
            "action": row.action, "mode": row.mode, "note": row.note,
            "enforce_approval": row.enforce_approval, "previous_version": row.previous_version,
            "config": row.config}


async def _latest(db: AsyncSession) -> RateLimitConfigVersion | None:
    return (await db.execute(
        select(RateLimitConfigVersion).order_by(RateLimitConfigVersion.id.desc()).limit(1)
    )).scalars().first()


async def _publish(version: int, config: RateLimitConfig, *, actor: str) -> bool:
    """Copy one Postgres version into the Redis cache. Compare-and-set in the store: written only
    when `version` is newer than what it holds, so a slow or stale writer can never replace a
    newer policy. Workers do not read this copy (they read Postgres); it is a cache for tools."""
    if not kv.configured():
        return False
    document = encode_document(version, config, written_by=actor)
    try:
        held = await kv.store().rl_publish(kv.RL_CONFIG_KEY, document, version)
    except kv.Unavailable as exc:
        log.error("rate_limit_config_publish_failed version=%s error=%s", version, exc)
        return False
    if held != version:
        log.warning("rate_limit_config_publish_skipped version=%s held=%s (newer in the cache)", version, held)
    return held == version


async def _insert(db: AsyncSession, config: RateLimitConfig, *, actor: str, action: str, note: str,
                  enforce_approval: str | None, base_version: int | None) -> dict:
    latest = await _latest(db)
    current = latest.id if latest else 0
    if base_version is not None and base_version != current:
        raise VersionConflict(current)
    row = RateLimitConfigVersion(
        actor=actor, action=action, mode=config.mode, config=config.model_dump(mode="json"),
        note=note, enforce_approval=enforce_approval, previous_version=latest.id if latest else None,
        created_at=utcnow_naive(),
    )
    db.add(row)
    await db.flush()
    out = _row_out(row)
    await db.commit()  # the audit row exists before the store can serve it; no session after this
    log.info("rate_limit_config_written version=%s action=%s mode=%s actor=%s",
             row.id, action, config.mode, actor)
    out["published"] = await _publish(row.id, config, actor=actor)
    return out


async def write(db: AsyncSession, raw: object, *, actor: str, note: str,
                enforce_approval: str | None = None, base_version: int | None = None) -> dict:
    config = validate(raw)
    approval = (enforce_approval or "").strip() or None
    if config.mode == "enforce" and approval is None:
        raise InvalidConfig(["enforce_approval: required to set mode enforce"])
    return await _insert(db, config, actor=actor, action="set", note=note,
                         enforce_approval=approval if config.mode == "enforce" else None,
                         base_version=base_version)


async def revert_to_shadow(db: AsyncSession, *, actor: str, note: str = "") -> dict:
    """The one-click way back: the current thresholds in shadow, and the blocklist in log_only."""
    latest = await _latest(db)
    base = (RateLimitConfig.model_validate(latest.config) if latest
            else get_settings().rate_limit_defaults)
    config = base.model_copy(update={"mode": "shadow"})
    # The blocklist goes back to log_only in the same transaction (its mode lives on its own row).
    from .blocklist_admin import record_mode
    await record_mode(db, "log_only", actor=actor, approved_by=actor, reason=note or "revert to shadow",
                      now=utcnow_naive())
    return await _insert(db, config, actor=actor, action="revert_to_shadow",
                         note=note or "revert to shadow", enforce_approval=None, base_version=None)


async def republish(db: AsyncSession, *, actor: str) -> dict:
    """Copy Postgres's newest version into the cache. Never forced: if a newer version reached
    the cache meanwhile, this one is dropped."""
    latest = await _latest(db)
    if latest is None:
        raise NothingToPublish()
    out = _row_out(latest)
    config = RateLimitConfig.model_validate(latest.config)
    await db.commit()
    out["published"] = await _publish(latest.id, config, actor=actor)
    return out


async def history(db: AsyncSession) -> list[dict]:
    rows = (await db.execute(
        select(RateLimitConfigVersion).order_by(RateLimitConfigVersion.id.desc()).limit(_HISTORY)
    )).scalars().all()
    out = [_row_out(r) for r in rows]
    await db.commit()
    return out
