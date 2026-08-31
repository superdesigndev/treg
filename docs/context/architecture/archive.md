---
title: Archive - every platform answer, kept and versioned (cache = the newest layer)
status: building
sources:
  - src/treg/archive.py
  - src/treg/alembic/versions/0002_archive_tables.py
  - src/treg/alembic/versions/0003_callrecord_cached.py
  - src/treg/alembic/versions/0004_archivekey_request_shape.py
  - src/treg/api.py
  - src/treg/bootstrap.py
  - src/treg/routers/admin.py
related:
  - architecture/data-model.md
  - architecture/proxy-model.md
  - architecture/catalog.md
---

# The archive

Two concepts, one word each — the vocabulary is deliberate and mirrors the charter's discipline:
**cache** is the newest stored answer for a key, served instead of a vendor call while it is
fresh; **archive** is every version of every answer, kept with its timestamp. The cache is the
archive's top layer. History is kept on purpose: it is the future data product (per-key
time-series — backlink profiles over time, price history), not waste.

**Build state: COMPLETE (PR 6 of 6 — the panel shipped after the original five).** All five slices exist behind `TREG_ARCHIVE_MODE`:
skeleton, recorder, catalog `cache` field + report, serve path, and the learner + refresh worker.
What does NOT exist: any billing difference for a cached hit (deferred founder decision), and the
phase-3 aggregator surfaces (history endpoints) — do not document either as existing.

## The learner (PR 5)

Runs inside the recorder on every refetch of a known key. AIMD on `ttl_s`: stable ⇒ ×1.5, capped
by min(30 d, the judged `cache.max_age_s`); changed ⇒ ×0.5, floored at 60 s. A key whose first
`_NEVER_AFTER` (4) refetches ALL changed marks itself `ttl_s = TTL_NEVER (-1)` — never served
until a stable refetch resets it. The lookup prefers the learned timer (`ttl_s > 0`) over the
fixed phase-1 guesses.

**Noise vs change.** When a refetch differs, the diff's leaf paths (lists collapse to `[]`,
bounded depth 6 / 400 paths) are compared to the previous diff-set (`volatile_paths`, kept per
key). The SAME set repeating counts as noise ⇒ stable, under two guards: it must be a minor
share (< 40%) of a body with ≥ 5 leaves — a tiny body whose one value moves every fetch is a
price and stays "changed". First occurrence always counts as changed. Stored bytes are never
touched; stripping exists only in comparison.

## The refresh worker (PR 5)

`archive.refresh_worker` runs in-process from lifespan (adsconv's discipline), gated by
`worker_enabled()` = serve mode AND `archive_refresh_daily_cap > 0`; interval
`archive_refresh_interval_s` (300 s). A key EARNS refreshing: window ≥ 80% consumed AND
`last_requested_at > fetched_at` (a caller asked since the last fetch — a refresh itself never
counts as demand). Brakes: per-provider daily call cap (counted from `origin="refresh"`
snapshots — no bookkeeping table to drift) and 10 per pass. The call replays the stored
request shape — method, vendor-facing URL, body, and the KEYING headers (`req_headers`; without
them the recording lands under a different key, found the hard way in tests) — with injection
built by the ONE authoritative builder (`oauth_providers.platform_bindings` — moved out of
api.py so this worker never imports api; the routers→api import boundary forbids that chain)
and the key value from settings. The refresh spend is treg's own, attached to no org and absent from the ledger — the
cap is the brake. Each refresh records through the same `_store` (`origin="refresh"`), so
refreshing IS the sampling that teaches the timer.

## Serving (PR 4)

`archive.lookup()` runs in `call_tool` at the RELAY's position — after every access/deny/cap gate
AND after the money reserve, replacing only the network trip. **Money on a hit is identical to a
live call, on purpose**: reserve, settle, cost header and ledger rows are byte-for-byte the same;
the response carries `X-Treg-Cache: hit`, `X-Treg-Fetched-At`, `X-Treg-Age`, and the audit row
(+ `/calls`) carries `cached: true`. The founder's deferred pricing decision attaches to that tag
later without touching this code. A hit is NOT a new observation: no snapshot, no change
statistics — only `last_requested_at` (fire-and-forget `_touch`), the demand signal PR 5 reads.

Freshness (phase 1) is `archive.ttl_for(entry)`: FIXED guesses per capability prefix
(`crypto.price` 5 min, `web.search` 1 h, `people.`/`company.` 7 d, default 1 h), always capped by
a judged `cache.max_age_s` (CoinGecko's 24 h duty). The learner (PR 5) replaces these per key.

Caller controls, always honored: `Cache-Control: no-cache`/`no-store` forces a live call (the
read-after-write escape — the archive never guesses cross-endpoint effects); `X-Treg-Max-Age`
tightens (never widens) the window; malformed values are ignored. None on every uncertain branch
— serving off, veto, unjudged policy, no/stale/hash-only snapshot — and a lookup fault degrades
to a live call (an api-side belt catches even a fault in lookup's own plumbing; tested by making
it explode).

`CallRecord.cached` (migration 0003) is declared LAST in the model to match ALTER TABLE's
append position, keeping `create_all` test schemas aligned with the migrated production shape.

## The catalog `cache` field (PR 3)

One licence judgment per PROVIDER, written once at the YAML file header and inherited by every
endpoint below it; an endpoint's own `cache:` overrides. `catalog_store` carries the header form
into `provider_meta["cache"]` (dict, not stringified) and stamps the effective value onto each
normalized endpoint (`entry["cache"]`, absent ⇒ None ⇒ forbidden). The provenance form is
`{mode, license_quote, source_url, checked}` plus optional `max_age_s` — a vendor-imposed refresh
ceiling the learner (PR 5) must treat as a hard cap. `tests/test_archive.py` validates every
declared field in the shipped catalog: a judged entry must carry its quote, source and date.

First judged set (checked 2026-08-27): **coingecko** `transient` with `max_age_s: 86400` — their
API terms permit caching with a mandatory 24-hour refresh and §6.2 forbids anything longer-lived;
**finnhub** `forbidden` — their terms bar sharing data or derived results with any third party,
and serving one team's cached fetch to another is exactly that. DataForSEO and SerpApi terms were
read the same day and are SILENT on storage — left absent (= forbidden) rather than guessed.
36 other platform providers remain unjudged: absent, forbidden, safe.

## The phase-0 report (PR 3)

`GET /admin/archive` (superadmin): totals (mode, keys, snapshots, bodies kept, kept bytes) and
per-endpoint rows — keys, refetches (stable+changed), `change_ratio` = changed/refetches (the raw
how-fast-does-it-move signal), newest fetch. This is the evidence surface for cache judgments and
later for the timers; it complements `/admin/reconcile/repeats`, which prices what repeats cost.

## The recorder (PR 2)

Hooked in `call_tool` immediately after `_buffer_response` — the one line where "metered platform
call, body already in memory" is a fact, which IS eligibility gate 3. Metered 2xx only; the
`X-Treg-Cache`-style serve headers do not exist yet. `archive.record()` is fire-and-forget with
audit's discipline: bounded pending set (512), failures swallowed with a log line, `drain()` on
shutdown (bootstrap) and in tests. A recorder crash cannot fail a call (tested). `drain()` removes
the tasks it gathered itself rather than waiting on their done callbacks — audit's exact drain
discipline; the busy-spin both avoid (the 2026-08 serial-Postgres CI hang) is explained and pinned
for both modules in `tests/test_audit.py`.

**Counted vs kept.** Statistics and the raw-body `content_hash` are recorded for every observed
answer (a hash is an identity, not the content); body BYTES are kept only when the entry's cache
policy is `transient`/`archive` AND the body fits `archive_max_body_bytes` (default 2 MB —
skipped whole, never truncated). Consecutive identical answers dedup via `body_of`; an identical
answer arriving where bytes were never kept stores them now, so a policy upgrade heals forward
without a backfill. The key URL is rebuilt as the vendor sees it (resolved upstream + forwarded
caller params, resolution-consumed names excluded) — credential injection happens later, in the
relay, so a credential cannot enter the key or the store from the request side. One considered
edge for PR 3's per-provider judgment: a vendor that ECHOES the request credential in a 2xx body
would have it stored — judge `cache` per provider with that in mind.

## The mode switch

`TREG_ARCHIVE_MODE` (config `archive_mode`, default `off`) → `archive.mode()`:
`off` | `shadow` (record + learn, serve nothing — phase 0) | `serve` (shadow + answer eligible
fresh hits — phase 1+). Any unrecognized value degrades to `off`: a typo must disable, never
enable. Rollback in production is a dashboard env edit, no deploy.

## Eligibility — three gates, in order

1. **Kind.** `kind: action` entries are never stored; only data reads pass.
2. **License.** Per catalog entry: `cache: forbidden | transient | archive` — either a bare
   string or a provenance dict `{mode, license_quote, source_url, checked}`, exactly like `cost`
   provenance. **Absent ⇒ `archive_default_policy`**, which is `transient` since the founder's
   keep-all decision (2026-08-29): unjudged providers' bodies ARE kept as short-lived cache, and
   the env flips it back to `forbidden` without a deploy. A JUDGED forbidden (a licence that was
   read and says no — Finnhub) is always respected, and a missing entry is never stored.
3. **Tier.** Only METERED PLATFORM calls are recorded. Those responses are already fully buffered
   for the settle (`_buffer_response` needs the provider's reported cost), so recording adds no
   latency and no new data path. Own-key and own-tool calls stream and are never touched — that
   is the privacy line, enforced at write time, not filtered at read time.

Gates 1+2 are `archive.policy(entry)`; gate 3 is the hook site's own context.

## The cache key

`archive.cache_key(method, endpoint_id, upstream_url, body, headers)` → sha256 over the canonical
request: uppercased method, catalog endpoint id (a provider URL reshuffle starts a fresh history),
sorted query pairs, canonical-JSON body hash (raw hash for non-JSON), plus only `Accept` and
`Accept-Language` from the caller's headers. Auth/cookies/tracing/encodings never enter the key —
and credentials could not anyway: injection happens after the key is taken.

## Tables (migration 0002)

`ArchiveKey` — one logical question: `key_hash` (unique), `endpoint_id`, `provider`, effective
`policy`, AIMD timer state (`ttl_s`, grow ×1.5 capped on stable refetch / shrink ×0.5 floored on
change — the learner lands in PR 5), change statistics (`change_seen`/`stable_seen`/
`last_changed_at`), learned `volatile_paths` (noisy JSON paths excluded from change detection,
never from stored bytes), and demand (`heat`, `last_requested_at`). Platform-scoped, no `org_id`:
one team's fetch may warm another team's hit, and own-key traffic never enters.

`ArchiveSnapshot` — one version: unique `(key_id, version)`, verbatim `body` bytes, `content_hash`
(raw sha256) for dedup — an identical consecutive answer stores a version row with `body=NULL,
body_of=<carrier row>` instead of the bytes again. Bodies live in Postgres (the `IdempotentCall`
precedent); oversized bodies are skipped by the recorder, never truncated. `origin` says who
fetched: `caller` | `refresh` | `sample`.

## What the archive must never touch

Money. A cached hit will only TAG existing records "cached" — billing of a cached hit is an
explicitly deferred founder decision, and no archive code imports ledger/billing. Relay
faithfulness also extends through time: served bytes are exactly what the vendor sent; noisy-field
stripping exists only on comparison copies inside change detection.

## Tests

`tests/test_archive.py` — mode degradation, policy refusal-by-default, key canonicalization, and
table round-trips; listed in CI's serial Postgres job (never xdist — shared database).

## The panel (PR 6)

`GET /admin/archive/panel` serves `src/treg/web/archive-panel.html` — a data-free page SHELL,
deliberately unauthenticated: every number arrives via fetch() with the admin token the page asks
the operator to paste (kept in localStorage; a refused token reopens the gate). It polls
`/admin/archive` every 5 s, and a clicked endpoint row loads `/admin/archive/keys?endpoint_id=`
(keys newest-demanded first, each with timer state and its last 12 versions, plus the endpoint's
recent call events with their `cached` flag — the panel's HIT/LIVE feed). Clicking a version
square opens `/admin/archive/body?key_hash=&version=` — the stored bytes pretty-printed; a dedup
reference follows `body_of` to its carrier and says which version carries it; a hash-only version
answers honestly that nothing was kept. Every metric, chip, bar, tag and version square carries a
`data-tip` popover explaining itself — the panel is expected to be read by people who forgot what
the numbers mean, so the explanations are part of the product, not decoration. The report gained
additive fields for the panel: per-endpoint `hits` and `kept_bytes`, and totals `hits_today`,
`refreshes_today`, `worker_on`, `refresh_daily_cap`.
