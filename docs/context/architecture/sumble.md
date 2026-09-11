---
title: Sumble — account intelligence, subscription credits and BYOK
status: shipped
sources:
  - src/treg/catalog/sumble.yaml
  - src/treg/catalog/sumble.extended.yaml
  - src/treg/catalog/examples/sumble.organizations.json
  - src/treg/application/call/sumble.py
  - tests/test_sumble.py
  - src/treg/web/logos/sumble.svg
related:
  - architecture/catalog.md
  - architecture/auth-secrets.md
  - architecture/money.md
  - ops/capacity.md
---

# Sumble

`oauth_providers.SUMBLE` connects a pasted Bearer key to `https://api.sumble.com/v9`.
The catalog was ingested from the [official v9 OpenAPI](https://api.sumble.com/openapi.json?version=v9)
on 2026-09-09: all 30 operations are listed, seven in the core file and 23 in the extended file.
The MCP transport is not a tool. There is no invented account/balance endpoint.
The registry entry verifies a key with a nonsense `POST /technologies/find` query.
Live: garbage Bearer returned 401; treg's `/connections/token` returned 422 and created no connection.
A valid key returned 200 with zero credits used; a valid connection and BYOK call were tested locally.

## Access and pricing

A team's own key always wins, including for workspace operations, and does not change its treg balance.
`platform_key_sumble` reads `TREG_PLATFORM_KEY_SUMBLE`. Platform access additionally requires
Sumble in the existing deployment allow-list; this change does not enable production.
The purchased Pro subscription supplies 9,900 monthly credits for $99. `fx.yaml` uses $0.01 per credit
before the configured platform margin, following other subscription providers. A later custom plan
can retain that list rate. Vendor auto-top-up is optional; its enabled state is not inferred.

Ten operations are live-verified platform offers:

- Organization matching/enrichment, jobs and teams.
- Organization confirmed-used tech stack.
- Technology find, technology lookup, technology category lookup, project lookup and job-title lookup.
- Documentation search (free).

`sumble.enforce` runs only after BYOK resolution and before reserve/relay. It rejects ambiguous
JSON and unsupported platform request shapes without rewriting the request. Platform organization
calls use explicit match inputs. Jobs and teams accept list mode or organization-ID filters.
Advanced-query filters, related people/job selections, ICP/CRM attributes, `all`/exploded metrics and
CSV exports require BYOK. Named technology/job-function/project metrics and aggregate categories
with explicit metric arrays can be used for organization enrichment. All account-specific routes
are `platform_blocked`, and their caches are forbidden.

`sumble.estimate` reads the credit rules in `cost.sumble`: one base credit per match, paid selected
attributes and explicit metric counts. Lookup arrays reserve ceil(input count / 100) credits.
`_observed_cost_micro` settles nonnegative integer `credits_used` at the request-time credit rate,
including zero; malformed/missing usage follows the existing estimate/miss fallback. It never
uses `credits_remaining` as a per-call charge. Tech stacks have no documented result cap: reserve
100 credits as an estimate, then settle actual usage, which can exceed the estimate. Only technologies
in category `technologies` arrays are billed, not the larger `evidence` arrays.

`adapters.yaml` adds organization matching to `treg.companies.enrich`, using domain or name and
explicit id/name/url/employee_count/industry attributes (three credits for a hit). Jobs, teams and
tech stack require provider-specific identifiers, so no unsupported canonical-input mapping is invented.
People routing is not added while platform async settlement is blocked.

## Why other tools need the caller's own key

People is asynchronous: POST `/people` starts a job, then POST the same URL with `request_id`
alone. The first `succeeded` poll charges once, and subsequent polls report zero. treg's existing
async contract uses GET utilities, and cannot safely recover the lost first charge evidence or
scope POST poll IDs without additional work. People remains a faithful BYOK tool; no platform async
support is claimed. Contact reveal add-ons (email 10, phone 80, successful email resolution 20)
are documented, not live-verified in this integration.

Organization signals also expose account signal configuration and CRM data. Intelligence briefs
are tailored to the calling account. Both remain BYOK, alongside organization/contact lists,
signal search/priority/configs/relevance and support. No support ticket was sent, no workspace
list was created or edited, and untested workspace operations are labeled honestly.

[Sumble scoring](https://docs.sumble.com/enterprise-services/account-scoring) depends on the caller's
ICP: `sumble_score`, `account_score`, `person_score`, team `score`, and CRM `account_status` must not
be exposed through the platform key. Organization identity attributes must be selected explicitly:
live responses contradicted the prose claim that free id/name/url fields are always included.

## Verification evidence (2026-09-09)

The initial account balance was 9,900. Every paid observation below used the response's
`credits_used`, reconciled to `credits_remaining`; public examples omit opaque call IDs and
replace the platform balance. People results were not committed.

| Operation / variant | HTTP | Credits observed |
|---|---:|---:|
| Technology find: nonsense / Kubernetes | 200 | 0 / 1 |
| Organizations: one / two matches with two paid attributes | 200 | 3 / 6 |
| Organizations: unmatched domain | 200 | 0 |
| Organizations: one match and two technology metrics | 200 | 3 |
| Organizations: two search results with industry | 200 | 4 (BYOK query variant) |
| Jobs: two results, location and posted_date; empty filter | 200 | 6 / 0 |
| Teams: two results, jobs_count and breadcrumbs; empty filter | 200 | 6 / 0 |
| Sumble organization tech stack | 200 | 20 |
| Technology lookup: 2 inputs / 101 inputs / miss | 200 | 1 / 2 / 0 |
| Category lookup: CRM | 200 | 1 |
| Project lookup: cloud migration | 200 | 1 |
| Job-title lookup: 2 titles | 200 | 1 |
| Documentation search | 200 | free, no credit meter |
| People: kickoff / first success / repeated success | 200 | 0 / 2 / 0 |

These first probes consumed 57 credits (9,900 → 9,843). Further exact routing and local connection
checks are recorded in the task's final verification report. Nonempty lookup rounding above 100
was observed for technologies; the other lookup families share the documented block rule.
The org signal/brief and workspace prices are documented only, with no verified stamp.

`collectors._sumble` reads remaining credits from the free technology-search miss. The capacity policy
uses `monthly_quota` / `quota_reset`, records the 10 requests/second shared rate limit, and does not
invent a renewal timestamp. Zero is exhausted; missing, malformed or negative balance is unknown.
Exhaustion signatures have not been forced, and no overflow route is claimed.

Final local verification: exact single-company fixture consumed 3 more credits; local BYOK
checks consumed 3 total and changed no treg balances; platform direct and routed enrichment each
consumed 3 credits and settled 30,000 micro-USD. Final Sumble balance: 9,831, a reconciled total
of 69 credits. All disposable test teams were deleted, and the dev allow-list was restored.
The routed response retained the native Sumble payload and mapped Stripe's firmographics correctly.
The full test suite passed (3,228 tests, 5 skips) with `TREG_PUBLIC_URL=https://treg.to` and
`TREG_OVERFLOW_MODE=off` to isolate local .env overrides; catalog validation and 14 import contracts
passed. An additional routed regression was run with the provider/validator checks afterward.

## Full operation coverage and verification state

| Catalog suffix | Method and v9 path | Platform / evidence |
|---|---|---|
| organizations | POST /organizations | Verified; restricted selections as above |
| organizations.techs | GET /organizations/{organization_id}/techs | Verified, 20 technologies/credits |
| jobs | POST /jobs | Verified, 2 results/6 credits |
| teams | POST /teams | Verified, 2 results/6 credits |
| technologies.find | POST /technologies/find | Verified, hit 1 / miss 0 |
| technologies.lookup | POST /technologies/lookup | Verified, 2 inputs 1 / 101 inputs 2 |
| technologies.categories.lookup | POST /technologies/categories/lookup | Verified, 1 matched category/credit |
| projects.lookup | POST /projects/lookup | Verified, 1 matched project/credit |
| job_functions.lookup | POST /jobs/title-lookup | Verified, 2 matched titles/1 credit |
| documentation.search | GET /documentation/search | Verified, free |
| people | POST /people | BYOK; async observed 0 → 2 → 0; reveals not tested |
| organizations.signals | GET /organizations/{organization_id}/signals | BYOK; documented 1/signal, not live tested |
| organizations.intelligence_brief | GET /organizations/{organization_id}/intelligence-brief | BYOK; documented 50/completion, not live tested |
| organization_lists.list | GET /organization-lists | BYOK; documented 1/list, not live tested |
| organization_lists.get | GET /organization-lists/{list_id} | BYOK; documented 1/organization, not live tested |
| organization_lists.create | POST /organization-lists | BYOK; documented free, not executed |
| organization_lists.rename | POST /organization-lists/{list_id}/name | BYOK; documented free, not executed |
| organization_lists.set_deleted | POST /organization-lists/{list_id}/deleted | BYOK; documented free, not executed |
| organization_lists.add | POST /organization-lists/{list_id}/organizations | BYOK; documented free, not executed |
| organization_lists.set_signals | POST /organization-lists/{list_id}/signals | BYOK; documented free, not executed |
| contact_lists.list | GET /contact-lists | BYOK; documented 1/list, not live tested |
| contact_lists.get | GET /contact-lists/{list_id} | BYOK; documented 1/person, not live tested |
| contact_lists.create | POST /contact-lists | BYOK; documented free, not executed |
| contact_lists.add | POST /contact-lists/{list_id}/people | BYOK; documented free, not executed |
| signals.search | POST /signals | BYOK; documented 1/signal, not live tested |
| signals.priority | POST /signals/priority | BYOK; documented 1/signal, not live tested |
| signals.priority.relevance | POST /signals/priority/{item_id}/relevance | BYOK; documented free, not executed |
| signals.configs | POST /signals/configs | BYOK; documented free, not live tested |
| support | POST /support | BYOK; documented free, no message sent |
| support.data_quality | POST /support/data-quality | BYOK; documented free, no message sent |

Vendor contact: support@sumble.com. Costs and behavior not observed above remain documented claims,
not verified promises. Official endpoint input schemas are preserved, including nested selection
fields, so BYOK retains the entire documented input surface.

## Catalog placement and price labels

Company data has 25 operations (six data tools and 19 account/helpers). People & contact data has
five (people search and four contact-list management operations). List mutations, signal relevance
and support are account management; documentation and lookup routes are helpers. The public
provider page counts all 30, showing 10 platform + BYOK and 20 BYOK-only tools, with 10 verified.
`cost.display` declares the presentation rules; the generic `cost_view` supplies display units
without reading Sumble billing modes or changing billing scalars: composite enrichment starts
at $0.01/result plus selections (the compact label is `$0.01+/result`), lookups cost $0.01 per started 100 matched inputs, and stacks
cost $0.01/technology. These values follow the configured credit rate. BYOK rows disclose that
upstream prices do not represent a treg charge. Local Sumble platform access is now enabled
in the dev allow-list following the user's root .env update.
