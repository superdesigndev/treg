"""Idempotency state for proxied calls."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...infra.db import session_maker
from ...domain.identity.access import Caller
from ...models import IdempotentCall
from .types import IdempotencyFailed, IdempotentReplay

if TYPE_CHECKING:
    from .intake import CallMeta


IDEMPOTENCY_WINDOW_S = 24 * 3600   # retries happen in seconds; a day is generous and easy to reason about
IDEMPOTENCY_HEADER = "idempotency-key"
_IDEM_MAX_KEY = 200


def _idempotency_key(raw_header: str | None) -> str:
    """The caller's label for this request, or "" when they sent none.

    Only ever the client's. A server-invented key — hashing the URL and body, say — would silently
    collapse two calls a caller genuinely MEANT to make twice, and "do this again" is a legitimate
    thing to ask of an API. No header means today's behaviour exactly: no lookup, no storage.
    """
    return (raw_header or "").strip()[:_IDEM_MAX_KEY]


_IDEM_SCOPE_SEP = "\x1f"


def _scoped_idempotency_key(key: str, meta: CallMeta) -> str:
    """The caller's label, PARTITIONED by the primary tag.

    A reselling builder runs every one of their users through one token, so two of them will both
    reach for `retry-1` — and `IdempotentCall` is unique on (membership_id, key), which would serve
    the second user the FIRST one's stored response body. That is the cross-tenant leak the table was
    built to prevent, reappearing one level down.

    Folding the value into the stored key partitions retries exactly as widening the unique constraint
    would, with no migration: `uq_idem_caller_key` is declared in `__table_args__`, so SQLAlchemy emits
    it as a table CONSTRAINT inside CREATE TABLE — Postgres could drop it, sqlite could not without
    rebuilding the table. Every access site keeps querying by (membership_id, key) and simply receives
    this value.

    Only the PRIMARY dimension partitions. Retry scoping cannot generalize the way budgets do: a call
    tagged `customer=a, workspace=b` has no principled answer for which of them owns the key.
    """
    if not key:
        return key
    return f"{meta.primary_val}{_IDEM_SCOPE_SEP}{key}" if meta.primary_val else key


def _idem_display(key: str) -> str:
    """The label as the CALLER wrote it — error messages must not echo our internal scoping."""
    return key.rsplit(_IDEM_SCOPE_SEP, 1)[-1]


def _request_fingerprint(method: str, rest: str, body: bytes, query: str = "") -> str:
    """What the label was used FOR, so reusing it on a different request can be caught.

    A client that reuses one label for two different requests has a bug. Quietly returning the first
    answer would hide it, and the caller would be left wondering why their second call returned
    somebody else's data. Refusing loudly is the useful behaviour, and it is what Stripe does.

    The QUERY STRING is part of the request. It was missing here at first, and since most catalog
    calls are GETs that carry all their arguments in the query, that made the check almost inert: two
    genuinely different lookups under one label matched, and the second was answered with the first
    one's data instead of the 422 this function exists to raise.
    """
    h = hashlib.sha256()
    h.update(method.upper().encode())
    h.update(b"\0")
    h.update(rest.encode())
    h.update(b"\0")
    h.update((query or "").encode())
    h.update(b"\0")
    h.update(body or b"")
    return h.hexdigest()


async def _replay_idempotent(key: str, fingerprint: str, caller: Caller,
                             db: AsyncSession) -> IdempotentReplay | None:
    """The stored answer for this caller's label, or None if there is nothing to replay.

    Returns a real response, so the provider is never reached and no money moves. That is the whole
    point: merely skipping the second CHARGE would still make the second upstream call, which means
    still paying the provider and simply absorbing the double cost ourselves.
    """
    row = (await db.execute(select(IdempotentCall).where(
        IdempotentCall.membership_id == caller.membership.id,
        IdempotentCall.key == key))).scalar_one_or_none()
    if row is None:
        return None
    if row.expires_at < datetime.now(timezone.utc).replace(tzinfo=None):
        # Past its window: the label is free again, and the call proceeds normally.
        await db.delete(row)
        await db.commit()
        return None
    if row.request_fingerprint and row.request_fingerprint != fingerprint:
        raise IdempotencyFailed(
            "idempotency_mismatch", status_code=422,
            detail=(f"Idempotency-Key {_idem_display(key)!r} was already used for a different request. Use a new key, or "
                    f"repeat the original request exactly."))
    if row.status != "done" or row.response_status is None:
        # Still in flight. The first call is talking to the provider right now; telling the caller to
        # retry is honest and cheap, and it is what stops the second one duplicating the spend.
        raise IdempotencyFailed(
            "idempotency_in_progress", status_code=409,
            detail=(f"a call with Idempotency-Key {_idem_display(key)!r} "
                    "is still in progress — retry shortly"))
    return IdempotentReplay(
        body=row.response_body or b"",
        status_code=row.response_status,
        media_type=row.response_media_type or "application/json",
        charged_micro=row.charged_micro,
        # The ORIGINAL call's id: a retry must resolve to the row that actually holds the
        # money, not to a fresh reference for work that never happened.
        call_ref=row.call_ref or "",
    )


async def _release_idempotent_claim(claim: tuple[int, str] | None) -> None:
    """Drop a claim this request took and never completed, so the label is usable again at once.

    Does nothing when there is no claim, which is every request that sent no key. Never raises: this
    runs while an error is already being returned.
    """
    if not claim:
        return
    membership_id, key = claim
    try:
        async with session_maker() as db:
            row = (await db.execute(select(IdempotentCall).where(
                IdempotentCall.membership_id == membership_id,
                IdempotentCall.key == key,
                IdempotentCall.status == "pending"))).scalar_one_or_none()
            if row is not None:
                await db.delete(row)
                await db.commit()
    except Exception as exc:  # noqa: BLE001 — an error is already on its way out
        logging.getLogger("treg.idempotency").error(
            "could not release idempotency claim %s: %s", key, exc, exc_info=True)


async def _claim_idempotent(key: str, fingerprint: str, rest: str, caller: Caller,
                            db: AsyncSession) -> bool:
    """Take the label for this caller, or report that somebody else already has it.

    The pending row IS the lock. It goes in before the upstream call, so a concurrent retry loses the
    insert on `(membership_id, key)` and is told to wait rather than duplicating the spend.
    """
    # Opportunistically release this caller's expired labels. The hourly worker additionally
    # cleans completed answers for callers who never return; see prune_expired_idempotency.
    #
    # Freeing the label matters as much as reclaiming the space: without this, reusing a label a day
    # later would hit the old row's unique constraint and be refused rather than starting fresh.
    await db.execute(delete(IdempotentCall).where(
        IdempotentCall.membership_id == caller.membership.id,
        IdempotentCall.expires_at < datetime.now(timezone.utc).replace(tzinfo=None)))

    row = IdempotentCall(
        org_id=caller.org_id, membership_id=caller.membership.id, key=key,
        request_fingerprint=fingerprint, endpoint_id=rest[:200], status="pending",
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None)
        + timedelta(seconds=IDEMPOTENCY_WINDOW_S))
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def _store_idempotent(key: str, caller: Caller, *, status_code: int, body: bytes,
                            media_type: str, charged_micro: int, metered: bool,
                            call_ref: str = "", terminal: bool = False) -> None:
    """Remember a metered success or an explicitly terminal, partially charged routed failure.

    Metered only. A team calling on its OWN key is billed by the provider, not by us, so there is
    nothing to protect and no reason for treg to hold their response. Ordinary failures are not
    retained, because no charge landed and the caller should be free to retry. A routed waterfall
    may already have settled paid children before its terminal failure; `terminal=True` retains that
    exact failure so a retry cannot repeat those charges.

    Anything else drops the claim, which frees the label immediately rather than making the caller
    wait out the window before they can try again.

    Never raises: the caller already has their answer, and a bookkeeping failure must not turn a
    served call into a 500. Its own session, because the request's may be mid-rollback.
    """
    keep = metered and (200 <= status_code < 300 or terminal)
    try:
        async with session_maker() as db:
            row = (await db.execute(select(IdempotentCall).where(
                IdempotentCall.membership_id == caller.membership.id,
                IdempotentCall.key == key))).scalar_one_or_none()
            if row is None:
                return
            if not keep:
                await db.delete(row)
            else:
                row.status = "done"
                row.response_status = status_code
                row.response_body = body
                row.response_media_type = media_type or "application/json"
                row.charged_micro = charged_micro
                row.call_ref = call_ref
                db.add(row)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.idempotency").error(
            "could not record idempotency key %s: %s", key, exc, exc_info=True)


@dataclass(frozen=True)
class IdempotencyPruneResult:
    cutoff: datetime
    upper_id: int
    eligible: int
    deleted: int
    batches: int
    complete: bool
    page_timeouts: int = 0


_PAGE_TIMEOUT_S = 60
_DELETE_TIMEOUT_S = 15


async def prune_expired_idempotency(*, batch_size: int = 200, pause_s: float = 0.25,
                                   max_batches: int = 10000, dry_run: bool = False,
                                   make_session=session_maker) -> IdempotencyPruneResult:
    """Remove only completed, expired answers, with a fixed window and bounded transactions.

    Page IDs first, then filter the DELETE: filtering before LIMIT can walk the entire cold
    history to find one expired row. The primary-key cursor bounds each page without a migration.
    Concurrent caller cleanup is harmless: the DELETE repeats the eligibility predicate.
    Pending claims and responses valid at the start of the sweep cannot be removed.

    **Timeout resilience.** After a large first prune (or any mass delete), dead tuple bloat can
    make even a bounded page SELECT slow: the executor must skip invisible rows to find N visible
    ones. The page SELECT uses a 60s timeout; if that expires, the cursor advances by batch_size
    (the page is skipped, not retried) and the sweep continues. The next cron run starts from the
    front, so skipped pages are retried on fresh autovacuum state. Any skipped page makes the
    result incomplete, even when later pages succeed; three consecutive timeouts stop traversal.

    Ops note: after a large initial prune, run `VACUUM (ANALYZE) idempotentcall` once. Autovacuum
    handles routine churn; manual vacuum is only needed after an abnormally large delete ratio.
    """
    if not 1 <= batch_size <= 1000 or not 1 <= max_batches <= 10000:
        raise ValueError("batch_size must be 1..1000 and max_batches must be 1..10000")
    if not 0 <= pause_s <= 60:
        raise ValueError("pause_s must be 0..60")
    log = logging.getLogger("treg.idempotency")

    async def bound_page(db):
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SET LOCAL lock_timeout = '1s'"))
            await db.execute(text(f"SET LOCAL statement_timeout = '{_PAGE_TIMEOUT_S}s'"))

    async def bound_delete(db):
        if db.bind.dialect.name == "postgresql":
            await db.execute(text("SET LOCAL lock_timeout = '1s'"))
            await db.execute(text(f"SET LOCAL statement_timeout = '{_DELETE_TIMEOUT_S}s'"))

    async with make_session() as db:
        await bound_page(db)
        cutoff = (await db.scalar(select(func.current_timestamp()))).replace(tzinfo=None)
        upper_id = (await db.scalar(select(func.max(IdempotentCall.id)))) or 0
        eligible_where = (
            IdempotentCall.status == "done",
            IdempotentCall.expires_at < cutoff,
            IdempotentCall.id <= upper_id,
        )
    cursor = eligible = deleted = batches = page_timeouts = 0
    consecutive_timeouts = 0
    complete = False
    while batches < max_batches:
        rows = None
        try:
            async with make_session() as db:
                await bound_page(db)
                rows = (await db.execute(select(
                    IdempotentCall.id, IdempotentCall.status, IdempotentCall.expires_at,
                ).where(
                    IdempotentCall.id <= upper_id, IdempotentCall.id > cursor,
                ).order_by(IdempotentCall.id).limit(batch_size))).all()
        except Exception as exc:
            is_timeout = "statement timeout" in str(exc).lower() or "QueryCanceledError" in type(exc).__name__
            if is_timeout:
                page_timeouts += 1
                consecutive_timeouts += 1
                log.warning(
                    "idempotency prune page timeout: cursor=%d batch_size=%d consecutive=%d (advancing)",
                    cursor, batch_size, consecutive_timeouts)
                cursor += batch_size
                batches += 1
                if cursor >= upper_id:
                    complete = True
                    break
                if consecutive_timeouts >= 3:
                    log.error("idempotency prune: %d consecutive page timeouts, stopping", consecutive_timeouts)
                    break
                await asyncio.sleep(pause_s)
                continue
            raise
        consecutive_timeouts = 0
        if not rows:
            complete = True
            break
        ids = [row.id for row in rows if row.status == "done" and row.expires_at < cutoff]
        eligible += len(ids)
        if ids and not dry_run:
            async with make_session() as db:
                await bound_delete(db)
                removed = (await db.execute(delete(IdempotentCall).where(
                    *eligible_where, IdempotentCall.id.in_(ids),
                ).returning(IdempotentCall.id))).all()
                await db.commit()
                deleted += len(removed)
        cursor = rows[-1].id
        batches += 1
        complete = len(rows) < batch_size or cursor == upper_id
        if batches % 50 == 0:
            log.info(
                "idempotency prune: batches=%d deleted=%d cursor=%d timeouts=%d",
                batches, deleted, cursor, page_timeouts)
        if complete:
            break
        await asyncio.sleep(pause_s)

    return IdempotencyPruneResult(
        cutoff, upper_id, eligible, deleted, batches, complete and page_timeouts == 0, page_timeouts,
    )
