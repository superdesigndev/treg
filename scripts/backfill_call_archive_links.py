#!/usr/bin/env python3
"""Link calls made BEFORE migration 0011 to the archived answers they received — best effort.

New calls carry `callrecord.archive_key_hash` / `archive_content_hash` themselves (the audit row
gets them from `archive.record()`), which is what `GET /calls/{id}/result` reads. Older rows cannot
be linked exactly: the archive key is computed from the full query string and body, and the audit
row never kept either. What both sides DO share is the endpoint, the byte size of the answer
(`callrecord.response_bytes` == `archivesnapshot.size_bytes`) and a fetch time within seconds of
each other, so this script matches on those and links a pair only when it is unambiguous in BOTH
directions: one snapshot ↔ one call. Anything else is counted and left alone — a wrong link would
show a team someone else's answer, and "no result on file" is the honest fallback.

Only metered platform 2xx rows that are not already linked are candidates (the archive never holds
anything else). Served hits are skipped.

    python scripts/backfill_call_archive_links.py                # dry run: counts only
    python scripts/backfill_call_archive_links.py --apply        # write the links
    python scripts/backfill_call_archive_links.py --window 10    # ± seconds (default 10)

Connects with `TREG_DATABASE_URL` / `--dsn` (a postgres:// DSN).
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# One statement finds every unambiguous pair. `snap_n` is how many calls a snapshot could be, and
# `call_n` how many snapshots a call could be — a link is written only where both are exactly 1.
MATCH_SQL = """
with cand as (
  select s.id as snapshot_id, k.key_hash, s.content_hash, c.id as call_id,
         count(*) over (partition by s.id) as snap_n,
         count(*) over (partition by c.id) as call_n
  from archivesnapshot s
  join archivekey k on k.id = s.key_id
  join callrecord c
    on c.endpoint_id = k.endpoint_id
   and c.credential_tier = 'platform'
   and c.status_code between 200 and 299
   and c.cached = false
   and c.archive_key_hash is null
   and c.response_bytes = s.size_bytes
   and c.created_at between s.fetched_at - make_interval(secs => $1)
                        and s.fetched_at + make_interval(secs => $1)
  where s.origin = 'caller'
)
select snapshot_id, key_hash, content_hash, call_id, snap_n, call_n from cand
"""

COUNT_SQL = """
select
  (select count(*) from archivesnapshot where origin = 'caller') as snapshots,
  (select count(*) from callrecord
     where credential_tier = 'platform' and status_code between 200 and 299
       and cached = false and archive_key_hash is null) as unlinked_calls
"""

UPDATE_SQL = "update callrecord set archive_key_hash = $1, archive_content_hash = $2 where id = $3 and archive_key_hash is null"


async def run(conn, *, window_s: int, apply: bool) -> dict:
    totals = dict(await conn.fetchrow(COUNT_SQL))
    rows = await conn.fetch(MATCH_SQL, float(window_s))
    exact = [r for r in rows if r["snap_n"] == 1 and r["call_n"] == 1]
    ambiguous = {r["call_id"] for r in rows if r["snap_n"] > 1 or r["call_n"] > 1}
    report = {**totals, "window_s": window_s, "matched": len(exact), "ambiguous_calls": len(ambiguous),
              "unmatched_calls": totals["unlinked_calls"] - len(exact) - len(ambiguous),
              "applied": 0}
    if apply and exact:
        async with conn.transaction():
            for r in exact:
                tag = await conn.execute(UPDATE_SQL, r["key_hash"], r["content_hash"], r["call_id"])
                report["applied"] += int(tag.split()[-1])
    return report


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="write the links (default: dry run)")
    ap.add_argument("--window", type=int, default=10, help="± seconds between call and fetch (default 10)")
    ap.add_argument("--dsn", default=os.environ.get("TREG_DATABASE_URL", ""), help="postgres DSN")
    args = ap.parse_args()

    import asyncpg

    if not args.dsn:
        print("no DSN: pass --dsn, set TREG_DATABASE_URL", file=sys.stderr)
        return 2
    dsn = args.dsn.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn, timeout=45)
    try:
        report = await run(conn, window_s=args.window, apply=args.apply)
    finally:
        await conn.close()

    mode = "APPLIED" if args.apply else "DRY RUN"
    print(f"[{mode}] snapshots={report['snapshots']} unlinked_platform_2xx_calls={report['unlinked_calls']} "
          f"window=±{report['window_s']}s → matched={report['matched']} "
          f"ambiguous={report['ambiguous_calls']} unmatched={report['unmatched_calls']} "
          f"written={report['applied']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
