---
title: AnyAPI — per-request USD gateway with reported charges
status: implemented; core tier live-verified 2026-09-19 on a maintainer key
sources:
  - src/treg/catalog/anyapi.yaml
  - src/treg/catalog/anyapi.extended.yaml
  - scripts/catalog_ingest.py
  - scripts/data/anyapi_measured_charges.json
  - src/treg/oauth_providers.py
  - src/treg/config.py
  - tests/test_anyapi.py
  - src/treg/catalog/adapters.yaml
---

# AnyAPI

A vendor-raised listing (PR #398). AnyAPI fronts many upstream sources behind one schema per SKU
and charges a prepaid USD wallet per request. Two things about it differ from other providers.

## Prices: list price where it is flat, measured where it is per result

A flat-priced SKU (one price per request, most of the listing) lists `pricing.from.maxUsd`, the
cheapest source's price (`source: rate_card_api`). That source serves a default-routed call, and
the listing drops AnyAPI's routing controls (`ANYAPI_SKIP_PARAMS`), so every call from here is
default-routed. A per-result SKU (`model: linear`) lists the p90 of what AnyAPI's own ledger
billed over a trailing 60-day window, exported by the vendor to
`scripts/data/anyapi_measured_charges.json` (`source: observed`), capped at `failoverMaxUsd`;
its rate card quotes the price at the input maximum, which nobody calls at. `_anyapi_cost` in
`scripts/catalog_ingest.py` owns the rule and core rows are hand-priced by it.

Flat rows listed the p90 too until 2026-09-19. Re-measured, it tracked who had called rather
than what the next call pays: one caller's single-day burst onto the rescue source
(google.search), one customer pinning a dearer source (twitter.trends), and charges older than
a newly added cheap source (tiktok.profile). Every row sets
`cost.reported_charge: {path: costUsd, unit: usd}`, so settlement uses the exact charge in the
response body; a rescue on a dearer source settles above the listing, which `ledger.settle`
handles as an overrun. The measured file cannot be refreshed here: it is the vendor's
production ledger, so a re-ingest only re-reads the rate card until the vendor re-exports it.
`linkedin.search_posts` is hand-set to its cheap source's billed $0.0012: AnyAPI had that source
paused at the 2026-09-19 read and the card showed only the $0.018 rescue.

109 extended rows carry a reviewed `capability` onto an existing shelf (carried across
re-ingests); none proposes a new capability.

## Registry and platform key

`X-API-Key`, probe `GET /v1/balance` (free; bogus key answers `401 invalid credential`).
`platform_key_anyapi` reads `TREG_PLATFORM_KEY_ANYAPI`; USD-priced, so no `fx.yaml` row.
`tests/test_anyapi.py` pins every row platform-eligible and settling on `costUsd`.

## Routing

22 core rows carry adapters in `src/treg/catalog/adapters.yaml` and are children of their
`treg.<capability>` parents (X search, posts, profile and replies; TikTok, Instagram, Facebook
and LinkedIn reads; Google organic, news, place lookup and autocomplete). The envelope is one
shape everywhere: `output.found` plus `output.data`. A profile miss is `found: false, data: null`;
a search miss is `found: true` with an empty list, so list rows test `coalesce(list, []) == []`.
Verified through a local server on 2026-09-19: hit and miss on `treg.x.search.posts` and
`treg.x.user.profile`, hit on `treg.x.post.comments`, each settling the reported `costUsd`
(a billed miss settles its charge, the reported-charge rule runs ahead of the miss rule).
Deliberately not adapted: `linkedin.email` (a $0.011 billed miss inside a waterfall of free
misses) and `tiktok.profile_videos` (the contract identity is `sec_uid`; the row takes a handle).

## Traps

- **Misses are billed.** A source answering not-found is a 2xx `output.found: false` with the
  full `costUsd` (linkedin.email $0.011, profiles, instagram.post); only `company_employees`
  answered a miss at $0. `reported_charge` settles it faithfully, so the caller pays for the miss.
  Each probed row carries a `miss:` block saying so. Probed 2026-09-19 with impossible targets.

- `google.ai_overview` takes ~90-110 s; `catalog_verify.py`'s 60 s client timeout fails it. AnyAPI
  charges the timed-out request once and serves the identical retry as `replayed: true` with the
  same `costUsd` and no second charge.
- `linkedin.email` returns a named person's work email: `untestable:`, no test_request, no example
  (catalog PII rule). Its price was observed once at exactly $0.011.
- The extended tier is generated with `carry_input=False` so a re-ingest refreshes parameters.
