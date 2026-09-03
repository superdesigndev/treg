"""The free balance / quota collectors for every provider treg holds a platform key for.

Moved unchanged from `scripts/provider_balances.py` (the script is now a thin CLI over this
module). Each collector is `coroutine(client, key) -> {"value", "unit", "note"}`: the provider's
own free account/limits call, never a metered one, so a sweep can run as often as it likes.

Pure collection: nothing here touches the database or the request path. The worker sweep
(`treg.domain.capacity.sweep`) turns these dicts into `CapacitySnapshot` rows.
"""

from __future__ import annotations

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


async def _litescrape(c, key):
    # GET /api/keys/status is the free balance probe and machine-readable rate card.
    d = await _get(c, "https://api.litescrape.com/api/keys/status",
                   headers={"Authorization": f"Bearer {key}"})
    cents = d.get("cents_per_1000_calls")
    rate = f"{cents:g}" if isinstance(cents, (int, float)) else "unknown"
    return {"value": d.get("remaining_calls"), "unit": "calls left",
            "note": f"{rate} cents per 1,000 successful calls"}


BALANCE_ROUTES = {
    "akta": _akta,
    "brightdata": _brightdata,
    "crustdata": _crustdata,
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
    "litescrape": _litescrape,
    "oceanio": _oceanio,
    "predictleads": _predictleads,
    "tomba": _tomba,
    "dataforseo": _dataforseo,
    "tikhub": _tikhub,
    "scrapecreators": _scrapecreators,
    "serpapi": _serpapi,
    "moz": _moz,
    "seranking": _seranking,
    "hunter": _hunter,
    "leadmagic": _leadmagic,
    "lusha": _lusha,
    "diffbot": _diffbot,
    "apify": _apify,
    "serpstat": _serpstat,
    "thecompaniesapi": _thecompaniesapi,
}

# Verified to publish NO balance/credits API (re-checked 2026-08-31) — the dashboard is the only meter. Kept
# explicit so the report names them instead of silently skipping, and so a future probe has a list
# of what to re-check.
NO_BALANCE_API = {
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
    "justoneapi": "balance available only via MCP server (get_account_balance tool), no public REST "
                  "endpoint documented (checked docs.justoneapi.com 2026-08-31) — dashboard only",
    "marketstack": "no usage endpoint (checked 2026-08-31) — monthly quota in the dashboard, "
                   "email alerts at 75/90/100%",
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
