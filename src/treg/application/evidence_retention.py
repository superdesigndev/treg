"""Age out failed-call evidence (`callrecord.error_request` / `error_response`) past retention.

Run by the `treg-worker admin purge-evidence` cron, never on a request: `GET /admin/errors` is a
read, and an admin reading errors during an incident must not blank evidence platform-wide. Until
the cron has run, the view itself withholds evidence older than the window (routers/admin.py), so
a late or missing schedule delays the UPDATE but never extends what a reader can see.

An UPDATE, not a DELETE: `callrecord` is the audit trail and the rest of the row must survive. The
sentinel rather than NULL keeps "captured, then aged out" distinguishable from "never captured" —
without it an old failure and a successful call look identical.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import func, or_, select, update

from ..infra.db import session_maker
from ..models import CallRecord
from ..timeutil import utcnow_naive

ERROR_EVIDENCE_TTL_DAYS = 14
ERROR_EVIDENCE_EXPIRED = "<expired>"


def cutoff():
    return utcnow_naive() - timedelta(days=ERROR_EVIDENCE_TTL_DAYS)


async def purge(batch_size: int = 5000, session_factory=session_maker) -> dict:
    """Blank expired evidence `batch_size` rows per transaction → {purged, batches, error}.

    Each batch is its own short transaction on a bounded id set, so no run holds a lock over the
    whole backlog, and a failure keeps the batches already committed (the next run resumes, because
    purged rows no longer match). Overlapping runs are harmless: the UPDATE is idempotent."""
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    limit = cutoff()
    # `coalesce`, not a bare `!=`: SQL three-valued logic makes `error_response != '<expired>'`
    # UNKNOWN when that column is NULL, so a row carrying request-only evidence would never age
    # out — excluded by the very predicate meant only to skip rows already purged.
    pending = (CallRecord.created_at < limit,
               or_(CallRecord.error_request.is_not(None), CallRecord.error_response.is_not(None)),
               or_(func.coalesce(CallRecord.error_request, "") != ERROR_EVIDENCE_EXPIRED,
                   func.coalesce(CallRecord.error_response, "") != ERROR_EVIDENCE_EXPIRED))
    purged = batches = 0
    try:
        while True:
            async with session_factory() as db:
                ids = (select(CallRecord.id).where(*pending)
                       .order_by(CallRecord.id).limit(batch_size).scalar_subquery())
                result = await db.execute(
                    update(CallRecord).where(CallRecord.id.in_(ids))
                    .values(error_request=ERROR_EVIDENCE_EXPIRED, error_response=ERROR_EVIDENCE_EXPIRED)
                    .execution_options(synchronize_session=False))
                await db.commit()
            count = int(result.rowcount or 0)
            purged += count
            batches += 1
            if count < batch_size:
                break
    except Exception as exc:  # noqa: BLE001 — reported, and the worker exits non-zero on it
        logging.getLogger("treg").warning("error-evidence purge failed: %s", exc)
        return {"purged": purged, "batches": batches, "error": str(exc)}
    return {"purged": purged, "batches": batches, "error": None}
