"""Three query-time reports that answer "is the money real?" — asked of the rows phase 3 started
writing, never of a scheduler. Nothing here mutates anything: `domain/money` owns every write, and this
module is the read side that checks its work against the outside world.

The three questions, and why each one needs its own source of truth:

* **Price drift** (`price_drift`) — did the catalog's price stay true? Compares, per endpoint, the
  estimate we RESERVED against the cost the provider actually REPORTED, both recorded on the same
  `CallRecord` row. Estimates come from a hand-verified sweep; providers re-price whenever they like,
  so a silent 10% climb turns a positive margin negative with nothing on fire. This report is the
  only thing that notices.
* **Provider spend** (`provider_spend`) — reads the LEDGER, not the audit table, because it is the
  number a human holds next to an invoice or a balance delta. Audit rows are fire-and-forget and may
  be missing; ledger rows may not (see `models.LedgerEntry`).
* **Repeat rate** (`repeat_rate`) — measurement only, the groundwork the plan asks for BEFORE any
  cache exists: how much of the bill was the same query twice. Answering it is what makes a cache a
  decision instead of a guess.

Units are integer micro-USD (`*_micro`) with a display-only `*_usd` twin, same contract as
`domain/money`: never compute against the USD field.

Two aggregations deliberately happen in Python rather than SQL: the ledger's provenance lives in a
JSON `meta` column (portable JSON extraction across SQLite and Postgres is not worth a report), and
these are admin-scale windows over a bounded number of metered calls — the same tradeoff
`admin_stats` already makes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import distinct, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from .config import get_settings
from .models import AsyncTaskRecord, CallRecord, LedgerEntry

# How far the observed cost may sit from the estimate before the endpoint is worth a human's
# attention. 5% is under the platform margin, so a drift that trips this is still profitable — which
# is the point of catching it here rather than at the point it goes negative.
DRIFT_TOLERANCE = 0.05


def usd(amount_micro: int | float) -> float:
    """micro-USD → USD, for DISPLAY only (mirrors `ledger.usd`)."""
    return round(amount_micro / 1_000_000, 6)


def window_start(days: int) -> datetime:
    """Naive-UTC cutoff `days` back — the convention every timestamp column in this app stores."""
    days = max(1, min(int(days), 365))
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


async def async_task_settlement(db: AsyncSession, since: datetime) -> dict:
    """Pending age, absorbed timeouts (holds released after 24h, the platform ate any upstream
    charge - each one is a review item), per-provider terminal settlement outcomes, and the two
    places a usage-settled task can cost more than its reserve:

    - `overruns`: settled tasks whose provider-reported cost exceeded the reserve (the rate-card
      estimate). The TEAM paid the difference from its balance; this is where an unpublished
      minimum charge (Wan 3.0, live 2026-09-02) shows up as a pattern worth encoding as data.
    - `absorbed_shortfalls`: settle entries whose `block_shortfall_micro` > 0 - the team's blocks
      could not cover the settle, so the PLATFORM absorbed the rest. Read from the ledger, all
      settles in the window, not only async ones.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = (await db.execute(select(AsyncTaskRecord).where(
        (AsyncTaskRecord.status == "pending") | (AsyncTaskRecord.completed_at >= since)))).scalars().all()
    ages = {"under_5m": 0, "5m_to_1h": 0, "1h_to_6h": 0, "6h_to_24h": 0, "over_24h": 0}
    absorbed = []
    overruns = []
    providers: dict[str, dict] = {}
    for row in rows:
        if row.status == "settled" and row.completed_at and row.completed_at >= since \
                and (row.settled_micro or 0) > row.reserved_micro:
            overruns.append({
                "call_id": row.call_id, "org_id": row.org_id, "provider": row.provider,
                "endpoint_id": row.endpoint_id, "reserved_micro": row.reserved_micro,
                "settled_micro": row.settled_micro,
                "overrun_micro": row.settled_micro - row.reserved_micro,
                "ratio": round(row.settled_micro / row.reserved_micro, 4) if row.reserved_micro else None,
                "completed_at": row.completed_at.isoformat()})
        if row.status == "pending":
            seconds = max(0, (now - row.created_at).total_seconds())
            bucket = ("under_5m" if seconds < 300 else "5m_to_1h" if seconds < 3600 else
                      "1h_to_6h" if seconds < 21600 else "6h_to_24h" if seconds < 86400 else
                      "over_24h")
            ages[bucket] += 1
        if row.status == "timed_out" and row.completed_at and row.completed_at >= since:
            absorbed.append({
                "call_id": row.call_id, "org_id": row.org_id, "provider": row.provider,
                "endpoint_id": row.endpoint_id, "reserved_micro": row.reserved_micro,
                "settled_micro": row.settled_micro, "completed_at": row.completed_at.isoformat(),
            })
        if row.completed_at and row.completed_at >= since and row.status in ("settled", "released"):
            item = providers.setdefault(row.provider, {
                "provider": row.provider, "successes": 0, "failures": 0, "settled_micro": 0})
            if row.status == "settled":
                item["successes"] += 1
                item["settled_micro"] += int(row.settled_micro or 0)
            else:
                item["failures"] += 1
    output = []
    for item in providers.values():
        total = item["successes"] + item["failures"]
        output.append({**item, "tasks": total,
                       "success_rate": round(item["successes"] / total, 4) if total else None,
                       "settled_usd": usd(item["settled_micro"])})
    output.sort(key=lambda item: item["provider"])
    absorbed.sort(key=lambda item: item["completed_at"], reverse=True)
    overruns.sort(key=lambda item: item["overrun_micro"], reverse=True)
    by_endpoint: dict[str, dict] = {}
    for item in overruns:
        agg = by_endpoint.setdefault(item["endpoint_id"], {
            "endpoint_id": item["endpoint_id"], "provider": item["provider"], "tasks": 0,
            "overrun_micro": 0, "max_ratio": 0.0})
        agg["tasks"] += 1
        agg["overrun_micro"] += item["overrun_micro"]
        agg["max_ratio"] = max(agg["max_ratio"], item["ratio"] or 0.0)
    settles = (await db.execute(select(LedgerEntry).where(
        LedgerEntry.kind == "settle", LedgerEntry.created_at >= since))).scalars().all()
    shortfalls = [{
        "call_id": e.call_id, "org_id": e.org_id, "endpoint_id": e.endpoint_id,
        "settled_micro": int((e.meta or {}).get("settled_micro") or 0),
        "consumed_micro": int((e.meta or {}).get("consumed_micro") or 0),
        "shortfall_micro": int((e.meta or {}).get("block_shortfall_micro") or 0),
        "created_at": e.created_at.isoformat() if e.created_at else None,
    } for e in settles if int((e.meta or {}).get("block_shortfall_micro") or 0) > 0]
    shortfalls.sort(key=lambda item: item["shortfall_micro"], reverse=True)
    return {"pending": sum(ages.values()), "pending_age": ages,
            "absorbed_timeouts": absorbed, "providers": output,
            "overruns": overruns,
            "overruns_by_endpoint": sorted(by_endpoint.values(), key=lambda a: -a["overrun_micro"]),
            "absorbed_shortfalls": shortfalls,
            "absorbed_shortfall_micro": sum(item["shortfall_micro"] for item in shortfalls)}


async def price_drift(
    db: AsyncSession, since: datetime, min_calls: int = 3, *, tolerance: float = DRIFT_TOLERANCE,
) -> list[dict]:
    """Per (endpoint, provider): what we estimated vs what the provider charged, since `since`.

    Only rows carrying BOTH numbers count, which restricts this to the providers that report their
    own charge in-band — today `dataforseo` (top-level `cost`) and `scrapecreators`
    (`credits_charged`), per `api._observed_cost_micro`. Everyone else settles at the estimate, so
    there is no second opinion to compare against and their endpoints simply never appear here. That
    is a real limit of the report, not a bug: drift on a silent provider is only visible on their
    invoice (see `provider_spend`).

    `flagged` is the actionable bit: at least `min_calls` samples AND a mean observed cost more than
    `tolerance` away from the mean estimate — a single call can be an outlier (a `per_result` route
    that returned one row), a run of them is a re-price. A zero mean estimate has no ratio to
    compute, so `drift_ratio` is None and the flag falls back to "we thought it was free and it
    wasn't".

    Sorted worst-drift-first. Amounts are means over the window, not per-call truth: a `per_result`
    endpoint legitimately costs a different amount every call, which is exactly why the flag needs
    several samples before it fires.
    """
    rows = (await db.execute(
        select(
            CallRecord.endpoint_id, CallRecord.provider, func.count(),
            func.sum(CallRecord.cost_estimated_micro), func.sum(CallRecord.cost_observed_micro),
            func.max(CallRecord.cost_observed_micro), func.max(CallRecord.cost_estimated_micro),
        )
        .where(CallRecord.created_at >= since,
               CallRecord.cost_estimated_micro.is_not(None),
               CallRecord.cost_observed_micro.is_not(None))
        .group_by(CallRecord.endpoint_id, CallRecord.provider)
    )).all()

    out: list[dict] = []
    for endpoint_id, provider, calls, est_sum, obs_sum, obs_max, est_max in rows:
        calls = int(calls or 0)
        if not calls:
            continue
        est_mean = (est_sum or 0) / calls
        obs_mean = (obs_sum or 0) / calls
        ratio = (obs_mean - est_mean) / est_mean if est_mean > 0 else None
        drifting = abs(ratio) > tolerance if ratio is not None else obs_mean > 0
        out.append({
            "endpoint_id": endpoint_id, "provider": provider, "calls": calls,
            "estimate_mean_micro": round(est_mean, 3), "estimate_max_micro": int(est_max or 0),
            "observed_mean_micro": round(obs_mean, 3), "observed_max_micro": int(obs_max or 0),
            "estimate_mean_usd": usd(est_mean), "observed_mean_usd": usd(obs_mean),
            "drift_ratio": round(ratio, 4) if ratio is not None else None,
            "drift_micro": round(obs_mean - est_mean, 3),
            "flagged": bool(drifting and calls >= min_calls),
        })
    out.sort(key=lambda r: (-abs(r["drift_ratio"]) if r["drift_ratio"] is not None else -1e9,
                            -r["calls"]))
    return out


async def provider_spend(db: AsyncSession, since: datetime) -> dict:
    """Settled platform spend per provider since `since` — the figure to hold next to the provider's
    own invoice or balance delta.

    Reads `settle` ledger entries, which exist ONLY for platform-tier calls (a call on the org's own
    credential spends nobody's balance and never reserves), so no tier filter is needed beyond that;
    `meta.tier` is honoured when a caller has stamped one. Three numbers come back per provider and
    they are not interchangeable:

      * `charged_micro` — what orgs were billed, margin included. Revenue, not cost.
      * `provider_reported_micro` — the provider's own reported charge, summed over the calls where
        it reported one. A true subtotal of the invoice, covering `reported_calls` of `calls`.
      * `provider_cost_est_micro` — `charged / (1 + margin)`: the whole window's spend backed out of
        the charge. This is the apples-to-apples invoice comparison, and it is an ESTIMATE for every
        call the provider stayed quiet about.

    A gap between `provider_cost_est_micro` and the invoice is either drift (`price_drift` names the
    endpoint) or calls we billed and they didn't, or the reverse. Either way margin was a guess until
    this report is read.
    """
    entries = (await db.execute(
        select(LedgerEntry).where(LedgerEntry.kind == "settle", LedgerEntry.created_at >= since)
    )).scalars().all()

    margin = float(get_settings().platform_margin or 0.0)
    per: dict[str, dict] = {}
    for e in entries:
        meta = e.meta or {}
        if meta.get("tier") not in (None, "", "platform"):
            continue
        p = per.setdefault(str(meta.get("provider") or "unknown"), {
            "provider": str(meta.get("provider") or "unknown"), "calls": 0, "reported_calls": 0,
            "charged_micro": 0, "provider_reported_micro": 0, "orgs": set(), "shortfall_micro": 0,
        })
        p["calls"] += 1
        p["charged_micro"] += int(-e.amount_micro)  # settle entries are negative (money leaving)
        p["orgs"].add(e.org_id)
        p["shortfall_micro"] += int(meta.get("block_shortfall_micro") or 0)
        observed = meta.get("observed_micro")
        if isinstance(observed, (int, float)) and not isinstance(observed, bool):
            p["reported_calls"] += 1
            p["provider_reported_micro"] += int(observed)

    rows = []
    for p in per.values():
        charged = p["charged_micro"]
        cost_est = int(charged / (1.0 + margin)) if margin > 0 else charged
        rows.append({**p, "orgs": len(p["orgs"]),
                     "charged_usd": usd(charged),
                     "provider_reported_usd": usd(p["provider_reported_micro"]),
                     "provider_cost_est_micro": cost_est,
                     "provider_cost_est_usd": usd(cost_est)})
    rows.sort(key=lambda r: -r["charged_micro"])
    total = sum(r["charged_micro"] for r in rows)
    return {"margin": margin, "providers": rows, "calls": sum(r["calls"] for r in rows),
            "charged_micro": total, "charged_usd": usd(total)}


async def repeat_rate(db: AsyncSession, since: datetime, *, top: int = 10) -> dict:
    """How much of the bill was the same call twice, per provider, plus the worst offenders.

    `params_hash` is an identity for "this exact call again" (`api._params_hash`: endpoint + sorted
    query + a digest of the body — bodies are never stored). `repeat_calls = calls - distinct` is
    therefore the number of calls a cache with an infinite TTL would have served for free, and
    `repeat_cost_micro` is what they cost. Cost per row is the observed charge when the provider
    reported one, else the estimate we reserved.

    Measurement only. Nothing here is safe to cache on its own: a cache needs `kind: data` +
    `scope: any_account`, a per-endpoint TTL and a per-provider ToS check on response retention — the
    conditions the plan spells out. This report only says whether it would be worth the trouble.
    """
    rows = (await db.execute(
        select(CallRecord.provider, func.count(), func.count(distinct(CallRecord.params_hash)),
               func.sum(func.coalesce(CallRecord.cost_observed_micro,
                                      CallRecord.cost_estimated_micro, 0)))
        .where(CallRecord.created_at >= since, CallRecord.params_hash.is_not(None),
               CallRecord.params_hash != "", CallRecord.provider.is_not(None))
        .group_by(CallRecord.provider)
    )).all()

    providers = []
    for provider, calls, uniq, cost in rows:
        calls, uniq = int(calls or 0), int(uniq or 0)
        repeats = max(0, calls - uniq)
        providers.append({
            "provider": provider, "calls": calls, "distinct_params": uniq, "repeat_calls": repeats,
            "repeat_ratio": round(repeats / calls, 4) if calls else 0.0,
            "cost_micro": int(cost or 0), "cost_usd": usd(int(cost or 0)),
        })
    providers.sort(key=lambda r: -r["repeat_calls"])

    # The individual queries worth a cache entry: same params_hash more than once, dearest first.
    # `min(endpoint_id)` because a hash already encodes the endpoint — the GROUP BY just needs a name.
    hot = (await db.execute(
        select(CallRecord.params_hash, func.min(CallRecord.endpoint_id), func.min(CallRecord.provider),
               func.count(), func.sum(func.coalesce(CallRecord.cost_observed_micro,
                                                    CallRecord.cost_estimated_micro, 0)))
        .where(CallRecord.created_at >= since, CallRecord.params_hash.is_not(None),
               CallRecord.params_hash != "")
        .group_by(CallRecord.params_hash)
        .having(func.count() > 1)
        .order_by(func.count().desc())
        .limit(max(1, min(int(top), 100)))
    )).all()
    top_repeated = [
        {"params_hash": h, "endpoint_id": ep, "provider": prov, "calls": int(n or 0),
         "cost_micro": int(cost or 0), "cost_usd": usd(int(cost or 0)),
         # What the repeats (every call after the first) cost — a cache's actual saving, assuming the
         # calls were identical in price, which a same-hash call to a per_call endpoint is.
         "repeat_cost_micro": int((cost or 0) * (int(n or 1) - 1) / int(n or 1))}
        for h, ep, prov, n, cost in hot
    ]
    totals_calls = sum(p["calls"] for p in providers)
    totals_repeats = sum(p["repeat_calls"] for p in providers)
    return {
        "calls": totals_calls, "repeat_calls": totals_repeats,
        "repeat_ratio": round(totals_repeats / totals_calls, 4) if totals_calls else 0.0,
        "providers": providers, "top_repeated": top_repeated,
    }


async def shared_plan_recovery(db: AsyncSession, since: datetime) -> dict:
    """Fee versus collected, per provider whose rate TREG SET (fx.yaml `kind: treg_shared_plan`).

    This is the monthly review's input (docs/SHARED-PLAN-PRICING-PLAN.md): a flat-fee subscription
    has no vendor per-call price, so treg publishes its own rate with a stated break-even, and this
    report is what keeps that rate honest over time. It REPORTS; a human edits fx.yaml. An
    auto-adjusting price would move under an agent's feet mid-session, and a rate card that moves on
    its own is not a rate card.

    Three numbers and a suggestion per provider:
      * `fee_micro`        — the subscription, from fx.yaml `fee_usd_month`, scaled to the window
                             (a 30-day report on a monthly fee compares like with like; a 7-day one
                             compares against a quarter of the fee, not all of it).
      * `collected_micro`  — settled platform charges for the provider in the window.
      * `recovery`         — collected / fee. 1.0 is break-even.
      * `suggested_usd`    — fee ÷ measured calls: the rate at which THIS volume would break even.
                             None when there were no calls, because $∞/call is not a suggestion.

    The action field applies the review rule mechanically so the reviewer applies judgement, not
    arithmetic: well over fee → "lower toward suggested"; well under → "raise toward suggested, or
    demote to own-key-only"; near 1.0 → "on target". Thresholds are ±50%, wide on purpose — a
    monthly hand edit should chase real drift, not noise.
    """
    catalog = _catalog().load()
    plans = catalog.shared_plans
    if not plans:
        return {"providers": [], "note": "no shared-plan rates configured"}

    window_days = max((datetime.now(timezone.utc).replace(tzinfo=None) - since).days, 1)
    entries = (await db.execute(
        select(LedgerEntry).where(LedgerEntry.kind == "settle", LedgerEntry.created_at >= since)
    )).scalars().all()

    per: dict[str, dict] = {}
    for e in entries:
        meta = e.meta or {}
        provider = str(meta.get("provider") or "")
        if provider not in plans:
            continue
        row = per.setdefault(provider, {"calls": 0, "collected_micro": 0})
        row["calls"] += 1
        row["collected_micro"] += abs(e.amount_micro or 0)

    out = []
    for provider, plan in sorted(plans.items()):
        fee_month = float(plan.get("fee_usd_month") or 0)
        fee_micro = int(round(fee_month * 1_000_000 * window_days / 30))
        row = per.get(provider, {"calls": 0, "collected_micro": 0})
        calls, collected = row["calls"], row["collected_micro"]
        recovery = (collected / fee_micro) if fee_micro else None
        suggested = round(fee_micro / calls / 1_000_000, 9) if calls else None
        if recovery is None:
            action = "no fee recorded — fix fx.yaml"
        elif calls == 0:
            action = "no calls — raise the question of demoting to own-key-only"
        elif recovery > 1.5:
            action = f"over-recovered — consider lowering toward ${suggested}"
        elif recovery < 0.5:
            action = f"under-recovered — raise toward ${suggested}, or demote to own-key-only"
        else:
            action = "on target"
        out.append({
            "provider": provider, "rate_usd": plan.get("usd"), "fee_usd_month": fee_month,
            "window_days": window_days, "calls": calls,
            "fee_micro": fee_micro, "collected_micro": collected,
            "recovery": round(recovery, 4) if recovery is not None else None,
            "suggested_usd": suggested, "action": action,
        })
    return {"providers": out}


def _catalog():
    """Deferred so reconcile stays importable without the catalog package in odd contexts, and so
    tests can monkeypatch the module in one obvious place."""
    from .domain.catalog import store as catalog_store

    return catalog_store
