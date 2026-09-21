---
title: Endpoint catalog — what you can DO with a connected key, and which provider should do it
status: shipped
sources:
  - src/treg/catalog/tavily.yaml
  - src/treg/catalog/exa.yaml
  - src/treg/catalog/anyapi.extended.yaml
  - src/treg/catalog/examples/tavily.web.search.json
  - src/treg/catalog/examples/tavily.web.extract.json
  - src/treg/catalog/examples/tavily.web.map.json
  - src/treg/catalog/examples/tavily.web.crawl.json
  - src/treg/web/logos/tavily.svg
  - src/treg/catalog/trestleiq.yaml
  - src/treg/catalog/examples/trestleiq.people.phone.verify.json
  - src/treg/catalog/examples/trestleiq.people.contact.verify.json
  - src/treg/catalog/examples/trestleiq.people.address.verify.json
  - src/treg/web/logos/trestleiq.svg
  - src/treg/catalog/financialdatasets.yaml
  - src/treg/catalog/examples/financialdatasets.company.facts.json
  - src/treg/catalog/examples/financialdatasets.company.facts.ciks.json
  - src/treg/catalog/examples/financialdatasets.company.facts.tickers.json
  - src/treg/catalog/examples/financialdatasets.earnings.json
  - src/treg/catalog/examples/financialdatasets.earnings.tickers.json
  - src/treg/catalog/examples/financialdatasets.filings.json
  - src/treg/catalog/examples/financialdatasets.filings.ciks.json
  - src/treg/catalog/examples/financialdatasets.filings.tickers.json
  - src/treg/catalog/examples/financialdatasets.filings.types.json
  - src/treg/catalog/examples/financialdatasets.financial-metrics.json
  - src/treg/catalog/examples/financialdatasets.financial-metrics.snapshot.json
  - src/treg/catalog/examples/financialdatasets.financial-metrics.snapshot.tickers.json
  - src/treg/catalog/examples/financialdatasets.financials.json
  - src/treg/catalog/examples/financialdatasets.financials.balance-sheets.json
  - src/treg/catalog/examples/financialdatasets.financials.cash-flow-statements.json
  - src/treg/catalog/examples/financialdatasets.financials.income-statements.json
  - src/treg/catalog/examples/financialdatasets.financials.income-statements.segments.json
  - src/treg/catalog/examples/financialdatasets.financials.search.screener.json
  - src/treg/catalog/examples/financialdatasets.financials.search.screener.filters.json
  - src/treg/catalog/examples/financialdatasets.insider-trades.json
  - src/treg/catalog/examples/financialdatasets.index-funds.json
  - src/treg/catalog/examples/financialdatasets.index-funds.tickers.json
  - src/treg/catalog/examples/financialdatasets.institutional-holdings.json
  - src/treg/catalog/examples/financialdatasets.institutional-holdings.investors.json
  - src/treg/catalog/examples/financialdatasets.institutional-holdings.tickers.json
  - src/treg/catalog/examples/financialdatasets.ipos.json
  - src/treg/catalog/examples/financialdatasets.kpi.guidance.json
  - src/treg/catalog/examples/financialdatasets.kpi.metrics.json
  - src/treg/catalog/examples/financialdatasets.kpi.non-gaap.json
  - src/treg/catalog/examples/financialdatasets.macro.interest-rates.json
  - src/treg/catalog/examples/financialdatasets.macro.interest-rates.banks.json
  - src/treg/catalog/examples/financialdatasets.news.json
  - src/treg/catalog/examples/financialdatasets.prices.json
  - src/treg/catalog/examples/financialdatasets.prices.snapshot.json
  - src/treg/catalog/examples/financialdatasets.prices.snapshot.tickers.json
  - src/treg/catalog/examples/financialdatasets.prices.tickers.json
  - tests/test_financialdatasets.py
  - src/treg/catalog/quickenrich.yaml
  - src/treg/catalog/quickenrich.extended.yaml
  - src/treg/catalog/examples/quickenrich.companies.search.json
  - src/treg/catalog/examples/quickenrich.people.email.find.json
  - src/treg/catalog/examples/quickenrich.people.enrich.json
  - src/treg/catalog/examples/quickenrich.people.phone.find.json
  - src/treg/catalog/examples/quickenrich.people.search.domain.json
  - src/treg/catalog/examples/quickenrich.people.search.json
  - src/treg/catalog/examples/quickenrich.x.company-services.json
  - src/treg/catalog/examples/quickenrich.x.country-codes.json
  - src/treg/catalog/examples/quickenrich.x.employee-ranges.json
  - src/treg/catalog/examples/quickenrich.x.industries.json
  - src/treg/catalog/examples/quickenrich.x.revenue-ranges.json
  - src/treg/catalog/trykitt.yaml
  - src/treg/catalog/examples/trykitt.people.email.find.json
  - src/treg/catalog/examples/trykitt.people.email.verify.json
  - src/treg/catalog/contracts.yaml
  - src/treg/catalog/millionverifier.yaml
  - src/treg/catalog/examples/millionverifier.people.email.verify.json
  - src/treg/catalog/examples/millionverifier.account.usage.json
  - src/treg/catalog/adapters.yaml
  - src/treg/catalog/prospeo.yaml
  - tests/test_route_cost_ceiling.py
  - src/treg/catalog/tomba.yaml
  - src/treg/catalog/examples/tomba.people.email.verify.json
  - src/treg/catalog/examples/findymail.search.business-profile.json
  - src/treg/domain/catalog/routing/__init__.py
  - src/treg/domain/catalog/routing/contracts.py
  - src/treg/domain/catalog/routing/paths.py
  - src/treg/domain/catalog/routing/plan.py
  - src/treg/domain/catalog/routing/synthetic.py
  - src/treg/application/call/route.py
  - tests/test_routing.py
  - .github/workflows/catalog-drift.yml
  - scripts/catalog_drift.py
  - scripts/catalog_ingest.py
  - scripts/catalog_validate.py
  - src/treg/catalog/aliases.yaml
  - src/treg/catalog/fx.yaml
  - src/treg/catalog/cloro.yaml
  - src/treg/catalog/aviato.yaml
  - src/treg/catalog/crustdata.yaml
  - src/treg/catalog/examples/aviato.companies.acquisitions.json
  - src/treg/catalog/examples/aviato.companies.employees.json
  - src/treg/catalog/examples/aviato.companies.enrich.bulk.json
  - src/treg/catalog/examples/aviato.companies.enrich.json
  - src/treg/catalog/examples/aviato.companies.founders.json
  - src/treg/catalog/examples/aviato.companies.funding_rounds.json
  - src/treg/catalog/examples/aviato.companies.investments.json
  - src/treg/catalog/examples/aviato.companies.outbound_investments.json
  - src/treg/catalog/examples/aviato.companies.search.json
  - src/treg/catalog/examples/aviato.linkedin.company.posts.json
  - src/treg/catalog/examples/aviato.linkedin.post.comments.json
  - src/treg/catalog/examples/aviato.linkedin.post.reactions.json
  - src/treg/catalog/examples/aviato.linkedin.post.reposts.json
  - src/treg/catalog/examples/aviato.linkedin.user.posts.json
  - src/treg/catalog/examples/aviato.people.contact.get.json
  - src/treg/catalog/examples/aviato.people.email.find.json
  - src/treg/catalog/examples/aviato.people.enrich.bulk.json
  - src/treg/catalog/examples/aviato.people.enrich.json
  - src/treg/catalog/examples/aviato.people.phone.find.json
  - src/treg/catalog/examples/aviato.people.search.json
  - src/treg/catalog/examples/aviato.people.search.simple.json
  - src/treg/catalog/examples/crustdata.companies.autocomplete.json
  - src/treg/catalog/examples/crustdata.companies.enrich.json
  - src/treg/catalog/examples/crustdata.companies.identify.json
  - src/treg/catalog/examples/crustdata.companies.jobs.search.json
  - src/treg/catalog/examples/crustdata.companies.search.json
  - src/treg/catalog/examples/crustdata.people.autocomplete.json
  - src/treg/catalog/examples/crustdata.people.enrich.json
  - src/treg/catalog/examples/crustdata.people.search.json
  - src/treg/catalog/google-search-console.yaml
  - src/treg/catalog/google-search-console.extended.yaml
  - src/treg/catalog/google-tag-manager.yaml
  - src/treg/catalog/google-tag-manager.extended.yaml
  - src/treg/catalog/instagram.yaml
  - src/treg/catalog/instagram.extended.yaml
  - src/treg/catalog/justoneapi.extended.yaml
  - src/treg/catalog/minimax.yaml
  - src/treg/catalog/apify.yaml
  - src/treg/catalog/brightdata.yaml
  - src/treg/catalog/companyenrich.yaml
  - src/treg/catalog/oceanio.yaml
  - src/treg/catalog/akta.extended.yaml
  - src/treg/catalog/dataforseo.yaml
  - src/treg/catalog/dataforseo.extended.yaml
  - tests/test_dataforseo_constraints.py
  - src/treg/catalog/scrapecreators.yaml
  - src/treg/catalog/scrapecreators.extended.yaml
  - src/treg/catalog/serpapi.yaml
  - src/treg/catalog/serpapi.extended.yaml
  - src/treg/catalog/diffbot.yaml
  - src/treg/catalog/diffbot.extended.yaml
  - src/treg/catalog/tikhub.extended.yaml
  - src/treg/catalog/lusha.extended.yaml
  - src/treg/catalog/examples/minimax.video-gen.result.retrieve.json
  - src/treg/catalog/examples/minimax.video-gen.from_image.json
  - src/treg/catalog/examples/minimax.video-gen.task.status.json
  - src/treg/catalog/examples/minimax.voice-gen.voices.list.json
  - src/treg/catalog/openrouter.yaml
  - src/treg/catalog/openrouter.extended.yaml
  - src/treg/catalog/examples/openrouter.x.alibaba-wan-3-0.json
  - src/treg/catalog/examples/openrouter.video-gen.seedance-2-5.json
  - src/treg/catalog/replicate.yaml
  - src/treg/catalog/replicate.extended.yaml
  - src/treg/catalog/reapi.yaml
  - src/treg/catalog/piapi.yaml
  - src/treg/catalog/examples/replicate.image-gen.nano-banana-pro.json
  - src/treg/catalog/examples/replicate.image-gen.gpt-image-2.json
  - src/treg/catalog/examples/replicate.image-gen.gpt-image-2-5-flare.json
  - src/treg/catalog/examples/replicate.image-gen.gpt-image-2-5-sunburst.json
  - src/treg/catalog/examples/reapi.tasks.get.json
  - src/treg/catalog/examples/reapi.video-gen.seedance-2-5.json
  - src/treg/catalog/examples/reapi.video-gen.seedance-2-5.unrestricted.json
  - src/treg/catalog/examples/reapi.image-gen.gpt-image-2-5.json
  - src/treg/catalog/examples/reapi.image-gen.gpt-image-2.json
  - src/treg/catalog/examples/reapi.image-gen.gemini-3-pro-image.json
  - src/treg/catalog/examples/piapi.task.get.json
  - src/treg/catalog/examples/piapi.video-gen.seedance-2-5.json
  - src/treg/catalog/examples/piapi.video-gen.seedance-2-5.less-restriction.json
  - src/treg/catalog/examples/piapi.image-gen.gemini-3-pro-image.json
  - src/treg/catalog/examples/piapi.image-gen.gpt-image-2-5.json
  - src/treg/catalog/examples/piapi.image-gen.gpt-image-2.json
  - src/treg/catalog/examples/replicate.image-gen.flux-schnell.json
  - src/treg/domain/catalog/__init__.py
  - src/treg/domain/catalog/store.py
  - src/treg/catalog/hunter.yaml
  - src/treg/mcp.py
  - src/treg/domain/money/settlement.py
  - src/treg/domain/catalog/stats.py
  - src/treg/infra/catalog_observations.py
  - src/treg/application/catalog_stats.py
  - src/treg/alembic/versions/0038_endpoint_day_stats.py
  - src/treg/routers/catalog.py
  - tests/test_aigc_pr_b.py
  - tests/test_catalog_api.py
  - tests/test_catalog_validate.py
related:
  - architecture/money.md
  - architecture/proxy-model.md
  - interface/cli.md
---

# Endpoint catalog — platform-grouped operations per provider

## Tavily web tools (2026-09-21)

Tavily contributes exactly four public data tools: Search, Extract, Map, and Crawl. Research,
account usage, logs, feedback, organization usage, and key-management routes are not catalogued.
All four are verified catalog tools supporting a team's own Bearer key and the optional platform
key, but only Search joins the automated `treg.web.search` route. Extract, Map, and
Crawl remain direct-only: their grouped account-level debit can land on a later call, and their
bounded shared-key holds are materially less competitive than the existing automatic choices.
None is an Enrich Arena task because Arena is limited to its declared enrichment jobs.

Every Tavily call requires the caller to send `include_usage: true`, keeping the catalog contract
consistent across own-key and platform calls; platform settlement then uses Tavily's response-reported
`usage.credits`. Map and Crawl additionally require an explicit `limit` from 1 through 20 on the
platform key. That bound caps the maximum hold while leaving Tavily's accepted range unchanged for
BYOK. Search reserves at most two credits; Extract, Map, and Crawl reserve their documented
request-specific ceilings. A finite nonnegative reported charge, including zero, wins at settle;
missing or malformed evidence falls back to the hold. The credit conversion uses Tavily's stable
PAYGO replacement rate from `fx.yaml`, not free monthly credits or a discounted subscription tier.

The four redacted hit fixtures were captured from live calls. Separate live probes established a
charged Search hit, a charged empty Search miss, a charged Extract hit, a free failed Extract URL,
empty Map/Crawl responses, HTTP 422 for missing required input, and HTTP 401 for a bogus key. No
Tavily 4xx is classified as a routing miss: honest misses are successful envelopes with an empty
`results` array (or Extract `failed_results`). Tavily may report zero credits before a grouped
Extract/Map/Crawl threshold and debit the whole group on a later response; treg deliberately bills
the exact response-reported debit rather than inventing an allocation. `/usage` may lag that
evidence. A controlled burst did not reproduce the documented 429, and exhausting the funded
account was not forced; 429, 432, and 433 therefore retain documented rather than observed status.

TrestleIQ adds three synchronous, non-bulk direct tools for phone numbers, contact details, and US
addresses. All three support own keys and the platform key. None has a routing adapter, so TrestleIQ
does not appear in automated `treg.*` routes or Enrich Arena. Paid add-ons and gated products stay
out of the catalog. Trestle returns a billable HTTP 200 for missing required inputs; catalog-required
selectors and `strict_query` reject those malformed calls before relay on both the platform key and
a team's own key.

LimaData exposes 21 of 24 Basic v2 operations. Fourteen fixed, synchronous operations can use the
shared key; variable and 404-billed operations require a team's own key. Account-scoped batch
submission and result operations are omitted. Six
fixture-verified adapters join existing routing and Enrich Arena contracts.

## BounceBan email verification (2026-09-16)

BounceBan adds four tools across standard single verification, BYOK waterfall verification, BYOK
single-result polling, and account usage. Only the standard single tool
is platform eligible. It has a fixed observed cost of one credit, priced at the supplied acquisition
rate of $0.004, and uses `per_call` so an accepted `status=verifying` submission is charged while a
rejected HTTP 400 request releases its hold. Waterfall retries, conditional zero-credit catch-all
results make waterfall unsafe for a shared key. Bulk submission and lifecycle operations are
omitted until an explicit cost-confirmation and owned-job workflow exists.

The verified adapter adds only the standard endpoint to `treg.people.email.verify`; routing and
Arena discover it from that adapter. Multipart upload, destructive bulk deletion, and the separately
funded Check API are not catalog tools.

## Datagma single-record enrichment (2026-09-18)

Datagma contributes five read-only, non-bulk catalog tools: verified work-email finding, basic
person enrichment, company enrichment, mobile finding, and job-change detection. All five support
both a team's key and the platform key. The account route is internal-only, and `find_people` is
not implemented. Verified adapters place email, person, and company enrichment in their existing
routed tools and Enrich Arena tasks. Mobile stays direct-only because its verified 30-credit cost
is not competitive for automatic routing; job-change detection has no corresponding route.

The credit rate is the assigned prepaid acquisition cost converted at the dated ECB reference
rate. Responses settle from Datagma's `creditBurn`, including zero-cost cached hits.

## ZeroBounce email verification (2026-09-17)

ZeroBounce adds single email validation plus BYOK-only credit and usage reads. Single validation is
the only platform-eligible tool. Its one-credit `per_success` price uses the supplied $69 / 5,000
replacement rate. The verified adapter adds it to `treg.people.email.verify`: unknown is a free
routed miss, while invalid and risk verdicts remain answers. Batch is excluded because live tests
showed that it needs the key in its JSON body, which the faithful relay does not rewrite. File,
state-changing, and ambiguous-price operations are also outside the safe first surface.

MoltSets adds 12 verified data tools: nine single-result shared-plan offers and three BYOK-only
variable-result searches. Five hybrid scalar/batch email and phone operations are omitted.

Sumble adds the full v9 surface with verified platform operations and explicit BYOK restrictions.

GetLeads.io adds 11 direct contact-data tools. Every tool accepts BYOK or a $0 platform trial with
five successful credit-using calls per team per day; its two free discovery tools do not consume
that allowance. Three enrichment tools keep the upstream `items` array but opt into `strict_body`
with exactly one item. `_enforce_catalog_body` rejects invalid cardinality on every credential tier
without rewriting an accepted request. The allowance counts calls rather than returned records or
upstream credits. Internal
account routes, stateful exports and monitoring are excluded.

## Financial Datasets v1 and v2 (2026-09-15)

`financialdatasets.yaml` adds 36 direct tools to the existing Market data / Stock Market Data
catalog: 22 data operations and 14 dataset-specific discovery helpers. Company facts and the other
standard data requests settle at $0.02 per successful platform call; KPI metrics, KPI guidance,
non-GAAP data, and IPOs settle at $0.16. The 14 discovery helpers are free because their verified
public upstream routes use the generic anonymous platform fallback. BYOK calls retain the normal
unmetered precedence and still win before that fallback.

Company, fundamentals, filing, ownership, earnings, news, and equity-price inputs are described as
US stock tickers; the Index Funds data tool instead accepts an ETF or index-fund ticker or a held
US security ticker. The free ticker, CIK, filing-type, investor, screener-filter, and bank helpers use
the existing `utility` kind because they enumerate valid inputs rather than return the primary
financial result; the dashboard folds them into its management/utility accordion while they remain
directly callable. treg does not call them as hidden preflights. Each one declares
`platform_auth: anonymous`, so the shared resolver builds a virtual tool with no credential binding;
there is no Financial Datasets branch in the relay. Each data input with a matching included helper
names that exact utility tool ID in its agent-facing note, so dashboard and CLI users can discover
valid values without assuming one dataset's coverage applies to another. Interest-rate data covers
the provider's listed major central banks globally. The catalog does not claim forex, options, or
general multi-asset coverage.

Seventeen list endpoints accept the provider's opaque `cursor`. Their agent-facing input notes tell
callers to take it from the response `next_page_url` and omit the original filters on the next call,
because the cursor preserves those filters. treg still relays the cursor and response unchanged.

Only `financialdatasets.prices.snapshot` joins a routed capability. Its adapter maps
`treg.stocks.quote.live`'s `symbol` to `queryParams.ticker`, uppercases it, reads the required numeric
`snapshot.price`, and preserves the provider object as `quote`. No new routed contract, category,
provider-specific router, or response model is introduced. Captured fixtures and
`tests/test_financialdatasets.py` verify the direct surface, fixed settlement, BYOK behavior, and
the existing quote route. One live request for each of the original 34 direct tools returned HTTP
`200` on 2026-09-15. A second pass captured the 18 response fixtures that were not already present;
the V2 checks described below supplied the two new fixtures, so every verified tool now has live
response evidence. The responses exposed no usage, credit, charge,
rate-limit, pagination-header, or request-ID evidence; paginated response bodies expose
`next_page_url` when another page exists. The provider later settled one authenticated 34-tool pass
at $1.24, including $0.26 for the 13 discovery requests. Three complete anonymous discovery passes
returned 200 without changing the provider balance; a separate authenticated 13-request pass cost
exactly $0.26. This proves that omitting the key, rather than a zero rate on keyed traffic, makes the
discovery surface free.

That free result is conditional on the request having no caller-supplied provider credential. The
anonymous virtual tool injects no key, but the faithful relay does not strip caller headers. A
caller who sends `X-API-KEY` can therefore spend that key's Financial Datasets Credits.

V2 adds `financialdatasets.index-funds` and its anonymous
`financialdatasets.index-funds.tickers` discovery helper. The data tool supports both provider
query directions: a fund ticker returns constituents and weights, while a held security ticker
returns funds that hold it. `as_of` and `asset_class` apply only to the fund-ticker direction. The
provider returns at most ten rows per page even when `limit` is larger; callers continue with the
opaque cursor from `next_page_url`. Live checks returned 200 for the SPY fund direction, the AAPL
holding direction, both pages of an eleven-row request, and the anonymous ticker helper. The
authenticated Index Funds request settled at the existing standard $0.02 rate; anonymous ticker
discovery did not use the provider account. Invalid requests with neither query direction or both
`ticker` and `holding` returned HTTP 400, so per-success settlement releases their holds. All 36
Financial Datasets documentation links were matched to the provider's current index and returned
HTTP 200 after its move from `/api-reference/` to route-specific `/api/` pages.

The computed cost view uses a `cost.table` fallback as its scalar validated upper bound for
eligibility and compact displays. Runtime charging evaluates the first matching row against request
values plus catalog defaults and freezes that settlement basis. Terminal usage or the recorded table
evidence feeds the shared money settlement function; provider variation stays declarative in YAML.

## QuickEnrich enrichment (2026-09-08)

`quickenrich.yaml` exposes email and phone finding, reverse email, people at a domain,
free contact discovery and company search. `quickenrich.extended.yaml` contains the five
public lookup utilities (countries, industries, employee ranges, revenue ranges, services).
This is the complete documented API surface; no account-usage or SMTP-verification endpoint
is invented. Company Finder accepts a domain filter but remains a search tool, not a duplicate
company-enrichment listing. Six adapters add the core tools to five existing routed capabilities:
`people.email.find`, `people.phone.find`, `people.enrich` (email input only), `people.search`
(discovery and domain search), and `companies.search` (domain, name or industry inputs).
Free discovery returns profiles and availability flags, not revealed emails or phones. It can
satisfy a people-search request without a paid reveal. Domain search retains its fixed 20-row
page; its adapter quotes one credit without a title and up to 20 with a title, independently of
`limit`. The router discloses unsupported filters, including the domain route's row limit.
No company-enrichment, email-verification or lookup-utility adapter is added.
The three paid single-person adapters use fixed synthetic hit fixtures matching the successful
field shapes recorded in the initial live checks. Catalog loading therefore verifies their output
paths on a hit; a miss fixture can no longer make those adapters eligible while leaving the hit
mapping unchecked. The fixtures use reserved example contact values and omit account balances.
The phone adapter retains `data.country_code` as the provider's reported country context (company
metadata, not proof of the phone owner's location). `people.phone.verify` accepts optional ISO-2
`country_code`, and Tomba forwards it for national-number parsing. International numbers need no
country hint; the phone verification verdict still establishes format only, not identity or reachability.

A free-plan key was supplied and verified against `https://app.quickenrich.io`; the alternative
marketing hostname `api.quickenrich.io` is unnecessary. The authenticated Contact Finder probe
returns 401 `Invalid or inactive API key` for a bogus key and 200 with `credits_used: 0` for a
valid key. This satisfies key-in-hand verification; self-serve provisioning of a new Growth key
was not independently tested. Setup copy retains the docs' support fallback.

Discover first with `quickenrich.people.search`, then selectively call email/phone find using
the returned profile URL or name/company. Discovery exposes availability flags, not email/phone
values. Contact filters use `industry_linkedin`, company filters use `industry`; Company Finder's
`company_url` is a string, unlike the discovery include/exclude object. Lookup values must match
exactly. Finder pagination defaults to 10 and caps at 100 rows; `meta.next_cursor` overrides page
and supports deep pagination according to docs. The actual Free subscription reports `max_pages: 5`;
paid deep-pagination behavior has not been exercised.

Billing evidence from the initial live run (credits before 300, after 289):

| Request | Observed credits |
|---|---:|
| Five public lookups; one-contact discovery | 0 |
| Email hit / phone hit / reverse-email hit | 1 each |
| Email miss / phone miss / empty domain | 0 each |
| Domain without title: 20 returned | 1 flat |
| Domain with title: 8 returned, 6 with email/phone | 6 |
| Company search: 1 result / empty result | 1 / 0 |

The standard verifier subsequently passed every core and utility test request. Total live
verification used 18 trial credits, leaving 282. A live request through treg’s `/connections/token`
returned 422 for the bogus key and provisioned no tool. Reverse misses
must use a valid mail domain: `.invalid` and `example.com` were rejected with HTTP 422 by the
upstream email validator; a unique nonexistent address at `stripe.com` returned a free 200 miss.
Captured public examples have contact names, email, phone and personal profile URLs replaced
with synthetic values. Shared billing tests use small inline payloads, following the existing
provider tests. Dollar provenance stays documented: the live meter
proved credit counts, not cash spent on the free account.

The base list rate in `fx.yaml` is $0.004834 per credit before configured platform margin.
It uses the purchased Starter monthly plan: $29 / 6,000 credits, rounded up to 4,834 micro-USD.
This assumes all monthly credits are used; unused credits increase effective cost. Direct tools,
settlement and routed estimates share this rate.
Free/Starter/Growth are subscription allowances; GTM Unlimited is a subscription with no finite
API allowance. The free subscription does not make billed enrichment a treg trial-priced product.
See [money](money.md) for reservation/settlement and [capacity](../ops/capacity.md) for renewal
and API balance reporting. Unlimited-plan status requires live verification; missing balance data remains unknown.
Starter has been purchased. Six routed live checks on Starter used three credits and confirmed
the existing response and credit rules. This price change does not enable production.

## MillionVerifier email verification (2026-09-08)

`millionverifier.yaml` adds single-email verification and the free own-account credit probe.
The USD price is documented at $89 / 50,000 prepaid credits ($0.00178 each), with no expiry.
The initial three live verification requests consumed three credits. Subsequent account-ledger
evidence shows deductions followed by separate goodwill credits for risky results, including
six catch-all credits returned after three ten-email bulk format tests (30 deducted, six returned).
Those bulk tests are evidence only, not catalog support. Immediate balance probes are not a
per-call meter. The account owner later confirmed $89 for the base 50,000-credit pack.
The catalog rate excludes initial free credits and variable promotional bonuses; it is not
the effective cost after bonuses. No receipt was inspected, so `confidence: documented` and
`source: docs` remain appropriate.

`adapters.yaml` adds `millionverifier.people.email.verify` to the existing
`treg.people.email.verify` contract beside Hunter, LeadMagic and Tomba. `ok` maps to valid;
other verdicts remain provider-native status words. Like the peer adapters, a risky or invalid
verdict is an answer, while error bodies (no `quality`) are misses. `settle._observed_cost_micro`
separately makes unknown/catch-all results free. The upstream `free` flag means a free email
service, and `credits` is a delayed balance; neither is per-call usage.

ContactOut also joins this contract via `contactout.people.email.verify`. Its direct price is free
under the agreed commercial terms. The captured `accept_all` response verifies the adapter; only
`valid` confirms deliverability, other status words remain intact, and unsuccessful envelopes or
missing verdicts fall through.

Bulk upload, file info/list/download, stop and delete are excluded: those operations use
`bulkapi.millionverifier.com` with `key` auth and a multipart file lifecycle, rather than this
provider's Single API host and `api` auth. The YAML records the complete eight-operation map.

## Tomba email verification (2026-09-08)

Tomba email verification uses `GET /v1/email-verifier?email=…`; its catalog input and routing
adapter both send `email` in query parameters. A live comparison with the same address and
credentials returned HTTP 200 with a verification verdict on this documented query route and
HTTP 422 `params_invalid` on the former `/v1/email-verifier/{email}` path. The response fixture
captures the returned verdict fields; the mapping remains `data.email.status` / `data.email.score`.
Historical failure-only samples do not establish coverage for the corrected request shape.

## Authorization metadata

Tomba email verification uses `GET /v1/email-verifier?email=…`; its catalog input and routing
adapter both send `email` in query parameters. A September 8, 2026 live comparison with the same
address and credentials returned a valid verification response on this documented query route
and HTTP 422 `params_invalid` on the former `/v1/email-verifier/{email}` path. The response
mapping remains `data.email.status` / `data.email.score`. Historical failure-only samples do not
establish coverage for the corrected request shape.

An endpoint can declare `authorization_method`, ordered `authorization_methods`, method-specific
`authorization_paths`, `required_scopes`, `required_resource`, and `token_type`. `_normalize`
keeps these fields on the internal row and exposes them on endpoint detail only when present.
Marketplace resolution uses them for preflight and grant selection. Instagram is the first user;
its 32-row audit is in [instagram-oauth](instagram-oauth.md).

Meta's published reference is not available as a machine-readable OpenAPI document. Reviewed
Instagram `input` and authorization contracts are therefore curated catalog data, and ingestion
carries them forward instead of erasing them on a later scrape.
Instagram is also parameter-multiplexed: profile lookup and business discovery intentionally share
`GET /{ig_user_id}`; the required `fields=business_discovery...` value selects the latter operation.

## Why

The marketplace registry (`oauth_providers.py`) catalogs *credentials*: how to connect a provider.
It says nothing about what you can DO once connected — which endpoints exist, what they cost, what
they return. Agents guess paths from external docs and burn paid calls. This layer answers that,
and it runs through the team's OWN keys: every call is proxied, governed and audited.

The catalog adds that operations layer:

- **platform** (tiktok, instagram, google, web, …) → the marketplace grouping axis: click TikTok,
  see every provider + endpoint that serves TikTok data.
- **capability** (`tiktok.user.profile`) → the same operation across providers, so a user can
  compare TikHub vs JustOneAPI for one job, and a future router can fail over between them.
- **verified example responses** → captured during live testing, because docs show request params
  but choosing an API comes down to what actually comes back.

Crustdata and Aviato support both BYOK and treg's platform-key tier. Their catalog costs stay in the
vendors' native credits; `fx.yaml` converts the actual replacement rates treg pays ($0.30 per
Crustdata credit from the configured 500-for-$150 auto-top-up, $0.01 per Aviato credit from the
configured 1,000-for-$10 recharge and paid receipt). Every paid row therefore has a computable USD
price and is platform-eligible when the deployment keys and allow-list are set.

Their core catalogs use only existing marketplace platforms. Crustdata has eight live-verified,
single-call operations: five on Company data and three on People & contact data. Batch routes are
omitted because they can create unexpectedly large jobs and costs; sales-enabled routes that the
connected account cannot verify are also omitted. Its generic web search and page fetch are not placed on the `web` platform because that
marketplace card currently means backlinks, authority and domain metrics. Aviato has 21 curated
operations: nine on Company data, seven on People & contact data, and five on LinkedIn social. Both
Aviato people-search forms remain: the POST route exposes the full DSL, while the GET route is a
separate simple-query workflow.

Bulk behavior stays inside the faithful relay. Crustdata batch operations are not catalogued.
Aviato company and person bulk enrichment are synchronous JSON calls. No provider-specific
buffering, callback receiver, or proxy branch is added. Crustdata's required
`x-api-version: 2025-11-01` header remains provider metadata and is bound on every BYOK and
platform-key call.

Variable prices use the existing reserve→settle path. Crustdata reserves the documented maximum
for the requested record count and settles the exact `X-Credits-Used` response header. cloro
(2026-09-07) is the second header-reporting provider: every billed response carries
`X-Credits-Charged`, the catalog value is the price of the full-surface `test_request` (an upper
bound — the ChatGPT ads/shopping include family and the Google AI Overview flags are +2 each), the
top-level `state` body field is a generic `cost.modifiers` rider, and the header settles the exact
charge. The header is absent on cloro's free routes and on a failed extraction, which it does not
bill, so an absent header settles as unreported rather than as zero. The `cost.modifiers` reserve
path is open to any credit-priced provider with a fx.yaml rate, not only Aviato. AI Ark is the
third header-reporting provider: its exact `X-Credit` debit is negative, and `_CREDIT_HEADERS`
declares an explicit -1 multiplier instead of treating every negative number as a charge. Aviato's
preview calls reserve zero; observed email/rescrape add-ons are declared in each endpoint's generic
`cost.modifiers` map and derived from request flags; synchronous bulk
calls reserve per lookup and settle per returned successful record. Simple people search reserves
the documented one-credit-per-result enrichment add-on but settles its observed 0.25-credit base.
A lower price needs repeat balance evidence because Aviato does not return the exact call charge.
That evidence showed that company single and bulk rescrape, person single rescrape, and person bulk
email riders are not billed, although the authenticated price page lists them. Person single email
and person bulk rescrape riders are billed. A `reserve_only: true` modifier keeps each documented
but unbilled rider in the temporary hold. `settle: modifiers` then uses only the measured modifiers
for the final charge. This protects treg from a documented maximum without overcharging the caller.

A `cost.modifiers` rule names a parameter location (`query`, `body`, or `lookups`), a match rule
(`truthy` or `present`), and exactly one credit effect: make the call free, add fixed credits, or add
credits per requested result. The validator rejects any other shape. This keeps vendor numbers in
catalog YAML while the billing code reads the rules without provider-specific credit constants.
An optional `cost.settle: base` keeps documented riders in the reserve but settles the successful
call at the catalog base when repeat live evidence proves that the provider neither bills nor
delivers those riders.

A verification stamp proves the request shape, response shape, and paid behavior that the evidence
actually observed. A placeholder path value or a free miss does not prove a paid hit. Such rows keep
the documented price and say which paid behavior remains unobserved. Captured examples use public
records and omit private identities or content when counts are enough to prove the response shape.

Path placeholders are substituted by the marketplace caller. Raw values are percent-encoded; a value
that already contains a valid `%HH` escape is kept verbatim so callers can safely reuse encoded resource
names returned by an upstream API. An invalid/literal `%` is still encoded as `%25`. Search Console's
`siteUrl` examples deliberately use the raw `sc-domain:example.com` form to demonstrate the default path.
Google Tag Manager is the opposite case: its `parent`/`path` values describe a hierarchy rather than
one opaque identifier, so the curated catalog exposes atomic account/container/workspace/version ids.
`catalog_ingest.google_flat_path_params` makes the generated GTM input schema use the same atomic
placeholders already present in Discovery's `flatPath`; no slash-delimited resource name is passed
through one placeholder and accidentally encoded as `%2F`.

## Where things live

```
src/treg/catalog/
  capabilities.yaml        # the shared capability taxonomy (the cross-provider join key)
  aliases.yaml             # query word -> catalog words (search-time vocabulary bridge)
  fx.yaml                  # currency -> USD rates + per-PROVIDER credit rates (see "Cost" below)
  <service>.yaml           # CORE tier — hand-curated; <service> = OAuthProvider.service
  <service>.extended.yaml  # EXTENDED tier — machine-generated full endpoint surface
  examples/<endpoint-id>.json  # truncated, scrubbed real responses captured at verify time
scripts/
  catalog_drift.py            # path+method drift against providers' public OpenAPI documents
  catalog_validate.py         # schema + referential checks (run in CI / after any edit)
  catalog_verify.py           # live-tests CORE endpoints with a real credential; writes examples/
  catalog_verify_extended.py  # the same for the extended tier, in bulk, under a spend cap
  catalog_ingest.py           # bulk-generates the extended tier from provider specs
  catalog_cost_provenance.py  # backfills cost units + provenance; re-run after any re-ingest
src/treg/routers/catalog.py    # open Catalog JSON routes, attached in legacy registration order
```

Data files are YAML (curation-friendly) and loaded through `catalog_store`. The open JSON handlers
live in `routers.catalog`; `api.py` attaches their router at the original position so the specific
Catalog API paths continue to precede the later `/catalog/{slug}` page route.

## Two tiers

Curation and coverage pull in opposite directions: an agent needs to know that TikHub *can* read
Zhihu answers (breadth), and separately needs one endpoint per job that is known to work and known
to cost $0.001 (depth). The catalog carries both, in separate files, distinguished by `tier`.

| | `tier: core` — `<service>.yaml` | `tier: extended` — `<service>.extended.yaml` |
|---|---|---|
| written by | a human, one endpoint at a time | `scripts/catalog_ingest.py`, from the provider's spec |
| size | ~10–15 per provider | every route the provider exposes (hundreds to ~1400) |
| `capability` | required — the cross-provider join key | absent; nothing is mapped to the taxonomy yet |
| `input` / `test_request` | required, hand-written | generated, when the provider documents parameters |
| `verified` + `example_response` | expected | where the generated `test_request` passed a live call |
| example size | ~10 KB, arrays → 2 items | ~2 KB, arrays → 1 item (shape, not fidelity) |
| `cost` | required | present when the provider publishes per-route prices |

The extended tier originally carried no `input`, `test_request` or verification at all — the tiers
split on *curated vs. generated*, and it read as if it also split on *tested vs. untested*. It does
not, and it should not: a provider that documents its parameters (with example values, as TikHub
does) gives us everything needed to generate a test request and make the call. What stays exclusive
to core is the part a machine cannot do — mapping the endpoint to a capability, choosing the test
target deliberately, and a full-fidelity example. See "bulk-verifying the extended tier" below.

**An ingested price is a claim we will charge on, so a generated `free` is a bug, not a default.**
`x.extended.yaml` shipped 168 routes priced `free` off a plan-tier model X had already abolished,
while the proxy — which skips a `free` block, its `usd` being falsy — billed the provider fallback:
the catalog published $0 and the balance moved $0.10. Where the upstream bills *treg* (an
`oauth_billed` provider, [auth-secrets](auth-secrets.md)), the generator must therefore price every
route it emits, and a test walks the provider asserting the published price equals the reserved one.

The second half of that lesson cost a review round: **the fix for a blanket price is not a smaller
blanket.** The first repair priced all 74 X writes at $0.015 — the post-creation rate — when X
publishes a row per ACTION, and creating a list is $0.010, managing one $0.005, deleting an
interaction $0.010. Read the rate card and transcribe it (`catalog_ingest.X_RATES` is the card,
`X_ROUTE_RATES` the route→row mapping, and each entry's note names the row it was priced from);
where the mapping is a judgement call, `confidence: inferred` says so and takes the dearer reading,
because treg pays the difference. Watch for **conditional** rates in particular: X's $0.001 "owned
read" applies only when the caller owns the developer app, which on a registry connect is treg —
quoting it for our members under-billed the very calls we are charged the most for.

Core wins on collision: `catalog_ingest.py` drops any `(method, path)` the provider's core file
already curates, so an endpoint appears exactly once across both tiers. Promoting an extended entry
means moving it into the core file and completing it (steps 3–8 below) — not editing it in place;
extended files are regenerated wholesale and hand edits are lost.

OpenRouter video models are the deliberate exception to path-only collision detection: every model
uses `POST /videos`, so `core_body_models` skips only the fixed `body.model` values already curated
in core. Its OpenRouter and Replicate generation ingesters emit no capability guesses and explicitly
set `domain: models`; this keeps every coverage row as a standalone model. Their
`carry_verification(..., carry_capability=False)` migration keeps verification evidence and
reviewed names/kinds without resurrecting old inferred capability tags.
The core AIGC generation rows pin `domain: models` too and carry PER-MODEL capabilities
(`video-gen.hailuo.from_text`, proposed in their provider files) rather than the job-level
`video-gen.from_text` family. Generation models are not interchangeable - a merged row comparing
Hailuo with Wan or Seedance is a false comparison - so the job-level capabilities are deliberately
memberless, reserved for hand-picked models (see capabilities.yaml). The AI generation modality
pages therefore render as flat model walls; the same model reachable over several routes (MiniMax
direct, OpenRouter, Replicate all serve Hailuo) sits adjacent under model-led names, which is the
comparison that actually means something. The per-model capability is the join key that lets those
routes merge onto one row if that comparison is later curated. reAPI and PiAPI are the first pair
to share join keys on purpose: both files propose `video-gen.seedance-2-5.generate`,
`video-gen.seedance-2-5-unrestricted.generate`, `image-gen.gpt-image-2-5.generate`,
`image-gen.gpt-image-2.generate` and `image-gen.gemini-3-pro-image.generate`, so the two routes to
one model sit on one row with their prices side by side. The `-unrestricted` key names the Less Restriction route (reAPI `content_filter: false`, PiAPI's `seedance-2.5-less-restriction` task): the
only route on which a real person's photo is accepted as the subject reference, which is the whole
reason those resellers are listed beside the official-rate OpenRouter route. OpenRouter's Seedance 2.5
is curated into `openrouter.yaml` on the same join key (its generated extended twin is therefore
skipped by the ingester's curated-model rule), so the default-filter row compares three routes and
the Less Restriction row two. Replicate's official `google/nano-banana-pro`, `openai/gpt-image-2` and both
`openai/gpt-image-2.5-*` models are curated into `replicate.yaml` on the image keys the same way (per
output image by quality or resolution, from the model pages' price criteria), so each image model
row compares reAPI, PiAPI and Replicate. Merged rows are titled by the capability description, which for these
per-model keys is the plain model name ("Seedance 2.5"), not a sentence.

## Schema

### capabilities.yaml

```yaml
capabilities:
  tiktok.user.profile: "Public profile of a TikTok user (followers, bio, stats)"
  web.backlinks.summary: "Aggregate backlink profile of a domain or URL"
platforms:
  tiktok: "TikTok"
  web: "The web at large (backlinks, authority, traffic)"
  video-gen: {label: "Video generation", category: "AI generation"}
  image-gen: {label: "Image generation", category: "AI generation"}
  voice-gen: {label: "Voice generation", category: "AI generation"}
```

Rules:
- A capability id is dot-delimited, lowercase; the FIRST segment is its platform slug.
- Ids name the *job*, not the provider's endpoint ("get user profile", not "fetch_user_profile_v2").
- Adding a capability = adding it here. Provider files may carry `proposed_capabilities:` (same
  mapping shape) when curation discovers a job the taxonomy lacks; the reviewer merges them into
  this file. The validator accepts a capability that is either global or proposed in the same file.
- Under `AI generation`, platform means the generated-media modality rather than a system that owns
  the data. The frozen vocabulary is `video-gen.from_text`, `video-gen.from_image`,
  `video-gen.task.status`, `image-gen.from_text`, `image-gen.edit`, and `voice-gen.from_text`;
  text-to-video and image-to-video stay separate because their required inputs and prices differ.

### `<service>.yaml`

```yaml
provider: tikhub                # must equal OAuthProvider.service in oauth_providers.py
source:
  docs: https://docs.tikhub.io/
  openapi: https://api.tikhub.io/openapi.json   # null when the provider has no spec
  curated: 2026-07-28
limits: "10 requests/second per key"   # optional, provider-level: the rate/quota model in one line
pricing_url: https://…                 # optional: where CURRENT prices live (values in cost blocks age)
endpoints:
  - id: tikhub.tiktok.user.profile   # unique; convention: <provider>.<capability>
    capability: tiktok.user.profile  # must exist in capabilities.yaml (or proposed_capabilities)
    platform: tiktok                 # must equal the capability's first segment
    domain: user                     # optional: the platform page's section. One lowercase word.
                                     #   Omit it and the loader derives one — capability's middle
                                     #   segment, else a path keyword, else the path's grouping
                                     #   segment, else "other". Set it only to override a bad guess.
    scope: any_account               # any_account (scrapers) | own_account (first-party OAuth)
    kind: data                       # optional; data (DEFAULT) | action | account | utility.
                                     #   what the endpoint IS — see "Kind" below. Absent ⇒ data.
    method: GET
    path: /api/v1/tiktok/web/fetch_user_profile   # relative to the provider's base_url
    name: "Get user profile"        # optional short DISPLAY title (≤60 chars). Set it when the
                                    #   summary is doc-prose too long for a row heading; clients
                                    #   fall back to `summary` when absent.
    summary: "Public TikTok profile by username"  # the provider's own description, kept VERBATIM —
                                    #   `name` is ours to word, `summary` is theirs
    input:                           # split by location — mirrors treg's binding model
      queryArrayEncoding: json      # optional default for array query params: json | comma |
                                    # repeated (the compatibility default).
      queryParams:
        uniqueId: {type: string, required: false, note: "username from the profile URL", example: "tiktok"}
        secUid:   {type: string, required: false}
      note: "one of uniqueId | secUid; uniqueId preferred"
      # also allowed: pathParams, body, bodyType (json|form)
    test_request:                    # EXACT params catalog_verify.py sends — must be cheap
      queryParams: {uniqueId: "tiktok"}
    expect:                          # optional; default is "HTTP 2xx"
      json_path: code                # dotted path into the response JSON
      equals: 200                    #   (for providers that answer HTTP 200 even on failure)
    cost:
      type: per_success              # per_call | per_result | per_success | free | quota_rows
      value: 0.0015
      currency: USD
      note: "charged on 2xx only; errors free"
    verified: 2026-07-28             # date of the last PASSING catalog_verify.py run; absent = unverified
    example_response: examples/tikhub.tiktok.user.profile.json   # written by catalog_verify.py
    docs_url: https://docs.tikhub.io/…
```

### Async descriptors

`catalog_store._normalize` sets `cache: forbidden` for `image-gen`, `video-gen`, and `voice-gen` endpoints,
including synchronous generation, task/result utilities and generated extended rows. Their `kind`
is unchanged. These requests must reach the provider, not replay shared-account task ids or media
from an identical prompt. Other platforms retain their declared/default cache policy.

An asynchronous submission endpoint may carry an `async:` descriptor. A provider file may put the
same block at top level as a default for every endpoint in that file; an endpoint block **replaces
it whole** (`effective_async_descriptor`): a descriptor is one protocol, and a protocol that differs
in one axis differs in poll target, status vocabulary and result location together (MiniMax v2
against v1), so a field-wise merge only produced descriptors nobody had written down. `catalog_store`
serves the effective descriptor on the normalized endpoint. An explicit endpoint `async: false` opts
a utility or synchronous endpoint out of the provider default; absence means inherit.

**Poll mode in practice.** Every listed provider polls a static catalog id (`poll.endpoint`), which
the CLI reaches through `/call/<id>` on any credential tier. Replicate offers both `urls.get` and the
stable `GET /v1/predictions/{id}`; the static form is listed (`replicate.predictions.get`) because a
`--await` that polled the absolute URL through `/call/https://…` was refused for a team on treg's
key - that path resolves only a team's own tool (sample run, 2026-09-02). The dynamic-URL mode
(`poll.url_from` + `url_hosts`) stays in the schema, validator and worker for a provider that offers
nothing else (BFL), but today it works only for BYOK teams; serving it on the platform key needs a
host-allow-listed relay that is not built. Do not document it as available.

**Envelope errors.** A submission endpoint may carry `expect` (the provider-wide or per-endpoint
success rule already used by settle); `application.call.service._submission_accepted` gates
deferral on it, so MiniMax's HTTP-200-with-`base_resp.status_code: 2013` releases at once instead of
becoming a task nobody can poll. The synchronous MiniMax image endpoint carries the same rule;
otherwise an invalid prompt or output count would be charged at the image table or fallback price.
OpenRouter's terminal failure set includes `failed`, `cancelled`, and `expired`; all three release
the hold as soon as the status endpoint reports them.

```yaml
async:
  id_from: task_id
  poll:
    endpoint: minimax.video-gen.task.status
    param: {in: queryParams, name: task_id}
    # Alternative mode:
    # url_from: polling_url
    # url_hosts: [api.example.com]
  status:
    path: task.status
    success: [succeeded]
    failure: [failed, cancelled]
  result:
    path: task.content.url
    # Alternative mode:
    # fetch: provider.video-gen.result.retrieve
    # fetch_param: {in: pathParams, name: video_id, value_from: id}
    ttl_note: 9h
  interval: 10
```

A `cost.table` also prices out as a range: at load time `_table_floor` computes the cheapest row
(a `times` row at its field's declared `min`) into `cost.table_min`, and `cost_view` exposes it as
`usd_min` beside `usd`, which stays the validated ceiling (what reserve and eligibility read). Every
price surface - the wall, `treg catalog search`, the dashboard, `/access` - shows `$low-$high` for a
table rather than the worst case alone. A table whose every row multiplies by a `duration` field is
a video model sold per second, and `$0.47-$13.9/success` (shortest clip at the cheapest resolution
up to the longest at the dearest) reads as nonsense beside a vendor page saying `$0.12/s`; so
`_table_rate` records the row span as `cost.table_rate`, `cost_view` serves it as `rate_usd_min`,
`rate_usd`, `rate_unit: s`, and the dashboard and CLI quote `$0.119-$0.462/s` for those rows while
`usd`/`usd_min` keep pricing the whole call for reserve. `type: per_success` on these rows is the
billing rule (a failed generation is not charged), not the display unit.

The validator checks the effective descriptor. Dotted JSON paths are syntactically valid; success and
failure are non-empty, disjoint lists; `interval` is positive; poll has exactly one of `endpoint`
or `url_from`; result has exactly one of `path` or `fetch`; every descriptor block rejects unknown
keys. Status values are compared after string coercion on both sides; a missing or unrecognized value
means still in progress, in both the CLI awaiter and the settlement worker. Static poll/fetch ids must
be same-provider GET utility endpoints. Their mapping is explicit:
poll `param` is exactly `{in, name}`, while result `fetch_param` is exactly `{in, name, value_from}`
so a terminal field such as MiniMax's `file_id` is not confused with the utility request parameter.
The named path/query input must exist on the target endpoint. Body-mode polling is deliberately
outside the frozen contract because no surveyed provider uses it and the generic client could not
faithfully execute it. Dynamic URLs require a non-empty `url_hosts` allow-list. Any endpoint with
`async:` must use `cost.type: per_success`. The descriptor is metadata
beside the faithful relay: it never changes provider-native parameters or response bodies. The call
router serializes the effective descriptor into `X-Treg-Async` before the response stream starts;
it does not inspect or buffer the upstream body.

Older async pairs that settle on their existing request paths use `resource_ownership` alongside
the deferred-settlement design. `produces` maps response JSON paths to provider-local resource
kinds; `requires` binds a path/query parameter to one of those kinds. On treg's shared key, a 2xx
producer records the opaque id for the caller org, and a consumer is refused before relay unless the
same org owns that provider/kind/id tuple. This covers Apify run/dataset ids, Bright Data snapshot
ids, CompanyEnrich bulk job ids and LeadsForge enrichment/followers job ids without changing their
billing behavior. Ownership is only as trustworthy as the producer's answer: a provider that dedupes
on `Idempotency-Key` would hand one org another's job under a shared label, which is why the relay
re-scopes that header per org on treg's key ([proxy-model](proxy-model.md)). The validator requires
declared parameters and exact non-empty `{kind, path}` / `{kind, param}` shapes. BYOK does not use
this metadata because the provider account itself belongs to the caller.
Formal descriptors also materialize their poll/fetch ids under endpoint-namespaced resource kinds;
their utility rows declare matching `requires` rules. The frozen `AsyncTaskRecord` remains the
compatibility authority for tasks created before the resource table existed, while the explicit
utility rule prevents a later catalog edit from silently turning a protected endpoint fail-open.
The catalog test additionally rejects platform-eligible task/status/result object reads that have a
required id but omit this metadata, so new legacy-style pairs cannot rely on a reviewer noticing the
boundary by hand.
Generated legacy task consumers for which no trustworthy producer→id chain is represented are
explicitly `platform_blocked` instead: Akta request status, TikHub's captions-result route, and the
DataForSEO on-page/SERP task consumers remain callable with BYOK but never receive treg's shared key.
`carry_verification` preserves that reviewed block across re-ingestion just like a verification
stamp; silently regenerating it away would reopen the tenant boundary.

MiniMax's curated Hailuo routes intentionally use the v1 three-step protocol: submit with
`POST /v1/video_generation`, poll `GET /v1/query/video_generation` with a query-string task id and
the terminal values `Success`/`Fail`, then pass the returned `file_id` to
`GET /v1/files/retrieve`. The v2 generation path serves the H3 family and is not a protocol upgrade
for the Hailuo models in this listing.

MiniMax also supplies the first `voice-gen` rows through the same provider connection. Speech 2.8
HD and Turbo are separate model rows over `POST /v1/t2a_v2`, each fixing its model plus
`stream: false` and `output_format: url`; this keeps the response bounded and returns a 24-hour
audio URL. The route is synchronous and billed per input character, so `_body_text_characters`
scales the reserve from the provider-facing `text` value. The two rows remain marked `skipped`
until a deliberate paid verification call is authorized; documentation provenance is enough for
platform eligibility, but is not presented as live route evidence. The Voice generation Actions
shelf exposes `minimax.voice-gen.voices.list` so callers can discover valid system voice IDs. Its
request is fixed to `voice_type: system`: account-specific cloned and generated voices must not
cross team boundaries when treg's shared MiniMax connection is used. The synchronous
`minimax.image-gen.from_text` row likewise pins `body.model: image-01` through `platform_request`.

reAPI answers every submission with a bare `{id, status}` and reports the charge on the poll body
(`usage.credits`, 1 credit = $0.001); video rows keep the file-level descriptor (`output.video_urls`)
and image rows replace it whole for `output.image_urls`. PiAPI wraps its task routes in
`{code, data}` (HTTP 200 with `code` 400 on a bad request, hence the provider-wide `expect`), but
its OpenAI-shaped `/api/v1/images/generations/async` route answers the bare task object, so those
two rows override both `id_from` and `expect` (`error.code` 0). PiAPI's `meta.usage` counts
"points" at ten million per dollar; it is read for the evidence ledger, not settled on, because
points carry no fx rate: the settlement engine accepts `usd` and a provider `credit` priced in
fx.yaml. reAPI's Seedance rows settle that way, on the terminal body's `usage.credits`, and price
the provider's `duration: -1` (auto, mandatory when the prompt edits a video reference) with flat
rows ahead of the per-second rows: thirty seconds at the requested resolution, as a reserve only.

OpenRouter ingest reads `/api/v1/videos/models`, emits one extended row per model on the shared
`POST /videos` route, and converts duration-based `pricing_skus` into price tables with
`rate_card_api` provenance. It converts `cents_per_*` units to USD, maps resolution/audio dimensions,
orders narrower conditions first, and collapses indistinguishable mode SKUs to the highest rate.
Token, image-input, reference-image, and megapixel-second SKUs preserve the live rate card but stay
explicitly unknown/BYOK-only because one bounded `times` field cannot safely describe them.
Two verified Wan 3.0 480p/2s calls each quoted $0.10 from `pricing_skus` but reported
`usage.cost: 0.2125`; rate-card rows are therefore `documented`, not observed-cost `verified`.
Replicate ingest joins the official text-to-image, text-to-video, and image-to-video collections;
each generated row takes its request fields from `latest_version.openapi_schema`. Its generated
prices are explicitly unknown, while the curated core rows carry per-model page provenance. Both
ingesters sort their inputs and produce byte-identical output when upstream data is unchanged.

Utility capability names still describe the utility's actual job. OpenRouter model discovery uses
the file-local proposed `video-gen.models.list`; OpenRouter and MiniMax content retrieval use the
proposed `video-gen.result.retrieve`; only polling uses the frozen `video-gen.task.status`. These
rows remain hidden management plumbing because `kind: utility`, and proposed capabilities avoid
expanding the global generation vocabulary merely to satisfy the core-row capability requirement.

### `<service>.extended.yaml`

Generated — never hand-edited. Re-run `uv run python scripts/catalog_ingest.py <service>` instead.

```yaml
provider: tikhub                  # same rule as core: equals OAuthProvider.service
source:
  method: openapi + provider rate card   # how the entries below were derived
  ingested: 2026-07-28                   # date of the generating run
  spec_urls:                             # every upstream the run read, so it is reproducible
    - https://api.tikhub.io/openapi.json
endpoints:
  - id: tikhub.x.zhihu-web-fetch-answer-comments   # <service>.x.<path-slugified>; the `.x.`
    tier: extended                                 #   infix keeps extended ids out of the
    platform: zhihu                                #   `<provider>.<capability>` namespace
    method: GET
    path: /api/v1/zhihu/web/fetch_answer_comments
    name: "Zhihu answer comments"   # optional, same meaning as core; the ingesters harvest it
                                    #   where the spec offers a human title distinct from the
                                    #   description (TikHub's Apifox op names, Just One API's
                                    #   per-op summary / info.title, DataForSEO's operationId).
                                    #   Carried across re-ingests by id; providers may also carry
                                    #   reviewed `capability` mappings when coverage policy permits.
    summary: "Get comments of a Zhihu answer"
    kind: data                      # optional; data (DEFAULT) | action | account | utility (see "Kind")
    cost: {type: per_success, value: 0.001, currency: USD}   # optional
    docs_url: https://docs.…                                 # optional
    input:                          # generated from the provider's parameter docs
      queryParams:
        answer_id: {type: string, required: true, note: "Answer id", example: "1913...”}
    test_request:                   # generated: documented example values, page sizes clamped
      queryParams: {answer_id: "1913…", limit: 5}
    verified: 2026-07-28                                     # a real call passed
    example_response: examples/tikhub.x.zhihu-web-fetch-answer-comments.json
```

Rules:
- Required: `id`, `platform`, `method`, `path`, `summary`. `platform` must exist in
  `capabilities.yaml` — that is what puts the endpoint on a marketplace shelf.
- `capability` is normally ABSENT (extended entries are unmapped). AIGC generation coverage forbids
  inferred mappings entirely: comparison membership is curated in core, and extended rows use the
  explicit `models` domain. If another extended file has a reviewed mapping, the validator holds it
  to the full core rules so promotion by hand cannot silently drift.
- `cost` is optional, because several providers price per API family rather than per route. When
  present it must still be a real cost model (`cost.type` from the same enum as core).
- `input` / `test_request` appear when the provider publishes enough parameter documentation to
  generate them; both are machine-written and are rewritten on the next ingest.
- Query arrays carry an explicit wire encoding when the provider does not accept repeated keys:
  `input.queryArrayEncoding` sets the endpoint-wide format. `catalog_store.query_values()` is shared
  by MCP request assembly and `call_template()`, so the structured schema and paste-ready command
  cannot disagree. Complete
  `name=value` arguments are shell-quoted with `shlex.quote` after canonical boolean/JSON encoding.
  Endpoint declarations are only valid when every array parameter shares a wire format. Meta Ad
  Library's array parameters all use JSON; undeclared endpoints retain repeated keys. Pinterest's
  mixed convention remains a documented catalog gap until a live connection can verify a separate
  per-parameter extension.
- Nested JSON bodies keep the dotted-key schema convention (`params.domain` beside a parent
  `params` object). `call_template()` runs `unflatten_dotted()` on the assembled `--data` object so
  the paste-ready command emits `{"params":{"domain":…}}` rather than a flat `"params.domain"` key
  plus a `"params":"<object>"` placeholder. MCP request assembly does not share this helper:
  callers already send a nested JSON `body`. Query parameter names that literally contain a dot
  (`user.fields`, `searchVolume.min`) are not bodies and stay unexpanded.
- `verified` + `example_response` mean a live call was made and passed, and carry exactly the same
  weight as in core — the validator applies one rule to both tiers: verified ⇒ a `test_request` to
  re-verify with and an `example_response` file that exists.
- Exactly one of `verified`, `unverified`, `untestable` or `skipped` should be present on an entry
  that has been through the pipeline:
  - `untestable: <reason>` — set at INGEST: no test request could be generated (route absent from
    the provider's docs, or a required parameter only the caller can supply, e.g. their own
    platform cookie). No call was made and none is possible with a bare key.
  - `unverified: http 404 …` — set at VERIFY: the call was made and failed, with the status code
    and the provider's message. This is a finding, not a gap: `402` means the route needs a paid
    plan tier, a family-wide run of `404`s means the provider's upstream scraper is broken.
  - `skipped: <reason>` — set at VERIFY: a usable test request exists and the call was deliberately
    not made, to conserve a paid balance. The reason names the sibling endpoint that WAS verified,
    or why one call costs too much. See "the fourth state" below — these clear with money, not
    investigation, which is what separates them from the two above.

  `catalog_validate.py` ENFORCES this: an extended entry that has been through the pipeline (it
  has a `test_request`, or any of the four keys) must claim exactly one of them, non-empty. Two at
  once is a contradiction; a key present but empty is the failure that motivated the rule — a
  re-run overwrote an endpoint's result record, dropped the reason string, and stamped an empty
  state, which every other check happily passed. A never-verified entry straight out of ingest has
  neither a request nor a state key and is left alone.
- Ids are unique across the WHOLE catalog, both tiers, all providers.
- Two optional fields exist only in this tier, both added for providers with split surfaces:
  - `host: <fqdn>` describes an additional API root for an endpoint whose `path` is not relative to
    the provider's primary `base_url`. It becomes executable only when the provider explicitly opts
    in with `OAuthProvider.catalog_targets`; otherwise historical host metadata remains inert and
    calling still uses the provider's primary profile. The catalog cannot authorize a host by itself.
    `OAuthProvider.catalog_targets` must map the exact hostname to a safe HTTPS base URL and any
    credential-profile override. `profile_for_catalog_host` rejects missing, duplicate, malformed,
    credential-bearing, port-bearing, query-bearing, and fragment-bearing targets before reserve or
    relay. The endpoint path is joined after the approved base URL's existing prefix, so primary KG
    paths and alternate Extract paths do not duplicate or erase version prefixes. Diffbot uses this
    for its KG, Extract, Web Search, and Natural Language host families; Web Search's target also
    changes query-token injection to its documented Bearer header. Absence of `host` retains the
    primary provider profile and `base_url`.
  - `scope_gap: <one line>` — the credential treg's OAuth app obtains CANNOT call this, and this is
    the scope that is missing. These are listed rather than dropped on purpose: the set of gaps is
    the answer to "which scopes should we add to the registered app", and it is only visible if the
    endpoints stay in the file. `scope_gap` present ⇒ expect 403 until the app is widened.

### Kind — the browse surface vs. the plumbing

`kind` says what an endpoint IS, so the marketplace can lead with the useful surface and tuck the
provider's own machinery out of the way. It is optional in BOTH tiers; absent reads as `data`.

| `kind` | what it is | examples | browse |
|---|---|---|---|
| `data` (default) | fetch / scrape / enrich a resource | get user profile, backlink summary, SERP | shown |
| `action` | a meaningful WRITE on the connected user's OWN account | post a video, reply, update an ad budget, upload | shown |
| `account` | the provider's own list/webhook/saved-search/credit CRUD | create/delete a lead-list, manage webhooks | hidden |
| `utility` | helpers with no data of their own | token/x-bogus generators, enum & location listings, decrypt/encrypt, device register | hidden |

`data` + `action` are the **browse surface**; `account` + `utility` are **management endpoints**.
Three things follow, and they are the whole point of the field:

- **The platform census counts data + action only.** `GET /catalog/platforms` reports each shelf's
  `endpoints` / `capabilities` / `verified` and its "from …" price over the browse surface — a
  management endpoint is real inventory but it is not what a tile advertises, so it never inflates
  those numbers (nor the marketplace tile counts the dashboard renders from them).
- **The default platform view drops them.** `GET /catalog/platforms/<slug>` returns only the
  browse surface in `capabilities` / `extended` / `domains`, plus a `hidden_count`. Pass
  `?include_hidden=1` to get the WHOLE surface back — every endpoint carries `kind`, so a client
  can fold the plumbing behind its own control. The dashboard does exactly this: it requests
  `include_hidden`, renders data/action in the ledger, and files account/utility behind a small
  per-section "N management endpoints" expander (the same show-more gesture as the platform tiles).
- **`kind` is a reviewed judgement, carried across re-ingests.** Like `capability` and `name`, an
  extended entry's `kind` is set by review, not derived from the spec, so `catalog_ingest.py`'s
  `carry_verification` re-attaches it by id — regenerating the file must not reset it to `data`.

`catalog_validate.py` only checks the value when present: a stated `kind` must be one of the four.

### Naming — `name` is the search surface we own

`summary` is the provider's text, verbatim; `name` is OURS, and since 2026-08-20 it is searched
(same weight as summary). That makes it the one per-endpoint field where curation may put the
words agents type. The formula: **job + the input the caller must hold + top output facets**, ≤60
characters, and it must read as a natural title — it is the row heading on every surface.

    Linkedin: get company profile (web_v2)   ->  LinkedIn company profile by URL or slug — headcount, industry
    Get user profile                         ->  TikTok user profile by username — followers, bio, stats

The rules (applied catalog-wide in the 2026-08-20 rewrite; every new provider follows them):

1. Name the JOB in task words — never the vendor's operation title or version codes.
2. Say the INPUT ("by name", "by domain", "by ASIN", "by LinkedIn URL"). Agents search by what
   they hold; only the caller knows its inputs — that doctrine applies to naming too.
3. Say the top OUTPUTS when people search by them ("headcount", "reviews", "hiring signal").
4. One concept, one word, catalog-wide: always "postings", never sometimes "vacancies";
   `aliases.yaml` covers the agent's side, our side must be consistent.
5. Prefer the longer word form — "postings" contains "posting"; substring matching never works
   backward.
6. No dead words: "API", "data", "get", "fetch", "endpoint" are soft tokens worth nothing.
7. No stuffing. If it does not read as a title, it is wrong. Overflow vocabulary belongs in the
   capability description (weight 3, shared by the group) or `aliases.yaml`, never in the name.
   Worked case (2026-08-27): "find instagram influencers by niche…" returned ZERO results because
   influencers.club's name/summary said only "creators" — fixed by naming the job in the endpoint
   (`…influencers by niche & size`), carrying the facet words (country, followers, engagement,
   Instagram/TikTok/YouTube) in the `creators.search` capability description, and aliasing
   `influencer(s)/kol(s)/microinfluencer(s) → creators` and `ig → instagram`. Long natural-language
   queries still require every rare word to appear somewhere; the `near:` hint tells the agent
   which words to drop.
8. TRUTH over vocabulary: derive the name only from the row's own summary, path and input fields.
   A name claiming an output the endpoint does not return is a lie an agent will spend money on.

### Cost — the file keeps the billing unit, the server computes USD

A `cost` block stays in whatever unit the PROVIDER bills in; that is the number that stays correct
when a rate moves. `cost.usd` is added at SERVE time by `Catalog.cost_view` from `fx.yaml`, so a
rate refresh re-prices the whole catalog without touching a provider file. Clients (dashboard cards,
`treg catalog search`, `treg catalog get`) lead with `usd` because a column is only comparable in
one unit, and fall back to the native amount when `usd` is null.

The full block:

```yaml
cost:
  type: per_result        # per_call | per_result | per_success | free | quota_rows
  value: 2.00             # non-null unless confidence: unknown
  currency: USD           # USD | CNY | credit | unit
  per: 1000               # the quantity `value` covers (default 1)
  unit: row               # what `per` counts — or, under `currency: unit`, the provider's meter
  source: docs            # rate_card_api | docs | observed | vendor_email | inferred
  source_url: https://…   # the exact rate card / pricing page (or rate-card endpoint)
  checked: 2026-07-28     # when the PRICE was confirmed — not when the route was called
  confidence: documented  # verified | documented | inferred | unknown
  note: "…"               # free text: the half of the charge the schema cannot hold, caveats, traps
```

For finite AIGC matrices, linear rates, and usage-settled generation, `value` is replaced by an
ordered first-match `table` plus an explicit fallback upper bound:

```yaml
cost:
  type: per_success
  table:
    - {when: {body.model: Model-A, body.resolution: 512P, body.duration: 6}, value: 0.3}
    - {when: {body.model: Model-B, body.resolution: 768P}, value: 0.13, times: body.duration}
  fallback: {value: 2.0, note: "most expensive supported combination"}
  currency: USD
  settle: table                 # or usage
  # usage: {path: usage.cost, unit: usd}
  source: docs
  source_url: https://example.com/pricing
  checked: 2026-09-01
  confidence: documented
```

Rows match in file order. `when` is a subset comparison: every named field must equal the request
value after input defaults are applied, using exact forms. References are location-qualified dotted
paths (`body.model`, `body.input.num_outputs`, `queryParams.mode`) so query/body collisions cannot
silently price the wrong field. Every `when` field must be required or declare `default` in `input`.
`times` multiplies by one numeric request field with a positive `max`. Narrow rows must precede broad
ones; the validator rejects a later condition shadowed by an earlier subset, duplicate conditions,
unknown row/fallback keys, non-finite values, values outside input enum/min/max, and simultaneous
`cost.value` plus `cost.table`. `fallback` is a hand-written, explained global upper bound, checked
against every row's maximum computable price. A `times` value outside the field's declared range
(or non-finite, or non-positive) matches no row and prices at the
fallback, so a request cannot reserve zero or bill past the ceiling. With `settle: table`, the
matched row is reserved and settled (fallback when unmatched). With `settle: usage`, the matched
row is reserved as the rate-card estimate and the terminal `usage.path` figure settles, which may
exceed the reserve (OpenRouter's unpublished minimums); `settle: usage` therefore requires an async
descriptor, exactly a dotted `usage.path` and a supported `usage.unit` (`usd`, or `credit` when
fx.yaml prices that provider's credit), and `settle: table` rejects a stray usage block. A `times`
value is never non-positive, whatever minimum the field declares, so a field that admits a sentinel
such as `-1` cannot multiply a rate by it; the sentinel is priced by a flat row that pins it, and
that row is left out of the advertised per-second rate span. The money fragment describes the settlement itself.

`value` + `currency` + `per` answer *how much*; `type` + `unit` answer *per what*; `source` +
`source_url` + `checked` + `confidence` answer *says who, and how sure*. All four questions have to
have an answer before treg will spend its OWN money on an endpoint (see "platform-eligible" below),
which is the whole reason the provenance keys exist.

**`per` and `unit`.** Read a block as "`value` `currency` per `per` `unit`". SpyFu bills a CPM, so
`value: 2.00, per: 1000, unit: row` — and `cost_view` divides, serving `usd: 0.002` per row. Hunter
Domain Search charges 1 SEARCH credit per 1–10 emails returned (`per: 10, unit: record`), so `usd`
is the linear slice ($0.00245/email) that reserve can scale with `limit`. A live hit does not sell
that slice: it bills one whole credit (~$0.0245) for one email or ten (observed 2026-07-31).
`cost.display` with `grouped` + `round_up` advertises the credit (`display_usd: 0.0245`, "started
10 emails"); `Catalog.advertised_usd` is what `catalog_search` / `catalog_get` put on
`usd_per_call`. Settlement still reads `usd` and the derived email-count rule — display only.
Akta bills 1.5 credits per 50 reviews the same `per` way. Without `per`, every one of those had
to be either wrong or rounded into prose. A scalar `unit: character` is request-priced rather than
page-priced: `_body_text_characters` counts the top-level JSON `text` string and multiplies the
normalized per-character USD rate. Invalid JSON or a missing/empty string reserves one character,
never zero; the normal request/envelope checks decide whether the provider served anything and
`per_success` releases a rejected call.

**Three kinds of denomination convert, and they convert differently:**

- **A real currency** (`currency: USD`, `CNY`) uses `fx.yaml`'s `rates_to_usd`, keyed by currency.
- **`currency: credit`** is NOT a currency. A credit is a PROVIDER-SCOPED unit — one scrapecreators
  credit and one lusha credit have nothing to do with each other — so it converts with the rate for
  the endpoint's provider from `fx.yaml`'s `credit_rates_usd` block, keyed by service. That is why
  `cost_view(cost, provider)` takes the provider: the same `value: 1, currency: credit` is worth
  $0.00188 on scrapecreators and $0.1248 on lusha.
- **`currency: unit`** is the provider's own METER: Semrush's "API units", Majestic's three
  independent allowances, Moz's row quota. `unit` names which meter, and the rate comes from
  `fx.yaml`'s `unit_rates_usd[provider][unit]`. A provider can spend several meters at once —
  Majestic's analysis / retrieval / index-item units no more convert into each other than two
  providers' credits do, so each gets its own row. Before this existed, Moz's `quota_rows` blocks
  carried no `currency` at all, defaulted to USD, and served every Moz route as costing $1.00.

A `credit_rates_usd` entry may carry **`kind: treg_shared_plan`**: a rate TREG SET for a flat-fee
provider (a subscription with a rate limit or unlimited calls), where no per-call vendor price can
exist. The credit is then "one call on treg's shared plan" and the machinery is unchanged — the
honesty lives in the entry: the basis must start with "treg shared-plan rate", name the vendor fee,
and state the break-even volume, and `fee_usd_month` must be present as data (the validator's
`check_fx` enforces all of it). The rate is reviewed monthly against `reconcile.shared_plan_recovery`
and edited by hand. The full ladder: docs/SHARED-PLAN-PRICING-PLAN.md; the billing side (429 never
billable, the recovery report): architecture/money.md.

For synchronous providers that disclose the exact charge in the response, a paid cost may declare
`reported_charge: {path: ..., unit: usd|credit}`. The catalog estimate still reserves a safe
ceiling. A finite nonnegative response value settles the call at that amount; missing, invalid, or
non-finite evidence falls back to the normal estimate/miss rules. Credit-denominated evidence
requires a provider rate in `fx.yaml`, and the request freezes that conversion before relay so a
later rate edit cannot re-price the in-flight call. `reported_charge` is generic catalog metadata,
not a provider-specific billing branch, and cannot be combined with `cost.settle`.

`platform_request` fixes exact body values needed only on the shared credential. The complementary
`platform_bounds` mapping requires a declared numeric body field and a finite in-schema min/max;
resolution rejects a missing, Boolean, non-finite, or out-of-range value before reserve. These
controls never narrow a team's own credential.

A second treg-set kind, **`kind: treg_trial`**, prices a provider at exactly **$0** with a
`trial_calls_per_team_day` allowance as data beside the zero: a capped taste served on treg's own
FREE-tier key. The allowance is what makes $0 honest — at zero the price gives no brake, so the cap
is the congestion control (`_enforce_trial_allowance`, per team per UTC day, successful platform
calls with a non-free catalog cost only, fail-closed). Free endpoints, failed calls and BYOK calls
do not consume it. `cost_view` attaches the allowance to every $0 it serves, because a bare $0.00
reads as unlimited. The validator refuses a non-zero "trial" and a zero with no allowance.

Each `credit_rates_usd` / `unit_rates_usd` entry carries `usd` plus the `basis`/`source`/`checked` that justify it —
the cheapest PUBLICLY listed tier (plan price ÷ credits included), so the served figure is an upper
bound on real spend, never an under-estimate. `usd: null` is a deliberate state, not a gap: the
provider publishes no per-credit price (sales-negotiated like Crunchbase, or not
credit-priced at all like BrightData). Those endpoints keep `cost.usd = null` and display natively
("3 credits/success"), because a guessed dollar figure is worse than an honest credit count. Both
blocks are hand-maintained and must stay ABOVE `rates_to_usd:` — `catalog_fx_update.py` rewrites the
file from the text before that key and discards anything below it.

#### Provenance — `confidence` is a claim about the PRICE, not about the route

`verified: 2026-07-28` on an endpoint says the route answered. `cost.confidence: verified` says the
money figure was confirmed. They are independent, and conflating them is how a guess gets spent:

| `confidence` | what earns it |
|---|---|
| `verified` | observed being billed on a real call (`source: observed`), or read from the provider's own live rate card (`source: rate_card_api` — TikHub's `get_all_endpoints_info`, DataForSEO's `/appendix/user_data`, ScrapeCreators' `credits_charged` in its OpenAPI) |
| `documented` | transcribed from the provider's docs or pricing page |
| `inferred` | the figure is a floor or the top of a published range — a base fee with a per-row half on top ("1 credit base + 1 per ad"), a spread ("1–9 credits", "$0.50–$5.00 per 1,000"). The recorded number is not the whole charge, and the note says what else applies |
| `unknown` | no figure is published anywhere citable. `value` MUST be null and `note` MUST say why |

Rules the validator enforces: `value: null` and `confidence: unknown` appear together or not at all;
a `verified`/`documented` price names its `source_url` (`source: observed` is exempt — its evidence
is the captured example response, not a page that may have moved); every priced entry carries
`checked`, and CI WARNS past 90 days. A file whose header says `UNVERIFIED` caps its prices at
`documented`: nothing in it has been called, so no price in it can have been seen being charged.

Free is spelled exactly one way — `type: free, value: 0, currency: USD, unit: call` — and needs no
provenance, because 0 does not move and there is nothing to re-check. It was previously written
three incompatible ways across 661 endpoints, which left `cost.usd` null on most of them:
indistinguishable, downstream, from "price unknown".

`scripts/catalog_cost_provenance.py` owns the mapping from what the repo knows about a provider's
pricing to these keys, and is re-runnable — the extended tier is regenerated wholesale, so
provenance typed by hand into a generated file would not survive the next `catalog_ingest.py`.

#### Platform-eligible — when treg may serve a catalog fallback

`Catalog.platform_eligible(endpoint)` is the single predicate behind catalog fallback access.
Most eligible rows use prepaid platform-key tier 4. A row with `platform_auth: anonymous` instead
uses the provider's verified public route without a credential. One implementation keeps the API,
validator and proxy in agreement. Eligibility requires ALL of:

- `cost_view(...)["usd"]` is not None — the charge is machine-computable;
- `cost.confidence` is `verified` OR `documented` (policy widened 2026-07-31: a rate the provider
  itself publishes is billable; `verified` stays the gold standard the drift reports police, and
  `inferred`/`unknown` stay refused — a guess is not a rate);
- `scope != own_account` and `kind != account` — the provider's own bookkeeping is never worth
  spending on, and an own-account route needs the caller's own credential by definition.

The live-called `verified:` stamp is no longer required (same 2026-07-31 change): a broken route
fails unbilled under `per_success`/`per_result` billing, providers that report in-band settle at 0,
and the fail-closed daily platform cap bounds whatever remains — coverage beats caution now that
the reserve/settle machinery is proven. Eligibility alone still enables nothing. A normal platform
call requires a configured key and the provider allow-list (`platform_key_for`). An anonymous
fallback requires only the same provider allow-list (`platform_provider_enabled`) because it loads
no provider key.

`platform_auth: anonymous` is deliberately narrower than ordinary eligibility. Catalog validation
accepts it only for live-verified, free `GET` operations with `scope: any_account`, no provider
authorization metadata, and no shared async-resource lifecycle. Resolution preserves the normal
team-tool then team-credential precedence. Only when both miss does `_anonymous_offer` create a
virtual tool with an empty binding list and credential tier `anonymous`. The faithful relay then
forwards the caller's request without injecting a provider credential. This is generic catalog
metadata; the call runtime contains no provider or path list.

Routed ranking assigns separate priority to the four tiers: team tool or credential first,
anonymous fallback second, and paid platform-key access third. This keeps the own-key guarantee
intact if an anonymous endpoint later receives a verified routing adapter.

The doctrine is asymmetric on purpose: **a missing or unknown price reads as "refuse", never as
free.** An endpoint with no `cost` block at all is therefore not platform-eligible without anything
having to be written out for it, which is why the extended tier's unpriced routes need no
annotation. Where an endpoint carries only `observed_cost` (DataForSEO prices per API family, not
per route), `_effective_cost` synthesizes the block with `source: observed, confidence: verified`
and `checked` = the verify date: a figure the provider itself reported charging is the strongest
provenance the catalog has.

### Core-wins dedup compares NORMALISED paths — except on Graph

A hand-curated core file and a machine-readable spec never agree on placeholder spelling: core says
`/v1beta/properties/{property_id}:runReport`, Google's discovery document says `{property}`. A naive
`(method, path)` comparison therefore misses, and the endpoint ships in both tiers — that is the
DataForSEO `/v3` bug below, in its other form. The Google and X ingesters compare with every
`{...}` collapsed to `{}`, so the two spellings match.

Meta is the exception and uses exact comparison, because on the Graph API the node id IS the first
path segment: `/{post_id}/insights` and `/{page_id}/insights` differ only by the placeholder name
and are genuinely different endpoints. Normalising there would silently drop post insights because
the core file curates page insights.

## Process — adding / curating a provider

Do these steps in order; each has a hard success criterion.

1. **Ingest.** If the provider publishes OpenAPI (`/openapi.json`), fetch it and list candidate
   operations from there — do not hand-transcribe paths (that is how typos ship). Otherwise work
   from the official docs and record `source.openapi: null`.
2. **Select.** Curate, don't mirror: pick the ~8–15 endpoints an agent would actually reach for,
   and ALWAYS include the endpoints matching capabilities other providers already implement —
   overlap is the point (comparison + failover). Skip exotic ops.
3. **Map.** Assign each endpoint a capability from `capabilities.yaml`. Missing job → add it under
   `proposed_capabilities:` in your provider file, don't edit the shared taxonomy in parallel work.
4. **Describe.** Fill `input` from the spec/docs: param names, types, which are required, where
   they ride (path/query/body). Copy real constraints ("one of A|B") into `note`.
5. **Cost.** Record the provider's price model per endpoint from their pricing page — with its
   provenance (`source`, `source_url`, `checked`, `confidence`) and its unit (`per`, `unit`), per
   "Cost" above. `quota_rows` is for row-quota APIs (Moz). Unknown exact value → `value: null` +
   `confidence: unknown` + a `note` saying why. If the provider exposes its rate card as an
   endpoint, prefer it over the pricing page and record it as `source: rate_card_api`: it is
   re-checkable, which is what lets treg serve the route on its own key.
6. **Test-request.** Give every endpoint a `test_request` that is CHEAP (smallest limit, one item,
   public well-known target — e.g. user "tiktok", domain "moz.com"). This is what verification and
   future health checks replay, so it must not burn meaningful credits.
   ⚠️ Quota trap (learned live, Moz 2026-07-28): never probe an endpoint with an empty body/params
   "expecting a free validation error" — an endpoint with NO required params answers with its FULL
   default result set and bills for it (Moz's global_top_* ate an entire 50-row period quota in two
   calls). Always pass an explicit smallest limit, and on row-quota APIs check the usage endpoint
   before and after the first call.
7. **Verify + capture.** Run `scripts/catalog_verify.py <service>.yaml` with the credential in the
   `TREG_CATALOG_CRED` env var. It calls every endpoint's `test_request`, checks `expect`, writes
   the truncated example response to `examples/`, and prints PASS/FAIL per endpoint. Stamp
   `verified: <today>` ONLY on endpoints that passed — documented ≠ verified; docs lie.
8. **Scrub.** Read every captured example: replace anything personal that is not the public test
   target's own public data. The account-info endpoints of YOUR OWN key (quota, balance) must have
   emails/ids masked before commit.
9. **Validate.** `scripts/catalog_validate.py` must exit 0: schema shape, unique ids, capability
   and platform referential integrity, example files exist for verified endpoints, provider exists
   in `oauth_providers.py`.

Success criteria for a provider PR: validator exits 0; every endpoint either carries a `verified`
date + example file or an explicit comment why it could not be live-tested; no credential value
appears anywhere in the diff.

## Process — bulk-ingesting the extended tier

```
uv run python scripts/catalog_ingest.py tikhub          # one provider
uv run python scripts/catalog_ingest.py all --refresh   # every provider, re-downloading the specs
uv run python scripts/catalog_validate.py               # must exit 0
```

The script owns `<service>.extended.yaml` end to end: it fetches the provider's spec, maps every
route to a platform, drops what the core file already covers, and rewrites the file. Downloads are
cached under `~/.cache/treg-catalog-ingest` (override `TREG_INGEST_CACHE`); `--refresh` re-fetches.
Output is deterministic — a re-run with unchanged upstreams produces a byte-identical file, so a
diff always means the provider changed.

Adding a provider means adding an `ingest_<service>()` function and registering it in `INGESTERS`.
Three rules it must honour:

- **Never probe with a real call.** Discovering an HTTP method by sending a GET is how you get
  billed 1400 times (see the quota trap above). TikHub's methods come from an `OPTIONS` request,
  which Starlette answers `405 + allow:` before the handler — and therefore the meter — runs.
- **The published spec outranks the probe** (`resolve_method`). A wrong method is not a cosmetic
  error: treg *enforces* the recorded verb, so the endpoint becomes uncallable from both sides at
  once — POST refused here ("… is GET"), GET refused upstream (405). The probe is weaker than it
  looks: a preflight answering with a method *list* walks its preference order and comes out `GET`
  whatever the handler takes. So when the OpenAPI declares exactly one method, that wins; probe and
  docs are the fallback for routes the spec doesn't describe.
- **The verb and the parameter POSITION are one decision, from one document.** TikHub's Apifox docs
  list every TikTok-Ads parameter under `parameters.query` while its OpenAPI declares the same route
  POST-with-a-JSON-body. Taking the verb from one and the position from the other yields a POST
  carrying its arguments in the query string — still uncallable, just differently. When the spec
  declares a JSON body and the docs gave us none, the documented "query" parameters ARE that body.

### Catalog rot is a category of bug, and it is not the ingester's fault

The 2026-08-17 TikTok-Ads breakage was first written up here as an ingester defect. It was not, and
the correction matters more than the original claim. Those twelve routes really were `GET` when
ingested: TikHub's July spec says `get`, and the captured `example_response` is a **real billed 200
from a GET on 2026-07-27**. TikHub moved them to POST some time after. The catalog did not mis-read
the provider — it went stale, and at the time **nothing re-checked a provider's spec for drift**.

That reframed the fix. Preferring the spec over the probe is a genuine hardening, but it only helps
*at re-ingest time*, and only if the cached spec was refreshed — the cache under
`~/.cache/treg-catalog-ingest` is what an unqualified `catalog_ingest.py <provider>` reads, so a
re-run against a months-old cache faithfully reproduces months-old truth. A `verified:` stamp is
evidence about the day it was written and nothing after it.

`scripts/catalog_drift.py` now closes that gap without making a paid API call: it discovers public
OpenAPI documents from each provider file's source provenance, downloads the document with no
credential, and compares every checked-in `(path, method)`. Plain JSON is preferred; the same
`salvage_json_map` used by the ingester recovers a complete `paths` map from a truncated document,
and YAML OpenAPI is accepted too. An unmarked missing path, method change, or marked route that has
reappeared exits non-zero. Known absent marked rows are reported as `acknowledged`, not drift. The
daily `catalog-drift.yml` workflow currently runs TikHub—the provider with demonstrated production
rot—and the script remains provider-general for every catalog file that cites a public OpenAPI URL.

### Retired and broken endpoints are tombstones, not offers

Provider rot must not turn an id an agent cached yesterday into either a bare provider 404 or an
unexplained registry 404. Keep the row and add:

```yaml
status: retired                 # or broken
status_note: why it is gone and what changed
superseded_by: provider.live-id # optional; only when the operation is genuinely equivalent
```

`catalog_store._parse` always retains the normalised row in `by_id`, so direct endpoint inspection
can return its story, but excludes it from `endpoints`, the source for search, browse, capability
counts and platform eligibility. On a direct endpoint-id call, `_resolve_marketplace_call` raises an
actionable 410 before choosing or loading any credential; the pre-relay audit class is `retired`.
`/catalog/endpoints/{id}/access` applies the same gate. This is catalog fallback only: an org tool
whose exact name matches the retired id resolves first and remains callable, and URL passthrough
never enters catalog lookup.

The validator treats the marker as a contract: only `retired` and `broken` are valid; every marker
needs a non-empty note; `status_note` and `superseded_by` cannot float without `status`; and a
successor must be a different, existing, live catalog id. A marked id is therefore an explanation,
not an alias chain or a route treg will still spend against.

The marker is not TikHub-specific, and the provider does not have to answer 404 for a row to be
dead. `lusha.x.decision-makers` (2026-09-19) is the second shape: Lusha removed
`POST /v3/contacts/decision-makers` on 2026-08-12 in favour of `/v3/contacts/buying-group`, the only
operation that accepts `contactsLimit` and `personas` - but a legacy handler kept answering
companies-only bodies on the old path and rejected the cap parameter with a 400. A route that still
returns 200 while silently ignoring the caller's spend control is broken in the way that costs the
most (every call ran at the 60-contacts-per-company default, 1 credit each), so it is retired with
`superseded_by: lusha.x.buying-group` even though the old URL "works". The successor was written from
the provider's OpenAPI bundle without a live probe and says so with `skipped` and no
`example_response`; an invented fixture would be worse than none. `lusha.extended.yaml` is
hand-maintained (no ingester reads Lusha's client-rendered reference), so the "regenerated wholesale"
caveat above does not apply to it and the tombstone survives.

### `platform_blocked:` — works upstream, but not on treg's plan

A third state sits between "offer" and "tombstone": the route works and the price is real, but
treg's own subscription cannot serve it — Akta answers every alternative-data call (jobs, posts,
website-traffic, employee-reviews, headcount-trends, product-reviews) on the shared key with a
free 403 "Your current subscription does not include access to this endpoint". Marking those
`status: broken` would be a lie (a caller's OWN key on a bigger plan serves them fine) and leaving
them unmarked sold them as platform offers — a customer ran a whole evaluation lane into that wall
of 403s before learning the gate existed. `platform_blocked: <reason>` keeps the row in discovery
but makes `platform_eligible()` refuse it, and the reason rides on the served row so every surface
can say "bring your own key" *before* the call instead of relaying the 403 after it.

- **Platform is the system the data is ABOUT**, not the API family it lives under: DataForSEO's
  `/v3/merchant/amazon/products/live/advanced` is `amazon`, not `merchant`. Anything not tied to
  one system is `web`. Every new slug goes into `capabilities.yaml`'s `platforms` in the same
  change — the script exits non-zero if a generated platform is unknown, which is the guard.
- **Normalise slugs across providers.** Just One API calls it `douyin-tiktok-china` and TikHub
  calls it `douyin`; if both don't land on `douyin`, the marketplace shelf splits in two and the
  cross-provider comparison the catalog exists for silently stops working.

### The first-party OAuth wave (2026-07-28; Google Tag Manager added 2026-08-27)

The scraper providers sell breadth and their extended tier reads as a menu. The nine providers
where treg owns the OAuth app are the opposite question — *what can this one connected account
actually do?* — and their sources differ per provider:

| service | source | entries | scope gaps |
|---|---|---|---|
| google-search-console | searchconsole v1 discovery | 7 | 0 |
| google-analytics | analyticsdata + analyticsadmin v1beta discovery | 63 (55 on the admin host) | 32 |
| google-tag-manager | tagmanager v2 discovery | 98 | 8 |
| google-business-profile | six My Business discovery docs + 7 hand-listed legacy v4 routes | 60 (45 off-host) | n/a |
| youtube | youtube v3 discovery + the published quota-cost table | 76 | 2 |
| google-ads | the GAQL resource reference — one entry per queryable resource | 42 | 0 |
| x | X's own v2 OpenAPI | 168 | 91 |
| facebook / instagram / meta-ads | hand-curated from the Graph HTML reference | 26 / 22 / 34 | 6 / 2 / 8 |

Three things generalise from it:

- **Google publishes a Discovery document for every API** at
  `https://<service>.googleapis.com/$discovery/rest?version=<v>` — httpMethod, flatPath, a
  description, the full typed parameter list with required flags, and the OAuth scopes each method
  accepts. It is the same class of source as an OpenAPI spec and should always be preferred to the
  HTML reference. Scopes are ALTERNATIVES (holding any one suffices), so coverage is an
  intersection, not a subset. The My Business documents are the exception that declares no scopes
  at all, which is why that provider has no computable gaps.
- **Google Tag Manager keeps risky administration outside the grant.** Its core catalog presents an
  audit → workspace edit → version/publish workflow across cumulative `read`/`write`/`manage` tiers.
  The generated catalog still lists methods requiring container deletion or account/user management,
  but marks all eight with `scope_gap`; those three scopes are intentionally never requested.
- **Google Ads is a resource list, not a route list.** One endpoint (`googleAds:searchStream`)
  answers every read and what varies is the GAQL `FROM` clause, so the unit of coverage is the
  queryable resource. Forty entries share a path and differ in `input.note` and `docs_url`.
- **No test_request anywhere in this wave.** Every route needs a property id, a customer id or a
  Page id that belongs to the connected business and that no spec can supply. They are verified by
  replay against a live connection (`--via-treg`), not by a generated blind call.
- **Instagram Messaging is deliberately core-curated.** Conversation listing and message sending
  carry the Page-token/IGSID/window constraints and complete Try-form inputs in `instagram.yaml`;
  conversation listing targets the linked Facebook Page id (`/{page_id}/conversations` with
  `platform=instagram`), and replies use that Page's `/{page_id}/messages` edge—not the Instagram
  account id used by profile/media routes. The send route remains explicitly unverified so no
  catalog sweep can deliver a real DM. The Instagram generator omits these two messaging routes;
  they exist only in core. Meta's exact `(method, path)` core-wins dedup still protects all other
  generated routes whose placeholder names carry different Graph object semantics.

## Process — bulk-verifying the extended tier

```
TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify_extended.py tikhub --dry-run
TREG_CATALOG_CRED='<secret>' uv run python scripts/catalog_verify_extended.py tikhub --budget 1.80
uv run python scripts/catalog_validate.py            # must exit 0
```

`--dry-run` prints the queue and what it would cost at list price; nothing is called. The real run
goes CHEAPEST FIRST and stops before any call that would push the run past `--budget`, so a
half-finished run has verified the cheap majority rather than an arbitrary slice. Results are
written back into the yaml after every run and a re-run skips what already carries `verified`,
which makes an interrupted run resumable instead of a repeat bill.

Three things to know before pointing it at a new provider:

- **A missing `cost` reads as free, and silently disables `--budget`.** DataForSEO publishes prices
  per API family, so not one of its 216 extended entries carries a `cost` block — which made the
  spend cap inert: a run queued the whole platform at an estimated $0.000 and still spent real
  money, with only the after-the-fact balance readback noticing. The fix is `observed_cost`: the
  charge the provider states in its own response (`tasks.0.cost`), written onto the endpoint at
  verify time and used to budget the next run. It is the better number regardless — measured, not
  transcribed from a price list — and summing it gives a defensible run total, which balance
  arithmetic cannot because it cannot separate our calls from anything else using the same key.
  DataForSEO's full sweep, summed this way: $4.85521 across 177 endpoints.
- **`observed_time` is measured, not read.** The wall-clock seconds WE waited for the response,
  recorded on the endpoint next to `observed_cost`. Two reasons it is not lifted out of the body:
  only DataForSEO reports its own duration, and TikHub's `time` field is a TIMESTAMP
  ("2026-07-27 23:27:48"), so an extractor trusting the field name would write a date into a
  numeric column. It is also the number that matters — `CALL_TIMEOUT` applies to OUR client. Worth
  having because a timeout is recorded as the endpoint's verdict, and the same DataForSEO route can
  swing wildly: `merchant/amazon/sellers/live/advanced` answered in 9s and 105s on two identical
  calls, `products/live/advanced` in 26s and 55s. Under the old 60s ceiling both were coin flips
  that would have written "unverified" onto a healthy route on some runs and not others. Elapsed
  time predicts nothing about price, either: a 0.04s call cost 4x a 26s one.
- **Cost accounting assumes the provider bills per success.** The run's spend is the sum of the
  prices of the calls that returned 2xx. If a provider bills per *call*, that is wrong in the
  optimistic direction — check the balance delta the script prints against its own estimate before
  trusting a large run. It reads the balance before and after for exactly this reason.
- **The parameter source has to give example VALUES, not just names.** A generated test request
  that invents an id verifies nothing: it produces a 404 that looks like a broken endpoint. If the
  provider documents parameters without examples, the honest output is `untestable`, not a guess.
  (For TikHub, the values come from `sampleValue` in their Apifox docs API — their own demo values.)
- **Examples are trimmed to ~2 KB, arrays to one item.** At 1385 endpoints, core's 10 KB cap would
  add ~14 MB of JSON. An extended example is there to show the response SHAPE.
- **Check for a PER-ROUTE rate limit, not just the account-wide one.** TikHub allows 10 req/s on
  the account but only 1 req/s on any single route. A global pacer does nothing about that — it
  spaces consecutive requests across *different* routes — while a retry by definition hits the same
  route again. Retrying after 0.5s therefore guarantees a 429, and the 429 lands in the file as
  though the endpoint had failed: 66 endpoints on the first full run carried a rate-limit verdict
  that said nothing about the endpoint. Any same-route retry has to wait out that window
  (`PER_ROUTE_GAP`), and 429 must count as retryable rather than as an answer.

⚠️ Read the recorded failures before believing them. A `unverified:` line is evidence about one
call at one moment, and the failure modes that look identical in a summary count are not: a 400
that repeats is a verdict, a 400 that passes on the third try is a flaky upstream (TikHub's
LinkedIn family), and a 429 is usually our own fault. Grouping the failures by status code and by
platform family, then re-running one family, is what separates them — pass rates per platform in
the same run ranged from 8% to 100%, and the low ones were mostly not the provider's fault.

**A fix landing mid-sweep leaves the un-noticed batches wrong.** DataForSEO's 8-batch sweep ran
across the moment the `/v3/v3` URL bug (see below) was fixed. The `web` batch failed loudly at 100%
and was re-run after the fix; the `amazon` batch had failed the same way, nobody re-ran it, and its
pre-fix results merged into the file as 7 endpoints marked `unverified: http 404` — which then read
as a retired Amazon route family. All 7 passed on a re-run, first try, for $0.075. Nothing was ever
wrong with them.

Two signals identified it, and both are worth checking before believing any block of failures:
- **The failures aligned exactly with a batch boundary.** `amazon` was the only platform in the
  file with a single `unverified`, and it held 100% of what that batch touched. Endpoint problems
  do not respect our batching; tooling problems do.
- **Siblings verified by a DIFFERENT code path passed.** `dataforseo_labs/amazon/ranked_keywords`
  and `merchant/amazon/asin` were green in the same two families, verified earlier by
  `catalog_verify.py` rather than the bulk runner. A family cannot be both retired and working, so
  the disagreement was between our two callers, not about the endpoints.

The general rule: after fixing a bug that could have produced failures, re-run **every** batch that
ran before the fix, not just the one whose failure you noticed. The loud batch is the one you
already know about; the quiet ones are what ship a false verdict into the catalog.

**How many passes, and when to stop.** On TikHub, LinkedIn went 8% → 27% → 67% → 90% verified over
four passes with no change other than being asked again — 43 of 48 endpoints that a single pass
called broken. Conversion per pass is the stopping signal, not a pass count: 672, +30, +16, +14,
+2. A pass that converts ~2 is convergence, and what remains after it is genuinely broken (for
tikhub, 107 of the final 115 failures are the provider's own "Request failed. Please retry." after
six attempts each). Raising `--retry-attempts` is the cheapest lever available on a flaky provider
and costs nothing but wall-clock under per-success billing.

Just One API shows the same curve from its far end, and what a *confirmed* verdict costs to
establish. Its 13 failures were one uniform error, `code 301 COLLECT FAILED`, clustered in whole
families (Kuaishou, Taobao, JD) — the exact shape that ought to mean "our fault". They survived 3
retries inside a call, then 4 runs, then a serial pass hours later, then a sixth with retry depth
raised 3 → 6: the last two passes converted one endpoint each, for ¥0.35. Same decay, further
along, so its 11 survivors are evidenced verdicts rather than impatience. The rule is therefore not
"retry until it works" but **retry until the result stops changing**.

Two things generalise from that. The one endpoint that flipped was LinkedIn — the family that is
also flaky through TikHub, a different vendor entirely. That is the scraped platform defending
itself, not the API vendor, so expect it from anyone scraping LinkedIn, and treat two LinkedIn
scrapers as one point of failure rather than a redundant pair. And retrying is only free under
`per_success` billing (both social providers); on a `per_call` provider like DataForSEO each retry
and each extra pass is a purchase, so that budget belongs in the plan rather than in a loop.

One caveat on reading `per_success` as "bad input is free": the provider decides what counts as
success. TikHub answers some invalid inputs (a bogus channel id) with HTTP **200**, the error nested
in the body, and "this request will incur a charge" — so the platform meter bills it, faithfully to
what TikHub charges us. When TikHub uses a real 4xx it says "You won't be charged" and the meter
releases the hold. Verified live 2026-07-30.

Then read a sample of the captured examples for PII before committing, as with core — bulk capture
does not remove the scrub step, it just means sampling per platform family rather than reading all
of them.

### `skipped:` — the fourth state, for a call that was affordable but not made

`verified` / `unverified` / `untestable` above cover *passed*, *called and failed*, and *no call is
possible*. Verifying two paid providers against nearly-empty accounts surfaced a fourth case they
cannot express: the test request exists, the call would very likely pass, and it was deliberately
NOT made because the balance was needed elsewhere. Calling that `untestable` is a lie about the
endpoint, and `unverified` is a lie about the provider — it invents a failure that never happened.

```yaml
  skipped: family verified via dataforseo.x.backlinks-summary-live; the DataForSEO account held
    $0.739 on 2026-07-28 and $0.58 of it was spent verifying one endpoint per API family
```

The reason must say what to do about it, which in practice is one of: **the sibling that WAS
verified** (whole-family skips — 155 of DataForSEO's 216, 6 of Just One API's WeChat endpoints at
¥1.0–1.5/call), or **why one call is too expensive to justify** (DataForSEO's `llm_responses`
routes exceed the $0.15/call ceiling). A `skipped` entry needs no re-investigation — only money —
so a top-up plus a re-run clears them in bulk, while `unverified` and `untestable` need a human.

The state distribution is itself the report. Just One API: 227 verified, 21 unverified (11 of them
the provider's own `code 301 COLLECT FAILED` after six passes, 5 `NO PERMISSION` — an account fact,
not an endpoint fact), 6 untestable, 6 skipped. DataForSEO after its top-up and full sweep: 177
verified, 0 unverified, 39 untestable, 0 skipped — nothing in its extended surface is broken, and
every remaining gap is structural (23 routes whose spec ships no example body, 16 on_page routes
needing an async crawl id).

### Two provider-specific traps

- **Chained ids.** Most detail endpoints need an id no spec can supply. The working order is:
  call the search/list endpoints first, harvest ids out of their responses by normalised field
  name (`aweme_id` fills `awemeId`), then call the detail endpoints, then repeat — Just One API's
  WeChat Channels comments need an `objectId` that only exists after search → `convert-export-id`.
  35 of 260 endpoints were verified only because of that second and third pass.
- **A query token is not always a token.** Just One API's POST routes take
  `application/x-www-form-urlencoded` and read the credential from the FORM BODY; leaving it in
  the query string as well makes them fail with a misleading `TOKEN INVALID/UNACTIVATE`.
  `catalog_verify.py --extended` moves it for `bodyType: form` entries.

### DataForSEO paths carry `/v3`, its base_url ends in `/v3`

`catalog_ingest.py` writes DataForSEO's extended paths exactly as the spec spells them
(`/v3/serp/google/organic/live/regular`) while `OAuthProvider.base_url` already ends in `/v3` and
the core file's paths are relative to it (`/serp/google/organic/live/regular`). Two consequences:
`catalog_verify.py --extended` strips a leading duplicate of the base_url path before calling, and
**the ingester's core-wins dedup silently misses**, because it compares `(method, path)` across the
two spellings — every DataForSEO route curated in core is also present in the extended file under
a different id. Fixing that belongs in `catalog_ingest.py` and needs a regeneration.

### Upstream quirks live in the row, not in this fragment

A provider's live API often disagrees with its own docs: a documented field is rejected, an enum is
narrower than the spec, a `limit` is ignored, a location only works with one language. The fix for
such a report is the row's `note` (and `enum` / `example` / `test_request` where the shape itself
was wrong) in the catalog YAML, because that is the text `catalog_get` serves to the agent that is
about to make the call. `carry_verification` carries `input` across a re-ingest, so a hand-corrected
note in an extended file survives regeneration; a generated `summary` does not, so a correction
there goes into the ingester's source table as well.

Neither this fragment nor the test suite restates a single row. A section per report turns the
design doc into a changelog, and a test that asserts a note's wording fails on the next honest
rewording without protecting any behaviour. What earns a test is a rule over a whole class of rows
or a code path: for example DataForSEO's **Live** routes accept exactly one task per POST array
(extra elements come back as per-task `40000` and `$0`, while Standard `/task_post` batches up to
100), which `test_live_endpoints_have_single_task_test_requests` checks over every Live row. Do not
auto-split a multi-task array into billed calls.

## Choosing between providers (`domain/catalog/stats.py`)

307 capabilities are served by more than one provider, and prices inside one capability differ by up
to **261×**. So "which provider" is a real decision, made on every call.

**The agent makes it, not treg** — see `docs/CAPABILITY-CHOICE-PLAN.md` for the measurement behind
that. Two reasons, and the second is the load-bearing one. Providers of the same capability take
*different requests* (only 5 of 171 match exactly), so a router would need a canonical schema treg
does not have; and they sometimes ask a different QUESTION entirely — `hunter.people.email.find`
wants a domain and a name, `leadmagic.x.b2b-profile-email` wants a LinkedIn URL. **Only the caller
knows which inputs it holds.** A router picking on price would choose the second for someone holding
a name, and fail. Routing would also have been the first feature to break the founding rule that treg
relays rather than models.

What treg owes instead is the half only treg can supply, because only treg sees every call from every
tenant: `endpoint_stats.observed()` aggregates **success rate, p50/p95 latency, last-answered and
sample size** per endpoint from `CallRecord` — which has recorded `endpoint_id`, `status_code` and
`duration_ms` since the marketplace shipped and was never read. It rides on
`/catalog/endpoints/{id}`, attached to the endpoint **and every sibling**, because the choice is made
on that page and an agent will not make a second round-trip to compare reliability.

The same page states the one price the `cost` block cannot: what the call bills when treg's own
account is out and the overflow relay serves it. `routers.catalog._overflow_disclosure` reads the
enabled `OverflowRoute` through `domain.capacity.routes_view` (a read, the worker stays the only
writer) and puts `overflow_price_usd`, `overflow_price_unit` and `overflow_via` on the endpoint view
plus a hint, only when the deployment can actually relay it (`TREG_OVERFLOW_MODE=on`, a key for the
aggregator, `platform_eligible`, an enabled route). A catalog-free endpoint with an overflow route is
the case that made this necessary (`apollo.people.search`, 2026-09-08); the MCP `catalog_get` lifts
the three fields onto its result so the schema advertises them.

The aggregate is authoritative but no longer request-time. `stats.EndpointObservationReader` is the
narrow domain port, and bootstrap supplies `CachedEndpointObservationReader` around a
`PostgresEndpointObservationReader`. Entries are keyed by endpoint id. They are fresh for five
minutes; from five through thirty minutes HTTP and MCP search serve the old value immediately and
start a refresh; after thirty minutes they publish no observation until a refresh succeeds. A cold
process therefore answers the first requests without reliability weighting instead of making either
Catalog entry point wait for Postgres. The API shape does not change: `observed` is `null` when no
acceptable entry exists.

Refresh is process-level singleflight. Concurrent misses join one shared Task, duplicate endpoint ids
already in flight are not queued again, and the Task batches the requested ids. Its
`PostgresEndpointObservationReader` opens an independent session only around one small read and
closes it as soon as that finishes. HTTP `/catalog/search`, both MCP catalog-search tools,
routed planning in `application.call.route.build_plan`, and the prose pages that print observed stats
(`/use-cases/*`, `/workflows` and `/workflows/*`) receive the same reader instance from bootstrap, so
their request paths have no observation DB dependency, check out zero connections, and join the same
refresh Task. A refresh failure keeps stale
entries, backs off before retry, and never changes the Catalog response status; a failure with no
cached entry is honest emptiness. The adapter exposes entry-level `fresh`, `stale`, and `miss`
counters plus `refresh` and `refresh_failure` counts. Its invalidation story is the two TTLs: deploys
and process restarts begin cold, and no cross-instance correctness depends on the cache.

**The evidence is folded once, off the request path** (`application/catalog_stats.py`, run by the
`treg-worker catalog stats` cron). Refreshing straight from `callrecord` meant every web process
re-aggregating thirty days of audit rows for an endpoint and its siblings whenever its cache
expired, and again from cold after each deploy: on a large audit table that is tens of seconds per
pass, each pass evicting the pages the money path needs. The worker instead
walks the audit table by primary key from a persisted cursor (`EndpointStatCursor`) and folds each
row into one `EndpointDayStat` bucket per endpoint per UTC day: counts, the newest success, the
`hit`/per-success tallies, and a uniform reservoir of at most `stats.LATENCY_SAMPLE` successful
durations. Rows younger than sixty seconds wait for the next run so an audit insert that commits
late is never skipped; a plain tool call (no `endpoint_id`) and a treg refusal (`refused_by`) are
not evidence and are not folded, exactly as the live query excludes them. The first run bisects the
primary key to the first row inside the window rather than reading older pages, consumes at most
`--max-rows` per run, and the reader keeps computing the live aggregate until a run reports it
has caught up (`caught_up_at`), so a deployment that never schedules the worker behaves as before.
The same fallback applies when the worker stops: a cursor not updated for `STALE_AFTER_S` (two
hours) sends the reader back to the live aggregate with a warning, so a dead cron degrades to the
old cost rather than to buckets that silently age out of the window. Each batch is one
transaction under the cursor row's lock and re-reads every bucket it touches inside that lock;
nothing about a bucket is carried between batches, so two overlapping runs (a slow backfill
still going when the next schedule fires) serialize cleanly instead of one erasing the other's
fold with the cursor already past the rows.
Once caught up, an observation is the sum of that endpoint's day buckets from the day of the
window's start onward (`stats.window_days`, at most one day more evidence than the live cut,
never less), published through the same `stats.publish` floors the live path uses; the fold and
the SQL are held equal by `tests/test_catalog_stats_refresh.py`. Merging days weights each
day's latency sample by the calls it stands for (`Tally.merge`, `Tally.percentile`): a reservoir
is uniform within its day, so a busy day's four hundred samples must count for its thousands of
calls, or the window's p95 would be the quiet days'. Buckets older than the window
are pruned at the end of each caught-up run. `stats.Tally` is the one shape all three paths
share: a day, a merged window, or the live aggregate.

Five rules worth keeping:

- **A 4xx never counts against the provider.** It usually means the caller sent bad parameters;
  counting it would let one agent's mistake make a healthy endpoint look broken to everyone. Only
  2xx versus 5xx decides the rate.
- **405 is the exception, and the rule's own justification is why.** "The caller sent bad
  parameters" cannot apply to a method the caller was never allowed to choose: `/call/` refuses a
  catalog call whose method differs from the recorded one with a 400, *before* relaying. So a 405
  coming back from the provider says the RECORDED METHOD is wrong — a stale contract, which is the
  one thing this module exists to surface — and it counts as decided against the endpoint. Without
  it, the seven straight 405s on `tikhub.x.tiktok-ads-search-ads` sat in the excluded bucket and the
  WORKS column read `— (7)`: indistinguishable from an endpoint nobody had tried. That is the half
  of the 2026-08-17 report that survived two rounds of review — fixing `LAST OK` stopped the row
  claiming success, but only this makes it say *failure*.
- **A treg refusal is not evidence about the endpoint.** Rows with `refused_by` set (a paywall 402,
  a daily-cap 429 — see the data-model fragment) never reached the provider; they are excluded even
  from `samples`, or a burst of refused calls dresses itself up as traffic. The 2026-08-12 Hunter
  incident — 309 refusals next to 488 real calls — is why.
- **`miss` semantics ride on the endpoint.** Some providers answer "asked and answered: no result"
  with an error status (PDL 404s a person it has no record of; Hunter's combined-find does the
  same). Endpoints with evidenced miss behaviour carry a `miss: {status, means}` block in their
  YAML, surfaced through `endpoint_view` — so an agent reads "404 = no match, don't retry" instead
  of treating an expected empty answer as a failure. Only annotate what the wire has demonstrated.
  **The router and the arena read the same block through one function**
  (`routing.contracts.declared_miss`, wrapped by `route._declared_miss` and called by
  `arena.classify`): a child answering the declared 4xx is a MISS — the waterfall goes on and a
  fully-missed call ends as a 200 miss, never `route_failed`. Where one status carries both a
  miss and a fault, `when:` adds a body predicate in the adapter expression language, evaluated
  only on a JSON-object body (`catalog_validate.py` rejects a `when` that is not a comparison or
  call, since a misspelt path would evaluate False forever and silently revert the endpoint to
  "every 4xx is an error"; `endpoint_view` shows agents `status` and `means` but not `when`):
  prospeo answers 400
  for `NO_MATCH` (a miss) and for `INVALID_DATAPOINTS` (a fault), so its three person endpoints
  declare `miss: {status: 400, when: "error_code == 'NO_MATCH'"}`. Provider knowledge lives in
  the YAML; `route.py` never names a provider. Live 2026-09-18: 64% of three days of
  `treg.people.email.find` 502s were a limadata 404 or a prospeo NO_MATCH among otherwise clean
  misses — the block had never been declared on either (limadata's cost note said "a 404 miss is
  free"; prose is not read by the router). Before 2026-09-04 only PDL carried the block; the annotated set (aviato, hunter,
  leadmagic, findymail, companyenrich, thecompaniesapi, fiber-ai, scrapecreators linkedin) came
  from 30 days of prod children answering 404 with a "not found" body, and the router treated each
  as a rejected request: 1,824 `phone.find` parents were 502 in that window, 768 of them with no
  failure but an aviato 404 (voice-ai-outbound's GT report). Only a 4xx is honoured — a
  `status: 200` block (tikhub) is agent documentation; the adapter's own `miss` predicate decides
  a 2xx. Note a `per_call` provider (companyenrich) still bills the request on its declared miss.
  Aviato company enrichment also declares 404 as a miss after the 2026-09-08 Arena sweep
  returned `Not Found` for microsoft.com; its company-enrich documentation identifies the
  response as `Company Not Found Error`. Arena and routed calls use the same metadata.
- **Below `MIN_SAMPLES` we publish the count and nothing else.** "100% from two calls" is noise
  dressed as evidence, and on a quiet endpoint a rate could expose one org's activity. The floor
  applies to **decided calls** (2xx + provider-fault failures), not total traffic: four caller 422s
  cannot lift one 200 or 405 into a published rate. Latency has its own floor of successful calls;
  one success is not both a p50 and p95 merely because enough failures made the rate publishable.
- **Sample size is always visible**, so `100% (8)` cannot beat `99% (121)` by looking rounder.
- **"Free" is a price, not a missing one.** `platform_eligible` used to demand
  `confidence in (verified, documented)` for every route, but `confidence` says how much we trust a
  NUMBER we are about to charge — and a free route has no number. Requiring it anyway refused 61
  endpoints across 8 providers (28 of Hunter's 35) as though "costs nothing" meant "we don't know",
  which is the one distinction this file otherwise keeps apart. A `type: free` route is now eligible
  without provenance; a PAID route without provenance is still refused.
- **A claim never wears a measurement's badge.** The `LAST OK` column prints a bare age when a real
  call produced it and a **`✓` age** when it came from the catalog's `verified:` stamp — the same
  discipline `confidence:` already applies to price. The stamp is the cold-start answer: it covers
  1,380 of 1,810 eligible endpoints for free, which is why the column is useful on day one.
- **`last_ok` means the last SUCCESS.** It was `max(created_at)` over every row, success or not, so
  an endpoint that had been called seven times today and failed all seven read `WORKS — (7)` next
  to `LAST OK: today` — which is how `tikhub.x.tiktok-ads-search-ads` passed for a merely new row
  while being uncallable (2026-08-17).
- **Below the floor, the outcome stays unpublished — not even a yes/no.** The 2026-08-17 fix first
  added `any_ok` ("has it EVER answered?") on the argument that a boolean survives any sample size.
  It did not survive the two rules above. On a quiet endpoint it exposed the *outcome* of one
  tenant's one call, which is half of why the floor exists; and because `samples` counts 4xx while
  successes do not, a single caller's malformed 422 published `any_ok: false` and made a healthy
  endpoint look broken to every other tenant — precisely the failure the 4xx rule prevents. It was
  removed. "Never worked" is read off `ok_rate == 0`, which is computed from DECIDED samples only,
  so no volume of caller errors can produce it.

### Search scoring — most words must match, and the rare ones decide

`catalog_store.search` demanded EVERY query token match (AND). Right for the 2–3 word refinement
("tiktok comments" must not return every tiktok endpoint), and fatal for how agents actually query:
the day the SearchMiss log shipped it recorded "company job postings hiring open jobs linkedin" → 0
results while three endpoints matched 6 of the 7 words. The only misses were "linkedin" on rows
shelved under `companies` (the agent names where the data lives, the catalog names what it is), and
"open" on the row shelved under `linkedin`. Since 2026-08-20 a query may miss one token in every
three (1–2 words: all still required), and each matched token scores its field weight times its BM25
idf — "by" matches 558 endpoints and is worth ~nothing, "postings" matches 4 and decides the order.
That asymmetry is also what keeps the miss allowance safe: dropping a rare word costs more score
than dropping filler, so full-match fluff cannot outrank a near-match on substance. Rows matching
the same tokens in the same fields still sum identical floats, so the tie band below keeps working.
Query-side layers close what scoring alone cannot. Function words ("on", "this", "what") and
single-letter tokens ("K&L" tokenizes to k + l, df 2,000+) are dropped before the miss allowance is
computed — they select nothing, but each one raised the number of real words a row had to match.
Tokens matching over `SOFT_DF_SHARE` (25%) of the catalog ("data" 33%, "api" 50%, "get" 40%) are
SOFT: they still add score where they match, but a row is never punished for missing them — a
statistical stopword list no hand list would keep up with. And `aliases.yaml` bridges vocabulary:
substring containment only works in one direction, so "cryptocurrency" never finds the catalog's
"crypto" without the map. A token matches under its own spelling or any curated alias, same field
weight. NOUNS ONLY: aliasing a verb to a commoner verb poisons the key (`lookup: [search, find]`
inflated lookup's match set 27 → 689 endpoints and destroyed its ranking power). The file is
query-side only — it rewrites no provider text, survives every re-ingest, and the validator
(`check_aliases`) rejects entries that could not survive the tokenizer and warns on aliases whose
target occurs nowhere in the catalog. The tokenizer also retains contiguous CJK text, so Chinese
task-phrase aliases are real searchable keys rather than discarded punctuation spans. Alias keys
remain one token, while targets may be lowercase hyphenated phrases because matching tests the
target string directly against catalog text. This makes `t2v` → `text-to-video` selective. The
original AIGC aliases mapped model names and Chinese task phrases to `video`/`image`: live
`treg catalog search` expanded Hailuo/Seedance/t2v to 521 endpoints and Flux to 172, including
YouTube and unrelated image utilities. Model-family aliases were removed once real endpoint text
contained those names; compact and Chinese task terms now target only `text-to-video` and/or
`image-to-video`. A post-change CLI run returned 10 Hailuo, 11 Seedance, and 14 Flux matches; the
task aliases returned 24 for t2v, 39 for i2v, 21 for the Chinese text-to-video query, and 35 for the
Chinese image-to-video query, with generation models at the top instead of unrelated utilities.
The SearchMiss log is its feed: a zero-result query whose
words name an existing endpoint in different vocabulary is one row here.

A query token that IS a platform slug ("tiktok", "linkedin") is the caller's hard filter, but idf
prices it low — half the catalog serves the big platforms — so rows matching a rarer facet word
("followers") outranked rows matching the asked-for platform. Platform-slug tokens therefore score
DOUBLE where they match; rows matching the same tokens in the same fields still sum identical
floats, so the tie band survives.

A zero-result answer surfaces its `near_misses` — the rows that just missed the admission gate,
with the exact words each one matched and missed ("apollo.companies.jobs matches job, hiring,
signal; misses law, firm"). The matcher had already computed this; discarding it and answering
with prose was the least useful thing the data allowed. Served structured over MCP (`near`), in
the HTTP route's `near` + a hint line, and as "almost:" lines in the CLI — the caller is usually
an LLM, and told exactly what to drop it re-queries correctly on the next call.

`scripts/search_bench.py` is the labeled replay (30 agent-shaped queries): sentence-style hit@8 went
14% → 100% (hit@1 64%, MRR .766) with the 8 short-query regression rows byte-identical. The residue
past this is semantic matching — an embedding model — which the bench so far says is not needed.

### The evidence decides the ORDER, not just the detail page

Token scoring ties by the dozen — all 24 `"ad library"` matches score alike — so "which 8 do I show?"
was answered by file order. That returned seven near-duplicate tikhub rows (one of them the
uncallable one above) and cut off `scrapecreators.x.v1-tiktok-ad-library-search`, cheaper and 17 for
17 measured. `catalog_store.rerank()` now settles equal scores over the band `rank_band()` returns, on
buckets rather than a weighted formula (an ok_rate and a price are different units, and a blended
score is one nobody can predict or argue with):

**relevance → measured (good · unknown · poor · never-worked) → core before extended → price**

where the measured bucket comes from `ok_rate` alone — `>= 0.9` good, `None` unknown, `0` never
worked, else poor — so a demotion always rests on calls the provider actually decided.

**The band takes the tie group whole.** A cut made *inside* a group of equally relevant rows is the
arbitrary cut, and reranking a slice that already dropped the best-measured row cannot put it back —
so `rank_band()` keeps taking while the score stays equal to the last row kept. That group is 17 rows
for "ad library" and 24 for "email", but 523 for the bare word "tiktok", and this is an OPEN route:
taking every group whole would put a 523-id `IN` clause behind every search. So it is bounded at
`RERANK_BAND` (250) and **says when it truncated** — `ranking_note` over MCP, a hint on the HTTP
route — because a bounded cut that announces itself is the thing this fix set out to build, and a
silent one is what it set out to remove.

Two orderings there are deliberate. Evidence outranks curation, because a core row that has never
answered is not the better suggestion. Curation outranks **price**, because `core` is the hand-picked
route and `extended` the bulk-ingested long tail — letting a tenth of a cent promote the tail made
`"tiktok comments"` lead with douyin danmaku. Unmeasured sits above measured-poor: a new endpoint is
an unknown, not a suspect.

Team policy sits on top: `CapabilityPin` (see [data-model](data-model.md)) lets an org fix a
capability to one provider, enforced in `_resolve_marketplace_call` before anything is reserved.

Its boundary, verified rather than assumed: a pin gates the **catalog id**, which is the only route
to treg's own key — so it cannot be side-stepped to spend our money (a URL-passthrough call resolves
against the org's OWN tools and 404s without one). A team holding its own key for another provider
can still call that provider by URL; that is their credential and their bill, and `DenyRule` —
host-scoped, applied to every shape of call — is the tool for blocking it.

### Routed groups in discovery — a search page is a list of JOBS (2026-08-28)

Three rules, all in `group_routed` / `search`, shared by `/catalog/search`, MCP `catalog_search`
and the CLI so the three surfaces cannot disagree:

- **A matched child brings its routed parent.** `find leads` matched `leadsforge.*` on the
  provider's NAME; the row an agent should see first for that job — `treg.people.search`, where
  treg chooses among every provider — contained no word of the query. `search` now adds
  `treg.<capability>` at the best child's score whenever a child matched. The token-filter tests
  exempt these pulled-in rows: their own text need not contain the query.
- **Vocabulary before ranking.** The same query first ranked `people.email.find` above
  `people.search` because `find` is a token of the former's capability NAME (weight 3) and only of
  the latter's summary (weight 2). The fix was to say in `capabilities.yaml` what the job is —
  `people.search` is "lead lists and prospects (sales leads)" — not to bend the scorer; `aliases.yaml`
  then only needs `lead → leads`, `prospect → leads, prospects`.
- **A group shows its best `MAX_ROUTED_CHILDREN` (5) children.** One capability's 24 providers had
  eaten the whole 25-row page. The parent is stamped `children_hidden`; the CLI prints
  `+ N more providers — treg catalog get <parent>`, MCP says so in `routed`. To keep the page full
  after collapsing, search ranks a band of 4× the page (≤ 100) and cuts to `limit` AFTER grouping.

## Routing — first-party routed endpoints (`treg.<capability>`)

The one place treg **models** an upstream API, and the explicit opt-in where the caller asks treg
to choose (`docs/CAPABILITY-ROUTING-PLAN.md`). Everything else in the catalog stays verbatim relay.

- **Contracts** — `contracts.yaml`: per capability, one-of *identity* variants (structural keys,
  never provider names — `{full_name, domain}`, `{first_name, last_name, domain}`,
  `{linkedin_url}`), `derive` rules so the two name shapes match the same adapters, a small
  *output* core (`email` required; `confidence`, names, `verified` optional) and `miss` in
  canonical terms. `raw` — the winning provider's body — is always returned and never documented
  as stable. `advice_unverified` (email and phone finds, and `people.search`) is one sentence the
  router attaches as `_treg.advice` to a hit whose `verified` is not true — a found contact is not
  a confirmed one (Hunter's `accept_all`, LeadMagic's personal finder, every phone provider), and a
  team that sent to such hits unverified bounced on most of them (2026-09-06). A search contract
  has no `verified` output, so its advice attaches to every hit: rows are directory listings, and
  the same team's 79-address bounce list (2026-09-08) was 73 unverified Hunter domain-search rows
  and agent-guessed `info@` addresses that one verify call each would have caught. The
  `hunter.companies.emails` catalog summary carries the same warning for direct `/call/` users,
  whose body is relayed verbatim. A suggestion only: treg never chains the verify call, which
  would double every hit's price and change what the find bills.
- **Adapters** — `adapters.yaml`, one per endpoint: `accepts` (identity variants), `in` (contract
  field → `queryParams.x` / `body.x`), `const` (fixed provider params), `out` (core field →
  expression over the body), `miss`. The expression language (`domain/catalog/routing/paths.py`)
  is deliberately tiny: dotted paths with `[i]` (root `[0]`, `.` = the whole body), `coalesce`,
  `/ N`, `==`/`!=` against literals, and named transforms (`split_first`, `split_last`, `join`,
  `has_type`, `len`, `list`, `obj`, `fmt`, `csv`, `lower`/`upper`, `at_least`, `null_if`, `choose`, `linkedin_handle`/
  `linkedin_url`, `email_domain`, `host`, `dfs_location`, `seranking_source`, `tca_filter`).
  `values` reads rows from object-keyed or list responses; `get` applies dotted/indexed lookup
  to another expression result (for example, the first company in a domain-keyed response).
  These are generic helpers, not provider-specific rewrites.
  `in_expr` builds provider params from expressions (URL-array bodies, DSL objects); `test_identity`
  states the fixture's identity when `in` builds a value rather than copying one; `filters` carry
  defaults and are always sent. `null_if` removes explicitly declared empty markers while retaining
  other values; `choose` selects between two expression values. Optional adapter `cost_units`
  expresses an upper bound in catalog-priced units (for example, fixed-page billing). Both the
  public quote and call-time plan use it through `routing/plan.py::cost_at`; invalid unit values
  remain unpriced. It does not replace the child's normal reserve/settle rules.
- **Verified at load, or absent** — `routing/contracts.py::verify`: `in` must reproduce the
  endpoint's own `test_request` and `out` must fill every required core field from its
  `example_response` (an example that is itself a miss passes with the hit half unverified).
  A failing adapter is not a candidate; the endpoint is still callable via `/call/` exactly as
  before. `tests/test_routing.py` pins that every shipped adapter passes.
- **The generated row** — `routing/synthetic.py`: every capability with ≥ 2 verified children gets
  `treg.<capability>` (`provider: treg`, `kind: routed`, `POST /<capability>`, `input` = the
  contract, `cost` = the children's range, `routed_children`). Never hand-written; not in any
  provider file.
  `catalog_get` on it returns the contract and the ranked **plan** (the quote) —
  nothing is reserved.
- **Ranking** — `routing/plan.py`: own keys (tier 2) first at cost 0; then
  `expected_cost_per_hit = cost_at(request) × P(billed) / P(hit)` where `cost_at` prices *this*
  request at its requested size (per-result × limit, credit-with-minimum rounded up) and `P(hit)`
  is the measured hit rate when ≥ 20 decided samples exist, else `ok_rate`, else 1.0 (flagged
  `unmeasured`). `build_plan` reads that evidence through bootstrap's shared process cache; cold or
  unavailable observations degrade to unmeasured ranking while the cache refreshes off the request
  path. `X-Treg-Route-Prefer` / `-Exclude` override; exhausted providers (capacity view)
  and providers with no key on the deployment are dropped and named in `dropped` (`needs {…}`
  says which identity variant a dropped child wanted).
- **Execution** — `application/call/route.py`, entered from `service._execute_call` when the
  resolved catalog row is `kind: routed`. Each attempt is a **full child `execute_call`** on a
  `CallContext` whose `call_ref` is `{parent}:r{n}` — its hold id, ladder (tiers 1/2/4/overflow),
  reserve, relay, settle, audit row and cancellation compensation are the ordinary ones. Vendor
  4xx (not 402/408/429) = usually the caller's fault, but scrapers answer 400 for their own outages
  (tikhub, live 2026-08-28), so the waterfall goes on ONLY to candidates that bill nothing for a
  rejected request — per_success, free, the org's own key, or per_call ≤ 1¢ (`CHEAP_RETRY_MICRO`;
  since 2026-09-07 a per_call rejection settles only at a charge the vendor itself reports, so this
  is a bound on the reported-charge risk, not on the estimate — see money.md)
  — never the same provider again, within the error bound; if every one rejects it, the caller
  gets `route_caller_fault` naming each attempt. A 4xx the endpoint's YAML declares as its
  "no result" status (`miss: {status: 404}` or `miss: {status: 400, when: …}`, see "`miss`
  semantics ride on the endpoint") is a MISS instead, not a fault. An adapter method
  (`to_upstream`, `from_upstream`, `is_miss`) that throws is recorded as an error attempt and the
  waterfall continues; the identity's `linkedin_url` is normalised once at planning time
  (`canonical_identity`: scheme-less URL or bare handle → public URL) so no adapter forwards an
  invalid URL. Our 5xx/503/429 or a vendor 5xx/429/402 = error →
  next candidate, at most two extra, only for idempotent contracts. A treg-side
  `tool_access_denied`, `policy_denied`, or `capability_pinned` refusal is local to that child and
  follows the same error fallback. A platform child's vendor 401/403 also falls back because it
  indicates treg's provider credential, not the routed caller's request. Balance and spend-cap
  refusals remain terminal because another provider cannot change the org-wide decision. A 2xx
  response whose body lacks a REQUIRED core field is a MISS, not a hit (dataforseo's `result: null`
  under a 20000 envelope). A
  MISS tries the next candidate — the waterfall is ON by default (decided
  2026-08-28: the endpoint's job is to find the thing, and misses on the per-success children are
  free); `X-Treg-Route-Waterfall: 0` stops at the first miss. Every attempt is settled at its real
  price and `X-Treg-Route-Max-Cost` (default $1) bounds the sum before each reserve (a candidate
  that would breach it is `skipped`). Quota-row quotes scale with the requested row count, just
  like per-result quotes. Each child also receives the remaining ceiling after actual earlier
  charges; the shared reservation gate checks the resolved estimate including margin, even when
  the advisory quote was too low or the child uses overflow. A budget refusal skips that candidate
  without using the provider-error retry allowance; if every candidate is skipped, return 402
  `route_max_cost`. A retained weak answer keeps its own outcome when later candidates are skipped.
  When the waterfall ends with some candidates skipped due to max-cost, the response includes
  `_treg.capped: true` and `X-Treg-Route-Capped: true` — a partial miss is distinguishable from an
  exhaustive one, so callers can raise their budget if needed (feedback #131, 2026-09).
  Response: `{output, raw, _treg: {served_by, provider, tier,
  outcome, tried[], charged_micro, capped?}}`, `X-Treg-Served-By`, `X-Treg-Providers-Tried`,
  `X-Treg-Route-Outcome`, `X-Treg-Route-Capped?`, `X-Treg-Cost-Micro` = the sum, one `X-Treg-Call-Id`. The parent owns
  the idempotency label (a success, or a terminal failure after a paid child, replays without
  touching a provider) and writes one audit row
  (`credential_tier: routed`) beside the children's.
- **Hit rate** — `CallRecord.hit` (nullable, alembic `0009`, last column) is the adapter's verdict
  written at settle; `stats.observed` publishes `hit_rate`/`hit_samples` (floor 20) and, for
  per-success endpoints, reads historical rows too (a 2xx with `cost_observed_micro == 0` is a miss).
  The plan, `catalog_get` and the CLI's HIT column read it; a registered tool (tier 1) or stored key
  (tier 2) for a provider ranks first at cost 0.
- **R0 done (2026-08-28)**: the top-traffic untagged `.x.` endpoints carry capabilities now
  (`google.serp.maps/news/local/ai_mode`, `google.keywords.trends` — each dataforseo + serpapi —
  plus `companies.jobs.search`, `companies.domain.find`, `amazon.product.sellers/variants`,
  `tiktok.video.captions`); untagged platform traffic fell from 12% to 1.4%, and 202 capabilities
  with 2+ eligible providers cover 88% of calls.
- **Ranking, specificity (2026-08-29)**: among candidates of the same tier, one that USES more of the
  keys the caller actually sent outranks a cheaper one that uses fewer — `{company_domain, title}`
  goes to a title-aware search, not a free domain-only one that would answer the whole company.
  Only caller-supplied keys count (`rank(given=…)`), never keys reached through `derive`. Price
  decides among equals.
- **Ranking, dropped filters (2026-08-29)**: a candidate whose adapter cannot express a filter the
  caller SENT ranks below every candidate that can — `len(candidate.ignored)` sits in `rank()`'s key
  between specificity and price. It answers a LOOSER question, and a non-empty answer to the looser
  question still passes `adapter.miss`, so cheapness alone must never buy it. Found live: a
  `people.search` for `{q, title, location: "London, United Kingdom", country: GB}` went to the
  cheapest child, which mapped neither geo filter, and returned people in Bengaluru and San
  Francisco — reported as a hit, $0.0025, no signal to the caller. `ignored_filters()` is pure and
  computed at PLANNING time (`routing/plan.py`), so the ranking and the per-attempt report read the
  same set. The provider stays reachable: it still wins when nothing better is callable, and price
  still decides among candidates that ignore equally much.
  **Coverage caveat**: of 16 `people.search` children, only icypeas maps geo today, so the rule
  currently floats one provider. lusha, crustdata, companyenrich and leadmagic all filter on
  location upstream — their adapters just do not map it. Until they do, the rule is doing more work
  than it should have to.
- **A contract that cannot say what the brief says (2026-08-29)**: `people.search` exposed only
  `{q, company_domain, title, full_name}` + `{country, location, limit}`, while icypeas natively
  filters on `keyword`, `skills`, `pastJobTitle`, `school`, `languages` and
  `totalYearsOfExperience`. And `q` is IDENTITY, so when icypeas matched the `{title}` variant the
  free text was never sent — a routed search for "backend developers in London with microservices"
  reached the provider as `title + location`, with the requirement dropped. Every failing bench
  query had this shape ("football scouting analysts" → `title="Football Analyst"`, 15 rows, 0
  qualified). `keywords` is now a FILTER (filters always travel; identity does not) mapped to
  icypeas `query.keyword.include`, leadsforge's keyword field, and folded into exa's semantic query.
  Measured on the bench's 30 recruiting briefs: **55.4 → 69.2 overall** (nDCG@10 51.2 → 64.3,
  coverage 49.7 → 68.1), failing queries 8 → 2.
  A `titles` list filter was tried at the same time and **REVERTED**: paired over the same 30
  queries it cost −0.068 ± 0.022 (95% CI [−0.112, −0.024]). Broader title variants ("Software
  Engineer" for a backend brief) buy recall the metric does not want and lose precision.
- **min_results, and why it is bounded (2026-08-29)**: `X-Treg-Route-Min-Results: N` records a hit
  with fewer than N rows as `weak` and keeps going, returning the fullest answer seen. It is what
  the hand-written bench policy did (`if len(rows) < 3 -> semantic fallback`) and the routed path
  could not express. Unbounded it is ruinous on LOOKUP briefs, whose honest answer IS one person:
  nothing ever clears the bar, so every call pays the whole ladder — the bench's deterministic set
  went **$1.76 → $22.35 over 28 queries, 12.7x**, for answers that were already right. Bounded at
  `MAX_WEAK_FALLBACKS = 2`, mirroring the error fallback, the same set costs ~$0.39 — cheaper than
  the baseline — and recruiting keeps its gain (it never needed more than one fall-through).
  Pair it with `X-Treg-Route-Max-Cost` on any capability where thin answers are normal.
- **Routed parity with a hand-written policy (2026-08-29)**: after the two changes above, paired
  over the same 30 recruiting briefs against the 08-27 hand-written icypeas policy, the routed path
  is indistinguishable — nDCG@10 −2.40 (95% CI [−7.12, +2.33]), utility −0.80 ([−3.10, +1.51]),
  qualified/query −0.53 ([−1.84, +0.77]) — and ahead of the published Lessie 68.2, Exa 64.7 and
  Claude Code 50.5. The remaining differences are agent-side, not routing: the hand-written policy
  post-filtered rows on location and over-fetched (`size: 20`, trimmed to 15).
- **The answer says what it ignored (2026-08-29)**: `ignored_filters` was on `_treg.tried[]` only,
  which no caller reads. It is now also on `_treg` itself for the child that served and on an
  `X-Treg-Ignored-Filters` response header, so an agent can post-filter, or say why the rows are
  wrong, without walking the attempt list. **Opt-in refusal (2026-09-04)**: `X-Treg-Route-Strict-Filters: 1`
  drops every candidate that cannot express a sent filter at planning time (listed in `dropped`
  with `strict: true` and what the adapter takes instead) and answers `route_no_candidate` 422,
  unbilled, when none is left — a 503 stays reserved for capacity/key drops. Off by default: the
  ignored-but-billed call (`{full_name, country: GT}` → New York, voice-ai-outbound 2026-09-03) is
  the documented behaviour, and the fix for that case was to give the candidate the filter.
- **Lusha is the sixth phone rung (2026-09-04)**: `lusha.people.phone.find` — the phone-only view
  of search-and-enrich — accepts every phone.find identity and ranks last on price (6 credits a
  hit; a miss free, a matched-but-no-number profile the 1-credit search, all settled from
  `billing.creditsCharged`). Added for LatAm coverage after a Guatemala test found 7 in 44 across
  the other five. Apollo cannot join: its phone reveal is webhook-only, never inline.
- **people.\* sweep (2026-08-29)**: people.search 6 → 16 children (aviato dsl/simple, companyenrich
  scroll, crustdata, fiber-ai, leadsforge, leadmagic search + role-finder, findymail employees +
  domain — the last retagged from email.find, it returns a list), people.enrich 9 → 14 (aviato bulk,
  fiber-ai, tomba profile/combined, hunter combined-find), people.email.find 9 → 11 (fiber-ai turbo,
  leadmagic personal), identity.resolve 3 → 4 (findymail reverse-email); five examples captured live.
  Still out: apollo/coresignal people.search (no fixture; apollo's `person_titles[]` needs a
  bracket-safe target), crustdata/diffbot people.enrich (truncated examples), the `*.bulk` jobs
  (async), hunter multi-domain (masked rows).
- **Filters reach providers, or say they did not (2026-08-29)**: `country` (ISO code) becomes a name
  through `country_name` (`catalog/countries.json`, 249 rows generated from pycountry) for providers
  that filter on a location NAME (icypeas); `location` is a free-text pass-through ("London, United
  Kingdom", "Europe") for the same providers; a filter the caller sent that an adapter never mentions
  is listed on the attempt as `ignored_filters` — silently unapplied was the worst outcome (the bench
  had post-filtered in the agent because of it). Bench re-run, recruiting 30: same icypeas rows as the
  hand-written policy, one automatic fall-through, region briefs rescued by the pass-through.
- **Routed DISCOVERY is a runtime switch (2026-08-29)**: `TREG_ROUTED_DISCOVERY=off` (default `on`)
  stops search leading with `treg.<capability>` and stops a routed parent riding in when a child
  matches — the endpoints stay callable, priced and reachable by id, and `catalog get`/`POST /call/`
  are untouched. Off also HIDES routed rows from search results, not merely ungroups them: a routed
  row matches a keyword query on its own summary, so leaving it in would steer by the back door.
  One choke point (`group_routed`) serves both callers (`mcp.py`, `routers/catalog.py`); MCP also
  narrows its rank band back to `limit` when off, since the widening exists only so groups can
  collapse. Same dashboard-flip shape as `platform_providers` and `TREG_OVERFLOW_MODE` — no
  redeploy. It covers every surface that steers, not just search: the platform BROWSE view
  (`/catalog/platforms/{slug}`, which sorts the routed parent to the top of its capability group)
  drops routed rows too, and `/skill.md` and `/llms.txt` strip their routed section — a deployment
  that hides the row from search must not keep TEACHING agents to call it, or the docs and the
  catalog disagree and the agent believes the docs. The section is delimited in those two files by
  `<!--routed-->…<!--/routed-->`; the markers are stripped either way, and the unrelated overflow /
  `provider_capacity_unavailable` guidance in the same paragraphs is kept (it came from the capacity
  work, not from routing — which is also why a `git revert` of #242 would be the wrong instrument). It exists because "does the router answer well" and "should every agent be led to it by
  default" are separate questions: the bench answered the first (55.4 → 69.2 on recruiting, parity
  with a hand-written policy), and only traffic can answer the second.
- **creators.search: routed, then UNROUTED (2026-08-31)**: the contract was added because
  influencersclub filters on `location` / `keywords_in_bio` / `number_of_followers` and returns that
  data inline while `exa.creators.search` returns a URL and a title, so an agent picking blind chose
  exa and then verified follower counts by hand. Measured, the contract made the bench category
  WORSE: influencer 54.4 → 49.3, queries answered 29 → 27, and the profile-verification calls it was
  meant to remove went UP (131 → 155). The cost fell 42% ($10.96 → $6.34), which is the only part
  that held. Best explanation, same shape as the reverted `titles` filter: sending the follower band
  and location as HARD filters over-constrains, and a metric that pads to K=15 pays for volume — five
  exact matches score below fifteen loose ones. Reverted so production matches the submitted bench
  data. If it returns, the filters should be opt-in rather than always-sent, and measured first.
- **What is not routed on purpose**: `*.bulk` endpoints (a routed call is one subject, one answer),
  and providers whose rows are teasers — hunter multi-domain (masked, no names, ignores limit),
  apollo people.search (free, but last names obfuscated: a search→reveal CHAIN, which mode C of the
  bench showed rescues hard B2B briefs and which the router does not do yet). `catalog get` lists
  them under ALSO with the rest of the same-job endpoints that have no adapter; the search page's
  "+N more" points there. The routed row's example body and `/access` dry-run use the identity
  variant MOST children accept, and the dry-run tries every variant before saying "unservable".
  That dry-run is ONE identity shape, so its drops are mostly "this adapter takes another identity",
  not "your team cannot reach this provider" — `/access` used to label them "not available here",
  which read as a missing key and sent a reader hunting for one (2026-08-29: aviato, callable on
  treg's platform key and serving live calls, was listed as unavailable). It now names the shape and
  gives each drop its own `why`.
- **Coverage (2026-08-28)**: 74 routed capabilities = 80.9% of 30-day platform calls (88% was the
  routable ceiling). The per-capability ledger — what shipped with which children, what is 🚫 and
  why (one usable vendor, async task-post engines, identity-less feeds), and the 49 zero-traffic
  rows still open — is `docs/CAPABILITY-EXPANSION.md` (git-excluded, Jason's working doc).
- **Not built** (plan R4): "prefer routed" in the agent files after a shadow week; a proper
  `kind: filters` / `Location` layer for the DSL/SQL providers (aviato dsl and pdl sql ride `obj`/
  `fmt` today; crustdata/diffbot/coresignal/apollo do not); own-key-dry → treg-key fallback.

- **Name-only Leadsforge requests (2026-09-07)**: email and phone adapters accept the derived
  `{first_name, last_name, domain}` variant or a LinkedIn URL. Removed the redundant
  `{full_name, domain}` fallback: a one-word name cannot derive `last_name`, so that fallback
  selected an identity variant whose name was not mapped and sent only `companyDomain`.
  Complete full names still derive both parts and work normally. Regression tests exercise the
  actual matched-variant request, including rejection of mononyms and preservation of LinkedIn.
  Arena also validates full names before quoting, preventing Hunter's `invalid_full_name` error.
  Leadsforge and Fiber contact lookup success flags no longer populate `verified`: neither
  flag is an explicit mailbox deliverability verdict. The field remains absent when unknown.

## Archive comparison declarations

The effective `cache:` block accepts `ignore_paths: [...]` alongside `max_age_s`. The default is
an empty list. Provider-header inheritance and whole-block endpoint override follow the existing
cache policy rules. `store._validate_cache` rejects an invalid list or path during catalog loading,
including provider-header declarations even when endpoint blocks override them.

Paths are case-sensitive dot-separated property names matching `[A-Za-z0-9_][A-Za-z0-9_-]*`, with
`[*]` suffixes for arbitrary array elements: `request_id`, `data.items[*].updated_at`, or
`matrix[*][*].request-id`. Leading digits are allowed, e.g. `2fa_enabled`. A root array can use `[*].request_id`. Empty lists are valid; null,
non-lists, non-string members, empty paths, numeric indices, plain `*`, `$` prefixes, spaces,
empty segments, escaping and recursive wildcards are rejected. Keys containing literal dots or
brackets are deliberately not addressable in this first grammar. Missing paths are harmless.

`archive._normalized_hash` removes only these paths from a parsed copy for TTL equality. It never
changes archived or served data, raw hashes, deduplication or hit/miss classification. Without a
nonempty list, exact byte comparison remains authoritative. No shipped endpoint has an ignore
list; use the bounded `archive_change_observed` reports and HogQL in [archive](archive.md) as
human review input, then add a justified declaration in a separate PR.


## Security

PII IS THE HARD RULE. This repo is public, and every captured example ships in it. Three checks
before any example is committed, all learned the hard way:

1. **No named private individuals.** Contact-lookup routes (LinkedIn contact info, people-enrichment
   by email) return a real person's name, personal email and phone. Such an endpoint stays in the
   catalog — the route is real and useful — but it is marked `untestable:` with the reason and
   carries NO `test_request` (so a re-verify cannot silently re-capture it). No captured person
   response is stored. A routing adapter may use a hand-sanitized structural fixture only when its
   contact values use reserved fake domains/numbers, it cannot be refreshed by the verifier, and
   separate live evidence establishes the mapped response fields.
2. **No third-party PII riding along.** Emails and phones turn up inside unrelated payloads — a
   YouTube description, a review body. Sweep every captured example for address-shaped strings and
   mask anything that isn't a business contact.
3. **No first-party identity.** Own-account verification (`mine=true`, your own site in Search
   Console) captures YOUR channel, sitemap and metrics. Point test requests at neutral public
   targets instead, and scrub what you already captured.


Credentials are NEVER written into catalog files, examples, scripts, or docs — the verifier reads
`TREG_CATALOG_CRED` from the environment only. Captured examples are truncated (arrays → 2 items,
long strings clipped, ~10 KB cap) by the verifier, then human-reviewed for PII before commit.

## Pilot providers (first wave)

| service | platform focus | auth (from oauth_providers.py) | overlap group |
|---|---|---|---|
| dataforseo | google, web | Basic (login:password base64) | SEO: web.backlinks.*, web.url.metrics |
| exa (2026-08-27) | web, people, companies | `x-api-key` header; dollar-priced, settles from `costDollars.total` | Search: web.search*, web.contents.get, web.similar, web.answer; Enrichment: people.search, companies.search |
| cloro (2026-09-07) | ai-search, google | `Authorization: Bearer sk_live_…`; credit-priced ($0.0004, Hobby metered rate), settles from `X-Credits-Charged` | AEO: ai-search.chatgpt.scrape, ai-search.copilot.scrape (new), ai-search.perplexity.answer, ai-search.gemini.scrape; SERP: google.serp.organic, google.serp.news, google.serp.ai_mode (overlaps dataforseo/serpapi extended) |
| moz | web | Basic (AccessID:SecretKey base64), POST JSON API | SEO: web.backlinks.*, web.url.metrics |
| tikhub | tiktok (+instagram, youtube, x) | Bearer key | Social: tiktok.* |
| justoneapi | tiktok (+instagram, xiaohongshu, weibo) | `?token=` query param | Social: tiktok.* |

The SEO pair and the social pair each implement the same capabilities on purpose — they are the
first real test that the capability taxonomy supports cross-provider comparison.

## Kitt AI (`trykitt`)

`trykitt.yaml` lists only realtime email find/verify. Account checks are not public
catalog tools; `/credit` remains the internal key probe and balance collector.
`adapters.yaml` adds both email tools to their existing routed parents. Find maps
`full_name`/derived first+last name and domain to `fullName` and `domain`; optional LinkedIn
URLs are passed as `linkedinStandardProfileURL`. Both adapters set `realtime: true`.
A find's `email: no-results-found` (or absent/empty email) is a miss; successful finds
preserve `verified` from `validity == valid`. Verification verdicts, including invalid
and unknown/catchall, are answers rather than waterfall misses.

The scalar prices reserve the published base rate. `platform_request: {body.realtime: true}`
binds platform calls to that value through `_enforce_platform_request`, before reserve.
This shared rule accepts declared body fields with a matching singleton enum; the catalog
validator rejects other forms. Existing fixed pricing selectors still use the same guard.
The loader preserves the rule; BYOK returns before the guard and remains a faithful relay.
`cost.reported_charge: {path: credits.jobCredits, unit: usd}` supplies the actual charge
through the common response-field reader. The validator permits this only with paid scalar
prices and no competing `settle` rule. Missing evidence uses the normal miss/base policy.
Polling `/job?id=` returned 500 in repeated live tests and is excluded, along with
asynchronous/webhook submission. The surface map is in the catalog header.

Paid evidence on 2026-09-09: find hit $0.005, miss $0, valid verification $0.0015,
invalid verification $0.0015; account balance eventually moved $10 → $9.992.
`credits.jobCredits` is USD; `remainingCredits` lags. Unknown/catchall pricing is
documented, not live-verified. Public API-doc example contact is used in fixtures; job IDs
are redacted. Billing and BYOK regressions live in `tests/test_marketplace_call.py`;
routing lives in `tests/test_routing.py`. Capacity tests use the shared collector, policy,
and signature test files. The reusable setup is in `tests/conftest.py`.


## ContactOut

`contactout.yaml` adds the core LinkedIn/contact surface with explicit work/personal selectors,
on-hit Starter rates supplied by the account owner, free verification, and deferred batches.
Contact reveals and availability checks live on the People shelf; profile identity tools remain
on LinkedIn. People entries stay `untestable:` without test requests under the PII rule. Work-email
and phone lookup use reserved-value structural fixtures and verified adapters for the existing
People routes. Personal email remains direct-only because the shared email contract is work-only;
people search/profile adapters remain omitted. Company search/enrichment and email verification
retain verified adapters. Profile-only LinkedIn enrichment costs $0.02 when found.

`Catalog.cost_view` reads optional provider-neutral `cost.display` metadata. `unit` names the
shown unit; `grouped` displays the price for `cost.per` units; `round_up` labels a started block;
`variable` adds a plus sign for selected additions; and `maximum` labels a validated reserve
ceiling as **up to**, rather than presenting it as the normal settled price. A maximum display may
wrap a price table because its fallback is already the validated global upper bound. It returns
computed display USD/unit/prefix/suffix
fields without changing `usd` or settlement. The CLI and web formatters consume those fields;
`Catalog.advertised_usd` prefers `display_usd` so MCP `usd_per_call` quotes the chargeable event.
The validator checks flags, requires grouped prices to declare a positive integer `per`, and
refuses a table display unless it explicitly describes the maximum.
Hunter Domain Search is the credit-block case (`1` credit / `10` emails → `$0.0245/started 10
emails`). Sumble keeps its billing rules in the existing provider-module pattern, separate from
display rules.


### Similar-company routing

The `companies.similar` contract accepts a seed `domain` and returns a nonempty `companies` list.
Tomba and CompanyEnrich adapters are checked against their existing saved catalog fixtures.
Tomba maps the domain to its query parameter and returns `data`; CompanyEnrich maps it to a
one-item `body.domains` list, fixes page to one and pageSize to ten, and returns `items`.
CompanyEnrich pricing therefore uses the explicit ten-row request. The contract has no common
limit filter because Tomba's endpoint does not accept one. The ordinary verified-adapter gate
controls synthesized routing availability; Arena additionally bounds its displayed rows.

### Phone validation adapter

The `people.phone.verify` contract maps Tomba's existing GET `/v1/phone-validator` endpoint
through `queryParams.phone`. The adapter reads `data.valid`, `data.e164_format`, country code,
line type and carrier. A boolean false is a returned invalid verdict; a missing verdict is a
miss. This validates numbering-plan/format details, not line activity or subscriber ownership.
The single verified adapter is usable by Arena; the two-provider public routing gate stays intact.


## HarvestAPI integration

`harvestapi.yaml` adds API-key-only LinkedIn reads with opt-in `strict_query` contracts and three profile variants.


## Dropleads integration

`dropleads.yaml` adds ten synchronous people and company tools. The balance check and export-cost
route stay outside the public catalog. Seven verified adapters add email finding, phone finding,
email verification, people search and enrichment, and company search and enrichment to the existing
routed tools and Enrich Arena. Count tools stay direct; two ten-person bulk tools are omitted. The provider uses
the existing `CatalogTarget` allow-list for its second API host; catalog data cannot send a
credential to another host.


## Prospeo integration

`prospeo.yaml` adds seven people and company tools on both own and platform keys. Six verified
adapters add email finding, phone finding, person/company enrichment and person/company search to
the routed tools and Enrich Arena; search suggestions stay direct-only and bulk enrichment is omitted. Search
pages are fixed at 25 upstream, so adapters cannot forward the contract `limit`; they expose
Prospeo's `pagination.total_count` while relaying the native result page. The account-information
route remains internal for key verification and capacity.


### Verified additional routing categories

An adapter can opt into `additional_capabilities` while its endpoint retains its primary
catalog capability and direct-call ID. `load_routing` verifies each additional contract against
the same request/response fixture and admits it through `verified_capabilities` only when the
primary adapter passes, the extra contract exists, the filter definitions match, and the extra
fixture check passes. Invalid extra contracts do not disable the primary adapter.
`Catalog.for_capability` includes these verified memberships for both routed tools and Arena.
No additional provider request, catalog row or billing path is introduced. The Harvest full
profile adapter also serves `people.enrich`; its company adapter also serves `companies.enrich`.
The basic profile adapter retains only its LinkedIn category.
