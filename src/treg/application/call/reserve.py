"""Reservation and spend-cap helpers for metered calls."""

import logging
from datetime import datetime, timezone

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...application import billing
from ...caller_metadata import TAG_DEFAULT
from ...config import get_settings
from ...domain import money as ledger
from ...domain.catalog import store as catalog_store
from ...domain.governance import budgets as budget_policy
from ...domain.governance.usage import _day_start_utc
from ...domain.identity.access import Caller
from ...infra.db import session_maker
from ...models import CallRecord, Org, TagBudget
from .intake import CallMeta, _NO_META
from .resolve import MarketplaceCall
from .types import ReservationFailed


async def _enforce_trial_allowance(caller: Caller, provider: str, db: AsyncSession) -> None:
    """Per-team, per-UTC-day call allowance for TRIAL-POOL providers (fx.yaml `kind: treg_trial`).

    A trial provider is served on treg's own FREE-tier key at a $0 price, so the price gives no
    brake at all — one looping agent would drain the shared vendor quota for every team at once.
    The allowance is the brake, and it lives in the same fx entry as the zero (catalog.trial_pools).

    Counted from audit rows: successful (2xx) calls only, because a failed call produced nothing —
    the same line billability draws. `tool_name` is the endpoint id, so the provider is its prefix.
    The audit is written fire-and-forget, so the count can lag a call or two under load; for a free
    trial that slack is acceptable and bounded. Own-key (tier 2) calls never reach this check — a
    team with its own key is never throttled by the trial it does not use.

    FAIL-CLOSED like the platform cap: the quota being protected is the shared vendor key, and
    serving blind when the count cannot be read is how the pool dies for everyone."""
    allowance = catalog_store.load().trial_pools.get(provider)
    if not allowance:
        return
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0,
                                                   tzinfo=None)
    try:
        used = (await db.execute(
            select(func.count(CallRecord.id)).where(
                CallRecord.org_id == caller.org_id,
                CallRecord.tool_name.like(f"{provider}.%"),  # type: ignore[union-attr]
                CallRecord.status_code >= 200, CallRecord.status_code < 300,
                CallRecord.created_at >= day_start))).scalar_one()
    except Exception as exc:  # noqa: BLE001 — cannot verify the pool ⇒ do not drain it
        logging.getLogger("treg.ledger").warning(
            "trial-allowance check failed for org %s / %s: %s", caller.org_id, provider, exc)
        raise ReservationFailed("trial_allowance_unavailable", status_code=429, detail=(
            f"cannot verify today's {provider} trial usage right now — retry shortly, or use "
            "your own key: treg connections connect"))
    if used >= allowance:
        raise ReservationFailed("trial_allowance_reached", status_code=429, detail={
            "error": "trial_allowance_reached", "provider": provider,
            "allowance_per_day": allowance, "used_today": int(used),
            "message": (f"this team has used its free {provider} trial for today "
                        f"({used}/{allowance} calls). It resets at 00:00 UTC — or connect your "
                        f"own {provider} key for unmetered calls at your plan's limits: "
                        "treg connections connect"),
        })


async def _enforce_platform_daily_cap(caller: Caller, add_micro: int, db: AsyncSession) -> None:
    """Per-org, per-UTC-day ceiling on tier-4 spend. FAIL-CLOSED, unlike `_enforce_daily_cap`: that one
    meters calls and may let a few extra through under load, this one meters OUR money, so a query that
    cannot answer refuses the call. The cap is the blast radius of a runaway agent (and of a pricing
    mistake in the catalog) — the balance alone is not enough, because auto-top-up can refill it."""
    cap = budget_policy._effective_daily_cap(caller.org)
    try:
        spent = await ledger.spent_today(db, caller.org_id)
    except Exception as exc:  # noqa: BLE001 — cannot verify the ceiling ⇒ do not spend
        logging.getLogger("treg.ledger").warning(
            "platform daily-cap check failed for org %s: %s", caller.org_id, exc)
        raise ReservationFailed("platform_cap_unavailable", status_code=429, detail=(
            "cannot verify today's platform spend right now — refusing to spend the team balance "
            "(retry shortly, or use your own key: treg connections connect)"))
    if spent + add_micro > cap:
        raise ReservationFailed("platform_daily_cap_reached", status_code=429, detail={
            "error": "platform_daily_cap_reached",
            "message": (f"this team has reached its daily limit for calls on treg's keys "
                        f"(${ledger.usd(spent):g} of ${ledger.usd(cap):g} today). It resets at 00:00 UTC. "
                        f"To keep going now, connect your own key: "
                        f"treg connections connect --provider <provider>"),
            "spent_today_micro": spent, "daily_cap_micro": cap, "estimated_cost_micro": add_micro,
        })


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _resolve_tag_budget(db: AsyncSession, org_id: int, dim: str, val: str) -> TagBudget | None:
    """The limit in force for one tag value: its own override, else the dimension's default, else
    none (unlimited — the shipped state, until a team sets a default).

    ONE indexed query for both, so adding defaults costs the call path nothing. A registry row
    (`auto`) is skipped: it exists to make the cardinality check cheap, and treating it as an override
    would mean the default never applied to anything that had ever been called.
    """
    rows = (await db.execute(select(TagBudget).where(
        TagBudget.org_id == org_id, TagBudget.dim == dim,
        TagBudget.val.in_([val, TAG_DEFAULT])))).scalars().all()
    own = next((r for r in rows if r.val == val and not r.auto), None)
    return own or next((r for r in rows if r.val == TAG_DEFAULT), None)


async def _enforce_tag_budgets(caller: Caller, meta: CallMeta, db: AsyncSession,
                               add_micro: int | None = None) -> None:
    """Refuse a call that breaches a builder-set limit on one of its tags.

    Two passes, called from two places. `add_micro is None` is the PRE-FLIGHT pass (blocked status and
    the daily call count), which runs before the idempotency replay so a blocked user can neither take
    a lock nor be served an answer cached before they were blocked. `add_micro` set is the SPEND pass,
    which needs the estimate and therefore runs inside `_platform_reserve`.

    Every declared dimension is evaluated and the FIRST breach in declaration order refuses, so the
    outcome is deterministic when budgets stack.

    THE CAPS ARE SOFT — advisory, not a gate. `ledger.reserve` is exact because the balance is a
    materialized column, so its check and its debit are one conditional UPDATE. A per-tag total is an
    aggregate over rows, so N concurrent calls can each read a compliant figure and together exceed
    the cap; the overshoot is bounded by concurrency × per-call estimate. That is acceptable ONLY
    because the hard gates sit behind this one: the org balance and the platform daily cap. Making it
    exact would need a second materialized authority on spend, reset daily, decremented on release and
    corrected on settle divergence — four new ways to disagree with domain/money, which is the one module
    allowed to move money. Never document these caps to builders as hard limits.
    """
    if not meta.tags:
        return
    dims = budget_policy._budget_dims_of(caller.org)
    for dim in dims:
        val = meta.tags.get(dim)
        if not val:
            continue
        try:
            # Registering the value (cardinality bound) happens on the pre-flight pass only; both
            # passes then resolve override → default.
            if add_micro is None:
                result = await budget_policy._tag_budget(
                    db, caller.org_id, dim, val, create=True)
                if result.created:
                    await db.commit()
            row = await _resolve_tag_budget(db, caller.org_id, dim, val)
        except budget_policy.BudgetPolicyError as exc:
            kind = (exc.detail.get("error", "tag_budget_unavailable")
                    if isinstance(exc.detail, dict) else "tag_budget_unavailable")
            raise ReservationFailed(
                kind, status_code=exc.status_code, detail=exc.detail) from exc
        except Exception as exc:  # noqa: BLE001 — cannot verify a ceiling ⇒ do not spend
            logging.getLogger("treg.ledger").warning(
                "tag budget check failed for org %s (%s=%s): %s", caller.org_id, dim, val, exc)
            raise ReservationFailed("tag_budget_unavailable", status_code=429, detail={
                "error": "tag_budget_unavailable", "dim": dim, "val": val,
                "message": "cannot verify this budget right now — retry shortly",
            })
        if row is None:
            continue
        if add_micro is None:
            if row.status == "blocked":
                raise ReservationFailed("tag_blocked", status_code=403, detail={
                    "error": "tag_blocked", "dim": dim, "val": val,
                    "message": f"{dim} {val!r} is blocked",
                })
            if row.calls_per_day is not None and row.calls_per_day >= 0:
                # From the LEDGER's tag rows, never CallRecord: audit rows are shed under load, so a
                # count cap would let a burst through exactly when it matters — and CallRecord only
                # carries the PRIMARY dimension, so a cap on any other declared key matched nothing
                # and never fired at all.
                used = await ledger.tag_calls_since(
                    db, caller.org_id, dim, val, _day_start_utc())
                if used >= row.calls_per_day:
                    raise ReservationFailed("tag_call_cap_reached", status_code=429, detail={
                        "error": "tag_call_cap_reached", "dim": dim, "val": val,
                        "used_today": int(used), "calls_per_day": row.calls_per_day,
                        "message": f"{dim} {val!r} has used its {row.calls_per_day} calls for today",
                    })
            continue
        for cap, since, period in ((row.daily_cap_micro, _day_start_utc(), "day"),
                                   (row.monthly_cap_micro, _month_start_utc(), "month")):
            if cap is None:
                continue
            spent = await ledger.tag_spent_since(db, caller.org_id, dim, val, since)
            if spent + add_micro > cap:
                # Deliberately NOT the org-level 402/429 shape: that one carries the team's balance and
                # a top-up link, and this response is the one a builder renders to their own end user.
                raise ReservationFailed("tag_spend_cap_reached", status_code=429, detail={
                    "error": "tag_spend_cap_reached", "dim": dim, "val": val,
                    "spent_micro": spent, "cap_micro": cap, "period": period,
                    "estimated_cost_micro": add_micro,
                    "message": (f"{dim} {val!r} has reached its spend limit for this {period} "
                                f"(${ledger.usd(spent):g} of ${ledger.usd(cap):g})"),
                })


async def _platform_reserve(mk: MarketplaceCall, caller: Caller, meta: CallMeta = _NO_META,
                            call_ref: str | None = None) -> None:
    """Withhold this call's estimated cost BEFORE a byte goes upstream, and record the hold on `mk`.
    Insufficient balance is a 402 whose body an agent can act on without reading prose.

    `meta` is the caller's parsed X-Treg-Meta bag, passed explicitly rather than hung off `mk`:
    attribution decides who a reselling builder bills, and it belongs to the request, not to the
    endpoint match. The already-parsed object travels, never a bare dict — re-deriving the primary
    dimension here would be a second place that could disagree about who pays."""
    # Read before `reserve_in_transaction`: the insufficient-balance path rolls back, and a lazy
    # attribute load while constructing the refusal would otherwise escape the application session.
    auto_on = bool(caller.org.autotopup_enabled and caller.org.autotopup_consented_at)
    prefs = billing.autotopup_prefs(caller.org) if auto_on else None
    try:
        async with session_maker() as db:
            # The builder's own per-tag ceilings first: a refusal that belongs to ONE of their users
            # must not surface as the team-wide balance error, which names the builder's private numbers.
            await _enforce_tag_budgets(caller, meta, db, add_micro=mk.estimate_micro)
            await _enforce_platform_daily_cap(caller, mk.estimate_micro, db)
            await _enforce_trial_allowance(caller, mk.provider, db)
            try:
                mk.call_id = await ledger.reserve_in_transaction(
                    db, caller.org_id, mk.endpoint_id, mk.estimate_micro,
                    meta={"tier": "oauth" if mk.billed_oauth else "platform",
                          "provider": mk.provider, "cost_type": mk.cost_type},
                    tags=meta.tags, call_id=call_ref)
            except ledger.InsufficientBalance:
                await db.rollback()
                raise
            await db.commit()
            # The conditional UPDATE bypasses an ORM instance. Reload after commit so auto-top-up sees
            # the balance crossing that triggered this reservation.
            org = await db.get(Org, caller.org_id)
            if org is not None:
                billing.maybe_schedule_autotopup(org)
    except ledger.InsufficientBalance as exc:
        wallet = f"treg's {mk.provider} " + ("app (pay-per-use)" if mk.billed_oauth else "key")
        # For a billed OAuth call "connect your own key" is not the fix — the connection already
        # exists; the way off the meter is bringing your OWN developer app to /oauth/start.
        alt = (f"  or bring your own {mk.provider} developer app (BYO OAuth) — those calls are never metered"
               if mk.billed_oauth else
               f"  or use your own key: treg connections connect --provider {mk.provider}")
        # A team that keeps hitting this by hand is the one that should hear about auto top-up; a team
        # that already has it on needs to know it is the cooldown/cap holding, not a missing card —
        # otherwise the natural reading of "add funds" is that auto top-up is broken.
        if auto_on:
            auto_line = (f"  auto top-up:    on — adds ${ledger.usd(prefs['amount_micro']):g} when the balance "
                         f"drops below ${ledger.usd(prefs['threshold_micro']):g}, at most once per "
                         f"{get_settings().autotopup_cooldown_s // 60} min and "
                         f"${ledger.usd(prefs['monthly_cap_micro']):g}/month. Raise the amount or the "
                         f"cap if your burn outruns it: treg topup --auto on --amount 50 --cap 500")
        else:
            auto_line = "  auto top-up:    off — refill automatically instead: treg topup --auto on --threshold 5 --amount 20"
        raise ReservationFailed("insufficient_balance", status_code=402, detail={
            "error": "insufficient_balance",
            "message": (f"{mk.endpoint_id} would cost ~${ledger.usd(exc.required_micro):g} on {wallet} "
                        f"and this team's balance is ${ledger.usd(exc.balance_micro):g}.\n"
                        f"  add funds:      {get_settings().public_url}/app#billing\n"
                        f"{auto_line}\n"
                        + alt),
            "balance_micro": exc.balance_micro,
            "estimated_cost_micro": exc.required_micro,
            "topup_url": "/app#billing",
            "autotopup_enabled": auto_on,
            "provider": mk.provider,
            "endpoint_id": mk.endpoint_id,
        })
