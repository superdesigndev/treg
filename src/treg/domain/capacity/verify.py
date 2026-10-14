"""Route verification — the weekly re-verify and the one-off body diff (plan §4.3).

A route stays enabled only while a $0.01-class call through the aggregator returns the same SHAPE
as the same call on our own key (`last_verified_at < 7 days`). `shape()` is the structural
fingerprint used by the mapping run (keys + leaf/list markers, values ignored) so PII never
matters to the comparison. The live half takes an httpx client and the keys from the caller;
this module reads no settings itself.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

import httpx

from ...timeutil import utcnow_naive
from ...infra.upstream.aggregators import (AGGREGATOR_SIDE, VENDOR_DRY, VENDOR_REFUSAL, by_name,
                                            with_vendor_verdict)
from . import signatures


_SAFE_SHAPE_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
_UUID_SHAPE_KEY = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)


def _safe_shape_key(key) -> str:
    """Keep ordinary schema field names; never persist identifier-shaped map keys."""
    text = str(key)
    if not _SAFE_SHAPE_KEY.fullmatch(text) or _UUID_SHAPE_KEY.fullmatch(text):
        return "<identifier>"
    return text


def shape(obj, depth: int = 0):
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        return {k: shape(v, depth + 1) for k, v in sorted(obj.items())}
    if isinstance(obj, list):
        return [shape(obj[0], depth + 1)] if obj else []
    return "leaf"


def shapes_match(a: bytes, b: bytes) -> bool | None:
    try:
        return json.dumps(shape(json.loads(a)), sort_keys=True) == json.dumps(shape(json.loads(b)), sort_keys=True)
    except ValueError:
        return None


def _shape_paths(value, path: str = "$") -> set[str]:
    """PII-free structural paths for explaining a failed shape comparison."""
    if isinstance(value, dict):
        paths = {f"{path}:object"}
        for key, child in value.items():
            paths |= _shape_paths(child, f"{path}.{_safe_shape_key(key)}")
        return paths
    if isinstance(value, list):
        return {f"{path}:list"} | (_shape_paths(value[0], f"{path}[]") if value else set())
    return {f"{path}:leaf"}


def shape_difference(a: bytes, b: bytes) -> str:
    """Describe only structural differences; never include response values."""
    try:
        direct = _shape_paths(json.loads(a))
        relay = _shape_paths(json.loads(b))
    except ValueError:
        return "non-JSON response"
    direct_only = sorted(direct - relay)[:8]
    relay_only = sorted(relay - direct)[:8]
    if not direct_only and not relay_only:
        return "difference is confined to redacted map keys"
    return f"direct-only={direct_only or '-'}; relay-only={relay_only or '-'}"[:500]


@dataclass
class Verification:
    endpoint_id: str
    aggregator: str
    direct_status: int | None
    relay_status: int | None
    same_shape: bool | None
    cost_micro: int | None
    verified_at: datetime | None
    note: str = ""
    failure: str | None = None   # AggregatorResult.failure, or "unreachable" for a dead host; None = relayed
    direct_dry: bool = False     # the direct leg was OUR account refusing in its recorded dialect

    @property
    def relay_ok(self) -> bool:
        return self.relay_status is not None and 200 <= self.relay_status < 300

    @property
    def direct_ok(self) -> bool:
        return self.direct_status is not None and 200 <= self.direct_status < 300

    @property
    def passed(self) -> bool:
        return bool(self.same_shape) and self.relay_ok


def verdict(v: Verification) -> str:
    """What one verification means for its route (worker.py acts on it, nothing else decides):
      passed       - relay 2xx and the same shape as the direct call → stamp `last_verified_at`
      aggregator   - our key, the aggregator's account (its own refusal, or the vendor's
                     out-of-credit answer relayed through it, VENDOR_DRY, or a vendor-specific
                     authentication or authorization refusal, VENDOR_REFUSAL), its host or envelope
                     (AGGREGATOR_SIDE, unreachable) → the ROUTE is untouched
      failed       - the aggregator relayed and this route is shown wrong: a contract refusal, or
                     a direct 2xx beside a relay non-2xx / a 2xx of a different shape → disable
      inconclusive - nothing shows the route wrong, nothing proves it right: no direct 2xx to
                     compare with (no key, unreachable, 401, our own account dry, a stale
                     test_request that fails both legs), or an async run still pending → untouched.
                     `direct_dry` with a relay 2xx still stamps in the worker: the relay served,
                     the shape cannot be checked for OUR reason, and an unstamped route decays at
                     the next sync exactly while our account is dry.
    Pure over the typed fields; the note is for people."""
    if v.passed:
        return "passed"
    if v.failure in AGGREGATOR_SIDE or v.failure in (VENDOR_DRY, VENDOR_REFUSAL, "unreachable"):
        return "aggregator"
    if v.failure == "contract":
        return "failed"
    if v.failure is not None:  # pending, or a kind this module does not know yet
        return "inconclusive"
    return "failed" if v.direct_ok else "inconclusive"


async def relay_once(client: httpx.AsyncClient, route, key: str, query: dict, body: bytes | None,
                     path_params: dict | None = None, *, max_polls: int = 20, poll_wait_s: float = 1.5):
    """One aggregator run, following an async run to its end. Returns the parsed result."""
    import asyncio
    agg = by_name(route.aggregator)
    req = agg.build(route, key, query, body, path_params)
    r = await client.request(req.method, req.url, headers=req.headers, json=req.json)
    res = agg.parse(r.status_code, r.content)
    polls = 0
    while res.failure == "pending" and res.poll_url and polls < max_polls:
        await asyncio.sleep(poll_wait_s)
        polls += 1
        pr = await client.get(res.poll_url, headers={"Authorization": f"Bearer {key}"})
        res = agg.parse(pr.status_code, pr.content)
    return with_vendor_verdict(res, route.provider)


async def verify_route(client: httpx.AsyncClient, route, *, key: str, direct: tuple[str, dict, bytes | None] | None,
                       test_request: dict, direct_headers: dict | None = None) -> Verification:
    """`direct` = (url, query, body) on our own key, or None to skip the body diff (relay-only)."""
    q = {k: str(v) for k, v in (test_request.get("queryParams") or {}).items()}
    body_doc = test_request.get("body")
    body = json.dumps(body_doc).encode() if body_doc is not None else None
    path_params = test_request.get("pathParams") or {}
    try:
        res = await relay_once(client, route, key, q, body, path_params)
    except httpx.RequestError as exc:  # a dead aggregator host is a failed verification, not a crash
        return Verification(route.endpoint_id, route.aggregator, None, None, None, None, None,
                            note=f"relay unreachable: {type(exc).__name__}: {exc}", failure="unreachable")
    now = utcnow_naive()
    if res.failure or res.upstream_status is None:
        return Verification(route.endpoint_id, route.aggregator, None, res.upstream_status, None,
                            res.cost_micro, None, note=f"{res.failure}: {res.detail}", failure=res.failure or "malformed")
    if direct is None:
        return Verification(route.endpoint_id, route.aggregator, None, res.upstream_status, None,
                            res.cost_micro, None, note="relay ok, direct not attempted")
    url, dq, dbody = direct
    try:
        dr = await client.request(route.method, url, params=dq or None, content=dbody, headers=direct_headers or {})
    except httpx.RequestError as exc:
        return Verification(route.endpoint_id, route.aggregator, None, res.upstream_status, None,
                            res.cost_micro, None, note=f"direct unreachable: {type(exc).__name__}: {exc}")
    if not 200 <= dr.status_code < 300:
        # Our OWN account dry (the state these routes exist for) proves nothing against the route;
        # any other direct failure (401, a bad test_request) is just no comparison.
        ours = signatures.classify(route.provider, dr.status_code, dr.headers, dr.content[:4096])
        dry = signatures.is_exhausting(ours)
        why = "direct dry" if dry else f"direct {dr.status_code}"
        return Verification(route.endpoint_id, route.aggregator, dr.status_code, res.upstream_status, None,
                            res.cost_micro, None, note=f"{why}, relay {res.upstream_status}, no comparison",
                            direct_dry=dry)
    same = shapes_match(dr.content, res.upstream_body)
    return Verification(route.endpoint_id, route.aggregator, dr.status_code, res.upstream_status, same,
                        res.cost_micro, now if same else None,
                        note=("" if same else f"direct {dr.status_code}, relay {res.upstream_status}, "
                              f"shape differs: {shape_difference(dr.content, res.upstream_body)}"))
