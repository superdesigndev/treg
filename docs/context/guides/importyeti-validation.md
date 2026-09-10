---
title: ImportYeti trade-data pilot — validation and release gates
status: draft; no valid-key or paid verification
sources:
  - src/treg/catalog/importyeti.yaml
  - src/treg/web/logos/importyeti.svg
  - tests/test_importyeti.py
related:
  - architecture/catalog.md
  - architecture/auth-secrets.md
  - guides/expanding-a-category.md
---

# ImportYeti trade-data pilot

This draft lists 15 operations from the vendor's 26-operation OpenAPI surface. No
valid credential or dollar/credit quote is available. Every endpoint carries an
explicit `platform_blocked` reason; the platform-key slot is preparatory only.
No paid response, target-industry coverage or production availability is claimed.

## Sources and auth evidence

- [Official reference](https://docs.importyeti.com/docs/reference).
- [Embedded OpenAPI](https://data.importyeti.com/swagger/swagger-ui-init.js): extract
  the `swaggerDoc` object. `/openapi.json` is not a working spec URL. The catalog
  preserves the native path/query schemas; its date note explains the dynamic
  previous-month-end default instead of freezing the fetched snapshot's date.
- [API credits](https://docs.importyeti.com/docs/credits): profile 1 credit,
  BOL detail 0.1 credit, lists 0.1 per returned record. Search is conditionally
  free when followed by full-data access; standalone search costs credits and
  may need account configuration. Its catalog price stays unknown, never free.
- [Data use](https://www.importyeti.com/policies/data-use) describes purchased-data
  reuse; free website data and bulk/continuous feeds have separate conditions.

On 2026-09-10 a real bogus `IYApiKey` sent to
`GET https://data.importyeti.com/v1.0/database-updated` returned HTTP 401 with
`{"message":"Unauthorized","statusCode":401}`. The same real upstream probe
through isolated treg `POST /connections/token` returned 422 and created no tool.
A public Walmart sample returned 200 without debit fields. It is demo evidence,
not a valid-key success or a paid sample. The automated success fixture is
synthetic and proves only treg's generic binding behavior.

## Full surface disposition

All paths below use GET. Deferred operations need entitlement, target-data and
billing checks before promotion; they are not catalog entries in this draft.

| Path | Disposition |
|---|---|
| `/v1.0/bol/{number}` | Core draft; valid-key and charge pending |
| `/v1.0/company/search` | Core draft; valid-key and charge pending |
| `/v1.0/company/{company}` | Core draft; valid-key and charge pending |
| `/v1.0/company/{company}/bols` | Core draft; valid-key and charge pending |
| `/v1.0/supplier/search` | Core draft; valid-key and charge pending |
| `/v1.0/supplier/{supplier}` | Core draft; valid-key and charge pending |
| `/v1.0/supplier/{supplier}/bols` | Core draft; valid-key and charge pending |
| `/v1.0/product/{product}/suppliers` | Core draft; valid-key and charge pending |
| `/v1.0/product/{product}/companies` | Core draft; valid-key and charge pending |
| `/v1.0/powerquery/us-import/bols` | Core draft; valid-key and charge pending |
| `/v1.0/powerquery/us-import/companies` | Core draft; valid-key and charge pending |
| `/v1.0/powerquery/us-import/suppliers` | Core draft; valid-key and charge pending |
| `/v1.0/powerquery/us-export/bols` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/us-export/bols/{bolNumber}` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/us-export/companies` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/mx-import/declarations` | Core draft; valid-key and charge pending |
| `/v1.0/powerquery/mx-import/declarations/{declarationId}` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/mx-import/companies` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/mx-import/suppliers` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/mx-import/brokers` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/mx-export/declarations` | Core draft; valid-key and charge pending |
| `/v1.0/powerquery/mx-export/declarations/{declarationId}` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/mx-export/companies` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/mx-export/suppliers` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/powerquery/mx-export/brokers` | Deferred: US export / additional Mexico detail and aggregates |
| `/v1.0/database-updated` | Core draft; valid-key and charge pending |

## Live validation required before merge or paid enablement

Obtain an API credential without putting it in code, a purchase receipt or API
quote (USD/credit, minimum top-up, expiry), an authorized test budget and written
clarity on shared-account search billing. No subscription or purchase is implied.

For each of the 15 core operations record: resolved target and exact parameters,
retrieval date, HTTP status, substantive response, expected credits, `requestCost`,
`creditsRemaining` and an independently observable serial balance delta. Test a
multi-row response and an empty result. Populate scrubbed examples and replayable
`test_request`s only with real identifiers harvested from those calls. Do not use
public demo meter values or mocked fixtures as evidence of actual billing.

Before removing `platform_blocked`, implement and validate reserve/settlement
against those actual responses, including fractional charges, default pagination,
zero hits, and JSON versus TOON. `fields`/`exclude` do not change billing. Leave FX
null until the dollar rate is evidenced; simply adding a key or rate does not
satisfy the gate. Rerun the validator, full test suite and plugin consistency check.

## News investigation acceptance

Compare at least three correctly identified relevant companies and one ambiguous
or negative control against a web-search baseline. Record legal entity, country,
operational address, parent relationship and source IDs before enriching contacts.
Keep event date, publication date, shipment date, database update and retrieval
separate. Explicit MM/DD/YYYY windows are essential: defaults start in 2021 and
end at the previous month end. PowerQuery defaults space-separated terms to OR.

US ocean records exclude air/land and can omit confidential or aliased entities.
Mexico declarations have a different coverage model. A shipper can be a forwarding
intermediary. Shipment counts or weights are not inventory, orders, revenue or a
product-specific design win. No current Apple supplier claim is established by
this draft. If target queries add little beyond public search, switch sources;
do not promote the provider just because the integration compiles.

The content deliverable is a real finding with traceable evidence, a recorded
through-treg call sequence and cost, and English X copy plus a reproducible demo.
Only enrich business contacts after entity resolution; do not publish personal
contact details. Measure views, qualified visits and first successful tool use
after publication. Code tested, PR merged, production enabled, case verified and
content published are separate states. Traffic is an outcome to measure, not a
promise this integration can validate in advance.
