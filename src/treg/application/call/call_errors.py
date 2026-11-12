"""One `call_error` log line per failed call: who refused it, and why, in one vocabulary.

The class is read from the most specific evidence the call has: the failure kind treg raised (its
class in `types.FAILURE_KINDS`, beside its blame), else treg's refusal mechanism (`refused_by`),
else what the provider's own documented error says (`domain.capacity.signatures`), else its status.
A provider account error is the caller's or treg's by who holds that account: an access error
(401/403) belongs to whoever owns the credential, the caller on a credential or tool tier; a credit
error (402, an out-of-credit answer) to whoever pays the provider, treg on its own key and on its
pay-per-use OAuth app. `caller_side` marks the classes a default error view leaves out; they are
still logged.
"""

from __future__ import annotations

import functools
import json
from typing import Mapping
from urllib.parse import urlsplit

from ... import oauth_providers
from ...domain.capacity import signatures as capacity_signatures
from ...domain.catalog import store as catalog_store
from ...infra.structured_logging import get_logger
from ..call_events import ref_parts
from .evidence import _decompress_for_scan
from .types import FAILURE_KINDS

log = get_logger("treg.call_errors")

EVENT = "call_error"
CALLER_TIERS = frozenset({"credential", "tool"})
_CREDIT = "provider_credit"
_ACCESS = "provider_access"

_CLASS_OF_REFUSAL = {
    "balance": "caller_balance_or_cap", "cap": "caller_balance_or_cap", "request": "caller_input",
    "capacity": "treg_capacity", "resolution": "not_found", "retired": "treg_policy",
    "policy": "caller_policy", "paused": "treg_policy", "auth": "caller_auth",
}

_CLASS_OF_SIGNAL = {
    "balance": _CREDIT, "quota": _CREDIT, "unrecorded": _CREDIT, "burst": "rate_limit",
    "unknown": "rate_limit", "edge_block": "upstream_error",
}

_CLASS_OF_STATUS = {
    400: "caller_input", 405: "caller_input", 409: "caller_input", 413: "caller_input",
    415: "caller_input", 422: "caller_input", 401: _ACCESS, 402: _CREDIT, 403: _ACCESS,
    404: "not_found", 410: "not_found", 408: "timeout", 504: "timeout", 429: "rate_limit",
}

CALLER_SIDE = frozenset({"caller_balance_or_cap", "caller_policy", "caller_provider_account"})
_UPSTREAM_ANSWERS = frozenset({"route_caller_fault", "route_not_found"})
_LOCATIONS = {"query": "queryParams", "path": "pathParams"}
_FLAT = ("pathParams", "queryParams")


def classify(status: int, *, tier: str | None, refused_by: str | None = None,
             kind: str | None = None, signal: str | None = None, answered: bool = True,
             billed_oauth: bool = False) -> tuple[str, str]:
    """(`by`, `error_class`): `by` is `upstream` when the provider answered the call or failed to
    (a timeout, a dropped connection), else `treg`."""
    blame, cls = FAILURE_KINDS.get(kind or "", (None, None))
    if kind == "route_no_candidate" and status >= 500:
        blame, cls = "treg", "treg_capacity"
    if kind is not None:
        by = "upstream" if blame == "upstream" or kind in _UPSTREAM_ANSWERS else "treg"
    else:
        by = "upstream" if refused_by is None and answered else "treg"
    cls = cls or _CLASS_OF_REFUSAL.get(refused_by or "")
    if cls is None and by == "upstream":
        cls = _CLASS_OF_SIGNAL.get(signal or "") or _CLASS_OF_STATUS.get(status) \
            or ("upstream_error" if status >= 500 else None)
    if cls is None:
        cls = "treg_error" if status >= 500 else "other"
    if cls in (_CREDIT, _ACCESS):
        owned = tier in CALLER_TIERS and not (cls == _CREDIT and billed_oauth)
        cls = "caller_provider_account" if owned else "our_provider_account"
    return by, cls


def _scopes(spec: object) -> list[Mapping]:
    """Where a declared field's own fields are declared: its `properties`, or its items'."""
    if not isinstance(spec, Mapping):
        return []
    items = spec.get("items")
    found = (spec.get("properties"), items.get("properties") if isinstance(items, Mapping) else None)
    return [scope for scope in found if isinstance(scope, Mapping)]


_INDEX_DIGITS = 4


def _is_array(spec: object) -> bool:
    if not isinstance(spec, Mapping):
        return False
    kind = spec.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    return "items" in spec or any(k in ("array", "list") for k in kinds)


def _parameter(endpoint_id: str | None, name: str | None, rule: str | None = None) -> str | None:
    """The parameter a refusal names when every part of it is declared by the endpoint (a short
    index only into a declared array); `undeclared` otherwise, since a name the caller made up is
    request content and never logged. A JSON key may itself hold dots, so a refusal of an
    undeclared field is never read as a path. A path placeholder may be declared with the query
    parameters."""
    if not name:
        return None
    if rule == "undeclared":
        return "undeclared"
    inputs = (catalog_store.load().by_id.get(endpoint_id or "") or {}).get("input") or {}
    head, _, rest = name.partition(".")
    location = _LOCATIONS.get(head, head)
    if location in _FLAT:
        flat = {key for where in _FLAT if isinstance(inputs.get(where), Mapping)
                for key in inputs[where]}
        return name if not rest or rest in flat else "undeclared"
    declared = inputs.get(location)
    if not isinstance(declared, Mapping):
        return name if any(isinstance(d, Mapping) and name in d for d in inputs.values()) \
            else "undeclared"
    scopes = [declared, *_scopes(declared)]
    spec: object = None
    for part in rest.split(".") if rest else ():
        if part.isdigit():
            if len(part) > _INDEX_DIGITS or not _is_array(spec):
                return "undeclared"
            spec = spec.get("items")
            continue
        spec = next((scope[part] for scope in scopes if part in scope), None)
        if spec is None:
            return "undeclared"
        scopes = _scopes(spec)
    return name


@functools.cache
def _services_by_host() -> dict[str, str]:
    hosts: dict[str, str] = {}
    for service, provider in oauth_providers.REGISTRY.items():
        if provider.base_url:
            hosts.setdefault(urlsplit(provider.base_url).netloc.lower(), service)
    return hosts


def emit(status: int, *, org_id: int | None, call_ref: str | None, road: str,
         endpoint_id: str | None = None, provider: str | None = None, tier: str | None = None,
         refused_by: str | None = None, failure: object = None, kind: str | None = None,
         billed_oauth: bool = False, answered: bool = True, body: bytes | None = None,
         headers=None, host: str | None = None) -> None:
    """Log one line for a call that failed (status 400 or above); never raises.

    `failure` is the refusal treg raised: its `kind`, and the `parameter` and `rule` it names, are
    read; `kind` stands in when there is none. `body` and `headers` are the provider's error answer,
    read only for its documented error code (`domain.capacity.signatures`), decoded by its
    `Content-Encoding`; no part of them is logged. An own tool's `host` names the provider whose
    codes are read when its base URL is on that host, the generic ones alone otherwise; the line's
    `provider` stays unset."""
    if status < 400:
        return
    try:
        catalog = catalog_store.load().by_id
        endpoint_id = endpoint_id if endpoint_id in catalog else None
        kind = getattr(failure, "kind", None) or kind
        signal = None
        if refused_by is None and kind is None and body is not None:
            encoding = next((v.decode("latin-1") for k, v in headers or ()
                             if k.lower() == b"content-encoding"), "")
            plain = _decompress_for_scan(body, encoding) or b""
            reader = provider or _services_by_host().get((host or "").lower(), "")
            found = capacity_signatures.classify(reader, status, headers, plain[:4096])
            signal = found.kind if found is not None else None
        by, cls = classify(status, tier=tier, refused_by=refused_by, kind=kind, signal=signal,
                           answered=answered, billed_oauth=billed_oauth)
        log.info("%s %s", EVENT, json.dumps({
            "org_id": org_id, "call_ref": call_ref, "step": ref_parts(call_ref)[1], "road": road,
            "endpoint_id": endpoint_id, "provider": provider, "tier": tier, "status": status,
            "by": by, "refused_by": refused_by, "error_class": cls, "caller_side": cls in CALLER_SIDE,
            "kind": kind, "signal": signal,
            "parameter": _parameter(endpoint_id, getattr(failure, "parameter", None),
                                    getattr(failure, "rule", None)),
            "rule": getattr(failure, "rule", None),
        }, separators=(",", ":")))
    except Exception:  # noqa: BLE001 - a log line must never surface into a call
        pass
