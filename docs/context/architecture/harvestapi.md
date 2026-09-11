---
title: HarvestAPI — API-key-only LinkedIn reads and reported USD charges
status: implemented; upstream and local platform/BYOK integration verified
sources:
  - src/treg/domain/catalog/routing/contracts.py
  - src/treg/catalog/harvestapi.yaml
  - src/treg/web/logos/harvestapi.svg
  - src/treg/oauth_providers.py
  - src/treg/config.py
  - src/treg/domain/catalog/store.py
  - src/treg/application/call/resolve.py
  - src/treg/domain/capacity/collectors.py
  - src/treg/domain/capacity/policy.py
  - src/treg/catalog/adapters.yaml
  - scripts/catalog_validate.py
  - render.yaml
  - tests/test_harvestapi.py
  - src/treg/catalog/examples/harvestapi.linkedin.ads.get.json
  - src/treg/catalog/examples/harvestapi.linkedin.ads.search.json
  - src/treg/catalog/examples/harvestapi.linkedin.comment.reactions.json
  - src/treg/catalog/examples/harvestapi.linkedin.company.posts.json
  - src/treg/catalog/examples/harvestapi.linkedin.company.profile.json
  - src/treg/catalog/examples/harvestapi.linkedin.company.search.json
  - src/treg/catalog/examples/harvestapi.linkedin.geo.search.json
  - src/treg/catalog/examples/harvestapi.linkedin.group.get.json
  - src/treg/catalog/examples/harvestapi.linkedin.group.search.json
  - src/treg/catalog/examples/harvestapi.linkedin.job.get.json
  - src/treg/catalog/examples/harvestapi.linkedin.leads.search.json
  - src/treg/catalog/examples/harvestapi.linkedin.post.comment_replies.json
  - src/treg/catalog/examples/harvestapi.linkedin.post.comments.json
  - src/treg/catalog/examples/harvestapi.linkedin.post.get.json
  - src/treg/catalog/examples/harvestapi.linkedin.post.reactions.json
  - src/treg/catalog/examples/harvestapi.linkedin.post.search.json
  - src/treg/catalog/examples/harvestapi.linkedin.profile.search.json
  - src/treg/catalog/examples/harvestapi.linkedin.search.jobs.json
  - src/treg/catalog/examples/harvestapi.linkedin.service.search.json
  - src/treg/catalog/examples/harvestapi.linkedin.user.comments.json
  - src/treg/catalog/examples/harvestapi.linkedin.user.posts.json
  - src/treg/catalog/examples/harvestapi.linkedin.user.profile.email.json
  - src/treg/catalog/examples/harvestapi.linkedin.user.profile.json
  - src/treg/catalog/examples/harvestapi.linkedin.user.profile.main.json
  - src/treg/catalog/examples/harvestapi.linkedin.user.reactions.json
related:
  - architecture/catalog.md
  - architecture/auth-secrets.md
  - architecture/money.md
  - architecture/proxy-model.md
  - ops/capacity.md
---

# HarvestAPI

`HARVESTAPI` registers a pasted `X-API-Key` on `https://api.harvestapi.io`.
`platform_key_harvestapi` reads `TREG_PLATFORM_KEY_HARVESTAPI`; the existing provider
allow-list must also contain `harvestapi`. Own tools/keys win and never spend the treg
balance, even when Harvest rejects or exhausts the customer's key. Platform calls follow
the existing reserve → relay → settle/release lifecycle.

## Scope and inputs

`harvestapi.yaml` contains 25 catalog tools across 23 GET paths. Basic, full and email
profile lookup are distinct tools with explicit prices and strict parameters. There are
no public wallet/account tools, LinkedIn interactions, custom-account cookies or private
pool selectors. URL-based search overrides and unverified profile add-ons are also omitted.

`strict_query: true` is an opt-in **catalog** contract, enforced by
`_enforce_catalog_query` before credential selection, reservation or upstream I/O on every
credential tier. Only declared query parameters, once each, and declared string enum values
are accepted; required values must be present and request bodies are rejected. Error messages
never echo supplied values. This is refusal, not rewriting. Unmarked catalog entries retain
their prior behavior. A team's arbitrary own-tool raw relay is unchanged; catalog scope is
not a provider-wide restriction on tools a team explicitly registers itself.

The three profile tools prevent cheap-row overrides and `main=true&findEmail=true`.
Live combined flags returned a basic profile with no email at $0.004. `main` and `findEmail`
are only accepted as the literal required string `true` on their respective variants.

## Prices and evidence

The first 40 sequential data calls covered all 23 paths and cost $0.2412, taking the wallet
from $20 to $19.7588. Every initial call's top-level `cost` matched the immediate
`usage.balance` delta. Full profile was $0.0064, basic $0.004, email lookup $0.02 total,
lead search $0.10, ads $0.0016, jobs $0.001, groups $0.002, and the remaining paid reads
$0.004. Two geo-ID searches returned useful results for $0; this is observed account pricing.
Examples preserve response structure with synthetic identities/content, and truncate arrays.

Two further email probes used identifiers returned by profile search. One returned an email
for $0.02; another returned a real profile with no email and reported $0.0064 with
`payments: [linkedinProfile]`. Its immediate wallet read was unchanged, but a later read
reconciled to $19.7324. This proves a lower-priced no-email outcome, not why Harvest skipped
the email charge. The response does not establish whether this was insufficient information
or another internal condition. Wallet deltas are verification evidence, never a production
per-call meter.

Each paid tool uses the existing `cost.reported_charge: {path: cost, unit: usd}` rule,
introduced for Kitt. No Harvest branch or shared money change is needed. Finite nonnegative
charges, including zero and charges for misses, override the estimate. Missing/malformed
cost evidence uses the existing scalar fallback; this is documented rather than claimed
as exact observed usage. The profile variants reserve 6400, 4000 and 20000 micro-USD,
respectively, before configured margin. Geo-ID uses the existing free-price path.

## Mistakes the response permits

- A nonexistent profile returned HTTP 200 with body status 404, null element and a $0.004
  charge. A nonexistent company returned HTTP/body 200, null element, no error and $0.004.
- Empty company searches, comment replies and comment reactions were each billed $0.004.
  Empty data must not zero the charge. A malformed job ID and omitted post URL returned
  HTTP 200/body 400 at zero cost. Missing service-search input returned six results and
  cost $0.004, so the catalog declares that input required.
- Profile posts, comments and reactions returned real rows with `totalElements: 0` and
  `totalPages: 0`. Read the elements and follow returned pagination tokens.
- Company search page 2, profile posts page 2 with its token, and ads with the next token
  returned new IDs without overlap in the sampled pages.
- Profile search returned anonymized entries as well as linked profiles. It is not a
  substitute for lead search when users need broad identifiable results.

Five existing-contract adapters cover basic/full member profiles, company profiles,
email finding and post detail. They derive provider requests using existing identity
transforms and wrap results with the ordinary route disclosure. Adapter misses do not
cancel Harvest's reported charges. Email `verified` means Harvest reported `status: valid`,
not independent deliverability testing. No provider priority or automatic preference is added.

## Internal account health

`/users/my-api-user` is only the internal connection probe and capacity collector. A live
bogus key returned 401 and valid key 200; a valid zero balance must still connect.
`collectors._harvestapi` reads numeric nonnegative `usage.balance`; missing, malformed or
negative evidence is unknown. `user.totalBalance` stayed 20 while the wallet decreased.
The default policy is prepaid cash with `funding_mode=auto_recharge` and
`auto_funding_enabled=true`, based on the owner's planned production configuration.
The owner must enable and confirm auto top-up in Harvest before production use. This
repository setting does not enable vendor payments or verify the vendor setting.
Existing stored policy rows are not overwritten by `ensure_policies`; check any earlier
Harvest row during rollout and update it through the existing policy management process.
A confirmed zero balance still means exhausted until a new observation shows funds.

Starter documents five concurrent requests and a ten-request upstream queue, with no RPM
cap. These facts are catalog/collector guidance, not a newly implemented distributed
concurrency limiter. Existing burst handling remains unchanged. No exhaustion signature or
overflow route is invented; the signature coverage test records the unobserved case.

## Validation scope

All upstream tests used only Harvest's API key. No interaction/write calls were made.
The live run is endpoint coverage, not load/reliability certification. No depleted-wallet,
queue saturation or real timeout was forced. `tests/test_harvestapi.py` exercises key
verification, all-tier strict-query refusal, own-key priority and unmetered rejection,
platform reported-cost settlement (including billed misses, zero and malformed evidence),
variant reservations and hold release, and remaining-balance parsing. Existing marketplace,
Kitt reported-charge, routing, catalog and capacity suites cover unaffected providers.

## Local integration evidence

At the time of the original smoke check, the local dev-key file overrode
`TREG_PLATFORM_PROVIDERS`. The server was temporarily started with `harvestapi` enabled
and then restored. At the user's subsequent request to check the dashboard with root
configuration, that local provider-list override was removed. The root `.env` was not
edited. Dev-local was then stopped at the user's request and remains off.

A real bogus-key `/connections/token` attempt returned 422. Direct company lookup returned
its unmodified Harvest body and charged 4000 micro-USD. Routed company and member-profile
lookups returned the disclosed wrapper and charged 4000 micro-USD each; the member route
selected the basic profile variant. A connected own key then served company lookup with no
`treg` cost header and no treg balance change. Excluded cookie input was refused with 400
before an upstream request on both platform and BYOK catalog calls. The isolated test org
holding the connected key was deleted afterward.

The Render template declares the secret on the web service and forwards it to the capacity
cron using the same `fromService` pattern as other providers. No production setting was
changed, no deployment was performed, and no files were staged or committed.

### Repository check results

Original pre-sync regression run: 3258 passed and 5 skipped, with the following five unrelated failures
explicitly deselected after reproducing them on an untouched `HEAD` copy:

- `tests/test_daily_spend_counter.py::test_counter_resets_on_a_new_utc_day`
- `tests/test_usage_caps.py::test_runs_and_calls_share_one_gate`
- `tests/test_usage_caps.py::test_setting_a_cap_seeds_the_counter_from_todays_journal`
- `tests/test_usage_caps.py::test_yesterdays_usage_does_not_count_today`
- `tests/test_usage_caps.py::test_counter_agrees_with_the_journal_after_real_calls`

The run used `TREG_PUBLIC_URL=https://treg.to` and `TREG_OVERFLOW_MODE=off` to avoid
local `.env` overrides of the hosted-page and default-overflow assumptions. The initial
unadjusted run reproduced those environment-related failures on `HEAD` too. Catalog
validation passed across the entire catalog with no errors or warnings; all 14 import
contracts passed. Generated plugin skill mirrors match the served skill. The five baseline
failures were not presented as a clean unrestricted full-suite pass at that point.

### Main synchronization — 2026-09-10

The Harvest branch was fast-forwarded to `c1a321cb` (the fetched `origin/main`) and the
unstaged implementation reapplied. Sumble registration, configuration, capacity collection,
deployment entries and tests were retained alongside Harvest. Main's catalog display metadata,
generated headline counts, Arena changes and reservation-time route ceilings were preserved.
Plugin mirrors and the context index were regenerated against the combined catalog.

New Harvest regression cases cover direct and routed calls with a zero spending ceiling:
platform calls return 402 before upstream I/O or ledger changes; an own key still succeeds
without changing the treg balance. No additional shared runtime change was needed for sync.

The complete combined Python suite passed: **3528 passed, 6 skipped, no deselections**
(`TREG_PUBLIC_URL=https://treg.to TREG_OVERFLOW_MODE=off`, xdist). This includes the five
date-sensitive cases listed above; their earlier failures did not recur. A separate UTC run
of daily-spend, usage-cap and Harvest tests also passed all 75 cases. JavaScript tests passed
all 114 cases. Catalog validation passed for 97 files and 3273 endpoints with zero errors or
warnings; all 14 import contracts passed. No new live upstream requests were needed for
this synchronization. All implementation changes remain unstaged; dev-local remains off.

The subsequent main update to `e43e8406` brought in feedback handling models and revision
0030. Only the generated context map overlapped with Harvest; no Harvest code changes were
needed. All 232 targeted feedback, handling-schema, review, maintenance, deletion, call
architecture and Harvest tests passed, along with all 14 import contracts. The full-suite
result above belongs to the preceding synchronization; this incremental update used targeted
regression checks. Changes remain unstaged, and dev-local remains off.

### Final PR verification

After synchronization with main at `1061db3f` (release 0.19.0), the final full Python
suite passed **3532 tests with 6 skipped and no deselections**. It included the planned
auto-top-up policy, supplied logo and provider-neutral dashboard tool names/search.
All 14 import contracts and all five generated plugin mirror checks passed. Catalog
validation remained at 97 files and 3273 endpoints with no errors or warnings. Basic,
full and email tool names, prices, summaries and identifier instructions were checked
in the local dashboard. The server remains running for local review; production rollout
and the owner's vendor auto-top-up confirmation remain separate steps.


### PR main synchronization — 2026-09-11

Merged main at `3cf4c45a` into the Harvest branch. The only conflict was the generated
context map; rebuilding the map retained both branches' source coverage. Main's archive,
signup, SSRF and dashboard changes remain in place. No Harvest runtime changes were needed.

The full Python suite passed **3677 tests with 6 skipped and no deselections** using the
same environment settings as the final PR verification above. All **125 JavaScript tests**,
**14 import contracts** and **five generated plugin mirror checks** passed. Catalog validation
passed for **97 files and 3273 endpoints**, with zero errors or warnings. No new live upstream
calls were made for this synchronization.


### Arena category extension

The full profile adapter opts into `people.enrich` and the company profile adapter into
`companies.enrich`, using shared `additional_capabilities` verification. Their native LinkedIn
categories and direct-call IDs remain intact. The existing email adapter continues to serve
`people.email.find`. Arena selects these tools only for LinkedIn URL input, in both run modes.
Basic profiles are not added to person enrichment. These memberships also apply to public
routed enrichment tools. Mocked Arena execution tests cover platform and own-key credentials,
request mapping and charges in Battle and Waterfall.

Local extension verification: the full Python regression suite passed 3690 tests with 6 skipped
and no deselections. Two additional public-route cases were then added; all six cases in that
route test passed. All 14 import contracts, five plugin mirrors and catalog validation passed.
The local browser showed Harvest in all three LinkedIn-URL tasks and in both mode controls.

Live Arena verification on 2026-09-11 used one public example per run, with optional verification
off. Person Battle returned results from Harvest ($0.0064) and Aviato ($0.05); person Waterfall
stopped after Harvest ($0.0064), without calling Aviato. Email Battle returned the same address
from Harvest ($0.02) and Aviato ($0.08); QuickEnrich returned an exhausted-credit/allowance error
and charged zero treg credits. Email Waterfall continued past that error, stopped at Harvest,
and charged $0.02 without calling Aviato. Company Battle and Waterfall each returned Stripe
through Harvest for $0.004. Company tests had no competing eligible provider from the selected
local provider set. Total local team debit was $0.1908, matching the six run totals. Returned
emails were not independently verified. These checks establish the observed flow and charges,
not general provider accuracy or availability.
