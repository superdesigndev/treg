---
title: Orbit — people context and enrichment
status: reference
sources:
  - src/treg/catalog/orbit.yaml
  - src/treg/web/logos/orbit.svg
  - tests/test_orbit_provider.py
related:
  - guides/expanding-a-category.md
  - architecture/catalog.md
---

# Orbit

Orbit finds people from identity signals or research criteria and returns source-backed
person context: current and past work, education, bio, social profiles, available contact
fields, generated profile sections and sources. The provider uses scoped `sk_orb_…` bearer
keys from https://developer.orbitsearch.com/dashboard/keys against
https://api.orbitsearch.com. Most workflows need `search:read` and `profile:read`.

## Current surface

The listing offers four person-context workflows: Search, Profile Read, single Enrich and
batch Enrich. They return deep, source-backed context for personalization, research and
person enrichment.

Treg needs two additional `kind: utility` rows for Search and Enrich status. Those rows
support the four workflows' async descriptors; they are not two extra product actions.
Only `search:read` and `profile:read` are needed. Request fields come from the current public
OpenAPI, including quick depth and the current optional fields on these same four workflows.

Requests use the actual identity inputs, continue the same run, distinguish a missing
field from a missing person, and document the output the caller can use. Company APIs,
directory management, account writes and partner-specific surfaces are outside this catalog.

## Resolve a person before reading fields

Search accepts a query, structured intent, `signals`, or a combination. Use a name plus
company when available, or the actual email, phone, LinkedIn URL, username, address or source
URL. A name alone can identify multiple people. A supplied company is matching evidence;
it does not establish that company's current-employer status or become a profile fact.

For example, the body for a name/company lookup is:

```json
{
  "intent": {
    "names": ["Patrick Collison"],
    "experiences": [{"organization": "Stripe", "temporalScope": "both"}]
  },
  "candidate_discovery": true,
  "candidate_discovery_limit": 1,
  "profile_depth": "quick",
  "include_profile": true,
  "limit": 1
}
```

`quick` builds Level 1 from supported anchored identity inputs: email, phone, address,
one name plus company, or URL/LinkedIn plus exactly one name in `intent.names`. It does not
run open-ended people discovery. Use partial or full for broader research.

Inspect candidate confidence and corroborating evidence before choosing a person. An email,
phone or address can resolve to several associated people. Return only observed profile
fields; empty contacts do not imply the person was not found. Email type and mailbox
verification are separate facts. Profile sections and available contact fields ride in
embedded Search results; explicit Profile Read also supplies sources and the image gallery.

## Continue long-running work

Search returns `search_id`; single Enrich returns `request_id`. Their async descriptors let
`treg call --await --timeout 2700` poll the status utilities. Partial generation usually
finishes in under two minutes; full and regenerate can take 25–30 minutes or longer. A
client timeout does not stop the build or release its hold: persist the ID and resume polling
rather than submitting new work. Use the same idempotency key only for a genuine POST retry.

Search `completed_with_errors` is terminal partial success: retain ready results and inspect
failures. Intermediate readable profiles can arrive before the requested full depth. Batch
Enrich has one request ID per child, no parent polling endpoint; follow each child separately.

Population/quote, bulk jobs, standalone face operations, watchers, webhooks, account usage,
company routes and partner-specific action APIs are outside this launch catalog. These need separate lifecycle and settlement review. The free usage route remains an internal connection
probe and capacity collector, not another catalog action. Future additions should be
reviewed separately with their reservation, settlement, recurring-charge and secret-handling
requirements; published API availability alone is not a reason to widen this integration.

## Prices and platform access

The live rate card is https://api.orbitsearch.com/v3/pricing, version `2026-10-22` at refresh.
The retired `/v2/developer/pricing` returns 404. A credit is $0.01 at every current purchase
size. Cached Search costs one credit per started block of ten returned cached profiles.
Generation costs 2 quick, 5 partial or 10 full credits per profile; one delivered tier replaces
discovery rather than adding to it. Full includes intermediate partial. Known-ID Enrich `quick` is a free no-op. Explicit Profile
Read costs one credit; embedded profiles and status polling add no read charge.

Optional face expansion during full generation costs 100 credits total per eligible profile
when a face provider succeeds, including an empty successful response or later build failure.
With no usable portrait or no successful provider, ordinary delivered-depth pricing applies.
A web face Search costs 100 per request plus ordinary profile charges. Both options are
opt-in; the cheap verification requests use neither.

Holds are ceilings. Settle from terminal `billing.consumedCredits` after `billing.status`
becomes `settled`; batch Enrich has child billing.
The verified no-op Enrich response can omit `billing` entirely. A fixed scalar/table price
cannot faithfully meter these cases. Search, single Enrich and batch Enrich are
therefore explicitly `platform_blocked` pending composite reservation and exact terminal
settlement. They remain callable with the caller's own key. The platform-key slot remains
available for the fixed-price Profile Read. This PR supplies no
credential and enables no platform traffic.

`GET /v3/credits/usage` is the free, scope-less connect probe, accepted only when the JSON
`status` is `success`. A live bogus `sk_orb_` key returns 403 `invalid_api_key`; a 200 failure
envelope, 404 or upstream failure never counts as connected. Credit shortage is 402
`developer_api_credits_insufficient`; honor 429 `Retry-After` without resubmitting accepted work.

Shared-key access requires correct reservation and final receipt handling. A scalar price
cannot stand in for an operation ceiling and final receipt; batch Enrich needs per-child
handling. Resolve these mechanics with the maintainers before enabling pooled paid starts.
BYOK remains functional.

Vendor evidence belongs in the PR ledger. Maintainers add verification stamps and captured
examples after their independent run; documented generation prices are not live hit claims.
