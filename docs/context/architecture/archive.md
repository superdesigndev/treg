---
title: Archive - versioned history and cache admission
status: building
sources:
  - src/treg/archive.py
  - src/treg/catalog/hunter.yaml
  - src/treg/domain/catalog/results.py
  - src/treg/alembic/versions/0031_archive_result_admission.py
  - tests/test_cache_result_admission.py
  - src/treg/archive_bodies.py
  - src/treg/config.py
  - src/treg/infra/object_store.py
  - src/treg/alembic/versions/0032_archive_body_storage.py
  - tests/test_archive_r2.py
  - tests/fake_object_store.py
  - scripts/smoke_archive_r2.py
  - src/treg/alembic/versions/0002_archive_tables.py
  - src/treg/alembic/versions/0003_callrecord_cached.py
  - src/treg/alembic/versions/0004_archivekey_request_shape.py
  - src/treg/alembic/versions/0011_callrecord_archive_link.py
  - src/treg/application/call/service.py
  - scripts/backfill_call_archive_links.py
  - src/treg/api.py
  - src/treg/bootstrap.py
  - src/treg/routers/admin.py
  - src/treg/application/asynctasks.py
related:
  - architecture/data-model.md
  - architecture/proxy-model.md
  - architecture/catalog.md
---

# The archive

Async settlement archives terminal provider JSON under `treg://asynctasks/<call_id>` using the
original endpoint and provider. This mandatory evidence may contain expiring result URLs; the worker
never follows those URLs and never stores generated media bytes. `load_terminal_responses((call_id, endpoint_id), …)`, which looks them up by the indexed key hash
they were stored under and tolerates a pruned carrier,
reads them back (newest snapshot per key, following `body_of`) for the Activity displays, which
extract the artifact through the descriptor rather than by guessing at the provider's shape.

Image/video API entries carry effective `cache: forbidden`, including extended catalog rows.
`lookup` and refresh check the current policy, so old cached responses are not replayed or refreshed.
This does not delete historical snapshots or change ordinary query caching. Terminal evidence is
the deliberate exception: only the winning caller/worker finalizer stores it under the original
call id, independent of replay policy; generated media bytes are never downloaded.

Two concepts, one word each — the vocabulary is deliberate and mirrors the charter's discipline:
**cache** is the last confirmed useful answer for a key, served instead of a vendor call while
it is fresh and no later decisive observation invalidates it; **archive** is every version of every answer, kept with its timestamp. The cache is the
archive's top layer. History is kept on purpose: it is the future data product (per-key
time-series — backlink profiles over time, price history), not waste.

**Build state: COMPLETE (PR 6 of 6 — the panel shipped after the original five).** All five slices exist behind `TREG_ARCHIVE_MODE`:
skeleton, recorder, catalog `cache` field + report, serve path, and the learner + refresh worker.
What does NOT exist: any billing difference for a cached hit (deferred founder decision), and the
phase-3 aggregator surfaces (history endpoints) — do not document either as existing. What DOES
exist since 2026-09-03 is the per-call read below: a team can see the answer ITS OWN call got.

## Body storage and R2 double writing

`archive_bodies` owns body preparation, object reads, the independent upload queue and completed
storage observations. `archive.py` retains request keys, snapshots, transactions, TTL learning
and pruning. `infra.object_store.ObjectStore` exposes only put/get/head; `open_r2` imports
obstore lazily and bootstrap owns its lifecycle. Tests inject `MemoryObjectStore` through
`bootstrap.configure_archive_object_store` or `create_app(archive_object_store=...)`.

Every object name is the raw body's SHA-256, with no prefix. The caller supplies its already-computed content hash to PUT, which validates the hash-shaped name and
uses `checksum_algorithm=SHA256` so R2 verifies the upload checksum. A successful single PUT
returns its hash and byte size, with no follow-up HEAD. HEAD makes one request for size. No custom
sha256 attribute is stored or checked; GET enforces size limits and verifies the downloaded hash.
Same-body concurrent uploads share one PUT in the process; recent successful hashes skip PUT.
R2 stores raw bytes, independent of the media type of any particular call.
DB compression remains unchanged. GET verifies the full hash before returning data.

Migration `0032` adds nullable `ArchiveSnapshot.body_storage`: `db`, `both`, or `r2`; NULL is
interpreted as the legacy DB path and does not promise that bytes were retained. The existing
`content_hash` names the R2 object. R2-only snapshots have no `body_of`; DB and double-write
snapshots keep existing carrier dedup. No historical rows, body columns or objects are migrated
or deleted by this change.

All switches are settings, with environment prefix `TREG_`:

| Setting | Values | Default |
|---|---|---|
| `ARCHIVE_BODY_WRITE` | `db`, `both`, `r2` | `db` |
| `ARCHIVE_BODY_READ_LOOKUP` | `db`, `r2-first` | `db` |
| `ARCHIVE_BODY_READ_RESULT` | `db`, `r2-first` | `db` |
| `ARCHIVE_BODY_READ_TERMINAL` | `db`, `r2-first` | `db` |

`both` first uploads, then enters the existing per-key lock / DB semaphore and transaction to
store the DB body and publish the R2 location. No PUT occurs under a row lock or with a checked-out
DB connection. On upload failure, both R2 write modes retain an eligible DB copy with location
`db`; the write setting chooses the normal destination, while DB remains the rare failure path.
A successful upload followed by a failed DB transaction can leave an unreferenced
content-addressed object; no pointer names a failed upload. Existing policy and size gates apply
before uploading. Hash-only history stays in DB when bytes are ineligible.

R2 has independent `ARCHIVE_R2_UPLOAD_CONCURRENCY` (8), `ARCHIVE_R2_MAX_PENDING` (256), and
`ARCHIVE_R2_MAX_PENDING_BYTES` (128 MiB) budgets. A leader holds one upload slot for
its bounded attempt sequence, including retry jitter; duplicate waiters hold no upload slot. The DB stage keeps its original two slots and 30-second deadline. Upload admission
failure falls back to the separately bounded DB queue in both R2 write modes. Bodies rejected by
policy or size remain hash-only; a later DB queue rejection remains an observable dropped recording.
`ARCHIVE_R2_TIMEOUT_S` (10 seconds) bounds the complete PUT/retry sequence after upload-slot
admission, including retry jitter. Upload-slot waiting is outside that timeout and is measured
separately as `queue_wait_ms`. No extra timeout layer is introduced.
`ARCHIVE_R2_READ_TIMEOUT_S` (2 seconds, configurable) separately bounds each lookup/result/terminal
GET including materializing bytes. The shared SDK transport uses the larger timeout so it cannot
prematurely cut off either operation; application deadlines enforce the separate budgets.
Terminal evidence bypasses best-effort queue admission and synchronously retries uploads up to
`ARCHIVE_R2_TERMINAL_ATTEMPTS` (3), with bounded backoff, before the DB write.
429/5xx are capped at two attempts even for terminal evidence; other terminal failures retain
the configured attempt limit, now within the shared transfer budget. Terminal evidence has an 8-second total upload budget (including queue
wait and retries), at most 20 seconds for DB, and a 28-second total deadline. Upload exhaustion
falls back to DB even for terminal evidence in R2-only mode. A cancelled waiter drains that
bounded evidence operation before propagating cancellation; failures and deadlines log explicitly. Its settlement has
already committed and cannot be undone by storage failure. Terminal failures also log a bounded
error because a worker completion has no pending caller event to annotate.

Readers use `archive_bodies.pointer` to collect R2 metadata inside
a session (`defer(ArchiveSnapshot.body)` for R2-first), then close it before `archive_bodies.read`. When DB bytes are needed, including after an R2 failure, a new short DB
session for fallback bytes. Terminal batches use at most eight simultaneous reads. This applies to lookup, call-result reads,
and terminal-result reads, including the Activity routes' outer auth/query sessions. `r2-first`
only tries R2 for a published `both`/`r2` location; missing objects, timeouts, errors and checksum
mismatches fall back to DB. Lookup selects `result_snapshot_id` under the existing result-state
and observed-version guards, then classifies the resolved body after closing the session. Unknown
results retain the decisive snapshot; empty results invalidate serving without deleting history.
Pruning protects the decisive snapshot and DB carriers of surviving versions. Eligible `both`
rows lose DB bytes and become `r2`; their objects remain untouched. `db` does not contact R2, including for R2-only rows. The admin body
viewer remains a DB-only diagnostic in this first delivery. Read switches should be enabled
before any future R2-only write rollout. No serving allowlist, cohort or production setting is
changed here.

Read diagnostics are in place before enabling any `r2-first` switch. Lookup adds
`cache_body_source` (`db`, `r2`, or `none` when no bytes are available),
`cache_body_fallback_reason` (`none` without fallback), and `cache_r2_read_ms` to the existing
`cache_diagnostics` / `tool_called`. No extra call event or per-call DB write is added.
All paths log bounded reasons without exception text, keys, bodies or credentials:
`not_found` and `timeout` are WARNING; `permission_denied` (including signature failures) and
`hash_mismatch` are ERROR. Oversized objects are also ERROR (`too_large`); other transport errors
and an unavailable client are WARNING (`store_error`, `store_unavailable`). HTTP 429 is
`rate_limited`, HTTP 5xx is `upstream_error`, both WARNING on read fallback. These same reason
names appear on failed uploads. Logs include the exception class, never the exception text. Result and terminal
reads use these logs because they have no `tool_called`. Existing per-path process counters remain;
additional bounded per-path/reason counters distinguish the failure classes.

DB fallback requires a snapshot that still has DB bytes or a DB carrier, normally written during
`db`, `both`, or a failed R2-only upload. Successful `r2` writes have no DB copy: an R2 read failure
becomes a cache miss and calls upstream for lookup; history returns `stored=false` with no response
body, and terminal views have no archived terminal body. An old `both` snapshot or an R2 upload's
DB fallback can still serve after the global write switch changes. A failed read does not mean the
object was never archived or has been deleted.
Read timeout and fallback observability must precede `r2-first`, so the entire double-write window
has visible fallback rates. Observing those rates is a prerequisite for closing the double-write
window and switching new writes to `r2`.

`tool_called` is emitted at call completion and includes `archive_body_write`; archive queue
latency cannot delay it. The separate `archive_body_stored` completion event carries `call_ref`,
`storage`, `upload_status`, `upload_ms`, `queue_wait_ms`, `dropped` and `drop_reason`. Join by
`call_ref`. A failed R2 upload followed by a committed DB copy is not dropped. R2-only upload
failure follows that same DB fallback for eligible bytes. These remain best-effort background
writes: a killed process can lose completion events, but cannot withhold the calling event.
The same completion event measures archive stages, including elapsed work on timeout or cancellation:

| Fields (milliseconds) | Scope |
| --- | --- |
| `compare_sem_wait_ms`, `compare_ms` | Precomparison slot wait, then pointer queries/reads and any JSON normalization. Both precede the DB write deadline. |
| `record_key_wait_ms`, `record_sem_wait_ms` | Same-key lock wait, then write-slot wait, inside the DB write deadline. |
| `record_db_ms` | Wall time while holding the write slot: pool checkout, body packing, SQL/commit, and integrity retries/backoff. This is not pure SQL time. |
| `observe_sem_wait_ms`, `observe_ms` | Optional post-commit change-report slot wait and work, outside the DB write deadline. |

Unentered phases are `null`. `failure_phase` names the measured phase interrupted by an escaping
exception (including cancellation); it is `null` when none was interrupted, including a queue
rejection before recording starts. Internally handled comparison failures still fall back to raw
hashes and use the existing counters. A post-commit observation cancellation does not mean the
snapshot was lost: `storage`/`dropped` continue to describe the committed write. Timings add no
DB writes or per-call events and do not change deadlines, the two archive slots, or pool sizes.

Stats count snapshots with recoverable bodies in DB or R2, including deduplicated versions;
`kept_bytes` is logical retained response bytes, not PostgreSQL physical table size.

When archive mode is enabled, any R2 switch requires `ARCHIVE_OBJECT_STORE_ENDPOINT`, `ARCHIVE_OBJECT_STORE_BUCKET`,
`ARCHIVE_OBJECT_STORE_ACCESS_KEY_ID` and `ARCHIVE_OBJECT_STORE_SECRET_ACCESS_KEY`; bootstrap refuses incomplete config
before opening DB connections. See SECURITY.md. `scripts/smoke_archive_r2.py`
performs a real PUT/HEAD/GET only on `treg-archive-dev`, outside CI; it leaves
one tiny test object. It reads environment variables only and prints SKIP with missing variable
names when configuration/credentials are absent. `.env.example` contains blank placeholders. No real smoke runs as part of unit tests. Production values are managed
separately in treg-internal.

## The call→archive link (2026-09-03)

`record()` returns `(key_hash, content_hash)` — computed synchronously and handed to `_store`
so the hash is taken once — and `lookup()` adds `key_hash`, `content_hash` and `version` to its
dict. The call service (`application/call/service.py`) keeps them in `archive_key_hash` /
`archive_content_hash` and the `_audit` closure writes them onto the `CallRecord` (migration
0011: two nullable columns, no index — the read starts from the org-scoped row by id and both
targets are already indexed). `archive.resolve_result(key_hash, content_hash)` walks
row → key → newest snapshot with that content hash → `body_of` carrier, and returns the request
shape (`req_*`, pre-injection) plus the answer; a hash-only version reports `stored: false`.
`GET /calls/{id}/result` (api.py) exposes it to members of the row's org, with a `note` on every
"nothing on file" branch; `/calls` rows carry `has_result`. The archive stays platform-scoped —
what makes the read safe is that the row belongs to the team and names the exact bytes that
team already received. Failure evidence (`error_*`) is untouched and still admin-only.

Rows older than the migration have no link and cannot get an exact one (the key needs the query
and body the audit row never kept); `scripts/backfill_call_archive_links.py` links them best-
effort — same endpoint, same byte size, fetch within ±10 s, unambiguous in both directions —
dry run by default and requires `--apply` to write. Deployment-specific execution belongs in the
operator runbook.

## Result admission

`domain.catalog.results.classify` inspects the already-buffered provider bytes without rewriting
any response. `has_result_rules` enables result-aware behavior only for endpoints with a
verified adapter and a nonempty hit/miss expression. Those endpoints reuse `Adapter.is_miss`.
Strict result validators cover `hunter.companies.emails`, `leadmagic.x.employee-finder`,
`seranking.google.keywords.volume`, `leadsforge.people.email.find`, `hunter.people.email.find`,
and `findymail.search.name`. The last two require a shaped email string inside `data` or `contact`;
an explicit null email is empty, and Findymail also accepts an explicit null contact as empty.
Missing fields, empty strings and malformed addresses are unknown. Leadsforge additionally
requires its successful status as described below. Results are `found`,
`empty`, `error`, or `unknown`, with bounded reason codes. Hunter needs actual email values;
LeadMagic needs person identity fields, not an email address; SE Ranking needs boolean
`is_data_found` and a valid nonnegative volume for found rows. Zero volume is useful data. A
mixed SE Ranking batch is useful if at least one row has data and every row has a valid shape.
Explicit empty arrays/no-data flags are empty. HTTP errors and explicit provider error envelopes
are errors; invalid JSON, predicate failures and unsupported shapes are unknown. Generic adapter
rules retain their existing semantics, including their limitations on missing fields; verification
against a fixture does not imply full response-schema validation.

Endpoints without enabled hit/miss rules retain their original latest-snapshot serving, TTL
learning, and refresh behavior. Their unknown business-result metric does not reject caching.
The mode, serving allowlist, cohort and retention gates remain authoritative for every endpoint.
This policy does not enable cache serving for additional endpoints.

`ArchiveKey.result_state` and `result_snapshot_id` track the last decisive found/empty observation,
separately from the latest historical snapshot. `result_observed_version` identifies the newest
version assessed by this code. Migration `0031` adds nullable columns without rewriting history
or resetting TTL/counters. Legacy keys are classified lazily from their newest body; a version
appended by an older binary during rollout similarly invalidates the saved decision. Lookup
rechecks the candidate's body against current rules and never searches behind an explicit empty.
The pointer is owned by the archive writer and key ownership is checked on reads; snapshots are
never deleted, so no cyclic foreign key is introduced.

For endpoints with enabled hit/miss rules, only found-to-found observations can count stable.
The default compares JSON with sorted object keys and compact whitespace; arrays, types and values
remain significant. Declared `cache.ignore_paths` additionally excludes specific fields. Non-JSON
or unavailable/ambiguous bodies fall back to exact raw-byte hashes.
Found-to-empty counts one change and invalidates serving; repeated empty results neither grow
nor shrink TTL. Empty-to-found counts a change and restores eligibility. Errors and unknowns add
history under the existing capture policy but do not replace decisive evidence or update learning.
A retained positive result still expires at its own timestamp, not the timestamp of a later error.
Cache hits remain reads, never fresh learning evidence. Existing stable/change rollups receive
only these eligible deltas; historical counters remain mixed-policy lifetime totals.

Byte retention and exact-byte dedup remain independent of usefulness. Empty 2xx answers still
link to the caller's history. Non-2xx capture scope is unchanged. Async terminal evidence remains
mandatory history and is never learning evidence. Recording and invalidation still use the
existing bounded, best-effort background writer, so they take effect after that transaction
commits; this is not a synchronous invalidation guarantee.

## The learner (PR 5)

Runs inside the recorder on decisive refetches of a known key, subject to result admission above. AIMD on `ttl_s`: stable ⇒ ×1.5, capped
by min(30 d, the judged `cache.max_age_s`); changed ⇒ ×0.5, floored at 60 s. A key whose first
`_NEVER_AFTER` (4) refetches ALL changed marks itself `ttl_s = TTL_NEVER (-1)` — never served
until a stable refetch resets it. The lookup prefers the learned timer (`ttl_s > 0`) over the
fixed phase-1 guesses.

**Comparison.** Result admission selects the decisive baseline and controls which transitions
train TTL. Identical raw hashes count stable; differing hashes can still count stable when default
JSON normalization, optionally excluding `cache.ignore_paths`, makes the comparison equal. The
legacy
field-noise heuristic remains removed; observation reporting never determines TTL.

## The refresh worker (PR 5)

`archive.refresh_worker` runs in-process from lifespan (adsconv's discipline), gated by
`worker_enabled()` = serve mode AND `archive_refresh_daily_cap > 0`; interval
`archive_refresh_interval_s` (300 s). For endpoints with enabled hit/miss rules, only keys with a confirmed `found` decision can earn
refreshing; unclassified/empty/unknown keys wait for caller observations. Other endpoints keep
the original refresh eligibility. A key EARNS refreshing: window ≥ 80% consumed AND
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
`declared_max_age_s()` supplies the vendor ceiling to defaults, learning and lookup. Serving
caps even an already-learned positive TTL by that declaration, then by caller `X-Treg-Max-Age`.
Without a declared ceiling, learned TTLs can exceed capability defaults; lookup never uses the
static default to cap a positive learned TTL.

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
`{mode, license_quote, source_url, checked}` plus optional `ignore_paths` and `max_age_s` — a vendor-imposed refresh
ceiling the learner (PR 5) must treat as a hard cap. A comparison-only mapping containing
`ignore_paths` need not declare a license mode; it retains the configured default retention policy. `tests/test_archive.py` validates every
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
serve path already emits `X-Treg-Cache: hit`. The call context also carries `cached` from
`served_hit`, so review invitations can exclude archive hits independently of response headers. `archive.record()` is fire-and-forget with
audit's discipline: bounded pending set (512), failures swallowed but logged at **ERROR** (a lost
recording has to clear `FaultCaptureHandler`'s threshold to be reportable at all; degradations that
cost nothing, like a lookup falling back to a live call, stay at WARNING), `drain()` on
shutdown (bootstrap, **before** `analytics.drain()` so a loss during it still gets reported) and in tests. A recorder crash cannot fail a call (tested). `drain()` removes
the tasks it gathered itself rather than waiting on their done callbacks — audit's exact drain
discipline; the busy-spin both avoid (the 2026-08 serial-Postgres CI hang) is explained and pinned
for both modules in `tests/test_audit.py`.

**Which pool.** Every write here — `_store_locked`, `_touch_write`, `prune_once`, `refresh_once` —
uses `background_session_maker`. `lookup` is the one exception and uses the API pool on purpose: it
runs INSIDE a caller's `/call/`, so it must not queue behind this module's own writes.
`tests/test_db_pool_isolation.py` pins that split per function; see also Recorder throttle below,
which bounds concurrency inside the pool.

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
enable. Rollback is an environment-setting change through the deployment's normal configuration
process.

## Conservative comparison and controlled serving (2026-09-08)

The comparison setting and helper are removed. Old `TREG_ARCHIVE_COMPARISON_MODE` environment
values are ignored, including `legacy_noise`. Events and admin props now report `json`: object
order and whitespace are ignored by default. Endpoint declarations can further relax comparison
using `cache.ignore_paths`; `archive_change_observed.masked_by_ignore` reports byte changes
rescued by either normalization or declared field exclusions. An empty changed-path list with
this flag indicates representation-only changes.

TTL learning and lookup retain the original behavior: stable observations grow the timer by
1.5, changed observations halve it, and TTL_NEVER remains respected. The fixed capability timer
is only the initial/fallback value. Switching comparison mode does not reset existing timers,
volatile paths or cumulative counters; new observations continue updating the existing state.
There is no separate fixed-TTL mode or learning-version migration. Old stable/changed counters
remain lifetime mixed-policy statistics, not a clean measurement of strict comparison.
`/admin/archive` exposes that caveat plus comparison mode, adaptive TTL policy, endpoint
allowlist and rollout percentage.

Serving now requires all three: `TREG_ARCHIVE_MODE=serve`, an exact endpoint ID in comma-separated
`TREG_ARCHIVE_SERVE_ENDPOINTS` (default empty), and admission by `TREG_ARCHIVE_SERVE_PERCENT`
(default 0). `rollout_reason` hashes the team ID plus endpoint into stable 0-99 buckets; increasing
the percentage preserves existing treatment teams. Out-of-range percentages disable serving.
Empty caller cohort also fails closed. These gates run before opening a lookup DB session.
Recording is independent, so unselected teams and endpoints continue live calls and observations.
A cohort is an experiment assignment, not an authorization boundary or a cache-key fix.
Only review endpoints whose request headers, account context and reuse rights fit the existing
legacy key before allowlisting; key v2 is still future work.

`TREG_ARCHIVE_REFRESH_DAILY_CAP` now defaults 0 so experiments do not accidentally trigger
platform spend. Explicitly enabled refresh requires a nonempty serving allowlist and valid
positive rollout percentage; its candidate query is restricted to allowlisted endpoints and
uses the same effective TTL policy. Turn it off during initial measurement.

The existing `tool_called` PostHog event carries `cache_outcome`, `cache_mode`,
`cache_comparison_mode`, `cache_ttl_policy`, `cache_rollout_percent`, and, when attempted,
`cache_lookup_ms`. Snapshot lookups additionally expose `cache_age_s` and `cache_window_s`.
Outcomes distinguish hit, key_missing, stale, snapshot_unavailable, body_missing, ttl_disabled,
policy_excluded, caller_bypass, endpoint_disabled, rollout_disabled, missing_cohort, control,
lookup_error, result_empty, result_error, result_unknown and not_attempted (or mode_disabled for
direct disabled lookups). Buffered call telemetry also includes `result_state`, `result_reason`,
`cache_result_policy` (`hit_miss` or `legacy`), and `cache_admission` (`eligible`, `empty`,
`error`, `unknown`, or `not_applicable` for legacy). Admission describes the result gate, not
persistence success or permission to serve. Business `hit` is true/false/null using the same
classifier; endpoints without enabled rules remain null.
`cached` and `cache_outcome=hit` describe cache reuse, a different fact. A rejected stored body
appears in `cache_outcome`; the final live answer appears in `result_state`.
No request key, body, ignored field paths or headers are added to analytics. The existing
bounded, lossy analytics sink is reused; no new per-hit DB row or extra network request is added.
`cache_lookup_ms` includes gate/DB/decompression time, not just SQL; `duration_ms` on the parent
event remains the end-to-end call timing. Early refusals before the capture funnel can lack
cache fields; missing fields are not misses. Shadow still only records/learns: it does not
produce hypothetical hit counts or fresh-answer comparisons.


## Eligibility — three gates, in order

1. **Kind.** `kind: action` entries are never stored; only data reads pass.
2. **License.** Per catalog entry: `cache: forbidden | transient | archive` — either a bare
   string or a provenance dict `{mode, license_quote, source_url, checked}`, exactly like `cost`
   provenance. **Absent ⇒ `archive_default_policy`**, which is `transient` since the founder's
   keep-all decision (2026-08-29): unjudged providers' bodies ARE kept as short-lived cache, and
   the env flips it back to `forbidden` without a deploy. A JUDGED forbidden (a licence that was
   read and says no — Finnhub) is always respected, and a missing entry is never stored.
3. **Tier.** Only fully buffered METERED PLATFORM calls are recorded. Those responses are already fully buffered
   for the settle (`_buffer_response` needs the provider's reported cost), so recording adds no
   latency and no new data path. Own-key and own-tool calls stream and are never touched — that
   is the privacy line, enforced at write time, not filtered at read time.

Gates 1+2 are `archive.policy(entry)`; gate 3 is the hook site's own context.

Successful free final fetches that qualify for `MarketplaceCall.streamable_free_result` bypass both
lookup and recording even though their zero-amount money lifecycle remains metered. They have no
buffered body, so no empty or partial body/hash is recorded. Other calls exceeding the settlement
buffer's 8 MiB limit fail before recording and cannot populate a cache or idempotent success.

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
`last_changed_at`), legacy `volatile_paths` (retained for schema compatibility, no longer read or updated), and demand (`heat`, `last_requested_at`). Platform-scoped, no `org_id`:
one team's fetch may warm another team's hit, and own-key traffic never enters.

`ArchiveSnapshot` — one version: unique `(key_id, version)`, verbatim `body` bytes, `content_hash`
(raw sha256) for dedup — an identical consecutive answer stores a version row with `body=NULL,
body_of=<carrier row>` instead of the bytes again. Bodies live in Postgres (the `IdempotentCall`
precedent); oversized bodies are skipped by the recorder, never truncated. `origin` says who
fetched: `caller` | `refresh` | `sample`.

## What the archive must never touch

Money. A cached hit will only TAG existing records "cached" — billing of a cached hit is an
explicitly deferred founder decision, and no archive code imports ledger/billing. Relay
faithfulness also extends through time: served bytes are exactly what the vendor sent.

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

## The running totals (2026-09-03)

The panel's report reads `ArchiveEndpointStat` — one row per endpoint (keys, stable, changed,
snapshots, bodies_kept, kept_bytes, newest_fetch), maintained by the recorder in the SAME
transaction as each snapshot, using atomic column arithmetic only (the credit-block drift is why
read-modify-write is banned near counters). The old per-load aggregates cost 9.9s + 8.0s + 14.8s
at 434k keys on prod; the rollup read is milliseconds at any scale. Migration 0013 backfills the
table once from the live archive and adds a partial index over refresh-origin snapshots for the
"refreshes today" count; a 30s in-process report cache remains as the belt against poll stacking
(cleared per test beside the drains). A dropped recording drops its counter bumps with it — the
rollup rides the recording's own commit, so the two cannot drift.

## Body compression (2026-09-03)

Stored bodies compress with zlib level 6 when it shrinks them (`enc` column: NULL = raw, "zlib");
bodies under 256 bytes stay raw. Measured on 40 real prod bodies: 5.2x, 68 MB/s in, ~1 GB/s out —
the recorder writes under 1 MB/s, so the cost is invisible and ~6.5 GB/day becomes ~1.25 GB/day.
Every DB body reader unpacks (serve lookup, the dedup-carrier follow, the
call-result reader, the panel's body viewer), so the caller always receives the exact original
bytes; `size_bytes` and `content_hash` describe RAW bytes always, keeping dedup and statistics
semantics unmoved. Rows from before migration 0014 stay raw and readable forever. The founder's
keys-vs-values idea was examined and declined: compression removes repeated JSON keys anyway, and
rebuilding JSON from stored values risks the byte-verbatim promise the hashes depend on.

## The pruner (2026-09-03)

Profit-shaped shelf clearing: a served hit is revenue with no vendor cost, so a body's right to
disk is its earning potential. `prune_once` strips BYTES only — every version row keeps its hash,
size and timestamp, the newest version of every key stays whole (serving and change-detection
need it), the decisive snapshot and its carrier remain protected even after later errors, and the strip set is decided before carrier protection so a surviving dedup reference
never loses its carrier. Rank of removal: never-servable keys (TTL_NEVER) keep exactly one body,
age no defense; then old (`archive_prune_min_age_days`, 7) versions beyond the newest
`archive_prune_keep_versions` (2) on keys undemanded for 14 days. Archive-policy endpoints are
exempt — their history is the future data product. Runs from the lifespan beside the refresh
worker whenever the archive records, `archive_prune_batch` (500) bodies per pass per
`archive_prune_interval_s` (3600); batch 0 disables. Rollup counters (bodies_kept, kept_bytes)
decrease atomically only when stripping the last retained copy. Stripped `both` rows become
`r2`, so their logical retained counts do not change. DB carriers of surviving versions stay
protected. No R2 delete or prune operation exists.

## Recorder throttle (2026-09-03, memory-bounded 2026-09-07)

At most `_MAX_CONCURRENT_WRITES` (2) recordings touch the database at once — audit's exact
loop-bound-semaphore pattern (four until 2026-09-07; every slot is paid per uvicorn worker and
again per rolling-deploy instance, and a recording is one INSERT of a body already in memory).
Before it, a burst could put up to 512 concurrent short sessions in front of the API's 15-slot pool
(SToneX's pool-pressure report); those writes now land on the BACKGROUND pool instead
(`ops/deploy.md` § Database pools), so the semaphore is the inner bound rather than the only one.
Queued recordings wait inside their fire-and-forget task, so the caller is unaffected; the 30s
bound covers wait+write, so a stuck queue still sheds rather than wedges. Throttled, not shed: the
burst test proves all 12 concurrent recordings land while peak DB concurrency stays ≤2.

**Memory bound.** Each pending task holds its `body` bytes in a closure, so task count alone is not a
sufficient memory bound. `_MAX_PENDING_BYTES` (256 MiB) caps body bytes in DB
pending work: `record()` sheds when EITHER the task count OR the bytes threshold is exceeded. The
done callback releases bytes when a task completes, keeping the budget accurate. The independent
R2 queue adds 128 MiB by default, for a combined 384 MiB body budget before SDK, compression
and terminal-evidence overhead. Production incident evidence and deployment sizing are maintained in
the private operator runbook.

The semaphore is process-local; the recorder also supports deployment with multiple processes. An exact in-process key
lock is acquired before the semaphore, so duplicate recordings queue without consuming both
database-write slots and unrelated keys keep moving; weak references discard inactive locks. Once
admitted, the writer locks and refreshes the matching `ArchiveKey` row before reading the newest
snapshot and allocating version N+1. The refresh matters because the earlier unlocked lookup
already populated SQLAlchemy's identity map. The per-key Postgres row lock serializes cross-process
writers; SQLite is protected in-process by the key lock. Snapshot insertion is explicitly flushed,
and version conflicts propagate to the bounded outer retry to cover first-key and multi-process
SQLite races.

## Keys-endpoint weight fix (2026-09-03, the pool-pressure repeat)

`/admin/archive/keys` ran one whole-row query per key, dragging every stored BODY out of the
database just to report whether one exists — and the panel fired it once per endpoint row
(limit=100) to fill TTL cells: ~50 near-simultaneous heavy calls per page open. Now: the
versions come from ONE columns-only query (`body IS NOT NULL` reads the header, never the
bytes), and the panel fills a TTL cell only for the endpoint the operator clicks, from the
inspector's own load. A dash in the TTL column means "click to learn".

Startup uses the normalized archive mode (unknown values disable it). R2-only writes require
all three reads to be `r2-first`; default, EU and FedRAMP R2 endpoints are accepted. The dev smoke
script has no bucket argument and accepts only `treg-archive-dev`.

ObjectStore owns the sole download hash validation; the memory fake follows the same contract.
`put` accepts the internally computed content hash to avoid rehashing immutable bytes. Read and
write errors use the same classification. `R2ObjectStore._failure` preserves typed auth/not-found/
timeout reasons. obstore 0.11.1 exposes HTTP 429/5xx as `GenericError` without a status attribute;
only that SDK type is inspected for its anchored transport-status prefix. Bare numbers and status
text inside a response body do not qualify. No SDK text is logged or emitted; `ObjectStoreError`
carries only a bounded reason and the underlying exception class. The real-wheel loopback test
`test_real_obstore_http_status_classification` covers PUT/GET/HEAD and proves zero SDK retries.

Pruning still strips eligible DB bytes during double writing. A stripped `both` row becomes
`r2`; its content hash and object remain intact, and logical retained-body statistics do not
decrease. Existing deduplicated DB carriers and result baselines retain their protections.
The admin DB body viewer identifies object-stored bodies without fetching them. Retired
`volatile_paths` remains in the schema but is no longer displayed.

Known result-admission upgrade limit: when a historical R2-only row has no current
`result_state`/observed-version metadata, the write path does not GET its old body to classify
it. The baseline becomes unknown; the next decisive result establishes a new baseline without
a stability comparison. Subsequent observations learn normally. This conservative loss of one
learning interval avoids object I/O inside a write session or an extra speculative GET per write.

`WritePlan` is the single body-retention decision passed into the DB writer. DB retention is
inferred from its storage location; failed eligible R2-only uploads become `db` plans. Each started
recording emits its completion report from one `finally` block, while queue callbacks release
budgets and report cancellation of tasks that never started. `tool_called` remains independent.
The object-store lifespan chooses a real or injected context once and always resets the seam.


## Same-hash upload deduplication

`archive_bodies.prepare` keeps a process-local LRU of 20,000 successfully uploaded content hashes.
An LRU hit returns a successful `WritePlan` with `upload_status=skipped_duplicate`, zero new
transfer time and no PUT/HEAD. Only a completed PUT with matching `ObjectInfo` enters the LRU.
Failures and cancellations never do. `configure` resets the cache when the store changes; restart
or LRU eviction permits a later idempotent re-upload. A GET that finds an object missing or corrupt
invalidates its recent-success entry so the existing live-refetch path can repair it.

For a new hash, `prepare` registers one in-flight future before waiting for the upload semaphore.
Followers await its result with `asyncio.shield`, holding no semaphore slot or DB connection, and
report `upload_status=coalesced` on success. A cancelled follower cannot cancel its leader. A
cancelled leader releases followers to normal DB fallback and removes the in-flight entry. All
followers share the failure reason; none publishes an R2 pointer for a failed flight. In-flight
entries are removed on every exit and stay within the existing admitted recording work.

`submit` checks recent, queued and in-flight hashes before the R2 count/byte admission gates.
A queued hash is registered synchronously, so even a burst before background tasks start consumes
only one R2 pending entry and one body-sized byte allocation. Duplicate recordings instead use
archive's existing bounded DB queue; `prepare` still joins/skips the PUT before any DB session.
Every admitted call keeps its own history/statistics write as before. Duplicates never turn into
unbounded work: the DB queue's 512-record/256 MiB limits still apply. Distinct-body bursts can
still exhaust R2's unchanged 256-record/128 MiB budget; increasing those settings is not part of
this fix. `duplicate_queue_bypass`, `skipped_duplicate` and `coalesced` process counters supplement
the existing per-record `archive_body_stored.upload_status` values.

Residual `rate_limited`/`upstream_error` failures get one retry on nonterminal uploads too, with
1.0-1.5 seconds of jitter before retry. This clears the one-write-per-second same-key window and
shares the existing transfer deadline rather than restarting it. Exhaustion keeps `storage=db`
in `both` mode with `upload_status=failed` and the classified `drop_reason`; the DB copy is not
reported as dropped. R2-only mode now uses the same DB fallback for eligible bytes. No SDK retries
are enabled, no extra DB transaction or new column/table is added, and no production setting is
changed.

### Timing and queue interpretation

Before this fix, `upload_ms` already started after acquiring the upload semaphore and ended after
`ObjectStore.put` returned/raised. Nonterminal recordings made one attempt. Semaphore wait was
already isolated in `queue_wait_ms`; a four-second failed nonterminal `upload_ms` was therefore
four seconds in the SDK/transport operation, not four seconds waiting for a local slot. The
locked obstore 0.11.1 with `max_retries=0` issues exactly one HTTP request on a 429 in the loopback
regression; its configured SDK retry count remains zero. Neither fact identifies which part of a
production network/server operation caused the delay.

After this fix, leader `upload_ms` includes all attempts and jitter under its transfer budget;
followers record their shared-flight wait in `queue_wait_ms` and zero transfer time. LRU hits have
zero transfer/wait time. Do not interpret these post-fix per-record values as one SDK request's
latency, or average duplicate statuses and zero-transfer failed waiters into physical PUT latency. R2 pending accounting spans the
recording's DB completion too; it is not just the number of active PUTs. Compare existing failure/
drop reasons, leader timing, and duplicate statuses after rollout before increasing queue budgets.


## Change observation

After `_store_locked` commits, `_observe_change` reports a byte-hash change from the baseline
that actually drove TTL learning for `caller` and `refresh` origins only. Cache hits and async terminal evidence
never emit `archive_change_observed`. The background task has a separate three-second observation
budget after the existing 30-second DB stage. Failure cannot undo or prevent the recording.
`_read_change_body` collects an `archive_bodies.pointer` in a short background session, closes it,
then calls `archive_bodies.read`. Observation follows `TREG_ARCHIVE_BODY_READ_LOOKUP`: default
`db` makes no R2 requests; `r2-first` uses verified GET and falls back exclusively to the background
pool. It cannot enable R2 independently of the existing startup checks or rollback switches.
`TREG_ARCHIVE_CHANGE_OBSERVATION_ENABLED=false` disables change reporting and its reads/CPU work
(default true). Declared ignore-path TTL comparison remains a separate decision mechanism.
Both ignore comparison and change reporting share the recorder/touch semaphore budget of two,
including fallback sessions and CPU work. No DB connection is held during object I/O. New bodies
not retained by policy/size/storage and known hash-only previous snapshots skip observation.
Process-local `change_outcomes` counts `observed`, `body_unavailable`, `observation_failed`,
`ignore_body_unavailable` and `ignore_comparison_failed`. `/admin/archive` exposes this mapping and
`body_outcomes` (archive_bodies.outcomes), including on cached report responses; counters reset on
restart and are per process, not durable or fleet-wide totals.

`_change_summary` runs off the event loop. Structure comparison visits each subtree once without
repeated JSON serialization, retaining type-sensitive comparisons. Cancellation keeps the semaphore
slot occupied until the bounded-size CPU job finishes; asyncio timeout does not stop a Python thread. JSON differences use dot paths, collapse array indices
to `[*]`, stop at six levels and retain the first 20 sorted distinct paths. `path_count` counts all
distinct paths before truncation; `truncated` marks more than 20. Added/removed fields, array length
and type changes count; a changed container at the depth boundary counts at its boundary path.
Root changes use `$`. `leaf_count` counts new-body leaves at the same depth boundary (empty
containers count as one). Byte-only whitespace/key-order changes can have zero changed paths.
Non-JSON pairs use `changed_paths: [non_json]`, `path_count: 1`, `leaf_count: 0`.
`sole_path` is the sole path when count is one, otherwise null.

Analytics emits `archive_change_observed` with distinct ID `archive` and only `endpoint_id`,
`provider`, `changed_paths`, `path_count`, `truncated`, `leaf_count`, `sole_path` and
`masked_by_ignore` (true only when a declared ignore comparison actually rescues a stable TTL
decision). The path report uses that same TTL baseline, including an older decisive snapshot
across intervening unknown/error observations for result-aware endpoints. No values, body
snippets, call references or key identities are sent. Paths are structural property names from JSON;
these reports are not a schema or evidence that a field is safe to ignore. Observation is read-only
and does not alter admission, learning, stored bytes, deduplication or serving.

### Seven-day HogQL review

Paste into the PostHog SQL editor. One row per endpoint/path; all ratios use that endpoint's
observed byte changes as denominator. `sole_change_ratio` counts events where that path alone
changed; `endpoint_sole_ratio` counts any sole-path change. Empty path lists remain in the total.
Truncation makes per-path ratios lower bounds, so inspect `truncated_ratio` before choosing a
list. Missing bodies and dropped analytics are not in these denominators. `non_json` is a marker,
not an ignore candidate. SQL uses PostHog's supported [JSON/array functions](https://posthog.com/docs/sql/clickhouse-functions).

```sql
WITH observed AS (
    SELECT properties.endpoint_id AS endpoint_id,
           properties.sole_path AS sole_path,
           toInt(properties.path_count) AS path_count,
           properties.truncated = true AS truncated,
           JSONExtract(ifNull(toString(properties.changed_paths), '[]'), 'Array(String)') AS paths
    FROM events
    WHERE event = 'archive_change_observed'
      AND timestamp >= now() - INTERVAL 7 DAY
), totals AS (
    SELECT endpoint_id, count() AS changes,
           countIf(path_count = 1) / count() AS endpoint_sole_ratio,
           countIf(truncated) / count() AS truncated_ratio
    FROM observed GROUP BY endpoint_id
), per_path AS (
    SELECT endpoint_id, path, count() AS path_changes,
           countIf(sole_path = path) AS sole_changes
    FROM (SELECT endpoint_id, sole_path, arrayJoin(paths) AS path FROM observed)
    GROUP BY endpoint_id, path
)
SELECT totals.endpoint_id, totals.changes, per_path.path,
       per_path.path_changes / totals.changes AS path_ratio,
       per_path.sole_changes / totals.changes AS sole_change_ratio,
       totals.endpoint_sole_ratio, totals.truncated_ratio
FROM totals LEFT JOIN per_path ON totals.endpoint_id = per_path.endpoint_id
ORDER BY totals.changes DESC, path_ratio DESC, per_path.path
```


## JSON comparison and declared ignore paths

Every retained caller/refresh response uses `_ignored_matches`, even with no `cache.ignore_paths`.
`_normalized_hash` parses a private JSON copy, deletes only declared paths, then hashes JSON with
sorted object keys and compact separators. Array order, length, types and unignored values remain
significant. Duplicate object keys, numbers whose parsing would lose precision, invalid JSON and
unavailable bodies fall back to raw byte hashes. The original bytes, content hash, deduplication,
history and served response never change.

`cache.ignore_paths` is optional and empty by default; only explicit declarations exclude fields.
Hunter declares `data.verification.date` for `hunter.people.email.find` and
`data.emails[*].verification.date` for `hunter.companies.emails`. Date-only changes no longer shrink
TTL, but mailbox values, scores, verification statuses and found/empty transitions remain significant.
Other pilot endpoints declare no ignored fields, including Findymail's `contact.id`.
A comparison-only mapping does not override retention policy or assert vendor licensing permission.
There is no automatic field selection; `ArchiveKey.volatile_paths` remains unused.

Path segments can begin with digits, and `[*]` selects array elements. Missing paths are no-ops.
Deleting `items[*].request_id` keeps all elements; deleting `items[*]` deletes the array contents.
Comparison has no six-level reporting limit and never uses reported/truncated paths as policy.

`_ignored_matches` preloads at most the latest and decisive snapshot bodies before the DB write.
It checks the key and raw hashes first, skipping new-response JSON normalization unless a differing
candidate has a readable body pointer. New keys and raw-identical baselines therefore need no
normalization; identical raw hashes also skip body reads. Pointer sessions close before JSON
normalization or object I/O. The pre-read uses the shared archive semaphore and a three-second
budget, separate from the write deadline.
Only matched snapshot IDs are passed to `_store_locked`; a concurrently changed baseline falls
back to raw hashes without holding a row lock across I/O or adding a reconciliation write.
`ignore_body_unavailable` and `ignore_comparison_failed` retain their diagnostic names for both
default JSON comparison and explicit field exclusions. Reporting can be disabled independently;
TTL comparison remains active.

`changed_paths` and `masked_by_ignore` describe the baseline actually used for learning. The latter
now covers default normalization as well as declared paths. Result-aware endpoints use their
last decisive found/empty snapshot across unknown/error responses; legacy endpoints use the latest.
Repeated empty results do not learn or emit change events. Ignore rules never relax admission,
mask found/empty transitions or alter cache billing. Existing TTLs/counters are not backfilled by
deployment; production operator caps bound served age independently of historical learned TTL.


## Leadsforge email cache pilot

`leadsforge.people.email.find` uses strict result admission: a `succeeded` response needs a
nonempty email string with a basic mailbox/domain shape; `not_found` without an email is empty.
Other status/field combinations are unknown and cannot serve. This is result validation, not
mailbox deliverability verification. Historical bytes remain unchanged and are reclassified on lookup.

`TREG_ARCHIVE_SERVE_MAX_AGE_S` is a JSON mapping of exact endpoint IDs to positive integer
seconds (empty by default). It is an operator freshness ceiling, independent of vendor declarations.
Lookup takes the minimum of the learned TTL, vendor ceiling, operator ceiling and caller max-age;
`TTL_NEVER` remains authoritative. The refresh worker also respects the operator ceiling for its
80% due threshold. It does not reset or rewrite historical learning counters. `/admin/archive`
reports `serve_max_age_s` so operators can verify the running configuration. The global team cohort
percentage is unchanged. Production pilot values and rollback live in treg-internal.
