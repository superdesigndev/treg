---
title: Provider capacity — knowing what treg's own vendor accounts have left
status: shipped
sources:
  - src/treg/domain/capacity/__init__.py
  - src/treg/domain/capacity/collectors.py
  - src/treg/domain/capacity/policy.py
  - src/treg/domain/capacity/sweep.py
  - src/treg/domain/capacity/view.py
  - src/treg/domain/capacity/routes.py
  - src/treg/domain/capacity/signatures.py
  - src/treg/domain/capacity/verify.py
  - src/treg/domain/capacity/marks.py
  - tests/test_capacity_protect.py
  - src/treg/infra/upstream/limiter.py
  - src/treg/domain/capacity/overflow_spend.py
  - src/treg/domain/capacity/routes_view.py
  - src/treg/application/call/overflow.py
  - src/treg/alembic/versions/0007_overflow_spend.py
  - tests/test_capacity_overflow.py
  - tests/test_capacity_overflow_spend.py
  - src/treg/alembic/versions/0008_org_platform_overflow_disabled.py
  - tests/test_capacity_smoothing.py
  - src/treg/domain/capacity/overflow_seed.json
  - src/treg/infra/upstream/aggregators/__init__.py
  - src/treg/infra/upstream/aggregators/orthogonal.py
  - src/treg/infra/upstream/aggregators/monid.py
  - src/treg/infra/upstream/aggregators/catalogs.py
  - src/treg/alembic/versions/0006_overflow_route.py
  - tests/test_capacity_overflow_routes.py
  - tests/test_influencersclub_overflow.py
  - src/treg/worker.py
  - scripts/provider_balances.py
  - src/treg/alembic/versions/0005_capacity_policy_snapshot.py
  - tests/test_capacity_know.py
  - tests/test_capacity_collectors.py
  - tests/test_financialdatasets.py
related:
  - architecture/data-model.md
  - architecture/money.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

# Provider capacity

LimaData exposes no free standalone balance API, so capacity reports its credit balance as
dashboard-only. The assigned account's existing automatic top-up is enabled, and the default policy
is `credits / auto_recharge / manual`. Shared-key smoothing uses the documented default one request
per second; BYOK bypasses it. See [LimaData](../architecture/limadata.md).

BounceBan's collector calls the free `GET /v1/account` route with the raw `Authorization` key and
reads `available_credits`. Zero and finite nonnegative numbers are exact balances; missing, Boolean,
string, negative, and non-finite values are unknown. Its policy is `credits / manual / api`, with a
conservative shared-key rate of 25 requests per second. No reset, renewal, or auto-top-up behavior is
inferred, and no overflow route is claimed. See [BounceBan](../architecture/bounceban.md).

Datagma's collector calls the free internal `GET /api/ingress/v1/mine` route with the query-bound
API ID and retains only `currentCredit`. Account identity and plan fields are discarded and never
become catalog output. Finite nonnegative numbers and decimal strings are accepted; malformed,
negative, and nonfinite values are rejected. HTTP failures are sanitized so the URL cannot expose
the query credential. Its policy is `credits / manual / api`, with the documented 10 requests per
second shared-key limit. No empty-account signature was forced and no overflow route is claimed.
See [Datagma](../architecture/datagma.md).

ZeroBounce's collector calls the free `GET /v2/getcredits` route with the query-bound key and reads
`Credits`. Nonnegative integers and decimal strings are exact balances. Boolean, missing,
malformed, and negative values are unknown; this includes the provider's invalid-key `-1` sentinel.
Its policy is `credits / auto_recharge / api` for vendor-managed Auto-Pay, but treg does not read or
change that setting. Shared-key pacing starts at 25 requests per second. No empty-account
response was forced and no overflow route is claimed. See
[ZeroBounce](../architecture/zerobounce.md).

`collectors._moltsets` reads the free account envelope and reports the tighter rolling enrichment
record remainder, with both enrichment/search request and record pools in its note. Missing
enrichment windows produce unknown capacity rather than substituting the separate token/phone
balance. Its `rolling_quota` type means the quota is measured over rolling windows; the explicit
10/second smoothing policy is a routing pace, not a reinterpretation of the 5,000-request/5h
capacity allowance. See [MoltSets](../architecture/moltsets.md).

`collectors._sumble` reads `credits_remaining` from a free technology-search miss. Its monthly allowance and optional vendor top-ups remain separate from per-call pricing; no renewal date or auto-funding status is assumed. See [Sumble](../architecture/sumble.md).

`collectors._getleadsio` reads numeric nonnegative `credits_remaining` from the free fair-use route.
It represents the promotional database-credit allocation, not the separate Live Leads wallet.
Default smoothing is the documented 100 requests per minute. An exact observed zero publishes the
normal exhausted state: platform calls then receive the shared typed 503 with an own-key instruction
before reserve, while BYOK remains available. No empty-account response was forced, so the upstream
exhaustion signature remains unrecorded and no overflow route is claimed. See [GetLeads.io](../architecture/getleadsio.md).

Financial Datasets uses the existing capacity path with `_KNOWN` policy
`credits / auto_recharge / manual`. The official API publishes no free balance or usage endpoint,
so `NO_BALANCE_API` reports its upstream remainder as `no API`; the dashboard is not scraped and a
paid data request is not scheduled as a meter. Its typed platform-key setting makes it appear in
`all_platform_providers()` and therefore in `scripts/provider_balances.py` without provider-specific
script logic. treg continues to report its own ledger-estimated spend, billed amount, and call count.
The shared bare HTTP 402 balance-exhaustion signature and strike/lock lifecycle apply to platform
calls; own-key calls remain outside capacity decisions.

Its `platform_auth: anonymous` discovery calls are authenticated treg team calls, not a public
internet proxy. The normal tool ACL, deny rules, and a configured member `daily_call_cap` apply, but
the default member cap is unlimited and the shared-key provider spacer applies only to the
`platform` tier. An anonymous upstream 429 or edge block is therefore relayed without marking the
platform account. Heavy anonymous traffic can still harm the shared egress address and disrupt paid
calls to the same provider. The immediate kill switch is removal of `financialdatasets` from
`TREG_PLATFORM_PROVIDERS`, which stops anonymous and platform-key offers while leaving team BYOK
credentials first and usable. A default anonymous-provider throttle is not shipped in this change.

**Problem.** When a registry-owned vendor account runs dry, callers receive an upstream refusal they
cannot fix themselves. Capacity handling has three layers: **know** the runway, **fund** before it
dies, and **protect** the call when it dies anyway through refusal before reserve, same-endpoint
overflow, and a typed 503. Live account state and production observations belong in the private
[provider-capacity runbook](https://github.com/superdesigndev/treg-internal/blob/main/docs/production/provider-capacity.md).

Scope: treg-owned platform credentials only. Tiers 1/2 (a caller's own tool or key) are never
consulted or affected by anything here.

MillionVerifier's platform slot has an acknowledged exhaustion-signature gap in
`tests/test_capacity_overflow_routes.py::_UNRECORDED_SIGNATURE`: no verified exhaustion response is
available. Its balance collector reads `credits` from the free `/api/v3/credits`
endpoint using query auth `api`. Both the balance script and sweep use this collector; it does
not add `bulk_credits` to the balance. No overflow route is claimed. Verify the funded
account's empty-credit response before adding a signature or enabling overflow.

## QuickEnrich subscriptions

`collectors._quickenrich` reads `meta.remaining_credits` from a free Contact Finder miss;
there is no account/balance endpoint to list. Default policy is `monthly_quota` / `quota_reset`,
with auto-funding disabled. The API does not report the renewal timestamp, so no calendar reset
is guessed. Subsequent sweeps discover replenished credits. Do not model this as prepaid packs
or auto-top-up. Hunter also uses renewal quotas: monthly plans reset monthly, yearly plans
annually ([Hunter reset rules](https://help.hunter.io/en/articles/1911597-when-do-credits-reset)).

Free, Starter and Growth use the API-reported remaining allowance. No manual plan setting
can override that value. A reported zero means exhausted; missing, negative or non-numeric
balance data means unknown, not unlimited. The unlimited-plan API response has not been
verified. Inspect its actual status and balance fields before adding common unlimited-plan
support. Per-call billing remains separate: use `meta.credits_used` at the treg list rate.

Exhaustion behavior is acknowledged as unrecorded in the existing shared signature guard;
we did not exhaust the trial to manufacture evidence. No overflow route is claimed.


## Dropleads credits

`collectors._dropleads` uses the free internal balance route and reads
`credits.totalAvailable`. Zero is a valid exhausted balance. A missing, negative, Boolean or
non-numeric value is unknown. Subscription, PAYG and `usePayg` values stay in the observation note;
they do not replace the spendable total. The default policy is `credits / manual / api`.
`scripts/provider_balances.py` needs no provider branch because the typed platform-key setting and
the shared collector table discover Dropleads automatically. The free trial was not exhausted, so
the actual upstream exhaustion response is acknowledged as unrecorded and no overflow route is
enabled. See [Dropleads](../architecture/dropleads.md).

## Prospeo subscriptions

`collectors._prospeo` uses the free `GET /account-information` route and reads
`response.remaining_credits`. Zero is a valid exhausted allowance; a missing, negative, Boolean,
non-finite or non-numeric value is unknown. The observation note carries the current plan, used
credits and `next_quota_renewal_date`. Default policy is
`monthly_quota / quota_reset / api`; no auto-funding behavior is inferred.

The shared Starter key has two endpoint families: enrichment permits 5 requests/second while
search permits 1 request/second. `_RATE_LIMITS` therefore defaults Prospeo to the conservative
provider-wide 1/second rate until smoothing becomes endpoint-aware. This protects routed search
calls from avoidable 429s at the cost of intentionally slowing platform enrichment; BYOK does not
use the shared-key limiter.

`scripts/provider_balances.py` requires no Prospeo branch. The typed
`platform_key_prospeo` setting makes `all_platform_providers()` discover it, and `BALANCE_ROUTES`
selects the shared collector. The collector reads its key setting independently of the serving
allow-list so disabling a provider does not hide its last balance check. A focused live
reconciliation matched one paid one-credit call beside two free calls without exposing the key.
The Starter allowance was not exhausted, so its provider-specific exhaustion response remains in
the acknowledged-unrecorded set and no overflow route is claimed. See
[Prospeo](../architecture/prospeo.md).

## Wiza prepaid API credits

`collectors._wiza` calls the free `GET /api/meta/credits` route with the Bearer platform key and
reads `credits.api_credits`. Zero is a valid exhausted balance. A missing, Boolean, negative,
non-finite or non-numeric value fails the observation instead of becoming an allowance. The default
policy is `credits / manual / api`; Wiza vendor auto-top-up is not enabled.

The same route verifies pasted keys. The funded grant was not exhausted to manufacture an error,
so Wiza remains in the acknowledged-unrecorded exhaustion set and has no overflow route. Search and
autocomplete limits are not published. As a provisional policy, `_RATE_LIMITS` reuses the
documented company-enrichment ceiling of 30 calls per minute for all Wiza platform calls because
smoothing is not endpoint-aware. This spaces sequential platform calls by about two seconds, adding
about 38 seconds of waiting across 20 one-row search pages; BYOK is unaffected. The bounded,
process-local limiter reduces ordinary bursts but is not a strict quota gate: calls whose computed
wait exceeds `DEFAULT_MAX_WAIT_MS` proceed. Relax the ceiling after real 429 evidence, or when
smoothing becomes endpoint-aware. The public replacement rate and platform/BYOK boundary are
documented in [Wiza](../architecture/wiza.md).

## Pieces (`src/treg/domain/capacity/`)

- **`collectors.py`** — the providers' *free* balance/quota calls (`coroutine(client, key) →
  {value, unit, note}`), shared with `scripts/provider_balances.py`. Providers such as DataForSEO,
  TikHub, Brightdata, and Kitt AI report balances in USD; other meters include credits, rows, and searches. `NO_BALANCE_API`
  names the 9 providers that publish no free standalone meter so they read as "no API", never as a
  broken key. Scrubby reports `remaining_credits` only on verification responses; collection never
  spends a verification merely to obtain that value.
  Litescrape's `/api/keys/status` supplies both `remaining_calls` and its live cents-per-1,000 rate.
  `provider_balance()` never raises — a failure is a row. It reads the *setting*, not
  `platform_key_for`: the tier-4 allow-list is a serving kill switch, and a provider just switched
  off is exactly one whose last balance we still want.
- **`policy.py`** — `CapacityPolicy` defaults per account (`_KNOWN`: capacity type, funding mode,
  source, plus the verified quota/rate facts for lusha, hunter, leadsforge, leadmagic, crustdata,
  tikhub). The population is every `platform_key_*` slot **plus `overflow:orthogonal` /
  `overflow:monid`** (aggregators are prepaid accounts that run dry too). `ensure_policies` inserts
  missing rows only — a hand-edited row is never overwritten — and returns the providers still
  `unknown`, which a person must classify; code never guesses. `latest_state()` is the pure rule:
  no/failed/old (> 6 h) observation → `stale` (never refuses a call); `remaining ≤ 0` on an exact
  observation → `exhausted` until `resets_at`, or until the next sweep can prove otherwise.
- **`sweep.py`** — `run_sweep(db)`: import policies → collect all providers in parallel (DB idle
  while the network is in flight) → one `CapacitySnapshot` per provider → publish each
  `LatestState` to ratestore as `capacity:state:<provider>` (24 h TTL) → one commit. A note that
  looks like a credential is withheld before it is stored. It never touches the call path's locks
  (`capacity:lock:*`): the meter it reads may not be the allowance that ran out.
- **`marks.py`** - the call-path breaker, its own namespace `capacity:lock:<key>`; key = provider
  for a balance signature, endpoint id for a quota one. Strike, lock on the second strike within
  10 min and at least 15 s later with no 2xx between, admit one probe per process per minute, clear on the probe's 2xx
  (conditional on the lock id). A guessed hold lasts 1 h, a vendor-stated reset at most 6 h.
  See `architecture/proxy-model.md`.
- **`view.py`** - `LatestStateView`: the in-process copy of both namespaces, reloaded from
  ratestore on a 60 s TTL by an explicit `await load()`; `is_exhausted(provider, endpoint_id)`
  and friends are sync and I/O-free so `resolve` can read them without breaking its rule. A
  writer calls `invalidate()`; other replicas see the change within one TTL.

## Where it runs — `treg-worker`

`treg-worker capacity sweep [--only a,b] [--json]` (`src/treg/worker.py`, console script in the
`[server]` extra). It is deliberately **not** a `treg` subcommand: the light CLI may not import the
DB stack (import-linter contract), and the sweep needs the platform keys in the env plus outbound
third-party calls, which are worker-profile work and never dataplane lifespan work. Operators schedule
it in a worker environment containing the required provider keys. Startup calls read-only
`verify_db()` and refuses a missing or stale schema. `scripts/provider_balances.py` remains the
by-hand reconciliation view over the same collectors. The hosted schedule and service wiring are
documented privately.

## Data

`CapacityPolicy` (one row per account; `capacity_type` is `cash`, `credits`, `requests`,
`monthly_quota`, `rolling_quota`, `subscription`, or `unknown`; `source`, `funding_mode`, auto-funding
fields, runway thresholds, `usd_per_unit_micro` NULL = never invent a dollar figure, `rate_limit`
+ `quota` JSON, `enabled` ⇔ a key exists) and `CapacitySnapshot` (append-only observations:
`remaining`, `total`, `unit`, `resets_at`, `source`, `confidence`, `note`, `error`). Written by the
worker only. Alembic revision `0005` is authoritative; production schema changes are Alembic-only.
Numbers only - no key or payment detail.

## Boundaries

`treg.domain.capacity` may not import `treg.api`, `treg.routers`, `treg.application`,
`treg.bootstrap`, `treg.audit`, FastAPI or Starlette (contract "Capacity domain does not depend on
outer layers"). Money is never touched: capacity marks are ratestore rows, never balances.

## Overflow routes (step B′) — derived, never hand-written

**Overflow** = the *same* vendor endpoint served through a treg-owned **aggregator** account
(Orthogonal first, Monid second) when our direct account is out. It is a credential rung
(`platform-overflow`), not a vendor: not in the catalog, not searchable, no BYO key. The caller
pays the aggregator's real price, 0% markup, disclosed in-band when it ships (step E).

- **`OverflowRoute`** (`overflowroute`, Alembic `0006`): one row per `(endpoint_id, aggregator)` -
  the aggregator's slug/path spelling, its list price (micro-USD), `agg_unit` (call | result),
  `ratio` = aggregator price ÷ our per-event price, `single_result`, `last_verified_at`, and a
  DERIVED `enabled` with `disabled_reason`. Worker-owned; the call path will only read it.
- **`routes.py`** — the rules, in one place (`eligible`): platform-eligible · policy allows
  overflow (tikhub and scrapecreators are barred by decision) · same unit (a per-result
  aggregator price is accepted for a per-call endpoint that returns ≤ 1 record; Hunter's "one
  credit per 10 emails" compares as a per-call price) · `ratio ≤ 4.0`, or for a FREE endpoint of
  ours an aggregator price ≤ `FREE_ROUTE_MAX_USD` (1¢ — free routes still 402 when the account is
  dry) · verified within 7 days. `match_catalogs` derives candidates from the aggregators' catalogs
  by exact `(host, method, path)` (Orthogonal) / `(provider, path)` (Monid); `apply_sync` upserts
  and re-derives `enabled`, and disables any row missing from the current sync.
- **Influencers Club discovery**: `request_priced` permits the two exact Orthogonal contracts
  `influencersclub.creators.search` and `.similar` to be admitted by an **absolute ceiling**
  instead of the ratio. `/v1/details` and live runs with 2 and 10 creators confirmed $0.03 per
  request on 2026-09-08. The worker requires a fresh verification and a price at or below the
  verified $0.03 ceiling; `route_for` applies no per-request check. Until 2026-09-17 it also
  compared the flat fee against the request's direct estimate ($0.00598 per creator), so a
  one-creator request was refused with the typed 503 telling the caller to bring their own key
  while a two-creator request was served: a customer's agent read that as "your plan no longer
  allows discovery". Three cents, disclosed through `X-Treg-Cost-Micro` and `X-Treg-Served-Via`,
  beats a refusal. Other unit mismatches remain disabled. A fallback charges the aggregator's
  actual flat fee, including an empty page, rather than multiplying by results.
- **The seed** - `overflow_seed.json` contains candidate mappings and recorded verification evidence.
  Tests pin its historical baseline and expiry behavior. Enabled routes decay after seven days
  without `treg-worker overflow verify`. The Influencers Club verification record is
  `tests/fixtures/aggregators/verification/influencersclub.json`; a redacted discovery envelope is
  `tests/fixtures/aggregators/orthogonal_influencersclub_search.json`. Email enrichment remains
  unverified because its catalog entry has no test request; profile/analytics enrichment and
  parameterized locations have no mapped fallback.
- **Mark scope on a failed child** (`overflow.py`): only `aggregator_auth` and `aggregator_balance`
  mark `overflow:<aggregator>` for every provider. Everything else - the aggregator's account for
  the vendor being dry, and a `malformed` answer (a 5xx, a transport timeout, a non-envelope) - marks
  `overflow:<aggregator>:<provider>`. On 2026-09-17 one Orthogonal Apollo relay answering
  "timeout of 30000ms exceeded" marked the whole aggregator and refused every other provider's
  fallback for 15 minutes, including 62 Influencers Club `similar` calls from one team. A dead
  aggregator host still ends up marked, one provider at a time.
- **`signatures.py`** - the signature table: what a provider's error body means for the registry
  account (`balance` / `quota` means exhausted, `burst` is smoothed but never exhausted, and an
  `unknown` 429 is logged).
  Lusha's "Daily" 429 and Hunter's "per billing period" 429 are quota exhaustion wearing a 429;
  a `retry-after ≤ 60 s` is a burst. cloro says it with a **403** `error.code INSUFFICIENT_CREDITS`
  (from its OpenAPI spec, 2026-09-07 — documented, not yet observed); its 429s are concurrency /
  rate bursts with `X-RateLimit-*` headers and the allowance resets monthly at the
  `cycleResetsAt` that `GET /v1/credits` reports. Apollo says "out of credits" with a **422** and Moz uses a
  **403** `{"issue": "insufficient-quota"}`. Influencers Club's
  documented HTTP 429 `Discovery API credit limit reached` is an endpoint quota signal; ordinary
  per-minute 429s with Retry-After remain bursts. reAPI's empty prepaid balance is a **402**
  `error.code 30001 "Insufficient credits. Required: N"` (observed 2026-09-14); PiAPI's wallet
  exhaustion is acknowledged unobserved. HTTP 402 still uses the shared balance signature.
  Litescrape's HTTP 402 `payment_required` envelope is recorded as balance exhaustion.
  Two guards against
  the next such vendor: `unrecorded`,
  a signal kind for a 4xx no row matched whose body still names credits/quota/balance (pattern =
  the table's own phrases plus generic nouns) - never a mark, only a log line and
  `capacity_signal=unrecorded` on `tool_called`; and the coverage guard in
  `tests/test_capacity_overflow_routes.py`, which fails when a `platform_key_*` provider has neither
  a table row nor an acknowledged gap. `edge_block` is the odd one out: the vendor's CDN refused the
  request's shape (decided on headers, never the caller's UA), so it exhausts nothing, overflows
  nothing and alerts nothing; it exists so a chart can tell a bot-filtered UA family from a
  provider outage. Shared by the sweep, the future call-path trigger and alerts.
- **`infra/upstream/aggregators/`** — the envelopes, and nothing else: `build()` wraps the
  vendor request (Orthogonal `POST /run {api, path, query, body}`; Monid `POST /run {provider,
  endpoint, input}`), `parse()` unwraps the vendor status + body + the real in-band charge, and
  names who to blame when the aggregator itself refused (`AGGREGATOR_SIDE` = `aggregator_auth`,
  `aggregator_balance`, `malformed` - the call path marks the aggregator unhealthy for everyone, the
  verifier leaves the route alone; `contract` - the aggregator's own per-request refusal, including
  Orthogonal's bare 400/422/404 with no vendor data - is request-scoped: child released, nothing
  charged, no mark, `malformed` being reserved for non-JSON, 5xx and transport errors;
  `VENDOR_DRY`, folded in by `with_vendor_verdict` from the
  signature table - the one place a relayed body is read - is the aggregator's account for THIS
  vendor (a relayed 402, Apollo's 422, a period 429): the call path marks
  `overflow:<aggregator>:<provider>` only, so one vendor's cap never takes the others offline.
  Deliberately not the direct path's strike ladder: the mark is immediate and a flat 15 min, because
  a relayed body carries no headers to tell a burst from a cap and the caller has already paid the
  aggregator's round trip;
  `contract` = its stricter schema, no vendor call, no charge; `pending` = a Monid async run to
  poll). Fixtures are recorded bodies (PII hashed) in `tests/fixtures/aggregators/`; every fixture
  round-trips. Keys are passed in by the caller and never read, logged or stored here.
- **`verify.py`** + `treg-worker overflow verify` — the weekly re-verify: one cheap call per
  route through the aggregator (and, when we hold the vendor key, directly), compare the shape
  fingerprint (keys and list/leaf markers, values ignored), stamp `last_verified_at` or disable
  with the reason. Two per-route price caps and one run budget: a route that is enabled or was
  stamped before is a **renewal**, held to `--renew-max-usd` (default $1); a never-verified pair is
  **discovery**, visited only under `--all` and held to `--max-usd` (default 2¢). Renewals go first,
  oldest stamp first, so the route nearest its 7-day decay is reached before `--budget-usd`
  (default $15, relay fee plus the direct comparison when we hold the vendor key) runs out; a route
  that does not fit the budget is skipped, not the rest of the run. Routes whose endpoint has no
  `test_request` are skipped. One cap for both once cost real routes: on 2026-09-07 the weekly
  `verify --all` at 2¢ skipped 134 routes, among them all 46 stamped on 2026-08-26 and priced
  3¢–65¢ (branddev, predictleads, findymail, leadsforge, apollo, companyenrich, fiber-ai, pdl,
  hunter, icypeas); they decayed off at the next sync and no run could ever bring them back.
  `--only` restricts the run to comma-separated provider IDs.
  Verify only STAMPS - `overflow sync` is what re-derives `enabled` from the
  stamps, so every verify must be followed by a sync (the weekly cron chains the two since
  2026-09-08, `ops/deploy.md`); a route disabled by one failed verify is only re-enabled by that
  sync, and a route past its 7 days keeps serving until a sync notices - the sync is the decay. A failed route is a result, not a failed run: the command exits 0 after
  completing (it used to exit 1 whenever any route failed, which made every Render run read
  "failed"). `verify.verdict` is the one place that decides what a verification means: `passed`
  stamps; `failed` (contract refusal, relay non-2xx, a 2xx of a different shape) disables with the
  reason, and only when the direct leg proved the request (a direct 2xx beside a relay non-2xx or
  a different shape, or a contract refusal); `aggregator` (`AGGREGATOR_SIDE`, `vendor_dry`,
  unreachable: our key, its account, the vendor's own out-of-credit answer relayed through it, its
  host or envelope) leaves the row untouched; `inconclusive` (no direct 2xx to compare with - no
  key, 401, our own account dry, a stale test_request failing both legs - or a run still pending)
  never disables. One inconclusive still STAMPS: `direct_dry` with a relay 2xx, our own account
  refusing in its recorded dialect - the relay served, the shape cannot be checked for OUR reason,
  and an unstamped route would be decayed by the next sync precisely while our account is dry.
  The verdict is pure over `Verification`'s typed fields (`failure`, `direct_dry`, statuses); the
  note is for people. The run exits 1 when OUR key or OUR prepaid balance was refused on any
  route, when every attempt was lost to the aggregator's side, or when nothing was attempted - so
  a schedule's failure notification means something and one timeout does not trip it. A vendor
  pool dry on the aggregator's side (`vendor_dry`) is theirs to refill: it counts under
  "aggregator errors" in the summary line and never fails the run by itself.
  It spends real money, needs aggregator keys in the environment, and runs as a worker job rather
  than in the dataplane. Hosted scheduling and manual operation are documented privately.

## Protect, part one (step D) — refuse before reserve

PDL's HTTP 402 `hit your account maximum for …` response is an operation quota signature,
so the existing strike ladder locks only the affected endpoint (for example `pdl.x.person-identify`).
Person/company enrichment can remain usable on the same key. Other PDL 402 responses retain the
balance classification. An Arena team top-up cannot replenish this vendor-side allowance.

The call path reads the view and runs the breaker (`marks.py`); the mechanics and the typed
`provider_capacity` 503 are documented in `architecture/proxy-model.md` § Platform capacity and
`interface/api.md`. In one line: locked provider or endpoint → 503 before any hold, with
alternatives named, one probe a minute excepted; two balance/quota signatures in a row on treg's
key → lock; the probe's 2xx → open. Burst 429s are smoothed (D′). Tiers 1/2 untouched.

The breaker is deliberately slow to open and quick to close: a false lock costs every caller a
503 (or, with an overflow route, the aggregator's price) for as long as it lasts, while a missed
one costs one relayed vendor error. A vendor with auto top-up whose balance hovers near zero
answers a genuine quota 429 once in thousands of calls; one strike must not take the provider
away from everyone.

## Protect, part two (step D′) — burst smoothing

`infra/upstream/limiter.py` (per-provider spacer, ≤ 2 s wait, in-process, no DB) and one bounded
`retry-after` re-send for body-less GET/HEAD, documented in `architecture/proxy-model.md` § Burst
smoothing. The provider's rate limit travels in the published latest state (`LatestState.rate_limit`,
from `CapacityPolicy.rate_limit`), so the request path never reads
the policy table. `rate_pressure` alerting is step C.

## Overflow, the child cycle (step E) — off by default

`application/call/overflow.py` is documented in `architecture/proxy-model.md` § Overflow. Operating
it: `TREG_OVERFLOW_MODE` = `off` (default) | `shadow` | `on`; `TREG_OVERFLOW_DAILY_BUDGET_USD`
per aggregator per UTC day is a hard admission cap backed by `OverflowSpend`. Before either an
`on` call or a `shadow` probe goes to the network, a conditional atomic upsert reserves the route's
estimated micro-USD only if the resulting daily total fits under the cap. Completion reconciles the
estimate to actual aggregator cost and increments `calls` once, including a vendor 5xx that the
aggregator charged but treg cannot charge to the caller. Known no-charge failures return the whole
estimate. Once network I/O has started, a timeout, disconnect, parser crash, cancellation, or any
other outcome without a known actual fee keeps the estimate reserved. A process crash after the
reservation commit can do the same; that bias is deliberately conservative because it reduces later
service instead of allowing excess prepaid spend.
The aggregator keys `TREG_OVERFLOW_KEY_*` live only in the service or worker secret environment. The
route view (`routes_view.py`) is the call path's
60 s copy of the enabled `OverflowRoute` rows; `overflow sync` / `overflow verify` are the only
writers. Shadow probes log their route and shape and write a child audit row with
`credential_tier=platform-overflow` and `error_response="treg overflow: shadow"`. Production
rollout duration, monitoring and mode changes belong in the private runbook.
Overflow remains advisory across its full attempt: an unexpected budget, adapter, capacity-mark,
or settlement-path failure is logged, any reserved child hold is released, and the caller falls
back to the direct vendor response. A skip-direct call has no direct response, so the same fallback
returns the original typed `provider_capacity` 503. Cancellation and typed call failures still
propagate to the call service for their dedicated cleanup and response handling.

Overflow reservations also enforce `MarketplaceCall.max_cost_micro` at the aggregator's own
estimate, before any aggregator request. The value is inherited from a direct caller's explicit
ceiling or a routed child's remaining ceiling. A refused reservation releases the temporary
`OverflowSpend` budget claim and leaves no child hold. See `architecture/money.md` for the shared
reservation guard.

## Enabling overflow (step F) — the opt-out and the rollout

`Org.platform_overflow_disabled` (Alembic `0008`; last column in the class on purpose — alembic
appends, keeping create_all test schemas aligned with the migrated shape) is the team opt-out:
`GET/PATCH /orgs/{id}/settings` carries `platform_overflow` (default true), `treg org overflow
[on|off]` sets it. Honoured before any aggregator is contacted, on both entry points (the
post-failure child cycle and the resolver's skip-direct rung); an opted-out team gets the typed 503.
Own keys are never relayed regardless. The charter's "not built" row, `llms.txt`, `skill.md` (+
plugin), `README.md` and `USAGE.md` now say what treg may do and how it discloses it.

The production rollout, account balances, verification schedule and rollback procedure are in the
private [provider-capacity runbook](https://github.com/superdesigndev/treg-internal/blob/main/docs/production/provider-capacity.md).
Not built: the `overflow_masking` alert, gated on the money-funding-transactions debt. Until it is
built, operators must monitor `OverflowSpend` directly.

## Not built yet (plan step C)

Forecasts, recharge verification and every alert (`quota_exhausted`, `rate_pressure`, `overflow_masking`,
…) — step C, gated on the `money-funding-transactions` debt. Until the rollout above flips the mode,
`TREG_OVERFLOW_MODE` is `off` and treg still relays a vendor's 402 unchanged (or answers the typed 503
when the account is marked exhausted).

## Kitt AI capacity

`collectors._trykitt` reads `/credit` in USD. A funded account uses the common
`latest_state` policy: a fresh zero balance marks it exhausted. Free Forever calls
worked at zero before funding, but paid exhaustion has not been tested. That old
free-plan observation does not override the paid account's balance policy.
`signatures._TABLE` records the observed HTTP 418 `temporarily throttled`
response as a burst, with no invented reset or retry delay. Routed calls treat this
as an upstream error and may try the next child; it does not mark the account dry.
Kitt documents 402 for both rate limits and insufficient funds, so only an explicit
insufficient-funds phrase marks balance exhaustion; ambiguous 402 stays unknown.
Paid exhaustion and paid concurrency have not been live-tested. Free-plan burst
results varied, so no numeric free-plan concurrency/rate limit is configured.


## ContactOut independent pools

`collectors._contactout` exposes the three raw credit pools through an informational observation,
not a scalar balance. `snapshot_from` and `latest_state` preserve it without marking the provider
exhausted. Prepaid quotas are already remaining credits. The pools are independent and require
separate operator monitoring and top-ups. Stats freshness remains unconfirmed; treg
keeps the existing sweep cadence and does not assume behavior at zero credits. See
[ContactOut](../architecture/contactout.md).

ContactOut overflow now has verified routes on Orthogonal and Monid, using the same price gates,
expiry, opt-out and budget controls. Its documented out-of-credit 403 is endpoint-scoped quota,
not a provider-wide balance lock. See the ContactOut fragment for enabled coverage and the paid
`scripts/contactout_overflow_verify.py --budget-usd 10 --apply` renewal command; nonexistent static
catalog examples cannot renew successful contact checks. Hosted policy, mode and renewal scheduling
remain private operational state.

## AnyAPI

A spent prepaid wallet or a key's own spend cap answers `402`. Observed 2026-09-19 by driving a
trial key past its cap: `{"error": "trial_cap_reached"}`, recorded in `signatures._TABLE` as a
`balance` signal. The funded-wallet body ("insufficient wallet balance", per its OpenAPI 402
description) matches the same row but has not been observed. No overflow route is claimed. See
[AnyAPI](../architecture/anyapi.md).
