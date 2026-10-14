"""What the calls we have already served say about each catalog endpoint.

The catalog's own numbers are *claims*: a price read off a rate card, a `verified:` stamp from the
day someone ran it. This module answers the other question — **does it still work, how fast, and
does it charge what it said?** — from `CallRecord`, which has recorded `endpoint_id`, `status_code`,
`duration_ms` and `cost_observed_micro` since the marketplace shipped. Nothing new is collected here;
it was always being written and never read.

This is the half of "compare providers" that only treg can do. Anyone can read a rate card; only the
party that sees every call, across every tenant, can say which of nine email-lookup providers
answered 400 times without failing. It is what makes an agent's choice factual instead of a guess —
see `docs/CAPABILITY-CHOICE-PLAN.md`.

**Aggregate only, and never below a floor.** Rows are pooled across every org, so the output must
carry nothing that could identify a caller: counts, rates and percentiles only — never who, never
when-exactly, never a params_hash. And an endpoint with fewer than `MIN_SAMPLES` calls reports
`samples` and nothing else: with two calls behind it, a "100% success" number is noise dressed as
evidence, and on a quiet endpoint it could also be one org's activity made visible.

This remains the authoritative read-only calculation. Catalog views reach it through the
`EndpointObservationReader` port; the hosted adapter keeps a bounded-staleness process cache so a
search burst cannot multiply this query by request concurrency. Percentiles are computed in Python
because `percentile_cont` is not portable to SQLite (the same tradeoff `reconcile.py` documents for
its JSON provenance).
"""

from __future__ import annotations

import math
import random
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Protocol, TypeAlias

from sqlalchemy import case, func, literal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import CallRecord

WINDOW_DAYS = 30
MIN_SAMPLES = 5          # below this we publish the count and nothing else (see module docstring)
_MAX_ROWS = 20_000       # bound the latency fetch; percentiles do not get truer past this
# Successful durations kept per endpoint per day in the folded read model (`EndpointDayStat`).
# Thirty days of these is 12,000 values, the same order as `_MAX_ROWS`; past that a percentile
# does not get truer, it only gets dearer to store and read.
LATENCY_SAMPLE = 400

EndpointObservation: TypeAlias = dict[str, int | float | None]
ObservationSnapshot: TypeAlias = dict[str, EndpointObservation]


@dataclass
class Tally:
    """The evidence about one endpoint, before any judgement is applied to it.

    One `Tally` is one UTC day of one endpoint in the folded read model, or the whole window when
    the days are merged, or the whole window straight from `callrecord` in `observed()`. All three
    reach the same numbers through `publish`, so the rules about what counts (a 4xx is the
    caller's fault, a 405 is the catalog's, a refusal is nobody's evidence) live in exactly one
    place each: the SQL in `observed` and the Python in `fold`, and the tests hold them equal.
    """

    n: int = 0
    ok: int = 0
    bad: int = 0
    last_ok: datetime | None = None
    hits: int = 0
    hit_decided: int = 0
    paid_hits: int = 0
    free_misses: int = 0
    # Calls per contract verdict word (`CallRecord.verdict`): `{"valid": 31, "catch_all": 4}`.
    # Folded and stored per day only: `publish` leaves it out until the word mappings are proven.
    verdicts: dict[str, int] = field(default_factory=dict)
    # Successful durations. `latency_seen` counts every one folded in; `latencies` keeps at most
    # `LATENCY_SAMPLE` of them, a uniform reservoir (Vitter's algorithm R) so a busy day's
    # percentiles are as honest as a quiet day's exact list. `latency_weights`, parallel to
    # `latencies`, is empty while a tally is one uniform sample and is filled by `merge`: a merged
    # window must weight each sample by the calls it stands for, or a day with ten thousand calls
    # and a day with ten would count the same and the window's p95 would be the quiet day's.
    latency_seen: int = 0
    latencies: list[int] = field(default_factory=list)
    latency_weights: list[float] = field(default_factory=list)

    def _weights(self) -> list[float]:
        if self.latency_weights:
            return list(self.latency_weights)
        if not self.latencies:
            return []
        return [self.latency_seen / len(self.latencies)] * len(self.latencies)

    def fold(self, *, status_code: int, created_at: datetime, duration_ms: int | None,
             hit: bool | None, cost_observed_micro: int | None, refused_by: str | None,
             verdict: str | None = None, rng: random.Random | None = None,
             empty_fails: bool = False) -> bool:
        """Fold one audit row in. Returns False when the row is not evidence about the endpoint.

        Mirrors the predicates in `observed()` exactly; see its docstring for why each one is
        what it is.
        """
        if refused_by is not None:
            return False     # treg said no before a byte went upstream: the caller's account, not the endpoint
        self.n += 1
        # `empty_fails` (the capability's `empty_is_failure`): a 2xx the adapter judged empty is the
        # provider failing the job, counted as bad, never as a success.
        empty = empty_fails and status_code < 300 and hit is False
        success = status_code < 300 and not empty
        if empty:
            self.bad += 1
        if success:
            self.ok += 1
            if self.last_ok is None or created_at > self.last_ok:
                self.last_ok = created_at
        elif status_code >= 500 or status_code == 405:
            self.bad += 1
        if hit is not None:
            self.hit_decided += 1
            if hit:
                self.hits += 1
        elif success and cost_observed_micro is not None:
            if cost_observed_micro > 0:
                self.paid_hits += 1
            elif cost_observed_micro == 0:
                self.free_misses += 1
        if verdict is not None:
            self.verdicts[verdict] = self.verdicts.get(verdict, 0) + 1
        if success and duration_ms is not None:
            self.latency_seen += 1
            ms = int(duration_ms)
            if len(self.latencies) < LATENCY_SAMPLE:
                self.latencies.append(ms)
            else:
                slot = (rng or random).randrange(self.latency_seen)
                if slot < LATENCY_SAMPLE:
                    self.latencies[slot] = ms
        return True

    def merge(self, other: "Tally") -> "Tally":
        """Sum two tallies (e.g. thirty days into a window). Samples concatenate with weights:
        each day's reservoir is uniform over that day, so a sample from a day with `seen` calls
        and `kept` samples stands for `seen / kept` calls, and the window's percentile is taken
        over those weights. A merged tally is never folded into again."""
        weights = self._weights() + other._weights()
        self.n += other.n
        self.ok += other.ok
        self.bad += other.bad
        if other.last_ok is not None and (self.last_ok is None or other.last_ok > self.last_ok):
            self.last_ok = other.last_ok
        self.hits += other.hits
        self.hit_decided += other.hit_decided
        self.paid_hits += other.paid_hits
        self.free_misses += other.free_misses
        for word, count in other.verdicts.items():
            self.verdicts[word] = self.verdicts.get(word, 0) + count
        self.latency_seen += other.latency_seen
        self.latencies = self.latencies + other.latencies
        self.latency_weights = weights
        return self

    def percentile(self, q: float) -> int | None:
        """Nearest-rank percentile of the successful durations, by weight when the samples stand
        for different numbers of calls (a merged window), plain when they are one uniform sample
        (a single day, or the live query's rows)."""
        if not self.latencies:
            return None
        if not self.latency_weights:
            return _pct(sorted(self.latencies), q)
        pairs = sorted(zip(self.latencies, self.latency_weights))
        total = sum(w for _, w in pairs)
        target = q * total - 1e-9      # the same rank rule as `_pct`, over weight instead of count
        acc = 0.0
        for value, weight in pairs:
            acc += weight
            if acc >= target:
                return int(value)
        return int(pairs[-1][0])


def publish(endpoint_ids: Iterable[str], tallies: dict[str, Tally], *,
            per_success: set[str] | None = None, now: datetime | None = None) -> ObservationSnapshot:
    """Turn evidence into what the catalog may say about it: the floors, the rounding, and the
    honest emptiness for an endpoint nobody has called. Every reader path ends here."""
    at = now or _now()
    out: dict[str, dict] = {}
    for ep_id, t in tallies.items():
        hits, hit_decided = t.hits, t.hit_decided
        if ep_id in (per_success or ()):
            hits += t.paid_hits
            hit_decided += t.paid_hits + t.free_misses
        hit_rate = round(hits / hit_decided, 4) if hit_decided >= MIN_HIT_SAMPLES else None
        decided = t.ok + t.bad          # 4xx excluded — the caller's fault, not the provider's
        if decided < MIN_SAMPLES:
            # Honest emptiness: say how thin the evidence is, claim nothing from it. An earlier
            # revision of this fix published `any_ok` here — "has it EVER answered?" — on the
            # argument that a yes/no survives any sample size. It doesn't survive THIS module's
            # own two rules, and it broke both. It leaked outcome (not just volume) about a single
            # tenant's single call on a quiet endpoint, which is what the floor exists to prevent;
            # and because `samples` counts 4xx while `ok` does not, one caller's malformed 422
            # produced `any_ok: false` and made a healthy endpoint look broken to everybody — the
            # exact failure the 4xx rule below is written to stop. "Never worked" is now read off
            # `ok_rate == 0`, which is computed only from DECIDED (2xx vs 5xx) samples above the
            # floor, so it cannot be inferred from caller errors at all. The floor must therefore
            # be tested against `decided`, not total traffic: four 422s plus one 405 previously
            # published the outcome of that ONE decided call as 0%, violating both the evidence
            # and privacy reasons for having the floor.
            out[ep_id] = {"samples": t.n, "decided": decided, "ok_rate": None,
                          "p50_ms": None, "p95_ms": None, "last_ok_days": None,
                          "hit_rate": hit_rate, "hit_samples": hit_decided}
            continue
        enough_latency = len(t.latencies) >= MIN_SAMPLES
        out[ep_id] = {
            "samples": t.n,
            # `decided` is the denominator of ok_rate (2xx + 5xx). Anything aggregating rates
            # across endpoints must weight by this, not by `samples`, which still counts 4xx.
            "decided": decided,
            "ok_rate": round(t.ok / decided, 4) if decided else None,
            # A rate may rest on five decided calls while only one succeeded. Calling that single
            # duration p50 AND p95 dresses one observation up as a distribution, so latency has
            # its own successful-sample floor.
            "p50_ms": t.percentile(0.50) if enough_latency else None,
            "p95_ms": t.percentile(0.95) if enough_latency else None,
            "last_ok_days": (at - t.last_ok).days if t.last_ok else None,
            "hit_rate": hit_rate, "hit_samples": hit_decided,
        }
    for ep_id in endpoint_ids:       # an endpoint nobody has called says so, rather than vanishing
        out.setdefault(ep_id, {"samples": 0, "decided": 0, "ok_rate": None,
                               "p50_ms": None, "p95_ms": None, "last_ok_days": None,
                               "hit_rate": None, "hit_samples": 0})
    return out


def window_days(now: datetime | None = None, *, days: int = WINDOW_DAYS) -> str:
    """The first UTC day (`YYYY-MM-DD`) a folded bucket must have to fall inside the window.

    The live query cuts at an exact instant thirty days back; the folded model is kept per whole
    day, so its window is that instant's day and everything after it — up to one day more of
    evidence, never less."""
    return ((now or _now()) - timedelta(days=days)).strftime("%Y-%m-%d")


def merged(rows: Iterable[tuple[str, Tally]]) -> dict[str, Tally]:
    """Sum per-day tallies into one per endpoint."""
    out: dict[str, Tally] = {}
    for ep_id, day_tally in rows:
        out.setdefault(ep_id, Tally()).merge(day_tally)
    return out


class EndpointObservationReader(Protocol):
    """Narrow read port used by Catalog views.

    The domain defines the aggregate's shape; bootstrap chooses whether it comes straight from
    Postgres, a process cache, or a future shared adapter. Callers never learn the storage choice.
    """

    async def get_many(self, endpoint_ids: Collection[str]) -> ObservationSnapshot: ...

    def pending(self, endpoint_ids: Collection[str]) -> bool:
        """Whether an id `get_many` just left out is still being read (a cold cache), rather than
        unmeasured: a client may ask again shortly instead of showing "no data" for good."""
        ...


def _now() -> datetime:
    # Naive UTC — CallRecord.created_at is TIMESTAMP WITHOUT TIME ZONE (models._now).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _pct(sorted_values: list[int], q: float) -> int | None:
    """Nearest-rank percentile: the smallest value at or above which lie `q` of the samples
    (rank `ceil(q * n)`). Deliberately not interpolated: these are milliseconds off a wire, and a
    reader comparing providers gains nothing from a fractional millisecond. `Tally.percentile`
    applies the same rule over weighted samples, so a merged window and a live aggregate of the
    same rows agree to the millisecond."""
    if not sorted_values:
        return None
    n = len(sorted_values)
    k = max(1, min(n, math.ceil(q * n - 1e-9)))
    return int(sorted_values[k - 1])


MIN_HIT_SAMPLES = 20     # a hit rate below this many decided lookups is published as None


async def observed(
    db: AsyncSession, endpoint_ids: list[str], *, days: int = WINDOW_DAYS,
    per_success: set[str] | None = None,
) -> ObservationSnapshot:
    """Per endpoint id: `{samples, ok_rate, p50_ms, p95_ms, last_ok_days}`.

    Cost drift (what we estimated vs what the provider charged) is deliberately NOT here — it is
    already `reconcile.price_drift`, computed over the same rows for a different audience. Two
    implementations of one number is how they start disagreeing.

    `endpoint_ids` is expected to be small — one endpoint and its capability siblings — so this is
    two bounded queries, not a scan of the audit table.

    A 4xx counts as a **failure of the call**, not of the endpoint: it usually means the caller sent
    the wrong parameters. It is excluded from `ok_rate` entirely rather than counted against the
    provider, because otherwise one agent's bad query would make a healthy endpoint look broken to
    everybody. Only 2xx (success) and 5xx/timeouts (the provider's fault) decide the rate.

    **405 is the exception**, and it is one the rule's own justification demands. "The caller sent
    the wrong parameters" cannot apply to a method the caller was never allowed to pick: a catalog
    call whose method differs from the recorded one is refused with a 400 before it is relayed. So a
    405 coming back from the provider says the recorded method is wrong — a stale catalog contract,
    not a bad query — and it is counted as decided against the endpoint.
    """
    ids = [e for e in dict.fromkeys(endpoint_ids) if e]
    if not ids:
        return {}
    since = _now() - timedelta(days=days)
    from .store import empty_is_failure
    strict = [e for e in ids if empty_is_failure(e)]
    # The same `empty_fails` rule as `Tally.fold`: a 2xx judged empty on a strict capability.
    empty = (CallRecord.endpoint_id.in_(strict) & (CallRecord.status_code < 300)
             & CallRecord.hit.is_(False)) if strict else literal(False)
    good = (CallRecord.status_code < 300) & ~empty

    rows = (await db.execute(
        select(
            CallRecord.endpoint_id,
            func.count().label("n"),
            func.sum(case((good, 1), else_=0)).label("ok"),
            # 5xx, plus the one 4xx the caller cannot possibly have caused: 405. On a CATALOG call
            # the method is not the caller's to choose — `_resolve_marketplace_call` refuses a
            # mismatch with a 400 BEFORE anything is relayed — so a 405 that came back from the
            # provider means the method THIS CATALOG RECORDED was rejected upstream. That is
            # evidence about the catalog being stale, which is the one thing these numbers exist to
            # surface, and lumping it in with "the caller sent bad parameters" is what let seven
            # straight 405s keep reading as `WORKS — (7)` — the exact row the 2026-08-17 report
            # could not interpret.
            func.sum(case(((CallRecord.status_code >= 500) | (CallRecord.status_code == 405) | empty, 1),
                          else_=0)).label("bad"),
            # LAST OK means last SUCCESS. This was `max(created_at)` over every row, success or
            # not — so an endpoint that had been called seven times today and failed every one
            # read "LAST OK: today", which is the opposite of the truth and exactly how a broken
            # row passes for a merely new one.
            func.max(case((good, CallRecord.created_at))).label("last_ok"),
            # HIT RATE: the adapter's verdict (`hit`), plus — for per-success endpoints, which bill
            # only when they found something — the provider's own zero-cost signal on rows written
            # before the column existed. `per_success` says which endpoints the fallback applies to.
            func.sum(case((CallRecord.hit.is_(True), 1), else_=0)).label("hits"),
            func.sum(case((CallRecord.hit.is_not(None), 1), else_=0)).label("hit_decided"),
            func.sum(case(((CallRecord.hit.is_(None)) & (CallRecord.status_code < 300)
                           & (CallRecord.cost_observed_micro > 0), 1), else_=0)).label("paid_hits"),
            func.sum(case(((CallRecord.hit.is_(None)) & (CallRecord.status_code < 300)
                           & (CallRecord.cost_observed_micro == 0), 1), else_=0)).label("free_misses"),
        )
        .where(CallRecord.endpoint_id.in_(ids), CallRecord.created_at >= since,
               # treg's own refusals (paywall 402s, caps, bad requests never relayed) are facts
               # about the CALLER's account, not the endpoint — they must not even count as
               # samples, or a burst of refused calls dresses itself up as evidence.
               CallRecord.refused_by.is_(None))
        .group_by(CallRecord.endpoint_id)
    )).all()

    lat = (await db.execute(
        select(CallRecord.endpoint_id, CallRecord.duration_ms)
        .where(CallRecord.endpoint_id.in_(ids), CallRecord.created_at >= since,
               CallRecord.duration_ms.is_not(None), good)
        .limit(_MAX_ROWS)
    )).all()
    by_id: dict[str, list[int]] = {}
    for ep_id, ms in lat:
        by_id.setdefault(ep_id, []).append(int(ms))

    tallies: dict[str, Tally] = {}
    for ep_id, n, ok, bad, last_ok, hits, hit_decided, paid_hits, free_misses in rows:
        tallies[ep_id] = Tally(
            n=int(n or 0), ok=int(ok or 0), bad=int(bad or 0), last_ok=last_ok,
            hits=int(hits or 0), hit_decided=int(hit_decided or 0),
            paid_hits=int(paid_hits or 0), free_misses=int(free_misses or 0),
            latency_seen=len(by_id.get(ep_id, [])), latencies=by_id.get(ep_id, []),
        )
    return publish(ids, tallies, per_success=per_success)
