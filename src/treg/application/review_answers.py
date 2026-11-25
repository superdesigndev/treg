"""The vendor answer a review was written about, from the archive, shortened for a judge to read.

Used by the hourly judge (`review_judge`), the body pilot and the body audit alike, so every judge
of a review's answer sees the same thing.
"""
from __future__ import annotations

from sqlmodel import select

from .. import archive
from ..domain.feedback import body_view
from ..infra.db import session_maker
from ..models import CallRecord

REQUEST_BUDGET = 800


async def answer_of(r) -> dict | None:
    """The answer the reviewed call got, from the archive, for a direct call (`r.routed_via` is
    None; the caller counts routed reviews apart). A routed call has no archive link of its own,
    and its audit rows cannot prove which attempt's answer was returned: with
    X-Treg-Route-Confirm the review is credited to the confirmer while the finder's answer is
    returned, an upgrade returns another vendor's answer, and a miss returns none. The route's
    `served_by` is in the answer and the call event, not on the rows."""
    async with session_maker() as db:
        record = (await db.execute(select(CallRecord).where(
            CallRecord.org_id == r.org_id, CallRecord.call_ref == r.call_id,
        ).order_by(CallRecord.id.asc()).limit(1))).scalar_one_or_none()
        link = (record.archive_key_hash, record.archive_content_hash) if record is not None else (None, None)
    if None in link:
        return None
    found = await archive.resolve_result(*link)
    if not found or not found["stored"] or found["response"]["body_text"] is None:
        return None
    return found


def view(i: int, r, found: dict, capability: str, budget: int) -> dict:
    req, res = found["request"], found["response"]
    return {
        "i": i, "usefulness": r.usefulness, "reason": r.reason.strip(), "endpoint": r.endpoint_id,
        "capability": capability, "routed": r.routed_via is not None,
        "request": {"method": req["method"],
                    "url": body_view.shrink(req["url"], REQUEST_BUDGET),
                    "body": body_view.shrink(req["body_text"], REQUEST_BUDGET)},
        "response": {"status_code": res["status_code"], "media_type": res["media_type"],
                     "size_bytes": res["size_bytes"], "body": body_view.shrink(res["body_text"], budget)},
    }
