"""The shadow provider: rules for asking a candidate provider the same question a served call
asked, and for comparing the two answers. Pure; `application.shadow` does the reading and calling.

The candidate is described entirely by the private `shadow_provider` setting, so nothing about it
lives in the public catalog. Per capability it is an adapter in the routing YAML's own shape
(`accepts`, `in`, `in_expr`, `const`, `out`, `miss`) plus a method and path on the candidate's base
URL. Optional `out` keys `count` and `with_email` give the number of rows and the number of rows
carrying an email, so "found" and "found with an email" are reported apart.

    {"base_url": "https://candidate.example", "sample": {"people.search": 0.05},
     "capabilities": {"companies.enrich": {"method": "GET", "path": "/company",
         "accepts": [["domain"]], "in": {"domain": "queryParams.domain"},
         "out": {"name": "data.name", "domain": "data.domain", "industry": "data.industry"},
         "miss": "data == null"}}}

A served call is paired only when its own adapter reads back an identity the candidate accepts, so
a request the candidate cannot express is counted as not mappable and never sent.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from .routing import paths as P
from .routing.contracts import Adapter, adapter_accepts, parse_adapters

# Fields compared by similarity rather than equality: a name written two ways is still one name.
SIMILAR_FIELDS = frozenset({"name", "title", "company_name", "full_name"})
SIMILAR_AT = 0.85
_LEGAL = re.compile(r"\b(inc|incorporated|llc|ltd|limited|gmbh|ag|sa|sas|srl|bv|nv|oy|ab|plc|co|corp"
                    r"|corporation|company|kg|ug|e\.?k|s\.?r\.?o|sp\.? z o\.?o|pty|pvt)\b\.?")


@dataclass(frozen=True)
class Target:
    method: str
    path: str
    adapter: Adapter


@dataclass(frozen=True)
class Spec:
    base_url: str
    targets: dict[str, Target] = field(default_factory=dict)
    sample: dict[str, float] = field(default_factory=dict)
    units_header: str = ""                       # the candidate's billed-units header, if it sends one

    def rate(self, capability: str) -> float:
        return max(0.0, min(1.0, float(self.sample.get(capability, 1.0))))


def parse(raw: str) -> Spec | None:
    """The setting's JSON → a Spec, or None when it is empty or names no capability."""
    if not raw or not raw.strip():
        return None
    doc = json.loads(raw)
    caps = doc.get("capabilities") or {}
    adapters = parse_adapters({"adapters": {cap: {k: v for k, v in t.items() if k not in ("method", "path")}
                                            for cap, t in caps.items()}})
    targets = {cap: Target(str(t.get("method", "GET")).upper(), str(t["path"]), adapters[cap])
               for cap, t in caps.items() if t.get("path")}
    if not targets or not doc.get("base_url"):
        return None
    return Spec(str(doc["base_url"]).rstrip("/"), targets, dict(doc.get("sample") or {}),
                str(doc.get("units_header") or ""))


def request_identity(adapter: Adapter, query: dict, body: Any) -> dict[str, Any]:
    """Read a served request back into the contract's fields through its adapter's plain `in` map.
    Fields set only by expressions cannot be read back and are left out."""
    doc = {"queryParams": query or {}, "body": body if isinstance(body, dict) else {}}
    out = {}
    for name, target in adapter.in_map.items():
        if target.startswith("pathParams."):
            value = (query or {}).get(target.split(".", 1)[1])
        else:
            value = P.get_path(doc, target)
        if isinstance(value, list) and len(value) == 1:
            value = value[0]  # a one-domain list is that domain, not the text "['x.com']"
        if value not in (None, "", []):
            out[name] = value
    return out


def shadow_request(target: Target, identity: dict[str, Any]) -> tuple[dict[str, str], Any] | None:
    """(query, body) for the candidate, or None when it cannot take this identity."""
    variant = adapter_accepts(target.adapter, identity)
    if variant is None:
        return None
    return target.adapter.to_upstream(identity, variant)


def read(adapter: Adapter, body: Any) -> tuple[bool, dict[str, Any]]:
    """(found, contract fields) from one provider answer; a body that breaks the adapter is a miss."""
    try:
        if adapter.is_miss(body):
            return False, {}
        return True, adapter.from_upstream(body)
    except Exception:  # noqa: BLE001 - a malformed answer is a miss, never a crash of the run
        return False, {}


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
    text = _LEGAL.sub(" ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def _domain(value: Any) -> str:
    text = str(value).strip().lower()
    text = re.sub(r"^[a-z]+://", "", text).split("/")[0]
    return text[4:] if text.startswith("www.") else text


def agree(name: str, served: Any, shadow: Any) -> bool | None:
    """True / False when both sides have the field, None when either side is empty."""
    if served in (None, "", [], {}) or shadow in (None, "", [], {}):
        return None
    if isinstance(served, (list, dict)) or isinstance(shadow, (list, dict)):
        return None
    if name in ("domain", "website"):
        return _domain(served) == _domain(shadow)
    a, b = normalize(served), normalize(shadow)
    if name in SIMILAR_FIELDS:
        return bool(a) and SequenceMatcher(None, a, b).ratio() >= SIMILAR_AT
    return a == b


def compare(served: dict[str, Any], shadow: dict[str, Any]) -> dict[str, bool]:
    """Per shared scalar field, whether the two answers agree; fields one side lacks are left out."""
    out = {}
    for name in sorted(set(served) & set(shadow)):
        if name in ("count", "with_email"):
            continue
        verdict = agree(name, served[name], shadow[name])
        if verdict is not None:
            out[name] = verdict
    return out


def sampled(capability: str, call_id: int, spec: Spec, salt: str) -> bool:
    rate = spec.rate(capability)
    if rate >= 1:
        return True
    digest = hashlib.sha256(f"{salt}:{call_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64 < rate


def subject_hash(identity: dict[str, Any], salt: str) -> str:
    text = json.dumps({k: normalize(v) for k, v in sorted(identity.items())}, sort_keys=True)
    return hashlib.sha256(f"{salt}:{text}".encode()).hexdigest()[:32]


def count(fields: dict[str, Any], key: str) -> int | None:
    value = fields.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return len(value) if isinstance(value, list) else None
