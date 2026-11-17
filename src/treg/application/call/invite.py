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
from .. import review_targets
from ...config import get_settings
from ...infra import kv
from .types import CallContext

Kind = Literal["review", "mark", "feedback"]
BUDGET_WINDOW_S = 3600
# What a client that does not say (`X-Treg-Hints`) can show: every CLI released before the
# header existed renders review and feedback, and drops anything else in silence.
LEGACY_KINDS: frozenset[str] = frozenset({"review", "feedback"})


def accepted_kinds(header: str | None) -> frozenset[str]:
    """The invitations a client says it shows (`X-Treg-Hints: review,mark,feedback`)."""
    if header is None:
        return LEGACY_KINDS
    return frozenset(k.strip().lower() for k in header.split(",") if k.strip())


def budget_key(org_id: int) -> str:
    return f"review-budget:{org_id}"


def mark_budget_key(org_id: int) -> str:
    return f"mark-hint-budget:{org_id}"


def catalog_endpoint(context: CallContext) -> str | None:
    """The catalog endpoint a call answered for: a direct row's id, or a routed parent's
    (`treg.<capability>`), which runs no marketplace target of its own. None for own tools."""
    if context.marketplace is not None:
        return getattr(context.marketplace, "endpoint_id", None)
    if context.route_summary is not None:
        return context.endpoint_id
    return None


def _markable(endpoint_id: str | None) -> bool:
    capabilities = get_settings().mark_hint_capabilities
    if not endpoint_id:
        return False
    return not capabilities or any(endpoint_id == c or endpoint_id.endswith(f".{c}")
                                   for c in capabilities)


def review_endpoint(context: CallContext) -> str | None:
    """The endpoint a review of this call would be credited to: a direct call's own, or the
    child that answered a routed one (`served_by`), as application.feedback attributes it."""
    if context.marketplace is not None:
        return getattr(context.marketplace, "endpoint_id", None)
    if context.route_summary is not None:
        return context.route_summary.get("served_by")
    return None


def reviewable(credential_tier: str | None) -> bool:
    """Calls a review is invited on: direct catalog calls on treg's platform key, and routed
    calls, whose review is credited to the child that answered (application.feedback)."""
    return credential_tier in ("platform", "routed")


async def invitation(context: CallContext, status: int, *, replayed: bool,
                     accepts: frozenset[str] = LEGACY_KINDS) -> Kind | None:
    """No database, no body access. A kind the client does not show is skipped without spending
    its budget, so the call falls through to one it does; an own tool never qualifies for a
    review even when its name matches an endpoint."""
    if not (200 <= status < 300) or replayed or context.cached:
        return None
    marketplace = context.marketplace
    platform = marketplace is not None and getattr(marketplace, "tier", None) == "platform"
    routed = marketplace is None and context.route_summary is not None
    if ("review" in accepts and (platform or routed)
            and review_targets.sampled(context.call_ref, review_endpoint(context))):
        if await kv.store().take(budget_key(context.input.caller.org_id),
                                 get_settings().review_budget_per_hour, BUDGET_WINDOW_S):
            return "review"
    # A mark asks for later work, only once the agent knows; capped per team like a review.
    if ("mark" in accepts and _markable(catalog_endpoint(context))
            and hints.sampled("mark", context.call_ref)):
        if await kv.store().take(mark_budget_key(context.input.caller.org_id),
                                 get_settings().mark_hint_per_hour, BUDGET_WINDOW_S):
            return "mark"
    if "feedback" in accepts and hints.sampled("feedback", context.call_ref):
        return "feedback"
    return None
