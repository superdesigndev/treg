"""The generated `treg.<capability>` catalog rows — never hand-written (plan §5)."""

from __future__ import annotations

from .contracts import Adapter, Contract

ROUTED_KIND = "routed"
# Example values for the generated row's `test_request`, by identity key (the call template reads it).
_EXAMPLE_VALUES = {"full_name": "Patrick Collison", "first_name": "Patrick", "last_name": "Collison",
                   "domain": "stripe.com", "linkedin_url": "https://www.linkedin.com/in/patrickcollison"}
ROUTED_PROVIDER = "treg"


def _best_variant(contract: Contract, kids: list[dict], adapters: dict[str, Adapter]) -> tuple[str, ...]:
    """The identity variant the MOST children accept — the example body (and the dry-run) should
    name a shape that several providers can serve, not the contract's first-listed one: people.search
    lists `{q}` first, which only exa takes, so the RUN IT line said "no provider can serve this"."""
    def _n(variant):
        keys = set(variant)
        return sum(1 for e in kids for v in (adapters[e["id"]].accepts or ()) if set(v) == keys)
    return max(contract.identity, key=lambda v: (_n(v), -len(v)))


def routed_endpoint(contract: Contract, children: list[dict], adapters: dict[str, Adapter], cost_view) -> dict | None:
    """One row per capability with ≥ 2 verified-adapter children. Price = the children's range."""
    kids = [e for e in children if adapters.get(e["id"]) and adapters[e["id"]].verified
            and adapters[e["id"]].route and not e.get("status")]
    if len(kids) < 2:
        return None
    prices = sorted(p for p in ((cost_view(e.get("cost"), e["provider"]) or {}).get("usd") for e in kids) if p is not None)
    lo, hi = (prices[0], prices[-1]) if prices else (None, None)
    # A child priced per call or per result bills its provider's answer even when treg judges it a
    # miss and moves on: the caller pays those too. Only per-success and free children are free
    # on a miss.
    billed_miss = sorted(e["id"] for e in kids
                         if ((e.get("cost") or {}).get("type") not in ("per_success", "free", None)))
    max_usd = contract.default_max_cost_usd or 1.00
    body = {}
    variants = " | ".join("{" + ", ".join(v) + "}" for v in contract.identity)
    for variant in contract.identity:
        for k in variant:
            body.setdefault(k, {"type": contract.identity_types.get(k, "str"), "required": False,
                                "note": "identity"})
    for k, spec in (contract.filters or {}).items():
        body.setdefault(k, {"type": (spec or {}).get("type", "str"), "required": False,
                            "note": "; ".join(["filter"]
                                              + ([f"default {spec['default']!r}"] if (spec or {}).get("default") is not None else [])
                                              + ([spec["note"]] if (spec or {}).get("note") else []))})
    cap = contract.capability
    # The section its providers file under, so the platform page shows the row beside theirs.
    domains = [e["domain"] for e in children if e.get("domain")]
    return {
        "id": f"{ROUTED_PROVIDER}.{cap}",
        "provider": ROUTED_PROVIDER,
        "capability": cap,
        "platform": cap.split(".")[0],
        "domain": max(domains, key=domains.count) if domains else "routed",
        "scope": "",
        "kind": ROUTED_KIND,
        "method": "POST",
        "path": f"/{cap}",
        "name": f"{cap} — routed: best of {len(kids)} providers, own keys first",
        "summary": contract.summary,
        "input": {"bodyType": "json", "body": body,
                  "note": ("Send what you know; treg sends each provider only the fields it accepts "
                           f"({variants})"
                           + (f" and fills {', '.join(sorted(contract.derive))} from the rest" if contract.derive else "")
                           + ". On a miss it tries the next provider, cheapest first. Options are "
                           "X-Treg-Route-* headers, never body fields. The answer is {output, raw, _treg}; "
                           "X-Treg-Served-By names the provider. When you sent a domain, _treg.domain_match "
                           "says whether the hit's mailbox domain matches it."
                           + (f" X-Treg-Route-Verify: true also checks a hit with {contract.check.endpoint}, "
                              "at that call's own price, in _treg.verification." if contract.check else "")
                           + (f" X-Treg-Route-Confirm: true counts a hit only once a second provider returns the same "
                              f"{contract.confirm}, each attempt at its own price, in _treg.confirmation."
                              if contract.confirm else "")
                           + (" X-Treg-Route-Decisive: true counts an answer only when its verdict is "
                              + " or ".join(contract.verdict_decisive) + ", asking up to two more providers (best measured "
                              "first, each at its own price), in _treg.decision." if contract.verdict_decisive else ""))},
        "test_request": {"body": {k: _EXAMPLE_VALUES.get(k, "…") for k in _best_variant(contract, kids, adapters)}} if contract.identity else {},
        "cost": {"type": "per_success", "value": lo, "currency": "USD", "per": 1, "unit": "call",
                 "source": "inferred", "confidence": "documented", "checked": None,
                 # No single quote is honest when children bill their misses: a $0 floor reads
                 # as "free". Quote nothing.
                 **({"varies": True} if billed_miss else {}),
                 "note": (("Not free: " if billed_miss else "")
                          + f"${lo:g}–${hi:g} per hit across {len(kids)} providers, 0% markup"
                          + (f". A call also pays each provider tried before the hit that bills a miss "
                             f"({len(billed_miss)} of {len(kids)} do), up to X-Treg-Route-Max-Cost (default "
                             f"${max_usd:g}); X-Treg-Route-Waterfall: 0 pays for one provider only"
                             if billed_miss else "; misses are free")
                          if lo is not None else "children unpriced")},
        "miss_billed_by": billed_miss,
        "cost_range_usd": [lo, hi],
        "tier": "core",
        "verified": None,
        "miss": {"status": 200, "means": f"output.{contract.miss}"},
        "status": "", "status_note": "", "platform_blocked": "", "superseded_by": "",
        "docs_url": "",
        "example_file": None,
        "routed_children": [e["id"] for e in kids],
    }
