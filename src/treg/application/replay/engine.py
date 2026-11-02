"""One record in, one normalized line out, through the production code of this checkout."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from datetime import datetime

import httpx

from ...domain import asynctasks as asynctasks_rules
from ...domain import money as ledger
from ...domain.catalog import results as catalog_results
from ...domain.catalog import store as catalog_store
from ...domain.catalog.stats import Tally
from ...domain.money import settlement as settlement_basis
from ..asynctasks import terminal_judgement
from ..call.resolve import MarketplaceCall, QueryValues, _spool_evidence_paths, price_call
from ..call.route import child_outcome
from ..call.service import submission_state
from ..call.settle import _observed_cost_micro, decide_settle
from . import records

GROUPS = ("reserve", "settle", "outcome", "adapter", "routing", "stats", "async")
_COUNTERS = ("n", "ok", "bad", "hits", "hit_decided", "paid_hits", "free_misses")
_FOLDED_AT = datetime(2000, 1, 1)


def _digest(value: object) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _failure(exc: Exception) -> dict:
    """The exception's type and typed kind, never its message: a message may quote the body."""
    kind = getattr(exc, "kind", "")
    return {"error": f"{type(exc).__name__}:{kind}" if isinstance(kind, str) and kind
            else type(exc).__name__}


def _guarded(step: Callable[[], dict | None]) -> dict | None:
    try:
        return step()
    except Exception as exc:  # noqa: BLE001 - one step's failure is that step's output
        return _failure(exc)


def _json(raw: bytes) -> object:
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def platform_call(ep: dict, query: QueryValues, body: bytes, consumed: set[str], *,
                  routed_child: bool) -> MarketplaceCall:
    """The MarketplaceCall the resolver builds for treg's own key, with a hold of the estimate."""
    pricing = price_call(ep, query, body, consumed)
    cost = catalog_store.load().cost_view(ep.get("cost"), ep["provider"]) or {}
    return MarketplaceCall(
        tool=None, upstream="", consumed=consumed, endpoint_id=ep["id"], provider=ep["provider"],
        tier="platform", cost_type=str(cost.get("type") or "per_call"),
        estimate_micro=pricing.estimate_micro, unit_micro=pricing.unit_micro,
        reported_charge_unit_micro=pricing.reported_charge_unit_micro,
        settlement_basis=pricing.settlement_basis, request_data=pricing.request_data,
        async_descriptor=ep.get("async"), resource_ownership=ep.get("resource_ownership"),
        managed_resource=ep.get("managed_resource"),
        spooled_evidence=_spool_evidence_paths(ep, ep.get("cost") or {}),
        call_id="replay", payer_org_id=0,
        reserved_micro=ledger.with_margin(pricing.estimate_micro),
        deferred=[] if routed_child else None,
    )


def _reserve(mk: MarketplaceCall) -> dict:
    basis = mk.settlement_basis
    return {
        "estimate_micro": mk.estimate_micro, "hold_micro": mk.reserved_micro,
        "unit_micro": mk.unit_micro, "reported_charge_unit_micro": mk.reported_charge_unit_micro,
        "basis_when": basis.get("when"), "basis_kind": (basis.get("amount") or {}).get("kind"),
        "basis_reserve_micro": basis.get("reserve_micro"),
        "basis_fallback_micro": basis.get("fallback_micro"), "basis_digest": _digest(basis),
    }


def _settle(mk: MarketplaceCall, status: int, raw: bytes, headers: httpx.Headers) -> dict:
    deferred, rejected = submission_state(mk, status, raw)
    if deferred:
        # An accepted submission is a task ticket, not an answer: it settles, is judged and is
        # routed on its terminal outcome, which an `async_terminal` record carries.
        return {"deferred": True}
    if rejected:
        # The call site's arguments for a 2xx that is not an accepted submission.
        reported = _observed_cost_micro(mk, raw, headers)
        decision = decide_settle(mk, status, raw, observed_override=reported or 0, reason=rejected)
    else:
        decision = decide_settle(mk, status, raw, headers=headers,
                                 reason=f"provider_failed_{status}" if status >= 500 else "")
    return {"deferred": False, "billable": decision.billable, "reason": decision.reason,
            "observed_micro": decision.observed_micro, "actual_micro": decision.actual_micro,
            "charged_micro": decision.charged_micro}


def _outcome(endpoint_id: str, status: int, raw: bytes) -> dict:
    result = catalog_results.classify(endpoint_id, status, raw)
    return {"state": result.state, "reason": result.reason, "hit": result.hit,
            "verdict": catalog_results.verdict(endpoint_id, status, raw)}


def _adapter(ep: dict, status: int, raw: bytes) -> dict | None:
    cat = catalog_store.load()
    adapter = cat.adapters.get(ep["id"])
    if adapter is None or not 200 <= status < 300:
        return None
    doc = _json(raw)
    if doc is None:
        return {"json": False}
    core = adapter.from_upstream(doc)
    contract = cat.contracts.get(ep.get("capability") or "")
    required = contract.required_output if contract is not None else ()
    return {"json": True, "miss": bool(adapter.is_miss(doc)), "core_digest": _digest(core),
            "empty_required": sorted(k for k in required if core.get(k) in (None, "", [], {}))}


def _routing(ep: dict, status: int, raw: bytes, *, async_failure: bool = False) -> dict | None:
    cat = catalog_store.load()
    adapter = cat.adapters.get(ep["id"])
    contract = cat.contracts.get(ep.get("capability") or "")
    if adapter is None or not adapter.route or not adapter.verified or contract is None:
        return None
    if async_failure:
        return {"kind": "miss", "why": ["async_failure"]}
    outcome = child_outcome(ep, contract, adapter, status, raw, tier="platform")
    return {"kind": outcome.kind, "why": list(outcome.why), "rows": outcome.rows}


def _stats(endpoint_id: str, status: int, settle: dict | None, outcome: dict | None) -> dict:
    tally = Tally()
    counted = tally.fold(
        status_code=status, created_at=_FOLDED_AT, duration_ms=None,
        hit=(outcome or {}).get("hit"), cost_observed_micro=(settle or {}).get("observed_micro"),
        refused_by=None, verdict=(outcome or {}).get("verdict"),
        empty_fails=catalog_store.empty_is_failure(endpoint_id))
    return {"counted": counted, "moved": [c for c in _COUNTERS if getattr(tally, c)]}


def _async_terminal(ep: dict, mk: MarketplaceCall | None, record: dict, status: int,
                    raw: bytes) -> dict:
    recorded = record.get("async") or {}
    descriptor = recorded.get("descriptor") or ep.get("async") or {}
    basis = recorded.get("settlement_basis") or (mk.settlement_basis if mk else None)
    document = _json(raw)
    outcome = asynctasks_rules.classify_terminal(descriptor, document)
    settled = None
    if outcome in ("success", "billed_failure") and basis:
        settled = settlement_basis.settle(basis, {"terminal": document})
    elif outcome == "failure":
        settled = 0
    hit, word = terminal_judgement(ep["id"], outcome, status, raw)
    return {"outcome": outcome, "settled_micro": settled,
            "charged_micro": None if settled is None else ledger.with_margin(settled),
            "hit": hit, "verdict": word}


def replay(record: dict) -> dict:
    """Replay one corpus record. Every step that cannot apply is None; a step that raises
    reports the exception's type in place of its fields."""
    cat = catalog_store.load()
    endpoint_id = record["endpoint_id"]
    ep = cat.by_id.get(endpoint_id)
    line: dict = {"rid": record["rid"], "endpoint_id": endpoint_id,
                  "provider": ep["provider"] if ep else None, "class": record.get("class")}
    line.update(dict.fromkeys(GROUPS))
    if ep is None:
        line["error"] = "endpoint_not_in_catalog"
        return line
    request = record.get("request") or {}
    response = record.get("response") or {}
    query = QueryValues(tuple((str(k), str(v)) for k, v in request.get("query_items") or ()))
    body = records.decode(request.get("body_b64"))
    consumed = set(request.get("path_params") or {})
    status = int(response["status"])
    raw = records.decode(response.get("body_b64"))
    headers = httpx.Headers(response.get("headers") or {})
    routed_child = bool((record.get("recorded") or {}).get("routed_child"))

    mk: MarketplaceCall | None = None
    try:
        mk = platform_call(ep, query, body, consumed, routed_child=routed_child)
        line["reserve"] = _reserve(mk)
    except Exception as exc:  # noqa: BLE001 - a refused reserve is this step's output
        line["reserve"] = _failure(exc)

    if record.get("class") == "async_terminal":
        line["async"] = _guarded(lambda: _async_terminal(ep, mk, record, status, raw))
        failed = (line["async"] or {}).get("outcome") == "failure"
        line["adapter"] = _guarded(lambda: _adapter(ep, status, raw))
        line["routing"] = _guarded(lambda: _routing(ep, status, raw, async_failure=failed))
        return line

    if mk is not None:
        line["settle"] = _guarded(lambda: _settle(mk, status, raw, headers))
    if not (line["settle"] or {}).get("deferred"):
        line["outcome"] = _guarded(lambda: _outcome(endpoint_id, status, raw))
        line["adapter"] = _guarded(lambda: _adapter(ep, status, raw))
        line["routing"] = _guarded(lambda: _routing(ep, status, raw))
    settle = line["settle"] if isinstance(line["settle"], dict) and "error" not in line["settle"] else None
    outcome = line["outcome"] if isinstance(line["outcome"], dict) and "error" not in line["outcome"] else None
    line["stats"] = _guarded(lambda: _stats(endpoint_id, status, settle, outcome))
    return line


def fingerprints() -> dict[str, str]:
    """A digest of every endpoint's effective catalog entry in this checkout: the loaded row, its
    priced cost view, and its adapter and contract. Two checkouts that differ here touched it."""
    cat = catalog_store.load()
    out = {}
    for endpoint_id, ep in sorted(cat.by_id.items()):
        adapter = cat.adapters.get(endpoint_id)
        contract = cat.contracts.get(ep.get("capability") or "")
        out[endpoint_id] = _digest({
            "endpoint": ep, "cost": cat.cost_view(ep.get("cost"), ep.get("provider")),
            "adapter": dataclasses.asdict(adapter) if adapter is not None else None,
            "contract": dataclasses.asdict(contract) if contract is not None else None,
        })
    return out
