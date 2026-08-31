"""Orthogonal — `POST /run {"api", "path", "query", "body"}` → `{"success", "data", "priceCents", …}`.

Observed 2026-08-26 on ~300 routes: the vendor's request passes through unchanged (query values
must be strings — Orthogonal rejects numbers), the vendor's body comes back verbatim under `data`,
and `priceCents` / `billing.chargedPriceCents` is the real charge (a vendor miss is still billed).
An upstream error is relayed as `success: false` with the vendor's status in `error` and its body
in `data`; Orthogonal's OWN refusals ride `_orthogonal.error` (`orthogonal_endpoint_contract` =
its stricter schema said no, no vendor call, no charge).
"""

from __future__ import annotations

import json
import re

from . import AggregatorRequest, AggregatorResult

BASE = "https://api.orthogonal.com/v1"
NAME = "orthogonal"


def build(route, key: str, query: list[tuple[str, str]] | dict, body: bytes | None,
          path_params: dict | None = None) -> AggregatorRequest:
    path = route.agg_path
    for k, v in (path_params or {}).items():
        path = path.replace("{" + k + "}", str(v))
    payload: dict = {"api": route.agg_slug, "path": path}
    items = list(query.items()) if isinstance(query, dict) else list(query)
    if items:
        payload["query"] = {k: str(v) for k, v in items}  # Orthogonal rejects numbers here
    if body:
        try:
            payload["body"] = json.loads(body)
        except ValueError:
            payload["body"] = body.decode("utf-8", "replace")
    return AggregatorRequest("POST", f"{BASE}/run",
                             {"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                             payload)


def _dump(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def parse(status: int, body: bytes | dict) -> AggregatorResult:
    if isinstance(body, (bytes, bytearray)):
        try:
            doc = json.loads(body or b"{}")
        except ValueError:
            return AggregatorResult(None, b"", None, "malformed", "orthogonal: not JSON")
    else:
        doc = body
    if not isinstance(doc, dict):
        return AggregatorResult(None, b"", None, "malformed", "orthogonal: not an object")
    if status in (401, 403) and "data" not in doc:
        return AggregatorResult(None, b"", None, "aggregator_auth", str(doc.get("error", ""))[:120])
    own = doc.get("_orthogonal") or {}
    cents = doc.get("priceCents")
    if cents is None:
        cents = (doc.get("billing") or {}).get("chargedPriceCents")
    cost = int(round(float(cents) * 10_000)) if cents is not None else None
    if doc.get("success") is True:
        return AggregatorResult(200, _dump(doc.get("data")), cost, None,
                                extra={"request_id": doc.get("requestId")})
    err = str(doc.get("error") or "")
    m = re.search(r"status (\d{3})", err)
    data = doc.get("data")
    upstream_status = int(m.group(1)) if m else (status if data is not None and status >= 400 else None)
    if own.get("error") == "orthogonal_endpoint_contract" and data is None:
        return AggregatorResult(None, b"", 0, "contract", str(own.get("message", ""))[:160])
    if upstream_status is None:
        # No vendor body and no vendor status: Orthogonal itself refused. 402 = ITS balance.
        kind = "aggregator_balance" if status == 402 else "aggregator_auth" if status in (401, 403) else "malformed"
        return AggregatorResult(None, b"", 0 if kind != "malformed" else cost, kind, err[:120])
    # The vendor answered (an error, but ITS error): relay it as data. An upstream 402 through the
    # aggregator means the AGGREGATOR's vendor account is empty — the caller decides that (E).
    return AggregatorResult(upstream_status, _dump(data) if data is not None else b"", cost or 0, None,
                            detail=err[:120])
