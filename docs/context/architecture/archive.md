---
title: Archive - versioned history and cache admission
status: building
sources:
  - src/treg/archive.py
  - src/treg/catalog/hunter.yaml
  - src/treg/domain/catalog/results.py
  - src/treg/alembic/versions/0031_archive_result_admission.py
  - tests/test_cache_result_admission.py
  - tests/test_archive_batch.py
  - tests/test_archive_batch_postgres.py
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
  - src/treg/application/call/settle.py
  - src/treg/alembic/versions/0039_archive_own_key_and_repeat_pricing.py
  - scripts/backfill_call_archive_links.py
  - src/treg/api.py
  - src/treg/bootstrap.py
  - src/treg/routers/admin.py
  - src/treg/application/asynctasks.py
  - src/treg/application/archive_erasure.py
  - src/treg/alembic/versions/0067_org_archive_opt_out.py
  - tests/test_archive_opt_out.py
related:
  - architecture/data-model.md
  - architecture/proxy-model.md
  - architecture/catalog.md
---

# The archive

Two concepts, one word each: **cache** is the last confirmed useful answer for a key, served
instead of a vendor call while it is fresh and no later decisive observation invalidates it;
**archive** is every version of every answer, kept with its timestamp. The cache is the archive's
top layer. History is kept on purpose (per-key time series are a future data product), so do not
"clean it up" as waste. History endpoints over it do not exist yet; a team can read only the
answer its own call got (see "The call-archive link").

Relay faithfulness extends through time: served bytes are exactly what the vendor sent, and
`size_bytes` / `content_hash` always describe the raw bytes.

## The mode switch and serving gates

`TREG_ARCHIVE_MODE` (`archive.mode()`): `off` (default) | `shadow` (record and learn, serve
nothing) | `serve` (shadow plus fresh eligible hits). Any unrecognized value degrades to `off`: a
typo must disable, never enable.

In serve mode two more gates run before any lookup DB session opens (`rollout_reason`):

- `TREG_ARCHIVE_SERVE_ENDPOINTS`, default `*`. Comma-separated exact endpoint ids,
  `capability:<prefix>` families (matched against the entry's capability) and `*`. Empty serves
  nothing: the rollback lever.
- `TREG_ARCHIVE_SERVE_PERCENT`, default `100`. A sha256 of endpoint plus team buckets teams into
  0-99, so raising the percentage keeps existing treatment teams. Out of range, or a caller with
  no cohort, fails closed. A cohort is an experiment assignment, not an authorization boundary.

Recording is independent of both gates. `TREG_ARCHIVE_SERVE_MAX_AGE_S` is a JSON map of exact
endpoint ids to an operator freshness ceiling, and `TREG_ARCHIVE_HIT_REPEAT_PRICE_PERCENT`
(default 10) the repeat price. A hub tool call (`<team-slug>.<name>`) returns through
`hub_runner` before reaching any of this ([hub](hub.md)).

## Eligibility - three gates, in order

1. **Kind.** Only a `data` read is stored (`cacheable_kind`). `action` changes the world;
   `utility` is a task-status poll or model list, where a stored "running" breaks every poller;
   `account` answers about the caller's own account and is stale the moment it is spent.
2. **Licence.** `cache: forbidden | transient | archive`, a bare string or a provenance dict
   `{mode, license_quote, source_url, checked}` (plus optional `ignore_paths`, `max_age_s`),
   written once on the provider YAML header and overridable per endpoint. Absent takes
   `archive_default_policy` (`transient`, flippable to `forbidden` by env). A judged `forbidden`
   is always respected and a missing entry is never stored. A judged entry must carry its quote,
   source and date (`tests/test_archive.py`).
3. **Body in hand.** A metered answer is already buffered for the settle, so recording adds no
   latency. An own-key catalog answer is read whole only when it fits the size cap ("Own-key
   answers"). Own-tool calls (no catalog entry) are never touched.

Gates 1 and 2 are `archive.policy(entry)`. Also never recorded or served:

- a 2xx the capacity signature table reads as treg's own account running dry;
- a submission (`_unfinished_submission`): on a row that creates an owned resource or is async,
  anything but a finished answer. A job id replayed to another team would sell it someone else's
  resource. The serve path skips such rows (`_only_submits`) unless `terminal_on_submission`;
- a body failing the endpoint's `expect` rule (lookup reports `result_error` for one already
  stored);
- free final fetches that stream (`streamable_free_result`) and `spooled_response` answers: no
  buffered body, so no empty or partial hash is recorded. A body over the 8 MiB settlement buffer
  fails before recording.

Image/video entries carry effective `cache: forbidden`, and lookup and refresh re-check the
current policy, so old stored responses are never replayed. Async terminal evidence is the
deliberate exception: the winning finalizer stores the terminal provider JSON under
`treg://asynctasks/<call_id>` independent of replay policy, as settlement evidence. It is never
cache or learning evidence, the worker never follows result URLs in it and never stores generated
media bytes. `load_terminal_responses` reads it back for Activity.

## The cache key

`archive.cache_key(method, endpoint_id, upstream_url, body, headers, scope="")` is a sha256 over
the uppercased method, the catalog endpoint id, sorted query pairs, the canonical-JSON body hash
(raw hash for non-JSON), only `Accept` and `Accept-Language` from the caller, and the sharing
scope when the answer is not public. The URL is rebuilt as the vendor sees it, before credential
injection, so a credential cannot enter the key or the stored request shape from the request
side. A vendor that echoes the credential in a 2xx body would have it stored on a platform-key
call; judge `cache` per provider with that in mind.

## Own-key answers

An own-key catalog call (tier `tool`/`credential`) is looked up before the relay and recorded
like a metered one. After the relay its 2xx is read whole only when it fits
`archive_max_body_bytes` (`settle._read_whole_if_small`, bounded, before headers go out); one
that does not fit, or declares a larger `Content-Length`, streams untouched and is not recorded,
never as a prefix. The relay asks for identity encoding (`force_identity`) and a vendor that
compresses anyway is not recorded (`_identity_encoded`). A caller body the path never read cannot
key the question: no lookup, no recording. **An answer that quotes the team's own credential back
is never recorded** (`_echoes_own_credential`, failing closed when the renderings cannot be
built). The same guard, provenance and sharing apply to a metered call riding treg's
pay-per-use OAuth app (`billed_oauth`): the token is the team's.

**A hit on an own key is free**: nothing is reserved or settled (non-negotiable 1), and
`cache_price` reports `free`.

### Sharing: whose question is it

The licence says whether bytes may be kept; `archive.sharing(entry, own_credential=...)` says
whose question the answer is. "The vendor allows storage" never implies "the answer does not
depend on who asked".

| Who asked | `scope: any_account` | `scope: own_account` |
|---|---|---|
| treg's platform key | `public` | (never) |
| the org's own credential (API key or OAuth token, metered or not) | `org`, or `public` only where the endpoint declares `cache.sharing: public` | `connection` |

Sharing is enforced by the key, not by a read-time filter: `scope_tags` folds `org:<id>` or
`conn:<id>:<sorted bound secret ids>` into `cache_key`, so a private history lives under a hash
nobody else computes and its timer learns only from its own answers (a reconnect is a new secret,
hence a fresh history). A caller tries its scopes most specific first (connection only; org then
public; public only) and records under the first. `ArchiveKey.scope` (`org` | `conn` | NULL =
public) lets the refresh worker skip private keys, which treg's platform key cannot re-ask.

`cache.sharing: public` is an endpoint declaration only: `_validate_cache` refuses it on a
provider header, on an `own_account` endpoint, and any value but `public`. It is never inherited
from a provider's licence.

### Opting out: a team that wants nothing stored

A team admin opts out with `PATCH /orgs/{id}/settings {"archive": false}`. The setting is
`Org.archive_opt_out_at`, a timestamp rather than a flag because the moment the team objected is
the processing record a data-protection request asks for. It rides the call runtime's
`OrgSnapshot`, so the gate costs no query.

The gate is a full bypass on every tier: no lookup and no recording, so no key hash reaches the
settle and no `ArchiveKeyOrg` mark is written. The team is not served public answers either: a
hit touches demand and marks the team against the question, and the promise is "we do not record
what you asked". `cache_outcome` reads `org_opt_out`. A call in flight at the switch may still
land a recording; erasure removes the rest.

**Erasure is a separate act, never a side effect of the switch.** Team deletion and
`treg-worker admin erase-archive --org <id>` run `application/archive_erasure.erase_org`. Both
refuse a team still in the archive: with the gate shut nothing new is recorded while rows go, so
there is no sweep, pending state or recorder coordination. What is "the team's" is decided in
governance (`teams.private_archive_key_ids`, `teams.erase_archive_rows`): every key under the
team's `org:`/`conn:` scopes with its snapshots and request shape, plus every `ArchiveKeyOrg`
mark. Platform-key answers are public questions and stay by default. Once the marks go, the
archive no longer names the team; `callrecord` still keeps the team's `params_hash` per call.

On request (`erase-archive --include-sole-public`), erasure also takes the public keys only this
team paid for (`teams.sole_public_archive_key_ids`: marked by it and by no other team). A question
another team also paid for stays. These keys are found through the team's marks, so this phase
runs first, before the private phase deletes the marks. It reads every candidate before anything
is deleted, a page of marks (`teams.archive_mark_hashes`, by `key_hash` from a cursor) per capped
statement, so the read fits the cap however many questions the team paid for. An erasure of the
same team running alongside (an owner delete, a second worker) can delete the marks at any moment,
so the mark count is read before and after; fewer after means pages were read against vanishing
marks, and the job raises before deleting anything. Each batch then re-checks only that no other
team has paid for the key since; it does not require the team's own mark. A plain erasure that
finishes before the public phase starts still leaves nothing for it to find, so the run order
matters. Candidates are held in memory. It deletes keys and snapshots through
`teams.erase_archive_keys`, which leaves marks alone. Unlike a private key, a public key can gain
a snapshot from another team's call between the orphan judgment and the delete. That snapshot
goes with the key, and its object, if new, is left unreferenced in the store. The cost of the
phase is that other teams lose future hits on those questions; their next call goes live and
re-records.

Order matters:

1. Objects before rows, in key batches, in short transactions with no connection held across
   object I/O (non-negotiable 3).
2. `teams.archive_bodies_only_under` judges in one anti-join which content hashes no other key's
   snapshot shares (bodies are content-addressed and shared across keys); those objects are
   deleted and dropped from the in-process upload cache (`archive_bodies.forget`).
3. Only then the rows. A failed object delete stops the pass before the rows, so a retry starts
   from them; deleting a row first would orphan its object for good.

Passes walk the team's keys by id from a cursor, and every statement in them runs under a
Postgres `statement_timeout`. One long transaction holds back the cleanup of old row versions in
every table, the hot `org` balance row included; a pass that runs over the cap fails the job
instead, and what earlier passes committed stays erased. That is why `body_of` is not a foreign
key (alembic 0094): with no index on it, Postgres checked each deleted snapshot with a scan of
the whole table, and no batch fit the cap. `erase-archive --dry-run`
(`archive_erasure.count_org`, with the same `--include-sole-public` switch) reads the same
passes and deletes nothing. It reports keys, snapshots, marks and distinct bodies; the bodies
count is an upper bound, because a shared body stays.

Endpoint running totals subtract what each DELETE returned, so two erasers subtract once, and
move in endpoint order, the order `archive.flush_stats` locks the same rows in, so the two cannot
deadlock. The worker prints the running totals to stderr after every committed batch. Because
another process's upload cache cannot be told, `archive_bodies.prepare` trusts a remembered
upload only after one `head` confirms the object still exists. Accepted race (documented in the
module): another team records identical bytes between the orphan judgment and the delete; its
snapshot then reads as "bytes not on file", which every reader treats as a miss.

Team deletion sets and commits the opt-out, calls `erase_org` after its request session has let go
of its connection and before the cascade (a failure keeps the team, opted out, for a retry), then
cascades. `cascade_delete_org` deletes the archive rows itself (the archive keys have no FK to the
team, so the FK-walking guard test cannot see them), which keeps every other deletion path from
leaving rows behind, at the price of objects those paths do not clear. Out of scope on purpose:
async terminal evidence (public key, no team provenance), the `callrecord` audit trail and the
idempotency replay store.

## Pricing a hit

A metered hit goes through the same reserve and settle as a live call: the hold is reserved
before the lookup and settled once, whatever answered. Only the amount differs, per team and per
question: a team's first billed call on a question pays full price whether the vendor or the
archive answered (the archive saved treg a vendor call, not the team its first price); from its
second call on, a hit settles at `archive_hit_repeat_price_percent` of the live price. A forced
live call (`Cache-Control: no-cache`) is billed and counts.

The evidence is `ArchiveKeyOrg`, one row per (org, key_hash), written by
`archive.note_org_use(s)_in_transaction` inside `_platform_settle` (the allowlisted write
`archive_org_use_in_settle`), so the mark lands with the charge or not at all. It is an atomic
upsert against the pair's unique constraint (the constraint, not any lookup, arbitrates
concurrent counting); batches go in `(org_id, key_hash)` order and a write error propagates so
charge and marks roll back together. Only a storable question is marked. `lookup` reports
`repeat_for_org`; the settle scales the raw amount (floor division) before the ledger applies the
margin. `tool_called` carries `cache_price` (`full` | `repeat` | `free`). 100 restores "a hit
bills like a live call"; 0 makes repeats free.

## Serving

`archive.lookup()` runs at the relay's position: after every access, deny and cap gate and after
the reserve, replacing only the network trip. The response carries `X-Treg-Cache: hit`,
`X-Treg-Fetched-At` and `X-Treg-Age`; the audit row carries `cached: true` (`CallRecord.cached`
is declared last in the model to match ALTER TABLE's append position). A hit is not an
observation: no snapshot, no learning, only a fire-and-forget `last_requested_at` touch, the
demand signal refresh reads. `lookup` uses the API pool on purpose so it never queues behind the
archive's own background writes.

Freshness: `ttl_for(entry)` gives fixed starting guesses by capability prefix; a positive learned
`ttl_s` replaces them. Serving takes the minimum of the learned TTL, the vendor
`cache.max_age_s` (`declared_max_age_s`), the operator ceiling and the caller's `X-Treg-Max-Age`;
the static default never caps a learned TTL. A **volatile capability** (`volatile_max_age_s`: a
segment `live`, `realtime`, `quote` at 60 s, `price` and `hot*`/`trend*` at 300 s) is a hard
ceiling even over a learned timer, because the learner cannot tell "flat over the weekend" from
"stable".

Caller controls are always honored: `Cache-Control: no-cache`/`no-store` forces a live call (the
read-after-write escape; the archive never guesses cross-endpoint effects); `X-Treg-Max-Age`
tightens, never widens; malformed values are ignored. Lookup returns None on every uncertain
branch, and a fault anywhere in lookup degrades to a live call.

## Result admission

`domain.catalog.results.classify` reads the buffered bytes, never rewriting them, into `found`,
`empty`, `error` or `unknown` with bounded reason codes. Result-aware behavior applies only to
endpoints with a verified adapter and a nonempty hit/miss expression (`has_result_rules`), reusing
`Adapter.is_miss`, and to a v2 row with a verified adapter that declares a 2xx miss, judged by its
outcome declaration ([catalog-schema](catalog-schema.md)); a capability that must never be routed can opt in through a
`routed: false` contract ([catalog](catalog.md)). A 2xx failing the `expect` success rule is `error` /
`expect_failed` before any adapter reads it, but a value listed in the rule's `misses` is judged by
the adapter. Strict per-endpoint validators live in `results.py`.

`ArchiveKey.result_state` / `result_snapshot_id` track the last decisive found or empty
observation, separately from the newest snapshot; `result_observed_version` marks the newest
version assessed, so a version appended by an older binary invalidates the saved decision. Legacy
keys are classified lazily; lookup rechecks the candidate body against current rules and never
searches behind an explicit empty. The pointer has no FK (snapshots are never deleted), avoiding a
cyclic foreign key.

For result-aware endpoints: only found-to-found counts stable; found-to-empty counts a change and
invalidates serving; repeated empties neither grow nor shrink TTL; empty-to-found restores
eligibility; errors and unknowns add history but never replace decisive evidence or learn. A
retained positive result expires at its own timestamp. Only confirmed `found` keys earn
refreshing. Endpoints without rules keep latest-snapshot serving and learning. Byte retention and
dedup stay independent of usefulness, and invalidation lands only when the background recording
commits: it is not a synchronous guarantee.

## The learner and comparison

The learner runs inside the recorder on decisive refetches of a known key. AIMD on `ttl_s`: stable
grows x1.5, capped by min(30 d, vendor `max_age_s`); changed halves, floored at 60 s. A key whose
first 4 refetches all changed marks itself `TTL_NEVER` (-1), never served until a stable refetch
resets it.

Comparison: identical raw hashes are stable; otherwise `_ignored_matches` compares JSON with
sorted keys and compact whitespace, after deleting declared `cache.ignore_paths` (`[*]` selects
array elements; missing paths are no-ops). Array order, length, types and other values stay
significant. Duplicate keys, precision-losing numbers, invalid JSON and unavailable bodies fall
back to raw hashes. Stored bytes, hashes and served responses never change. Declare an ignore path
only for a field the provider changes on every request without changing the answer (request ids,
credit counters, processing timestamps); never mailbox values, scores or statuses.
`test_catalog_preserves_ignore_paths_and_defaults` pins the shipped declarations. A
comparison-only `cache` mapping asserts no licence. `ArchiveKey.volatile_paths` is retained for
schema compatibility only.

The pre-read loads at most the latest and decisive snapshot bodies before the DB write, closes its
pointer session before JSON work or object I/O, and passes only matched snapshot ids into
`_store_locked`; a baseline that changed concurrently falls back to raw hashes rather than
holding a row lock across I/O.

## The refresh worker

`archive.refresh_worker` runs in-process from lifespan when `worker_enabled()`: serve mode, a
positive `archive_refresh_daily_cap` (default 0, so nothing spends treg's money unasked), a
nonempty allowlist and a valid percent. A key earns refreshing only when its window is at least
80% consumed (operator ceiling included) and a caller asked since the last fetch (a refresh is
never demand). Brakes: the per-provider daily cap, counted from `origin="refresh"` snapshots so no
bookkeeping table can drift, and 10 per pass. Refresh spend is treg's own, attached to no org and
absent from the ledger.

The call replays the stored request shape including the keying headers (`req_headers`; without
them the recording lands under a different key), with injection from the one authoritative
builder `oauth_providers.platform_bindings` (outside `api.py`, which this worker must not import),
and records through the same `_store`.

## The recorder

Hooked right after `_buffer_response`. `archive.record()` is fire-and-forget with audit's
discipline: a pending set bounded by count (512) and body bytes (256 MiB; each task holds its
body), and failures logged at ERROR so a lost recording clears `FaultCaptureHandler`'s threshold
(free degradations such as a lookup falling back stay at WARNING). `drain()` runs on shutdown
before `analytics.drain()` and removes the tasks it gathered itself (the busy-spin this avoids is
pinned in `tests/test_audit.py`). A recorder crash cannot fail a call.

Every write (`_store_locked`, `_touch_write`, `prune_once`, `refresh_once`) uses
`background_session_maker` (`tests/test_db_pool_isolation.py`). At most two recordings touch the
database at once (`_MAX_CONCURRENT_WRITES`, paid per process); the 30 s bound covers wait plus
write so a stuck queue sheds rather than wedges. An in-process per-key lock is taken before the
semaphore so duplicates do not consume both slots. Once admitted, the writer locks and refreshes
the `ArchiveKey` row before reading the newest snapshot and allocating version N+1 (the refresh
matters because the earlier unlocked lookup filled the identity map); the row lock serializes
processes on Postgres, and version conflicts go to the bounded retry for SQLite and first-key
races.

**Counted vs kept.** Statistics and the raw `content_hash` are recorded for every observed answer.
Bytes are kept only under a `transient`/`archive` policy and within `archive_max_body_bytes`
(skipped whole, never truncated). Consecutive identical answers dedup via `body_of`; an identical
answer arriving where bytes were never kept stores them, so a policy upgrade heals forward. DB
bodies compress with zlib when it shrinks them (`enc`; under 256 bytes stay raw); every reader
unpacks. Do not store bodies as rebuilt JSON values: that breaks the byte-verbatim promise the
hashes depend on.

## The call-archive link

`record()` returns `(key_hash, content_hash)`, which the call service writes onto `CallRecord`
(`archive_key_hash`, `archive_content_hash`). `archive.resolve_result` walks row, key, newest
snapshot with that content hash, `body_of` carrier, and returns the pre-injection request shape
plus the answer; a hash-only version reports `stored: false`. `GET /calls/{id}/result` exposes it
to members of the row's org; what makes it safe is that the row belongs to the team and names
bytes that team already received. A routed parent has no link of its own and lists its
`children`, audited before the parent, so the lookup reads the hour before the parent's row.
Failure evidence (`error_*`) stays admin-only. `scripts/backfill_call_archive_links.py` links
older rows best effort (dry run unless `--apply`).

## Body storage and R2 double writing

`archive_bodies` owns body preparation, object reads, the upload queue and storage observations;
`archive.py` owns keys, snapshots, transactions, learning and pruning.
`infra.object_store.ObjectStore` exposes put, get, head and delete. Every object name is the raw
body's SHA-256 with no prefix; PUT sends a SHA256 checksum and GET verifies size and the full hash
before returning bytes. `ArchiveSnapshot.body_storage` is `db`, `both` or `r2`; NULL is the legacy
DB path and does not promise bytes were retained. R2-only snapshots have no `body_of`.

| Setting (`TREG_` prefix) | Values | Default |
|---|---|---|
| `ARCHIVE_BODY_WRITE` | `db`, `both`, `r2` | `db` |
| `ARCHIVE_BODY_READ_LOOKUP` / `_RESULT` / `_TERMINAL` | `db`, `r2-first` | `db` |

Startup refuses `r2` writes unless all three reads are `r2-first`, and refuses any R2 switch
without the four `ARCHIVE_OBJECT_STORE_*` settings, before opening DB connections.
`scripts/smoke_archive_r2.py` runs a real PUT/HEAD/GET outside CI against the dev bucket only.

Invariants:

- **No object I/O under a row lock or a checked-out connection.** Uploads finish before the
  writer's transaction; readers collect an `archive_bodies.pointer` in a session, close it, then
  `archive_bodies.read`, opening a new short session only for DB fallback bytes. This holds for
  lookup, result, terminal, admin, Arena and replay reads.
- **A failed upload never publishes a pointer.** Eligible bytes fall back to a DB copy (`db`
  plan) in both R2 write modes, including terminal evidence in R2-only mode. A successful upload
  followed by a failed transaction can leave an unreferenced object; that is accepted.
- **A failed read never returns a different version.** `r2-first` tries the selected snapshot's
  hash regardless of `body_storage`; missing, timeout, error or checksum mismatch falls back to
  that exact snapshot or carrier, else unavailable. A successful `r2`-only write has no DB copy,
  so an R2 read failure is a cache miss. Authorization, key scope and result selection precede
  body resolution.
- **Uploads are deduplicated per process**: concurrent same-hash uploads share one leader flight
  (followers hold no slot and cannot cancel the leader), and a bounded LRU of completed hashes
  skips PUT, re-checked with `head` (see "Opting out"). A GET that finds an object missing or
  corrupt invalidates its entry.
- **Bounded and separate budgets.** R2 has its own concurrency and pending count/byte budgets
  (`ARCHIVE_R2_*`); admission failure falls back to the DB queue. Retryable failures (`timeout`,
  `rate_limited`, `upstream_error`, `store_error`) get one retry inside the same deadline; SDK
  retries stay disabled. Terminal evidence bypasses best-effort admission and retries
  synchronously before its DB write, because its settlement has already committed.
- Logs and events carry bounded reason names and the exception class, never exception text, keys,
  bodies or credentials.

`archive_body_stored` and `archive_body_read` are best-effort completion events; `tool_called`
never waits for archive work. Per-process counters reset on restart and are not fleet totals.

## The running totals

`ArchiveEndpointStat` is one row per endpoint (keys, stable, changed, snapshots, bodies_kept,
kept_bytes, newest_fetch), changed only by atomic column arithmetic, never read-modify-write. The
row is shared by every team, so a recording adds its counts to an in-process delta after its own
commit (`_count_stats`; a dropped or retried recording counts nothing), and `flush_stats` lands
them as one statement per endpoint, in endpoint order so two processes cannot deadlock, under the
archive's write semaphore. A failed or interrupted flush keeps its deltas; a process that dies
between flushes loses that window, which is fine because nothing serves or bills from the totals.
The pruner and erasure subtract directly. Arithmetic commutes only while the row exists, so the
first recording of an endpoint creates it zeroed inside its own transaction (`_ensure_stat_row`),
and a process skips that check only after one of its recordings committed with the row.

## The pruner

`prune_once` strips bytes only: every version row keeps hash, size and timestamp; the newest
version of every key, the decisive snapshot and the carriers of surviving versions stay whole (the
strip set is decided before carrier protection). Order: never-servable (`TTL_NEVER`) keys keep
one body; then versions older than `archive_prune_min_age_days` beyond the newest
`archive_prune_keep_versions` on keys undemanded for 14 days. `archive`-policy endpoints are
exempt. Batch 0 disables. Stripped `both` rows become `r2` and keep their logical counts; only
erasure deletes objects.

## Change observation

After `_store_locked` commits, `_observe_change` emits `archive_change_observed` for `caller` and
`refresh` origins, comparing against the baseline that actually drove learning. It reports only
structural JSON paths, counts and `masked_by_ignore`, never values, call references or key
identities; reported paths are not evidence a field is safe to ignore. It runs under the archive
semaphore with its own budget, reads bodies through the pointer-then-read pattern, cannot affect
admission, learning, storage or serving, and is disabled by
`TREG_ARCHIVE_CHANGE_OBSERVATION_ENABLED=false`.

## Admin surfaces

`GET /admin/archive` reports totals and per-endpoint rows from the running totals.
`GET /admin/archive/panel` serves a data-free page shell, deliberately unauthenticated: every
number arrives by fetch with the admin token the operator pastes. `/admin/archive/keys` reads
versions in one columns-only query (`body IS NOT NULL`), never loading bodies.

## What the archive must never touch

Balances. Archive code never imports the ledger or billing and never changes a balance; its one
write on the money path is the `ArchiveKeyOrg` mark inside the settle transaction. Nor the
upstream response: what it serves is byte for byte what the vendor sent.
