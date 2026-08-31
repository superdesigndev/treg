"""Monid — `POST /v1/run {"provider", "endpoint", "input": {queryParams, body, pathParams}}` →
a run object: `status` (COMPLETED | FAILED | …), `output` (the vendor body), `providerResponse.
httpStatus`, `price` (list) and `billing.reportedCost` (the real charge, micro-dollars).

Some providers answer 202 with a `runId` and finish asynchronously — `GET /v1/runs/{runId}` until
`status` is terminal (observed for tikhub, once for hunter). Monid validates inputs against its
own schema on a few endpoints (HTTP 400 "Invalid input for …" = no vendor call, no charge) and
models some GET params as `body` (hunter /domain-search); the route's `agg_path` is its spelling.
"""

from __future__ import annotations

import json
import re

from . import AggregatorRequest, AggregatorResult

BASE = "https://api.monid.ai/v1"
NAME = "monid"


_INT = re.compile(r"-?\d{1,15}")
_FLOAT = re.compile(r"-?\d+\.\d+")


def _typed(v):
    """Monid validates `input` against the vendor's JSON schema, so a numeric query value must be a
    JSON number and a flag a JSON boolean — but a proxied query string is text. The vendor's own
    GET parser would coerce exactly these shapes, so restoring them is faithful, not a rewrite.
    Live 2026-08-28: akta `limit=1` and hunter `limit=1` were refused as strings."""
    if not isinstance(v, str):
        return v
    if _INT.fullmatch(v):
        return int(v)
    if _FLOAT.fullmatch(v):
        return float(v)
    if v in ("true", "false"):
        return v == "true"
    return v


def build(route, key: str, query: list[tuple[str, str]] | dict, body: bytes | None,
          path_params: dict | None = None, *, params_as_body: bool = False) -> AggregatorRequest:
    items = {k: _typed(v) for k, v in (query.items() if isinstance(query, dict) else query)}
    parsed: dict = {}
    if body:
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"_raw": body.decode("utf-8", "replace")}
    inp = {"queryParams": {} if params_as_body else items,
           "body": (items if params_as_body and not parsed else parsed) or {},
           "pathParams": {k: _typed(v) for k, v in (path_params or {}).items()}}
    return AggregatorRequest("POST", f"{BASE}/run",
                             {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                             {"provider": route.agg_slug, "endpoint": route.agg_path, "input": inp})


def poll_request(run_id: str, key: str) -> AggregatorRequest:
    return AggregatorRequest("GET", f"{BASE}/runs/{run_id}", {"Authorization": f"Bearer {key}"}, {})


def _dump(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _cost_micro(doc: dict) -> int | None:
    rep = (doc.get("billing") or {}).get("reportedCost")
    if isinstance(rep, dict) and rep.get("value") is not None:
        v = float(rep["value"])
        unit = str(rep.get("unit") or "MICRO_DOLLAR").upper()
        return int(round(v if unit == "MICRO_DOLLAR" else v * 1_000_000))
    actual = (doc.get("billing") or {}).get("actualCost")
    if actual is not None:
        return int(round(float(actual) * 1_000_000))
    cost = doc.get("cost")
    if isinstance(cost, dict) and cost.get("value") is not None:
        return int(round(float(cost["value"]) * 1_000_000))
    return None


def parse(status: int, body: bytes | dict) -> AggregatorResult:
    if isinstance(body, (bytes, bytearray)):
        try:
            doc = json.loads(body or b"{}")
        except ValueError:
            return AggregatorResult(None, b"", None, "malformed", "monid: not JSON")
    else:
        doc = body
    if not isinstance(doc, dict):
        return AggregatorResult(None, b"", None, "malformed", "monid: not an object")
    if status in (401, 403):
        return AggregatorResult(None, b"", None, "aggregator_auth", str(doc.get("message", ""))[:120])
    if status == 402:
        return AggregatorResult(None, b"", 0, "aggregator_balance", str(doc.get("message", ""))[:120])
    if status == 400 and "runId" not in doc:
        return AggregatorResult(None, b"", 0, "contract", str(doc.get("message", ""))[:160])
    run_id = doc.get("runId")
    state = str(doc.get("status") or "").upper()
    if state not in ("COMPLETED", "FAILED"):  # a 202 whose body is already terminal is terminal
        if run_id:
            return AggregatorResult(None, b"", None, "pending", state, poll_url=f"{BASE}/runs/{run_id}")
        return AggregatorResult(None, b"", None, "malformed", "monid: no run id")
    upstream_status = (doc.get("providerResponse") or {}).get("httpStatus")
    if upstream_status is None:
        upstream_status = 200 if state == "COMPLETED" else 502
    cost = _cost_micro(doc)
    out = doc.get("output")
    if state == "FAILED" and out is None:
        return AggregatorResult(int(upstream_status), _dump(doc.get("error") or doc.get("message") or {}),
                                cost or 0, None, detail=str(doc.get("message") or "failed")[:120],
                                extra={"run_id": run_id})
    return AggregatorResult(int(upstream_status), _dump(out) if out is not None else b"", cost, None,
                            extra={"run_id": run_id, "result_count": doc.get("resultCount")})
