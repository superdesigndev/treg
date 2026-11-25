"""A dry run of coding negative reviews with the vendor's answer in view, against the reason-only codes.

The hourly judge (`application.review_judge`) codes a `partly` or `not_useful` review from its
reason alone. This pilot takes the newest such reviews already coded under the current codebook
whose call still has its answer on file (`archive.resolve_result`), asks Jev the same codebook
with the request and a shortened answer beside the reason (`review_codes.body_code_question`,
`body_view.shrink`), and reports how the two codings compare.

It writes nothing and prints aggregates only: counts, code pairs, tokens, and the review ids
where the codings differ, never a reason, a request or an answer. Run by
`treg-worker reviews body-pilot`.
"""
from __future__ import annotations

import time
from collections import Counter
from datetime import timedelta

from sqlmodel import select

from .. import archive
from ..domain.catalog import store as catalog_store
from ..domain.feedback import body_view, review_codes
from ..infra.db import session_maker
from ..models import CallRecord, CallReview
from ..timeutil import utcnow_naive
from .review_judge import NEGATIVE, _ask, configured

WINDOW_DAYS = 28
REQUEST_BUDGET = 800
DIFFS_SHOWN = 100


async def _candidates(limit: int) -> list:
    since = utcnow_naive() - timedelta(days=WINDOW_DAYS)
    async with session_maker() as db:
        return (await db.execute(
            select(CallReview.id, CallReview.reason, CallReview.usefulness, CallReview.endpoint_id,
                   CallReview.routed_via, CallReview.primary_code, CallReview.secondary_code,
                   CallRecord.archive_key_hash, CallRecord.archive_content_hash)
            .join(CallRecord, CallRecord.call_ref == CallReview.call_id)
            .where(CallReview.created_at >= since, CallRecord.created_at >= since - timedelta(days=1),
                   CallReview.usefulness.in_(NEGATIVE), CallReview.reason.is_not(None),
                   CallReview.codebook_version == review_codes.CODEBOOK_VERSION,
                   CallReview.primary_code.is_not(None), CallReview.primary_code != "NOREASON",
                   CallRecord.archive_key_hash.is_not(None), CallRecord.archive_content_hash.is_not(None))
            .order_by(CallReview.created_at.desc()).limit(limit))).all()


def _item(i: int, r, found: dict, capability: str, budget: int) -> dict:
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


async def run(*, sample: int, batch: int, budget: int, max_seconds: float) -> dict:
    """Code up to `sample` reviews with their answers in view, `batch` per Jev request, each answer
    cut to `budget` characters. Off (`{"skipped": "no_judge_key"}`) without a key."""
    if not configured():
        return {"skipped": "no_judge_key"}
    deadline = time.monotonic() + max_seconds
    by_id = catalog_store.load().by_id
    rows = await _candidates(sample * 3)
    out = {"candidates": len(rows), "no_body": 0, "asked": 0, "coded": 0, "unreadable": 0,
           "failed_requests": 0, "tokens_in": 0, "same_primary": 0, "primary_in_either": 0,
           "stopped_by_deadline": False}
    pairs, reason_codes, body_codes = Counter(), Counter(), Counter()
    by_capability: dict[str, Counter] = {}
    diffs: list[dict] = []
    ready: list[tuple] = []

    async def ask(items: list[tuple]) -> None:
        state = {"reviews": [item for _, item in items]}
        out["asked"] += len(items)
        reply = await _ask(state, {f"a{i}": review_codes.body_code_question(i) for i in range(len(items))})
        if reply.answers is None:
            out["failed_requests"] += 1
            return
        out["tokens_in"] += reply.tokens_in or 0
        for i, (r, item) in enumerate(items):
            codes = review_codes.codes_from(reply.answers.get(f"a{i}"))
            if codes is None:
                out["unreadable"] += 1
                continue
            body_primary, body_secondary = codes
            out["coded"] += 1
            reason_codes[r.primary_code] += 1
            body_codes[body_primary] += 1
            tally = by_capability.setdefault(item["capability"] or "unknown", Counter())
            tally["coded"] += 1
            if body_primary == r.primary_code:
                out["same_primary"] += 1
                tally["same_primary"] += 1
            else:
                pairs[f"{r.primary_code}->{body_primary}"] += 1
                if len(diffs) < DIFFS_SHOWN:
                    diffs.append({"review_id": r.id, "reason_code": r.primary_code,
                                  "body_code": body_primary, "body_secondary": body_secondary})
            if body_primary in (r.primary_code, r.secondary_code) or r.primary_code == body_secondary:
                out["primary_in_either"] += 1

    for r in rows:
        if out["asked"] + len(ready) >= sample:
            break
        if time.monotonic() >= deadline:
            out["stopped_by_deadline"] = True
            break
        found = await archive.resolve_result(r.archive_key_hash, r.archive_content_hash)
        if not found or not found["stored"] or found["response"]["body_text"] is None:
            out["no_body"] += 1
            continue
        capability = (by_id.get(r.endpoint_id) or {}).get("capability") or ""
        ready.append((r, _item(len(ready), r, found, capability, budget)))
        if len(ready) == batch:
            await ask(ready)
            ready = []
    if ready and time.monotonic() < deadline:
        await ask(ready)
    out["pairs"] = dict(pairs.most_common())
    out["reason_codes"] = dict(reason_codes.most_common())
    out["body_codes"] = dict(body_codes.most_common())
    out["by_capability"] = {c: dict(t) for c, t in sorted(by_capability.items(), key=lambda kv: -kv[1]["coded"])}
    out["diffs"] = diffs
    return out
