"""`treg-worker` — the scheduled, server-side maintainer commands (the `worker` profile).

    treg-worker capacity sweep [--only provider,...] [--json]
    treg-worker overflow sync [--live]          # seed (+ live aggregator catalogs) → overflow_route
    treg-worker overflow verify [--all] [--max-usd 0.02]   # weekly re-verify of enabled routes
    treg-worker asynctasks settle [--limit 50]       # complete deferred metered-call holds
    treg-worker arena insights [--max-seconds 110]   # fold new audit rows into the Arena aggregate
    treg-worker catalog stats [--max-rows 500000]    # fold new audit rows into per-day endpoint stats
    treg-worker catalog requeue-async-stats [--apply]   # preview or queue one async-window repair
    treg-worker catalog repair-status --manifest <path>  # check the saved applied target list
    treg-worker spend rollup [--max-rows 50000]      # fold new ledger settles into per-key daily spend
    treg-worker usage rollup [--max-rows 500000]     # fold new audit rows into per-member daily call counts
    treg-worker showcase activity                    # add new calls to the /showcase activity rows (hourly)
    treg-worker rankings refresh                     # rebuild the public /rankings boards from the day stats (hourly)
    treg-worker media sweep                          # delete public pictures nothing uses any more (daily)
    treg-worker jev xboost [--posts 60] [--min-likes 150]   # the /jev launch-radar demo: X posts <24h -> jev
    treg-worker admin purge-evidence [--batch-size 5000]  # blank expired error evidence past 14-day retention
    treg-worker admin purge-ad-clicks                      # delete email-to-click stash rows past 90 days
    treg-worker admin erase-archive --org <id>             # erase what an opted-out team stored in the archive
    treg-worker enterprise reconcile [--org <id>]          # repair missed Directory Sync removals
    treg-worker enterprise cleanup [--org <id>]            # remove WorkOS setup after paid SSO ends
    treg-worker enterprise purge-audit [--days 400]        # delete expired SSO audit evidence

Not the light `treg` CLI: these need the server extra (DB, platform keys in the env) and make
outbound calls to third parties, so they run as Render cron jobs with the server's env — never as
dataplane lifespan work (refactor plan §2.2). The worker never originates a money movement. It may
complete a hold opened by the request path, settling or releasing it with full call and org attribution.

The audit-table analytics commands exist so that no web process walks `callrecord` next to the money path:
each is a bounded incremental pass over the audit table that a cron repeats, and the request path
reads only what they published.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _need_server() -> None:
    try:
        import sqlalchemy  # noqa: F401
    except ImportError as exc:  # a base install has no DB stack
        print("treg-worker needs the server extra: pip install 'treg[server]'", file=sys.stderr)
        raise SystemExit(2) from exc


async def _capacity_sweep(args) -> int:
    from .infra.db import session_maker, verify_db
    from .domain.capacity.sweep import run_sweep

    await verify_db()
    only = {p.strip() for p in (args.only or "").split(",") if p.strip()} or None
    async with session_maker() as db:
        result = await run_sweep(db, only=only)
    if args.json:
        print(json.dumps({p: s.to_json() for p, s in result.states.items()}, indent=2))
    else:
        print(f"{'provider':<22}{'remaining':>14}  unit / state")
        for p, s in result.states.items():
            rem = "—" if s.remaining is None else f"{s.remaining:,.2f}"
            print(f"{p:<22}{rem:>14}  {s.unit} · {s.health}" + (f" · {s.note}" if s.note else ""))
        if result.unknown_policies:
            print(f"\nunclassified policies (capacity_type/funding_mode = unknown): "
                  f"{', '.join(result.unknown_policies)}", file=sys.stderr)
    return 0


def _our_endpoints() -> list[dict]:
    from . import oauth_providers
    from .domain.catalog import store as catalog_store
    cat = catalog_store.load()
    out = []
    for ep in cat.endpoints:
        prov = oauth_providers.get(ep["provider"])
        if prov is None or not prov.base_url:
            continue
        out.append({"endpoint_id": ep["id"], "provider": ep["provider"],
                    "method": (ep.get("method") or "GET").upper(), "path": ep["path"],
                    "base_url": prov.base_url})
    return out


async def _overflow_sync(args) -> int:
    from .config import get_settings
    from .infra.db import session_maker, verify_db
    from .domain.capacity import routes as R
    from .domain.catalog import store as catalog_store

    await verify_db()
    candidates = R.load_seed()
    if args.live:
        import httpx
        from .infra.upstream.aggregators import catalogs
        s = get_settings()
        async with httpx.AsyncClient(timeout=60) as c:
            orth = await catalogs.orthogonal_apis(c, s.overflow_key_orthogonal) if s.overflow_key_orthogonal else []
        seeded = {(x["endpoint_id"], x["aggregator"]): x for x in candidates}
        for row in R.match_catalogs(_our_endpoints(), orthogonal_apis=orth):
            key = (row["endpoint_id"], row["aggregator"])
            if key in seeded:
                seeded[key].update({k: row[k] for k in ("agg_slug", "agg_path", "agg_price_usd") if row.get(k) is not None})
            else:
                seeded[key] = {**row, "matched_at": None, "verified_at": None}
        candidates = list(seeded.values())
    async with session_maker() as db:
        result = await R.apply_sync(db, candidates, catalog=catalog_store.load())
        await db.commit()
    print(f"overflow routes: {result.rows} rows, {result.enabled} enabled")
    for reason, n in sorted(result.disabled.items(), key=lambda kv: -kv[1]):
        print(f"  disabled · {reason}: {n}")
    return 0


RENEW_MAX_USD = 1.00
"""Per-route price cap for RENEWING a route that is enabled or was stamped before. The 2¢ default
`--max-usd` is a discovery cap for never-verified pairs under `--all`; held to it, the weekly cron
skipped every stamped route priced above 2¢ (46 of them on 2026-09-07, all mapped and verified on
2026-08-26) and they decayed off with no run ever able to bring them back."""
VERIFY_BUDGET_USD = 15.00
"""What one verify run may spend in total (relay fee + direct comparison), whatever the caps say."""


def _verify_plan(rows, *, all_rows: bool, only: set[str] | None, max_usd: float,
                 renew_max_usd: float) -> list[tuple[object, float]]:
    """Which routes this run visits, in order, each with the price cap it is held to. Renewals
    (enabled or previously stamped) come first, oldest stamp first, so the route nearest its 7-day
    decay is reached before the budget is; never-verified pairs follow only under `--all`."""
    renew, discover = [], []
    for r in rows:
        if only and r.provider not in only:
            continue
        if r.enabled or r.last_verified_at:
            renew.append(r)
        elif all_rows:
            discover.append(r)
    renew.sort(key=lambda r: (r.last_verified_at is not None, r.last_verified_at or 0, r.endpoint_id))
    discover.sort(key=lambda r: r.endpoint_id)
    return [(r, renew_max_usd) for r in renew] + [(r, max_usd) for r in discover]


async def _overflow_verify(args) -> int:
    import httpx
    from sqlalchemy import select
    from .config import get_settings, platform_setting_name
    from . import oauth_providers
    from .infra.db import session_maker, verify_db
    from .domain.capacity import verify as V
    from .domain.catalog import store as catalog_store
    from .models import OverflowRoute
    from .timeutil import utcnow_naive

    await verify_db()
    s = get_settings()
    cat = catalog_store.load()
    by_id = {e["id"]: e for e in cat.endpoints}
    async with session_maker() as db:
        rows = (await db.execute(select(OverflowRoute))).scalars().all()
    only = {p.strip() for p in (getattr(args, "only", None) or "").split(",") if p.strip()}
    renew_max_usd = getattr(args, "renew_max_usd", RENEW_MAX_USD)
    budget_usd = getattr(args, "budget_usd", VERIFY_BUDGET_USD)
    todo = _verify_plan(rows, all_rows=args.all, only=only or None,
                        max_usd=args.max_usd, renew_max_usd=renew_max_usd)
    keys = {"orthogonal": s.overflow_key_orthogonal, "monid": s.overflow_key_monid}
    tally = {"passed": 0, "failed": 0, "aggregator": 0, "inconclusive": 0}
    skipped, over_budget, spent_usd, key_failures = 0, 0, 0.0, []
    # One SHORT transaction per route. A single session held open across every network round-trip
    # autoflushes each row's UPDATE on the next `db.get`, and the row locks pile up until a lock
    # timeout kills the run.
    async with httpx.AsyncClient(timeout=60) as c:
        for r, cap in todo:
            ep = by_id.get(r.endpoint_id)
            key = keys.get(r.aggregator)
            tr = (ep or {}).get("test_request")
            usd = (r.agg_price_micro or 0) / 1e6
            if not ep or not key or not tr or usd > cap:
                skipped += 1
                continue
            direct = None
            prov = oauth_providers.get(ep["provider"])
            pkey = getattr(s, platform_setting_name(ep["provider"]), "")
            # The run budget bounds what one run may spend: the relay's fee plus, when we hold the
            # vendor key, the direct comparison at the same list price. A route that does not fit
            # is skipped, not the rest of the run - a cheaper route further down may still fit.
            est_usd = usd * (2 if prov is not None and pkey else 1)
            if spent_usd + est_usd > budget_usd:
                over_budget += 1
                continue
            spent_usd += est_usd
            hdrs = {}
            if prov is not None and pkey:
                url = prov.base_url.rstrip("/") + "/" + ep["path"].lstrip("/")
                for k, v in (tr.get("pathParams") or {}).items():
                    url = url.replace("{" + k + "}", str(v))
                q = {k: str(v) for k, v in (tr.get("queryParams") or {}).items()}
                if prov.token_location == "query":
                    q[prov.token_param] = (prov.token_format or "{secret}").format(secret=pkey)
                else:
                    hdrs[prov.token_header] = (prov.token_format or "{secret}").format(secret=pkey)
                for name, value in prov.required_headers:
                    hdrs[name] = value
                if prov.needs_extra_credential and prov.platform_extra_setting:
                    extra = getattr(s, prov.platform_extra_setting, "")
                    if extra:
                        hdrs[prov.extra_credential_header] = extra
                body = tr.get("body")
                direct = (url, q, __import__("json").dumps(body).encode() if body is not None else None)
                if body is not None:
                    hdrs["Content-Type"] = "application/json"
            v = await V.verify_route(c, r, key=key, direct=direct, test_request=tr, direct_headers=hdrs)
            if v.failure in ("aggregator_auth", "aggregator_balance"):
                key_failures.append(v.note)  # OUR key or OUR prepaid balance: someone here must act
            async with session_maker() as db:
                row = await db.get(OverflowRoute, (r.endpoint_id, r.aggregator))
                if row is None:
                    skipped += 1
                    continue
                verdict = V.verdict(v)
                tally[verdict] += 1
                if verdict == "passed" or (verdict == "inconclusive" and v.direct_dry and v.relay_ok):
                    # Our own key dry and the relay served: the shape cannot be checked for OUR
                    # reason; without the stamp `overflow sync` would decay the route after 7 days -
                    # exactly while our account is dry, when it is needed. Any other inconclusive
                    # (no key, 401, a stale test_request) is not evidence and does not stamp.
                    row.last_verified_at = v.verified_at or utcnow_naive()
                elif verdict == "failed" and row.enabled:
                    row.enabled, row.disabled_reason = False, f"re-verify failed: {v.note}"[:200]
                row.updated_at = utcnow_naive()
                await db.commit()
            print(f"{verdict:<12} {r.endpoint_id} via {r.aggregator} "
                  f"direct={v.direct_status} relay={v.relay_status} cost={v.cost_micro} {v.note}")
    attempted = sum(tally.values())
    print(f"verified {tally['passed']}, failed {tally['failed']}, inconclusive {tally['inconclusive']}, "
          f"aggregator errors {tally['aggregator']}, skipped {skipped}, over budget {over_budget} "
          f"(~${spent_usd:.2f} of ${budget_usd:g})")
    # A failed ROUTE is a result (its row is disabled with the reason). A failed RUN is one that
    # could not verify: our key or our balance refused on any route, every attempt lost to the
    # aggregator's side (a host down for the whole run, not one timeout), or nothing attempted at
    # all (ops/capacity.md). A vendor pool dry on the aggregator's side (VENDOR_DRY) is theirs to
    # refill: counted under aggregator errors, never a failed run on its own.
    if attempted == 0 or key_failures or tally["aggregator"] == attempted:
        print("overflow verify: run failed - check aggregator keys, balances and the route table", file=sys.stderr)
        return 1
    return 0


async def _asynctasks_settle(args) -> int:
    from .infra.db import verify_db
    from .application.asynctasks import settle_due
    from .application.call import keyslots

    keyslots.check_accounts()  # polls use treg's accounts: an unusable one refuses to start
    from . import call_events

    await verify_db()
    try:
        result = await settle_due(limit=args.limit)
    finally:
        await call_events.drain()  # the terminal events this tick observed
    print(json.dumps(result.__dict__, sort_keys=True))
    return 0


async def _hub_check(args) -> int:
    """The scheduled health check (docs/HUB-DECISIONS.md round 2 q8; architecture/hub.md): every
    live tool's newest version runs its check.json once as its maker. The verdict lands on the
    row; health is derived from the last three runs. Cron it every 6 hours."""
    import httpx
    from sqlalchemy import select
    from .application.hub import enabled as hub_enabled
    from .application.hub.health import check_as_maker
    from .infra.db import session_maker, verify_db
    from .models import HubTool
    from .application.call import keyslots
    keyslots.check_accounts()  # checks make platform calls: an unusable account refuses to start
    await verify_db()
    if not hub_enabled():
        print("hub is off (TREG_HUB_ENABLED); nothing to check")
        return 0
    out = []
    async with httpx.AsyncClient(timeout=200.0) as upstream:
        async with session_maker() as db:
            q = select(HubTool).where(HubTool.status == "live").order_by(HubTool.tool_id, HubTool.version.desc())
            if args.only:
                q = q.where(HubTool.tool_id == args.only)
            rows = (await db.execute(q)).scalars().all()
            seen: set[str] = set()
            for row in rows:
                if row.tool_id in seen:
                    continue
                seen.add(row.tool_id)
                v = await check_as_maker(db, row, upstream)
                await db.commit()
                out.append({"tool_id": row.tool_id, "version": row.version, "check": v.get("status"),
                            "run_id": v.get("run_id"), "charged_micro": v.get("charged_micro", 0),
                            "error": (v.get("error") or {}).get("error") or (v.get("error") or {}).get("message")})
    if args.json:
        print(json.dumps(out, indent=2))
    else:
        for o in out:
            print(f"{o['tool_id']}@{o['version']}  {o['check']}  run {o['run_id']}  {o['charged_micro']} µ$"
                  + (f"  {o['error']}" if o["error"] else ""))
        print(f"{len(out)} tool(s) checked")
    return 0 if all(o["check"] == "passed" for o in out) else 1
async def _arena_insights(args) -> int:
    from .infra.db import verify_db
    from .application.arena_insights import drain
    from .bootstrap import archive_object_store
    from . import analytics
    from .application import web_arena, web_arena_calls, web_arena_publications

    async with archive_object_store():
        await verify_db()
        analytics.capture_service_started("arena-worker")
        try:
            result = await drain(max_seconds=args.max_seconds)
        finally:
            await analytics.drain()
    if web_arena.enabled() or await web_arena_calls.seeded():
        result["web_arena_calls"] = await web_arena_calls.collect()
        result["web_arena"] = await web_arena_publications.refresh_live_if_due()
    print(json.dumps(result, sort_keys=True))
    return 1 if result["failed"] else 0


async def _shadow_collect(args) -> int:
    from .infra.db import verify_db
    from .application import shadow
    from .bootstrap import archive_object_store

    async with archive_object_store():
        await verify_db()
        result = await shadow.collect(max_seconds=args.max_seconds)
    print(shadow.dumps(result))
    return 0


async def _shadow_report(args) -> int:
    from .infra.db import verify_db
    from .application import shadow

    await verify_db()
    print(shadow.dumps(await shadow.report(days=args.days)))
    return 0


async def _web_arena_totals(args) -> int:
    from .infra.db import verify_db
    from .application.web_arena_publications import refresh_live
    await verify_db()
    print(json.dumps(await refresh_live(), sort_keys=True))
    return 0


async def _web_arena_seed(args) -> int:
    from .infra.db import verify_db
    from .application.web_arena_publications import seed_recent
    await verify_db()
    print(json.dumps(await seed_recent(), sort_keys=True))
    return 0


async def _catalog_stats(args) -> int:
    from .infra.db import verify_db
    from .application.catalog_stats import refresh

    await verify_db()
    result = await refresh(max_rows=args.max_rows, max_dirty_buckets=args.max_dirty_buckets,
                           max_repair_seconds=args.max_repair_seconds,
                           bucket_timeout_s=args.bucket_timeout_seconds)
    print(json.dumps(result, sort_keys=True))
    return 0


async def _catalog_requeue_async_stats(args) -> int:
    from .infra.db import verify_db
    from .application.catalog_stats import requeue_async_stats

    await verify_db()
    try:
        result = await requeue_async_stats(apply=args.apply)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


async def _catalog_repair_status(args) -> int:
    from pathlib import Path
    from .infra.db import verify_db
    from .application.catalog_stats import repair_status

    try:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        await verify_db()
        result = await repair_status(manifest)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["complete"] else 1


async def _showcase_activity(args) -> int:
    from .infra.db import verify_db
    from .application.showcase import refresh_activity

    await verify_db()
    print(json.dumps(await refresh_activity(), sort_keys=True))
    return 0


async def _rankings_refresh(args) -> int:
    from .infra.db import verify_db
    from .application.rankings import refresh
    from .bootstrap import configure_provider_labels

    configure_provider_labels()   # the boards print display names ("Crawl4AI"), as the web app does
    await verify_db()
    print(json.dumps(await refresh(), sort_keys=True))
    return 0


async def _media_sweep(args) -> int:
    from .infra.db import verify_db
    from .application.images import sweep

    await verify_db()
    print(json.dumps(await sweep(), sort_keys=True))
    return 0


async def _spend_rollup(args) -> int:
    from .infra.db import verify_db
    from .application.key_spend_rollup import refresh

    await verify_db()
    result = await refresh(max_rows=args.max_rows, max_recount_seconds=args.max_recount_seconds)
    print(json.dumps(result, sort_keys=True))
    return 0


async def _usage_rollup(args) -> int:
    from .infra.db import verify_db
    from .application.call_usage_rollup import refresh

    await verify_db()
    print(json.dumps(await refresh(max_rows=args.max_rows), sort_keys=True))
    return 0


async def _jev_xboost(args) -> int:
    import httpx

    from .infra.db import session_maker, verify_db
    from .application import jev_xboost
    from . import ratestore

    await verify_db()
    async with httpx.AsyncClient() as http:
        run = await jev_xboost.run_daily(http, topics=args.topic or None, max_posts=args.posts, min_likes=args.min_likes)
    async with session_maker() as db:
        previous = await ratestore.kv_get(db, jev_xboost.KV_NS, jev_xboost.KV_KEY) or {}
        run["manual"] = (previous.get("manual") or [])[:jev_xboost.MANUAL_CAP]   # visitors' verdicts survive the daily run
        await ratestore.kv_put(db, jev_xboost.KV_NS, jev_xboost.KV_KEY, run, ttl_s=jev_xboost.KV_TTL_S)
        await db.commit()
    print(json.dumps({k: run[k] for k in ("ran_at", "posts_found", "seconds", "costs", "calls")}, sort_keys=True),
          f"judged={len(run['posts'])}")
    return 0


async def _admin_purge_evidence(args) -> int:
    """Blank failed-call evidence past the 14-day retention window (application/evidence_retention)."""
    from .application import evidence_retention
    from .infra.db import verify_db

    await verify_db()
    result = await evidence_retention.purge(batch_size=args.batch_size)
    print(json.dumps(result, sort_keys=True))
    return 1 if result.get("error") else 0


async def _admin_purge_ad_clicks(args) -> int:
    """Delete email-to-click stash rows past the 90-day lookup window (application/signup)."""
    from .application import signup
    from .infra.db import verify_db

    await verify_db()
    print(json.dumps(await signup.purge_expired_ad_clicks(), sort_keys=True))
    return 0


async def _admin_erase_archive(args) -> int:
    """Erase what one opted-out team stored in the archive (application/archive_erasure): the
    answer to an erasure request. Refuses a team still in the archive; opt it out first."""
    from . import bootstrap
    from .application import archive_erasure
    from .infra.db import verify_db

    await verify_db()
    try:
        async with bootstrap.archive_object_store():
            result = await archive_erasure.erase_org(args.org)
    except archive_erasure.NotOptedOut as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


async def _enterprise_reconcile(args) -> int:
    """Bounded full-roster repair; run periodically, independent of webhook delivery."""
    import time
    import httpx
    from sqlmodel import select
    from .application import enterprise_sso
    from .config import get_settings
    from .infra import workos
    from .infra.db import session_maker, verify_db
    from .models import EnterpriseSSO

    await verify_db()
    key = get_settings().workos_api_key
    if not key:
        print(json.dumps({"error": "WorkOS API key is not configured"}))
        return 1
    async with session_maker() as db:
        statement = select(EnterpriseSSO).where(
            EnterpriseSSO.plan == "sso_directory",
            EnterpriseSSO.subscription_status.in_(("active", "trialing", "past_due")),
        )
        if args.org:
            statement = statement.where(EnterpriseSSO.org_id == args.org)
        targets = [(row.org_id, row.workos_org_id, row.directory_id, row.directory_active) for row in
                   (await db.execute(statement)).scalars().all() if row.workos_org_id]
    results = []
    failed = False
    async with httpx.AsyncClient() as client:
        for org_id, workos_org_id, known_directory_id, was_active in targets:
            observed_at_ms = int(time.time() * 1000)
            try:
                directories = await workos.directories_for_organization(
                    client, key=key, organization=workos_org_id)
                active = [row for row in directories if row.get("state") == "linked"
                          and row.get("organization_id") == workos_org_id]
                if not active:
                    removed = await enterprise_sso.deactivate_directory_from_api(
                        org_id, workos_org_id)
                    results.append({"org_id": org_id, "roster_size": 0,
                                    "memberships_removed": removed})
                    continue
                if len(active) != 1 or not isinstance(active[0].get("id"), str):
                    raise enterprise_sso.EnterpriseSSOError("expected one active directory")
                directory_id = active[0]["id"]
                if was_active and known_directory_id != directory_id:
                    raise enterprise_sso.EnterpriseSSOError("directory changed; review required")
                roster = await workos.directory_users(client, key=key, directory=directory_id)
                result = await enterprise_sso.reconcile_directory(
                    org_id, directory_id, roster, observed_at_ms,
                    verified_workos_org_id=workos_org_id)
                results.append({"org_id": org_id, **result})
            except (workos.WorkOSError, enterprise_sso.EnterpriseSSOError):
                failed = True
                results.append({"org_id": org_id, "error": "reconciliation failed; no partial roster applied"})
    print(json.dumps({"results": results}, sort_keys=True))
    return 1 if failed else 0


async def _enterprise_cleanup(args) -> int:
    """Retry canceled-team WorkOS teardown without holding a DB connection over HTTP."""
    from sqlmodel import select
    from .application import billing
    from .infra import workos
    from .infra.db import session_maker, verify_db
    from .models import EnterpriseSSO

    await verify_db()
    async with session_maker() as db:
        statement = select(EnterpriseSSO.org_id).where(
            EnterpriseSSO.subscription_status.in_(("canceled", "incomplete_expired")),
            EnterpriseSSO.workos_cleanup_completed_at.is_(None),
        )
        if args.org:
            statement = statement.where(EnterpriseSSO.org_id == args.org)
        targets = (await db.execute(statement.order_by(EnterpriseSSO.org_id))).scalars().all()
    results = []
    failed = False
    for org_id in targets:
        try:
            results.append(await billing.cleanup_canceled_workos(org_id))
        except (billing.EnterpriseBillingError, workos.WorkOSError):
            failed = True
            results.append({"org_id": org_id, "error": "WorkOS cleanup failed; retry required"})
    print(json.dumps({"results": results}, sort_keys=True))
    return 1 if failed else 0


async def _enterprise_purge_audit(args) -> int:
    from datetime import datetime, timedelta, timezone
    from sqlalchemy import delete
    from .infra.db import session_maker, verify_db
    from .models import EnterpriseSSOAudit

    await verify_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).replace(tzinfo=None)
    async with session_maker() as db:
        result = await db.execute(delete(EnterpriseSSOAudit).where(
            EnterpriseSSOAudit.created_at < cutoff))
        await db.commit()
    print(json.dumps({"audit_rows_deleted": result.rowcount, "retention_days": args.days}))
    return 0


def _positive_int(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


async def _run_command(args) -> int:
    from . import analytics
    from .infra import kv
    from .infra.money_admission_reporting import emit_snapshot
    from .infra.money_trace_runner import money_trace_lifespan

    # In particular, async hold settlement runs here without any web application's lifespan.
    try:
        async with money_trace_lifespan(role="worker"):
            try:
                return await args.fn(args)
            finally:
                try:
                    emit_snapshot(role="worker", shutdown=True)
                except Exception:  # noqa: BLE001 - preserve the command's result/failure
                    pass
                finally:
                    await kv.close()
    finally:
        try:
            # The trace lifespan also queues lease/trace exit summaries. Drain once after all
            # producers stop; a short worker must not queue its tail after its final send.
            async with asyncio.timeout(1):
                await analytics.drain()
        except Exception:  # noqa: BLE001 - local exit summaries remain the fallback evidence
            pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="treg-worker", description=__doc__)
    sub = ap.add_subparsers(dest="group", required=True)
    cap = sub.add_parser("capacity", help="platform vendor-account capacity")
    capsub = cap.add_subparsers(dest="cmd", required=True)
    sweep = capsub.add_parser("sweep", help="collect balances/quotas → snapshots → latest state")
    sweep.add_argument("--only", help="comma-separated providers (default: all)")
    sweep.add_argument("--json", action="store_true")
    sweep.set_defaults(fn=_capacity_sweep)
    ov = sub.add_parser("overflow", help="aggregator overflow routes")
    ovsub = ov.add_subparsers(dest="cmd", required=True)
    sync = ovsub.add_parser("sync", help="seed (+ live aggregator catalogs) → overflow_route, derive enabled")
    sync.add_argument("--live", action="store_true", help="also fetch the aggregators' catalogs (needs keys)")
    sync.set_defaults(fn=_overflow_sync)
    ver = ovsub.add_parser("verify", help="re-verify routes with a cheap call (spends money; needs keys)")
    ver.add_argument("--all", action="store_true", help="every row, not only enabled/previously verified")
    ver.add_argument("--only", help="comma-separated providers (default: all)")
    ver.add_argument("--max-usd", type=float, default=0.02,
                     help="per-route price cap for never-verified routes (discovery under --all)")
    ver.add_argument("--renew-max-usd", type=float, default=RENEW_MAX_USD,
                     help="per-route price cap for routes already enabled or previously verified")
    ver.add_argument("--budget-usd", type=float, default=VERIFY_BUDGET_USD,
                     help="stop attempting routes once the run's estimated spend would exceed this")
    ver.set_defaults(fn=_overflow_verify)
    tasks = sub.add_parser("asynctasks", help="deferred asynchronous task settlement")
    tasksub = tasks.add_subparsers(dest="cmd", required=True)
    settle = tasksub.add_parser("settle", help="poll due tasks and complete their existing holds")
    settle.add_argument("--limit", type=int, default=50)
    settle.set_defaults(fn=_asynctasks_settle)
    hub = sub.add_parser("hub", help="the tool hub")
    hubsub = hub.add_subparsers(dest="cmd", required=True)
    chk = hubsub.add_parser("check", help="run every live hub tool's check.json once, as its maker (spends the maker's balance at step prices)")
    chk.add_argument("--only", help="one tool id (default: every live tool, newest version)")
    chk.add_argument("--json", action="store_true")
    chk.set_defaults(fn=_hub_check)
    arena = sub.add_parser("arena", help="Enrich Arena database-backed statistics")
    arenasub = arena.add_subparsers(dest="cmd", required=True)
    insights = arenasub.add_parser("insights", help="fold new audit rows into the rolling Arena aggregate")
    insights.add_argument("--max-seconds", type=float, default=110.0,
                          help="stop after this long even with backlog left; the next run resumes")
    insights.set_defaults(fn=_arena_insights)
    sh = sub.add_parser("shadow", help="the shadow provider run (off unless shadow_provider is set)")
    shsub = sh.add_subparsers(dest="cmd", required=True)
    shc = shsub.add_parser("collect", help="pair new served calls with the shadow provider's answers")
    shc.add_argument("--max-seconds", type=float, default=110.0)
    shc.set_defaults(fn=_shadow_collect)
    shr = shsub.add_parser("report", help="print the comparison report as JSON")
    shr.add_argument("--days", type=int, default=1)
    shr.set_defaults(fn=_shadow_report)
    web_arena = sub.add_parser("web-arena", help="Web Arena saved leaderboard totals")
    websub = web_arena.add_subparsers(dest="cmd", required=True)
    totals = websub.add_parser("totals", help="publish content-free Web provider totals")
    totals.set_defaults(fn=_web_arena_totals)
    seed = websub.add_parser("seed", help="resume a bounded ten-day Web observation seed")
    seed.set_defaults(fn=_web_arena_seed)
    catalog = sub.add_parser("catalog", help="catalog read models derived from the audit table")
    catalogsub = catalog.add_subparsers(dest="cmd", required=True)
    stats = catalogsub.add_parser("stats", help="fold new audit rows into per-endpoint, per-day reliability stats")
    stats.add_argument("--max-rows", type=int, default=500_000,
                       help="audit rows to consume in one run; the next run resumes from the cursor")
    stats.add_argument("--max-dirty-buckets", type=int, default=20,
                       help="maximum endpoint/day corrections to attempt in one run (zero pauses repairs)")
    stats.add_argument("--max-repair-seconds", type=float, default=60.0,
                       help="total time budget for late-terminal corrections after the append fold")
    stats.add_argument("--bucket-timeout-seconds", type=float, default=10.0,
                       help="timeout for one endpoint/day replacement; failures stay queued for retry")
    stats.set_defaults(fn=_catalog_stats)
    requeue = catalogsub.add_parser("requeue-async-stats",
        help="preview existing mutable async day buckets; --apply queues one full repair")
    requeue.add_argument("--apply", action="store_true",
        help="reset target readiness and enqueue repairs (default: read-only preview)")
    requeue.set_defaults(fn=_catalog_requeue_async_stats)
    repair_status = catalogsub.add_parser("repair-status",
        help="check coverage of the same database's saved applied repair manifest")
    repair_status.add_argument("--manifest", required=True,
        help="JSON output saved from requeue-async-stats --apply")
    repair_status.set_defaults(fn=_catalog_repair_status)
    showcase = sub.add_parser("showcase", help="the /showcase leaderboard's read models")
    showcasesub = showcase.add_subparsers(dest="cmd", required=True)
    activity = showcasesub.add_parser("activity", help="add each approved app's new calls to its weekly activity rows")
    activity.set_defaults(fn=_showcase_activity)
    ranks = sub.add_parser("rankings", help="the public /rankings page's read model")
    rankssub = ranks.add_subparsers(dest="cmd", required=True)
    rrefresh = rankssub.add_parser("refresh", help="rebuild every board from the per-day endpoint stats and save it")
    rrefresh.set_defaults(fn=_rankings_refresh)
    media = sub.add_parser("media", help="picture files in the public media bucket")
    mediasub = media.add_subparsers(dest="cmd", required=True)
    media_sweep = mediasub.add_parser("sweep", help="delete public pictures no team, person or app uses any more")
    media_sweep.set_defaults(fn=_media_sweep)
    spend = sub.add_parser("spend", help="spend read models derived from the ledger")
    spendsub = spend.add_subparsers(dest="cmd", required=True)
    rollup = spendsub.add_parser("rollup", help="fold new ledger settles into per-key, per-tool, per-day spend")
    rollup.add_argument("--max-rows", type=_positive_int, default=50_000,
                        help="settle entries to fold in one run; the next run resumes from the cursor")
    rollup.add_argument("--max-recount-seconds", type=float, default=60.0,
                        help="time one run spends on the daily recount; the next run resumes it")
    rollup.set_defaults(fn=_spend_rollup)
    usage = sub.add_parser("usage", help="call-count read models derived from the audit table")
    usagesub = usage.add_subparsers(dest="cmd", required=True)
    urollup = usagesub.add_parser("rollup", help="fold new audit rows into per-member, per-tool, per-day call counts")
    urollup.add_argument("--max-rows", type=_positive_int, default=500_000,
                         help="audit rows to fold in one run; the next run resumes from the cursor")
    urollup.set_defaults(fn=_usage_rollup)
    jev = sub.add_parser("jev", help="the /jev landing-page demos")
    jevsub = jev.add_subparsers(dest="cmd", required=True)
    xb = jevsub.add_parser("xboost", help="launch posts on X from the last 24h -> forensics -> jev; stores the run for /jev")
    xb.add_argument("--topic", action="append", help="search phrase (repeatable); default is the AI-product set")
    xb.add_argument("--posts", type=int, default=60, help="cap on posts judged, by views")
    xb.add_argument("--min-likes", type=int, default=150)
    xb.set_defaults(fn=_jev_xboost)
    admin = sub.add_parser("admin", help="admin maintenance tasks")
    adminsub = admin.add_subparsers(dest="cmd", required=True)
    purge = adminsub.add_parser("purge-evidence", help="blank expired error evidence past the 14-day retention window")
    purge.add_argument("--batch-size", type=_positive_int, default=5000,
                       help="rows to update per transaction (default 5000)")
    purge.set_defaults(fn=_admin_purge_evidence)
    clicks = adminsub.add_parser("purge-ad-clicks", help="delete email-to-click stash rows past 90 days")
    clicks.set_defaults(fn=_admin_purge_ad_clicks)
    erase = adminsub.add_parser("erase-archive", help="erase what an opted-out team stored in the archive")
    erase.add_argument("--org", type=_positive_int, required=True, help="the team's org id")
    erase.set_defaults(fn=_admin_erase_archive)
    enterprise = sub.add_parser("enterprise", help="enterprise identity maintenance")
    enterprisesub = enterprise.add_subparsers(dest="cmd", required=True)
    reconcile = enterprisesub.add_parser("reconcile", help="repair missed WorkOS Directory Sync changes")
    reconcile.add_argument("--org", type=_positive_int, help="one team id (default: every connected team)")
    reconcile.set_defaults(fn=_enterprise_reconcile)
    cleanup = enterprisesub.add_parser("cleanup", help="remove WorkOS setup after paid SSO ends")
    cleanup.add_argument("--org", type=_positive_int, help="one team id (default: every canceled team)")
    cleanup.set_defaults(fn=_enterprise_cleanup)
    purge_audit = enterprisesub.add_parser("purge-audit", help="delete expired enterprise access evidence")
    purge_audit.add_argument("--days", type=_positive_int, default=400)
    purge_audit.set_defaults(fn=_enterprise_purge_audit)
    args = ap.parse_args(argv)
    _need_server()
    return asyncio.run(_run_command(args))


if __name__ == "__main__":
    raise SystemExit(main())
