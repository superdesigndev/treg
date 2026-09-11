---
title: ContactOut — LinkedIn enrichment, Starter billing and independent credit pools
status: implemented; live connect and core surface verified, informational capacity monitoring
sources:
  - src/treg/catalog/contactout.yaml
  - src/treg/catalog/adapters.yaml
  - src/treg/catalog/examples/contactout.people.email.verify.json
  - tests/test_routing.py
  - src/treg/catalog/examples/contactout.companies.search.json
  - src/treg/catalog/examples/contactout.companies.enrich.json
  - src/treg/application/call/resolve.py
  - src/treg/application/call/contactout.py
  - src/treg/web/logos/contactout.svg
  - tests/test_marketplace_call.py
  - tests/test_key_providers.py
  - tests/test_capacity_collectors.py
  - tests/test_catalog_validate.py
  - tests/test_capacity_overflow.py
  - scripts/contactout_overflow_verify.py
  - tests/fixtures/aggregators/verification/contactout.json
related:
  - architecture/catalog.md
  - architecture/auth-secrets.md
  - architecture/money.md
  - ops/capacity.md
---

# ContactOut

`oauth_providers.CONTACTOUT` is a pasted-key provider. It injects the raw credential in the
`token` header and verifies via free `GET /v1/stats`, requiring both HTTP success and
`status_code: 200`. A zero allowance does not invalidate a credential. The existing credential
ladder makes an org's tool/key win over the platform key and bypass treg billing and capacity checks.
`Settings.platform_key_contactout` reads `TREG_PLATFORM_KEY_CONTACTOUT`; `render.yaml` declares
the server slot and forwards it to the worker. The existing platform provider allow-list still
controls serving. No credential is committed or copied into a platform Secret row.

## Surface and selectors

Each tool has one catalog home: eight LinkedIn-specific lookup/contact tools on `linkedin`,
ten general people tools on `people`, and two company tools on `companies`. LinkedIn placement covers the three contact splits, three availability checkers,
LinkedIn profile enrichment and email-to-LinkedIn lookup. Their existing `contactout.people.*`
IDs remain stable for saved CLI/API calls; platform and capability metadata control browsing.
Capability labels distinguish work/personal email lookup from availability checks. Global catalog
search remains cross-platform. Email verification, company search and company enrichment
participate in their existing routed contracts. People search and profile enrichment remain direct-only: the PII rule excludes their
verification requests/examples, so their adapter registrations are omitted.

The catalog covers count, personal/work email and phone availability, single email verification,
people and company search, company domain enrichment, email-to-LinkedIn, decision makers,
LinkedIn contact/profile enrichment, person enrichment and profile-from-email.
Account stats are not a public catalog tool. `/v1/stats` remains the internal key probe
and balance collector; dashboard, CLI and MCP catalog discovery do not list it.
Free availability/count tools have distinct capabilities so they cannot be advertised as free
contact finders or profile searches. Work/personal contact splits use `email_type`; person
enrichment splits use `include`.
Phone-only contact uses `email_type=none&include_phone=true`. All split rows retain real upstream
paths. Platform calls must explicitly supply each required fixed selector; the resolver rejects a
mismatched selector before reserving or contacting the provider. BYOK keeps its ordinary relay.

Search `data_types` filters availability; it is NOT a reveal selector. Search/decision-maker
`reveal_info=true` can reveal both email types and phones. The work-only recipe is search without
reveal followed by `people.contact.work`. LinkedIn profile enrichment documents `profile_only`,
not `email_type`; profile-from-email only documents `include=work_email`. Those tools do not promise
work-only contact responses. The ContactOut logo was supplied by the account owner.

Deferred: synchronous/asynchronous LinkedIn batches (v1/v2), batch email verification. Hashed-email
and campaign endpoints are also outside this requested enrichment surface.

## Starter cost model

The account owner's supplied commercial rate is authoritative:

| Unit | USD |
|---|---:|
| Work email hit | 0.15 |
| Personal email hit | 0.25 |
| Phone hit | 0.25 |
| Profile/company returned by search; company found by domain enrichment | 0.02 |
| Email-to-LinkedIn served call | 0.06 |
| Count, availability, single email verification | 0 |

Contact charges are per profile with the requested type present, not per address/phone in its
array. Work plus phone is $0.40; personal plus phone is $0.50. Person enrichment adds $0.02 when
it finds a profile. Search and decision makers add $0.02 per returned profile even without reveal.
Availability booleans and `metadata.total_results` are never billing evidence. An echoed input
email and combined email arrays are not billed again as personal-email reveals.

`cost.contactout.rates_micro` carries integer micro-USD rates in YAML. `contactout.estimate`
calculates a hold from actual request selectors, input domains, or a maximum 25-row page.
Platform reveal search requires an explicit integer `page_size` from 1 to 25; one result reserves
at most $0.67. Decision makers has no page-size control: its maximum hold stays $16.75 with
reveal and $0.50 without reveal. Settlement charges actual results and releases unused funds.
`contactout.observed` derives the final charge from returned contacts and records. The existing
reserve/settle/release lifecycle owns all money and failure cleanup. This is derived billing,
not a claim that ContactOut reports dollar costs in each response. Responses without recognizable hit evidence, including malformed payloads,
misses and embedded errors, cost zero rather than settling the maximum contact hold.

LinkedIn enrichment with `profile_only=true` reserves $0.02 and settles $0.02 for a non-empty
profile. Empty/malformed profiles and failed envelopes settle zero. Full contact enrichment retains
contact-hit pricing; a contact miss costs zero at treg, although the vendor consumes a search
credit. Maintainer live verification found the previous zero-cost profile-only rule under-billed
two successful calls by one search credit each; `estimate` and `observed` now include that rate.
Single email verification remains free under current commercial terms; recheck if ContactOut
starts charging verifier credits. No public plan estimate replaces these commercial rates.

## Capacity: informational only

`collectors._contactout` reads `/v1/stats` through the existing worker sweep and balance script.
It preserves email, phone and search counters in the observation note with `value=None` and
`informational=True`. `snapshot_from` preserves that as a successful informational observation;
`latest_state` never derives exhaustion from it and ages it stale after the existing six-hour limit.
No independent scheduler or automatic purchase is added. Overflow candidates and documented
exhaustion handling are described below.
The existing sweep cadence remains unchanged; no new 15-minute polling schedule is installed.

The public docs distinguish two meanings: prepaid `quota` is already remaining credits; postpaid
`remaining` is quota minus count. Never subtract count from a prepaid quota. The three pools are
not interchangeable and cannot honestly become one provider-wide remaining balance. The account
owner confirmed the pools are independent. ContactOut's designated account manager monitors usage
and arranges top-ups at agreed volume pricing; ContactOut also sends low-credit email notifications.
Treg collects stats for visibility, without automatic purchases or pool-based blocking. Stats freshness
and support for 15-minute polling remain pending; the existing sweep cadence is unchanged.
The technical behavior at zero credits is not assumed from the managed top-up arrangement.

## Live evidence — 2026-09-08

Against `https://api.contactout.com/`, using the private root-env platform credential:

- Stats: valid credential HTTP 200; garbage credential HTTP 401. The full treg
  `/connections/token` flow returned 200 and 422 respectively in an isolated test database;
  stored test credentials were removed afterward.
- Count, all availability checkers, single verification: HTTP 200.
- No-reveal people search returned one profile; company search two companies; domain enrichment
  one company; no-reveal decision makers ten profiles. All HTTP 200.
- Synthetic LinkedIn targets returned 404 on work/personal/phone contact and person enrichment;
  LinkedIn profile enrichment returned 200 with an empty profile. Synthetic email targets returned
  404 on email-to-LinkedIn and profile-from-email. These prove miss behavior, not positive contact hits.
- Before: email count/quota 0/6000; phone 0/3000; search 0/5000. Immediately after: email and phone
  unchanged; search count/quota 0/4986. The 14-credit decline matches the returned search/company
  records and confirms prepaid quota semantics. It does not prove a general update-latency guarantee.

## Positive reveal verification — 2026-09-08

A bounded manual run used the funded root-env credential through treg's full platform call path,
an isolated pytest database/team, and a profile discovered through ContactOut company search.
Only field names, usage counters and balance deltas were logged; no contact values or credentials
were added to the repository. These were observed ledger deductions, not displayed estimates:

| Successful call | Treg deduction (USD) |
|---|---:|
| People search, one profile, all contact types revealed | 0.67 |
| LinkedIn work email plus phone | 0.40 |
| LinkedIn personal email | 0.25 |
| LinkedIn phone only | 0.25 |
| LinkedIn work email only | 0.15 |
| Full LinkedIn profile/contact enrichment | 0.65 |
| Person enrichment, work email | 0.17 |
| Person enrichment, personal email | 0.27 |
| Profile from email, phone returned and input email echoed | 0.25 |
| Email-to-LinkedIn | 0.06 |

The deductions match the agreed Starter rules for the observed fields. Email enrichment did not
bill the echoed input email again. Non-reveal one-profile searches deducted 0.02.
Contact, search and person-enrichment pool deltas were observed where stats succeeded: one email
credit for an email-bearing reveal, one phone credit for phone reveal, and one search credit for
search/person enrichment. The shared email pool does not separately expose work/personal credit
usage; these observations do not independently establish separate vendor dollar charges for both
types. Treg's commercial rates remain the account owner's supplied rates.

One stats request returned non-JSON HTML and interrupted the first multi-call verification pass;
a later pass succeeded. This does not establish a polling-frequency or freshness guarantee.
Decision-maker reveal and every optional-selector combination were not positively live-tested;
synthetic tests continue to cover their billing. Catalog prices remain commercial/documented,
not universally marked observed. Live connection verification was performed manually; the
shared test suite uses synthetic responses and does not run live ContactOut calls.

## Verified overflow routes

ContactOut permits overflow in the default policy. Existing stored policies are deliberately
not overwritten by `ensure_policies`; rollout must explicitly enable `overflow_allowed` on the
ContactOut policy, then run `treg-worker overflow sync`. The global mode and team opt-out remain
unchanged. BYOK is never eligible. No new request-path adapter or response rewrite was added.

The seed contains 26 candidates, of which 13 pass current verification and existing unit/ratio
rules: Orthogonal count, three LinkedIn contact splits, full LinkedIn enrichment, and work/personal
person enrichment; Monid count, three availability checkers, company search and domain enrichment.
Orthogonal's company/search per-call vs direct per-result mismatch is deliberately not bypassed.
Monid's work/personal contact wrappers returned extra `*_units` fields and failed shape comparison;
they remain unverified. Other candidates have no live stamp. No metadata listing is treated as
proof of response compatibility.

Orthogonal's `/details` was queried for every direct path: the listing response was incomplete.
Its headline $0.03 contact price is not the full charge. Live email-only calls cost $0.33, while
phone-only and work+phone cost $0.55. All three contact seeds reserve $0.55 to cover optional phone;
actual reported aggregator cost settles the child. Full LinkedIn/person enrichment cost $0.55.
Monid company-domain enrichment cost $0.018/result, company search returned two results for $0.036,
and free checkers/count cost zero. Direct-vs-relay shape/status/cost evidence, without identities,
is in `tests/fixtures/aggregators/verification/contactout.json`.

The documented 403 phrase `You're out of credits` is classified as endpoint quota exhaustion;
`No access to endpoint` is not a capacity signal. The existing Retry-After handling classifies
rate limits. Endpoint-level locks preserve independent credit pools. Source:
https://api.contactout.com/#errors (checked 2026-09-08).

### Renewal and rollout

People routes carry `untestable:` with no catalog test request or stored example under the PII
rule. These entries cannot participate in automated catalog re-verification.
`scripts/contactout_overflow_verify.py --budget-usd 10 --apply` discovers one profile
at runtime, builds requests using that ephemeral URL and required catalog selectors, compares
direct and aggregator shapes through the existing verifier, then syncs stamps.
It logs only endpoint IDs, statuses, verdicts and costs; no contact values. It makes paid calls;
the budget includes direct requests and conservative aggregator estimates. Missing keys are skipped,
failed compatibility routes are withheld from sync, and inconclusive routes retain their old stamp
and expire normally. Run this weekly in the worker environment in addition to the general verifier.
No recurring job was installed by this branch. Existing routes expire after seven days without a
successful verification/sync. Use existing shadow mode and daily budgets before production `on`;
do not change the global mode merely to test this provider in an existing deployment.

## Shared email verification

`contactout.people.email.verify` participates in `treg.people.email.verify` through the existing
`adapters.yaml` mechanism. Input `email` maps to `queryParams.email`; `data.status` is retained as
`status`, and only `valid` maps to `valid: true`. `invalid`, `accept_all`, `disposable`, and `unknown`
retain their original status words with `valid: false` (not confirmed deliverable, not a claim
that every such address is invalid). Unsuccessful envelopes and absent/empty verdicts are misses
and allow the existing waterfall to continue. The verifier itself needs no new runtime or
expression helpers.

The direct rate remains free. Existing ranking selects eligible own keys first, then platform
candidates by expected cost; ContactOut is a free platform candidate when configured. Other
providers may still charge if selected by preferences, own-key availability, or fallback.

On 2026-09-09, a free authenticated `GET /v1/email/verify` for the catalog's public support address
returned HTTP 200 with `status_code: 200` and `data.status: accept_all`. The complete response is
recorded in `examples/contactout.people.email.verify.json`; no credential is included. Shared
routing tests cover all five documented verdicts, query preservation, zero cost, provenance,
and fallback on missing verdicts or embedded errors.

## Shared discovery and profile routing

Company adapters join the existing contracts without changing capability labels. The three people
adapter registrations are omitted after removal of PII-bearing catalog fixtures; direct calls
remain available. Tests assert their absence from shared routing; direct platform tests cover
profile-only billing and own-key exclusion. No verification gate is bypassed:

| Routed tool | ContactOut child | Selected behavior |
|---|---|---|
| Ineligible: `treg.people.search` | `contactout.people.search` | `reveal_info=false`; domain/title/name/keyword identities; page size and location filters |
| `treg.companies.search` | `contactout.companies.search` | Domain/name/industry/technology identities; vendor page size retained |
| `treg.companies.enrich` | `contactout.companies.enrich` | One domain, sent as a one-element `domains` array |
| Ineligible: `treg.people.enrich` | `contactout.people.enrich` | LinkedIn URL or email; `include=[]` prevents contact reveal |
| Ineligible: `treg.linkedin.user.profile` | `contactout.people.linkedin.enrich` | LinkedIn URL or handle; `profile_only=true` |

These mappings do not route decision-maker search, personal-email splits, or combined-reveal
variants. The direct provider tools retain those capabilities. Company search does not document
a page-size control: the adapter does not forward the contract's `limit` as an invented parameter.
Its returned companies are still metered at $0.02 each. People search and domain enrichment use
$0.02 per returned result; person enrichment uses $0.02 when found with no contact charge;
LinkedIn profile-only lookup costs $0.02 when a profile is found.

Object-keyed response rows use the reusable `values` and `get` expression helpers in
`routing/paths.py`. `values` reads dictionary values or preserves a list; `get` applies the existing
dotted/indexed path lookup to an expression result. Neither helper contains provider logic. Raw
responses remain unchanged. Empty rows, missing required identity fields, and embedded errors
are misses under the existing routing waterfall.

Live captures on 2026-09-09 used the private funded key: people search returned one profile,
company search two companies, domain enrichment one company, and person/LinkedIn enrichment
one profile each. The first sample LinkedIn URL missed, so the two successful profile captures
used a profile from that search. All captured contact arrays were empty with the above selectors.
The people captures and their catalog requests were removed after maintainer review under the
catalog PII rule; only company and email-verification examples remain. People tools are marked
`untestable:` to prevent re-capture. Other accepted company input variants were not separately live-verified.
