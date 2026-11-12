"""One `call_error` log line per failed call: who refused it, and why, in one vocabulary.

The class is read from the most specific evidence the call has: the failure kind treg raised,
else treg's refusal mechanism (`refused_by`), else what the provider's own documented error says
(`domain.capacity.signatures`), else its status. An account error is the caller's own on a
credential or tool tier and treg's on every other tier. `caller_side` marks the classes a default
error view leaves out; they are still logged.
"""

from __future__ import annotations

import json
import re
from typing import Mapping

from ...domain.capacity import signatures as capacity_signatures
from ...domain.catalog import store as catalog_store
from ...infra.structured_logging import get_logger
from ..call_events import ref_parts

log = get_logger("treg.call_errors")

EVENT = "call_error"
CALLER_TIERS = frozenset({"credential", "tool"})
# Resolved by tier: the caller's provider account, or treg's own.
_ACCOUNT = "provider_account"

_CLASS_OF_KIND: dict[str, str] = {kind: cls for cls, kinds in {
    "caller_input": (
        "metadata_invalid", "metadata_pin_mismatch", "idempotency_mismatch", "invalid_target",
        "target_ambiguous", "catalog_parameter_invalid", "method_mismatch", "hub_input_invalid",
        "route_caller_fault", "route_no_candidate", "tag_cardinality_exceeded",
        "idempotency_in_progress"),
    "not_found": ("unknown_endpoint", "target_not_found", "route_not_found"),
    "caller_balance_or_cap": (
        "insufficient_balance", "free_credit_excludes_video", "daily_cap_reached",
        "platform_daily_cap_reached", "trial_allowance_reached", "tag_call_cap_reached",
        "tag_spend_cap_reached", "route_max_cost", "hub_price_unaffordable", "hub_busy",
        "public_demo_rate_limited"),
    "treg_policy": (
        "policy_denied", "admission_denied", "tool_access_denied", "capability_pinned",
        "tag_blocked", "catalog_retired", "provider_paused", "org_endpoint_quota_exceeded",
        "async_resource_not_owned", "provider_resource_not_owned"),
    "treg_capacity": (
        "provider_capacity", "provider_rate_limited", "share_busy", "platform_cap_unavailable",
        "money_admission_timeout", "tag_budget_unavailable", "trial_allowance_unavailable",
        "db_pool"),
    "our_provider_account": (
        "platform_account_retired", "platform_account_split", "platform_account_unavailable"),
    "caller_provider_account": ("credential_missing", "authorization_required", "refresh_failed"),
    "treg_error": (
        "injection_failed", "ssrf_refused", "response_buffer_limit", "catalog_price_invalid",
        "provider_resource_state_failed", "hub_not_runnable", "unexpected_exception"),
    "timeout": ("read_timeout",),
    "upstream_error": ("connect_failed", "stream_interrupted", "route_failed", "hub_run_failed"),
}.items() for kind in kinds}

_CLASS_OF_REFUSAL = {
    "balance": "caller_balance_or_cap", "cap": "caller_balance_or_cap", "request": "caller_input",
    "capacity": "treg_capacity", "resolution": "not_found", "retired": "treg_policy",
    "policy": "treg_policy", "paused": "treg_policy", "auth": "caller_provider_account",
}

_CLASS_OF_SIGNAL = {
    "balance": _ACCOUNT, "quota": _ACCOUNT, "burst": "rate_limit", "unknown": "rate_limit",
    "edge_block": "upstream_error",
}

_CLASS_OF_STATUS = {
    400: "caller_input", 405: "caller_input", 409: "caller_input", 413: "caller_input",
    415: "caller_input", 422: "caller_input", 401: _ACCOUNT, 402: _ACCOUNT, 403: _ACCOUNT,
    404: "not_found", 410: "not_found", 408: "timeout", 504: "timeout", 429: "rate_limit",
}

CALLER_SIDE = frozenset({"caller_balance_or_cap", "caller_provider_account", "treg_policy"})
_RULE = re.compile(r"(items:)*[a-z_]{1,32}")
_UPSTREAM_KINDS = frozenset({"connect_failed", "read_timeout", "stream_interrupted", "route_failed",
                             "hub_run_failed"})


def classify(status: int, *, tier: str | None, refused_by: str | None = None,
             kind: str | None = None, signal: str | None = None,
             answered: bool = True) -> tuple[str, str]:
    """(`by`, `error_class`): `by` is `upstream` when the provider answered the call or failed to
    (a timeout, a dropped connection), else `treg`."""
    by = "upstream" if kind in _UPSTREAM_KINDS or (
        kind is None and refused_by is None and answered) else "treg"
    cls = _CLASS_OF_KIND.get(kind or "") or _CLASS_OF_REFUSAL.get(refused_by or "")
    if cls is None and by == "upstream":
        cls = _CLASS_OF_SIGNAL.get(signal or "") or _CLASS_OF_STATUS.get(status) \
            or ("upstream_error" if status >= 500 else None)
    if cls is None:
        cls = "treg_error" if status >= 500 else "other"
    if cls == _ACCOUNT:
        cls = "caller_provider_account" if tier in CALLER_TIERS else "our_provider_account"
    return by, cls


def _parameter(endpoint_id: str | None, detail: object) -> str | None:
    """The parameter a refusal names, when the endpoint declares it; `undeclared` for a name the
    caller made up, which is request content and never logged."""
    name = detail.get("parameter") if isinstance(detail, Mapping) else None
    if not isinstance(name, str) or not name:
        return None
    ep = catalog_store.load().by_id.get(endpoint_id or "") or {}
    location, _, rest = name.partition(".")
    field = next((p for p in rest.split(".") if p and not p.isdigit()), None)
    declared = (ep.get("input") or {}).get(location)
    if isinstance(declared, Mapping) and (field is None or field in declared):
        return name
    return "undeclared"


def emit(status: int, *, org_id: int | None, call_ref: str | None, road: str,
         endpoint_id: str | None = None, provider: str | None = None, tier: str | None = None,
         refused_by: str | None = None, kind: str | None = None, detail: object = None,
         answered: bool = True, body: bytes | None = None, headers=None) -> None:
    """Log one line for a call that failed (status 400 or above); never raises.

    `body` and `headers` are the provider's error answer, read only for its documented error code
    (`domain.capacity.signatures`); no part of them is logged."""
    if status < 400:
        return
    try:
        catalog = catalog_store.load().by_id
        endpoint_id = endpoint_id if endpoint_id in catalog else None
        signal = None
        if refused_by is None and kind is None and body is not None and provider:
            found = capacity_signatures.classify(provider, status, headers, body[:4096])
            signal = found.kind if found is not None else None
        by, cls = classify(status, tier=tier, refused_by=refused_by, kind=kind, signal=signal,
                           answered=answered)
        rule = detail.get("rule") if isinstance(detail, Mapping) else None
        if not isinstance(rule, str) or not _RULE.fullmatch(rule):
            rule = None
        log.info("%s %s", EVENT, json.dumps({
            "org_id": org_id, "call_ref": call_ref, "step": ref_parts(call_ref)[1], "road": road,
            "endpoint_id": endpoint_id, "provider": provider, "tier": tier, "status": status,
            "by": by, "refused_by": refused_by, "error_class": cls, "caller_side": cls in CALLER_SIDE,
            "kind": kind, "signal": signal, "parameter": _parameter(endpoint_id, detail),
            "rule": rule,
        }, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - a log line must never surface into a call
        pass
