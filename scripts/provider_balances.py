#!/usr/bin/env python3
"""What each provider says our account has left, next to what our ledger says we spent.

    uv run python scripts/provider_balances.py [--days 30] [--json]

The collectors moved to `treg.domain.capacity.collectors` (the worker's `capacity sweep` persists
them as snapshots); this script is the by-hand reconciliation view: the provider's balance beside
`/admin/reconcile/spend` for the window. Needs the platform keys in the env, like the sweep.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from sqlalchemy.exc import OperationalError  # noqa: E402

from treg import reconcile  # noqa: E402
from treg.domain import money as ledger  # noqa: E402
from treg.config import get_settings  # noqa: E402
from treg.infra.db import session_maker  # noqa: E402
from treg.domain.capacity.collectors import (  # noqa: E402,F401 — re-exported for older callers
    AUX_SLOTS, BALANCE_ROUTES, NO_BALANCE_API, all_platform_providers, provider_balance,
)

_all_platform_providers = all_platform_providers
_provider_balance = provider_balance


async def main(days: int, as_json: bool) -> int:
    since = reconcile.window_start(days)
    try:
        async with session_maker() as db:
            spend = await reconcile.provider_spend(db, since)
    except OperationalError as exc:  # the ledger tables land in the explicit release upgrade
        print(f"no ledger in {get_settings().database_url}: {exc.orig}\n"
              "Point TREG_DATABASE_URL at the deployment's database (the balances below still work).",
              file=sys.stderr)
        spend = {"margin": float(get_settings().platform_margin or 0.0), "providers": []}
    by_provider = {p["provider"]: p for p in spend["providers"]}
    providers = sorted(set(_all_platform_providers()) | set(by_provider))
    balances = {b["provider"]: b for b in
                await asyncio.gather(*(_provider_balance(p) for p in providers))}

    if as_json:
        print(json.dumps({"since": since.isoformat(), "days": days, "margin": spend["margin"],
                          "providers": [{**balances[p], **by_provider.get(p, {})} for p in providers]},
                         indent=2, default=str))
        return 0

    print(f"provider balances vs ledger spend, last {days}d (since {since:%Y-%m-%d %H:%M} UTC)\n")
    print(f"{'provider':<18}{'they report':>26}{'our spend (est)':>18}{'billed':>12}{'calls':>8}")
    for p in providers:
        b, s = balances[p], by_provider.get(p, {})
        if b["value"] is not None:
            v = b["value"]
            theirs = f"{'$' if b['unit'].startswith('USD') else ''}{v:,.2f} {b['unit'].removeprefix('USD ')}".strip()
        else:
            theirs = "—"
        cost = ledger.usd(s["provider_cost_est_micro"]) if s else 0.0
        print(f"{p:<18}{theirs:>26}{'$%.4f' % cost:>18}"
              f"{'$%.4f' % ledger.usd(s.get('charged_micro', 0)):>12}{s.get('calls', 0):>8}")
        if b["note"]:
            print(f"{'':<18}{b['note']}")
    print(f"\n'our spend (est)' backs the {spend['margin']:.0%} platform margin out of what orgs were "
          "billed — it is the figure to compare against the provider's invoice or balance delta.\n"
          "A balance is a POINT in time: the reconciliation is (balance_then - balance_now) vs spend.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=30, help="ledger window in days (default: 30)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.days, args.json)))
