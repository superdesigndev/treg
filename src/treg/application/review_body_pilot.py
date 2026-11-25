"""A dry run of coding negative reviews with the vendor's answer in view, against the reason-only codes.

It asks Jev the codebook twice for the newest `partly` and `not_useful` reviews whose direct call
still has its answer on file (`review_answers.answer_of`; a routed review is counted as
`answer_unconfirmed`): from the reason alone (`review_codes.code_question`), and with the request and
the shortened answer beside it (`review_codes.body_code_question`, `body_view.shrink`), and reports
how the two codings compare. The reason-only coding is asked afresh, never read from the review
row, which the hourly judge (`application.review_judge`) codes with the answer.

It writes nothing and prints aggregates only: counts, code pairs, tokens, and the review ids
where the codings differ, never a reason, a request or an answer. Run by
`treg-worker reviews body-pilot`.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import timedelta

from sqlalchemy import and_, or_
from sqlmodel import select

from ..domain.catalog import store as catalog_store
from ..domain.feedback import review_codes
from ..infra.db import session_maker
from ..models import CallReview
from ..timeutil import utcnow_naive
from .review_answers import answer_of, view
from .review_judge import NEGATIVE, _ask, configured

WINDOW_DAYS = 28
DIFFS_SHOWN = 100
PAGE = 200


async def _candidates(since, after: tuple | None, limit: int) -> list:
    """One page of reviews to try, newest first in a stable order (created_at, then id), after the
    last row of the previous page."""
    page = (select(CallReview.id, CallReview.created_at, CallReview.org_id, CallReview.user_email, CallReview.call_id,
                   CallReview.reason, CallReview.usefulness, CallReview.endpoint_id, CallReview.routed_via)
            .where(CallReview.created_at >= since, CallReview.usefulness.in_(NEGATIVE),
                   CallReview.reason.is_not(None))
            .order_by(CallReview.created_at.desc(), CallReview.id.desc()).limit(limit))
    if after is not None:
        at, review_id = after
        page = page.where(or_(CallReview.created_at < at,
                              and_(CallReview.created_at == at, CallReview.id < review_id)))
    async with session_maker() as db:
        return (await db.execute(page)).all()


async def run(*, sample: int, batch: int, budget: int, max_seconds: float) -> dict:
    """Code up to `sample` reviews with their answers in view, `batch` per Jev request, each answer
    cut to `budget` characters. Off (`{"skipped": "no_judge_key"}`) without a key."""
    if not configured():
        return {"skipped": "no_judge_key"}
    deadline = time.monotonic() + max_seconds
    by_id = catalog_store.load().by_id
    since = utcnow_naive() - timedelta(days=WINDOW_DAYS)
    out = {"candidates": 0, "no_reason": 0, "answer_unconfirmed": 0, "no_body": 0, "asked": 0, "coded": 0, "unreadable": 0,
           "failed_requests": 0, "read_not_asked": 0, "half_asked": 0, "tokens_in": 0, "same_primary": 0, "primary_in_either": 0, "changed": 0,
           "stopped_by_deadline": False}
    pairs, reason_codes, body_codes = Counter(), Counter(), Counter()
    by_capability: dict[str, Counter] = {}
    diffs: list[dict] = []
    ready: list[tuple] = []

    async def ask(items: list[tuple]) -> None:
        # Reading an answer can start before the deadline and end after it: no paid request then.
        if time.monotonic() >= deadline:
            out["stopped_by_deadline"] = True
            out["read_not_asked"] += len(items)
            return
        # The reason-only control is asked here too, never read from the review row: the hourly
        # judge codes rows with their answer, so a stored code is no control.
        reason_state = {"reviews": [{k: item[k] for k in ("i", "usefulness", "reason", "endpoint", "capability",
                                                          "routed")} for _, item in items]}
        replies = []
        for state, question in (({"reviews": [item for _, item in items]}, review_codes.body_code_question),
                                (reason_state, review_codes.code_question)):
            if time.monotonic() >= deadline:  # no paid request after it; a half-asked batch is not paired
                out["stopped_by_deadline"] = True
                out["half_asked" if replies else "read_not_asked"] += len(items)
                return
            if not replies:
                out["asked"] += len(items)
            reply = await _ask(state, {f"a{i}": question(i) for i in range(len(items))})
            if reply.answers is None:
                out["failed_requests"] += 1
                return
            out["tokens_in"] += reply.tokens_in or 0
            replies.append(reply.answers if isinstance(reply.answers, dict) else {})
        body_answers, reason_answers = replies
        for i, (r, item) in enumerate(items):
            body = review_codes.codes_from(body_answers.get(f"a{i}"))
            reason = review_codes.codes_from(reason_answers.get(f"a{i}"))
            if body is None or reason is None:
                out["unreadable"] += 1
                continue
            (body_primary, body_secondary), (reason_primary, reason_secondary) = body, reason
            out["coded"] += 1
            reason_codes[reason_primary] += 1
            body_codes[body_primary] += 1
            tally = by_capability.setdefault(item["capability"] or "unknown", Counter())
            tally["coded"] += 1
            if body_primary == reason_primary:
                out["same_primary"] += 1
                tally["same_primary"] += 1
            else:
                pairs[f"{reason_primary}->{body_primary}"] += 1
            if body != reason:
                out["changed"] += 1
                if len(diffs) < DIFFS_SHOWN:
                    diffs.append({"review_id": r.id, "reason_codes": [reason_primary, reason_secondary],
                                  "body_codes": [body_primary, body_secondary]})
            if body_primary in reason or reason_primary == body_secondary:
                out["primary_in_either"] += 1

    # Page through the window until enough answers were read, the window ends or time runs out:
    # how many reviews still have their answer on file is not known in advance.
    after, done = None, False
    while not done:
        page = await _candidates(since, after, PAGE)
        if not page:
            break
        after = (page[-1].created_at, page[-1].id)
        for r in page:
            if out["asked"] + len(ready) >= sample:
                done = True
                break
            if time.monotonic() >= deadline:
                out["stopped_by_deadline"] = done = True
                break
            out["candidates"] += 1
            if not r.reason.strip():  # coded NOREASON without asking; nothing to read beside the answer
                out["no_reason"] += 1
                continue
            if r.routed_via is not None:  # which attempt's answer it got is not provable (`_answer`)
                out["answer_unconfirmed"] += 1
                continue
            answer = await answer_of(r)
            if answer.state != "read":
                out["no_body"] += 1
                continue
            found = answer.found
            capability = (by_id.get(r.endpoint_id) or {}).get("capability") or ""
            ready.append((r, view(len(ready), r, found, capability, budget)))
            if len(ready) == batch:
                await ask(ready)
                ready = []
        # No further page when this one filled the sample or was the window's last: the ready tail
        # goes out now, not after a query that could carry it past the deadline.
        if out["asked"] + len(ready) >= sample or len(page) < PAGE:
            break
    if ready:
        await ask(ready)
    out["pairs"] = dict(pairs.most_common())
    out["reason_codes"] = dict(reason_codes.most_common())
    out["body_codes"] = dict(body_codes.most_common())
    out["by_capability"] = {c: dict(t) for c, t in sorted(by_capability.items(), key=lambda kv: -kv[1]["coded"])}
    out["diffs"] = diffs
    return out
