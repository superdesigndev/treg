"""Idempotency state for proxied calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, or_, update
from sqlalchemy.exc import IntegrityError, TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...config import get_settings
from ...infra.db import session_maker
from ...domain.identity.access import Caller
from ...models import AsyncTaskRecord, Hold, IdempotentCall, LedgerEntry
from .types import IdempotencyFailed, IdempotentReplay

if TYPE_CHECKING:
    from .intake import CallMeta


IDEMPOTENCY_WINDOW_S = 24 * 3600   # retries happen in seconds; a day is generous and easy to reason about
IDEMPOTENCY_HEADER = "idempotency-key"
# A pending claim is a LEASE owned by one call (`call_ref`). The owner renews it every
# IDEMPOTENCY_LEASE_RENEW_S while it runs (`_hold_claim_lease`), so `created_at` older than
# IDEMPOTENCY_STALE_PENDING_S means the owner stopped: its process died, or the bookkeeping that
# marks the claim done or releases it failed (a pool timeout). Such a claim is closed with a stored
# terminal answer rather than answering 409 for the whole window; see `_resolve_stale_claim`.
IDEMPOTENCY_LEASE_RENEW_S = 5 * 60
IDEMPOTENCY_STALE_PENDING_S = 3 * IDEMPOTENCY_LEASE_RENEW_S
_POOL_RETRY_PAUSE_S = 0.5  # the settle retry's pause (settle.py); one retry, fresh session

_IDEM_MAX_KEY = 200


def _idempotency_key(raw_header: str | None) -> str:
    """The caller's label for this request, or "" when they sent none.

    Only ever the client's. A server-invented key — hashing the URL and body, say — would silently
    collapse two calls a caller genuinely MEANT to make twice, and "do this again" is a legitimate
    thing to ask of an API. No header means today's behaviour exactly: no lookup, no storage.
    """
    return (raw_header or "").strip()[:_IDEM_MAX_KEY]


_IDEM_SCOPE_SEP = "\x1f"


def _scoped_idempotency_key(
    key: str, meta: CallMeta, *, pinned_tags: dict | None = None,
) -> str:
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

    The primary caller-asserted dimension still partitions unpinned callers. An enforced pin is
    additionally part of the namespace: changing any pin must not expose an earlier cached body.
    """
    if not key:
        return key
    scoped = f"{meta.primary_val}{_IDEM_SCOPE_SEP}{key}" if meta.primary_val else key
    if pinned_tags:
        pins = json.dumps(pinned_tags, sort_keys=True, separators=(",", ":"))
        return f"pins:{pins}{_IDEM_SCOPE_SEP}{scoped}"
    return scoped


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
    if (row.status != "done" and row.call_ref
            and row.created_at < _utcnow() - timedelta(seconds=IDEMPOTENCY_STALE_PENDING_S)):
        resolved = await _resolve_stale_claim(row, caller, db)
        if resolved is not None:
            return resolved
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _owned(membership_id: int, key: str, call_ref: str):
    """The WHERE clause every owner write carries: a claim taken over by another call is not ours."""
    return (IdempotentCall.membership_id == membership_id, IdempotentCall.key == key,
            IdempotentCall.call_ref == call_ref)


async def _money_of(db: AsyncSession, org_id: int, call_ref: str, since: datetime,
                    until: datetime) -> dict:
    """What one call and every child it spawned did with money, from the hold and ledger tables.

    A call's money lives under ids prefixed by its call_ref: its own hold, `:overflow`, routed
    children `:r{n}` and their polls, hub steps `:s{n}` and the hub `:price`. uuid hex carries no
    LIKE wildcard. The LIKE cannot use an index under a non-C collation, so the ledger read is
    bounded to the owner's lifetime on `(org_id, created_at)`: from the claim to one renewal past
    its last lease renewal. The only money that can move after that is an async task's worker
    settling or releasing it, so the tasks the owner started in its lifetime are found the same
    way and their ledger rows read by exact call id, with no time bound: a still-pending task is
    money in flight, and a late settle is a charge.
    """
    def mine(col):
        return or_(col == call_ref, col.like(f"{call_ref}:%"))
    open_hold = (await db.execute(select(Hold.id).where(
        Hold.org_id == org_id, mine(Hold.id)).limit(1))).first() is not None
    rows = list((await db.execute(select(LedgerEntry.kind, LedgerEntry.amount_micro).where(
        LedgerEntry.org_id == org_id, LedgerEntry.created_at >= since, LedgerEntry.created_at <= until,
        mine(LedgerEntry.call_id)))).all())
    tasks = (await db.execute(select(AsyncTaskRecord.call_id, AsyncTaskRecord.status).where(
        AsyncTaskRecord.org_id == org_id, AsyncTaskRecord.created_at >= since,
        AsyncTaskRecord.created_at <= until, mine(AsyncTaskRecord.call_id)))).all()
    if any(status == "pending" for _, status in tasks):
        open_hold = True  # a task's hold is its worker's until it resolves
    if tasks:
        rows += (await db.execute(select(LedgerEntry.kind, LedgerEntry.amount_micro).where(
            LedgerEntry.call_id.in_([task_id for task_id, _ in tasks]),
            LedgerEntry.created_at > until))).all()
    # settle entries are negative (money leaving)
    return {"in_flight": open_hold, "charged": -sum(amount for kind, amount in rows if kind == "settle")}


async def _resolve_stale_claim(row: IdempotentCall, caller: Caller, db: AsyncSession):
    """Close an abandoned lease with a stored terminal answer; never run the key again.

    An open hold or a pending async task means the owner or its worker is not done: 409 (None
    here). Otherwise the answer is a stored 410, so every later retry gets the same reply:
    `idempotency_response_lost` with the charge when the owner's money shows one (the answer was
    paid for but not kept), else `idempotency_outcome_unknown`. The claim is never handed to a new
    call: a lease that stopped renewing does not prove its owner stopped, and a second run under
    the same key is exactly the double charge this table exists to prevent. A new key calls again.

    The write is a compare-and-swap on the lease as read (owner AND `created_at`): a renewal or
    another retry in between wins, and this one answers 409 on the fresh state.
    """
    started = row.expires_at - timedelta(seconds=IDEMPOTENCY_WINDOW_S)
    lifetime_end = row.created_at + timedelta(seconds=IDEMPOTENCY_LEASE_RENEW_S + 60)
    money = await _money_of(db, caller.org_id, row.call_ref, started, lifetime_end)
    if money["in_flight"]:
        return None
    label, owner, charged = _idem_display(row.key), row.call_ref, max(0, money["charged"])
    if charged:
        detail = {"error": "idempotency_response_lost", "call_id": owner, "charged_micro": charged,
                  "message": (f"the call with Idempotency-Key {label!r} completed and was charged, but "
                              f"its response was not retained. GET /calls/{owner}/result may still "
                              "have it; send a new key to call again.")}
    else:
        detail = {"error": "idempotency_outcome_unknown", "call_id": owner,
                  "message": (f"the call with Idempotency-Key {label!r} stopped before its outcome was "
                              f"recorded and may have reached the provider. GET /calls/{owner} shows "
                              "what it cost; send a new key to call again.")}
    body = json.dumps({"detail": detail}, separators=(",", ":")).encode()
    won = (await db.execute(update(IdempotentCall).where(
        IdempotentCall.id == row.id, IdempotentCall.status == "pending",
        IdempotentCall.call_ref == owner, IdempotentCall.created_at == row.created_at,
    ).values(status="done", response_status=410, response_body=body,
             response_media_type="application/json", charged_micro=charged,
    # A lost swap must not paint the loaded row as done: the caller re-reads it as a 409.
    ).execution_options(synchronize_session=False))).rowcount == 1
    await db.commit()
    if not won:
        return None
    logging.getLogger("treg.idempotency").warning(
        "idempotency claim %s: owner %s abandoned it; stored 410 %s", label, owner, detail["error"])
    return IdempotentReplay(body=body, status_code=410, media_type="application/json",
                            charged_micro=charged, call_ref=owner)


async def _with_pool_retry(work):
    """One retry after a pool timeout, on a fresh session: the settle retry's shape. A saturated
    pool is the usual reason bookkeeping fails, and a failed write here strands a claim."""
    try:
        return await work()
    except PoolTimeoutError:
        await asyncio.sleep(_POOL_RETRY_PAUSE_S)
        return await work()


async def _renew_claim_lease(claim: tuple[int, str, str]) -> None:
    """Refresh the owner's lease. Never raises: a missed renewal only shortens the lease."""
    membership_id, key, call_ref = claim

    async def _renew() -> None:
        async with session_maker() as db:
            await db.execute(update(IdempotentCall).where(
                *_owned(membership_id, key, call_ref), IdempotentCall.status == "pending",
            ).values(created_at=_utcnow()))
            await db.commit()

    try:
        await _with_pool_retry(_renew)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("treg.idempotency").warning(
            "could not renew idempotency claim %s: %s", _idem_display(key), exc)


async def _hold_claim_lease(state) -> None:
    """Renew `state.idem_claim` until the request drops it; the caller cancels this task on exit."""
    while True:
        await asyncio.sleep(IDEMPOTENCY_LEASE_RENEW_S)
        claim = getattr(state, "idem_claim", None)
        if not claim:
            return
        await _renew_claim_lease(claim)


async def _release_idempotent_claim(claim: tuple[int, str, str] | None) -> None:
    """Drop a claim this request took and never completed, so the label is usable again at once.

    Does nothing when there is no claim, which is every request that sent no key. Never raises: this
    runs while an error is already being returned.
    """
    if not claim:
        return
    membership_id, key, call_ref = claim

    async def _drop() -> None:
        async with session_maker() as db:
            await db.execute(delete(IdempotentCall).where(
                *_owned(membership_id, key, call_ref), IdempotentCall.status == "pending"))
            await db.commit()

    try:
        await _with_pool_retry(_drop)
    except Exception as exc:  # noqa: BLE001 — an error is already on its way out
        logging.getLogger("treg.idempotency").error(
            "could not release idempotency claim %s: %s", key, exc, exc_info=True)


async def _claim_idempotent(key: str, fingerprint: str, rest: str, caller: Caller,
                            db: AsyncSession, *, call_ref: str) -> bool:
    """Take the label for this caller, or report that somebody else already has it.

    The pending row IS the lock. It goes in before the upstream call, so a concurrent retry loses the
    insert on `(membership_id, key)` and is told to wait rather than duplicating the spend. It
    carries the owner's `call_ref` from the start: every later write is fenced on it, and a stale
    claim is resolved from that call's money.
    """
    # Sweep this caller's expired labels first. LAZY and caller-scoped, matching the hold reaper in
    # domain/money and for the same reasons: a background timer would need a scheduler and a leader
    # election on a multi-instance deploy, and would still only run on a timer. One indexed DELETE
    # paid by the caller who benefits from it, and a caller who never calls again leaves rows that
    # can no longer answer anything, because a replay checks the window before it serves.
    #
    # Freeing the label matters as much as reclaiming the space: without this, reusing a label a day
    # later would hit the old row's unique constraint and be refused rather than starting fresh.
    await db.execute(delete(IdempotentCall).where(
        IdempotentCall.membership_id == caller.membership.id,
        IdempotentCall.expires_at < datetime.now(timezone.utc).replace(tzinfo=None)))

    row = IdempotentCall(
        org_id=caller.org_id, membership_id=caller.membership.id, key=key, call_ref=call_ref,
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


def _too_large_note(key: str, call_ref: str, charged_micro: int, size: int) -> tuple[bytes, int, str]:
    """What a retry gets instead of an answer over the archive's size limit (`archive_max_body_bytes`).

    The caller already received the full answer; only the retry copy is dropped. A 410, never a new
    run: running the key again is the double charge this table prevents. Without the limit every
    body was kept whole for 24 h, however large.
    """
    detail = {"error": "idempotency_response_too_large", "call_id": call_ref, "charged_micro": charged_micro,
              "size_bytes": size,
              "message": (f"the call with Idempotency-Key {_idem_display(key)!r} completed and was charged, "
                          f"but its response ({size} bytes) is over treg's retry size limit, so it was "
                          "not kept for replay. Send a new key to call again.")}
    return json.dumps({"detail": detail}, separators=(",", ":")).encode(), 410, "application/json"


async def _store_idempotent(key: str, caller: Caller, *, status_code: int, body: bytes,
                            media_type: str, charged_micro: int, metered: bool,
                            call_ref: str, terminal: bool = False) -> None:
    """Remember a metered success or an explicitly terminal, partially charged routed failure.

    Metered only. A team calling on its OWN key is billed by the provider, not by us, so there is
    nothing to protect and no reason for treg to hold their response. Ordinary failures are not
    retained, because no charge landed and the caller should be free to retry. A routed waterfall
    may already have settled paid children before its terminal failure; `terminal=True` retains that
    exact failure so a retry cannot repeat those charges.

    Anything else drops the claim, which frees the label immediately rather than making the caller
    wait out the window before they can try again.

    Never raises: the caller already has their answer, and a bookkeeping failure must not turn a
    served call into a 500. Its own session, because the request's may be mid-rollback. Fenced on
    `call_ref`: a claim another call took over is left alone.
    """
    keep = metered and (200 <= status_code < 300 or terminal)
    owned = (*_owned(caller.membership.id, key, call_ref), IdempotentCall.status == "pending")
    if keep and len(body) > get_settings().archive_max_body_bytes:
        body, status_code, media_type = _too_large_note(key, call_ref, charged_micro, len(body))

    async def _write() -> None:
        async with session_maker() as db:
            if not keep:
                await db.execute(delete(IdempotentCall).where(*owned))
            else:
                await db.execute(update(IdempotentCall).where(*owned).values(
                    status="done", response_status=status_code, response_body=body,
                    response_media_type=media_type or "application/json",
                    charged_micro=charged_micro))
            await db.commit()

    try:
        await _with_pool_retry(_write)
    except Exception as exc:  # noqa: BLE001 — loudly, but never into the caller's response
        logging.getLogger("treg.idempotency").error(
            "could not record idempotency key %s: %s", key, exc, exc_info=True)
