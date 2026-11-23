"""Jev's judgements of review reasons, written once per review (`domain.feedback.review_codes`).

Two passes over the last `verdicts.WINDOW_DAYS`, newest first, each on its own: a failed request
or an unreadable answer leaves that review unjudged for the next run and never blocks the other
pass.

- Privacy: every reason that could be quoted (`verdicts.quotable`) gets `person_p`, the
  probability it names or identifies a person. The catalog quotes only reasons judged below
  `verdicts.PERSON_P_MAX`; an unjudged reason is never quoted.
- Attribution: every `partly` or `not_useful` review gets the codebook's `primary_code` and
  optional `secondary_code` under `CODEBOOK_VERSION`. A review without a reason is `NOREASON`
  without asking.

Run by `treg-worker reviews judge`. The table is small (one row per rated call), so each pass reads
its whole window's unjudged rows and writes back in batches of `review_judge_batch`.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import timedelta

from contextlib import asynccontextmanager

from sqlalchemy import or_, text
from sqlmodel import select

from ..config import get_settings
from ..domain.catalog import store as catalog_store
from ..domain.feedback import review_codes, reviews, verdicts
from ..infra import judge
from ..infra.db import session_maker
from ..models import CallReview
from ..timeutil import utcnow_naive

log = logging.getLogger("treg.review_judge")

NEGATIVE = ("partly", "not_useful")
# One run at a time (pg_try_advisory_lock), so overlapping runs never pay Jev twice for a review;
# any constant no other advisory lock uses. The conditional writes in `domain.feedback.reviews`
# keep the first answer even without it.
RUN_LOCK_KEY = 0x72766A67


def configured() -> bool:
    return bool(get_settings().typesafe_api_key)


async def _ask(state: dict, questions: dict[str, dict]) -> judge.Reply:
    s = get_settings()
    return await judge.ask(state, questions, api_key=s.typesafe_api_key, model=s.typesafe_model,
                           url=s.typesafe_url, timeout_s=float(s.review_judge_timeout_s))


def _batches(rows: list, size: int):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


async def _write_privacy(values: dict[int, float]) -> int:
    now, written = utcnow_naive(), 0
    async with session_maker() as db:
        for review_id, person_p in values.items():
            written += await reviews.record_privacy(db, review_id, person_p, now)
        await db.commit()
    return written


async def _write_codes(values: dict[int, tuple[str, str | None]]) -> int:
    now, written = utcnow_naive(), 0
    async with session_maker() as db:
        for review_id, (primary, secondary) in values.items():
            written += await reviews.record_codes(db, review_id, primary, secondary,
                                                  review_codes.CODEBOOK_VERSION, now)
        await db.commit()
    return written


@asynccontextmanager
async def _only_run():
    """Whether this run holds the run lock, on a connection of its own. SQLite: always."""
    async with session_maker.kw["bind"].connect() as connection:
        if connection.dialect.name != "postgresql":
            yield True
            return
        got = bool((await connection.execute(text("SELECT pg_try_advisory_lock(:key)"),
                                             {"key": RUN_LOCK_KEY})).scalar_one())
        await connection.commit()
        try:
            yield got
        finally:
            if got:
                await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": RUN_LOCK_KEY})
                await connection.commit()


async def judge_privacy(*, deadline: float) -> dict:
    since = utcnow_naive() - timedelta(days=verdicts.WINDOW_DAYS)
    async with session_maker() as db:
        rows = (await db.execute(
            select(CallReview.id, CallReview.reason)
            .where(CallReview.created_at >= since, CallReview.privacy_judged_at.is_(None),
                   CallReview.reason.is_not(None))
            .order_by(CallReview.created_at.desc()))).all()
    rows = [r for r in rows if verdicts.quotable(r.reason)]
    out = {"pending": len(rows), "judged": 0, "already_judged": 0, "failed_requests": 0, "unreadable": 0,
           "tokens_in": 0}
    for batch in _batches(rows, get_settings().review_judge_batch):
        if time.monotonic() >= deadline:
            break
        state = {"reviews": [{"i": i, "reason": r.reason.strip()} for i, r in enumerate(batch)]}
        reply = await _ask(state, {f"p{i}": review_codes.privacy_question(i) for i in range(len(batch))})
        if reply.answers is None:
            out["failed_requests"] += 1
            log.warning("review privacy judge abstained: %s", reply.error)
            continue
        out["tokens_in"] += reply.tokens_in or 0
        values = {}
        for i, r in enumerate(batch):
            p = review_codes.person_p_from(reply.answers.get(f"p{i}"))
            if p is None:
                out["unreadable"] += 1
            else:
                values[r.id] = p
        written = await _write_privacy(values)
        out["judged"] += written
        out["already_judged"] += len(values) - written
    return out


async def judge_codes(*, deadline: float) -> dict:
    since = utcnow_naive() - timedelta(days=verdicts.WINDOW_DAYS)
    version = review_codes.CODEBOOK_VERSION
    async with session_maker() as db:
        rows = (await db.execute(
            select(CallReview.id, CallReview.reason, CallReview.usefulness, CallReview.endpoint_id,
                   CallReview.routed_via)
            .where(CallReview.created_at >= since, CallReview.usefulness.in_(NEGATIVE),
                   or_(CallReview.codebook_version.is_(None), CallReview.codebook_version < version))
            .order_by(CallReview.created_at.desc()))).all()
    out = {"pending": len(rows), "judged": 0, "no_reason": 0, "already_judged": 0, "failed_requests": 0,
           "unreadable": 0, "tokens_in": 0}
    blank = {r.id: ("NOREASON", None) for r in rows if not (r.reason or "").strip()}
    out["no_reason"] = await _write_codes(blank)
    out["already_judged"] += len(blank) - out["no_reason"]
    rows = [r for r in rows if r.id not in blank]
    by_id = catalog_store.load().by_id
    for batch in _batches(rows, get_settings().review_judge_batch):
        if time.monotonic() >= deadline:
            break
        state = {"reviews": [{
            "i": i, "usefulness": r.usefulness, "reason": r.reason.strip(), "endpoint": r.endpoint_id,
            "capability": (by_id.get(r.endpoint_id) or {}).get("capability") or "",
            "routed": r.routed_via is not None,
        } for i, r in enumerate(batch)]}
        reply = await _ask(state, {f"a{i}": review_codes.code_question(i) for i in range(len(batch))})
        if reply.answers is None:
            out["failed_requests"] += 1
            log.warning("review code judge abstained: %s", reply.error)
            continue
        out["tokens_in"] += reply.tokens_in or 0
        values = {}
        for i, r in enumerate(batch):
            codes = review_codes.codes_from(reply.answers.get(f"a{i}"))
            if codes is None:
                out["unreadable"] += 1
                continue
            values[r.id] = codes
        written = await _write_codes(values)
        out["judged"] += written
        out["already_judged"] += len(values) - written
    return out


async def run(*, max_seconds: float) -> dict:
    """Both passes, sharing `max_seconds`: privacy first (it gates what the catalog may quote), then
    attribution with whatever time is left. Off (`{"skipped": "no_judge_key"}`) without a key, and
    `{"skipped": "another_run"}` while another run holds the lock."""
    if not configured():
        return {"skipped": "no_judge_key"}
    deadline = time.monotonic() + max_seconds
    out = {}
    async with _only_run() as got:
        if not got:
            return {"skipped": "another_run"}
        for name, judge_pass in (("privacy", judge_privacy), ("codes", judge_codes)):
            try:
                out[name] = await judge_pass(deadline=deadline)
            except Exception as exc:  # one pass failing never stops the other
                log.exception("review %s pass failed", name)
                out[name] = {"error": type(exc).__name__}
    return out


def dumps(result: dict) -> str:
    return json.dumps(result, sort_keys=True)
