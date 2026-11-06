---
title: Provider capacity - knowing what treg's own vendor accounts have left
status: shipped
sources:
  - src/treg/domain/capacity/__init__.py
  - src/treg/capacity_notice.py
  - tests/test_capacity_notice.py
  - src/treg/domain/capacity/collectors.py
  - src/treg/domain/capacity/policy.py
  - src/treg/domain/capacity/sweep.py
  - src/treg/domain/capacity/view.py
  - src/treg/domain/capacity/routes.py
  - src/treg/domain/capacity/signatures.py
  - src/treg/domain/capacity/verify.py
  - src/treg/domain/capacity/marks.py
  - src/treg/domain/capacity/events.py
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
  - src/treg/domain/capacity/fair_share.py
  - tests/test_capacity_fair_share.py
  - src/treg/infra/provider_concurrency.py
  - tests/test_provider_concurrency_redis.py
  - src/treg/domain/capacity/rate_observe.py
  - src/treg/infra/provider_rate.py
  - tests/test_capacity_rate_observe.py
  - src/treg/domain/capacity/overflow_seed.json
  - src/treg/infra/upstream/aggregators/__init__.py
  - src/treg/infra/upstream/aggregators/orthogonal.py
  - src/treg/infra/upstream/aggregators/monid.py
  - src/treg/infra/upstream/aggregators/catalogs.py
  - src/treg/alembic/versions/0006_overflow_route.py
  - tests/test_capacity_overflow_routes.py
  - tests/test_influencersclub_overflow.py
  - scripts/contactout_overflow_verify.py
  - src/treg/worker.py
  - scripts/provider_balances.py
  - src/treg/alembic/versions/0005_capacity_policy_snapshot.py
  - tests/test_capacity_know.py
  - tests/test_capacity_collectors.py
related:
  - architecture/data-model.md
  - architecture/money.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

# Provider capacity

When a treg-owned vendor account runs dry, callers receive an upstream refusal they cannot fix
themselves. Capacity handling has three layers: **know** the runway, **fund** before it dies, and
**protect** the call when it dies anyway (refusal before reserve, same-endpoint overflow, a typed
503). Live account state and production observations belong in the private provider-capacity
runbook in `treg-internal`.

Scope: treg-owned platform credentials only. A caller's own tool or key is never consulted or
affected by anything here (non-negotiable 1). A `platform_auth: anonymous` catalog fallback is not
the platform tier either: the shared-key spacer and capacity marks do not apply to it, and an
anonymous upstream 429 is relayed without marking treg's account. Removing the provider from
`TREG_PLATFORM_PROVIDERS` stops both the anonymous and the platform-key offers while team keys keep
working.

A provider can have several treg-owned accounts (`TREG_PLATFORM_ACCOUNTS`,
`architecture/proxy-model.md`). Smoothing buckets, fair-share places and call-path locks are per
account (a further account's keys are suffixed `#<label>`), and the provider is out only when every
account is. Policies, the sweep and balance collectors read only the primary, so a further
account's balance is not observed and its lock lifts at expiry or by its own probe.

Provider-specific facts (meter quirks, rate sources, exhaustion bodies) live on the provider's
collector, its `_KNOWN` / `_RATE_LIMITS` entry, its signature row or its catalog file, not here.

## Know

- **Every platform-key slot has a balance decision**: it is on `BALANCE_ROUTES` (a free collector
  reads its balance) or on `NO_BALANCE_API` (with the reason none exists), under the slot's
  spelling; `test_every_platform_key_has_a_balance_decision` fails a new slot that is on neither.
  A collector never spends a paid call to read a balance.
- **What a collector reports.** A sweep reading below `EMPTY_BELOW` refuses the provider's
  shared-key calls, so a collector reports a meter that bills overage rather than refusing as
  informational once spent, and sums every pool that can pay for a call. Vendors in
  `OVERDRAFT_SERVED` keep serving past zero, so their low balance is informational and `overdrawn`;
  each needs a refusal signature. A meter of several independent pools that cannot be one scalar
  is an informational observation that never marks the provider exhausted.
- **`collectors.py`** returns `{value, unit, note}` and is shared with `scripts/provider_balances.py`.
  `provider_balance()` never raises (a failure is a row) and reads the key *setting*, not
  `platform_key_for`: the serving allow-list is a kill switch, and a provider just switched off is
  exactly one whose last balance we still want.
- **`policy.py`** - `CapacityPolicy` defaults per account (`_KNOWN`). The population is every
  `platform_key_*` slot plus `overflow:orthogonal` / `overflow:monid` (aggregators are prepaid
  accounts too). `ensure_policies` inserts missing rows only (a hand-edited row is never
  overwritten) and returns the providers still `unknown`, which a person must classify; code never
  guesses. `latest_state()` is the pure rule: no, failed or old (`STALE_AFTER`) observation is
  `stale` and never refuses a call; `remaining < EMPTY_BELOW` on an exact observation (a spent float
  pool can read a few millionths, never zero) is `exhausted` until `resets_at` or the next sweep.
- **`sweep.py`** - `run_sweep(db)` collects all providers in parallel with the DB idle, writes one
  `CapacitySnapshot` per provider, publishes each `LatestState` to ratestore as
  `capacity:state:<provider>` and commits once. The key is the Settings slot spelling
  (`fiber_ai`); `state_key()` maps a catalog id (`fiber-ai`) onto it, so readers find the state
  under either spelling. A note that looks like a credential is withheld before it is stored. The
  sweep never touches the call path's locks: the meter it reads may not be the allowance that ran
  out.
- **`view.py`** - `LatestStateView`, the in-process copy of both namespaces reloaded from ratestore
  on a TTL by an explicit `await load()`; its readers are sync and I/O-free so `resolve` can call
  them. A writer calls `invalidate()`; other replicas see the change within one TTL.

`treg-worker capacity sweep [--only a,b] [--json]` runs it. It is deliberately not a `treg`
subcommand: the light CLI may not import the DB stack, and the sweep needs the platform keys plus
outbound third-party calls, which are worker work and never dataplane lifespan work.

`CapacityPolicy` and `CapacitySnapshot` are written by the worker only. `usd_per_unit_micro` NULL
means never invent a dollar figure. Numbers only, no key or payment detail.
`treg.domain.capacity` may not import outer layers (import-linter contract), and money is never
touched: capacity marks are ratestore rows, never balances.

## Protect: refuse before reserve

The call path reads the view and runs the breaker (`marks.py`, namespace `capacity:lock:<key>`;
key = provider for a balance signature, endpoint id for a quota one). The mechanics and the typed
`provider_capacity` 503 are in `architecture/proxy-model.md` § Platform capacity and
`interface/api.md`. In one line: locked provider or endpoint means a 503 before any hold, with
alternatives named and one probe a minute excepted; two signatures on treg's key within the strike
window, a minimum gap apart with no 2xx between, lock it; the probe's 2xx opens it, whether the
lock or the sweep's reading refused it.

The breaker is deliberately slow to open and quick to close: a false lock costs every caller a 503
(or the aggregator's price) for as long as it lasts, while a missed one costs one relayed vendor
error. A vendor with auto top-up hovering near zero answers a genuine quota 429 once in thousands
of calls; one strike must not take the provider away from everyone.

- The sweep's `exhausted` reading gets the same probe, from one minute after the reading, so a
  top-up is noticed before the next sweep; the probe's 2xx lifts the reading (`clear_sweep_state`,
  conditional on the reading it was admitted under) until the next sweep.
- A free route (catalog `cost.type: free`) spends no balance, so the provider's balance row and the
  sweep's reading neither refuse it nor take its 2xx as recovery; only its endpoint's own quota row
  applies. Otherwise a vendor answering free searches at zero credits erases the paid routes'
  strikes.
- A signature the call could not avoid (the strike itself, a probe through a lock, a free route)
  is relayed as the provider answered, with `X-Treg-Capacity: exhausted` so the caller is told it is
  treg's account, not its balance (`capacity_notice.py`).
- An expired guessed hold that is still dry opens again on its next two strikes, so a lock nobody
  lifts reopens about hourly.

**`signatures.py`** says what a provider's error body means for treg's account: `balance` / `quota`
mean exhausted, `burst` is smoothed but never exhausted, an `unknown` 429 is logged. A quota row
with no wait header takes its reset from the row's `resets`, else from the sweep. Wait headers are
parsed by format, never by provider; a 429 no row matched is a burst when the wait is at most 60 s
and a quota when longer. Exhaustion can arrive as any status, including a 2xx: the call path reads
a platform 2xx through the table too, so such an answer is released rather than settled, never
archived, counted as a strike and eligible for overflow. Bare HTTP 402 uses the shared balance
signature. Two guards against the next unrecorded vendor:

- `unrecorded`: a 4xx no row matched whose body still names credits, quota or balance (generic
  nouns plus each row's `tripwire` phrase; a row's regex never joins by itself, since alternations
  like `insufficient|balance` would flag a caller's own error). It is never a mark, only an event
  and `capacity_signal=unrecorded` on `tool_called`.
- The coverage guard in `tests/test_capacity_overflow_routes.py` fails when a `platform_key_*`
  provider has neither a balance/quota row nor an entry in `_UNRECORDED_SIGNATURE` (a burst row says
  nothing about an empty account). Record an exhaustion body only from a real response.

`edge_block` is the odd one out: the vendor's CDN refused the request's shape (decided on headers,
never the caller's UA), so it exhausts, overflows and alerts nothing; it lets a chart tell a
bot-filtered UA family from a provider outage.

**Events.** `capacity {json}` lines (`events.py`, logger `treg.capacity_events`): `strike`,
`lock_opened` (written once, stored as the lock's `opened_at`), `lock_cleared` (`via` `probe` or
`sweep_probe`, `locked_s` from `opened_at`), `unrecorded`, `edge_block`, `rate` and `rate_observe`.
Each carries `provider`, `slot`, `endpoint_id`, `kind` and the upstream `status`, never the
vendor's body. A signature meeting an existing lock emits nothing; the `request` line
(`org-traffic.md`) carries every refused call. Overflow marks write no events. No alerts,
forecasts or recharge verification exist in code; alerting on these lines is deployment
configuration, and `OverflowSpend` must be watched directly.

## Protect: burst smoothing

`infra/upstream/limiter.py` (per-bucket spacer, bounded wait, in-process, no DB) plus one bounded
`retry-after` re-send for body-less GET/HEAD; see `architecture/proxy-model.md` § Burst smoothing.
Rates come from `_RATE_LIMITS` (provider-wide) and `_ENDPOINT_RATE_LIMITS` (an endpoint with its
own allowance, its own bucket) and travel in the published `LatestState.rate_limit`, so the request
path never reads the policy table. The spacer is not a quota gate: a call whose computed wait
exceeds `DEFAULT_MAX_WAIT_MS` proceeds, and a provider-wide rate is the tightest of the provider's
endpoint families. A concurrency allowance is not a rate and goes in `_CONCURRENCY`, never into the
token bucket.

## Protect: fair share of a provider's concurrency

A provider that caps the calls in progress on one account (`_CONCURRENCY`, read through
`view.concurrency_pool`) is shared by every team on treg's key. An endpoint family with its own
allowance (`_ENDPOINT_CONCURRENCY`: a name, a limit and its endpoints) has its own places, which
replace the provider's for those endpoints and do not draw from them. `fair_share.py` applies max-min fairness
on those places, DB-free, after the DB phase and around the relay (platform tier only):

1. A free place with no one waiting is taken at once.
2. When every place is taken the call waits in one line; each freed place goes to the waiting call
   whose team has the fewest calls in progress, oldest first. No place stays empty while a call
   waits.
3. After `fair_share_wait_s` (0 turns the gate off) the call leaves as the typed `share_busy` 429,
   not charged. A routed job reads it as a child error and asks its next provider.

The place is held until the answer is read and released in the relay block's `finally` on every
path; a streamed free answer releases when the relay block ends. A wait shows in `X-Treg-Smoothed`
as `share_wait=<ms>`.

The line is per process; the cap is per account across processes. With `TREG_KV_URL` set each
process asks the shared store for places (`infra/provider_concurrency.py`, one Redis sorted set per
account on the same Redis lease scripts as org concurrency slots), as many at once as it has free places and waiting
calls, and hands each landed place to the call rule 2 picks at that moment. A place that lands
after its call left, or a reply that arrives after the lease could have expired, goes straight
back. Leases are renewed while the call runs, so a crashed process frees its places within one
lease; expiry cannot stop a request already sent. When the store fails the process counts locally
and skips the store briefly; without `TREG_KV_URL` each process enforces the cap alone. Choosing an
account (`keyslots`) reads only this process's places.

## Protect: a shared rate window, observed everywhere, enforced where listed

The smoother is per process and never refuses, so every further process adds its own allowance.
The shared window holds a declared rate across processes. It observes every bucket, so the declared
numbers are checked against what providers actually serve, and refuses only on the buckets an
operator lists once those numbers hold.

`TREG_PROVIDER_RATE_MODE` = `off` (default) | `observe` turns it on; `TREG_PROVIDER_RATE_ENFORCE`
names the buckets it refuses on (`view.rate_bucket` keys without the `#account` suffix,
case-insensitive, so one name covers every treg-owned account of that bucket; startup warns
`event=provider_rate_enforce_unknown` for a name this build declares no rate for). Either needs a
Redis `TREG_KV_URL`, and an enforce list needs `observe`; `Settings` refuses to start otherwise.

- A platform call about to go upstream (not an archive hit, never an own key) asks
  `infra/provider_rate.admit` inside relay's `before_outbound`, after relay's own pre-send refusals,
  so a call relay refuses takes no room. It runs after the smoother's wait.
- One sorted set per bucket and window length (`treg:provider-rate:v3:<bucket>:<window_ms>`),
  expiring by the store's clock: a changed limit judges the same members, a changed length starts a
  new window. A refusal adds no member, so the admitted count is what the provider sees.
- A store fault or a reply slower than 200 ms is `unknown`, never refuses, and skips the store for
  5 s in that process; then one call probes while the others still skip, and only that probe
  closes the breaker.
- On an enforced bucket a full window refuses before sending: the typed `provider_rate_limited`
  429 ([api](../interface/api.md)), `Retry-After` rounded up from the oldest member's expiry, hold
  released, the smoother's token given back, `refused_by=capacity` on its record. Overflow then gets
  its chance as for a provider's burst 429 (forced trigger `burst`); when it does not serve, the
  caller still gets the rate refusal. A routed job records the candidate `skipped` without spending
  its error budget.
- A routed child on treg's own key whose answer the signature table marks `balance` or `quota`
  (treg's account out of money or out of its period allowance, e.g. LeadOcean's 429 "Fair-use pace
  reached") is likewise `skipped` (`child_outcome` kind `account_exhausted`), never an error: one
  used-up account beside providers that answered "nobody" leaves the call a 200 miss, not a 502.
  A provider that is down (5xx) is still an error, so a failed routed call stays free.
- A capacity probe (`probe_lock_id`) and a team's poll of its own job (`free_owned_poll`) are
  counted but never refused. The one `retry-after` re-send asks the window after its sleep; on a
  full enforced bucket it is not sent and the provider's first 429 stands.
- With an org/endpoint quota pending, the window is asked first: a rate refusal spends no quota,
  and a quota refusal gives its member back (`provider_rate.release`, awaited by
  `provider_rate.drain` before the store closes) and is not counted.

`rate_observe.py` counts verdicts (`admit`, `over`, `unknown`) against the provider's status (`2xx`,
`429`, `other`, `none`) or `refused`. Each process emits one `rate_observe` line per bucket and rate
about once a minute (`flush_worker`, started by every app role's lifespan when the window is on) and
at shutdown; `treg-worker` flushes when its command ends, and an abrupt exit loses unemitted counts.
Sum lines across processes. A large `over_2xx` says the declared rate is too low. `admit_429` says
it is too high only while `over_*` is near zero: refused-but-relayed calls still spend the
provider's allowance, and public-resource checks, compensating deletes and the worker's async polls
on the same key are not counted.

## Overflow routes - derived, never hand-written

**Overflow** = the same vendor endpoint served through a treg-owned aggregator account (Orthogonal
first, Monid second) when treg's direct account is out. It is a credential rung
(`platform-overflow`), not a vendor: not in the catalog, not searchable, no own key. The caller pays
the aggregator's real price with no markup, disclosed through `X-Treg-Cost-Micro` and
`X-Treg-Served-Via`.

- **`OverflowRoute`**: one row per `(endpoint_id, aggregator)` with a derived `enabled` and
  `disabled_reason`. Worker-owned; the call path reads it through `routes_view.py`.
  `overflow sync` and `overflow verify` are the only writers.
- **`routes.py` `eligible`** holds every rule: platform-eligible, policy allows overflow
  (`_NO_OVERFLOW`), same unit (a per-result price is accepted for a per-call endpoint returning at
  most one record), `ratio <= MAX_RATIO` or, for an endpoint free to us, an aggregator price at most
  `FREE_ROUTE_MAX_USD` (free routes still refuse when the account is dry), verified within
  `VERIFY_MAX_AGE`. `request_priced` admits the exact fixed-fee contracts in `_FIXED_DISCOVERY_PATHS`
  by an absolute ceiling instead of the ratio and with no per-request comparison: a per-request
  check refused small requests while serving larger ones, which callers read as a plan change. Such
  a fallback charges the aggregator's flat fee, including an empty page.
- **The seed** (`overflow_seed.json`) holds candidate mappings and verification evidence. Enabled
  routes decay without verification. A free-to-us vendor route still carries the aggregator's list
  price here (Apollo people search is `$0.002`/call): that figure is what catalog disclosure and the
  child reserve use; settlement still follows the aggregator's in-band charge. Verification records
  live in `tests/fixtures/aggregators/verification/`; `scripts/contactout_overflow_verify.py` renews
  ContactOut routes whose static catalog examples cannot.
- **Mark scope on a failed child** (`overflow.py`): only `aggregator_auth` and `aggregator_balance`
  mark `overflow:<aggregator>` for every provider. Everything else (the aggregator's account for
  this vendor dry, a vendor refusal, a `malformed` answer such as a 5xx or timeout) marks
  `overflow:<aggregator>:<provider>`, so one vendor's timeout never takes every other provider's
  fallback offline. A dead aggregator host still ends up marked, one provider at a time. The mark
  is immediate and flat, unlike the direct strike ladder: a relayed body carries no headers to tell
  a burst from a cap, and the caller already paid the aggregator's round trip.
- **`infra/upstream/aggregators/`** - the envelopes only: `build()` wraps the vendor request and
  `parse()` unwraps vendor status, body and real in-band charge, naming who to blame:
  `AGGREGATOR_SIDE` (`aggregator_auth`, `aggregator_balance`, `malformed`) marks the aggregator;
  `contract` (the aggregator's own per-request refusal, including Orthogonal's bare 400/422/404
  with no vendor data) is request-scoped: child released, nothing charged, no mark; `VENDOR_DRY`
  (from `with_vendor_verdict`, the one place a relayed body is read through the signature table)
  and `VENDOR_REFUSAL` (an otherwise unrecognized relayed 401/403) use the provider-scoped mark;
  `pending` is a Monid async run to poll. A valid Monid run envelope takes precedence over its
  outer HTTP status, because Monid mirrors relayed vendor 401/402/403: only a refusal with no run id
  proves the Monid key or balance failed. Fixtures are recorded bodies with PII hashed; every
  fixture round-trips. Keys are passed in and never read, logged or stored here.
- **`verify.py`** + `treg-worker overflow verify`: one cheap call per route through the aggregator
  (and directly when treg holds the vendor key), comparing shape fingerprints (keys and list/leaf
  markers, values ignored). A mismatch prints a bounded key-only structural diff with
  identifier-shaped keys replaced, never values. A route enabled or stamped before is a
  **renewal** held to `--renew-max-usd`; a never-verified pair is **discovery**, visited only under
  `--all` and held to `--max-usd`. One cap for both once let every renewal above the discovery cap
  decay with no way back, so keep them separate. Renewals go first, oldest stamp first, so the
  route nearest decay is reached before `--budget-usd` runs out; a route that does not fit is
  skipped, not the rest of the run.
- **Verify only stamps; `overflow sync` re-derives `enabled`.** Every verify must be followed by a
  sync: a route disabled by a failed verify is re-enabled only by that sync, and a route past its
  age keeps serving until a sync notices (the sync is the decay).
- **`verify.verdict`** decides what a verification means. `passed` stamps. `failed` disables, and
  only when the direct leg proved the request. `aggregator` (aggregator side, `vendor_dry`,
  unreachable) leaves the row untouched. `inconclusive` (no direct 2xx to compare, or still
  pending) never disables, but `direct_dry` with a relay 2xx still stamps: otherwise the next sync
  would decay the route precisely while treg's own account is dry. A failed route is a result,
  not a failed run; the run exits 1 only when treg's own aggregator key or balance was refused,
  every attempt was lost to the aggregator side, or nothing was attempted. It spends real money and
  runs as a worker job.

## Overflow: the child cycle

`application/call/overflow.py` is described in `architecture/proxy-model.md` § Overflow.
`TREG_OVERFLOW_MODE` = `off` (default) | `shadow` | `on`; the aggregator keys
`TREG_OVERFLOW_KEY_*` live only in service or worker secrets.

- **Daily budget.** `TREG_OVERFLOW_DAILY_BUDGET_USD` per aggregator per UTC day is a hard admission
  cap backed by `OverflowSpend`. Before an `on` call or a `shadow` probe reaches the network, a
  conditional atomic upsert reserves the route's estimate only if the new total fits. Completion
  reconciles to actual aggregator cost and counts the call once, including a vendor 5xx the
  aggregator charged but treg cannot pass on; known no-charge failures return the estimate. Once
  network I/O started, any outcome without a known fee (timeout, cancellation, crash) keeps the
  estimate: deliberately conservative, since it reduces later service instead of allowing excess
  prepaid spend.
- **Caller ceiling.** Reservations also enforce `MarketplaceCall.max_cost_micro` at the
  aggregator's estimate before any request (inherited from a direct caller's ceiling or a routed
  child's remainder). A refusal releases the budget claim and leaves no child hold
  (`architecture/money.md`).
- **Advisory.** An unexpected budget, adapter, mark or settlement failure is logged, any child hold
  is released, and the caller gets the direct vendor response (or, on a skip-direct call, the
  original `provider_capacity` 503). Cancellation and typed call failures still propagate.
- **Shadow** probes log route and shape and write a child audit row with
  `credential_tier=platform-overflow` and `error_response="treg overflow: shadow"`.
- **Team opt-out.** `Org.platform_overflow_disabled`, exposed as `platform_overflow` on
  `GET/PATCH /orgs/{id}/settings` and `treg org overflow [on|off]`, is honoured before any
  aggregator is contacted on both entry points (the post-failure child cycle and the resolver's
  skip-direct rung); an opted-out team gets the typed 503. `llms.txt`, `skill.md`, `README.md` and
  `USAGE.md` describe what treg may do and how it discloses it; keep them in step.
