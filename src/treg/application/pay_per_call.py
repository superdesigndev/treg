"""Pay per call: a `/call/` with no treg token pays that one call's price over x402 or MPP.

The call runs as the pay-per-call team on its prepaid balance like any metered call. The payment
is validated before the call and taken only when the call settled a charge; taking it tops the
team's balance back up by what the payer paid. See docs/context/architecture/pay-per-call.md.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from ..config import get_settings
from ..domain import money as ledger
from ..domain.catalog import store as catalog_store
from ..infra.db import session_maker
from .call.resolve import _platform_estimate_micro

log = logging.getLogger(__name__)

# A price is offered only when it cannot depend on the request or the answer: one fixed figure per
# call. Any other key (reported charges, tables, settle rules, usage meters, modifiers) prices a
# call from what it asked for or what came back, which a payment fixed up front cannot follow.
_FIXED_PRICE_KEYS = frozenset({"type", "value", "currency", "per", "unit", "source", "source_url",
                               "checked", "confidence", "note", "usd"})

# Caller headers that carry the payment or treg's own controls, never relayed: the payment
# credential is treg's, an idempotency label would be shared by every payer of the one team, and
# tags, routing and ceilings belong to signed-in teams.
_PAYMENT_HEADERS = frozenset({b"payment-signature", b"payment-authorization", b"idempotency-key"})


@dataclass(frozen=True)
class Offer:
    endpoint_id: str
    price_micro: int


class NotPayable(Exception):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def offer_for(rest: str, http_method: str) -> Offer:
    """The price to pay for this call, or why it cannot be paid per call."""
    floor = get_settings().pay_per_call_min_price_micro
    catalog = catalog_store.load()
    ep = catalog.by_id.get(rest) if "/" not in rest and "." in rest else None
    if ep is None:
        raise NotPayable("not_a_catalog_tool",
                         "only catalog tools can be paid per call; sign in to call anything else")
    endpoint_id = ep["id"]
    if str(ep.get("method", "GET")).upper() != http_method.upper():
        raise NotPayable("method_not_allowed", f"{endpoint_id} is called with {ep.get('method')}")
    cost = ep.get("cost") if isinstance(ep.get("cost"), dict) else {}
    fixed = (
        ep.get("kind") != "routed"
        and not ep.get("async")
        and not ep.get("spooled_response")
        and not ep.get("ceiling_param")
        and cost.get("type") in ("per_call", "per_success")
        and cost.get("unit", "call") == "call"
        and (cost.get("per") or 1) == 1
        and set(cost) <= _FIXED_PRICE_KEYS
    )
    if not catalog.platform_eligible(ep) or not fixed:
        raise NotPayable("variable_price",
                         f"{endpoint_id} has no fixed price per call; sign in to call it")
    view = catalog.cost_view(cost, ep.get("provider")) or {}
    price = ledger.with_margin(_platform_estimate_micro(view, {}))
    if price <= 0:
        raise NotPayable("free", f"{endpoint_id} is free; sign in to call it")
    if price < floor:
        raise NotPayable("below_minimum",
                         f"{endpoint_id} costs ${ledger.usd(price):g}, under the "
                         f"${ledger.usd(floor):g} minimum paid per call; sign in to call it")
    return Offer(endpoint_id=endpoint_id, price_micro=price)


def forwarded_headers(raw_headers) -> tuple[tuple[bytes, bytes], ...]:
    """The caller's headers without the payment and treg's controls."""
    kept = []
    for key, value in raw_headers:
        name = key.lower()
        if name in _PAYMENT_HEADERS or name.startswith(b"x-treg-"):
            continue
        if name == b"authorization" and value.strip().lower().startswith(b"payment "):
            continue
        kept.append((key, value))
    return tuple(kept)


async def record_payment(org_id: int, amount_micro: int, *, method: str, reference: str,
                         payer: str | None, endpoint_id: str, call_ref: str) -> None:
    """Top the pay-per-call team's balance back up by one taken payment. The payment is already
    taken, so a failure here is logged for reconciliation, never raised to the payer."""
    try:
        async with session_maker() as db:
            await ledger.topup(db, org_id, amount_micro, f"{method}:{reference}",
                               meta={"source": "pay_per_call", "method": method, "payer": payer,
                                     "endpoint_id": endpoint_id, "call_ref": call_ref})
            await db.commit()
    except Exception:
        log.exception("pay per call: payment %s:%s taken but not recorded", method, reference)
