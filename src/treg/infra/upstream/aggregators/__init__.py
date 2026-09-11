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

from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class AggregatorRequest:
    method: str
    url: str
    headers: dict[str, str]
    json: dict
    poll_url: str | None = None  # set by parse() on an async run, not by build()


AGGREGATOR_SIDE = ("aggregator_auth", "aggregator_balance", "malformed")
"""The `failure` kinds that blame the aggregator (our key, its account, its host or envelope), never
the route or the vendor: the call path marks the aggregator unhealthy on them, the verifier leaves
the route as it is. `contract` is request-scoped (this route, this request) and never marks the
aggregator; `pending` is nobody's yet."""

VENDOR_DRY = "vendor_dry"
"""The aggregator relayed the vendor's own out-of-credit / quota answer (a 402, Apollo's 422, a
period-cap 429): the AGGREGATOR'S account for THIS vendor is dry. Not the route's fault and not the
whole aggregator's: the call path marks `overflow:<aggregator>:<provider>`, the verifier leaves
the route as it is. Set by `with_vendor_verdict`, the one place a relayed body is read."""


def with_vendor_verdict(res: "AggregatorResult", provider: str) -> "AggregatorResult":
    """Fold the vendor's own capacity dialect into the result, so every consumer sees one
    `failure` instead of re-classifying the relayed body. Keeps `cost_micro`: the aggregator may
    have charged for the relay."""
    from ....domain.capacity import signatures  # pure; the capacity domain imports this package back
    if res.failure is not None or res.upstream_status is None:
        return res
    sig = signatures.classify(provider, res.upstream_status, None, res.upstream_body[:4096])
    if not signatures.is_exhausting(sig):
        return res
    return replace(res, failure=VENDOR_DRY, detail=f"{sig.kind}: {sig.detail[:100]}")


@dataclass(frozen=True)
class AggregatorResult:
    """`failure` is None when the aggregator relayed the vendor's answer (whatever its status).
    Otherwise it names who to blame, which is what decides the next rung:
      aggregator_auth    - our key was rejected → mark the aggregator unhealthy
      aggregator_balance - the aggregator's own account is empty → unhealthy
      contract           - the aggregator's own per-request refusal (its stricter input schema,
                           a validation 4xx with no vendor data) → this route is wrong for this
                           call; no vendor call happened, nothing is charged, nothing is struck
      pending            - async run not finished; poll `poll_url`
      malformed          - not an envelope at all: non-JSON, a 5xx, a transport error
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
