"""Measured answer quality per endpoint, for `X-Treg-Route-Decisive` and `X-Treg-Route-Quality` (route.py).

A verifier is worth asking next in proportion to how often it decides (`decisive_rate`: valid or
invalid, not unknown / catch-all / risky) and how often that decision agrees with the other
verifiers' majority (`agreement`). Expected net correct verdicts per call:

    decisive_rate x (agreement - (1 - agreement)) = decisive_rate x (2 x agreement - 1)

a right verdict +1, a wrong one -1, none 0. The numbers come from production call events scored
outside the request path; this module only reads them. An endpoint with too few comparable
verdicts is not rated and keeps its price order behind the rated ones.

A field-level contract (people.enrich) publishes the same three numbers per core field asked:
`decisive_rate` is the share of core fields an answer returns that two or more other providers can
check, times the hit rate; `agreement` the share of those that agree; `comparable` the fields
compared. Its slices are identity variants (`email`, `linkedin_url`, `domain+full_name`), see
`variant_slice`."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Protocol, TypeVar

MIN_COMPARABLE = 200  # verdicts compared with other verifiers before a rate is trusted
# A file may set a lower bar (`min_comparable`) when its scorer judges every answer against an outside
# reference (a controlled test checked by HLR) rather than against other providers; never below this floor.
MIN_COMPARABLE_FLOOR = 30
SLICES = ("all", "catch_all", "normal")
_VARIANT_SLICE = re.compile(r"^[a-z][a-z_]*(\+[a-z][a-z_]*)*$")


@dataclass(frozen=True)
class VerdictQuality:
    decisive_rate: float
    agreement: float
    comparable: int
    min_comparable: int = MIN_COMPARABLE   # the bar this entry's file set

    @property
    def expected_net(self) -> float:
        return self.decisive_rate * (2 * self.agreement - 1)


Snapshot = dict[str, dict[str, VerdictQuality]]   # slice -> endpoint_id -> quality


class RouteQualityReader(Protocol):
    async def get(self, capability: str) -> Snapshot | None: ...


def parse(doc: Any) -> Snapshot:
    """The routing file the scorer publishes: `{"min_comparable": n, "slices": {slice: {endpoint_id:
    {decisive_rate, agreement, comparable}}}}`. Entries missing a number are left out, never guessed;
    `min_comparable` is optional and clamped to [MIN_COMPARABLE_FLOOR, MIN_COMPARABLE]."""
    out: Snapshot = {}
    try:
        bar = int(doc.get("min_comparable", MIN_COMPARABLE)) if isinstance(doc, dict) else MIN_COMPARABLE
    except (TypeError, ValueError):
        bar = MIN_COMPARABLE
    bar = min(MIN_COMPARABLE, max(MIN_COMPARABLE_FLOOR, bar))
    slices = doc.get("slices") if isinstance(doc, dict) else None
    for name, rows in (slices.items() if isinstance(slices, dict) else ()):
        if not (name in SLICES or _VARIANT_SLICE.match(str(name))) or not isinstance(rows, dict):
            continue
        for endpoint_id, row in rows.items():
            try:
                q = VerdictQuality(float(row["decisive_rate"]), float(row["agreement"]), int(row["comparable"]), bar)
            except (KeyError, TypeError, ValueError):
                continue
            if not (0 <= q.decisive_rate <= 1 and 0 <= q.agreement <= 1 and q.comparable >= 0):
                continue
            out.setdefault(name, {})[str(endpoint_id)] = q
    return out


def variant_slice(variant: tuple[str, ...]) -> str:
    """The slice an identity variant is rated on: its keys, sorted, joined by `+`."""
    return "+".join(sorted(variant))


def rated(snapshot: Snapshot | None, endpoint_id: str, slice_: str) -> VerdictQuality | None:
    """This endpoint's quality on the slice; None when it has too few compared verdicts there."""
    q = (snapshot or {}).get(slice_, {}).get(endpoint_id)
    return q if q is not None and q.comparable >= q.min_comparable else None


T = TypeVar("T")


def by_quality(items: list[T], endpoint_of, snapshot: Snapshot | None, slice_: str) -> list[T]:
    """Endpoints rated on the slice first, best expected net first; then those rated only on all
    calls (a catch-all address is harder than the average one, so the two never compete on one
    number); the unrated last, in their given order."""
    tiers = (slice_, "all") if slice_ != "all" else ("all",)
    ranked: list[tuple[int, float, int, T]] = []
    for i, x in enumerate(items):
        tier = next(((t, q) for t, name in enumerate(tiers) if (q := rated(snapshot, endpoint_of(x), name))), None)
        ranked.append((tier[0], -tier[1].expected_net, i, x) if tier else (len(tiers), 0.0, i, x))
    return [x for *_, x in sorted(ranked, key=lambda r: r[:3])]
