"""Contracts and adapters: parsing, identity matching, fixture verification."""

from __future__ import annotations

import re

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths as P


@dataclass(frozen=True)
class Contract:
    capability: str
    summary: str
    identity: tuple[tuple[str, ...], ...]      # variants, each a sorted tuple of key names
    identity_types: dict[str, str]
    derive: dict[str, str]                     # field → expression over the identity
    filters: dict[str, Any]
    output: dict[str, dict]                    # core field → {type, required?, note?}
    miss: str
    idempotent: bool = True
    default_max_cost_usd: float | None = None  # the per-call ceiling when the caller sends none
    # One sentence the router attaches as `_treg.advice` to a HIT whose `output.verified` is not
    # true — a found contact the provider did not confirm deliverable, which an agent should verify
    # before outreach. Empty = no advice. A search contract has no `verified` output, so advice
    # set there attaches to EVERY hit — deliberate for `people.search`, whose rows carry emails
    # nobody vouched for (2026-09-08: 73 of 79 bounces were unverified directory rows). A
    # suggestion only: treg never chains the verify call itself, which would double every hit's
    # price and change what the find bills for.
    advice_unverified: str = ""

    @property
    def required_output(self) -> tuple[str, ...]:
        return tuple(k for k, v in self.output.items() if (v or {}).get("required"))


@dataclass(frozen=True)
class Adapter:
    endpoint_id: str
    accepts: tuple[tuple[str, ...], ...]       # identity variants (sorted key tuples)
    in_map: dict[str, str]                     # contract field → `queryParams.x` | `body.x` | `pathParams.x`
    out_map: dict[str, str]                    # core field → expression over the provider body
    miss: str
    const: dict[str, Any] = field(default_factory=dict)   # fixed provider params (`body.type: work`)
    in_expr: dict[str, str] = field(default_factory=dict) # provider param ← expression over the request (filters)
    body_array: bool = False                   # the provider wants `[body]` (DataForSEO's task list)
    test_identity: dict[str, Any] = field(default_factory=dict)  # what the endpoint's test_request stands for, when `in` cannot read it back
    cost_units: str = ""                      # optional upper-bound chargeable units for a routed request
    verified: bool = False
    verify_note: str = ""
    _filter_keys: tuple[str, ...] = ()        # contract filter names, set at load (always sent)

    def to_upstream(self, identity: dict[str, Any], variant: tuple[str, ...] | None = None) -> tuple[dict[str, str], Any]:
        """(query params, JSON body) for this provider from a canonical request (identity + filters).

        Only the MATCHED variant's identity keys are sent (filters always are): a caller may hand
        treg everything it knows — email, LinkedIn URL, name, domain — and a provider that insists on
        exactly one identifier (tomba: "supply exactly one of email / domain / linkedin") must not
        receive them all. `variant=None` sends every mapped key (the verifier's fixture path)."""
        query: dict[str, str] = {}
        body: dict[str, Any] = {}
        doc = {"queryParams": query, "body": body}
        identity_keys = set(variant) if variant is not None else None
        for field_name, target in self.in_map.items():
            v = identity.get(field_name)
            if v is None:
                continue
            if identity_keys is not None and field_name not in identity_keys and field_name not in (self._filter_keys or ()):
                continue
            if target.startswith("pathParams."):
                # `/v1/email-verifier/{email}`: the call path fills placeholders from the query
                # (`_marketplace_upstream` consumes them), so a path param travels as a query value.
                query[target.split(".", 1)[1]] = str(v)
                continue
            P.set_path(doc, target, v if target.startswith("body.") else str(v))
        # Expressions run AFTER the plain map so they can reshape a mapped value (`list(domain)`).
        for target, expr in (self.in_expr or {}).items():
            v = P.evaluate(expr, identity)
            if v is None:
                continue
            if target.startswith("pathParams."):
                query[target.split(".", 1)[1]] = str(v)
            else:
                P.set_path(doc, target, v if target.startswith("body.") else str(v))
        for target, v in (self.const or {}).items():
            P.set_path(doc, target, v)
        return query, ([body] if self.body_array else body)

    def from_upstream(self, provider_body: Any) -> dict[str, Any]:
        return {k: P.evaluate(expr, provider_body) for k, expr in self.out_map.items()}

    def is_miss(self, provider_body: Any) -> bool:
        return bool(P.evaluate(self.miss, provider_body))


def _variants(raw) -> tuple[tuple[str, ...], ...]:
    out = []
    for v in raw or []:
        keys = tuple(sorted(v.keys() if isinstance(v, dict) else v))
        out.append(keys)
    return tuple(out)


def parse_contracts(doc: dict) -> dict[str, Contract]:
    out = {}
    for cap, c in (doc.get("contracts") or {}).items():
        types = {}
        for v in c.get("identity") or []:
            if isinstance(v, dict):
                types.update({k: str(t) for k, t in v.items()})
        out[cap] = Contract(
            capability=cap, summary=str(c.get("summary") or ""), identity=_variants(c.get("identity")),
            identity_types=types, derive=dict(c.get("derive") or {}),
            filters={k: (v if isinstance(v, dict) else {"type": str(v)}) for k, v in (c.get("filters") or {}).items()},
            output={k: (v if isinstance(v, dict) else {"type": str(v)}) for k, v in (c.get("output") or {}).items()},
            miss=str(c.get("miss") or ""), idempotent=bool(c.get("idempotent", True)),
            default_max_cost_usd=(float(c["default_max_cost_usd"]) if c.get("default_max_cost_usd") is not None else None),
            advice_unverified=str(c.get("advice_unverified") or ""))
    return out


def parse_adapters(doc: dict) -> dict[str, Adapter]:
    out = {}
    for eid, a in (doc.get("adapters") or {}).items():
        out[eid] = Adapter(
            endpoint_id=eid, accepts=_variants(a.get("accepts")), in_map=dict(a.get("in") or {}),
            in_expr=dict(a.get("in_expr") or {}), body_array=bool(a.get("body_array")),
            test_identity=dict(a.get("test_identity") or {}),
            cost_units=str(a.get("cost_units") or ""),
            const=dict(a.get("const") or {}), out_map=dict(a.get("out") or {}), miss=str(a.get("miss") or ""))
    return out


# ---- identity -------------------------------------------------------------------------------

def canonical_identity(contract: Contract, given: dict[str, Any]) -> tuple[dict[str, Any], tuple[str, ...] | None]:
    """The caller's fields + everything derivable → (identity, the variant they supplied), or
    (identity, None) when no variant is complete. Derived keys count for matching adapters."""
    ident = {k: v for k, v in given.items() if k in contract.identity_types and v not in (None, "")}
    supplied = next((v for v in contract.identity if all(k in ident for k in v)), None)
    if supplied is None:
        return ident, None
    for _ in range(2):  # derive until stable (join needs first+last; split needs full_name)
        for k, expr in contract.derive.items():
            if ident.get(k) in (None, ""):
                v = P.evaluate(expr, ident)
                if v not in (None, ""):
                    ident[k] = v
    for k, spec in (contract.filters or {}).items():  # shared filters ride with the identity
        v = given.get(k)
        ident[k] = v if v not in (None, "") else (spec or {}).get("default")
    return ident, supplied


def adapter_accepts(adapter: Adapter, identity: dict[str, Any]) -> tuple[str, ...] | None:
    """The first accepted variant fully present in the (derived) identity, or None."""
    return next((v for v in adapter.accepts if all(identity.get(k) not in (None, "") for k in v)), None)


# ---- verification ---------------------------------------------------------------------------

_PLACEHOLDER = {"list": ["x"], "int": 1, "url": "https://www.example.com/x"}


def required_inputs(endpoint: dict) -> tuple[str, ...]:
    """Every input the endpoint's own YAML marks `required: true`, as adapter targets
    (`body.roles`, `queryParams.domain`, `pathParams.id`). A param required only under another
    authorization method (`authorization_methods`) is not required of this route."""
    inp = endpoint.get("input") or {}
    method = str(endpoint.get("authorization_method") or "") or next(iter(endpoint.get("authorization_methods") or ()), "")
    out: list[str] = []
    for where in ("pathParams", "queryParams", "body"):
        params = inp.get(where)
        if not isinstance(params, dict):
            continue
        for k, v in params.items():
            if not (isinstance(v, dict) and v.get("required")):
                continue
            if v.get("authorization_methods") and method not in v["authorization_methods"]:
                continue
            out.append(f"{where}.{k}")
    return tuple(out)


def missing_required_inputs(adapter: Adapter, contract: Contract, endpoint: dict, variant: tuple[str, ...]) -> list[str]:
    """The endpoint's required inputs this variant CANNOT produce - through `in`, `in_expr` or
    `const`, with only the variant's keys, the contract's derivations and the filters' defaults
    in hand. Built by running the adapter itself on a placeholder identity, so the answer is the
    request the router would actually send, not a reading of the YAML. `findymail.search.domain`
    once accepted `{company_domain}` alone against a body whose `roles` is required: every routed
    call to it was a vendor 4xx by construction, which the router took for the caller's fault."""
    ident = {k: _PLACEHOLDER.get(contract.identity_types.get(k, "str"), "x") for k in variant}
    ident, _ = canonical_identity(contract, ident)
    # The variant the router would send for THIS caller: the first accepted one the derived identity
    # completes (`plan.py`), which is how `{first_name, last_name, domain}` reaches a `body.name`.
    q, b = adapter.to_upstream(ident, adapter_accepts(adapter, ident) or variant)
    bare_array = isinstance(b, list)
    if bare_array:
        b = b[0] if b and isinstance(b[0], dict) else {}
    doc = {"queryParams": q, "pathParams": q, "body": b}  # path params travel as query values
    missing = []
    for target in required_inputs(endpoint):
        where, name = target.split(".", 1)
        spec = ((endpoint.get("input") or {}).get(where) or {}).get(name) or {}
        if bare_array and where == "body" and str(spec.get("type") or "").startswith("array"):
            # `input: {type: array}` on a bare-array body (brightdata) labels the array itself,
            # not a wrapper key; dataforseo's task fields are read from the first task.
            if not b:
                missing.append(target)
            continue
        if P.get_path(doc, target) in (None, "", [], {}):
            missing.append(target)
    return missing


def verify(adapter: Adapter, contract: Contract, endpoint: dict, example: Any) -> tuple[bool, str]:
    """Fixture round-trip: `in` must reproduce the endpoint's own `test_request` from the contract's
    view of it, every accepted identity variant must be able to fill every `required: true` input
    of the endpoint, and `out` must fill every required core field from the example response (or
    the example must be a recognised miss). Anything else = not a candidate."""
    if not adapter.accepts or not adapter.out_map or not adapter.miss:
        return False, "adapter incomplete"
    for variant in adapter.accepts:
        if not all(k in contract.identity_types for k in variant):
            return False, f"accepts {{{', '.join(variant)}}}, which is not an identity of the contract"
        missing = missing_required_inputs(adapter, contract, endpoint, variant)
        if missing:
            return False, f"in: {{{', '.join(variant)}}} cannot produce required {missing}"
    tr = endpoint.get("test_request") or {}
    # Reconstruct the identity from the test request through the adapter's own `in` map.
    ident: dict[str, Any] = {}
    tr_body = tr.get("body") or {}
    if isinstance(tr_body, list):
        tr_body = tr_body[0] if tr_body and isinstance(tr_body[0], dict) else {}
    doc = {"queryParams": tr.get("queryParams") or {}, "body": tr_body,
           "pathParams": tr.get("pathParams") or {}}
    for field_name, target in adapter.in_map.items():
        v = P.get_path(doc, target)
        if isinstance(v, list) and len(v) == 1 and contract.identity_types.get(field_name, "str") != "list":
            v = v[0]  # `domains: ["stripe.com"]` in the fixture is the scalar `company_domain`
        if v not in (None, ""):
            ident[field_name] = v
    if adapter.test_identity:  # the fixture's identity is stated, because `in` builds a value (a URL) rather than copying one
        ident = dict(adapter.test_identity)
    ident, variant = canonical_identity(contract, ident)
    if variant is None or adapter_accepts(adapter, ident) is None:
        return False, "test_request does not express an accepted identity variant"
    q, b = adapter.to_upstream(ident)
    if isinstance(b, list):
        b = b[0] if b else {}
    for k, v in (tr.get("pathParams") or {}).items():
        if k in {t.split(".", 1)[1] for t in adapter.in_map.values() if t.startswith("pathParams.")} and str(q.get(k)) != str(v):
            return False, f"in: pathParams.{k} → {q.get(k)!r}, test_request has {v!r}"
    for k, v in (tr.get("queryParams") or {}).items():
        if k in {t.split(".", 1)[1] for t in adapter.in_map.values() if t.startswith("queryParams.")} and str(q.get(k)) != str(v):
            return False, f"in: queryParams.{k} → {q.get(k)!r}, test_request has {v!r}"
    for k, v in tr_body.items():
        # `in_expr` targets count only when they are built from the identity (a URL from a handle);
        # filter expressions carry defaults the fixture need not share.
        ident_names = set(contract.identity_types)
        expr_targets = [t for t, e in (adapter.in_expr or {}).items()
                        if any(re.search(rf"\b{re.escape(n)}\b", e) for n in ident_names)]
        mapped = {t.split(".", 1)[1].split(".")[0] for t in list(adapter.in_map.values()) + expr_targets if t.startswith("body.")}
        if k in mapped and b.get(k) != v:
            return False, f"in: body.{k} → {b.get(k)!r}, test_request has {v!r}"
    if example is None:
        return False, "no example_response to verify `out` against"
    if adapter.is_miss(example):
        return True, "example is a miss; out unverified on a hit"
    core = adapter.from_upstream(example)
    missing = [k for k in contract.required_output if core.get(k) in (None, "")]
    if missing:
        return False, f"out: example lacks required {missing}"
    return True, ""


def load_routing(directory: Path, endpoints_by_id: dict[str, dict], read_yaml, read_example) -> tuple[dict[str, Contract], dict[str, Adapter]]:
    """Parse both files and verify every adapter against its endpoint's fixtures."""
    contracts = parse_contracts(read_yaml(directory / "contracts.yaml") or {})
    adapters = parse_adapters(read_yaml(directory / "adapters.yaml") or {})
    verified: dict[str, Adapter] = {}
    for eid, ad in adapters.items():
        ep = endpoints_by_id.get(eid)
        contract = contracts.get((ep or {}).get("capability") or "")
        if contract is not None:
            ad = Adapter(**{**ad.__dict__, "_filter_keys": tuple(contract.filters or ())})
        if ep is None or contract is None:
            verified[eid] = Adapter(**{**ad.__dict__, "verified": False, "verify_note": "unknown endpoint or no contract"})
            continue
        ok, note = verify(ad, contract, ep, read_example(ep))
        verified[eid] = Adapter(**{**ad.__dict__, "verified": ok, "verify_note": note})
    return contracts, verified
