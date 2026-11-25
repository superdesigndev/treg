"""The vendor answer a review was written about, from the archive, shortened for a judge to read.

Used by the hourly judge (`review_judge`), the body pilot and the body audit alike, so every judge
of a review's answer sees the same thing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlmodel import select

from .. import archive
from ..domain.feedback import body_view
from ..infra.db import session_maker
from ..models import CallRecord

log = logging.getLogger("treg.review_answers")

REQUEST_BUDGET = 800


@dataclass(frozen=True)
class Answer:
    """`read`: `found` is the answer as delivered. `absent`: none is on file for good (no archive
    link, the key or answer expired, a hash-only version, or bytes that cannot be decoded), so the
    reason is all a judge has. `unreadable`: the bytes are on file but could not be read now (object
    storage unavailable or a failed read), so the review should wait for a later run."""
    state: str
    found: dict | None = None
    error: bool = False


ABSENT = Answer("absent")


async def answer_of(r) -> Answer:
    """The answer the reviewed direct call got (`r.routed_via` is None; the caller counts routed
    reviews apart: their rows cannot prove which attempt's answer was returned). A read that raises
    (a corrupt snapshot) is `absent` with `error`, so one bad row never stops the others."""
    async with session_maker() as db:
        record = (await db.execute(select(CallRecord).where(
            CallRecord.org_id == r.org_id, CallRecord.call_ref == r.call_id,
        ).order_by(CallRecord.id.asc()).limit(1))).scalar_one_or_none()
    if record is None or record.archive_key_hash is None or record.archive_content_hash is None:
        return ABSENT
    link = (record.archive_key_hash, record.archive_content_hash)
    try:
        found = await archive.resolve_result(*link)
        if found is None:
            return ABSENT
        if found["stored"]:
            if found["response"]["body_text"] is None:  # not UTF-8 text
                return ABSENT
            return Answer("read", as_delivered(found, record.status_code, record.endpoint_id,
                                               record.cost_charged_micro))
        async with session_maker() as db:
            on_file = await archive.bytes_on_file(db, {link})
    except Exception:  # one unreadable snapshot is that review's problem only
        log.warning("review answer unreadable: review %s", r.id, exc_info=True)
        return Answer("absent", error=True)
    return Answer("unreadable") if on_file else ABSENT


def as_delivered(found: dict, status_code: int, endpoint_id: str | None, charged: int | None) -> dict:
    """The archived answer as the reviewed call delivered it: that call's own status (one stored
    body can answer several calls with different statuses) and, for a metered 2xx answer under
    `strip_reported_charge`, without the provider's reported charge, as `/calls/{id}/result` shows
    it. Whether the request kept that charge is not on file, so it is always left out: a field the
    caller may not have seen never takes part in a judgement."""
    from .call import service as call_service

    response = dict(found["response"], status_code=status_code)
    if charged is not None and 200 <= status_code < 300:
        response["body_text"] = call_service.answer_for_caller(
            endpoint_id, response["body_text"].encode("utf-8"), {}).decode("utf-8")
    return dict(found, response=response)


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
