"""Jev's blind verdicts on stored comparisons (`domain.feedback.compare_rubric`), apart from the
team's agent's vote.

A comparison that holds both answers (`ready`, or `decided` by an agent) on a capability in
`compare_judge_capabilities` is asked twice, `compare_judge_batch` pairs per Jev request: once with
A shown as answer 1, once with B. Both orders go out together; the pair is written only when both
answered (`compare_rubric.combine`: their verdict, or `split`). Decided pairs come first, so the
agents' votes calibrate Jev's from the first run. At most `compare_judge_per_run` pairs a run.

A pair Jev could not judge steps aside so the cap never stays on the same rows: an unreadable
answer backs off from an hour to a week, a failed request waits an hour without counting a try.
Each request gets only the time left before the deadline; one the deadline cut short stops the run
and its pairs wait for the next one, not counted as failed. Tokens are counted for every request
that answered, also when its other order failed.
Run by `treg-worker reviews judge` (`review_judge.run`), off with `compare_judge`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import case, func, or_
from sqlmodel import select

from ..config import get_settings
from ..domain.feedback import compare_rubric, comparisons
from ..infra import judge
from ..infra.db import session_maker
from ..models import CallComparison
from ..timeutil import utcnow_naive

log = logging.getLogger("treg.compare_judge")

FAILED_REQUEST_DELAY = timedelta(hours=1)


async def _ask(state: dict, questions: dict[str, dict], *, timeout_s: float) -> judge.Reply:
    s = get_settings()
    return await judge.ask(state, questions, api_key=s.typesafe_api_key, model=s.typesafe_model,
                           url=s.typesafe_url, timeout_s=timeout_s)


def _retry_at(now, attempts: int):
    """An hour, doubling with each failed try, up to a week."""
    return now + min(timedelta(hours=2 ** (attempts - 1)), timedelta(days=7))


def _cut(row, a_first: bool) -> list[str]:
    stored = set(filter(None, (row.truncated or "").split(",")))
    one, two = ("a", "b") if a_first else ("b", "a")
    return [name for name, part in (("input", "input"), ("answer_1", one), ("answer_2", two)) if part in stored]


def _state(batch: list, a_first: bool, chars: int) -> dict:
    return {"pairs": [compare_rubric.view(
        i, r.capability, r.input, *((r.a_body, r.b_body) if a_first else (r.b_body, r.a_body)),
        _cut(r, a_first), chars) for i, r in enumerate(batch)]}


async def judge_pairs(*, deadline: float) -> dict:
    s = get_settings()
    now = utcnow_naive()
    version = compare_rubric.VERSION
    unjudged = (CallComparison.status.in_((comparisons.READY, comparisons.DECIDED)),
                CallComparison.capability.in_(s.compare_judge_capabilities),
                or_(CallComparison.jev_version.is_(None), CallComparison.jev_version < version))
    async with session_maker() as db:
        pending = (await db.execute(select(func.count()).select_from(CallComparison).where(*unjudged))).scalar_one()
        rows = (await db.execute(
            select(CallComparison.id, CallComparison.capability, CallComparison.input, CallComparison.a_body,
                   CallComparison.b_body, CallComparison.truncated, CallComparison.jev_attempts)
            .where(*unjudged, or_(CallComparison.jev_retry_at.is_(None), CallComparison.jev_retry_at <= now))
            .order_by(case((CallComparison.status == comparisons.DECIDED, 0), else_=1),
                      CallComparison.created_at.desc())
            .limit(s.compare_judge_per_run))).all()
    out = {"pending": pending, "selected": len(rows), "judged": 0, "split": 0, "already_judged": 0,
           "failed_requests": 0, "unreadable": 0, "retry_later": 0, "tokens_in": 0,
           "stopped_by_deadline": False}

    async def step_aside(batch: list, failed: bool) -> None:
        at = utcnow_naive()
        async with session_maker() as db:
            for r in batch:
                retry_at = (at + FAILED_REQUEST_DELAY if failed
                            else _retry_at(at, (r.jev_attempts or 0) + 1))
                out["retry_later"] += await comparisons.record_jev_retry(
                    db, r.id, version, r.jev_attempts, retry_at, count=not failed)
            await db.commit()

    size = s.compare_judge_batch
    for start in range(0, len(rows), size):
        left = deadline - time.monotonic()
        if left <= 0:
            out["stopped_by_deadline"] = True
            break
        batch = rows[start:start + size]
        questions = {f"c{i}": compare_rubric.question(i) for i in range(len(batch))}
        timeout_s = min(float(s.review_judge_timeout_s), left)
        replies = await asyncio.gather(
            *(_ask(_state(batch, a_first, s.compare_judge_answer_chars), questions, timeout_s=timeout_s)
              for a_first in (True, False)))
        out["tokens_in"] += sum(reply.tokens_in or 0 for reply in replies if reply.answers is not None)
        if any(reply.answers is None for reply in replies):
            if time.monotonic() >= deadline:  # cut short by the run's end, not Jev's fault
                out["stopped_by_deadline"] = True
                break
            out["failed_requests"] += 1
            log.warning("comparison judge abstained: %s", "; ".join(str(r.error) for r in replies if r.answers is None))
            await step_aside(batch, True)
            continue
        answers = [reply.answers if isinstance(reply.answers, dict) else {} for reply in replies]
        verdicts, unread = {}, []
        for i, r in enumerate(batch):
            shown = [compare_rubric.choice_from(a.get(f"c{i}")) for a in answers]
            if None in shown:
                out["unreadable"] += 1
                unread.append(r)
                continue
            verdicts[r.id] = compare_rubric.combine(*shown)
        if unread:
            await step_aside(unread, False)
        at = utcnow_naive()
        async with session_maker() as db:
            for comparison_id, (choice, orders) in verdicts.items():
                written = await comparisons.record_jev(db, comparison_id, choice=choice, orders=orders,
                                                       version=version, at=at)
                out["judged"] += written
                out["already_judged"] += not written
                out["split"] += written and choice == compare_rubric.SPLIT
            await db.commit()
    return out
