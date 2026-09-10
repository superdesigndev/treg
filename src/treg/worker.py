"""`treg-worker` — the scheduled, server-side maintainer commands (the `worker` profile).

    treg-worker capacity sweep [--only provider,...] [--json]
    treg-worker overflow sync [--live]          # seed (+ live aggregator catalogs) → overflow_route
    treg-worker overflow verify [--all] [--max-usd 0.02]   # weekly re-verify of enabled routes
    treg-worker asynctasks settle [--limit 50]       # complete deferred metered-call holds

Not the light `treg` CLI: these need the server extra (DB, platform keys in the env) and make
outbound calls to third parties, so they run as Render cron jobs with the server's env — never as
dataplane lifespan work (refactor plan §2.2). The worker never originates a money movement. It may
complete a hold opened by the request path, settling or releasing it with full call and org attribution.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _need_server() -> None:
    try:
        import sqlalchemy  # noqa: F401
    except ImportError:  # pragma: no cover — a base install has no DB stack
        print("treg-worker needs the server extra: pip install 'treg[server]'", file=sys.stderr)
        raise SystemExit(2)


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
    from .domain.capacity import routes as R
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
    # One SHORT transaction per route. The first prod run (2026-08-28) kept a single session open
    # across every network round-trip: each `db.get` autoflushed the previous row's UPDATE, the row
    # locks piled up for minutes, and the run died at route 60 with LockNotAvailableError. Where
    # that 5 s bound came from is NOT db.py — its pools set no lock_timeout, and `alembic/env.py`'s
    # applies only to the migration connection, which this worker never opens. Most likely the
    # database role carries one. Recorded as observed rather than explained: the fix (one short
    # transaction per route) is right whatever set it.
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

    await verify_db()
    result = await settle_due(limit=args.limit)
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
    args = ap.parse_args(argv)
    _need_server()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
