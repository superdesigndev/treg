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
    todo = [r for r in rows if args.all or r.enabled or r.last_verified_at]
    keys = {"orthogonal": s.overflow_key_orthogonal, "monid": s.overflow_key_monid}
    tally = {"passed": 0, "failed": 0, "aggregator": 0, "inconclusive": 0}
    skipped, key_failures = 0, []
    # One SHORT transaction per route. The first prod run (2026-08-28) kept a single session open
    # across every network round-trip: each `db.get` autoflushed the previous row's UPDATE, the row
    # locks piled up for minutes, and the run died at route 60 with LockNotAvailableError. Where
    # that 5 s bound came from is NOT db.py — its pools set no lock_timeout, and `alembic/env.py`'s
    # applies only to the migration connection, which this worker never opens. Most likely the
    # database role carries one. Recorded as observed rather than explained: the fix (one short
    # transaction per route) is right whatever set it.
    async with httpx.AsyncClient(timeout=60) as c:
        for r in todo:
            ep = by_id.get(r.endpoint_id)
            key = keys.get(r.aggregator)
            tr = (ep or {}).get("test_request")
            usd = (r.agg_price_micro or 0) / 1e6
            if not ep or not key or not tr or usd > args.max_usd:
                skipped += 1
                continue
            direct = None
            prov = oauth_providers.get(ep["provider"])
            pkey = getattr(s, platform_setting_name(ep["provider"]), "")
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
          f"aggregator errors {tally['aggregator']}, skipped {skipped}")
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
    ver.add_argument("--max-usd", type=float, default=0.02, help="skip routes priced above this")
    ver.set_defaults(fn=_overflow_verify)
    tasks = sub.add_parser("asynctasks", help="deferred asynchronous task settlement")
    tasksub = tasks.add_subparsers(dest="cmd", required=True)
    settle = tasksub.add_parser("settle", help="poll due tasks and complete their existing holds")
    settle.add_argument("--limit", type=int, default=50)
    settle.set_defaults(fn=_asynctasks_settle)
    args = ap.parse_args(argv)
    _need_server()
    return asyncio.run(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
