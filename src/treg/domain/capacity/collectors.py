"""The free balance / quota collectors for every provider treg holds a platform key for.

Moved unchanged from `scripts/provider_balances.py` (the script is now a thin CLI over this
module). Each collector is `coroutine(client, key) -> {"value", "unit", "note"}`: the provider's
own free account/limits call, never a metered one, so a sweep can run as often as it likes.

Pure collection: nothing here touches the database or the request path. The worker sweep
(`treg.domain.capacity.sweep`) turns these dicts into `CapacitySnapshot` rows.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

import httpx

from ...config import get_settings, platform_setting_name

# provider → coroutine(client, key) → {"value": float|None, "unit": str, "note": str}.
# `unit` says what the number IS ("USD", "credits", "units left", "rows used") — the one lesson of
# collecting these: only DataForSEO and TikHub speak dollars; everyone else meters something else.


async def _get(c: httpx.AsyncClient, url: str, **kw) -> dict:
    r = await c.get(url, **kw)
    r.raise_for_status()
    return r.json()


async def _dataforseo(c, key):
    d = await _get(c, "https://api.dataforseo.com/v3/appendix/user_data",
                   headers={"Authorization": f"Basic {key}"})
    money = (d.get("tasks") or [{}])[0].get("result", [{}])[0].get("money", {})
    return {"value": money.get("balance"), "unit": "USD", "note": ""}


async def _tikhub(c, key):
    d = await _get(c, "https://api.tikhub.io/api/v1/tikhub/user/get_user_info",
                   headers={"Authorization": f"Bearer {key}"})
    return {"value": (d.get("user_data") or {}).get("balance"), "unit": "USD", "note": ""}


async def _tinyfish(c, key):
    d = await _get(c, "https://agent.tinyfish.ai/v1/wallet",
                   headers={"X-API-Key": key})
    raw = d.get("available_balance")
    try:
        balance = Decimal(str(raw)) if not isinstance(raw, bool) and raw is not None else None
    except InvalidOperation:
        balance = None
    if balance is None or not balance.is_finite() or balance < 0:
        raise ValueError("TinyFish wallet returned an invalid available_balance")
    reload_state = d.get("auto_reload")
    if isinstance(reload_state, dict) and isinstance(reload_state.get("state"), str):
        note = f"vendor auto-reload {reload_state['state']}"
    elif reload_state is True:
        note = "vendor auto-reload enabled"
    elif reload_state is False:
        note = "vendor auto-reload not enabled"
    else:
        note = "vendor auto-reload state unavailable"
    return {"value": float(balance), "unit": str(d.get("currency") or "USD").upper(),
            "note": note}


async def _fishaudio(c, key):
    workspace_id = get_settings().platform_fishaudio_workspace_id.strip()
    if not workspace_id:
        return {
            "value": None,
            "unit": "USD",
            "note": "TREG_PLATFORM_FISHAUDIO_WORKSPACE_ID is not configured; "
                    "the unscoped route reports a separate personal wallet and is not used",
        }
    d = await _get(
        c,
        "https://api.fish.audio/wallet/self/api-credit",
        headers={"Authorization": f"Bearer {key}"},
        params={"team_id": workspace_id},
    )
    raw = d.get("credit") if isinstance(d, dict) else None
    try:
        credit = Decimal(str(raw)) if not isinstance(raw, bool) and raw is not None else None
    except (InvalidOperation, ValueError):
        credit = None
    value = float(credit) if credit is not None and credit.is_finite() and credit >= 0 else None
    return {
        "value": value,
        "unit": "USD",
        "note": "Workspace API-credit balance; funding and top-ups are operator-managed",
    }


async def _tavily(c, key):
    d = await _get(c, "https://api.tavily.com/usage",
                   headers={"Authorization": f"Bearer {key}"})

    def remaining(meter, used_name, limit_name):
        used, limit = meter.get(used_name), meter.get(limit_name)
        if (isinstance(used, (int, float)) and not isinstance(used, bool)
                and isinstance(limit, (int, float)) and not isinstance(limit, bool)
                and math.isfinite(float(used)) and math.isfinite(float(limit))
                and used >= 0 and limit >= 0):
            return max(0, limit - used)
        return None

    meter = d.get("key") or {}
    key_remaining = remaining(meter, "usage", "limit")
    if key_remaining is not None:
        return {"value": key_remaining, "unit": "API credits",
                "note": f"key usage {meter['usage']:g} of {meter['limit']:g}; account pools are informational"}
    # A key with no configured per-key cap returns `limit: null` even though its account plan has a
    # finite pool. That is the normal shape of an unrestricted Tavily key, not an unknown balance.
    account = d.get("account") or {}
    plan = remaining(account, "plan_usage", "plan_limit")
    paygo = remaining(account, "paygo_usage", "paygo_limit")
    known = [value for value in (plan, paygo) if value is not None]
    if known:
        pools = ", ".join(name for name, value in (("plan", plan), ("PAYGO", paygo))
                          if value is not None)
        return {"value": sum(known), "unit": "API credits",
                "note": f"key has no finite cap; remaining {pools} account pool(s)"}
    return {"value": None, "unit": "API credits",
            "note": "Usage response did not contain a finite key or account limit"}


async def _serper(c, key):
    d = await _get(c, "https://google.serper.dev/account",
                   headers={"X-API-KEY": key})
    raw = d.get("balance") if isinstance(d, dict) else None
    try:
        balance = Decimal(str(raw)) if not isinstance(raw, bool) and raw is not None else None
    except (InvalidOperation, ValueError):
        balance = None
    if balance is None or not balance.is_finite() or balance < 0:
        raise ValueError("Serper account returned an invalid balance")
    rate = d.get("rateLimit")
    rate_note = (f"account rate limit {rate:g} queries/s"
                 if isinstance(rate, (int, float)) and not isinstance(rate, bool)
                 and math.isfinite(float(rate)) and rate >= 0
                 else "account rate limit unavailable")
    return {"value": float(balance), "unit": "credits", "note": rate_note}


async def _olostep(c, key):
    # Free authenticated account read. `credits` is the authoritative sum of unexpired lots;
    # endpoint responses report their own `credits_consumed`, which settlement handles separately.
    d = await _get(c, "https://api.olostep.com/user/credits/info",
                   headers={"Authorization": f"Bearer {key}"})
    subscription = d.get("active_subscription") or {}
    plan = subscription.get("display_name") or subscription.get("id") or "unknown"
    allowed = d.get("allow_usage")
    state = "allowed" if allowed is True else "blocked" if allowed is False else "unknown"
    return {"value": d.get("credits"), "unit": "credits",
            "note": f"plan {plan}; usage {state}"}


async def _scrapegraphai(c, key):
    d = await _get(c, "https://v2-api.scrapegraphai.com/api/credits",
                   headers={"SGAI-APIKEY": key})
    raw = d.get("remaining") if isinstance(d, dict) else None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) \
            or not math.isfinite(float(raw)) or raw < 0:
        raise ValueError("ScrapeGraphAI returned no valid remaining-credit balance")
    jobs = d.get("jobs") if isinstance(d.get("jobs"), dict) else {}
    crawl, monitor = jobs.get("crawl") or {}, jobs.get("monitor") or {}
    return {
        "value": raw,
        "unit": "credits",
        "note": (
            f"plan {d.get('plan') or 'unknown'}; used {d.get('used', '?')}; "
            f"crawl jobs {crawl.get('used', '?')}/{crawl.get('limit', '?')}; "
            f"monitors {monitor.get('used', '?')}/{monitor.get('limit', '?')}"
        ),
    }


async def _scrapecreators(c, key):
    d = await _get(c, "https://api.scrapecreators.com/v1/account/credit-balance",
                   headers={"x-api-key": key})
    return {"value": d.get("creditCount"), "unit": "credits", "note": ""}


async def _serpapi(c, key):
    d = await _get(c, "https://serpapi.com/account.json", params={"api_key": key})
    left = d.get("total_searches_left", d.get("plan_searches_left"))
    return {"value": left, "unit": "searches left",
            "note": f"plan {d.get('plan_id')}, {d.get('this_month_usage')} used this month"}


async def _moz(c, key):
    # Rows CONSUMED this period (there is no remaining-rows call) — trend it against the plan size.
    r = await c.post("https://api.moz.com/v2/usage_data",
                     headers={"Authorization": f"Basic {key}"}, json={})
    r.raise_for_status()
    return {"value": r.json().get("rows_consumed"), "unit": "rows USED this period",
            "note": "Moz reports usage, not remaining — compare against the plan's row quota"}


async def _seranking(c, key):
    # Two pools: the monthly subscription bucket AND a non-expiring top-up wallet. The old
    # /account/subscription call only saw the first, reporting "0 left" while 248k sat in the
    # wallet (2026-08-20). /account/credits reports both plus which one is granting access.
    d = await _get(c, "https://api.seranking.com/v1/account/credits",
                   headers={"Authorization": f"Token {key}"})
    t = d.get("totals", {})
    sub, wallet = t.get("subscription", {}), t.get("wallet", {})
    total = (sub.get("remaining") or 0) + (wallet.get("remaining") or 0)
    reason = (d.get("access") or {}).get("primary_reason", "?")
    return {"value": total, "unit": "credits left",
            "note": f"wallet {wallet.get('remaining', 0):,} (non-expiring) + subscription "
                    f"{sub.get('remaining', 0):,}/{sub.get('total', 0):,} (resets "
                    f"{sub.get('expire_at', '?')}); access via {reason}"}


async def _sumble(c, key):
    r = await c.post("https://api.sumble.com/v9/technologies/find",
                     headers={"Authorization": f"Bearer {key}"},
                     json={"query": "treg-nonexistent-probe-20260909"})
    r.raise_for_status()
    doc = r.json()
    remaining = doc.get("credits_remaining") if isinstance(doc, dict) else None
    if type(remaining) is not int or remaining < 0:
        remaining = None
    return {"value": remaining, "unit": "credits",
            "note": "Monthly allowance plus purchased credits; renewal date and auto-top-up state not reported."}


async def _moltsets(c, key):
    r = await c.post("https://api.moltsets.com/api/v1/tools/get_account",
                     headers={"Authorization": f"Bearer {key}",
                              "User-Agent": "treg/1.0 (+https://treg.to)"}, json={})
    r.raise_for_status()
    doc = r.json()
    account = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(account, dict) or doc.get("status") != "ok":
        raise ValueError("MoltSets account probe returned an invalid response")
    fair_use = account.get("fair_use")
    enrich = fair_use.get("enrich") if isinstance(fair_use, dict) else None
    records = enrich.get("records") if isinstance(enrich, dict) else None
    remaining = []
    if isinstance(records, dict):
        for window in ("5h", "1w"):
            row = records.get(window)
            value = row.get("remaining") if isinstance(row, dict) else None
            if type(value) is int and value >= 0:
                remaining.append(value)
    value = min(remaining) if remaining else None

    def left(kind, meter, window):
        section = fair_use.get(kind) if isinstance(fair_use, dict) else None
        pool = section.get(meter) if isinstance(section, dict) else None
        row = pool.get(window) if isinstance(pool, dict) else None
        return row.get("remaining") if isinstance(row, dict) else None

    return {
        "value": value,
        "unit": "enrichment records",
        "note": f"plan {account.get('plan', '?')}; enrichment records "
                f"{left('enrich', 'records', '5h')}/5h, {left('enrich', 'records', '1w')}/week; "
                f"search records {left('search', 'records', '5h')}/5h, "
                f"{left('search', 'records', '1w')}/week; requests "
                f"{left('enrich', 'requests', '5h')}/5h enrichment, "
                f"{left('search', 'requests', '5h')}/5h search. Phone/token balances are separate "
                "meters and are never substituted for enrichment capacity.",
    }


async def _openmart(c, key):
    d = await _get(c, "https://api.openmart.ai/api/v2/credit-balance",
                   headers={"Authorization": f"Bearer {key}"})
    balance = d.get("balance") if isinstance(d, dict) else None
    if type(balance) is not int or balance < 0:
        balance = None
    period_end = d.get("period_end") if isinstance(d, dict) else None
    return {"value": balance, "unit": "credits",
            "note": f"Monthly subscription balance; current period ends {period_end or 'at the account renewal date'}."}


async def _harvestapi(c, key):
    d = await _get(c, "https://api.harvestapi.io/users/my-api-user",
                   headers={"X-API-Key": key})
    usage = d.get("usage") if isinstance(d, dict) else None
    remaining = usage.get("balance") if isinstance(usage, dict) else None
    if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining < 0:
        remaining = None
    return {"value": remaining, "unit": "USD",
            "note": "Prepaid wallet; usage.balance is remaining, user.totalBalance is not. "
                    "Starter: 5 concurrent requests and queue of 10; no RPM cap. "
                    "Auto top-up is managed in HarvestAPI."}


async def _fetchinio(c, key):
    d = await _get(c, "https://api.fetchin.io/api/v1/subscription",
                   headers={"X-API-Key": key})
    remaining = d.get("creditsRemaining") if isinstance(d, dict) else None
    if type(remaining) not in (int, float) or not math.isfinite(remaining) or remaining < 0:
        raise ValueError("Fetchin returned no valid remaining-credit balance")
    rps = d.get("rpsLimit")
    renewal = d.get("renewalDate")
    payg = d.get("paygCreditsRemaining")
    return {
        "value": remaining,
        "unit": "credits",
        "note": (f"plan {d.get('plan', 'unknown')}, status {d.get('status', 'unknown')}; "
                 f"PAYG {payg if type(payg) in (int, float) else 'unknown'}; "
                 f"renews {renewal or 'not scheduled'}; "
                 f"account limit {rps if type(rps) is int else 'unknown'} requests/s"),
    }


async def _dropleads(c, key):
    d = await _get(c, "https://prime.dropleads.io/api/v2/prime-db/credits/balance",
                   headers={"X-API-Key": key})
    credits = d.get("credits") if isinstance(d, dict) and d.get("success") is True else None
    remaining = credits.get("totalAvailable") if isinstance(credits, dict) else None
    if (type(remaining) not in (int, float) or not math.isfinite(remaining)
            or remaining < 0):
        remaining = None
    subscription = credits.get("subscription") if isinstance(credits, dict) else None
    payg = credits.get("payg") if isinstance(credits, dict) else None
    use_payg = credits.get("usePayg") if isinstance(credits, dict) else None
    return {
        "value": remaining,
        "unit": "credits",
        "note": f"subscription {subscription}, PAYG {payg}, use PAYG {use_payg}; "
                "totalAvailable is the spendable balance",
    }


async def _quickenrich(c, key):
    # Free discovery carries the remaining subscription allowance; no account endpoint exists.
    r = await c.post("https://app.quickenrich.io/api/employees/contact-finder",
                     headers={"Authorization": f"Bearer {key}"},
                     json={"company_url": {"include": ["treg-probe-nonexistent.invalid"], "exclude": []},
                           "per_page": 1})
    r.raise_for_status()
    doc = r.json()
    meta = doc.get("meta") if isinstance(doc, dict) else None
    remaining = meta.get("remaining_credits") if isinstance(meta, dict) else None
    # Missing or unclear allowance data is unknown, never evidence of an unlimited plan.
    if not isinstance(doc, dict) or doc.get("success") is not True or type(remaining) is not int or remaining < 0:
        return {"value": None, "unit": "credits", "note": "No finite subscription allowance reported; check QuickEnrich plan"}
    return {"value": remaining, "unit": "credits",
            "note": "Subscription allowance; resets at renewal, no auto-top-up. Reset date not reported."}


async def _prospeo(c, key):
    d = await _get(c, "https://api.prospeo.io/account-information",
                   headers={"X-KEY": key})
    response = d.get("response") if isinstance(d, dict) and d.get("error") is False else None
    remaining = response.get("remaining_credits") if isinstance(response, dict) else None
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) \
            or not math.isfinite(remaining) or remaining < 0:
        raise ValueError("Prospeo returned no valid remaining-credit balance")
    return {
        "value": remaining,
        "unit": "credits",
        "note": (f"plan {response.get('current_plan', 'unknown')}, "
                 f"{response.get('used_credits', 'unknown')} used, renews "
                 f"{response.get('next_quota_renewal_date', 'unknown')}"),
    }


async def _aiark(c, key):
    d = await _get(c, "https://api.ai-ark.com/api/developer-portal/v1/payments/credits",
                   headers={"X-TOKEN": key, "Content-Type": "application/json"})
    remaining = d.get("total") if isinstance(d, dict) else None
    if (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining) or remaining < 0):
        raise ValueError("AI Ark returned no valid remaining-credit balance")
    return {
        "value": remaining,
        "unit": "credits",
        "note": "Monthly subscription credits; unused credits can roll over to twice the allowance",
    }


async def _wiza(c, key):
    d = await _get(c, "https://wiza.co/api/meta/credits",
                   headers={"Authorization": f"Bearer {key}"})
    credits = d.get("credits") if isinstance(d, dict) else None
    remaining = credits.get("api_credits") if isinstance(credits, dict) else None
    if (isinstance(remaining, bool) or not isinstance(remaining, (int, float))
            or not math.isfinite(remaining) or remaining < 0):
        raise ValueError("Wiza returned no valid API credit balance")
    return {
        "value": remaining,
        "unit": "API credits",
        "note": "Prepaid API credits; vendor auto-top-up is not enabled",
    }


async def _getleadsio(c, key):
    d = await _get(c, "https://app.getleads.io/api/v1/usage/fair-use",
                   headers={"Authorization": f"Bearer {key}"})
    remaining = d.get("credits_remaining") if isinstance(d, dict) and d.get("ok") is True else None
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)) \
            or not math.isfinite(remaining) or remaining < 0:
        raise ValueError("GetLeads.io returned no valid remaining-credit balance")
    return {
        "value": remaining,
        "unit": "credits",
        "note": ("Promotional database-credit allocation; no published USD replacement price. "
                 "The separate Live Leads wallet is not included."),
    }


async def _hunter(c, key):
    d = await _get(c, "https://api.hunter.io/v2/account", params={"api_key": key})
    req = (d.get("data") or {}).get("requests", {})
    s, v = req.get("searches", {}), req.get("verifications", {})
    # Use Hunter's own `remaining` — `available - used` goes negative once credit packages are
    # stacked on the plan (used counts lifetime-in-period, available is the pack sum; the dashboard
    # shows `remaining`). Verified 2026-08-26: remaining=25 while available-used=-849.
    cr = req.get("credits", {})
    return {"value": s.get("remaining", s.get("available", 0) - s.get("used", 0)),
            "unit": "searches left",
            "note": f"verifications {v.get('remaining', 0)} left, credits {cr.get('remaining')}, "
                    f"plan {(d.get('data') or {}).get('plan_name')}, "
                    f"resets {(d.get('data') or {}).get('reset_date')}"}


async def _trykitt(c, key):
    d = await _get(c, "https://api.trykitt.ai/credit", headers={"x-api-key": key})
    value = d.get("credits")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return {"value": None, "unit": "USD", "note": "Missing Kitt balance"}
    return {"value": value, "unit": "USD", "note": ""}


async def _contactout(c, key):
    d = await _get(c, "https://api.contactout.com/v1/stats",
                   headers={"token": key, "Accept": "application/json"})
    if not isinstance(d, dict) or d.get("status_code") != 200 or not isinstance(d.get("usage"), dict):
        raise ValueError("ContactOut returned no valid usage stats")
    usage = d["usage"]
    pools = []
    for label, prefix in (("email", ""), ("phone", "phone_"), ("search", "search_")):
        count, quota = usage.get(prefix + "count"), usage.get(prefix + "quota")
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in (count, quota)):
            raise ValueError("ContactOut returned incomplete credit pools")
        remaining = usage.get(prefix + "remaining")
        if remaining is not None and (isinstance(remaining, bool) or not isinstance(remaining, (int, float))):
            raise ValueError("ContactOut returned invalid remaining credits")
        # Prepaid: quota is remaining already. Postpaid supplies remaining explicitly.
        pools.append(f"{label}: used={count}, quota={quota}" +
                     (f", remaining={remaining}" if remaining is not None else ""))
    # Three non-interchangeable pools cannot become one provider-wide exhaustion number.
    # Pools are independent; the account manager monitors usage and arranges top-ups.
    return {"value": None, "unit": "credit pools", "informational": True,
            "note": "; ".join(pools) + "; informational: independent pools; account-manager-managed top-ups"}


async def _millionverifier(c, key):
    # Free balance probe. Do not add bulk_credits to credits: they can name the same pool.
    try:
        d = await _get(c, "https://api.millionverifier.com/api/v3/credits", params={"api": key})
    except httpx.HTTPError as exc:
        # HTTP errors can include the request URL, which contains the private query key.
        raise ValueError(f"MillionVerifier balance request failed ({type(exc).__name__})") from None
    if not isinstance(d, dict) or d.get("error"):
        raise ValueError("MillionVerifier rejected the balance request")
    credits = d.get("credits")
    if isinstance(credits, bool) or not isinstance(credits, (int, float)) or credits < 0:
        raise ValueError("MillionVerifier returned no valid credit balance")
    return {"value": credits, "unit": "credits", "note": ""}


async def _bounceban(c, key):
    d = await _get(c, "https://api.bounceban.com/v1/account",
                   headers={"Authorization": key})
    credits = d.get("available_credits") if isinstance(d, dict) else None
    if (isinstance(credits, bool) or not isinstance(credits, (int, float))
            or not math.isfinite(credits) or credits < 0):
        raise ValueError("BounceBan returned no valid verification-credit balance")
    return {"value": credits, "unit": "verification credits", "note": ""}


async def _zerobounce(c, key):
    # Free balance route. ZeroBounce returns the balance as either a JSON number or a decimal
    # string. A bad key can still answer HTTP 200 with the documented -1 sentinel.
    try:
        d = await _get(c, "https://api.zerobounce.net/v2/getcredits",
                       params={"api_key": key})
    except httpx.HTTPError as exc:
        # HTTP errors may include the request URL and its private query key.
        raise ValueError(f"ZeroBounce balance request failed ({type(exc).__name__})") from None
    raw = d.get("Credits") if isinstance(d, dict) else None
    if type(raw) is int:
        credits = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        credits = int(raw.strip())
    else:
        raise ValueError("ZeroBounce returned no valid credit balance") from None
    if credits < 0:
        raise ValueError("ZeroBounce rejected the balance request")
    return {"value": credits, "unit": "credits",
            "note": "PAYG balance; treg treats replenishment as manual"}


async def _datagma(c, key):
    """Read only the spendable balance from Datagma's private account response."""
    try:
        d = await _get(c, "https://gateway.datagma.net/api/ingress/v1/mine",
                       params={"apiId": key})
    except httpx.HTTPError as exc:
        # HTTP errors may include the request URL and its private query credential.
        raise ValueError(f"Datagma balance request failed ({type(exc).__name__})") from None
    raw = d.get("currentCredit") if isinstance(d, dict) else None
    try:
        credits = float(raw)
    except (TypeError, ValueError):
        raise ValueError("Datagma returned no valid credit balance") from None
    if isinstance(raw, bool) or not math.isfinite(credits) or credits < 0:
        raise ValueError("Datagma returned no valid credit balance")
    value = int(credits) if credits.is_integer() else credits
    return {"value": value, "unit": "credits", "note": "Prepaid balance; replenished manually"}


async def _leadmagic(c, key):
    r = await c.post("https://api.leadmagic.io/v1/credits", headers={"X-API-Key": key})
    r.raise_for_status()
    d = r.json()
    return {"value": d.get("credits"), "unit": "credits",
            "note": "FROZEN" if d.get("is_frozen") else ""}


async def _lusha(c, key):
    d = await _get(c, "https://api.lusha.com/v3/account/usage", headers={"api_key": key})
    cr = d.get("credits", {})
    return {"value": cr.get("remaining"), "unit": "credits",
            "note": f"{cr.get('used')}/{cr.get('total')} used"}


async def _diffbot(c, key):
    # The account API lives on api.diffbot.com, not the kg.diffbot.com the catalog routes use.
    d = await _get(c, "https://api.diffbot.com/v4/account", params={"token": key})
    used = sum(day.get("credits", 0) for day in d.get("usage", []))
    plan = d.get("planCredits")
    return {"value": (plan - used) if isinstance(plan, (int, float)) else None,
            "unit": "credits left", "note": f"plan {plan}, {used} used since {d.get('planStart')}"}


async def _apify(c, key):
    d = await _get(c, "https://api.apify.com/v2/users/me/limits",
                   headers={"Authorization": f"Bearer {key}"})
    data = d.get("data", {})
    used = (data.get("current") or {}).get("monthlyUsageUsd", data.get("monthlyUsageUsd"))
    cap = (data.get("limits") or {}).get("maxMonthlyUsageUsd")
    val = (cap - used) if isinstance(cap, (int, float)) and isinstance(used, (int, float)) else None
    return {"value": val, "unit": "USD left this cycle",
            "note": f"${used:.2f} of ${cap} cap used" if isinstance(used, (int, float)) else ""}


async def _serpstat(c, key):
    # JSON-RPC over POST with the token as a query param; the no-"Api"-infix method spelling is the
    # one that works live — see the discrepancy note in src/treg/catalog/serpstat.yaml.
    r = await c.post(f"https://api.serpstat.com/v4/?token={key}",
                     json={"id": "1", "method": "SerpstatLimitsProcedure.getStats", "params": {}})
    r.raise_for_status()
    data = (r.json().get("result") or {}).get("data", {})
    return {"value": data.get("left_lines"), "unit": "API lines left",
            "note": f"{data.get('used_lines')}/{data.get('max_lines')} used"}


async def _thecompaniesapi(c, key):
    # Two hops: the user object names the team, the team object carries the credits.
    h = {"Authorization": f"Basic {key}"}
    user = await _get(c, "https://api.thecompaniesapi.com/v2/user", headers=h)
    team = await _get(c, f"https://api.thecompaniesapi.com/v2/teams/{user['currentTeamId']}",
                      headers=h)
    return {"value": team.get("credits"), "unit": "credits", "note": ""}


async def _apollo(c, key):
    # api_profile with include_credit_usage returns the caller's remaining balances directly.
    # `num_credits_remaining` is the LEGACY field and went stale at 0 on 2026-08-24 while the
    # account was still fine: a live 1-credit /people/match returned a verified email (200) with
    # the field still reading 0, and only `total_unified_credits_used` moved. Apollo has moved
    # this account to unified credits, so derive what is left from the granted lead pool minus
    # unified usage, and keep the legacy field as a fallback for accounts still on the old model.
    d = await _get(c, "https://api.apollo.io/api/v1/users/api_profile",
                   params={"include_credit_usage": "true"}, headers={"X-Api-Key": key})
    granted, used = d.get("effective_num_lead_credits"), d.get("total_unified_credits_used")
    left = (granted - used if isinstance(granted, (int, float)) and isinstance(used, (int, float))
            else d.get("num_credits_remaining"))
    return {"value": left, "unit": "credits",
            "note": f"lead pool {granted} granted / {used} unified credits used, "
                    f"direct-dial {d.get('effective_num_direct_dial_credits')}, "
                    f"ai {d.get('effective_num_ai_credits')}"}


async def _companyenrich(c, key):
    d = await _get(c, "https://api.companyenrich.com/me",
                   headers={"Authorization": f"Bearer {key}"})
    cr = d.get("credits", {})
    used, total = cr.get("used"), cr.get("total")
    left = (total - used) if isinstance(total, (int, float)) and isinstance(used, (int, float)) else None
    return {"value": left, "unit": "credits left", "note": f"{used}/{total} used"}


async def _oceanio(c, key):
    d = await _get(c, "https://api.ocean.io/v2/credits/balance", headers={"X-Api-Token": key})
    cr = d.get("credits", {})
    one, rec = cr.get("oneTime", 0) or 0, cr.get("recurrent", 0) or 0
    return {"value": one + rec, "unit": "credits",
            "note": f"{one:g} one-time + {rec:g} recurring, "
                    f"{d.get('dailyLimitRateLeft')} daily-rate calls left"}


async def _tomba(c, key):
    # Tomba's meaningful routes need the key AND the secret header; the secret rides its own
    # platform slot (TOMBA.platform_extra_setting) rather than being packed into one value.
    secret = get_settings().platform_key_tomba_secret or ""
    d = await _get(c, "https://api.tomba.io/v1/me",
                   headers={"X-Tomba-Key": key, "X-Tomba-Secret": secret})
    d = d.get("data", d)
    req = d.get("requests", {})
    dom, ver = req.get("domains", {}), req.get("verifications", {})
    return {"value": (dom.get("available", 0) - dom.get("used", 0)), "unit": "searches left",
            "note": f"verifications {ver.get('available', 0) - ver.get('used', 0)} left, "
                    f"plan {((d.get('pricing') or {}).get('name', '?'))}"}


async def _predictleads(c, key):
    # The platform slot holds base64("api_key:api_token") for HTTP Basic — pass it straight through.
    d = await _get(c, "https://predictleads.com/api/v3/api_subscription",
                   headers={"Authorization": f"Basic {key}"})
    attrs = ((d.get("data") or [{}])[0]).get("attributes", {})
    quota, used = attrs.get("monthly_credits_quota"), attrs.get("monthly_credits_used")
    left = (quota - used) if isinstance(quota, (int, float)) and isinstance(used, (int, float)) else None
    return {"value": left, "unit": "credits left this month",
            "note": f"{used}/{quota} used, subscription {attrs.get('status', '?')}"}


async def _findymail(c, key):
    d = await _get(c, "https://app.findymail.com/api/credits",
                   headers={"Authorization": f"Bearer {key}", "Accept": "application/json"})
    return {"value": d.get("credits"), "unit": "finder credits",
            "note": f"verifier {d.get('verifier_credits')} left (separate pool)"}


async def _branddev(c, key):
    # No free account route exists — but a deliberate no-param call is a FREE validation error
    # (400, credits_consumed 0) whose body still carries key_metadata.credits_remaining.
    r = await c.get("https://api.brand.dev/v1/brand/retrieve",
                    headers={"Authorization": f"Bearer {key}"})
    meta = (r.json() or {}).get("key_metadata", {}) if r.status_code < 500 else {}
    return {"value": meta.get("credits_remaining"), "unit": "credits",
            "note": "read from the free validation-error response (no account endpoint exists)"}


async def _leadsforge(c, key):
    d = await _get(c, "https://api.leadsforge.ai/public/v1/balance",
                   headers={"Authorization": f"Bearer {key}"})
    return {"value": d.get("availableCredits"), "unit": "credits",
            "note": f"{d.get('reservedCredits', 0)} reserved; "
                    f"email {d.get('emailEnrichmentPrice')} / phone {d.get('phoneNumberEnrichmentPrice')} credits"}


async def _fiber_ai(c, key):
    # GET /v1/get-org-credits is Fiber's free registry probe (documented in catalog/fiber-ai.yaml);
    # it is not a catalog endpoint. `usagePeriodResetsOn` sits a century out on the trial pool, so
    # treat `available` as a prepaid balance, not a monthly quota.
    d = await _get(c, "https://api.fiber.ai/v1/get-org-credits", headers={"x-api-key": key})
    org = (d.get("output") or [{}])[0]
    resets = (org.get("usagePeriodResetsOn") or "")[:10]
    return {"value": org.get("available"), "unit": "credits",
            "note": f"{org.get('used')} of {org.get('max')} used; period resets {resets}"}


async def _coingecko(c, key):
    # /key is the Pro API's free usage route (Basic plan and up); the quota is calls per billing month.
    d = await _get(c, "https://pro-api.coingecko.com/api/v3/key", headers={"x-cg-pro-api-key": key})
    return {"value": d.get("current_remaining_monthly_calls"), "unit": "calls left this month",
            "note": f"plan {d.get('plan')}, {d.get('current_total_monthly_calls')}/"
                    f"{d.get('monthly_call_credit')} used, {d.get('rate_limit_request_per_minute')} rpm"}


async def _twelvedata(c, key):
    # Twelve Data meters per MINUTE, not per month: plan_limit is the credits/min cap. The call
    # itself costs 1 credit — trivial, but not zero like the others.
    d = await _get(c, "https://api.twelvedata.com/api_usage", params={"apikey": key})
    used, cap = d.get("current_usage"), d.get("plan_limit")
    left = (cap - used) if isinstance(cap, (int, float)) and isinstance(used, (int, float)) else None
    daily = d.get("daily_usage")
    return {"value": left, "unit": "credits left this minute",
            "note": f"{used}/{cap} per-minute credits used" + (f", {daily} used today" if daily is not None else "")}


async def _influencersclub(c, key):
    # Plural `accounts` + trailing slash is the path that answers; the guides page's
    # /account/credits spelling 404s (probed 2026-08-22). Credits are a carry-over balance.
    d = await _get(c, "https://api-dashboard.influencers.club/public/v1/accounts/credits/",
                   headers={"Authorization": f"Bearer {key}"})
    return {"value": d.get("credits_available"), "unit": "credits",
            "note": f"{d.get('credits_used')} used to date"}


async def _spyfu(c, key):
    # The Account API was missed on the 2026-08-12 sweep; it reports the month's included units
    # (rows) against usage, plus the overage cost in USD. Month is UTC.
    from datetime import datetime, timezone
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    d = await _get(c, f"https://api.spyfu.com/apis/accounts_api/v2/usage/month/{month}",
                   params={"api_key": key})
    base, used = d.get("baseUnits"), d.get("unitsUsed")
    left = (base - used) if isinstance(base, (int, float)) and isinstance(used, (int, float)) else None
    return {"value": left, "unit": "units left this month",
            "note": f"{used}/{base} used, {d.get('requestCount')} requests, "
                    f"overage ${d.get('finalCost', 0)} ({d.get('serviceLevelName')})"}


async def _icypeas(c, key):
    # Raw key in Authorization (no Bearer); an empty body returns the key owner's own record.
    r = await c.post("https://app.icypeas.com/api/a/actions/subscription-information",
                     headers={"Authorization": key}, json={})
    r.raise_for_status()
    d = r.json()
    return {"value": d.get("credits"), "unit": "credits",
            "note": f"status {d.get('status')}, plan {d.get('plan') or '-'}, "
                    f"daily quota {(d.get('quotas') or {}).get('daily')}"}


async def _pdl(c, key):
    # No account route (/v5/account 404s) — but a param-less enrich call is a FREE 400
    # (x-call-credits-spent: 0) that still carries the x-totallimit-* balance headers.
    r = await c.get("https://api.peopledatalabs.com/v5/person/enrich", headers={"X-Api-Key": key})
    h = r.headers
    spent = h.get("x-call-credits-spent")
    if spent not in (None, "0"):
        return {"value": None, "unit": "", "note": f"probe unexpectedly charged {spent} credits"}
    val = h.get("x-totallimit-remaining")
    return {"value": float(val) if val else None, "unit": "credits (enrich meter)",
            "note": f"purchased {h.get('x-totallimit-purchased-remaining')} + overage "
                    f"{h.get('x-totallimit-overages-remaining')}, lifetime used {h.get('x-lifetime-used')}; "
                    "read from the free 400's headers"}


async def _brightdata(c, key):
    # GET /customer/balance returns account balance and pending charges (USD).
    # Requires billing permission on the API token — if the token lacks it, the request
    # returns 403 and the exception path surfaces the permission issue as a note.
    # Response: {balance, credit, prepayment, pending_costs}
    d = await _get(c, "https://api.brightdata.com/customer/balance",
                   headers={"Authorization": f"Bearer {key}"})
    return {"value": d.get("balance"), "unit": "USD",
            "note": f"pending ${d.get('pending_costs', 0):.2f} this cycle"}


async def _crustdata(c, key):
    # GET /account/credits is free (does not consume credits). Requires x-api-version header.
    d = await _get(c, "https://api.crustdata.com/account/credits",
                   headers={"Authorization": f"Bearer {key}", "x-api-version": "2025-11-01"})
    acct = d.get("account", {})
    rec = acct.get("recurring_credits")
    freq = acct.get("recurring_credits_frequency")
    refresh = (acct.get("recurring_credits_refresh_date") or "")[:10]
    note = f"recurring {rec} {freq}, refreshes {refresh}" if rec else "no recurring grant"
    return {"value": acct.get("credits"), "unit": "credits", "note": note}


async def _akta(c, key):
    # GET /mcp/account is free — returns plan tier and remaining credit balance.
    # Response: {credit_balance, balance_amount, currency, package_type, is_enterprise, ...}
    d = await _get(c, "https://api.akta.pro/api/v1/mcp/account",
                   headers={"x-api-key": key})
    tier = d.get("package_type", "unknown")
    enterprise = " (enterprise)" if d.get("is_enterprise") else ""
    # credit_balance is the prepaid credits; balance_amount is USD if any
    return {"value": d.get("credit_balance"), "unit": "credits",
            "note": f"tier {tier}{enterprise}, lifetime {d.get('lifetime_consumed_credits', 0)} used"}


BALANCE_ROUTES = {
    "akta": _akta,
    "brightdata": _brightdata,
    "crustdata": _crustdata,
    "dropleads": _dropleads,
    "fiber_ai": _fiber_ai,
    "spyfu": _spyfu,
    "icypeas": _icypeas,
    "pdl": _pdl,
    "coingecko": _coingecko,
    "twelvedata": _twelvedata,
    "influencersclub": _influencersclub,
    "apollo": _apollo,
    "branddev": _branddev,
    "companyenrich": _companyenrich,
    "findymail": _findymail,
    "leadsforge": _leadsforge,
    "oceanio": _oceanio,
    "predictleads": _predictleads,
    "tomba": _tomba,
    "dataforseo": _dataforseo,
    "tikhub": _tikhub,
    "tinyfish": _tinyfish,
    "fishaudio": _fishaudio,
    "tavily": _tavily,
    "serper": _serper,
    "olostep": _olostep,
    "scrapegraphai": _scrapegraphai,
    "scrapecreators": _scrapecreators,
    "serpapi": _serpapi,
    "moz": _moz,
    "seranking": _seranking,
    "hunter": _hunter,
    "harvestapi": _harvestapi,
    "fetchinio": _fetchinio,
    "quickenrich": _quickenrich,
    "prospeo": _prospeo,
    "aiark": _aiark,
    "wiza": _wiza,
    "getleadsio": _getleadsio,
    "sumble": _sumble,
    "moltsets": _moltsets,
    "openmart": _openmart,
    "trykitt": _trykitt,
    "contactout": _contactout,
    "millionverifier": _millionverifier,
    "bounceban": _bounceban,
    "zerobounce": _zerobounce,
    "datagma": _datagma,
    "leadmagic": _leadmagic,
    "lusha": _lusha,
    "diffbot": _diffbot,
    "apify": _apify,
    "serpstat": _serpstat,
    "thecompaniesapi": _thecompaniesapi,
}

# Verified to publish NO free standalone balance/credits API. Some are dashboard-only; Scrubby
# exposes remaining credits only on verification responses, which the collector must not spend to
# obtain. Kept explicit so the report names them instead of silently skipping, and so a future probe
# has a list of what to re-check.
NO_BALANCE_API = {
    "adyntel": "no public balance or usage endpoint in the official API reference "
                "(checked docs.adyntel.com 2026-09-22) — PAYG credits are visible in the "
                "provider dashboard only",
    "aviato": "no public balance endpoint documented (checked docs.data.aviato.co 2026-08-31) — "
              "internal playbooks reference aviato_get_balance but it is not in the public API; "
              "dashboard only",
    "coresignal": "no dedicated balance endpoint (checked docs.coresignal.com 2026-08-31) — "
                  "x-credits-remaining header rides only on BILLED 200s (a free 422 has none); "
                  "dashboard only",
    "exa": "no balance endpoint (checked exa.ai/docs 2026-08-31) — GET /team-management/api-keys/{id}/usage "
           "returns historical costs, not remaining balance; dashboard only",
    "finnhub": "no account/usage endpoint and no rate-limit headers (checked 2026-08-31) — "
               "per-minute limits only, nothing to read back",
    "financialdatasets": "no free balance or usage endpoint in the official API "
                         "(checked www.financialdatasets.ai/openapi.json 2026-09-15) — "
                         "prepaid Credits are visible in the vendor dashboard only",
    "justoneapi": "balance available only via MCP server (get_account_balance tool), no public REST "
                  "endpoint documented (checked docs.justoneapi.com 2026-08-31) — dashboard only",
    "keenable": "no public REST balance or usage endpoint in the official OpenAPI document "
                "(checked docs.keenable.ai 2026-09-23) — the console shows remaining credits and "
                "authenticated MCP calls report only per-call usage",
    "limadata": "no free standalone balance or usage endpoint in the official Basic v2 API "
                "(checked api.limadata.com/docs/basic_v2 2026-09-17) — dashboard only",
    "trestleiq": "no public balance or usage endpoint in the official API reference "
                  "(checked docs.trestleiq.com 2026-09-21) — Developer Portal only",
    "marketstack": "no usage endpoint (checked 2026-08-31) — monthly quota in the dashboard, "
                   "email alerts at 75/90/100%",
    "scrubby": "no free standalone balance or usage endpoint in the official API "
               "(checked docs.scrubby.io 2026-09-16) — remaining_credits appears only on "
               "verification responses; do not spend a verification merely to collect capacity",
    "tiingo": "no usage API (api/account/usage 404s, checked 2026-08-31) — tiingo.com/account/usage is "
              "a logged-in HTML page only",
}

# platform_key_* slots that are the SECOND half of a provider's credential pair, not a provider of
# their own (see OAuthProvider.platform_extra_setting) — they must not become report rows.
AUX_SLOTS = {"tomba_secret"}


def all_platform_providers() -> list[str]:
    """Every provider with a platform-key slot in Settings — the population a sweep is about."""
    return sorted(name.removeprefix("platform_key_")
                  for name in type(get_settings()).model_fields
                  if name.startswith("platform_key_")
                  and name.removeprefix("platform_key_") not in AUX_SLOTS)


async def provider_balance(provider: str, client: httpx.AsyncClient | None = None) -> dict:
    """Ask one provider what it thinks our account has left. Never raises — a failure is a row.

    Reads the SETTING, not `platform_key_for`: the tier-4 allow-list is a serving kill switch, and a
    provider we just switched off is exactly one whose final balance we still want to see."""
    setting = platform_setting_name(provider)
    key = getattr(get_settings(), setting, "") or ""
    if not key:
        return {"provider": provider, "value": None, "unit": "", "no_key": True,
                "note": f"no TREG_{setting.upper()} in the env"}
    if provider in NO_BALANCE_API:
        # `no_api` is the machine-readable half of the note: a provider that publishes no meter
        # must not read as a broken key.
        return {"provider": provider, "value": None, "unit": "", "no_api": True,
                "note": NO_BALANCE_API[provider]}
    fetch = BALANCE_ROUTES.get(provider)
    if fetch is None:
        return {"provider": provider, "value": None, "unit": "", "no_api": True,
                "note": "no fetcher written yet"}
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=30) as c:
                row = await fetch(c, key)
        else:
            row = await fetch(client, key)
    except Exception as exc:  # noqa: BLE001 — a sweep must report, not crash
        return {"provider": provider, "value": None, "unit": "",
                "note": f"{type(exc).__name__}: {exc}"}
    return {"provider": provider, **row}
