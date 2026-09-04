"""Overflow — the child cycle (docs/PROVIDER-CAPACITY-PLAN.md §4.3).

When treg's OWN account for a provider fails a tier-4 call (a balance/quota signature, a burst-429
that smoothing could not absorb, a connect error), serve the SAME vendor endpoint through a
treg-owned aggregator account: reserve a child hold (own id `{call_ref}:overflow`) → one aggregator
run, no DB open → settle the child at the aggregator's real price (0% markup) and fold the daily
spend delta into that settle → return the vendor's body from the aggregator's envelope, disclosed via
`X-Treg-Served-Via`. One hop: an aggregator that fails is data (child released, aggregator marked
unhealthy 15 min, typed 503 with alternatives), never a second aggregator.

Shadow mode (`overflow_mode=shadow`): everything except the child hold and the answer — the
aggregator is called, status/shape/cost logged and the spend recorded (treg pays the probe, bounded
by the daily budget), and the caller still gets the vendor's own error, charged nothing.

Never on: tiers 1/2, a caller-caused 4xx, a timeout, a non-idempotent method (PUT/PATCH/DELETE), an
org that opted out (step F), or a route the worker has not enabled.
"""

from __future__ import annotations

from datetime import timedelta
import asyncio
import json
import logging
from dataclasses import dataclass, replace

import httpx

from ... import audit
from ...config import get_settings
from ...infra.db import session_maker
from ...domain.capacity import marks as capacity_marks
from ...domain.capacity import overflow_spend as overflow_spend_ledger
from ...domain.capacity import signatures as capacity_signatures
from ...domain.capacity.routes_view import view as routes_view
from ...domain.capacity.verify import shape
from ...domain.capacity.view import view as capacity_view
from ...infra.upstream.aggregators import (AGGREGATOR_SIDE, VENDOR_DRY, AggregatorRequest, AggregatorResult,
                                            by_name, with_vendor_verdict)
from ...timeutil import utcnow_naive
from .resolve import MarketplaceCall
from .reserve import _platform_reserve
from .settle import _platform_settle
from .types import CallFailure, UpstreamResponse

log = logging.getLogger("treg.overflow")

AGGREGATOR_UNHEALTHY_S = 15 * 60
OVERFLOW_METHODS = frozenset({"GET", "HEAD", "POST"})  # a POST lookup with a buffered body is re-sendable
POLL_MAX = 10
POLL_WAIT_S = 1.5


@dataclass
class OverflowOutcome:
    served: bool                       # the caller gets the child's answer
    response: UpstreamResponse | None  # when served
    body: bytes = b""
    charged_micro: int = 0
    observed_micro: int | None = None
    aggregator: str = ""
    failure: CallFailure | None = None  # on-mode: the typed 503 to raise instead of the vendor error
    note: str = ""


@dataclass
class _BudgetReservation:
    aggregator: str
    estimate_micro: int
    direct_micro: int
    outbound: bool = False
    actual_micro: int | None = None
    finalized: bool = False


def _trigger(mk: MarketplaceCall, status: int, headers, body: bytes) -> str | None:
    """Why this call may overflow, or None. Only treg-side failures qualify (plan §4.1)."""
    if status == 401:
        return None
    signal = capacity_signatures.classify(mk.provider, status, headers, body[:4096])
    if signal is None:
        return None
    if capacity_signatures.is_exhausting(signal):
        return signal.kind
    if signal.kind == "burst":
        return "burst"
    return None


async def _send(client: httpx.AsyncClient, req: AggregatorRequest) -> httpx.Response:
    """One HTTP exchange with the aggregator. A seam: tests replace it; production uses the shared
    upstream client (same timeouts as a direct relay)."""
    return await client.request(req.method, req.url, headers=req.headers, json=req.json)


async def _run(client: httpx.AsyncClient, aggregator: str, route, key: str, query, body: bytes | None,
               path_params: dict | None, budget: _BudgetReservation | None = None) -> AggregatorResult:
    adapter = by_name(aggregator)
    req = adapter.build(route, key, query, body, path_params)
    if budget is not None:
        budget.outbound = True
    r = await _send(client, req)
    res = adapter.parse(r.status_code, r.content)
    polls = 0
    while res.failure == "pending" and res.poll_url and polls < POLL_MAX:
        await asyncio.sleep(POLL_WAIT_S)
        polls += 1
        pr = await _send(client, AggregatorRequest("GET", res.poll_url, {"Authorization": f"Bearer {key}"}, {}))
        res = adapter.parse(pr.status_code, pr.content)
    return res


def _child(mk: MarketplaceCall, route) -> MarketplaceCall:
    agg = int(route.agg_price_micro or 0)
    # The child settles against ITS price: an aggregator that reports no cost settles at the
    # aggregator reserve, never at the parent's direct price or table ceiling.
    return replace(mk, tier="platform-overflow", call_id=None, estimate_micro=agg,
                   cost_type="per_call" if route.agg_unit == "call" else "per_result",
                   unit_micro=agg,
                   settlement_basis={"when": "response", "amount": {"kind": "observed"},
                                     "fallback_micro": agg, "reserve_micro": agg})


def _response(res: AggregatorResult) -> UpstreamResponse:
    async def _one():
        yield res.upstream_body

    async def _closed():
        return None

    raw = [(b"content-length", str(len(res.upstream_body)).encode()),
           (b"content-type", b"application/json")]
    return UpstreamResponse(int(res.upstream_status or 200), tuple(raw), _one(), _closed)


def _capacity_503(mk: MarketplaceCall, aggregator: str, why: str) -> CallFailure:
    from .resolve import _capability_alternatives, _catalog_endpoint_for
    ep = _catalog_endpoint_for(mk.endpoint_id) or {"id": mk.endpoint_id}
    alts = _capability_alternatives(ep)
    return CallFailure("provider_capacity", status_code=503, detail={
        "error": "provider_capacity_unavailable", "provider": mk.provider, "endpoint_id": mk.endpoint_id,
        "resets_at": None, "alternatives": [ln.strip() for ln in alts[1:]],
        "message": (f"treg's own {mk.provider} account is out and the overflow relay ({aggregator}) "
                    f"could not serve {mk.endpoint_id} ({why}); nothing was charged.\n"
                    f"  use your own key: treg secret add {mk.provider} --env-var "
                    f"{mk.provider.upper().replace('-', '_')}_API_KEY\n" + "\n".join(alts)),
    })


async def _record_shadow(
    aggregator: str, actual_micro: int, estimate_micro: int, delta_micro: int,
) -> None:
    """Shadow mode's spend row — the same `overflow_spend` write as the child settle, on its own
    short session because there is no child hold to settle in."""
    async with session_maker() as db:
        await overflow_spend_ledger.add_in_transaction(
            db, aggregator, actual_micro - estimate_micro, delta_micro,
        )
        await db.commit()


async def _release_budget(reservation: _BudgetReservation) -> None:
    """Return an estimate for an attempt that never reached the aggregator."""
    if reservation.finalized:
        return
    async with session_maker() as db:
        await overflow_spend_ledger.release_reservation_in_transaction(
            db, reservation.aggregator, reservation.estimate_micro,
        )
        await db.commit()
    reservation.finalized = True


async def _finish_budget(reservation: _BudgetReservation, actual_micro: int) -> None:
    """Reconcile a completed attempt to actual spend and count it once."""
    if reservation.finalized:
        return
    async with session_maker() as db:
        await overflow_spend_ledger.add_in_transaction(
            db, reservation.aggregator, actual_micro - reservation.estimate_micro,
            actual_micro - reservation.direct_micro,
        )
        await db.commit()
    reservation.finalized = True


async def _preserve_unknown_budget(reservation: _BudgetReservation) -> None:
    """Count a cancelled outbound attempt while conservatively retaining its estimate."""
    if reservation.finalized:
        return
    async with session_maker() as db:
        await overflow_spend_ledger.add_in_transaction(
            db, reservation.aggregator, 0,
            reservation.estimate_micro - reservation.direct_micro,
        )
        await db.commit()
    reservation.finalized = True


def _overflow_spend_adjustment(
    reservation: _BudgetReservation,
) -> tuple[str, int, int] | None:
    """Return the known actual adjustment, or None when the estimate must stay reserved."""
    if reservation.actual_micro is None:
        return None
    return (
        reservation.aggregator,
        reservation.actual_micro - reservation.estimate_micro,
        reservation.actual_micro - reservation.direct_micro,
    )


async def _record_shadow_budget(reservation: _BudgetReservation) -> None:
    if reservation.actual_micro is None:
        await _preserve_unknown_budget(reservation)
        return
    await _record_shadow(
        reservation.aggregator, reservation.actual_micro,
        reservation.estimate_micro, reservation.actual_micro - reservation.direct_micro,
    )
    reservation.finalized = True


async def _maybe_overflow_attempt(
    *, mk: MarketplaceCall, caller, meta, call_ref: str, status: int, headers, body: bytes,
    method: str, query_items: list[tuple[str, str]], caller_body: bytes, client: httpx.AsyncClient,
    audit_client: str = "", force_trigger: str | None = None,
    reserved: list[MarketplaceCall],
    budget_reservations: list[_BudgetReservation],
) -> OverflowOutcome | None:
    """After the primary's settle released — or, with `force_trigger`, INSTEAD of a direct attempt
    the resolver already knows would 402 (`mk.skip_direct`). None = nothing to do (the vendor's
    answer stands)."""
    settings = get_settings()
    mode = settings.overflow_mode
    if mode == "off" or mk.tier != "platform" or method.upper() not in OVERFLOW_METHODS:
        return None
    if getattr(caller.org, "platform_overflow_disabled", False):
        return None  # the team opted out: never contact an aggregator on its behalf
    why = force_trigger or _trigger(mk, status, headers, body)
    if why is None:
        return None
    routes = routes_view.for_endpoint(mk.endpoint_id)
    routes = [r for r in routes if settings.overflow_key_for(r.aggregator)
              and not capacity_view.is_exhausted(f"overflow:{r.aggregator}")
              and not capacity_view.is_exhausted(f"overflow:{r.aggregator}:{mk.provider}")]
    if not routes:
        return None
    route = routes[0]
    aggregator = route.aggregator
    key = settings.overflow_key_for(aggregator) or ""
    price = int(route.agg_price_micro or 0)
    # Atomically reserve the estimate before any network call. A rejected upsert means another
    # concurrent attempt already claimed the remaining daily budget.
    async with session_maker() as db:
        budget_row = await overflow_spend_ledger.reserve_in_transaction(
            db, aggregator, price, settings.overflow_daily_budget_micro,
        )
        await db.commit()
    if budget_row is None:
        log.warning("overflow budget reached for %s (estimate %d, cap %d)", aggregator, price,
                    settings.overflow_daily_budget_micro)
        return None
    budget = _BudgetReservation(aggregator, price, int(mk.estimate_micro or 0))
    budget_reservations.append(budget)
    child = _child(mk, route)
    query = [(k, v) for k, v in query_items if k not in mk.consumed]
    path_params = {k: v for k, v in query_items if k in mk.consumed}
    if mode == "on":
        # Child hold: own id, same call_ref family. Insufficient balance here is the normal 402.
        await _platform_reserve(child, caller, meta=meta, call_ref=f"{call_ref}:overflow")
        reserved.append(child)
    res: AggregatorResult | None = None
    try:
        res = await _run(
            client, aggregator, route, key, query, caller_body or None, path_params, budget,
        )
    except httpx.RequestError as exc:
        res = AggregatorResult(None, b"", None, "malformed", f"{type(exc).__name__}: {exc}")
    res = with_vendor_verdict(res, mk.provider)
    budget.actual_micro = int(res.cost_micro) if res.cost_micro is not None else None
    delta = (budget.actual_micro - budget.direct_micro
             if budget.actual_micro is not None else None)
    # --- decide ---
    if res.failure in AGGREGATOR_SIDE or res.failure == VENDOR_DRY:
        why_agg = res.failure
        # The aggregator itself (key, account, host, envelope) is out for everyone; its account
        # for THIS vendor being dry (a relayed 402 / Apollo 422 / period 429) is out for this
        # provider only - one vendor's daily cap must not take hunter and lusha offline too.
        mark_key = f"overflow:{aggregator}" if res.failure in AGGREGATOR_SIDE else f"overflow:{aggregator}:{mk.provider}"
        await capacity_marks.strike(
            mark_key, endpoint_id=None, kind="balance", immediate=True,
            resets_at=utcnow_naive().replace(microsecond=0) + timedelta(seconds=AGGREGATOR_UNHEALTHY_S),
            note=f"{why_agg}: {res.detail[:80]}")
        capacity_view.invalidate()
        if mode == "on":
            spend_adjustment = _overflow_spend_adjustment(budget)
            await _platform_settle(
                child, None, reason=f"overflow_{why_agg[:24]}",
                overflow_spend=spend_adjustment,
            )
            if spend_adjustment is None:
                await _preserve_unknown_budget(budget)
            else:
                budget.finalized = True
        else:
            await _record_shadow_budget(budget)
        log.warning("overflow via %s failed for %s: %s %s", aggregator, mk.endpoint_id, why_agg, res.detail)
        _audit_child(mk, child, call_ref, aggregator, res, charged=0, client=audit_client, note=why_agg)
        return OverflowOutcome(False, None, aggregator=aggregator, note=why_agg,
                               failure=_capacity_503(mk, aggregator, why_agg) if mode == "on" else None)
    if res.failure == "contract" or res.failure == "pending":
        # The aggregator's stricter schema refused (no vendor call, no charge): this route is wrong for
        # this call; the vendor's own answer stands. Worth a log line — verify should have caught it.
        if mode == "on":
            spend_adjustment = _overflow_spend_adjustment(budget)
            await _platform_settle(
                child, None, reason="overflow_contract",
                overflow_spend=spend_adjustment,
            )
            if spend_adjustment is None:
                await _preserve_unknown_budget(budget)
            else:
                budget.finalized = True
        else:
            await _record_shadow_budget(budget)
        log.warning("overflow via %s refused %s: %s", aggregator, mk.endpoint_id, res.detail)
        _audit_child(mk, child, call_ref, aggregator, res, charged=0, client=audit_client, note=res.failure)
        return OverflowOutcome(False, None, aggregator=aggregator, note=res.failure)
    # The vendor answered through the aggregator.
    if mode == "shadow":
        try:
            body_shape = json.dumps(shape(json.loads(res.upstream_body)), sort_keys=True)[:400]
        except ValueError:
            body_shape = "non-json"
        log.info("overflow SHADOW %s via %s: vendor %s direct→%s relay, cost %s, delta %s, shape %s",
                 mk.endpoint_id, aggregator, status, res.upstream_status, res.cost_micro, delta, body_shape)
        await _record_shadow_budget(budget)
        _audit_child(mk, child, call_ref, aggregator, res, charged=0, client=audit_client, note="shadow")
        return OverflowOutcome(False, None, aggregator=aggregator, note="shadow")
    spend_adjustment = _overflow_spend_adjustment(budget)
    charged, observed = await _platform_settle(
        child, int(res.upstream_status or 200), res.upstream_body,
        observed_override=res.cost_micro,
        overflow_spend=spend_adjustment)
    if spend_adjustment is None:
        await _preserve_unknown_budget(budget)
    else:
        budget.finalized = True
    response = _response(res)
    _audit_child(mk, child, call_ref, aggregator, res, charged=charged, client=audit_client)
    return OverflowOutcome(True, response, res.upstream_body, charged, observed, aggregator)


async def maybe_overflow(
    *, mk: MarketplaceCall, caller, meta, call_ref: str, status: int, headers, body: bytes,
    method: str, query_items: list[tuple[str, str]], caller_body: bytes, client: httpx.AsyncClient,
    audit_client: str = "", force_trigger: str | None = None,
) -> OverflowOutcome | None:
    """Run one optional overflow attempt without letting its infrastructure replace the caller's
    existing vendor answer. A skip-direct caller interprets None as its original capacity 503."""
    reserved: list[MarketplaceCall] = []
    budget_reservations: list[_BudgetReservation] = []
    try:
        return await _maybe_overflow_attempt(
            mk=mk, caller=caller, meta=meta, call_ref=call_ref, status=status, headers=headers,
            body=body, method=method, query_items=query_items, caller_body=caller_body,
            client=client, audit_client=audit_client, force_trigger=force_trigger,
            reserved=reserved, budget_reservations=budget_reservations,
        )
    except asyncio.CancelledError:
        if budget_reservations:
            try:
                if budget_reservations[0].actual_micro is not None:
                    await _finish_budget(
                        budget_reservations[0], budget_reservations[0].actual_micro,
                    )
                elif budget_reservations[0].outbound:
                    await _preserve_unknown_budget(budget_reservations[0])
                else:
                    await _release_budget(budget_reservations[0])
            except Exception:  # noqa: BLE001 - cancellation must remain the result
                log.exception("overflow budget cleanup failed for cancelled %s", mk.endpoint_id)
        raise
    except CallFailure:
        if budget_reservations:
            try:
                await _release_budget(budget_reservations[0])
            except Exception:  # noqa: BLE001 - preserve the typed call failure
                log.exception("overflow budget release failed for %s", mk.endpoint_id)
        raise
    except Exception:  # noqa: BLE001 - overflow is advisory and must not replace the primary result
        log.exception("overflow attempt crashed for %s", mk.endpoint_id)
        if reserved:
            await _platform_settle(reserved[0], None, reason="overflow_crashed")
        if budget_reservations:
            try:
                if budget_reservations[0].actual_micro is not None:
                    await _finish_budget(
                        budget_reservations[0], budget_reservations[0].actual_micro,
                    )
                elif budget_reservations[0].outbound:
                    await _preserve_unknown_budget(budget_reservations[0])
                else:
                    await _release_budget(budget_reservations[0])
            except Exception:  # noqa: BLE001 - the primary vendor answer still wins
                log.exception("overflow budget release failed for crashed %s", mk.endpoint_id)
        return None


def _audit_child(mk: MarketplaceCall, child: MarketplaceCall, call_ref: str, aggregator: str,
                 res: AggregatorResult, *, charged: int, client: str, note: str = "") -> None:
    """The child's own audit row: same call_ref as the primary, credential_tier platform-overflow,
    provider = the vendor. Fire-and-forget like every audit row."""
    audit.record_call(
        org_id=child.tool.org_id, user_email=child.tool.owner, tool_name=mk.endpoint_id,
        method="OVERFLOW", path=f"overflow:{aggregator}/{child.tool.name}",
        status_code=int(res.upstream_status or 0), client=client,
        telemetry={"call_ref": call_ref, "endpoint_id": mk.endpoint_id, "provider": mk.provider,
                   "credential_tier": "platform-overflow", "cost_estimated_micro": child.estimate_micro,
                   "cost_observed_micro": res.cost_micro, "cost_charged_micro": charged,
                   "response_bytes": len(res.upstream_body), "params_hash": mk.params_hash,
                   **({"error_response": f"treg overflow: {note}"} if note else {})})
