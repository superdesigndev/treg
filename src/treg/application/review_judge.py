"""Jev's judgements of review reasons, written once per review (`domain.feedback.review_codes`).

Two passes over the last `verdicts.WINDOW_DAYS`, newest first, each on its own: a failed request
or an unreadable answer leaves that review unjudged for the next run and never blocks the other
pass.

- Privacy: every reason that could be quoted (`verdicts.quotable`) gets `person_p`, the
  probability it names or identifies a person. The catalog quotes only reasons judged below
  `verdicts.PERSON_P_MAX`; an unjudged reason is never quoted.
- Attribution: every `partly` or `not_useful` review gets the codebook's `primary_code` and
  optional `secondary_code` under `CODEBOOK_VERSION` from its reason (`code_basis`), and a direct
  call's review is then upgraded with the call's answer in view when it can be read
  (`judge_answers`). A review without a reason is `NOREASON` without asking.

Run by `treg-worker reviews judge`. The table is small (one row per rated call), so each pass reads
its whole window's unjudged rows and writes back in batches of `review_judge_batch`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import timedelta

from sqlalchemy import or_
from sqlmodel import select

from ..config import get_settings
from ..domain.catalog import store as catalog_store
from ..domain.feedback import review_codes, reviews, verdicts
from ..infra import judge
from ..infra.db import session_maker
from ..models import CallReview
from ..timeutil import utcnow_naive
from .review_answers import Answer, answer_of, view

log = logging.getLogger("treg.review_judge")

NEGATIVE = ("partly", "not_useful")
_wait_for = asyncio.wait_for  # a seam for tests that run on a fake clock
BUDGET_CUT_DELAY = timedelta(hours=6)


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


async def _write_codes(values: dict[int, tuple[str, str | None]], bases: dict[int, str]) -> int:
    now, written = utcnow_naive(), 0
    async with session_maker() as db:
        for review_id, (primary, secondary) in values.items():
            written += await reviews.record_codes(db, review_id, primary, secondary,
                                                  review_codes.CODEBOOK_VERSION, now,
                                                  basis=bases.get(review_id, reviews.REASON))
        await db.commit()
    return written


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
    """Code every negative review not yet coded under `CODEBOOK_VERSION`, from its reason. A direct
    call's review is `reason` (`judge_answers` may upgrade it with the answer); a routed call's,
    whose served answer its rows cannot prove, or one without a reason, is `reason_final`."""
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
    final = {r.id: reviews.REASON_FINAL for r in rows if r.routed_via is not None or not (r.reason or "").strip()}
    blank = {r.id: ("NOREASON", None) for r in rows if not (r.reason or "").strip()}
    out["no_reason"] = await _write_codes(blank, final)
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
        answers = reply.answers if isinstance(reply.answers, dict) else {}
        values = {}
        for i, r in enumerate(batch):
            codes = review_codes.codes_from(answers.get(f"a{i}"))
            if codes is None:
                out["unreadable"] += 1
                continue
            values[r.id] = codes
        written = await _write_codes(values, final)
        out["judged"] += written
        out["already_judged"] += len(values) - written
    return out


def _retry_at(now, attempts: int):
    """An hour, doubling with each failed read, up to a week."""
    return now + min(timedelta(hours=2 ** (attempts - 1)), timedelta(days=7))


async def judge_answers(*, deadline: float) -> dict:
    """Upgrade direct reviews coded from their reason with their call's answer in view
    (`review_answers.answer_of`, `review_codes.body_code_question`), `review_judge_answer_batch` per
    Jev request. An answer gone for good, or a delivery the archive cannot show, makes the reason
    codes final. One that cannot be read now is tried again later, backing off from an hour to a
    week, and after `review_judge_answer_attempts` tries the reason codes stand: so a review whose
    answer is unreadable is skipped until its time, and never holds back the others.

    Reading stops with a request's worth of time left before the deadline, each read gets only the
    time left, and no request starts after the deadline, so what was read is sent."""
    s = get_settings()
    now = utcnow_naive()
    since = now - timedelta(days=verdicts.WINDOW_DAYS)
    version = review_codes.CODEBOOK_VERSION
    async with session_maker() as db:
        rows = (await db.execute(
            select(CallReview.id, CallReview.org_id, CallReview.user_email, CallReview.call_id,
                   CallReview.reason, CallReview.usefulness, CallReview.endpoint_id, CallReview.routed_via,
                   CallReview.answer_attempts)
            .where(CallReview.created_at >= since, CallReview.usefulness.in_(NEGATIVE),
                   CallReview.routed_via.is_(None), CallReview.codebook_version == version,
                   or_(CallReview.code_basis.is_(None), CallReview.code_basis == reviews.REASON),
                   or_(CallReview.answer_retry_at.is_(None), CallReview.answer_retry_at <= now))
            .order_by(CallReview.created_at.desc()))).all()
    rows = [r for r in rows if (r.reason or "").strip()]
    out = {"pending": len(rows), "judged": 0, "final": 0, "retry_later": 0, "failed_requests": 0,
           "unreadable": 0, "tokens_in": 0, "stopped_by_deadline": False}
    by_id = catalog_store.load().by_id
    read_until = deadline - min(s.review_judge_timeout_s, max(deadline - time.monotonic(), 0) / 4)

    async def ask(batch: list[tuple]) -> None:
        if time.monotonic() >= deadline:
            out["stopped_by_deadline"] = True
            return
        reply = await _ask({"reviews": [item for _, item in batch]},
                           {f"a{i}": review_codes.body_code_question(i) for i in range(len(batch))})
        if reply.answers is None:
            out["failed_requests"] += 1
            log.warning("review answer judge abstained: %s", reply.error)
            return
        out["tokens_in"] += reply.tokens_in or 0
        answers = reply.answers if isinstance(reply.answers, dict) else {}
        at = utcnow_naive()
        async with session_maker() as db:
            for i, (r, _) in enumerate(batch):
                codes = review_codes.codes_from(answers.get(f"a{i}"))
                if codes is None:
                    out["unreadable"] += 1
                    continue
                out["judged"] += await reviews.record_answer_codes(db, r.id, *codes, version, at)
            await db.commit()

    async def settle(r, state: str) -> None:
        async with session_maker() as db:
            attempts = (r.answer_attempts or 0) + 1
            if state == "absent" or attempts >= s.review_judge_answer_attempts:
                out["final"] += await reviews.record_answer_final(db, r.id, version)
            else:
                out["retry_later"] += await reviews.record_answer_retry(
                    db, r.id, version, attempts, _retry_at(utcnow_naive(), attempts))
            await db.commit()

    read: list[tuple] = []
    for r in rows:
        if time.monotonic() >= read_until:
            out["stopped_by_deadline"] = True
            break
        try:  # one slow read cannot carry the run past the point where what was read is sent
            answer = await _wait_for(answer_of(r), max(read_until - time.monotonic(), 0))
        except TimeoutError:
            # Cut by this run's budget, not a failure: no try is spent, but it waits a few hours so
            # the next runs reach the reviews after it.
            out["stopped_by_deadline"] = True
            async with session_maker() as db:
                await reviews.record_answer_retry(db, r.id, version, r.answer_attempts or 0,
                                                  utcnow_naive() + BUDGET_CUT_DELAY)
                await db.commit()
            break
        item = None
        if answer.state == "read":
            capability = (by_id.get(r.endpoint_id) or {}).get("capability") or ""
            try:
                item = view(len(read), r, answer.found, capability, s.review_judge_answer_chars)
            except Exception:  # a body that cannot be shortened fails that review only
                log.warning("review answer could not be shortened: review %s", r.id, exc_info=True)
                answer = Answer("unreadable")
        if item is None:
            await settle(r, answer.state)
            continue
        read.append((r, item))
        if len(read) == s.review_judge_answer_batch:
            await ask(read)
            read = []
    if read:
        await ask(read)
    return out


async def run(*, max_seconds: float) -> dict:
    """The passes, sharing `max_seconds`: privacy first (it gates what the catalog may quote), then
    attribution from reasons, then the upgrade with answers, with whatever time is left. Off (`{"skipped": "no_judge_key"}`) without a key.

    No lock: no connection is held across a Jev request (each pass reads its rows in a session it
    closes before asking, and each batch writes in a session of its own), and the writes in `domain.feedback.reviews` keep the first answer. An
    hourly cron capped well under the hour does not overlap itself; a hand-run backfill that does
    only asks Jev twice about some reviews."""
    if not configured():
        return {"skipped": "no_judge_key"}
    deadline = time.monotonic() + max_seconds
    out = {}
    for name, judge_pass in (("privacy", judge_privacy), ("codes", judge_codes), ("answers", judge_answers)):
        try:
            out[name] = await judge_pass(deadline=deadline)
        except Exception as exc:  # one pass failing never stops the other
            log.exception("review %s pass failed", name)
            out[name] = {"error": type(exc).__name__}
    return out


def dumps(result: dict) -> str:
    return json.dumps(result, sort_keys=True)
