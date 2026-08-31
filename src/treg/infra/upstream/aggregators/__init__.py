"""Aggregator envelopes — the transport adapters for overflow (refactor plan §0.3 amendment).

An aggregator resells a vendor's API on one balance: the SAME vendor request goes in wrapped in
the aggregator's envelope, and the vendor's body comes back out of it. These modules do exactly
the wrap and the unwrap — nothing else — so faithfulness at the caller boundary is preserved and
verifiable against recorded fixtures (`tests/fixtures/aggregators/`).

    build(route, key, query, body) -> AggregatorRequest      (one POST to the aggregator)
    parse(status, body)            -> AggregatorResult        (vendor status + body + real cost)

The key is passed in by the caller from settings (`overflow_key_<name>`); it is never read here,
never logged, never part of a result. No DB, no framework imports (import-linter contract on
`treg.infra.upstream`).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AggregatorRequest:
    method: str
    url: str
    headers: dict[str, str]
    json: dict
    poll_url: str | None = None  # set by parse() on an async run, not by build()


@dataclass(frozen=True)
class AggregatorResult:
    """`failure` is None when the aggregator relayed the vendor's answer (whatever its status).
    Otherwise it names who to blame, which is what decides the next rung:
      aggregator_auth    — our key was rejected → mark the aggregator unhealthy
      aggregator_balance — the aggregator's own account is empty → unhealthy
      contract           — the aggregator's stricter input schema refused the request → this
                           route is wrong for this call; no vendor call happened
      pending            — async run not finished; poll `poll_url`
      malformed          — not the envelope we know
    `cost_micro` is the aggregator's in-band charge for this call (0 on a miss), the number the
    caller pays. None when the envelope carried no price."""
    upstream_status: int | None
    upstream_body: bytes
    cost_micro: int | None
    failure: str | None = None
    detail: str = ""
    poll_url: str | None = None
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.failure is None and self.upstream_status is not None and 200 <= self.upstream_status < 300


def by_name(name: str):
    from . import monid, orthogonal
    return {"orthogonal": orthogonal, "monid": monid}[name]
