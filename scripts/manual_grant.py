#!/usr/bin/env python3
"""Comp an org some credit by hand — the sanctioned way, through `money.grant()`.

    # 1. look only. Prints the user, every team they belong to, and each one's balance.
    uv run --frozen python scripts/manual_grant.py --email someone@example.com

    # 2. write, once you have picked the org id from step 1.
    uv run --frozen python scripts/manual_grant.py --email someone@example.com \
        --org-id 42 --amount-usd 100 --ref hs-1234 --reason "goodwill, approved by <you>" --confirm

Step 1 is read-only and is not optional: a person can belong to several teams, and the credit lands
on an ORG, not on a person. Guessing which one is how the money ends up in the wrong team.

WHY THIS EXISTS AT ALL
----------------------
There is no admin HTTP route that credits a balance, and there must not be a hand-written UPDATE.
`Org.balance_micro` is a MATERIALIZED column, not the truth: spend is drawn from `CreditBlock` rows
(`money._consume_blocks`). Raising the column alone produces a balance that looks spendable and
isn't, plus a hole in the ledger that `reconcile.py` will report forever. So this script does what
every other funding path does — it calls `money.grant()` and lets the one money module write all
three rows (block, balance, ledger entry) in one transaction.

WHY `kind="promotional"` AND NOT SOMETHING DESCRIPTIVE LIKE "manual"
--------------------------------------------------------------------
Spend burns blocks in `money._KIND_ORDER` order, and that map is a closed set:

    _KIND_ORDER = {"promotional": 0, "referral": 0, "bonus": 0, "purchased": 1}

An unrecognised kind falls through to `.get(kind, 99)` and therefore sorts AFTER purchased. A block
tagged "manual" would be spent only once the team's own paid-for credit was gone — the exact
opposite of what a comp is for, and it silently shrinks the refundable pool first. Comped credit is
a marketing expense and non-refundable, which is what `promotional` already means, so that is the
correct kind. Adding a new one is a code change to `_KIND_ORDER`, not a flag on this script.

WHY `once=False`, AND WHAT REPLACES THE IDEMPOTENCY IT GIVES UP
---------------------------------------------------------------
`money.grant(once=True)` is idempotent per (org, kind) — it grants nothing if the org already holds
a block of that kind. Every org gets a `promotional` block at signup, so `once=True` here would
silently no-op and report success. This passes `once=False` and supplies its own guard instead:
`--ref` is recorded at `meta.ref` on the ledger entry, and a run whose ref is already present on a
grant for that org refuses. Reuse the same `--ref` (a ticket id works well) for a given comp and a
sequential re-run refuses instead of crediting again.

That guard is BEST-EFFORT, against sequential reuse only - it is a scan of prior ledger entries in
application code with no database uniqueness behind it (`meta` is JSON; nothing constrains
`meta.ref`), so it is NOT concurrency-safe: two simultaneous invocations with the same `--ref` both
pass the scan and both credit. That is acceptable for a human-driven ops script - one operator, one
terminal - and the fix if it ever stops being true is a unique column, not a smarter scan. Do not
run two of these at once for the same comp.

WHAT IT DOES TO PRODUCTION
--------------------------
Prod Postgres keeps an EMPTY `ipAllowList`, so it opens a hole for this machine's /32, works, and
closes it in a `finally` — then re-reads the resource to PROVE it closed, exactly as
`usage_report.py` does. If you see "allowlist NOT closed", close it by hand before anything else.

RUN IT FROM `main`, NOT FROM A FEATURE BRANCH
----------------------------------------------
Unlike `usage_report.py` (raw SQL, branch-proof), this one goes through the ORM, so it is bound to
whatever `src/treg/models.py` the checkout has. A branch carrying an unmigrated column makes the
query fail against prod — or worse, half-match. Check out `main` before running it against prod.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from usage_report import DB_ID, env, my_ip, render_api  # noqa: E402


def usd(micro: int | None) -> str:
    return f"${(micro or 0) / 1_000_000:,.2f}"


def to_micro(amount: str) -> int:
    """USD string -> integer micro-USD. Decimal, never float: `100.10 * 1_000_000` is 100099999.99."""
    try:
        value = Decimal(amount)
    except InvalidOperation:
        raise SystemExit(f"--amount-usd {amount!r} is not a number")
    if value <= 0:
        raise SystemExit("--amount-usd must be positive")
    micro = value * 1_000_000
    if micro != micro.to_integral_value():
        raise SystemExit(f"--amount-usd {amount} is finer than micro-USD; it cannot be represented")
    return int(micro)


def prod_dsn() -> str:
    """Render's external connection string, rewritten for SQLAlchemy's asyncpg driver.

    `sslmode` is a libpq parameter that asyncpg rejects outright, so it is stripped here and the TLS
    requirement is re-expressed as a connect_arg on the engine below.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    raw = render_api("GET", f"/postgres/{DB_ID}/connection-info")["externalConnectionString"]
    parts = urlsplit(raw)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"]
    scheme = "postgresql+asyncpg" if parts.scheme in ("postgres", "postgresql") else parts.scheme
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


async def run(args) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
    from sqlmodel import select

    from treg.domain import money
    from treg.models import CreditBlock, LedgerEntry, Membership, Org, User

    ip = my_ip()
    print(f"opening prod allowlist for {ip}/32 ...", file=sys.stderr)
    render_api("PATCH", f"/postgres/{DB_ID}",
               {"ipAllowList": [{"cidrBlock": f"{ip}/32", "description": "manual_grant.py"}]})
    try:
        engine = create_async_engine(prod_dsn(), connect_args={"ssl": "require"}, future=True)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            # Prove the connection BEFORE any money code runs, and ride out the two transient
            # failures that mean nothing is wrong: the allowlist PATCH above takes a moment to take
            # effect, and Render's Postgres hostname intermittently SERVFAILs. Without this the
            # first attempt reliably dies with ConnectionDoesNotExistError (observed 2026-08-27).
            # The `finally` below still closes the allowlist if every attempt fails.
            for attempt, pause in enumerate((2, 5, 10, 0), start=1):
                try:
                    async with engine.connect():
                        pass
                    break
                except Exception as exc:  # noqa: BLE001 — asyncpg raises several unrelated types
                    if not pause:
                        raise
                    print(f"  connect attempt {attempt} failed ({type(exc).__name__}); "
                          f"retrying in {pause}s", file=sys.stderr)
                    await asyncio.sleep(pause)
            async with maker() as db:
                return await _work(db, args, money, User, Membership, Org, CreditBlock,
                                   LedgerEntry, select)
        finally:
            await engine.dispose()
    finally:
        # Runs on success, on error and on Ctrl-C. The re-read is the point: a PATCH that 200s but
        # leaves the list populated would quietly leave prod exposed until somebody noticed.
        try:
            render_api("PATCH", f"/postgres/{DB_ID}", {"ipAllowList": []})
            still = render_api("GET", f"/postgres/{DB_ID}").get("ipAllowList")
            if still:
                print(f"!! allowlist NOT closed — still {still}. Close it by hand NOW.", file=sys.stderr)
            else:
                print("prod allowlist closed and verified.", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — never let a bug here leave prod open silently
            print(f"!! could not close the allowlist ({exc}). Close it by hand NOW.", file=sys.stderr)


async def _work(db, args, money, User, Membership, Org, CreditBlock, LedgerEntry, select) -> int:
    user = (await db.execute(select(User).where(User.email == args.email))).scalars().first()
    if user is None:
        print(f"no user with email {args.email}", file=sys.stderr)
        return 1

    rows = (await db.execute(
        select(Membership, Org).join(Org, Org.id == Membership.org_id)
        .where(Membership.user_id == user.id).order_by(Org.id)
    )).all()
    if not rows:
        print(f"{args.email} (user {user.id}) belongs to no team", file=sys.stderr)
        return 1

    print(f"\nuser {user.id}  {user.email}"
          f"{'  [superadmin]' if user.is_superadmin else ''}"
          f"{'  [SUSPENDED]' if user.suspended else ''}")
    print(f"\n{'org':>6}  {'slug':<28} {'role':<7} {'balance':>12}  flags")
    for m, org in rows:
        flags = " ".join(f for f, on in (("suspended", org.suspended), ("demo", org.demo),
                                         ("public_demo", org.public_demo)) if on)
        print(f"{org.id:>6}  {org.slug:<28} {m.role:<7} {usd(org.balance_micro):>12}  {flags}")

    if not args.confirm:
        print("\nread-only. Re-run with --org-id <id> --amount-usd <n> --ref <id> --reason <text> "
              "--confirm to credit one of these teams.")
        return 0

    org = next((o for _, o in rows if o.id == args.org_id), None)
    if org is None:
        print(f"\norg {args.org_id} is not one of {args.email}'s teams — refusing.", file=sys.stderr)
        return 1

    # The guard replacing the idempotency `once=False` gives up - BEST-EFFORT, sequential-only.
    # `meta` is JSON, so this filters in Python rather than betting on one dialect's JSON operators,
    # and no unique index backs it: two simultaneous invocations with the same --ref both pass this
    # scan and both credit (see the module docstring).
    prior = (await db.execute(
        select(LedgerEntry).where(LedgerEntry.org_id == org.id, LedgerEntry.kind == "grant")
    )).scalars().all()
    clash = next((e for e in prior if (e.meta or {}).get("ref") == args.ref), None)
    if clash is not None:
        print(f"\nref {args.ref!r} was already granted to org {org.id} "
              f"({usd(clash.amount_micro)}, entry {clash.id}, {clash.created_at}) — refusing.",
              file=sys.stderr)
        return 1

    micro = to_micro(args.amount_usd)
    print(f"\ncrediting org {org.id} ({org.slug}) {usd(micro)} as a promotional block"
          f"\n  balance {usd(org.balance_micro)} -> {usd(org.balance_micro + micro)}"
          f"\n  ref={args.ref!r} reason={args.reason!r}")

    block = await money.grant(
        db, org.id, amount_micro=micro, kind="promotional", once=False,
        meta={"ref": args.ref, "reason": args.reason, "source": "scripts/manual_grant.py",
              "granted_to": args.email, "granted_by": args.by},
    )
    await db.commit()  # grant only stages; without this the credit never lands
    if block is None:  # `once=False` cannot return None for a positive amount; belt and braces
        print("grant returned None — nothing was credited.", file=sys.stderr)
        return 1

    fresh = (await db.execute(select(Org).where(Org.id == org.id))).scalars().first()
    total = (await db.execute(
        select(CreditBlock).where(CreditBlock.org_id == org.id)
    )).scalars().all()
    print(f"\nok. block {block.id}  balance now {usd(fresh.balance_micro)} "
          f"across {len(total)} block(s).")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Comp an org credit through money.grant().")
    p.add_argument("--email", required=True, help="the member whose team is being credited")
    p.add_argument("--org-id", type=int, help="which of their teams (see the read-only listing)")
    p.add_argument("--amount-usd", help='e.g. "100"')
    p.add_argument("--ref", help="dedupe key - a sequential re-run with the same ref refuses "
                                 "(best-effort only; not safe against concurrent runs)")
    p.add_argument("--reason", help="recorded on the ledger entry")
    p.add_argument("--by", default=os.environ.get("USER", ""), help="who authorised this")
    p.add_argument("--confirm", action="store_true", help="actually write; omit to only look")
    args = p.parse_args()

    if args.confirm and not all((args.org_id, args.amount_usd, args.ref, args.reason)):
        raise SystemExit("--confirm needs --org-id, --amount-usd, --ref and --reason")
    env("RENDER_API_KEY")  # fail before touching the allowlist, not halfway through
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
