"""The folded read model behind catalog reliability: `treg-worker catalog stats`.

What these pin: that the buckets say exactly what the live aggregate says for the same rows (the
judgement calls live once, in `stats.publish`, and the fold mirrors the SQL predicates); that the
cursor never consumes a row still inside the commit lag; that the first run starts inside the
window and later runs resume; that the reader publishes synchronous evidence only once the
worker has caught up, without falling back to an audit-table aggregate.
"""

from __future__ import annotations

import random
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from treg.application import catalog_stats
from treg.domain.catalog import stats
from treg.infra import catalog_observations
from treg.infra.catalog_observations import PostgresEndpointObservationReader
from treg.infra.db import session_maker
from treg.models import CallRecord, EndpointDayStat, EndpointStatCursor

EP = "tikhub.tiktok.user.profile"
EP2 = "tomba.people.email.find"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _record(endpoint_id: str | None, status: int, ms: int | None = 100, *, ago: timedelta,
                  refused_by: str | None = None, hit: bool | None = None, cost: int | None = None,
                  verdict: str | None = None) -> int:
    async with session_maker() as db:
        row = CallRecord(org_id=1, user_email="a@b.c", tool_name=endpoint_id or "plain", method="GET",
                         path="/x", status_code=status, endpoint_id=endpoint_id, duration_ms=ms,
                         refused_by=refused_by, hit=hit, cost_observed_micro=cost, verdict=verdict,
                         created_at=_now() - ago)
        db.add(row)
        await db.commit()
        return row.id


async def _live(ids, **kw):
    async with session_maker() as db:
        return await stats.observed(db, ids, **kw)


async def _cursor() -> EndpointStatCursor | None:
    async with session_maker() as db:
        return await db.get(EndpointStatCursor, catalog_stats.CURSOR_ID)


async def test_buckets_publish_exactly_what_the_live_aggregate_publishes(clients):
    """Same rows, same numbers: every rule the live query encodes in SQL, the fold encodes in
    Python, and `publish` is shared. A drift here is a catalog that changes its mind about a
    provider depending on which path answered."""
    old = timedelta(days=3)
    for i in range(8):
        await _record(EP, 200, 100 + i * 10, ago=old, hit=True)
    for _ in range(2):
        await _record(EP, 500, 900, ago=old, hit=None)
    await _record(EP, 405, 50, ago=old)                       # the catalog's fault: counts as bad
    await _record(EP, 422, 50, ago=old)                       # the caller's fault: a sample, not decided
    await _record(EP, 402, None, ago=old, refused_by="balance")  # never reached the provider
    for _ in range(6):
        await _record(EP2, 200, 300, ago=timedelta(days=1), hit=None, cost=0)   # per_success: free miss
    for _ in range(3):
        await _record(EP2, 200, 200, ago=timedelta(days=1), hit=None, cost=5000)  # per_success: paid hit
    await _record(None, 200, 10, ago=old)                     # a plain tool call: not catalog evidence
    await _record(EP, 200, 5, ago=timedelta(days=31))         # outside the window

    result = await catalog_stats.refresh(session_maker, now=_now())
    assert result["caught_up"] and result["rows"] > 0

    live = await _live([EP, EP2], per_success={EP2})
    reader = PostgresEndpointObservationReader(session_maker)
    folded = await reader.get_many([EP, EP2])
    # The reader computes per_success from the catalog; EP2 is per_success there too.
    assert folded[EP] == live[EP]
    assert folded[EP]["samples"] == 12 and folded[EP]["decided"] == 11
    assert folded[EP]["ok_rate"] == round(8 / 11, 4)
    assert folded[EP]["p50_ms"] == live[EP]["p50_ms"] and folded[EP]["p95_ms"] == live[EP]["p95_ms"]
    assert folded[EP]["last_ok_days"] == 3
    assert folded[EP2]["samples"] == 9 and folded[EP2]["hit_samples"] == live[EP2]["hit_samples"]
    async with session_maker() as db:
        days = (await db.execute(select(EndpointDayStat.day).where(EndpointDayStat.endpoint_id == EP))).scalars().all()
    assert len(days) == 1       # every EP row landed on one UTC day


async def test_rows_inside_the_commit_lag_wait_for_the_next_run(clients):
    """An audit row commits a few seconds after its `created_at`. A row younger than the lag is
    not consumed, and neither is anything after it, so a slow commit can never be skipped."""
    for _ in range(6):
        await _record(EP, 200, 100, ago=timedelta(minutes=5))
    young = await _record(EP, 500, 100, ago=timedelta(seconds=5))
    result = await catalog_stats.refresh(session_maker, now=_now())
    assert result["rows"] == 6 and result["caught_up"]
    assert (await _cursor()).cursor_id == young - 1
    folded = await PostgresEndpointObservationReader(session_maker).get_many([EP])
    assert folded[EP]["samples"] == 6 and folded[EP]["ok_rate"] == 1.0

    later = await catalog_stats.refresh(session_maker, now=_now() + timedelta(minutes=2))
    assert later["rows"] == 1
    assert (await _cursor()).cursor_id == young
    folded = await PostgresEndpointObservationReader(session_maker).get_many([EP])
    assert folded[EP]["samples"] == 7 and folded[EP]["ok_rate"] == round(6 / 7, 4)


async def test_async_hits_are_read_after_the_fold_cursor_passes_submission(clients):
    endpoint = "wiza.people.email.find"
    row_ids = [await _record(endpoint, 200, 100, ago=timedelta(hours=1), cost=0)
               for _ in range(20)]
    assert (await catalog_stats.refresh(session_maker, now=_now()))["caught_up"]
    reader = PostgresEndpointObservationReader(session_maker)
    pending = (await reader.get_many([endpoint]))[endpoint]
    assert pending["hit_samples"] == 0

    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.id.in_(row_ids)))).scalars().all()
        for n, row in enumerate(rows):
            row.hit = n < 12
            db.add(row)
        await db.commit()
    final = (await reader.get_many([endpoint]))[endpoint]
    assert final["hit_samples"] == 20 and final["hit_rate"] == 0.6


async def test_async_per_success_uses_terminal_hits_including_failed_attempts(clients):
    endpoint = "wiza.people.email.find"
    ids = [await _record(endpoint, 200, 100, ago=timedelta(hours=1), cost=0)
           for _ in range(24)]
    async with session_maker() as db:
        rows = (await db.execute(select(CallRecord).where(CallRecord.id.in_(ids)))).scalars().all()
        for n, row in enumerate(rows):
            row.hit = True if n < 12 else False if n < 20 else None
            db.add(row)
        await db.commit()
    observation = (await PostgresEndpointObservationReader(session_maker).get_many([endpoint]))[endpoint]
    assert observation["hit_samples"] == 20
    assert observation["hit_rate"] == 0.6


async def test_the_reader_waits_for_complete_folds_without_scanning_live(clients):
    """No cron means no synchronous evidence; a partial backfill is not a complete sample."""
    for _ in range(6):
        await _record(EP, 200, 100, ago=timedelta(hours=1))
    reader = PostgresEndpointObservationReader(session_maker)
    assert await reader.get_many([EP]) == {}
    assert await _cursor() is None

    # A run that stops on its row budget before draining does not flip the reader either.
    partial = await catalog_stats.refresh(session_maker, max_rows=2, batch_rows=2, now=_now())
    assert partial["rows"] == 2 and not partial["caught_up"]
    assert (await _cursor()).caught_up_at is None
    assert await reader.get_many([EP]) == {}  # do not publish the two-row partial backfill

    drained = await catalog_stats.refresh(session_maker, now=_now())
    assert drained["rows"] == 4 and drained["caught_up"]
    assert (await _cursor()).caught_up_at is not None
    assert (await reader.get_many([EP]))[EP]["samples"] == 6


async def test_the_first_run_starts_inside_the_window_and_prunes_old_days(clients):
    """Bisection on the primary key finds the first row inside the window, so a backfill never
    reads a month-old page; a bucket that has fallen out of the window is deleted."""
    for _ in range(3):
        await _record(EP, 200, 100, ago=timedelta(days=40))
    first_inside = await _record(EP, 200, 100, ago=timedelta(days=20))
    for _ in range(5):
        await _record(EP, 200, 100, ago=timedelta(days=2))
    async with session_maker() as db:
        assert await catalog_stats._first_id_at(db, _now() - timedelta(days=stats.WINDOW_DAYS)) == first_inside
        # A stale bucket, as if the window had moved past it since a previous run.
        db.add(EndpointDayStat(endpoint_id=EP, day="2000-01-01", n=9, ok=9, latency_sample=[]))
        await db.commit()
    result = await catalog_stats.refresh(session_maker, now=_now())
    assert result["rows"] == 6                      # the three 40-day-old rows were never visited
    async with session_maker() as db:
        days = sorted((await db.execute(select(EndpointDayStat.day))).scalars().all())
    assert "2000-01-01" not in days and len(days) == 2
    folded = await PostgresEndpointObservationReader(session_maker).get_many([EP])
    assert folded[EP]["samples"] == 6


async def test_an_overlapping_run_cannot_erase_what_the_other_folded(clients):
    """A slow backfill still running when the next schedule fires: both runs take the cursor row
    lock batch by batch and interleave. A bucket value remembered by the first run from an
    earlier batch would be stale after the second run's fold, and upserting it would erase those
    rows for good, since the cursor is already past them. Every batch must re-read its buckets
    under the lock."""
    for _ in range(8):
        await _record(EP, 200, 100, ago=timedelta(hours=1))
    at = _now()

    class Interleaved:
        """The outer run's session factory; before its second batch, another run consumes two rows."""
        calls = 0

        def __call__(self):
            Interleaved.calls += 1
            return self._session(Interleaved.calls)

        @asynccontextmanager
        async def _session(self, n):
            if n == 2:
                intruder = await catalog_stats.refresh(session_maker, max_rows=2, batch_rows=2, now=at)
                assert intruder["rows"] == 2
            async with session_maker() as db:
                yield db

    outer = await catalog_stats.refresh(Interleaved(), batch_rows=4, now=at)
    assert outer["rows"] == 6 and outer["caught_up"]
    folded = await PostgresEndpointObservationReader(session_maker).get_many([EP])
    assert folded[EP]["samples"] == 8         # 4 (outer) + 2 (intruder) + 2 (outer), nothing erased


async def test_a_dead_worker_omits_evidence_without_scanning_live(clients, caplog):
    """A stopped cron emits a health warning and omits evidence instead of increasing DB load."""
    for _ in range(6):
        await _record(EP, 200, 100, ago=timedelta(hours=1))
    assert (await catalog_stats.refresh(session_maker, now=_now()))["caught_up"]
    await _record(EP, 500, 100, ago=timedelta(minutes=30))      # never folded: the worker is gone
    reader = PostgresEndpointObservationReader(session_maker)
    assert (await reader.get_many([EP]))[EP]["samples"] == 6    # buckets, still trusted
    async with session_maker() as db:
        cursor = await db.get(EndpointStatCursor, catalog_stats.CURSOR_ID)
        cursor.updated_at = _now() - timedelta(seconds=catalog_observations.STALE_AFTER_S + 60)
        db.add(cursor)
        await db.commit()
    with caplog.at_level("WARNING", logger="treg.catalog"):
        assert await reader.get_many([EP]) == {}
    assert "catalog_stats_unavailable reason=stale" in caplog.text


async def test_an_empty_audit_table_caught_up_immediately(clients):
    result = await catalog_stats.refresh(session_maker, now=_now())
    assert result == {"rows": 0, "buckets": 0, "caught_up": True, "cursor": 0}
    folded = await PostgresEndpointObservationReader(session_maker).get_many([EP])
    assert folded[EP]["samples"] == 0 and folded[EP]["ok_rate"] is None


def test_a_busy_day_keeps_a_uniform_latency_reservoir():
    """Past `LATENCY_SAMPLE` successful durations a day keeps a uniform sample, not the first N:
    the percentile of a 10,000-call day must reflect the whole day."""
    t = stats.Tally()
    rng = random.Random(7)
    at = datetime(2026, 9, 15)
    for ms in range(10_000):      # durations grow through the day
        assert t.fold(status_code=200, created_at=at, duration_ms=ms, hit=None,
                      cost_observed_micro=None, refused_by=None, rng=rng)
    assert t.latency_seen == 10_000 and len(t.latencies) == stats.LATENCY_SAMPLE
    published = stats.publish([EP], {EP: t}, now=at)[EP]
    assert 4_000 < published["p50_ms"] < 6_000
    assert published["p95_ms"] > 9_000


def test_a_busy_day_outweighs_a_quiet_one_in_the_merged_percentile():
    """Each day keeps at most `LATENCY_SAMPLE` durations, so ten thousand fast calls and four
    hundred slow ones would meet as equals in a plain concatenation and the window's p95 would
    be the slow day's. Weighted by the calls each sample stands for, the slow calls are under
    four percent of the window and the p95 stays fast."""
    fast, slow = stats.Tally(), stats.Tally()
    rng = random.Random(3)
    at = datetime(2026, 9, 15)
    for _ in range(10_000):
        fast.fold(status_code=200, created_at=at, duration_ms=1, hit=None, cost_observed_micro=None, refused_by=None, rng=rng)
    for _ in range(stats.LATENCY_SAMPLE):
        slow.fold(status_code=200, created_at=at, duration_ms=1000, hit=None, cost_observed_micro=None, refused_by=None, rng=rng)
    window = stats.merged([(EP, fast), (EP, slow)])[EP]
    assert window.latency_seen == 10_400 and len(window.latencies) == 2 * stats.LATENCY_SAMPLE
    assert window.percentile(0.50) == 1 and window.percentile(0.95) == 1
    assert window.percentile(0.99) == 1000
    # A single uniform sample is unaffected: no weights, the plain nearest rank.
    assert slow.percentile(0.95) == 1000 and fast.percentile(0.95) == 1


def test_merging_days_keeps_the_newest_success_and_every_count():
    a = stats.Tally(n=3, ok=2, bad=1, last_ok=datetime(2026, 9, 1), hits=1, hit_decided=2, latency_seen=2, latencies=[10, 20],
                    verdicts={"valid": 2, "unknown": 1})
    b = stats.Tally(n=4, ok=4, bad=0, last_ok=datetime(2026, 9, 3), paid_hits=2, free_misses=1, latency_seen=4, latencies=[30, 40, 50, 60],
                    verdicts={"valid": 1, "catch_all": 3})
    m = stats.merged([(EP, a), (EP, b)])[EP]
    assert (m.n, m.ok, m.bad, m.last_ok) == (7, 6, 1, datetime(2026, 9, 3))
    assert (m.hits, m.hit_decided, m.paid_hits, m.free_misses) == (1, 2, 2, 1)
    assert m.verdicts == {"valid": 3, "unknown": 1, "catch_all": 3}
    assert m.latency_seen == 6 and sorted(m.latencies) == [10, 20, 30, 40, 50, 60]


async def test_observed_from_drops_the_days_before_a_replaced_service(clients, monkeypatch):
    """An entry's `observed_from` keeps the folded days before it out of the published figures:
    the provider replaced the service, and those calls measured the old one."""
    from treg.domain.catalog import store as catalog_store
    for _ in range(6):
        await _record(EP, 502, 100, ago=timedelta(days=3))
    for _ in range(6):
        await _record(EP, 200, 100, ago=timedelta(minutes=5))
    await catalog_stats.refresh(session_maker, now=_now())
    reader = PostgresEndpointObservationReader(session_maker)
    assert (await reader.get_many([EP]))[EP]["ok_rate"] == 0.5
    entry = catalog_store.load().by_id[EP]
    monkeypatch.setitem(entry, "observed_from", (_now() - timedelta(days=1)).strftime("%Y-%m-%d"))
    after = (await reader.get_many([EP]))[EP]
    assert after["samples"] == 6 and after["ok_rate"] == 1.0


VERIFY = "millionverifier.people.email.verify"


async def _bucket(endpoint_id: str) -> EndpointDayStat:
    async with session_maker() as db:
        return (await db.execute(select(EndpointDayStat).where(
            EndpointDayStat.endpoint_id == endpoint_id))).scalar_one()


async def test_the_fold_counts_verdict_words_per_day(clients):
    """Each word is counted under the same refusal rule as every other count. `unknown` is its
    own word, never folded into a miss, and a call without a word adds none."""
    old = timedelta(days=2)
    for word, count in (("valid", 12), ("invalid", 5), ("catch_all", 3), ("unknown", 2)):
        for _ in range(count):
            await _record(VERIFY, 200, 100, ago=old, hit=word != "unknown", verdict=word)
    await _record(VERIFY, 200, 100, ago=old, hit=True)                        # no word: not counted
    await _record(VERIFY, 402, None, ago=old, refused_by="balance", verdict="valid")  # a refusal is no evidence
    assert (await catalog_stats.refresh(session_maker, now=_now()))["caught_up"]
    assert (await _bucket(VERIFY)).verdicts == {"valid": 12, "invalid": 5, "catch_all": 3, "unknown": 2}


async def test_verdict_counts_are_not_published_yet(clients):
    """Stored, not shown: the catalog observation an agent reads carries no verdict counts on
    either read path until the word mappings are proven."""
    for _ in range(stats.MIN_HIT_SAMPLES + 5):
        await _record(VERIFY, 200, 100, ago=timedelta(days=1), hit=True, verdict="valid")
    live = await _live([VERIFY])
    assert (await catalog_stats.refresh(session_maker, now=_now()))["caught_up"]
    folded = await PostgresEndpointObservationReader(session_maker).get_many([VERIFY])
    assert "verdicts" not in live[VERIFY] and "verdicts" not in folded[VERIFY]
    assert folded[VERIFY] == live[VERIFY]
    assert (await _bucket(VERIFY)).verdicts == {"valid": stats.MIN_HIT_SAMPLES + 5}


async def test_a_bucket_folded_before_verdicts_existed_reads_as_empty(clients):
    """`verdicts` is NULL on a bucket the worker wrote before the column; the next fold into it
    starts that day's words from the rows it reads."""
    day = (_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    async with session_maker() as db:
        db.add(EndpointDayStat(endpoint_id=VERIFY, day=day, n=3, ok=3, bad=0, verdicts=None))
        await db.commit()
    await _record(VERIFY, 200, 100, ago=timedelta(days=1), hit=True, verdict="valid")
    assert (await catalog_stats.refresh(session_maker, now=_now()))["caught_up"]
    async with session_maker() as db:
        bucket = await db.get(EndpointDayStat, (VERIFY, day))
    assert bucket.n == 4 and bucket.verdicts == {"valid": 1}
