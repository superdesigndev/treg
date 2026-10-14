"""Which optional hint, if any, rides on a successful call's answer.

Decided once, server-side, after the upstream has answered and before the body streams; every
surface (plain HTTP, CLI, both MCP transports) only translates the resulting `X-Treg-Hint` header.
A review invitation asks the agent for work, so it is rationed twice: `hints.sampled` picks WHICH
calls qualify (deterministic, so `POST /reviews` can recompute eligibility), and a per-team hourly
budget in the shared store caps HOW MANY a team is asked about, whatever its call volume. A team
hammering one endpoint 2,000 times in an hour is asked `review_budget_per_hour` times, not 100.
The feedback hint asks for nothing and stays purely sampled.
"""
from __future__ import annotations

from typing import Literal

from ... import hints
from ...config import get_settings
from ...infra import kv
from .types import CallContext

Kind = Literal["review", "feedback"]
BUDGET_WINDOW_S = 3600


def budget_key(org_id: int) -> str:
    return f"review-budget:{org_id}"


async def invitation(context: CallContext, status: int, *, replayed: bool) -> Kind | None:
    """No database, no body access. Phase 1 invites reviews only on direct catalog calls served
    on treg's platform key; an own tool never qualifies even when its name matches an endpoint."""
    if not (200 <= status < 300) or replayed or context.cached:
        return None
    marketplace = context.marketplace
    platform = marketplace is not None and getattr(marketplace, "tier", None) == "platform"
    if platform and hints.sampled("review", context.call_ref):
        if await kv.store().take(budget_key(context.input.caller.org_id),
                                 get_settings().review_budget_per_hour, BUDGET_WINDOW_S):
            return "review"
    if hints.sampled("feedback", context.call_ref):
        return "feedback"
    return None
