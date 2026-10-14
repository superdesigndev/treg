#!/usr/bin/env python3
"""Backfill the `cost` schema's unit + provenance keys across src/treg/catalog/*.yaml.

Run from the repo root: `uv run python scripts/catalog_cost_provenance.py [--check] [service ...]`

Why this is a script and not a one-off hand edit: 2,617 endpoints carry a price, 661 of them wrote
"free" three incompatible ways, and the EXTENDED tier is regenerated wholesale by
scripts/catalog_ingest.py — which would drop every provenance key a human typed in. So the mapping
from "what the repo already knows about a provider's pricing" to "the keys `cost_view` and
`platform_eligible` need" lives here, in one table, and is re-runnable after any re-ingest.

It is IDEMPOTENT and additive: it never invents a figure. Every number it writes is either already
in the file (in `cost.value`, or in prose in `cost.note`) or is the arithmetic of two numbers that
are. Where a provider publishes no figure, the entry is marked `confidence: unknown` and left
without one — a guessed price is worse than an honest gap, because a gap refuses a platform key and
a guess spends real money (docs/context/architecture/catalog.md, Cost).

The three confidence levels it assigns, and what earns them:
  verified   — the price came from the provider's OWN live rate card (TikHub's
               get_all_endpoints_info, DataForSEO's /appendix/user_data) or was observed being
               billed on a real call. Re-checkable without trusting a docs page.
  documented — transcribed from the provider's docs or pricing page during the 2026-07-28 sweep.
  inferred   — the recorded figure is a floor or the top of a published range (base-fee-plus-per-row
               endpoints, "1-9 credits", "$0.50-$5.00 per 1,000"), so it is not the whole charge.

Rewriting preserves each block's existing STYLE (a one-line `cost: {…}` stays one line) and every
comment in the file, because the comments in these files are the curation record.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "src" / "treg" / "catalog"
SWEEP = "2026-07-28"  # the day the pricing sweep read every provider's rate card / pricing page

# The order a cost block reads best in: how much, of what, per what, says who, when, how sure.
KEY_ORDER = ("type", "value", "currency", "per", "unit", "source", "source_url", "checked",
             "confidence", "note")
CANONICAL_FREE = {"type": "free", "value": 0, "currency": "USD", "unit": "call"}

# ---- provider-level provenance ---------------------------------------------------------------
# `url` is the rate card the figures came from — a live rate-card ENDPOINT where the provider
# exposes one (those are the `rate_card_api` rows), else its pricing page. `unit` is what one
# charge covers for each cost type, so `cost_view` can divide by `per` and a reader knows whether
# "0.0015" is per call or per row.
PROVIDERS: dict[str, dict] = {
    # --- prices from the provider's own machine-readable rate card → verified ------------------
    "tikhub": {
        "source": "rate_card_api", "confidence": "verified",
        "url": "https://api.tikhub.io/api/v1/tikhub/user/get_all_endpoints_info",
        "units": {"*": "call"},
    },
    "dataforseo": {
        "source": "rate_card_api", "confidence": "verified",
        "url": "https://api.dataforseo.com/v3/appendix/user_data",
        "units": {"*": "call", "per_result": "page"},
    },
    # Credit counts come from `credits_charged` in the provider's own OpenAPI response examples —
    # first-party, machine-readable and re-fetchable, so the COUNT is verified. (What is merely
    # inferred is the credits→USD rate, and that lives in fx.yaml with its own basis.)
    "scrapecreators": {"source": "rate_card_api", "confidence": "verified",
                       "url": "https://docs.scrapecreators.com/openapi.json", "units": {"*": "call"}},
    # --- prices transcribed from docs / pricing pages → documented ----------------------------
    # Just One API's figures are a hand-exported snapshot of the logged-in dashboard's pricing page
    # (operator-supplied justoneapi_prices.json) — first-party, but a local file nothing can re-check.
    "justoneapi": {"source": "rate_card_api", "confidence": "documented",
                   "url": "https://www.justoneapi.com/", "units": {"*": "call"}},
    "brightdata": {"source": "docs", "confidence": "documented",
                   "url": "https://brightdata.com/pricing", "units": {"*": "record"}},
    "diffbot": {"source": "docs", "confidence": "documented",
                "url": "https://www.diffbot.com/pricing/",
                "units": {"*": "page", "per_result": "record"}},
    "apify": {"source": "docs", "confidence": "documented",
              "url": "https://apify.com/pricing", "units": {"*": "ad"}},
    "akta": {"source": "docs", "confidence": "documented",
             "url": "https://docs.akta.pro/getting-started/pricing", "units": {"*": "call"}},
    "apollo": {"source": "docs", "confidence": "documented",
               "url": "https://www.apollo.io/pricing", "units": {"*": "record"}},
    "coresignal": {"source": "docs", "confidence": "documented",
                   "url": "https://coresignal.com/pricing/", "units": {"*": "record"}},
    "crunchbase": {"source": "docs", "confidence": "documented",
                   "url": "https://data.crunchbase.com/docs/using-the-api", "units": {"*": "call"}},
    "hunter": {"source": "docs", "confidence": "documented",
               "url": "https://hunter.io/pricing", "units": {"*": "call", "per_result": "record"}},
    "leadmagic": {"source": "docs", "confidence": "documented",
                  "url": "https://leadmagic.io/pricing", "units": {"*": "call", "per_result": "record"}},
    "lusha": {"source": "docs", "confidence": "documented",
              "url": "https://www.lusha.com/pricing/", "units": {"*": "call", "per_result": "result"}},
    "moz": {"source": "docs", "confidence": "documented",
            "url": "https://moz.com/api/pricing", "units": {"*": "quota_row"}},
    "pdl": {"source": "docs", "confidence": "documented",
            "url": "https://www.peopledatalabs.com/pricing",
            "units": {"*": "call", "per_result": "record"}},
    "serpapi": {"source": "docs", "confidence": "documented",
                "url": "https://serpapi.com/pricing", "units": {"*": "call"}},
    "thecompaniesapi": {"source": "docs", "confidence": "documented",
                        "url": "https://www.thecompaniesapi.com/pricing",
                        "units": {"*": "call", "per_result": "record"}},
    # --- providers whose prices live in prose only; see PRICES below --------------------------
    "majestic": {"source": "docs", "confidence": "documented",
                 "url": "https://majestic.com/plans-pricing", "units": {"*": "call"}},
    "semrush": {"source": "docs", "confidence": "documented",
                "url": "https://www.semrush.com/api-documentation/", "units": {"*": "call"}},
    "seranking": {"source": "docs", "confidence": "documented",
                  "url": "https://seranking.com/pricing.html", "units": {"*": "call"}},
    "serpstat": {"source": "docs", "confidence": "documented",
                 "url": "https://serpstat.com/pay/", "units": {"*": "call"}},
    "spyfu": {"source": "docs", "confidence": "documented",
              "url": "https://www.spyfu.com/api", "units": {"*": "row"}},
}

# ---- per-endpoint prices lifted out of prose -------------------------------------------------
# Every figure below is already written in that endpoint's own `cost.note` (or its file header);
# this moves it from prose into `value`/`per`/`unit` so the server can price it. Nothing is invented.
#
# `currency: unit` means the provider bills in its own meter and `unit` names it — the rate lives in
# fx.yaml `unit_rates_usd[provider][unit]`, and is null where the provider publishes no dollar price.
_UNIT = "unit"


def _u(value, unit, per=1, **extra):
    return {"value": value, "currency": _UNIT, "unit": unit, "per": per, **extra}


def _cr(value, unit, per=1, **extra):
    return {"value": value, "currency": "credit", "unit": unit, "per": per, **extra}


# Semrush: "N API units per LINE returned" / "per REQUEST" — units are pre-purchased in packages,
# so the unit price in dollars is unpublished and stays null in fx.yaml.
PRICES: dict[str, dict] = {
    "semrush.google.domain.ranked_keywords": _u(10, "api_unit"),
    "semrush.google.domain.paid_keywords": _u(20, "api_unit"),
    "semrush.google.domain.competitors": _u(40, "api_unit"),
    "semrush.google.keywords.volume": _u(10, "api_unit"),
    "semrush.google.keywords.ideas": _u(40, "api_unit"),
    "semrush.google.serp.organic": _u(10, "api_unit"),
    "semrush.web.backlinks.summary": _u(40, "api_unit"),
    "semrush.web.backlinks.list": _u(40, "api_unit"),
    "semrush.web.linking_domains.list": _u(40, "api_unit"),
    "semrush.web.anchors.list": _u(40, "api_unit"),
    "semrush.web.backlinks.competitors": _u(40, "api_unit"),

    # Majestic: three independent meters. A list command charges a flat ANALYSIS fee plus 1
    # RETRIEVAL unit per row; the schema records one price, so it records the dominant flat fee and
    # the note keeps the per-row half. `confidence: inferred` says exactly that: the figure is a
    # floor, not the whole charge.
    "majestic.web.url.metrics": _u(1, "index_item_unit"),
    "majestic.web.backlinks.summary": _u(1, "index_item_unit"),
    "majestic.web.backlinks.list": _u(5000, "analysis_unit", confidence="inferred"),
    "majestic.web.linking_domains.list": _u(1000, "analysis_unit", confidence="inferred"),
    "majestic.web.anchors.list": _u(1000, "analysis_unit", confidence="inferred"),
    "majestic.web.site.top_pages": _u(5000, "analysis_unit", confidence="inferred"),

    # SE Ranking: credits, sold annually at $2,148 per 12M on the entry tier (file header) — that
    # is a real published per-credit price, so it goes in fx.yaml `credit_rates_usd`.
    "seranking.google.keywords.volume": _cr(10, "keyword"),
    "seranking.google.keywords.ideas": _cr(10, "keyword"),
    "seranking.google.domain.overview": _cr(100, "call"),
    "seranking.google.domain.ranked_keywords": _cr(100, "call"),
    "seranking.google.domain.competitors": _cr(100, "call"),
    "seranking.web.backlinks.summary": _cr(100, "target"),
    "seranking.web.backlinks.list": _cr(1, "row"),
    "seranking.web.linking_domains.list": _cr(1, "domain"),
    "seranking.web.anchors.list": _cr(1, "row"),
    "seranking.web.url.metrics": _cr(10, "target"),

    # Serpstat: API credits come with the plan at no published per-credit price → rate null.
    "serpstat.google.domain.overview": _cr(5, "domain"),
    "serpstat.google.domain.ranked_keywords": _cr(1, "keyword"),
    "serpstat.google.domain.competitors": _cr(1, "result"),
    "serpstat.google.keywords.volume": _cr(1, "keyword"),
    "serpstat.google.keywords.ideas": _cr(1, "keyword"),
    "serpstat.google.serp.organic": _cr(1, "row"),
    "serpstat.web.backlinks.summary": _cr(5, "call"),
    "serpstat.web.backlinks.list": _cr(1, "row"),
    "serpstat.web.linking_domains.list": _cr(1, "domain"),
    "serpstat.web.anchors.list": _cr(1, "row"),
    "serpstat.web.site.top_pages": _cr(1, "page"),

    # SpyFu prices in dollars per 1,000 rows (a CPM), which is exactly what `per` is for. Two
    # endpoints sit on a published RANGE rather than a figure; the top of the range is recorded (an
    # over-estimate is safe, an under-estimate spends money we did not reserve) and marked inferred.
    "spyfu.google.domain.overview": {"value": 1.00, "currency": "USD", "per": 1000, "unit": "row",
                                     "confidence": "inferred"},
    "spyfu.google.domain.paid_keywords": {"value": 2.00, "currency": "USD", "per": 1000, "unit": "row"},
    "spyfu.google.domain.ad_history": {"value": 3.00, "currency": "USD", "per": 1000, "unit": "row"},
    "spyfu.google.domain.ranked_keywords": {"value": 5.00, "currency": "USD", "per": 1000,
                                            "unit": "row", "confidence": "inferred"},
    "spyfu.google.domain.competitors": {"value": 0.20, "currency": "USD", "per": 1000, "unit": "row"},
    "spyfu.google.domain.ppc_competitors": {"value": 0.20, "currency": "USD", "per": 1000, "unit": "row"},

    # Moz meters a ROW QUOTA, not money: `quota_rows` is the provider's own meter, so it stops
    # pretending to be dollars. (It was `currency:` absent, which defaulted to USD and served every
    # Moz endpoint as costing $1.00 per row.)
    "moz.web.url.metrics": _u(1, "quota_row"),
    "moz.web.backlinks.summary": _u(2, "quota_row"),
    "moz.web.backlinks.list": _u(1, "quota_row"),
    "moz.web.linking_domains.list": _u(1, "quota_row"),
    "moz.web.anchors.list": _u(1, "quota_row"),
    "moz.web.site.top_pages": _u(1, "quota_row"),
    "moz.web.backlinks.status": _u(2, "quota_row"),
    "moz.web.backlinks.intersect": _u(5, "quota_row"),
    "moz.web.global.top_pages": _u(1, "quota_row"),
    "moz.web.global.top_domains": _u(1, "quota_row"),

    # --- `per` > 1: the provider charges once per N of something ----------------------------
    # `per` > 1 only makes sense against a `type` that counts the same thing: these four billed
    # "per N returned items" while claiming `per_success` (per successful CALL), which would have
    # priced a 50-review response as one charge. The quantity and the trigger have to agree.
    "hunter.companies.emails": {"per": 10, "unit": "record"},        # 1 credit per 10 emails
    "akta.companies.reviews": {"type": "per_result", "per": 50, "unit": "review"},  # 1.5cr / 50
    "akta.x.product-reviews": {"type": "per_result", "per": 50, "unit": "review"},
    "akta.x.jobs": {"type": "per_result", "per": 10, "unit": "item"},      # 4 credits per 10 listings
    "akta.x.social-posts": {"type": "per_result", "per": 10, "unit": "item"},  # 1 credit per 10 posts
    "lusha.people.search": {"per": 25, "unit": "result"},            # 1 credit per up to 25 results
    "lusha.companies.search": {"per": 25, "unit": "result"},
    "leadmagic.x.employee-finder": {"per": 1, "unit": "employee"},
    # 1 credit per 10,000 characters of input — a real figure, it was just never in `value`
    "diffbot.x.nl-process": _cr(1, "character", per=10000),

    # --- named guesses: fixed where the repo has a figure, downgraded where it does not ------
    # An endpoint whose recorded value is a BASE fee with a per-row half on top understates the
    # charge, so it is `inferred`, not `documented` — the note carries the rest.
    "akta.companies.news": {"confidence": "inferred"},               # 0.1 base + 0.01/article
    "apollo.people.enrich": {"confidence": "inferred"},              # "1-9 credits per person"
    "apollo.people.bulk_enrich": {"confidence": "inferred"},
    "leadmagic.x.b2b-ads-search": {"confidence": "inferred"},        # 1 base + 1/ad, 1-51 per call
    "leadmagic.people.search": {"confidence": "inferred"},           # 1/person + email/mobile extras
    "thecompaniesapi.companies.enrich_by_email": {"confidence": "inferred"},  # note says "presumed"
    "coresignal.companies.collect": {"confidence": "inferred"},      # 20 or 10 depending on dataset
    "coresignal.people.enrich": {"confidence": "inferred"},
    "diffbot.companies.enrich": {"confidence": "inferred"},          # 25, or 100 with refresh=true
    "diffbot.people.enrich": {"confidence": "inferred"},
    "apify.meta-ads.library.search": {"confidence": "inferred"},     # "~$5.00 … $3.40-$5.80 by tier"
    "apify.tiktok-ads.library.search": {"confidence": "inferred"},
    # No figure at all. These were carrying a number the note itself calls unconfirmed, or a
    # provider that publishes none. `value: null` + `confidence: unknown` is the honest pair, and
    # it is what refuses the endpoint a platform key.
    "moz.web.index.metadata": {"value": None, "confidence": "unknown"},
}

# Endpoints whose price is genuinely unknown: no figure is published anywhere the repo can cite.
# Listed explicitly rather than inferred from `value: null`, so that a null someone forgot to fill
# in stays a validator error instead of quietly becoming "unknown".
UNKNOWN_PRICE = {
    "akta.companies.enrich",                 # priced per SECTION requested; no single count applies
    "crunchbase.companies.enrich", "crunchbase.companies.search",
    "crunchbase.people.enrich", "crunchbase.people.search",   # licence-gated, no per-call price
    "diffbot.x.crawl-create", "diffbot.x.bulk-create",        # bill at the target API's page rate
    "pdl.x.person-enrich-preview",                            # preview rate is plan-negotiated
    "moz.web.index.metadata",
}

# A note that says the charge was watched happening beats any docs page — and beats the file's own
# UNVERIFIED header, which is about the ROUTE never having been called, not about the price.
OBSERVED_NOTE = re.compile(r"\bOBSERVED live\b", re.I)


# ---- cost-block rewriting --------------------------------------------------------------------
_FLOW = re.compile(r"^(\s*)cost: (\{.*\})\s*$")
# Some ingesters emit one anchored block and alias it from every sibling that shares the price
# (`cost: &id001` … `cost: *id001`). The anchor must survive the rewrite or ~130 aliases dangle;
# the aliases themselves need no rewriting, since they resolve to the block that was rewritten.
_BLOCK = re.compile(r"^(\s*)cost:(?:\s+&(\S+))?\s*$")
_ID = re.compile(r"^\s*-\s+id:\s*(\S+)\s*$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _emit(indent: str, cost: dict, flow: bool, anchor: str | None = None) -> list[str]:
    """The block back as YAML, in the style it was found in. Key order is KEY_ORDER; anything the
    file carried that this script does not know about is kept, at the end, rather than dropped."""
    ordered = {k: cost[k] for k in KEY_ORDER if k in cost}
    ordered |= {k: v for k, v in cost.items() if k not in ordered}
    if flow:
        body = yaml.safe_dump(ordered, default_flow_style=True, sort_keys=False,
                              width=10 ** 9, allow_unicode=True).strip()
        return [f"{indent}cost: {body}"]
    out = [f"{indent}cost:" + (f" &{anchor}" if anchor else "")]
    for key, value in ordered.items():
        # Block style for the key line itself (flow style would wrap it in braces); a note that
        # spans lines comes back out as the block scalar it was written as.
        dumped = yaml.safe_dump({key: value}, default_flow_style=False, sort_keys=False,
                                width=10 ** 9, allow_unicode=True).rstrip("\n")
        out += [f"{indent}  {line}" for line in dumped.split("\n")]
    return out


def _regions(lines: list[str]):
    """Every `cost:` block as (start, end_exclusive, indent, is_flow, endpoint_id, anchor)."""
    eid = ""
    out = []
    i = 0
    while i < len(lines):
        if m := _ID.match(lines[i]):
            eid = m.group(1)
        if m := _FLOW.match(lines[i]):
            out.append((i, i + 1, m.group(1), True, eid, None))
        elif m := _BLOCK.match(lines[i]):
            base, j = len(m.group(1)), i + 1
            while j < len(lines) and (not lines[j].strip() or _indent_of(lines[j]) > base):
                j += 1
            out.append((i, j, m.group(1), False, eid, m.group(2)))
            i = j - 1
        i += 1
    return out


def transform(cost: dict, eid: str, provider: str, header_unverified: bool) -> dict:
    """One cost block, with units and provenance filled in. Pure — takes the parsed block, returns
    a new one — so `--check` can diff without writing."""
    if cost.get("type") == "free":
        # Free is the one price that needs no provenance: 0 does not move, and there is nothing to
        # re-check. What it needed was ONE spelling, so `usd` comes out 0.0 rather than null.
        return CANONICAL_FREE | ({"note": cost["note"]} if cost.get("note") else {})

    meta = PROVIDERS.get(provider, {})
    out = dict(cost)
    if meta:
        confidence = meta["confidence"]
        # An UNVERIFIED file header caps confidence at `documented`: nothing in it has been called,
        # so no price in it can have been seen being charged.
        if header_unverified and confidence == "verified":
            confidence = "documented"
        out |= {"per": out.get("per", 1), "unit": out.get("unit") or _unit_for(meta, out),
                "source": meta["source"], "source_url": meta["url"],
                "checked": SWEEP, "confidence": confidence}
    out |= PRICES.get(eid, {})
    if OBSERVED_NOTE.search(str(out.get("note") or "")):
        out |= {"source": "observed", "confidence": "verified", "checked": SWEEP}
        out.pop("source_url", None)  # evidence is the captured example, not a page
    if eid in UNKNOWN_PRICE:
        out |= {"value": None, "confidence": "unknown"}
        out.pop("source_url", None)
    return out


def _unit_for(meta: dict, cost: dict) -> str:
    units = meta["units"]
    return units.get(str(cost.get("type")), units["*"])


def main(argv: list[str]) -> int:
    check = "--check" in argv
    only = {a for a in argv if not a.startswith("-")}
    changed = touched = 0
    for path in sorted(CATALOG.glob("*.yaml")):
        if path.name in ("capabilities.yaml", "fx.yaml"):
            continue
        if only and path.stem.removesuffix(".extended") not in only:
            continue
        text = path.read_text(encoding="utf-8")
        lines = text.split("\n")
        provider = (yaml.safe_load(text) or {}).get("provider") or ""
        header_unverified = "UNVERIFIED" in text.split("endpoints:")[0]
        # Rewrite back-to-front so earlier regions' line numbers stay valid.
        for start, end, indent, flow, eid, anchor in reversed(_regions(lines)):
            block = yaml.safe_load("\n".join(l[len(indent):] for l in lines[start:end]))
            cost = (block or {}).get("cost")
            if not isinstance(cost, dict):
                continue
            new = transform(cost, eid, provider, header_unverified)
            touched += 1
            if new == cost:
                continue
            changed += 1
            lines[start:end] = _emit(indent, new, flow, anchor)
        out = "\n".join(lines)
        if out != text and not check:
            path.write_text(out, encoding="utf-8")
    print(f"{'would rewrite' if check else 'rewrote'} {changed} of {touched} cost block(s)")
    return 1 if (check and changed) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
