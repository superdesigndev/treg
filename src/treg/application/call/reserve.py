"""Reservation and spend-cap helpers for metered calls."""

import contextvars
import functools
import json
import logging
import random
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import event, func
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...application import billing
from ...caller_metadata import TAG_DEFAULT
from ...config import get_settings
from ...domain import money as ledger
from ...domain.money import allowance as money_allowance
from ...domain.catalog import store as catalog_store
from ...domain.governance import budgets as budget_policy
from ...domain.governance.usage import _day_start_utc
from ...domain.identity.access import Caller
from ...infra.db import session_maker
from ...infra import money_admission
from ...infra.money_session import money_session
from ...infra.money_timing import observe_money
from ...infra.money_trace import mark_money, money_stage
from ...infra.structured_logging import get_logger
from ...models import CallRecord, Org, TagBudget
from . import allowance as allowance_runtime
from . import idempotency
from .intake import CallMeta, _NO_META
from .resolve import MarketplaceCall, held_micro
from .types import ReservationFailed


# A keyless wallet's request (docs/context/architecture/agent-payments.md): its reservations
# take no allowance, so the wallet's credit is its team's balance alone, which the payment path
# reads to decide whether a payment draws credit or settles.
keyless_payer: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "treg_keyless_payer", default=False)


@asynccontextmanager
async def reserve_admission(org_id: int) -> AsyncIterator[None]:
    """Reject a contended reserve before its money session, after releasing partial gates."""
    try:
        async with money_admission.admit(
            money_admission.require_balance_orgs([org_id]), operation="reserve",
        ):
            yield
    except money_admission.ReserveAdmissionTimeout as exc:
        # Money admission owns its retry floor, with integer-second jitter for Retry-After.
        retry_base_s = get_settings().money_admission_retry_after_s
        retry_after_s = random.randint(retry_base_s, 2 * retry_base_s)
        try:
            logging.getLogger("treg.money_admission").warning(
                "money_admission_rejected operation=reserve org_id=%s reason=wait_timeout retry_after_s=%s",
                org_id, retry_after_s)
        except Exception:  # noqa: BLE001 - diagnostics cannot change the refusal
            pass
        raise ReservationFailed("money_admission_timeout", status_code=429, detail={
            "error": "money_admission_timeout",
            "message": "the team's money queue is busy; retry shortly. Nothing was reserved or charged.",
        }, retry_after_s=retry_after_s) from exc


async def _enforce_trial_allowance(caller: Caller, provider: str, endpoint_id: str,
                                   db: AsyncSession) -> None:
    """Per-team, per-UTC-day call allowance for TRIAL-POOL providers (fx.yaml `kind: treg_trial`).

    A trial provider is served on treg's own FREE-tier key at a $0 price, so the price gives no
    brake at all — one looping agent would drain the shared vendor quota for every team at once.
    The allowance is the brake, and it lives in the same fx entry as the zero (catalog.trial_pools).

    Counted from audit rows: successful (2xx) platform calls whose catalog cost is not `free`.
    A free discovery call consumes no vendor credit, so it neither checks nor consumes this paid-call
    allowance. The audit is written fire-and-forget, so the count can lag a call or two under load;
    for a free trial that slack is acceptable and bounded. Own-key (tier 2) calls never reach this
    check — a team with its own key is never throttled by the trial it does not use.

    FAIL-CLOSED like the platform cap: the quota being protected is the shared vendor key, and
    serving blind when the count cannot be read is how the pool dies for everyone.

    The count reads every audit row of the team's day, and a busy team has a large day. A day's
    count only grows, so a team found at its allowance is remembered in this process until 00:00
    UTC and refused without counting again; the calls that still count are the few below it. The
    team is remembered with its creation time, since SQLite gives a deleted team's id to the next
    one. `used_today` is the count when the team was found at its allowance: a lower bound, as the
    audit lag already made it."""
    catalog = catalog_store.load()
    allowance = catalog.trial_pools.get(provider)
    if not allowance:
        return
    endpoint = catalog.by_id.get(endpoint_id) or {}
    if (endpoint.get("cost") or {}).get("type") == "free":
        return
    paid_endpoint_ids = [
        ep["id"] for ep in catalog.for_provider(provider)
        if (ep.get("cost") or {}).get("type") != "free"
    ]
    day_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0,
                                                   tzinfo=None)
    spent_key = (caller.org_id, caller.org.created_at, provider, day_start.date().isoformat())
    used = _spent_trials.get(spent_key, 0)
    if used < allowance:
        used = await _trial_calls_today(caller, provider, paid_endpoint_ids, day_start, db)
        if used >= allowance:
            _remember_spent_trial(spent_key, used)
    if used >= allowance:
        raise ReservationFailed("trial_allowance_reached", status_code=429, detail={
            "error": "trial_allowance_reached", "provider": provider,
            "allowance_per_day": allowance, "used_today": int(used),
            "message": (f"this team has used its free {provider} trial for today "
                        f"(at least {used} of {allowance} calls). It resets at 00:00 UTC — or connect your "
                        f"own {provider} key for unmetered calls at your plan's limits: "
                        "treg connections connect"),
        })


_spent_trials: dict[tuple[int, datetime, str, str], int] = {}
_SPENT_TRIALS_KEPT = 10_000


def _remember_spent_trial(key: tuple[int, datetime, str, str], used: int) -> None:
    if len(_spent_trials) >= _SPENT_TRIALS_KEPT:
        _spent_trials.clear()
    for stale in [k for k in _spent_trials if k[3] != key[3]]:
        del _spent_trials[stale]
    _spent_trials[key] = used


async def _trial_calls_today(caller: Caller, provider: str, paid_endpoint_ids: list[str],
                             day_start: datetime, db: AsyncSession) -> int:
    try:
        return int((await db.execute(
            select(func.count(CallRecord.id)).where(
                CallRecord.org_id == caller.org_id,
                CallRecord.credential_tier == "platform",
                CallRecord.tool_name.in_(paid_endpoint_ids),  # type: ignore[union-attr]
                CallRecord.status_code >= 200, CallRecord.status_code < 300,
                CallRecord.created_at >= day_start))).scalar_one())
    except Exception as exc:  # cannot verify the pool ⇒ do not drain it
        logging.getLogger("treg.ledger").warning(
            "trial-allowance check failed for org %s / %s: %s", caller.org_id, provider, exc)
        raise ReservationFailed("trial_allowance_unavailable", status_code=429, detail=(
            f"cannot verify today's {provider} trial usage right now — retry shortly, or use "
            "your own key: treg connections connect")) from exc


async def _committed_today(org_id: int) -> int:
    """The day's committed spend (`allowance.committed_today`), read on its own session."""
    async with session_maker() as db:
        return await money_allowance.committed_today(db, org_id)


async def _enforce_platform_daily_cap(caller: Caller, add_micro: int, db: AsyncSession) -> None:
    """Per-org, per-UTC-day ceiling on tier-4 spend. FAIL-CLOSED, unlike `_enforce_daily_cap`: that one
    meters calls and may let a few extra through under load, this one meters OUR money, so a query that
    cannot answer refuses the call. The cap is the blast radius of a runaway agent (and of a pricing
    mistake in the catalog) — the balance alone is not enough, because auto-top-up can refill it."""
    cap = budget_policy._effective_daily_cap(caller.org)
    if cap <= 0:  # no limit applies: the balance (and the auto-top-up monthly cap) is the bound
        return
    try:
        # Today's counter plus what an allowance can still spend today outside it. Read from
        # the rows, not from the allowance switch: a hold and its queued charges outlive it.
        spent = await money_allowance.committed_today(db, caller.org_id)
    except Exception as exc:  # cannot verify the ceiling ⇒ do not spend
        logging.getLogger("treg.ledger").warning(
            "platform daily-cap check failed for org %s: %s", caller.org_id, exc)
        raise ReservationFailed("platform_cap_unavailable", status_code=429, detail=(
            "cannot verify today's platform spend right now — refusing to spend the team balance "
            "(retry shortly, or use your own key: treg connections connect)")) from exc
    if spent + add_micro > cap:
        raise ReservationFailed("platform_daily_cap_reached", status_code=429, detail={
            "error": "platform_daily_cap_reached",
            "message": (f"this team has reached its daily limit for calls on treg's keys "
                        f"(${ledger.usd(spent):g} of ${ledger.usd(cap):g} today). It resets at 00:00 UTC. "
                        f"To keep going now, connect your own key: "
                        f"treg connections connect --provider <provider>"),
            "spent_today_micro": spent, "daily_cap_micro": cap, "estimated_cost_micro": add_micro,
        })


async def _enforce_member_spend_cap(caller: Caller, add_micro: int, db: AsyncSession, *,
                                    reap: bool = True) -> int | None:
    """Refuse a call that would take this member past their daily spend limit on treg's keys.
    Returns the member id to attribute the hold to when a limit applies, else None.
    `reap=False` keeps the check read-only, for a caller outside money admission.

    FAIL-CLOSED, like the platform cap: it limits money. SOFT like the tag caps: the figure is an
    aggregate over `MemberSpend`, so concurrent calls can each pass and together overshoot by at most
    their estimates. The org balance and the platform daily cap are the hard gates behind it.
    """
    cap = budget_policy.member_daily_spend_cap(caller.membership, caller.org)
    if cap is None:
        return None
    try:
        spent = await ledger.member_spent_since(db, caller.membership.id, _day_start_utc())
        # A crashed call's expired hold still counts here. `reserve_in_transaction` reaps those, but
        # only after this gate, so a refusal reaps first and reads again.
        if reap and spent + add_micro > cap and await ledger.reap_stale_holds(db, org_id=caller.org_id):
            spent = await ledger.member_spent_since(db, caller.membership.id, _day_start_utc())
    except Exception as exc:  # cannot verify the limit => do not spend
        logging.getLogger("treg.ledger").warning(
            "member spend-cap check failed for membership %s: %s", caller.membership.id, exc)
        raise ReservationFailed("member_spend_cap_unavailable", status_code=429, detail={
            "error": "member_spend_cap_unavailable",
            "message": "cannot verify your daily spend limit right now - retry shortly",
        }) from exc
    if spent + add_micro > cap:
        resets_at = _day_start_utc() + timedelta(days=1)
        wait_s = max(1, int((resets_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()))
        raise ReservationFailed("member_spend_cap_reached", status_code=429, retry_after_s=wait_s, detail={
            "error": "member_spend_cap_reached",
            "message": (f"you have reached your daily spend limit on this team "
                        f"(${ledger.usd(spent):.2f} of ${ledger.usd(cap):.2f} today). It resets at "
                        f"00:00 UTC, in {wait_s // 3600} h {wait_s % 3600 // 60} min. Ask a team "
                        f"admin to raise your limit."),
            "spent_today_micro": spent, "daily_spend_cap_micro": cap,
            "estimated_cost_micro": add_micro,
            "resets_at": resets_at.isoformat() + "Z", "retry_after_s": wait_s,
        })
    return caller.membership.id


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
                               add_micro: int | None = None) -> bool:
    """Refuse a call that breaches a builder-set limit on one of its tags; return whether any
    limit (a spend cap or a call count) applies to one of them.

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
        return False
    limited = False
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
        except Exception as exc:  # cannot verify a ceiling ⇒ do not spend
            logging.getLogger("treg.ledger").warning(
                "tag budget check failed for org %s (%s=%s): %s", caller.org_id, dim, val, exc)
            raise ReservationFailed("tag_budget_unavailable", status_code=429, detail={
                "error": "tag_budget_unavailable", "dim": dim, "val": val,
                "message": "cannot verify this budget right now — retry shortly",
            }) from exc
        if row is None:
            continue
        limited = limited or _limits(row)
        if add_micro is None:
            if row.status == "blocked":
                raise ReservationFailed("tag_blocked", status_code=403, detail={
                    "error": "tag_blocked", "dim": dim, "val": val,
                    "message": f"{dim} {val!r} is blocked",
                })
            if row.calls_per_day is not None and row.calls_per_day >= 0:
                # From the LEDGER's tag rows, never CallRecord: audit rows are shed under load, so a
                # count cap would let a burst through exactly when it matters - and CallRecord only
                # carries the PRIMARY dimension, so a cap on any other declared key would never fire.
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
    return limited


def _limits(row: TagBudget) -> bool:
    """The row caps spend or counts calls, both read from the tag rows a hold writes."""
    return (row.daily_cap_micro is not None or row.monthly_cap_micro is not None
            or (row.calls_per_day is not None and row.calls_per_day >= 0))


def _purchased_only(endpoint_id: str) -> bool:
    """The endpoint's platform declares `purchased_credit_only` (capabilities.yaml)."""
    catalog = catalog_store.load()
    platform = (catalog.by_id.get(endpoint_id) or {}).get("platform")
    return bool((catalog.platforms.get(platform) or {}).get("purchased_credit_only"))


def _holdless_listed(org_id: int) -> bool:
    """The team's untagged free calls may take no hold (`holdless_free_*`)."""
    settings = get_settings()
    if org_id in settings.holdless_free_excluded_org_ids:
        return False
    return settings.holdless_free_all_orgs or org_id in settings.holdless_free_org_ids


async def _holdless_reserve(mk: MarketplaceCall, caller: Caller) -> None:
    """Admit a $0 call without a hold: the daily-cap and member-limit reads, no balance row, no
    money admission. A member already past their limit is refused, as on the hold path."""
    mk.holdless = True  # before any await: a cancel during the cap read has no hold to release
    mk.payer_org_id = caller.org_id
    mk.reserved_micro = 0
    mk.reserved_at = time.monotonic()
    team_cap = budget_policy._effective_daily_cap(caller.org) > 0
    member_cap = budget_policy.member_daily_spend_cap(caller.membership, caller.org) is not None
    if team_cap or member_cap:
        async with session_maker() as db:
            # The member first, as on the hold path: its refusal gives back the member's call slot.
            if member_cap:
                await _enforce_member_spend_cap(caller, 0, db, reap=False)
            if team_cap:
                await _enforce_platform_daily_cap(caller, 0, db)


def _allowance_skip(mk: MarketplaceCall, caller: Caller, meta: CallMeta, call_ref: str | None,
                    allowed: bool) -> str | None:
    """Why a call keeps its own hold, or None for a call the team's allowance may pay for: a
    direct, synchronous call on treg's platform key, untagged or (for a team `tagged_listed`) with
    no tag budget limiting any of its tags, by a member with no spend limit, on an endpoint any
    credit may pay for.

    Everything else keeps its own hold, because something reads that hold: a tag row a budget
    counts, a member row written with it, an async task or a routed parent that closes it later, the purchased-credit
    gate. A child call's reference is its parent's plus a `:suffix` (`_child_kind`). A call whose application did
    not allow the allowance (`allowed`) keeps its hold too. A routed child whose root lets its
    children take (`mk.routed_allowance`) is judged as a direct call: its parent finishes its take.
    """
    if not call_ref:
        return "no_call_ref"
    if ":" in call_ref:
        if mk.routed_allowance and _routed_chain(call_ref):
            return _direct_skip(mk, caller, meta, allowed, deferred_settle=False)
        return _child_kind(call_ref)
    return _direct_skip(mk, caller, meta, allowed)


def _direct_skip(mk: MarketplaceCall, caller: Caller, meta: CallMeta, allowed: bool, *,
                 deferred_settle: bool = True) -> str | None:
    """`_allowance_skip` for a call judged as a direct call; `deferred_settle=False` leaves out
    the deferred-settle rule, which every routed child meets."""
    if not allowed:
        return "not_allowed"
    if (mk.reserved_micro or 0) <= 0:
        return "zero_price"
    if not allowance_runtime.enabled_for(caller.org_id):
        return "allowance_off"
    if meta.tags:
        if not allowance_runtime.tagged_listed(caller.org_id):
            return "tagged"
        if meta.tag_limited:
            return "tag_budget"
        if caller.membership.pinned_tags:
            # A pinned token's reads find its calls by the tags on their reserve entry when the
            # audit row is gone; an allowance call has none until its fold, or ever if unbilled.
            return "pinned"
    if mk.billed_oauth:
        return "billed_oauth"
    if deferred_settle and mk.deferred is not None:
        return "deferred_settle"
    if mk.async_owner_call_id is not None or mk.async_descriptor:
        return "async"
    if mk.provider in catalog_store.load().trial_pools:
        return "trial_pool"
    if budget_policy.member_daily_spend_cap(caller.membership, caller.org) is not None:
        return "member_spend_cap"
    if _purchased_only(mk.endpoint_id):
        return "purchased_credit_only"
    return None


_ROUTED_SEGMENT = re.compile(r"r\d+(?:\.\d+)*|v|u")


def _routed_chain(call_ref: str | None) -> bool:
    """Every segment after the root is a routed attempt `rN`, a check `v` or an upgrade `u`."""
    segments = (call_ref or "").split(":")[1:]
    return bool(segments) and all(_ROUTED_SEGMENT.fullmatch(s) for s in segments)


def _as_direct(mk: MarketplaceCall, caller: Caller, meta: CallMeta, call_ref: str | None,
               allowed: bool) -> str | None:
    """For a child of a routed call (every segment after the root a routed attempt `rN`, a
    check `v` or an upgrade `u`): what `_allowance_skip` would answer if it were a direct
    call, `eligible` when nothing else keeps it off the allowance. None for any other call."""
    if not _routed_chain(call_ref):
        return None
    return _direct_skip(mk, caller, meta, allowed, deferred_settle=False) or "eligible"


def _child_kind(call_ref: str) -> str:
    """The skip reason of a child call, named by its own `:suffix`: a routed child `:rN`, an
    overflow rung `:overflow`, a hub step `:sN`, or another child (a poll, a verification)."""
    last = call_ref.rsplit(":", 1)[1]
    if last == "overflow":
        return "child_call:overflow"
    if last[:1] in ("r", "s") and last[1:].replace(".", "").isdigit():
        return "child_call:routed" if last[0] == "r" else "child_call:hub_step"
    return "child_call:other"


_SKIP_LOG_EVERY_S = 60.0
# A minute names at most this many teams before it is logged early; the number is the
# allowance's own prune threshold for its per-team maps.
_SKIP_TEAMS_MAX = allowance_runtime._TEAM_STATE_PRUNE_AT
_SKIP_TOP = 5
_skips: dict[tuple[int, str], list] = {}
_skip_totals: dict[str, dict] = {}
_skip_log = get_logger("treg.allowance_skips")


def _note_allowance_skip(org_id: int, reason: str, held_micro: int,
                         as_direct: str | None = None) -> None:
    """Count a listed team's call sent to the exact path, by reason, once its reserve is decided:
    `held_micro` is what the hold it committed reserved, 0 for a call refused or cancelled first.

    Every team counts toward one `allowance_skips` line per reason per minute in this process:
    the calls, the micro-USD they held, how many teams sent them and the teams that held the
    most. A routed child also counts under `as_direct`, the reason it would have had as a
    direct call (`_as_direct`). A team named in `money_allowance_org_ids` also gets its own `allowance_skipped` lines:
    the first at once, then one per reason per minute with the count since the last.
    Diagnostics never fail a call."""
    try:
        now = time.monotonic()
        total = _skip_window(now, org_id, reason)
        total["calls"] += 1
        total["held_micro"] += held_micro
        team = total["orgs"][org_id]
        team[0] += 1
        team[1] += held_micro
        if as_direct is not None:
            direct = total["as_direct"].setdefault(as_direct, [0, 0])
            direct[0] += 1
            direct[1] += held_micro
        if org_id not in get_settings().money_allowance_org_ids:
            return
        entry = _skips.setdefault((org_id, reason), [None, 0])
        entry[1] += 1
        if entry[0] is not None and now - entry[0] < _SKIP_LOG_EVERY_S:
            return
        _skip_log.info("allowance_skipped %s", json.dumps(
            {"org_id": org_id, "reason": reason, "calls": entry[1]}, separators=(",", ":")))
        entry[0], entry[1] = now, 0
    except Exception:  # noqa: BLE001
        pass


def _skip_window(now: float, org_id: int, reason: str) -> dict:
    """The reason's current minute with a row for the team, logging the last one first when the
    minute is up or it already names `_SKIP_TEAMS_MAX` other teams."""
    total = _skip_totals.get(reason)
    if total is not None and (now - total["since"] >= _SKIP_LOG_EVERY_S
                              or (org_id not in total["orgs"]
                                  and len(total["orgs"]) >= _SKIP_TEAMS_MAX)):
        _log_skip_total(now, reason, total)
        total = None
    if total is None:
        total = _skip_totals[reason] = {"since": now, "calls": 0, "held_micro": 0, "orgs": {},
                                        "as_direct": {}}
    total["orgs"].setdefault(org_id, [0, 0])
    return total


def _log_skip_total(now: float, reason: str, total: dict) -> None:
    held = [item for item in total["orgs"].items() if item[1][1] > 0]
    top = sorted(held, key=lambda kv: (-kv[1][1], -kv[1][0], kv[0]))[:_SKIP_TOP]
    _skip_log.info("allowance_skips %s", json.dumps({
        "reason": reason, "calls": total["calls"], "held_micro": total["held_micro"],
        "orgs": len(total["orgs"]), "window_s": round(now - total["since"], 1),
        "top": [[org_id, calls, held] for org_id, (calls, held) in top],
        **({"as_direct": dict(sorted(total["as_direct"].items()))} if total["as_direct"] else {}),
    }, separators=(",", ":")))


async def _schedule_autotopup_after(org_id: int) -> None:
    """Let auto top-up see the balance a committed reservation just moved. The conditional UPDATE
    bypasses any ORM instance, so this reloads the org on its own session, outside every lease.
    The money is committed; a failed read must not fail the call."""
    org = None
    try:
        async with session_maker() as read_db:
            org = await read_db.get(Org, org_id)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("treg.ledger").warning(
            "auto top-up read after reserve failed for org %s: %s", org_id, exc)
    if org is not None:
        billing.maybe_schedule_autotopup(org)


def wallet_and_alternative(mk: MarketplaceCall) -> tuple[str, str]:
    """Whose money a metered call spends, and the line naming the way off that meter."""
    wallet = f"treg's {mk.provider} " + ("app (pay-per-use)" if mk.billed_oauth else "key")
    # For a billed OAuth call "connect your own key" is not the fix: the connection already exists,
    # and the way off the meter is bringing your OWN developer app to /oauth/start.
    alt = (f"  or bring your own {mk.provider} developer app (BYO OAuth) - those calls are never metered"
           if mk.billed_oauth else
           f"  or use your own key: treg connections connect --provider {mk.provider}")
    return wallet, alt


async def _platform_reserve(mk: MarketplaceCall, caller: Caller, meta: CallMeta = _NO_META,
                            call_ref: str | None = None, *, allow_holdless: bool = False,
                            allow_allowance: bool = False, mark_in_flight: bool = False,
                            retry_short: bool = True, skip_reason: str | None = None,
                            skip_as_direct: str | None = None) -> None:
    """Withhold this call's estimated cost BEFORE a byte goes upstream, and record the hold on `mk`.
    Insufficient balance is a 402 whose body an agent can act on without reading prose.

    `meta` is the caller's parsed X-Treg-Meta bag, passed explicitly rather than hung off `mk`:
    attribution decides who a reselling builder bills, and it belongs to the request, not to the
    endpoint match. The already-parsed object travels, never a bare dict — re-deriving the primary
    dimension here would be a second place that could disagree about who pays.

    `allow_holdless` admits a `holdless_eligible` call from an untagged caller of a team
    `_holdless_listed`, unless it holds an Idempotency-Key claim (`mark_in_flight`), without a hold: a $0 reserve and its release only queued on the org's
    balance row. A tagged call keeps its hold,
    because the tag rows written with it count toward `calls_per_day`.

    `allow_allowance` lets an eligible call (`_allowance_skip` is None) of a team on the allowance
    (`allowance_runtime.listed`) reserve from the team's allowance counter instead: no hold, no
    balance row. Whenever the allowance cannot serve it, the call takes its own hold below.
    `mark_in_flight` (a call holding an Idempotency-Key claim) first marks the call as running in
    the shared store, the one sign of it a stale claim can read; unmarked, it keeps its hold.
    A hold the balance cannot cover is tried once more (`retry_short`) when the balance covers
    it after all, the team's allowance counter given back to it as needed
    (`allowance_runtime.onto_balance`).

    The hold is the estimate, or the row's declared worst case when that is higher: a settle that
    reads the provider's figure may exceed the estimate, and calls in flight together all passed
    the balance gate already, so only a hold that covers the worst case bounds them."""
    if (allow_holdless and not mark_in_flight and mk.holdless_eligible and not meta.tags
            and _holdless_listed(caller.org_id)):
        await _holdless_reserve(mk, caller)
        return
    mk.holdless = False
    held = held_micro(mk.estimate_micro, mk.worst_case_micro)
    charged = ledger.with_margin(held)
    mk.payer_org_id = caller.org_id
    mk.reserved_micro = max(0, charged)
    mk.reserved_at = time.monotonic()
    if mk.max_cost_micro is not None and charged > mk.max_cost_micro:
        raise ReservationFailed("route_max_cost", status_code=402, detail={
            "error": "route_max_cost", "endpoint_id": mk.endpoint_id, "provider": mk.provider,
            "max_cost_micro": mk.max_cost_micro, "estimated_cost_micro": charged,
            "message": (f"{mk.endpoint_id} would reserve ~${ledger.usd(charged):g} and "
                        f"the remaining X-Treg-Route-Max-Cost is ${mk.max_cost_micro / 1_000_000:g}; "
                        "nothing was charged for this attempt. Ask for fewer rows/targets or raise the ceiling."),
        })
    # Read before `reserve_in_transaction`: the insufficient-balance path rolls back, and a lazy
    # attribute load while constructing the refusal would otherwise escape the application session.
    auto_on = bool(caller.org.autotopup_enabled and caller.org.autotopup_consented_at)
    prefs = billing.autotopup_prefs(caller.org) if auto_on else None
    mk.allowance_epoch, mk.allowance_marked = None, False
    listed = (retry_short and not keyless_payer.get()
              and allowance_runtime.listed(caller.org_id))
    skip = _allowance_skip(mk, caller, meta, call_ref, allow_allowance) if listed else "unlisted"
    # Marked before the take: from the take on, the mark is all a stale claim can see of the call.
    if skip is None and mark_in_flight and not await idempotency.mark_in_flight(call_ref):
        skip = "in_flight_unmarked"
    if skip is None:
        before = allowance_runtime.refills(caller.org_id)
        epoch = await allowance_runtime.take(
            caller.org_id, mk.reserved_micro,
            daily_cap_micro=budget_policy._effective_daily_cap(caller.org),
            after_refill=(
                functools.partial(_schedule_autotopup_after, caller.org_id) if auto_on else None))
        if epoch:
            # Recorded on the call before anything else is awaited: a cancel from here on
            # finds the take and returns it.
            mk.call_id, mk.allowance_epoch = call_ref, epoch
            mk.allowance_marked = mark_in_flight
            mk.allowance_tags = dict(meta.tags) or None
        if auto_on and allowance_runtime.refills(caller.org_id) != before:
            await _schedule_autotopup_after(caller.org_id)
        if epoch:
            return
        skip = ("refill_admission_busy" if allowance_runtime.take_met_busy_refill()
                else "take_refused")
        if mark_in_flight:
            await idempotency.clear_in_flight(call_ref)
    # A listed team's call is counted once, when its reserve is decided (`_note_allowance_skip`);
    # a retry after a give-back (`skip_reason`) is counted under the first attempt's reason.
    note = skip if listed else skip_reason
    as_direct = skip_as_direct
    if listed and skip.startswith("child_call:"):
        as_direct = _as_direct(mk, caller, meta, call_ref, allow_allowance)
    noted, committed = note is None, []
    # Loaded inside the session, scheduled only after the lease is gone. The refill task opens
    # its own session and may call Stripe; that work is not part of this reserve.
    refused_org = None
    try:
        with observe_money("reserve", org_id=caller.org_id, call_id=call_ref) as timing:
            # Admit before checkout. The lease covers this session only, including the lazy
            # reaper's releases on the same org; upstream work happens after this returns.
            async with (
                reserve_admission(caller.org_id),
                money_session(session_maker()) as db,
            ):
                mark_money(db, "reserve", org_id=caller.org_id, call_id=call_ref)
                # The builder's own per-tag ceilings first: a refusal that belongs to ONE of their users
                # must not surface as the team-wide balance error, which names the builder's private numbers.
                with timing.phase("preflight"), money_stage(db, "preflight"):
                    await _enforce_tag_budgets(caller, meta, db, add_micro=held)
                    member_id = await _enforce_member_spend_cap(caller, charged, db)
                    # What the hold will take, margin applied, as the day's committed spend counts it.
                    await _enforce_platform_daily_cap(caller, charged, db)
                    await _enforce_trial_allowance(caller, mk.provider, mk.endpoint_id, db)
                try:
                    with timing.phase("ledger"):
                        mk.call_id = await ledger.reserve_in_transaction(
                            db, caller.org_id, mk.endpoint_id, held,
                            meta={"tier": "oauth" if mk.billed_oauth else "platform",
                                  "provider": mk.provider, "cost_type": mk.cost_type},
                            tags=meta.tags, call_id=call_ref,
                            purchased_only=_purchased_only(mk.endpoint_id), member_id=member_id)
                except ledger.InsufficientBalance:
                    await db.rollback()
                    # A refused call asks for a refill too, or a team at $0 with auto top-up on would stay
                    # empty, refusing every call. One read by primary key, refusals only; the wait check in
                    # `maybe_schedule_autotopup` keeps it from starting a task per call.
                    if auto_on:
                        mark_money(db, "reserve_refusal_read", org_id=caller.org_id, call_id=call_ref)
                        with money_stage(db, "post_refusal_read"):
                            refused_org = await db.get(Org, caller.org_id)
                    raise
                # A cancellation can reach `commit()` after the database committed;
                # only the session's commit event says the hold exists.
                event.listen(db.sync_session, "after_commit",
                             lambda _session: committed.append(True), once=True)
                with timing.phase("commit"):
                    await db.commit()
        if auto_on:
            await _schedule_autotopup_after(caller.org_id)
    except ledger.PurchasedCreditShort as exc:
        if refused_org is not None:
            billing.maybe_schedule_autotopup(refused_org)
        raise ReservationFailed("free_credit_excludes_video", status_code=402, detail={
            "error": "free_credit_excludes_video",
            "message": (f"Free credit does not cover video generation. {mk.endpoint_id} would cost "
                        f"~${ledger.usd(exc.required_micro):g} and this team has "
                        f"${ledger.usd(exc.purchased_micro):g} of purchased credit for it. "
                        f"Add funds to the team's balance to use it: "
                        f"{get_settings().public_url}/app#billing. Nothing was charged."),
            "balance_micro": exc.balance_micro,
            "purchased_balance_micro": exc.purchased_micro,
            "estimated_cost_micro": exc.required_micro,
            "topup_url": "/app#billing",
            "provider": mk.provider,
            "endpoint_id": mk.endpoint_id,
        }) from exc
    except ReservationFailed as exc:
        # The day's committed spend counts all of the allowance hold, what its counter still
        # offers included: give that back and run every check again.
        # A counter another request has just given back leaves nothing to give, and the day's
        # figure is then lower than the refusal read: read it again before refusing.
        cap = exc.detail if exc.kind == "platform_daily_cap_reached" else None
        if retry_short and isinstance(cap, dict) and allowance_runtime.listed(caller.org_id) and (
                await allowance_runtime.give_back_for_exact_path(
                    caller.org_id,
                    cap["spent_today_micro"] + cap["estimated_cost_micro"] - cap["daily_cap_micro"])
                or await _committed_today(caller.org_id) + cap["estimated_cost_micro"]
                <= cap["daily_cap_micro"]):
            noted = True
            return await _platform_reserve(
                mk, caller, meta, call_ref, allow_holdless=allow_holdless,
                mark_in_flight=mark_in_flight, retry_short=False,
                skip_reason=note, skip_as_direct=as_direct)
        raise
    except ledger.InsufficientBalance as exc:
        if refused_org is not None:
            billing.maybe_schedule_autotopup(refused_org)
        # The team's counter may hold what the balance is short of, or another request may just
        # have given it back: `onto_balance` puts it on the balance, or says it is not there.
        moved = retry_short and await allowance_runtime.onto_balance(
            caller.org_id, exc.required_micro)
        if moved:
            noted = True
            return await _platform_reserve(
                mk, caller, meta, call_ref, allow_holdless=allow_holdless,
                mark_in_flight=mark_in_flight, retry_short=False,
                skip_reason=note, skip_as_direct=as_direct)
        wallet, alt = wallet_and_alternative(mk)
        # A team that keeps hitting this by hand is the one that should hear about auto top-up; a team
        # that already has it on needs to know it is the cooldown/cap holding, not a missing card —
        # otherwise the natural reading of "add funds" is that auto top-up is broken.
        if auto_on:
            cap = prefs["monthly_cap_micro"]
            monthly = f" and ${ledger.usd(cap):g}/month" if cap else ""
            auto_line = (f"  auto top-up:    on — adds ${ledger.usd(prefs['amount_micro']):g} when the balance "
                         f"drops below ${ledger.usd(prefs['threshold_micro']):g}, at most "
                         f"{prefs['per_hour']} times per hour{monthly}. Raise the amount or the "
                         f"refills per hour if your burn outruns it: treg topup --auto on --amount 50 --per-hour 10")
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
        }) from exc
    except DBAPIError as exc:
        # The money session's statement_timeout cancelled a statement (57014): the transaction
        # rolled back before its commit, so nothing is held and the provider was not reached.
        if committed or getattr(exc.orig, "sqlstate", None) != "57014":
            raise
        retry_base_s = get_settings().money_admission_retry_after_s
        retry_after_s = random.randint(retry_base_s, 2 * retry_base_s)
        try:
            logging.getLogger("treg.money_admission").warning(
                "money_statement_timeout operation=reserve org_id=%s retry_after_s=%s",
                caller.org_id, retry_after_s)
        except Exception:  # noqa: BLE001 - diagnostics cannot change the refusal
            pass
        raise ReservationFailed("money_statement_timeout", status_code=503, detail={
            "error": "money_statement_timeout",
            "message": ("the team's money writes are slow right now; retry shortly. Nothing was "
                        "reserved or charged."),
        }, retry_after_s=retry_after_s) from exc
    finally:
        if not noted:
            _note_allowance_skip(caller.org_id, note, mk.reserved_micro if committed else 0,
                                 as_direct)
