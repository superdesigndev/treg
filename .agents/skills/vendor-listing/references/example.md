# Example listing: "Acme SEO"

## Contents

- 1. Registry entry
- 2. Catalog file
- 3. If Acme priced in credits
- 4. The pull request description

A fictional vendor. Acme SEO sells backlink data at `https://api.acmeseo.example/v1`. The key goes
in an `X-Api-Key` header, `/account` is free, and its pay-as-you-go price is $0.002 a request for a
summary and $2 per 1,000 backlinks returned, at `https://acmeseo.example/pricing`. A failed request
is not billed.

## 1. Registry entry: `src/treg/oauth_providers.py`

```python
ACMESEO = OAuthProvider(
    service="acmeseo",
    display_name="Acme SEO",
    auth_kind="key",
    token_label="API key",
    token_placeholder="your Acme SEO API key",
    token_header="X-Api-Key",
    token_format="{secret}",
    setup_url="https://acmeseo.example/dashboard/api-keys",
    setup_action_label="Get your Acme SEO API key",
    setup_steps=(
        "Sign in to Acme SEO and open Settings, then API Keys.",
        "Copy your API key.",
    ),
    setup_note="Backlink queries are billed per successful request; the account check is free.",
    auth_uri="", token_uri="",
    scopes={},
    client_id_setting="", client_secret_setting="",
    category="SEO",
    summary="Backlink profiles, referring domains and anchor texts for any domain or URL.",
    base_url="https://api.acmeseo.example/v1",
    docs_url="https://docs.acmeseo.example",
    probe_path="/account",  # free; an invalid key gets a 401 here
)
```

Then `ACMESEO` joins the provider tuple that builds `REGISTRY`, and
`src/treg/web/logos/acmeseo.svg` holds a neutral lettermark.

## 2. Catalog file: `src/treg/catalog/acmeseo.yaml`

```yaml
provider: acmeseo
source:
  docs: https://docs.acmeseo.example
  openapi: https://api.acmeseo.example/v1/openapi.json
  curated: 2026-11-24
limits: "60 requests a minute per key"
pricing_url: https://acmeseo.example/pricing
defaults:
  provenance: {source: docs, source_url: 'https://acmeseo.example/pricing', checked: '2026-11-24', confidence: documented}
endpoints:
  - id: acmeseo.web.backlinks.summary
    capability: web.backlinks.summary
    platform: web
    scope: any_account
    method: GET
    path: /backlinks/summary
    name: "Backlink summary"
    summary: "Summarize a domain's or URL's backlink profile: total backlinks, referring domains and authority"
    input:
      queryParams:
        target: {type: string, required: true, note: "a domain or a full URL", example: "moz.com"}
        mode: {type: string, required: false, enum: [domain, url], note: "default domain", example: "domain"}
    test_request:
      queryParams: {target: "moz.com", mode: "domain"}
    price: {value: '0.002', currency: USD, per: 1, unit: call}
    # A target Acme has never crawled answers 200 with no backlinks counted.
    miss: {when: {path: response.backlinks_total, op: absent}}
    docs_url: https://docs.acmeseo.example/backlinks-summary
    added: '2026-11-24'                  # written by catalog_added.py

  - id: acmeseo.web.backlinks.list
    capability: web.backlinks.list
    platform: web
    scope: any_account
    method: GET
    path: /backlinks
    name: "List backlinks"
    summary: "List the backlinks pointing at a domain or URL, with source page, anchor text and first-seen date"
    input:
      queryParams:
        target: {type: string, required: true, note: "a domain or a full URL", example: "moz.com"}
        limit: {type: integer, required: false, min: 1, max: 1000, note: "backlinks to return, default 100", example: 10}
    test_request:
      queryParams: {target: "moz.com", limit: 1}
    # Billed per backlink returned: the hold is the requested limit, the charge the rows that came back.
    price: {value: '2.00', currency: USD, per: 1000, unit: row}
    reserve: {rows: {param: query.limit, default: {value: 100, source: docs}, max: 1000}}
    settle: {count: {path: response.backlinks}}
    miss: {when: {path: response.backlinks, op: empty}}
    docs_url: https://docs.acmeseo.example/backlinks
    added: '2026-11-24'

  # ...more endpoints in the same shape
```

Both `added:` lines come from `scripts/catalog_added.py`; do not type them.

## 3. If Acme priced in credits

Each row would carry `price: {value: '1', currency: credit, per: 1, unit: call}`, and
`src/treg/catalog/fx.yaml` would gain one line under `credit_rates_usd:`, above `rates_to_usd:`, at
Acme's entry level:

```yaml
  acmeseo: {usd: 0.0245, basis: "Starter $49/mo for 2,000 credits", source: "https://acmeseo.example/pricing", checked: "2026-11-24"}
```

## 4. The pull request description

```
Adds Acme SEO (backlink data) as a key provider: 10 endpoints.

Contact: partnerships@acmeseo.example

## Verification ledger (2026-11-24)

| endpoint | http | test target | charge observed | catalog price | matches? | meter |
|---|---|---|---|---|---|---|
| acmeseo.web.backlinks.summary | 200 | moz.com | $0.002 | $0.002 / call | ✅ | `x-charge-usd: 0.002` header |
| acmeseo.web.backlinks.list | 200 | moz.com, limit 1 | $0.002 | $2.00 / 1,000 rows | ✅ | balance $20.000 → $19.998 |
...

Invalid key on GET /account (2026-11-24): 401 {"error": "invalid_api_key"}
Miss: acmeseo.web.backlinks.summary on never-crawled.example: 200 {"target": "never-crawled.example"}, 340 ms, $0

## Endpoint map

Catalogued: summary, list, referring domains, anchors, ...
Excluded: /account/usage (own account), /exports (async bulk file, not an agent call), ...

## Pricing

https://acmeseo.example/pricing, pay-as-you-go. Per success: failed requests are not billed.
OpenAPI: https://api.acmeseo.example/v1/openapi.json

catalog_validate.py: OK, 0 errors. pytest: 0 failed.
```
