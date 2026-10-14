"""Accept a report in one transaction, independently of best-effort call auditing."""

import asyncio
import time
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .. import hints, ratestore
from ..domain import feedback
from ..domain.feedback import reviews, verdicts
from ..domain.governance.access import pinned_tag_predicates
from ..feedback_contract import FeedbackCategory, ReviewUsefulness
from ..infra.db import session_maker
from ..models import CallRecord, LedgerEntry
from ..timeutil import utcnow_naive

RATE_MAX = 30
RATE_WINDOW_S = 3600
RATE_NAMESPACE = "feedback"


class FeedbackRateLimited(Exception):
    pass


async def submit(
    *, org_id: int, user_email: str, category: FeedbackCategory, message: str,
    call_ids: list[str], endpoint_id: str | None, pinned_tags: dict | None = None,
) -> int:
    async with session_maker() as db:
        await ratestore.sweep(db, RATE_NAMESPACE)
        if not await ratestore.rate_check(
            db, RATE_NAMESPACE, [(str(org_id), RATE_MAX)], RATE_WINDOW_S,
        ):
            await db.commit()
            raise FeedbackRateLimited
        # Only look inside this team. Missing audit rows and delayed reports remain reportable;
        # unknown references never grant access or count as verified attribution.
        verified: set[str] = set()
        if call_ids:
            # A pinned reporter verifies only against its own pin's rows, like every other read.
            verified.update((await db.execute(select(CallRecord.call_ref).where(
                CallRecord.org_id == org_id, CallRecord.call_ref.in_(call_ids),
                *pinned_tag_predicates(CallRecord.tags, pinned_tags),
            ))).scalars())
            verified.update((await db.execute(select(LedgerEntry.call_id).where(
                LedgerEntry.org_id == org_id, LedgerEntry.call_id.in_(call_ids),
                # Only the reserve entry carries `meta.tags`, so a pinned reporter matches on it.
                *([LedgerEntry.kind == "reserve",
                   *pinned_tag_predicates(LedgerEntry.meta["tags"], pinned_tags)] if pinned_tags else []),
            ))).scalars())
        row = feedback.add(
            db, org_id=org_id, user_email=user_email, category=category, message=message,
            call_ids=call_ids, verified_call_ids=[ref for ref in call_ids if ref in verified],
            endpoint_id=endpoint_id, tags=dict(pinned_tags) if pinned_tags else None,
        )
        await db.flush()
        feedback_id = row.id
        await db.commit()
        return feedback_id


class ReviewCallNotFound(Exception):
    pass


class ReviewOwnTool(Exception):
    pass


async def submit_review(
    *, org_id: int, user_email: str, call_id: str, usefulness: ReviewUsefulness,
    reason: str | None = None, client: str = "", pinned_tags: dict | None = None,
) -> tuple[int, bool]:
    """Return (review_id, inserted); attribution never comes from the caller.

    A pinned caller may only review a call carrying its pin: the same read scope as `/calls`, so
    a foreign reference is a 404 here too rather than an oracle for another customer's calls."""
    async with session_maker() as db:
        record = (await db.execute(select(CallRecord).where(
            CallRecord.org_id == org_id, CallRecord.call_ref == call_id,
            *pinned_tag_predicates(CallRecord.tags, pinned_tags),
        ).order_by(CallRecord.id.asc()).limit(1))).scalar_one_or_none()
        if record is None:
            # The ledger can verify provenance while audit is delayed, but cannot establish
            # status/provider/cache state. Wait for that evidence rather than inventing it.
            raise ReviewCallNotFound
        if not record.endpoint_id:
            raise ReviewOwnTool
        # A maker's review of its own hub tool is not a buyer's word (hub simulation run 3).
        from ..models import HubTool
        if (await db.execute(select(HubTool.id).where(
                HubTool.tool_id == record.endpoint_id, HubTool.org_id == org_id).limit(1))).first() is not None:
            raise ReviewOwnTool
        endpoint_id, provider, routed_via = record.endpoint_id, record.provider, None
        if record.credential_tier == "routed":
            routed_via = record.endpoint_id
            child = (await db.execute(select(CallRecord).where(
                CallRecord.org_id == org_id,
                CallRecord.call_ref.startswith(f"{call_id}:r", autoescape=True),
                CallRecord.status_code >= 200, CallRecord.status_code < 300,
            ).order_by(CallRecord.id.desc()).limit(1))).scalar_one_or_none()
            if child is not None and child.endpoint_id:
                endpoint_id, provider = child.endpoint_id, child.provider
        invited = (200 <= record.status_code < 300 and not record.cached
                   and record.credential_tier == "platform"
                   and hints.sampled("review", call_id))
        existing = await reviews.get(db, call_id, org_id)
        if existing is not None:
            return existing.id, False
        # Keep the savepoint open until the application's commit, mirroring money.topup:
        # releasing the first savepoint can commit early with SQLite's deferred BEGIN.
        nested = await db.begin_nested()
        try:
            row = reviews.add(
                db, org_id=org_id, user_email=user_email, call_id=call_id,
                endpoint_id=endpoint_id, provider=provider, routed_via=routed_via,
                invited=invited, client=client, usefulness=usefulness, reason=reason,
            )
            await db.flush()
        except IntegrityError:
            await nested.rollback()
            existing = await reviews.get(db, call_id, org_id)
            if existing is None:
                raise
            return existing.id, False
        review_id = row.id
        await db.commit()
        return review_id, True


VERDICTS_TTL_S = 300
_verdicts: tuple[float, dict[str, dict]] | None = None
_verdicts_lock = asyncio.Lock()
_verdicts_refresh: asyncio.Task | None = None


async def _fold_verdicts() -> dict[str, dict]:
    async with session_maker() as db:
        rows = await verdicts.since(db, utcnow_naive() - timedelta(days=verdicts.WINDOW_DAYS))
    return verdicts.summarize(rows)


async def _refresh_verdicts() -> None:
    global _verdicts
    assert _verdicts is not None
    try:
        fold = await _fold_verdicts()
    except Exception:
        fold = _verdicts[1]   # keep serving the previous fold; retry after a full TTL, not per request
    _verdicts = (time.monotonic(), fold)


async def endpoint_verdicts() -> dict[str, dict]:
    """Every endpoint's published verdicts (`domain.feedback.verdicts`), one fold per process every
    `VERDICTS_TTL_S`. The whole window is read at once: the catalog asks for a platform's worth of
    endpoints per page, and one small table scan serves every page. Only the first fold is waited
    on; after that an expired fold is served while the next one is read in the background, so no
    catalog page waits on the database. A failed first fold raises; the caller decides what no
    fold at all means."""
    global _verdicts, _verdicts_refresh
    if _verdicts is None:
        async with _verdicts_lock:
            if _verdicts is None:
                _verdicts = (time.monotonic(), await _fold_verdicts())
        return _verdicts[1]
    if time.monotonic() - _verdicts[0] >= VERDICTS_TTL_S and (_verdicts_refresh is None or _verdicts_refresh.done()):
        _verdicts_refresh = asyncio.create_task(_refresh_verdicts())
    return _verdicts[1]


def forget_endpoint_verdicts() -> None:
    global _verdicts, _verdicts_lock, _verdicts_refresh
    if _verdicts_refresh is not None:
        _verdicts_refresh.cancel()
    _verdicts, _verdicts_lock, _verdicts_refresh = None, asyncio.Lock(), None
