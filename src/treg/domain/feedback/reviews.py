"""The sole writer of callreview. The application owns the transaction."""
from datetime import datetime

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import CallReview


def add(db: AsyncSession, **values) -> CallReview:
    row = CallReview(**values)
    db.add(row)
    return row


async def get(db: AsyncSession, call_id: str, org_id: int) -> CallReview | None:
    return (await db.execute(select(CallReview).where(
        CallReview.call_id == call_id, CallReview.org_id == org_id,
    ))).scalar_one_or_none()


async def recent(
    db: AsyncSession, *, limit: int, before: int | None = None, endpoint_id: str | None = None,
) -> list[CallReview]:
    query = select(CallReview)
    if before is not None:
        query = query.where(CallReview.id < before)
    if endpoint_id is not None:
        query = query.where(CallReview.endpoint_id == endpoint_id)
    return list((await db.execute(query.order_by(CallReview.id.desc()).limit(limit))).scalars())


async def record_privacy(db: AsyncSession, review_id: int, person_p: float, at: datetime) -> bool:
    """Jev's privacy judgement, written only onto a review not judged yet: a second run that asked
    too never replaces the first answer (which may already decide a public quote)."""
    result = await db.execute(update(CallReview).where(
        CallReview.id == review_id, CallReview.privacy_judged_at.is_(None),
    ).values(person_p=person_p, privacy_judged_at=at))
    return result.rowcount == 1


REASON, ANSWER, REASON_FINAL = "reason", "answer", "reason_final"


async def record_codes(db: AsyncSession, review_id: int, primary: str, secondary: str | None,
                       version: str, at: datetime, *, basis: str = REASON) -> bool:
    """Codebook codes from the reason, written only over none or an older codebook version (versions
    are ISO dates, so they order as strings): a worker on an older build never downgrades a newer
    judgement. `basis` is `reason`, or `reason_final` when no answer will ever be shown for it;
    any answer back-off starts over."""
    result = await db.execute(update(CallReview).where(
        CallReview.id == review_id,
        or_(CallReview.codebook_version.is_(None), CallReview.codebook_version < version),
    ).values(primary_code=primary, secondary_code=secondary, codebook_version=version, code_judged_at=at,
             code_basis=basis, answer_retry_at=None, answer_attempts=None))
    return result.rowcount == 1


def _upgradable(version: str):
    return (CallReview.codebook_version == version,
            or_(CallReview.code_basis.is_(None), CallReview.code_basis == REASON))


async def record_answer_codes(db: AsyncSession, review_id: int, primary: str, secondary: str | None,
                              version: str, at: datetime) -> bool:
    """Codes judged with the call's answer in view, written over this version's reason codes, final
    ones included: an answer one run read wins over another run that, meanwhile, gave up reading it.
    Never over another answer judgement, and never over codes of a newer codebook."""
    result = await db.execute(update(CallReview).where(
        CallReview.id == review_id, CallReview.codebook_version == version,
        or_(CallReview.code_basis.is_(None), CallReview.code_basis.in_((REASON, REASON_FINAL)))).values(
        primary_code=primary, secondary_code=secondary, code_judged_at=at, code_basis=ANSWER,
        answer_retry_at=None, answer_attempts=None))
    return result.rowcount == 1


def _attempts_still(seen: int | None):
    return CallReview.answer_attempts.is_(None) if seen is None else CallReview.answer_attempts == seen


_ANY = object()


async def record_answer_final(db: AsyncSession, review_id: int, version: str, seen=_ANY) -> bool:
    """No answer will be shown for this review: its reason codes stand. Given the `seen` failed
    tries this run read (its tries ran out), only while the row still holds them: a worker allowed
    more tries that recorded another failure meanwhile keeps its back-off. Without `seen` (the
    answer is gone for good) it ends whatever the count."""
    where = [CallReview.id == review_id, *_upgradable(version)]
    if seen is not _ANY:
        where.append(_attempts_still(seen))
    result = await db.execute(update(CallReview).where(*where)
                              .values(code_basis=REASON_FINAL, answer_retry_at=None, answer_attempts=None))
    return result.rowcount == 1


async def record_answer_retry(db: AsyncSession, review_id: int, version: str, seen: int | None,
                              retry_at: datetime) -> bool:
    """The answer could not be read now: one more failed try, and the next from `retry_at`. Only
    while the row still holds the `seen` tries this run read, so two runs failing together count
    once and never move the count back."""
    result = await db.execute(update(CallReview).where(
        CallReview.id == review_id, *_upgradable(version), _attempts_still(seen))
        .values(answer_attempts=(seen or 0) + 1, answer_retry_at=retry_at))
    return result.rowcount == 1


async def record_answer_wait(db: AsyncSession, review_id: int, version: str, seen: int | None,
                             retry_at: datetime) -> bool:
    """A read cut by the run's time, not failed: the next try waits until `retry_at`, the count of
    failed tries untouched. Only while the row still holds the `seen` tries: a failure another run
    recorded meanwhile, with its own back-off, stands."""
    result = await db.execute(update(CallReview).where(
        CallReview.id == review_id, *_upgradable(version), _attempts_still(seen))
        .values(answer_retry_at=retry_at))
    return result.rowcount == 1
