#!/usr/bin/env python3
"""Backfill documented prices onto replicate.extended.yaml from Replicate's own model pages.

Run from the repo root:
  uv run python scripts/catalog_replicate_prices.py --check            # fetch + report, write nothing
  uv run python scripts/catalog_replicate_prices.py --only flux-dev    # price matching models
  uv run python scripts/catalog_replicate_prices.py                    # price every qualifying model

Why this is a script and not a hand edit: the extended tier is regenerated wholesale by
scripts/catalog_ingest.py, which prices every generated replicate row `confidence: unknown`
(the collections API carries no prices), so a hand-typed price is lost on the next re-ingest.
This script is the re-runnable pricing pass: run it after any re-ingest, exactly like
scripts/catalog_cost_provenance.py.

Where the numbers come from: each official model's public page embeds the provider's own
structured rate card as a `billingConfig` JSON blob (verified 2026-09-06 against the
human-transcribed core price of flux-schnell: the page says "$3 per thousand output images",
core says value 0.003 - identical). That is a provider-published figure, so the blocks it
writes earn `confidence: documented` with the model page as `source_url`.

A price this script cannot read UNAMBIGUOUSLY is not written at all - the row stays
`confidence: unknown` and therefore BYOK-only, because a wrong price bills real money
(docs/context/architecture/catalog.md, Cost). What qualifies:

  * every price is per-unit on `image_output_count` (per-second hardware billing, megapixel
    metering and `unspecified_billing_metric` are all skipped - an input image's megapixels
    cannot be bounded by any `times` field, the same reason OpenRouter's megapixel SKUs
    stay BYOK);
  * tiers either have no criteria (one flat price) or every tier carries exactly one
    string-equals criterion whose title resolves through CRITERIA_FIELDS to exactly one
    declared input field (the rate card's vocabulary -> our field names is a transcription
    judgement, so it is an explicit table here, per the X_RATES lesson);
  * how many images one call may produce is either fixed, or bounded by one known integer
    count field (OUTPUT_COUNT_FIELDS) with a declared max, or is seedream's
    `sequential_image_generation` + `max_images` pair - emitted as a `times`-multiplied row.
    Two count fields on one model, or an unknown count-shaped field, disqualify it.

The emitted blocks reuse the curated flux-schnell shape (first-match table + explicit
fallback ceiling) so reserve always holds the request maximum.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
EXTENDED = ROOT / "src" / "treg" / "catalog" / "replicate.extended.yaml"

# Input fields that multiply how many billed output images one call produces, in every official
# model observed so far. All are integer fields with a declared max. A model carrying two of
# them, or a count-shaped field not listed here, is skipped instead of guessed at.
OUTPUT_COUNT_FIELDS = ("num_outputs", "num_images", "number_of_images", "max_images")
# ... except seedream's pair: `max_images` only multiplies when sequential generation is on.
SEQUENTIAL_GATE = "sequential_image_generation"

# Rate-card criteria title -> the input fields it may describe. The card speaks the provider's
# vocabulary ("target resolution", "model variant"); which request field that IS on a given model
# is a transcription judgement, so it lives in this explicit reviewable table. A title not listed
# here, or resolving to zero or several declared fields, skips the model.
CRITERIA_FIELDS = {
    "target resolution": ("resolution", "size"),
    "model variant": ("quality", "generation_mode", "variant"),
}

PRICE_TITLES = {"per output image": 1, "per thousand output images": 1000}
MAX_ENUMERATED_OUTPUTS = 10  # above this a 1..N table stops being a readable rate card

# The same neutral scene the curated core rows test with. Every input beyond the prompt keeps
# its documented default, so the verified call is the model's default-priced, single-output form.
TEST_PROMPT = "A small red kite above a calm beach."


def fetch_billing(model_name: str) -> dict | None:
    """The `billingConfig` blob embedded in the model's public page, or None."""
    req = urllib.request.Request(f"https://replicate.com/{model_name}",
                                 headers={"User-Agent": "treg-catalog-pricing/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        html = resp.read().decode("utf-8", "replace")
    marker = html.find('"billingConfig"')
    if marker < 0:
        return None
    start = html.find("{", marker + len('"billingConfig"'))
    depth = 0
    for pos in range(start, len(html)):
        if html[pos] == "{":
            depth += 1
        elif html[pos] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[start:pos + 1])
    return None


def _per_image_usd(price: dict) -> tuple[float, str] | str:
    """USD per one output image and the quoted rate line, or a reason string."""
    if price.get("type") != "per-unit" or price.get("metric") != "image_output_count":
        return f"metric {price.get('metric')!r} type {price.get('type')!r}"
    title = str(price.get("title") or "").strip()
    per = PRICE_TITLES.get(title)
    amount = re.fullmatch(r"\$(\d+(?:\.\d+)?)", str(price.get("price") or "").strip())
    if per is None or amount is None:
        return f"unparsed price {price.get('price')!r} {title!r}"
    return float(amount.group(1)) / per, f"{price['price']} {title}"


def parse_billing(billing: dict | None, properties: dict) -> tuple[list, str] | str:
    """[(when-or-None, usd, quote)] plus a rate summary, or a reason string for skipping.

    `when` is a ready {input field: value} mapping for criteria tiers, None for a flat price.
    """
    tiers = (billing or {}).get("current_tiers") or []
    if not tiers:
        return "no pricing tiers"
    if len(tiers) == 1 and not tiers[0].get("criteria"):
        prices = tiers[0].get("prices") or []
        if len(prices) != 1:
            return f"{len(prices)} price rows"
        parsed = _per_image_usd(prices[0])
        if isinstance(parsed, str):
            return parsed
        usd, quote = parsed
        return [(None, usd, quote)], quote

    rows, titles = [], set()
    for tier in tiers:
        criteria, prices = tier.get("criteria") or [], tier.get("prices") or []
        if len(criteria) != 1 or len(prices) != 1:
            return f"{len(criteria)} criteria x {len(prices)} prices in one tier"
        criterion = criteria[0]
        if criterion.get("type") != "equals" or not isinstance(criterion.get("value"), str):
            return f"unsupported criterion {json.dumps(criterion)[:80]}"
        title = str(criterion.get("title") or "").strip().lower()
        candidates = [f for f in CRITERIA_FIELDS.get(title, ()) if f in properties]
        if len(candidates) != 1:
            return f"criteria title {title!r} resolves to {candidates or 'no field'}"
        field = candidates[0]
        spec = properties[field] or {}
        if spec.get("required") is not True and "default" not in spec:
            return f"criteria field {field!r} lacks a default"
        parsed = _per_image_usd(prices[0])
        if isinstance(parsed, str):
            return parsed
        usd, quote = parsed
        titles.add(title)
        rows.append(({f"body.input.{field}": criterion["value"]}, usd, quote))
    if len(titles) != 1 or len({json.dumps(w, sort_keys=True) for w, _, _ in rows}) != len(rows):
        return "criteria tiers overlap or mix titles"
    summary = ", ".join(f"{list(w.values())[0]} {q}" for w, _, q in rows)
    return rows, summary


def output_multiplier(properties: dict) -> tuple[str, int] | None | str:
    """(count field, max) that multiplies billed outputs, None when fixed, or a skip reason."""
    counts = [f for f in OUTPUT_COUNT_FIELDS if f in properties]
    if not counts:
        if SEQUENTIAL_GATE in properties:
            return f"{SEQUENTIAL_GATE!r} without a bounded max_images"
        return None
    if len(counts) > 1:
        return f"two output count fields {counts}"
    field = counts[0]
    spec = properties[field] or {}
    top = spec.get("max")
    if spec.get("type") != "integer" or not isinstance(top, int) or not 1 <= top <= 15 \
            or (spec.get("required") is not True and "default" not in spec):
        return f"unbounded or non-integer {field!r}"
    return field, top


def priced_cost(endpoint: dict, billing: dict | None, checked: str) -> tuple[dict, str] | str:
    """(billable cost block in the curated flux-schnell shape, rate summary), or a skip reason."""
    properties = (((endpoint.get("input") or {}).get("body") or {}).get("input") or {}) \
        .get("properties") or {}
    parsed = parse_billing(billing, properties)
    if isinstance(parsed, str):
        return parsed
    price_rows, summary = parsed
    multiplier = output_multiplier(properties)
    if isinstance(multiplier, str):
        return multiplier

    sequential = multiplier and multiplier[0] == "max_images" and SEQUENTIAL_GATE in properties
    if sequential and (properties[SEQUENTIAL_GATE] or {}).get("default") != "disabled":
        return f"{SEQUENTIAL_GATE!r} default is not 'disabled'"
    table, ceiling = [], 0.0
    for when, usd, _ in price_rows:
        if sequential:
            # One image unless sequential generation is switched on; then up to `max_images`.
            gate = f"body.input.{SEQUENTIAL_GATE}"
            table.append({"when": {**(when or {}), gate: "disabled"}, "value": round(usd, 9)})
            table.append({"when": {**(when or {}), gate: "auto"}, "value": round(usd, 9),
                          "times": "body.input.max_images"})
            ceiling = max(ceiling, usd * multiplier[1])
        elif multiplier and when is None:
            field, top = multiplier
            if top > MAX_ENUMERATED_OUTPUTS:
                return f"{field!r} max {top} is too large to enumerate"
            table.extend({"when": {f"body.input.{field}": n}, "value": round(usd * n, 9)}
                         for n in range(1, top + 1))
            ceiling = max(ceiling, usd * top)
        elif multiplier:
            field, top = multiplier
            table.append({"when": when, "value": round(usd, 9), "times": f"body.input.{field}"})
            ceiling = max(ceiling, usd * top)
        elif when is None:
            ceiling = max(ceiling, usd)
        else:
            table.append({"when": when, "value": round(usd, 9)})
            ceiling = max(ceiling, usd)

    cost: dict = {"type": "per_success"}
    if table:
        bound = f"{multiplier[1]} outputs at " if multiplier else ""
        cost["table"] = table
        cost["fallback"] = {"value": round(ceiling, 9),
                            "note": f"{bound}the dearest published rate ({summary}) is the "
                                    "explicit request maximum."}
    else:
        cost["value"] = round(ceiling, 9)
    cost.update({"currency": "USD", "source": "docs",
                 "source_url": f"https://replicate.com/{endpoint['name']}",
                 "checked": checked, "confidence": "documented",
                 "note": f"Replicate's model page lists {endpoint['name']} at {summary}."})
    return cost, summary


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fetch and report; write nothing")
    parser.add_argument("--only", nargs="*", default=None,
                        help="substrings of model names/ids to price (default: all image-gen rows)")
    parser.add_argument("--test-requests", action="store_true",
                        help="also write a minimal prompt-only test_request onto priced rows; "
                             "run scripts/catalog_verify_extended.py immediately after, because "
                             "an unverified test_request fails catalog_validate.py")
    args = parser.parse_args(argv)
    checked = dt.date.today().isoformat()

    text = EXTENDED.read_text()
    doc = yaml.safe_load(text)
    priced, skipped = [], []
    for endpoint in doc["endpoints"]:
        if endpoint.get("platform") != "image-gen":
            continue
        if args.only and not any(pat in endpoint["id"] or pat in endpoint["name"]
                                 for pat in args.only):
            continue
        try:
            billing = fetch_billing(endpoint["name"])
        except Exception:  # a fetch failure must never write a price
            # Do not interpolate the exception: CodeQL treats HTTP-derived
            # text as sensitive, and the page body can leak into urllib errors.
            skipped.append((endpoint["id"], "fetch failed"))
            continue
        result = priced_cost(endpoint, billing, checked)
        if isinstance(result, str):
            skipped.append((endpoint["id"], result))
            continue
        cost, summary = result
        priced.append((endpoint["id"], summary))
        if not args.check:
            endpoint["cost"] = cost
            # Only written when the model's sole required input is the prompt; every other
            # field keeps its documented default. Behind a flag because the validator rightly
            # refuses a test_request with no verdict - write these only as the first half of a
            # catalog_verify_extended run, and commit the two results together.
            properties = endpoint["input"]["body"]["input"].get("properties") or {}
            required = {k for k, v in properties.items() if (v or {}).get("required") is True}
            if args.test_requests and "test_request" not in endpoint and required == {"prompt"}:
                endpoint["test_request"] = {"body": {"input": {"prompt": TEST_PROMPT}}}

    # Log only catalog ids (local YAML). Rate summaries and skip reasons are
    # derived from the fetched model page, which CodeQL classifies as private;
    # printing them trips py/clear-text-logging-sensitive-data on CI.
    for eid, _summary in priced:
        print(f"priced    {eid}")
    for eid, _reason in skipped:
        print(f"skipped   {eid}")
    print(f"\n{len(priced)} priced, {len(skipped)} skipped (skipped rows stay BYOK-only)")

    if priced and not args.check:
        # The exact dump call catalog_ingest.py uses, so untouched rows do not reflow.
        header, _, _ = text.partition("provider:")
        EXTENDED.write_text(header + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True,
                                                    width=4096))
        print(f"wrote {EXTENDED.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
