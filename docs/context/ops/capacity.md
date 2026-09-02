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
  looks like a credential is withheld before it is stored. Observe-only: no alerts, no marks the
  call path acts on.
- **`view.py`** — `LatestStateView`: the in-process copy of the published state, reloaded from
  ratestore on a 60 s TTL by an explicit `await load()`; `get()`/`is_exhausted()` are sync and
  I/O-free so `resolve._platform_offer` can read them later without breaking its rule. Invalidation
  story (refactor plan §2.2): time-based only — every replica sees a mark within one TTL. **Nothing
  on the call path reads it yet** (that is step D).

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
  a `retry-after ≤ 60 s` is a burst. `edge_block` is the odd one out: the vendor's CDN refused the
  request's shape (decided on headers, never the caller's UA), so it exhausts nothing, overflows
  nothing and alerts nothing; it exists so a chart can tell a bot-filtered UA family from a
  provider outage. Shared by the sweep, the future call-path trigger and alerts.
- **`infra/upstream/aggregators/`** — the envelopes, and nothing else: `build()` wraps the
  vendor request (Orthogonal `POST /run {api, path, query, body}`; Monid `POST /run {provider,
  endpoint, input}`), `parse()` unwraps the vendor status + body + the real in-band charge, and
  names who to blame when the aggregator itself refused (`aggregator_auth`, `aggregator_balance`,
  `contract` = its stricter schema, no vendor call, no charge; `pending` = a Monid async run to
  poll). Fixtures are recorded bodies (PII hashed) in `tests/fixtures/aggregators/`; every fixture
  round-trips. Keys are passed in by the caller and never read, logged or stored here.
- **`verify.py`** + `treg-worker overflow verify` — the weekly re-verify: one cheap call per
  route through the aggregator (and, when we hold the vendor key, directly), compare the shape
  fingerprint (keys and list/leaf markers, values ignored), stamp `last_verified_at` or disable
  with the reason. Spends real money (bounded by `--max-usd`, default 2¢); needs the aggregator
  keys in the env — a Render cron, never the dataplane.
  
  **Verification outcomes** (`VerifyOutcome` enum, `classify_verification()`): a verification is
  classified into one of four outcomes to decide whether to disable a route. **PASS** = both sides
  2xx with matching shapes (updates `last_verified_at`). **FLAKE** = transient vendor/aggregator
  error (malformed JSON, 5xx, 429, network errors, 4xx-vs-4xx) — don't disable or count as fail.
  **SKIP** = can't verify (contract miss from incomplete test_request, pending async run, no direct
  key) — don't count. **FAIL** = real 200/200 shape mismatch — the only case that disables the
  route. The cron exits 0 when only flakes/skips, exit 1 only when real FAILs exist.
  
  **`treg-worker overflow reenable-if-flake [--dry-run]`** — re-enables routes disabled by
  flake-like reasons (malformed, 5xx, 429, contract, network errors). Use `--dry-run` to preview.

## Protect, part one (step D) — refuse before reserve

The call path now reads the view and writes one mark (`marks.py`); the mechanics and the
typed `provider_capacity` 503 are documented in `architecture/proxy-model.md` § Platform capacity
and `interface/api.md`. In one line: exhausted provider → 503 before any hold, with alternatives
named; a balance/quota signature on treg's key → exhausted mark in ratestore for the next caller;
burst 429s only logged until D′. Tiers 1/2 untouched.

## Protect, part two (step D′) — burst smoothing

`infra/upstream/limiter.py` (per-provider spacer, ≤ 2 s wait, in-process, no DB) and one bounded
`retry-after` re-send for body-less GET/HEAD, documented in `architecture/proxy-model.md` § Burst
smoothing. The provider's rate limit travels in the published latest state (`LatestState.rate_limit`,
from `CapacityPolicy.rate_limit`; a call-path mark carries it forward), so the request path never reads
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
