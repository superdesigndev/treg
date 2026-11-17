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

import hashlib
import json

from ... import hints
from ...config import get_settings
from ...infra import kv
from .. import call_events
from .types import CallContext

Kind = Literal["review", "mark", "feedback"]
BUDGET_WINDOW_S = 3600


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
    # A mark asks for later work, only once the agent knows; capped per team like a review.
    if _markable(catalog_endpoint(context)) and hints.sampled("mark", context.call_ref):
        if await kv.store().take(mark_budget_key(context.input.caller.org_id),
                                 get_settings().mark_hint_per_hour, BUDGET_WINDOW_S):
            return "mark"
    if hints.sampled("feedback", context.call_ref):
        return "feedback"
    return None


def _found_key(context: CallContext, email: object) -> str | None:
    """The store key for an address one team and pin found: an HMAC, never the address. The pin is
    part of it, so a check by one pinned agent never names another pin's call."""
    normal = call_events.normalize("email", email) if isinstance(email, str) else None
    if not normal:
        return None
    caller = context.input.caller
    pin = json.dumps(caller.membership.pinned_tags or {}, sort_keys=True)
    scope = hashlib.sha256(pin.encode()).hexdigest()[:16]
    return f"mark-found:{caller.org_id}:{scope}:{call_events.keyed(normal)}"


def _routed(context: CallContext, capability: str, status: int, replayed: bool) -> dict | None:
    summary = context.route_summary
    if (summary is None or not (200 <= status < 300) or replayed
            or not (context.endpoint_id or "").endswith(f".{capability}")):
        return None
    return summary


async def remember_found(context: CallContext, status: int, *, replayed: bool) -> None:
    """A routed email find that returned an address: note which call found it, for a later check.
    No database, no body access; a store fault only loses a later invitation."""
    if not get_settings().mark_evidence_hint:
        return
    summary = _routed(context, "people.email.find", status, replayed)
    if summary is None or summary.get("hit") is not True:
        return
    key = _found_key(context, (summary.get("output") or {}).get("email"))
    if key is not None:
        await kv.store().note_found(key, context.call_ref, get_settings().mark_evidence_ttl_s)


async def evidence_invitation(context: CallContext, status: int, *, replayed: bool) -> str | None:
    """A routed email check that answered `invalid` for an address this team and pin found through
    treg: the text inviting a mark on that find, with the check as evidence. Else None."""
    settings = get_settings()
    if not settings.mark_evidence_hint:
        return None
    summary = _routed(context, "people.email.verify", status, replayed)
    if summary is None or summary.get("answer_verdict") != "invalid":
        return None
    key = _found_key(context, (summary.get("identity") or {}).get("email"))
    if key is None:
        return None
    found = await kv.store().found_by(key)
    if not found or found == context.call_ref:
        return None
    if not await kv.store().take(f"mark-evidence-budget:{context.input.caller.org_id}",
                                 settings.mark_evidence_per_hour, BUDGET_WINDOW_S):
        return None
    return hints.mark_evidence_hint(context.call_ref, found)
