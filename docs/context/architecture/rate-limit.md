---
title: Request admission — blocklist, overload cap, global ceiling, surge signals (batch 1)
status: built; blocklist log_only, global ceiling off, overload cap off by default
sources:
  - src/treg/domain/ratelimit/__init__.py
  - src/treg/domain/ratelimit/policy.py
  - src/treg/application/ratelimit/__init__.py
  - src/treg/application/ratelimit/limiter.py
  - src/treg/application/ratelimit/config_state.py
  - src/treg/application/ratelimit/config_sync.py
  - src/treg/application/ratelimit/blocklist_state.py
  - src/treg/application/ratelimit/blocklist_sync.py
  - src/treg/application/ratelimit/background.py
  - src/treg/application/ratelimit/metrics.py
  - src/treg/application/ratelimit/signals.py
  - src/treg/application/ratelimit/signals_flush.py
  - src/treg/application/ratelimit/inflight.py
  - src/treg/application/ratelimit/admin.py
  - src/treg/application/ratelimit/blocklist_admin.py
  - src/treg/application/ratelimit/receipts.py
  - src/treg/routers/admin_rate_limit.py
  - src/treg/bootstrap_ratelimit.py
  - src/treg/infra/request_scope.py
  - src/treg/infra/kv.py
  - src/treg/infra/db.py
  - src/treg/config.py
  - src/treg/domain/identity/access.py
  - src/treg/alembic/versions/0072_request_admission.py
  - tests/test_rate_limit_policy.py
  - tests/test_rate_limit_config.py
  - tests/test_rate_limit_middleware.py
  - tests/test_rate_limit_admin.py
  - tests/test_rate_limit_overload.py
  - tests/test_rate_limit_blocklist_sync.py
  - tests/test_rate_limit_codex_review.py
  - tests/test_rate_limit_redis.py
related:
  - architecture/import-boundaries.md
  - architecture/money.md
  - ops/capacity.md
---

# Request admission

A flood through many valid keys exhausts **in-flight concurrency** before request rate. treg has
no reverse proxy, so admission lives in the app, as the outermost middleware
(`bootstrap_ratelimit.RateLimitMiddleware`, added last in `bootstrap.create_app`), before auth, any
database session and money admission. Decisions are `application.ratelimit.limiter.RateLimiter`;
rules are `domain.ratelimit.policy`. Per-key, per-org and tier layers, and IP tightening, are a later batch.

**Identity.** The caller is the one credential the route's auth honors
(`request_scope.credential`: the Bearer on `/mcp`, `X-Treg-Token` elsewhere; MCP's `_bearer` uses
the same helper), and it is known only once auth has accepted it: `access.require_member` publishes
the hash of the credential it actually used and whether that credential pins its team. A pinned
credential is cached by its hash alone, so no other header changes who the limiter sees; an
unpinned identity token, whose team auth takes from `X-Treg-Org`, is cached by hash and that team.
A request whose credential is not resolved (none, unknown, or rejected by auth) is unresolved.

**Client IP.** The entry `rate_limit_proxy_hops` (default 1) from the right of `X-Forwarded-For`,
the one the nearest proxy appended (`bootstrap_ratelimit.client_ip`); what the client wrote further
left is never used. 0 hops means the TCP peer, for a deployment with no proxy in front.

Order per request:

1. **Overload cap** (`overload_cap_enabled`, off by default): an in-process `asyncio.Semaphore` of
   `overload_cap_size`, no shared store. When it is full (`locked()`) the request gets **503**,
   `Retry-After`, `reason: overload_cap` (`X-Treg-Overload-Reason`), at once; it never queues. The
   permit is held until the app returns, which for ASGI is after the last streamed chunk, a client
   disconnect, an exception or a cancellation. Health checks and `/admin/*` are not counted. MCP
   streams (`GET /mcp...`) never take a main permit: they have a separate `overload_cap_stream_size`
   semaphore, refused as `overload_cap_stream`. An MCP POST takes a main permit even when it
   accepts `text/event-stream`. treg's own in-process requests (an MCP tool's `/call/`) take no
   permit and are not counted in flight: the outer request already holds one.
   Distinct from the database pool's `503 treg_saturated`. The size is provisional: measured
   busy-hour concurrency is above it, so it is set from `inflight_gauge` before the flag is on.
   Every external request is counted in flight (`inflight`) whether or not the cap is on.
   `background.inflight_loop` emits `inflight_gauge` (pid, current and peak in flight, per-pool
   checked out, peak and capacity) every `inflight_gauge_emit_s`, as a log line and a metric. Both
   peaks are true maxima for the window, not samples: in flight updates on every request, the pool
   peak on every checkout (`db.install_checkout_peaks`); both reset at each emit.
2. **Blocklist by key hash**, before auth (SHA-256 of the honored credential, the `ApiKey.key_hash`
   spelling), and by org id when the credential's org is already in the key cache.
3. **Unresolved requests**: a lease in the client IP's small bucket (`anonymous_ip_concurrency`).
   Unset, it is counted and never refused; set, a full bucket is **429**
   `treg_rl.anonymous_ip_concurrency`.
4. **Global concurrency ceiling** (below), last of the middleware's checks.
5. **Blocklist by org id after auth**: the middleware puts `check_org` in the request's scope slot
   and `access.require_member` calls it once it knows the org (`_refuse_blocked_org`), so a blocked
   org is refused even on a key's first request in a process. Auth publishes the key's org first,
   so from the key's next request the refusal comes from the key cache, before any session opens.
   The cache holds the key-to-org mapping, not the verdict: an unblock applies at the next sync.
   An MCP client's Bearer is cached through the alias its in-process `/call/` teaches.

Every refusing layer has its own label: `overload_cap`, `overload_cap_stream`,
`treg_blocklist.key_hash`, `treg_blocklist.org_id`, `treg_rl.anonymous_ip_concurrency`,
`treg_rl.global_concurrency`.

**Leases.** Both counting layers are sorted sets of request ids in the shared store (`kv._RL_ADMIT`),
scored by their last renewal. The middleware releases them in a `finally` shielded from
cancellation (anyio cancels every await in a cancelled scope, `finally` included), the background
`lease_loop` renews every in-flight request's leases (`limiter.renew_leases`, `kv._RL_RENEW`) well
inside `concurrency_ttl_s` so a long stream keeps counting, and a lease nobody renews ages out after
the TTL: the backstop for a process that dies holding one.

## Blocklist

Mode (on the version row, set by `POST /admin/blocklist/mode`, super-admin only for every target
mode, and back to `log_only` by the policy's `revert-to-shadow`): `off`, `log_only` (the default: match, log
`rate_limit_blocklist_match`, serve), `enforce_403` (refuse with **403**, `X-Treg-Block-Reason`, a body holding only the fixed
reason code and the blocklist version, so clients do not retry; the operator's reason text stays in
the audit record).

**Source:** Postgres only. `BlocklistEntry` (one row per target, `expires_at` always set,
`revoked_at` for an unblock), `BlocklistAudit` (one row per call, append-only, carrying the version
it produced) and the single `BlocklistVersion` row (version and mode), pre-inserted by migration
0072. Every block, unblock or mode change runs `UPDATE ... SET version = version + 1 RETURNING
version` on it in the same transaction as its rows (`blocklist_admin.advance`); the row lock
serializes writers and the returned value is the version the readback waits for. Redis holds no
blocklist.

**Cache:** every `rate_limit_blocklist_refresh_s` (at least 10 s) each process reads the version
row by primary key on the dedicated `ratelimit` pool (one connection, statement and connect
timeouts), the whole poll bounded by `rate_limit_blocklist_poll_timeout_s`. Only a changed version
or mode reloads, and the reload reads version, mode and active entries in one statement
(`blocklist_sync._read_snapshot`), so a new version is never cached with old entries. A reload past
`rate_limit_blocklist_max_entries` is refused whole: the last good list stays and the process
alerts. A failed poll keeps the cached list and alerts once per distinct failure
(`rate_limit_blocklist_load_failed`). The request path reads the in-memory copy only
(`blocklist_state`). Shutdown cancels the background loops between polls: the poll already in
flight finishes and closes its session before the task ends (`background._finish`).

**Startup:** each worker makes one first-load attempt capped at
`rate_limit_blocklist_first_load_s` (`blocklist_sync.first_load`, one log line with pid and elapsed
ms) and serves either way. On failure it fails open, the background loop retries every couple of
seconds, and `blocklist_cache_empty` is reported until a list loads. `/health` never depends on the
blocklist.

**Writer:** expected to move to treg-internal. Until then, every write route here is hard-off
(404, before auth) unless its token is configured. `POST /admin/blocklist` (`blocklist_admin.block`), authorized by
`blocklist_admin_token`, a credential that opens the block, unblock, list and status endpoints only
(never the mode)
(`require_blocklist_writer`; a super-admin also passes). Guards, each a **400** naming the failure,
and one bad target refuses the whole call:

- every target resolves to an org (a key hash through `ApiKey` or `Membership.token_hash`; a signed
  Default key has no stored hash and is blocked through its org);
- the org has **never paid** (no completed Stripe top-up; treg records no refund amount) and was
  created inside `blocklist_new_org_max_age_s`;
- at most `blocklist_max_entries_per_call` targets; every entry expires, `ttl_s` defaults to
  `blocklist_default_ttl_s` (24 h) and may not exceed `blocklist_max_ttl_s`;
- reason, approver and non-empty evidence are required.

The call is budgeted (`blocklist_writes_per_hour`, a shared-store window that fails closed, 429).
Entries, the audit row and the version commit in one transaction; there is no second copy to
publish. After the write the server posts a receipt (entries, TTL, version, audit id) to
Slack (`receipts`, `blocklist_receipt_slack_token`/`_channel`) in a task: best-effort, counted on
failure (`blocklist_receipt_failures`), never delaying or undoing the write. `POST
/admin/blocklist/{id}/unblock` is the one-action unblock. `GET /admin/blocklist/status?version=N` is
the readback: `live` once every process with a fresh heartbeat (`rl:live:<instance>`) serves at
least N. A process whose heartbeat expired is not counted.

The window, per-call cap and write budget defaults are placeholders, to be set from surge signals.

## Global concurrency ceiling

Mode `off` (the env default, until the store's request-path latency is measured), `shadow` (logs
`rate_limit_would_block`) or `enforce` (**429**, `Retry-After`). Each limiter store call has a
`rate_limit_store_timeout_ms` budget (default 30 ms) on its own Redis client (`RedisStore._rl_client`).
That socket timeout is not the money store's one-second `_TIMEOUT_S`. A slower answer is a miss.
`concurrency_ttl_s` is at least `call_timeout_s`. `global_paid_reserve` slots are usable by paid orgs only
(`RateLimitConfig.global_cap`). A key not yet in the key cache (every key's first request after a
deploy) is counted but never refused: refused before auth it would never be learned. The figure is
unset by default; it comes from measured peak in-flight concurrency and p95 latency. A store fault
fails open to `policy.LocalCeiling` per process, with a short cooldown.

The key cache (`KeyCache`) learns a credential's org, paid status and age from
`access.require_member` when the middleware asks (`want_caller`), one indexed read per key per TTL.

## Policy

`RateLimitConfig` (mode and the counting figures; the blocklist's mode is on its own row). **Env**
holds defaults (mode `off` or `shadow`). **Postgres** `RateLimitConfigVersion` is the audited
source and the only one processes serve from (`PUT /admin/rate-limit/config`, super-admin; range
checked; `enforce` needs `enforce_approval`; `revert-to-shadow` writes a new, higher version). Every
`rate_limit_config_refresh_s` each process reads the newest row on the `ratelimit` pool
(`config_sync.poll_once`). A bad or unreadable policy keeps the last good one for at most
`rate_limit_config_max_age_s`; past that `enforce` is served as `shadow` and
`rate_limit_config_stale` alerts. With nothing good and Postgres unreadable, env defaults serve and
`rate_limit_config_env_fallback` alerts; an empty table is simply the env defaults. Every version
change is logged.

**`rl:config`** is a cache for tools, never read by a serving process. Writes to it are a
compare-and-set (`kv._RL_PUBLISH`): stored only when the incoming version is strictly newer, so a
stale republish or a slow writer after a Redis restart can neither overwrite a newer policy nor be
served. The request path never reads Postgres (import contract).

## Surge signals

Record-only. `signals` aggregates in process memory per IP, per org and per (IP, org): requests,
call requests, unknown keys, 401/403/429/5xx, blocklist matches, distinct orgs and keys, peak in
flight; bounded by `surge_max_subjects`. `signals_flush` writes one `SurgeSignal` row per subject
per `surge_flush_s` on the `ratelimit` pool, with the org's `created_at` and paid total. Pruning past
`surge_retention_days` is a separate transaction after the insert, at most hourly, with the attempt
time recorded before the DELETE, so a failed prune never takes an insert batch with it. Signals
record the same client IP the limiter uses. `rate_limit_proxy_hops` must equal the number of
proxies that append to `X-Forwarded-For` (1 behind the platform's one proxy). 0 hops, for a
deployment with nothing in front, uses the ASGI client address: a client can otherwise supply the
only forwarded-for entry and pick its own bucket. `python -m treg` starts uvicorn with its
defaults (`proxy_headers` on, `forwarded_allow_ips` from `FORWARDED_ALLOW_IPS` or loopback only),
which may rewrite that address from a trusted peer; the limiter does not use that rewrite when
hops is at least 1. Trusting every peer (`*`) is never the setting.

## Telemetry

`ratelimit_decision` rows by layer, reason, result, tier, paid, config version and mode;
`ratelimit_gauge` per process with its config and blocklist versions and modes, peak global
concurrency, store latency, `rl:` memory sample and the counters above.
