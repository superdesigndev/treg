# Catalog file keys, v2 spelling

The keys of `src/treg/catalog/<service>.yaml`. Unknown keys are refused, and
`scripts/catalog_validate.py` names any that are wrong.

## Contents

- File header and `defaults:`
- Endpoint keys
- `price` and `provenance`
- Credit rates in `fx.yaml`
- `reserve` and `settle`: when the request or the answer sets the charge
- Outcome rules: `bill`, `miss`, `expect`, `error`, and the order they apply in
- Paths and predicates
- `summary` and `note`

## File header and `defaults:`

```yaml
provider: acmeseo                      # the registry entry's `service`
source:
  docs: https://docs.acmeseo.example
  openapi: https://api.acmeseo.example/v1/openapi.json   # null when none is published
  curated: 2026-11-24
limits: "60 requests a minute per key"   # optional; leave it out when unpublished
pricing_url: https://acmeseo.example/pricing
defaults:
  provenance: {source: docs, source_url: 'https://acmeseo.example/pricing', checked: '2026-11-24', confidence: documented}
endpoints:
  - ...
```

`defaults:` holds any endpoint key shared by every endpoint, usually `provenance`, and `bill` or
`expect` when they hold API-wide. An endpoint's own value replaces the default whole; its `null`
opts out.

## Endpoint keys

| Key | Value |
|---|---|
| `id` | `<service>.` + the capability: `acmeseo.web.backlinks.summary`; when two endpoints share a capability, the second adds a short suffix that names the difference (`.bulk`, `.preview`, `.by_domain`) |
| `capability` | from `capabilities.yaml`, or one proposed in this file |
| `platform` | what the data is about (`web`, `people`, `amazon`), usually the capability's first part |
| `scope` | `any_account` (data about anything) or `own_account` (the caller's own account; never served on treg's key) |
| `method`, `path` | `path` relative to `base_url`; keep a trailing slash the API redirects to |
| `name`, `summary`, `note` | see "`summary` and `note`" |
| `input` | parameters under `queryParams`, `pathParams` or `body`; a form body adds `bodyType: form` (JSON needs none) |
| `test_request` | the values verification sends, under the same three keys |
| `price`, `provenance` | below |
| `reserve`, `settle`, `bill`, `miss`, `expect`, `error` | below, when needed |
| `docs_url` | the endpoint's docs page, or the docs root when there is none |
| `domain` | optional grouping, one the catalog already uses: `contacts`, `companies`, `search`, `jobs`, `extract`, `account` |
| `kind` | optional: `account` for an endpoint about the caller's own account, `utility` for a helper (a count, a status check) |
| `added` | written by `scripts/catalog_added.py`, never by hand |

An endpoint without parameters leaves `input` out and sends `test_request: {queryParams: {}}`.

Each parameter: `type` (`string`, `integer`, `number`, `boolean`, `object`, or `array` with
`items`), `required`, `example`, and when they apply `default`, `enum`, `min`/`max` (numbers),
`minLength`/`maxLength` (strings), `note` (constraints). Write `enum` when the API refuses other
values and the list is short; a long list (country or language codes) is described in `note`.

## `price` and `provenance`

`price` reads "`value` `currency` per `per` `unit`". `value` is a quoted decimal.

```yaml
price: {value: '0.002', currency: USD, per: 1, unit: call}
price: {value: '2.00', currency: USD, per: 1000, unit: row}
price: {value: '1', currency: credit, per: 1, unit: call, note: "a miss is free"}
```

`unit` names what one unit is: `call`, or the thing counted, closest to the vendor's own word
(`result` for search hits, `record` for a looked-up record, `row` for exported rows; `keyword`,
`page`, `character` and more, which the validator lists). `price.note` holds what a caller must
know about the charge.

- A free endpoint: `price: {value: '0', currency: USD, unit: call}`, even in a credit-priced file.
- Charged per whole block: `round: up`. "1 credit per 10 rows, rounded up" is
  `{value: '1', currency: credit, per: 10, unit: row, round: up}`.

`provenance` says where the price came from:

| Key | Value |
|---|---|
| `source` | `docs` (the pricing page), `rate_card_api` (an endpoint returning current prices), `observed` (your meter) |
| `source_url` | the page or endpoint |
| `checked` | the day you read it, quoted: `'2026-11-24'` |
| `confidence` | `documented` for a published price, `verified` once your meter confirmed it |

Usually one `provenance` in `defaults:`, and an endpoint whose charge you observed carries its own
`{source: observed, ..., confidence: verified}`.

## Credit rates in `fx.yaml`

When `currency: credit`, add one line under `credit_rates_usd:` in `src/treg/catalog/fx.yaml`, above
`rates_to_usd:`, at the entry-level price:

```yaml
  acmeseo: {usd: 0.0245, basis: "Starter $49/mo for 2,000 credits", source: "https://acmeseo.example/pricing", checked: "2026-11-24"}
```

`basis` names the plan and the arithmetic.

## `reserve` and `settle`: when the request or the answer sets the charge

Without them, one call costs one `per` of `price`.

- `reserve` is how many units a request holds before the call:
  - `rows`: a count parameter sets it.
    `{rows: {param: query.limit, default: {value: 100, source: docs}, max: 1000}}`. Give a
    `default` whenever the API has one.
  - `entities`: a list parameter sets it, one unit per item.
    `{entities: {from: query.asin, max: 100}}`.
- `settle` is what the answer says was used:
  - `count`: items returned. `{count: {path: response.backlinks}}`.
  - `reported`: the answer states the charge, in its body (`{reported: {path:
    response.credits_used, unit: credit}}`) or in a response header (`{reported: {header:
    x-cost-usd, unit: usd}}`). Prefer it whenever the answer carries the charge: it bills exactly
    what the API metered. A `unit` in credits needs the `fx.yaml` rate too.

## Outcome rules: `bill`, `miss`, `expect`, `error`, and the order they apply in

treg reads each answer as a hit, a miss (the request was fine, there is nothing to return) or an
error, and bills and routes by it.

- `bill`: misses and errors are free unless stated. An API that bills them:
  `bill: {miss: charged, error: charged}`, usually in `defaults:`. `bill: {free_when: <predicate>}`
  makes a matching answer free.
- `miss`: required on every endpoint that finds or enriches something. Without it a 4xx miss reads
  as an error, and a route asking several providers fails.
  - On a 2xx: `miss: {when: {path: response.data.email, op: empty}}`.
  - On a status: `miss: {status: [404], means: "no person on record for this identifier"}`.
- `expect`: what every good answer carries, when a 2xx can still be a failure.
  `expect: {path: response.code, op: equals, value: 0}`.
- `error`: a 2xx that is an error. `error: {when: {path: response.error, op: present}}`.
  Required when the API can answer an error with a 2xx; without it that answer counts as a hit.

The order:

1. A non-2xx is a miss when a `miss` rule lists its status, otherwise an error.
2. A 2xx is an error when `expect` does not hold or `error` holds.
3. A 2xx is a miss when a `miss` rule without a status holds.
4. Anything else is a hit.

So an `error` rule in `defaults:` does not touch a 404 that `miss` lists.

## Paths and predicates

- Paths start at `response.` (the answer), or at `query.` or `body.` (the request):
  `response.data.items.0.id`, `body.limit`.
- A predicate is `{path, op, value}`. `op` is `equals`, `in`, `contains`, `present`, `absent` or
  `empty`; the last three take no `value`.
- A list of predicates holds when all hold; `{any: [...]}` when one does.

## `summary` and `note`

- `summary`: what the endpoint does, starting with a verb and its object, then the key outputs.
  Positive only, under about 50 tokens.
- `note`: caveats about the whole endpoint that most callers meet and that change how they call it
  or read the answer. Under about 75 tokens, each sentence a fact and its consequence: "Answers for
  a private account are empty: check `is_private` before paging."
- A caveat goes to its most specific home: a parameter's `note`, then `price.note`, then the
  endpoint's `note`. Evidence (dates, how a price was measured) goes in the pull request.
- Write only what your calls showed. No "may" or "often"; a behavior that holds only sometimes names
  the case.
