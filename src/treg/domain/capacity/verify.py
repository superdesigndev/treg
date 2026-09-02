"""Route verification — the weekly re-verify and the one-off body diff (plan §4.3).

A route stays enabled only while a $0.01-class call through the aggregator returns the same SHAPE
as the same call on our own key (`last_verified_at < 7 days`). `shape()` is the structural
fingerprint used by the mapping run (keys + leaf/list markers, values ignored) so PII never
matters to the comparison. The live half takes an httpx client and the keys from the caller;
this module reads no settings itself.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import httpx

from ...timeutil import utcnow_naive
from ...infra.upstream.aggregators import by_name


class VerifyOutcome(str, Enum):
    """Classification of a verification result — determines whether to disable the route.

    PASS  — Both sides 2xx with matching shapes; update last_verified_at.
    FLAKE — Transient vendor/aggregator error (5xx, 429, malformed, network). Don't disable.
    SKIP  — Can't verify (contract, pending, no direct key). Don't count as pass or fail.
    FAIL  — Real shape mismatch at 200/200. Disable the route.
    """
    PASS = "pass"
    FLAKE = "flake"
    SKIP = "skip"
    FAIL = "fail"


def classify_verification(v: "Verification") -> VerifyOutcome:
    """Classify a Verification result into one of the four outcomes.

    This classifier is intentionally conservative: only 200/200 shape mismatches cause FAIL.
    Transient aggregator issues (malformed, 5xx, 429) are FLAKE. Contract misses are SKIP.
    """
    if v.passed:
        return VerifyOutcome.PASS

    if v.relay_status is None:
        if v.note.startswith("relay unreachable:"):
            return VerifyOutcome.FLAKE
        if "malformed" in v.note or "not JSON" in v.note:
            return VerifyOutcome.FLAKE
        if "contract:" in v.note:
            return VerifyOutcome.SKIP
        if "pending" in v.note:
            return VerifyOutcome.SKIP
        return VerifyOutcome.FLAKE

    if v.relay_status >= 500:
        return VerifyOutcome.FLAKE

    if v.relay_status == 429:
        return VerifyOutcome.FLAKE

    if v.direct_status is None:
        if "direct not attempted" in v.note or "direct unreachable:" in v.note:
            return VerifyOutcome.SKIP
        if v.note.startswith("relay ok"):
            return VerifyOutcome.SKIP
        return VerifyOutcome.SKIP

    if v.direct_status >= 500:
        return VerifyOutcome.FLAKE

    if v.direct_status == 429:
        return VerifyOutcome.FLAKE

    if 200 <= v.direct_status < 300 and 200 <= v.relay_status < 300:
        if v.same_shape is False:
            return VerifyOutcome.FAIL
        if v.same_shape is None:
            return VerifyOutcome.FLAKE
        return VerifyOutcome.PASS

    if (400 <= v.direct_status < 500) and (400 <= v.relay_status < 500):
        return VerifyOutcome.FLAKE

    if (200 <= v.direct_status < 300) and (v.relay_status >= 500):
        return VerifyOutcome.FLAKE

    if (200 <= v.relay_status < 300) and (v.direct_status >= 500):
        return VerifyOutcome.FLAKE

    return VerifyOutcome.FAIL


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

    @property
    def passed(self) -> bool:
        return bool(self.same_shape) and self.relay_status is not None and 200 <= self.relay_status < 300

    @property
    def outcome(self) -> VerifyOutcome:
        """The classified outcome of this verification."""
        return classify_verification(self)


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
    return res


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
                            note=f"relay unreachable: {type(exc).__name__}: {exc}")
    now = utcnow_naive()
    if res.failure or res.upstream_status is None:
        return Verification(route.endpoint_id, route.aggregator, None, res.upstream_status, None,
                            res.cost_micro, None, note=f"{res.failure}: {res.detail}")
    if direct is None:
        return Verification(route.endpoint_id, route.aggregator, None, res.upstream_status, None,
                            res.cost_micro, None, note="relay ok, direct not attempted")
    url, dq, dbody = direct
    try:
        dr = await client.request(route.method, url, params=dq or None, content=dbody, headers=direct_headers or {})
    except httpx.RequestError as exc:
        return Verification(route.endpoint_id, route.aggregator, None, res.upstream_status, None,
                            res.cost_micro, None, note=f"direct unreachable: {type(exc).__name__}: {exc}")
    same = shapes_match(dr.content, res.upstream_body) if 200 <= dr.status_code < 300 else None
    return Verification(route.endpoint_id, route.aggregator, dr.status_code, res.upstream_status, same,
                        res.cost_micro, now if same else None,
                        note="" if same else f"direct {dr.status_code}, relay {res.upstream_status}, shape differs")
