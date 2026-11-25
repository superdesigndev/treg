"""The vendor answer a review was written about, from the archive, shortened for a judge to read.

Used by the hourly judge (`review_judge`), the body pilot and the body audit alike, so every judge
of a review's answer sees the same thing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import select

from .. import archive, archive_bodies
from ..config import get_settings
from ..domain.catalog import reported_charge
from ..domain.catalog import store as catalog_store
from ..domain.feedback import body_view
from ..infra.db import session_maker
from ..models import CallRecord

log = logging.getLogger("treg.review_answers")

REQUEST_BUDGET = 800


@dataclass(frozen=True)
class Answer:
    """`read`: `found` is the answer as delivered. `absent`: no answer can ever be shown, so the
    reason is all a judge has. `unreadable`: it was not read now (storage or the database failed, a
    read failed, object storage is in use but not configured here, a corrupt snapshot); the caller
    tries again later and gives up after a few tries, so these need not be told apart."""
    state: str
    found: dict | None = None


ABSENT = Answer("absent")
UNREADABLE = Answer("unreadable")
# A read that found every copy missing, or bytes not matching their hash, is an answer gone for good.
_GONE = ("none", "not_found", "hash_mismatch")


async def answer_of(r) -> Answer:
    """The answer the reviewed direct call got (`r.routed_via` is None; routed reviews are never
    read: their rows cannot prove which attempt's answer was returned).

    `absent` when there is no archive link, the key or every copy of these bytes is gone, the
    answer was kept as a hash only, its text is not UTF-8, or the archive cannot show what the
    caller received: a call through `/table` (`CallRecord.surface`), or one made before
    `review_answer_surface_since`, from when a deployment that had `/table` on could not tell."""
    try:
        async with session_maker() as db:
            record = (await db.execute(select(CallRecord).where(
                CallRecord.org_id == r.org_id, CallRecord.call_ref == r.call_id,
            ).order_by(CallRecord.id.asc()).limit(1))).scalar_one_or_none()
        if record is None or record.archive_key_hash is None or record.archive_content_hash is None:
            return ABSENT
        since = get_settings().review_answer_surface_since
        if record.surface is not None or (since is not None and record.created_at < since):
            return ABSENT
        report: dict = {}
        found = await archive.resolve_result(record.archive_key_hash, record.archive_content_hash,
                                             prefer_bytes=True, diagnostics=report)
        if found is None:
            return ABSENT
        if found["stored"]:
            text = found["response"]["body_text"]
            if text is None or "\x00" in text:  # not UTF-8 text, or text in another encoding
                return ABSENT
            return Answer("read", as_delivered(found, record.status_code, record.endpoint_id,
                                               record.cost_charged_micro))
        if archive_bodies.uses_r2() and not archive_bodies.has_store():
            return UNREADABLE
        return ABSENT if report.get("cache_body_fallback_reason", "none") in _GONE else UNREADABLE
    except Exception:  # tried again later; a corrupt snapshot gives up after a few tries
        log.warning("review answer not read: review %s", r.id, exc_info=True)
        return UNREADABLE


def as_delivered(found: dict, status_code: int, endpoint_id: str | None, charged: int | None) -> dict:
    """The archived answer as the reviewed call delivered it: that call's own status (one stored
    body can answer several calls with different statuses) and, for a metered 2xx answer, without
    the provider's reported charge the endpoint declares. Whether this call's delivery kept it
    depended on settings and a request header that are not on file, so it is always left out: a
    field the caller may not have seen never takes part in a judgement."""
    response = dict(found["response"], status_code=status_code)
    if charged is not None and 200 <= status_code < 300:
        path = reported_charge.path_of(catalog_store.load().by_id.get(endpoint_id or ""))
        response["body_text"] = reported_charge.without(path, response["body_text"].encode("utf-8")).decode("utf-8")
    return dict(found, response=response)


def view(i: int, r, found: dict, capability: str, budget: int) -> dict:
    req, res = found["request"], found["response"]
    return {
        "i": i, "usefulness": r.usefulness, "reason": r.reason.strip(), "endpoint": r.endpoint_id,
        "capability": capability, "routed": r.routed_via is not None,
        "request": {"method": req["method"],
                    "url": body_view.shrink(req["url"], REQUEST_BUDGET),
                    "body": body_view.shrink(req["body_text"], REQUEST_BUDGET)},
        # Only what this call's delivery is known to have had: its own status and the body. The
        # stored media type and size describe whichever call first fetched these bytes.
        "response": {"status_code": res["status_code"], "body": body_view.shrink(res["body_text"], budget)},
    }
