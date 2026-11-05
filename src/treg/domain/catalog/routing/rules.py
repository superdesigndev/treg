"""Published route rules: a provider order for one segment of one capability's routed calls.

A rule set is one versioned document an operator publishes; `application/call/route_rules.py`
reads it. A rule names a capability, a `match` over the segment values a call event records in
`input_meta` (plus `input_type`, the identity variant), and an `order` of providers asked first.
It only reorders: every other candidate still follows in the contract's order.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

MAX_BYTES = 32768
MAX_RULES = 200
MAX_RULES_PER_CAPABILITY = 50
_MAX_INT = 2147483647
_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_KEY = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_VALUE_MAX = 100
# Segment keys every capability records (`application/call_events.input_columns`); a contract
# filter declared `analytics: value` adds its own name.
BUILTIN_KEYS = frozenset({"input_type", "tld", "domain_class"})


@dataclass(frozen=True)
class Rule:
    id: str
    capability: str
    match: tuple[tuple[str, str], ...]
    order: tuple[str, ...]


@dataclass(frozen=True)
class RuleSet:
    version: int
    enabled: bool
    rules: tuple[Rule, ...]
    canonical: str


def _object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate field")
        out[key] = value
    return out


def _text(value, *, pattern: re.Pattern | None = None, limit: int = _VALUE_MAX) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= limit and value == value.strip()
            and (pattern is None or bool(pattern.fullmatch(value))))


def parse(raw: str) -> RuleSet:
    """The published envelope `{version, config}`; anything unexpected raises ValueError."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_BYTES:
        raise ValueError("invalid rule set size")
    data = json.loads(raw, object_pairs_hook=_object)
    if not isinstance(data, dict) or set(data) != {"version", "config"}:
        raise ValueError("invalid envelope")
    version = data["version"]
    if type(version) is not int or not 0 < version <= _MAX_INT:
        raise ValueError("invalid version")
    config = data["config"]
    if (not isinstance(config, dict) or set(config) != {"schema_version", "enabled", "rules"}
            or type(config["schema_version"]) is not int or config["schema_version"] != 1
            or type(config["enabled"]) is not bool or not isinstance(config["rules"], list)
            or len(config["rules"]) > MAX_RULES):
        raise ValueError("invalid configuration")
    rules, ids, per_capability = [], set(), {}
    for raw_rule in config["rules"]:
        if not isinstance(raw_rule, dict) or set(raw_rule) - {"id", "capability", "match", "order", "reason"} \
                or not {"id", "capability", "match", "order"} <= set(raw_rule):
            raise ValueError("invalid rule")
        rid, cap, match, order = (raw_rule[k] for k in ("id", "capability", "match", "order"))
        if not _text(rid, pattern=_NAME) or rid in ids:
            raise ValueError("invalid or duplicate rule id")
        if not _text(cap, pattern=_NAME):
            raise ValueError(f"rule {rid}: invalid capability")
        if "reason" in raw_rule and not _text(raw_rule["reason"], limit=500):
            raise ValueError(f"rule {rid}: invalid reason")
        if (not isinstance(match, dict) or not match
                or any(not _text(k, pattern=_KEY) or not _text(v) for k, v in match.items())):
            raise ValueError(f"rule {rid}: match maps segment keys to non-empty strings")
        if (not isinstance(order, list) or not order or len(order) != len(set(order))
                or any(not _text(p, pattern=_NAME) for p in order)):
            raise ValueError(f"rule {rid}: order lists distinct lowercase provider names")
        per_capability[cap] = per_capability.get(cap, 0) + 1
        if per_capability[cap] > MAX_RULES_PER_CAPABILITY:
            raise ValueError(f"capability {cap}: too many rules")
        ids.add(rid)
        rules.append(Rule(rid, cap, tuple(sorted(match.items())), tuple(order)))
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return RuleSet(version, config["enabled"], tuple(rules), canonical)


def check_against(rules: RuleSet, contracts: dict, providers_of) -> None:
    """Every rule names a routed capability, segment keys it records and providers that serve it.
    `providers_of(capability)` is the set of provider names with an endpoint for it."""
    for rule in rules.rules:
        contract = contracts.get(rule.capability)
        if contract is None:
            raise ValueError(f"rule {rule.id}: no contract {rule.capability}")
        keys = BUILTIN_KEYS | {k for k, spec in (contract.filters or {}).items()
                               if (spec or {}).get("analytics") == "value"}
        if unknown := sorted({k for k, _ in rule.match} - keys):
            raise ValueError(f"rule {rule.id}: {rule.capability} records no {', '.join(unknown)}")
        serving = providers_of(rule.capability)
        if missing := [p for p in rule.order if p not in serving]:
            raise ValueError(f"rule {rule.id}: {', '.join(missing)} serve no {rule.capability}")


def match(rules: RuleSet | None, capability: str, segment: dict[str, Any]) -> Rule | None:
    """The rule for this call: the one matching the most keys, then the earliest listed."""
    if rules is None or not rules.enabled:
        return None
    best: Rule | None = None
    for rule in rules.rules:
        if rule.capability != capability or any(segment.get(k) != v for k, v in rule.match):
            continue
        if best is None or len(rule.match) > len(best.match):
            best = rule
    return best
