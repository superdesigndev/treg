---
title: Provider capacity — knowing what treg's own vendor accounts have left
status: shipped (steps B–F built; production rollout = the shadow week, then TREG_OVERFLOW_MODE=on)
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

**Problem.** Tier 4 serves ~2,850 catalog endpoints on treg's own vendor keys. When one of *our*
accounts runs dry, every caller on that endpoint inherits a 402 that isn't theirs to fix — 4,604
such errors in the 30 days to 2026-08-26, almost all on the enrichment (money) workload. The plan
(`docs/PROVIDER-CAPACITY-PLAN.md`, local) has three layers: **know** the runway, **fund** before it
dies, **protect** the call when it dies anyway (refuse-before-reserve, overflow through an
aggregator on the *same* endpoint, typed 503). This fragment covers what is built: **know**.

Scope: treg-owned platform credentials only. Tiers 1/2 (a caller's own tool or key) are never
consulted or affected by anything here.

## Pieces (`src/treg/domain/capacity/`)

- **`collectors.py`** — the 31 providers' *free* balance/quota calls (`coroutine(client, key) →
  {value, unit, note}`), moved byte-identically from `scripts/provider_balances.py`. Only DataForSEO,
  TikHub, and Brightdata speak dollars; everyone else meters credits, rows, searches. `NO_BALANCE_API`
  names the 7 providers that publish no meter (dashboard-only) so they read as "no API", never as a
  broken key.
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
third-party calls, which are worker-profile work — never dataplane lifespan work. In production it
is a **Render cron job** (`treg-capacity-sweep` in `render.yaml`, hourly) that pulls its env from
the web service via `fromService`; startup calls read-only `verify_db()` and refuses a missing or stale
schema. `scripts/provider_balances.py` remains the by-hand reconciliation view (balance beside
ledger spend) over the same collectors.

## Data

`CapacityPolicy` (one row per account; `capacity_type`, `source`, `funding_mode`, auto-funding
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
- **The seed** — `overflow_seed.json`: the 461 candidate pairs from the 2026-08-26 mapping run,
  145 of them carrying `verified_at` (direct vs relay, identical body shape, in-band price = list
  price). Under the rules, `treg-worker overflow sync` enables **113** of the 145 (pinned by test);
  the rest are off for a named reason — 23 are our per-result price vs the aggregator's per-call
  price (an open decision, plan §7), 7 are not platform-eligible, the remainder have no aggregator
  price, a 56× ratio, or a barred provider. The enabled count decays to 0 after 7 days without
  `treg-worker overflow verify`.
- **`signatures.py`** — the signature table: what a provider's error body means for OUR account
  (`balance` / `quota` → exhausted; `burst` → smoothed, never exhausted; `unknown` 429 → logged).
  Lusha's "Daily" 429 and Hunter's "per billing period" 429 are quota exhaustion wearing a 429;
  a `retry-after ≤ 60 s` is a burst. Apollo says "out of credits" with a **422** ("Insufficient
  credits"), recorded after 2026-09-01: eleven hours of `people.enrich` 422s with overflow on and
  not one attempt, because no row matched. Moz says it with a **403** `{"issue": "insufficient-quota"}`
  (`quota`: the row allowance resets on Moz's billing day, which the body does not name → default
  lock), recorded after 2026-09-04: 115 of one org's calls went upstream to the spent key and came
  back 403, and "quota" alone is not a tripwire word so nothing was even logged. Two guards against
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
  verifier leaves the route alone; `VENDOR_DRY`, folded in by `with_vendor_verdict` from the
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
  with the reason. `--max-usd` (default 2¢) is a per-route price cap, not a run budget: pricier
  routes are skipped, as are routes whose endpoint has no `test_request`. Without `--all` only
  enabled or previously-stamped rows are visited, so a never-verified pair never enters the rota;
  run it with `--all`. Verify only STAMPS - `overflow sync` is what re-derives `enabled` from the
  stamps, so every verify must be followed by a sync (by hand today, `ops/deploy.md`); a route
  disabled by one failed verify is only re-enabled by that sync. A failed route is a result, not a failed run: the command exits 0 after
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
  Spends real money; needs the aggregator keys in the env - a Render job, never the dataplane.
  How it is scheduled and run by hand is in `ops/deploy.md` § Worker commands.

## Protect, part one (step D) — refuse before reserve

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
it: `TREG_OVERFLOW_MODE` = `off` (default) | `shadow` | `on`; `TREG_OVERFLOW_DAILY_BUDGET_USD` (20)
per aggregator per UTC day is a hard admission cap backed by `OverflowSpend`. Before either an
`on` call or a `shadow` probe goes to the network, a conditional atomic upsert reserves the route's
estimated micro-USD only if the resulting daily total fits under the cap. Completion reconciles the
estimate to actual aggregator cost and increments `calls` once, including a vendor 5xx that the
aggregator charged but treg cannot charge to the caller. Known no-charge failures return the whole
estimate. Once network I/O has started, a timeout, disconnect, parser crash, cancellation, or any
other outcome without a known actual fee keeps the estimate reserved. A process crash after the
reservation commit can do the same; that bias is deliberately conservative because it reduces later
service instead of allowing excess prepaid spend.
The aggregator keys `TREG_OVERFLOW_KEY_*` live in the web service env (the cron pulls them). The
route view (`routes_view.py`) is the call path's
60 s copy of the enabled `OverflowRoute` rows; `overflow sync` / `overflow verify` are the only
writers. **Rollout (plan §5):** run `shadow` for a week with routes enabled — every probe logs
`overflow SHADOW <endpoint> via <aggregator>: … shape …` and lands a child audit row
(`credential_tier=platform-overflow`, `error_response="treg overflow: shadow"`) — then switch to
`on` (step F, which also adds the org opt-out and amends the charter).
Overflow remains advisory across its full attempt: an unexpected budget, adapter, capacity-mark,
or settlement-path failure is logged, any reserved child hold is released, and the caller falls
back to the direct vendor response. A skip-direct call has no direct response, so the same fallback
returns the original typed `provider_capacity` 503. Cancellation and typed call failures still
propagate to the call service for their dedicated cleanup and response handling.

## Enabling overflow (step F) — the opt-out and the rollout

`Org.platform_overflow_disabled` (Alembic `0008`; last column in the class on purpose — alembic
appends, keeping create_all test schemas aligned with the migrated shape) is the team opt-out:
`GET/PATCH /orgs/{id}/settings` carries `platform_overflow` (default true), `treg org overflow
[on|off]` sets it. Honoured before any aggregator is contacted, on both entry points (the
post-failure child cycle and the resolver's skip-direct rung); an opted-out team gets the typed 503.
Own keys are never relayed regardless. The charter's "not built" row, `llms.txt`, `skill.md` (+
plugin), `README.md` and `USAGE.md` now say what treg may do and how it discloses it.

**Rollout runbook (Jason):** 1. set `TREG_OVERFLOW_KEY_ORTHOGONAL` / `_MONID` (rotated keys) in the
Render web service; 2. `treg-worker overflow sync` (113 routes enable from the seed; `--live` also
refreshes prices) and schedule `treg-worker overflow verify` weekly (routes decay off after 7 days
without it); 3. `TREG_OVERFLOW_MODE=shadow` for a week — watch the `overflow SHADOW` log lines and
the `platform-overflow` audit rows for shape mismatches and the daily `OverflowSpend`; 4.
`TREG_OVERFLOW_MODE=on`. Keep the Orthogonal balance ≤ \$20 (ToS risk accepted 2026-08-26).
Not built: the `overflow_masking` alert (step C, gated on the money-funding-transactions debt) —
until then, `OverflowSpend` per day is the masking signal to read by hand.

## Not built yet (plan step C)

Forecasts, recharge verification and every alert (`quota_exhausted`, `rate_pressure`, `overflow_masking`,
…) — step C, gated on the `money-funding-transactions` debt. Until the rollout above flips the mode,
`TREG_OVERFLOW_MODE` is `off` and treg still relays a vendor's 402 unchanged (or answers the typed 503
when the account is marked exhausted).
