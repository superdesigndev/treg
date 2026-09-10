"""Accept a report in one transaction, independently of best-effort call auditing."""

from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from .. import hints, ratestore
from ..domain import feedback
from ..domain.feedback import reviews
from ..feedback_contract import FeedbackCategory, ReviewUsefulness
from ..infra.db import session_maker
from ..models import CallRecord, LedgerEntry

RATE_MAX = 30
RATE_WINDOW_S = 3600
RATE_NAMESPACE = "feedback"


class FeedbackRateLimited(Exception):
    pass


async def submit(
    *, org_id: int, user_email: str, category: FeedbackCategory, message: str,
    call_ids: list[str], endpoint_id: str | None,
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
            verified.update((await db.execute(select(CallRecord.call_ref).where(
                CallRecord.org_id == org_id, CallRecord.call_ref.in_(call_ids),
            ))).scalars())
            verified.update((await db.execute(select(LedgerEntry.call_id).where(
                LedgerEntry.org_id == org_id, LedgerEntry.call_id.in_(call_ids),
            ))).scalars())
        row = feedback.add(
            db, org_id=org_id, user_email=user_email, category=category, message=message,
            call_ids=call_ids, verified_call_ids=[ref for ref in call_ids if ref in verified],
            endpoint_id=endpoint_id,
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
    reason: str | None = None, client: str = "",
) -> tuple[int, bool]:
    """Return (review_id, inserted); attribution never comes from the caller."""
    async with session_maker() as db:
        record = (await db.execute(select(CallRecord).where(
            CallRecord.org_id == org_id, CallRecord.call_ref == call_id,
        ).order_by(CallRecord.id.asc()).limit(1))).scalar_one_or_none()
        if record is None:
            # The ledger can verify provenance while audit is delayed, but cannot establish
            # status/provider/cache state. Wait for that evidence rather than inventing it.
            raise ReviewCallNotFound
        if not record.endpoint_id:
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
