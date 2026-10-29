---
title: Running & deploying the server
status: shipped
sources:
  - pyproject.toml
  - hatch_build.py
  - scripts/build-dashboard.sh
  - scripts/build-web.sh
  - scripts/frontend-e2e-server.sh
  - src/treg/__main__.py
  - src/treg/maintenance.py
  - src/treg/alembic/env.py
  - src/treg/alembic/versions/0034_managed_api_keys.py
  - src/treg/alembic/versions/0035_default_key_generation.py
  - src/treg/alembic/versions/0036_activity_key_indexes.py
  - src/treg/worker.py
  - src/treg/web/selfhost.sh
  - src/treg/config.py
  - src/treg/infra/db.py
  - src/treg/infra/money_admission.py
  - scripts/bench_money_admission.py
  - src/treg/email.py
  - src/treg/audit.py
  - scripts/dev-local.sh
  - deploy/render.example.yaml
related:
  - ops/capacity.md
  - architecture/data-model.md
  - architecture/ads-conversions.md
  - foundation/charter.md
---

# Running & deploying

This fragment documents behavior every operator needs to run or self-host a registry. It does not
describe the private topology, live settings, account funding, incidents or rollout state of the
hosted treg.to service. Superdesign operators use the private
[treg.to deployment runbook](https://github.com/superdesigndev/treg-internal/blob/main/docs/production/deploy.md).

Fish Audio shared-key capacity monitoring requires both `TREG_PLATFORM_KEY_FISHAUDIO` and
`TREG_PLATFORM_FISHAUDIO_WORKSPACE_ID`. The latter is the Fish workspace selector used only by the
free API-credit probe; without it, capacity remains unknown rather than reading the unrelated
personal wallet.

OpenRouter capacity monitoring reads `TREG_PLATFORM_OPENROUTER_MANAGEMENT_KEY` (sweep only);
without it, OpenRouter reports no balance API.

## Call events

Call events ([call-events.md](../architecture/call-events.md)) are off until
`TREG_CALL_EVENTS_URL` and `TREG_CALL_EVENTS_HMAC_SECRET` are set on every process that serves
calls or settles async tasks (web and the async worker); `TREG_CALL_EVENTS_TOKEN` is the stream's
bearer when it requires one. Keep the secret stable: changing it breaks every join across the
change.

## Additional public domains

`TREG_PUBLIC_HOST_ALIASES` is a JSON array of bare hostnames accepted for same-host social-login
callbacks, MCP hosts/origins and token audiences, and the hub self-relay guard. It defaults to an empty tuple. Each deployment supplies its complete alias list explicitly;
setting it replaces the list rather than appending to it.
Adding an alias does not change `TREG_PUBLIC_URL`, canonical links or provider-connect callbacks.
Register each alias's `/auth/google/callback` and `/auth/github/callback` with the respective OAuth
client before enabling it. The OAuth state and browser session cookies stay on that alias.

Turnstile's widget hostname list must include every browser alias. Server-side hostname validation
accepts the canonical host and every configured alias; a non-empty
`TREG_TURNSTILE_HOSTNAMES` overrides this fallback and must itself list every accepted host.

Apply and verify the complete alias configuration before deploying code that consumes it. Older
builds ignore the extra setting; deploying the new code first can invalidate existing alias grants.

## Entry point (`__main__.py`)

`python -m treg upgrade` runs the explicit release phase. `maintenance._upgrade_schema()` runs
`alembic upgrade head` for an empty or stamped database. A non-empty unstamped database is refused
without writes: adoption ended with 0.14.x, so the operator must install
`tools-registry[server]==0.14.*`, run `python -m treg upgrade` there once, then continue onward. A
database stamped at a revision this build does not know is also refused because explicit upgrade may
not cross the rollback floor. The ordered, idempotent release-task registry runs only after schema
success and never provisions a local user.

The default `python -m treg` serve path runs the same upgrade phase, calls
`api._bootstrap_single_user()`, disposes the async engine inside the pre-serve event loop, and then
calls `uvicorn.run("treg.api:app", host="0.0.0.0", port=int($PORT or 18790))`. `--reload` is optional.
It honors `$PORT` for platforms that route and health-check an injected port. `python -m treg keygen`
prints a Fernet key for `TREG_SECRET_KEY` without importing the server maintenance stack.
`treg.api:app` is `bootstrap.create_app(role="all")`.

The FastAPI lifespan calls read-only `verify_db()`. It refuses an unstamped database or a known
revision behind head and directs raw-ASGI operators to `python -m treg upgrade`. An unknown newer
revision means the code is older than the schema: startup warns and serves because additive revisions
tolerate rollback. A revision marked `contract = True` is the documented hard rollback floor. Every
role startup manifest is schema-write-free.

Both pre-Uvicorn entry paths dispose the engine before their `asyncio.run()` loop closes. The next
event loop therefore creates fresh pooled connections instead of receiving connections bound to a
closed maintenance loop. Calling `maintenance.upgrade()` directly does not dispose the engine.

## Schema upgrade safety
- **Managed-key rollback floor:** revision `0034` adds key controls, audit rows, Activity snapshots,
  and a hash-only backfill for existing membership credentials. It is marked `contract = True`
  because old code cannot enforce newly stored disable or revoke state. The migration is additive
  and uses SQL that works on SQLite and Postgres.
- **First-run lookup removal floor:** revision `0078` drops `onboardingprofile`. Code older than it
  reads or writes that table only while a `TREG_ONBOARDING_V2*` setting is on, so turn those off
  before upgrading, and keep them off on any rollback past it.
- **Alembic is authoritative:** migration scripts ship inside `src/treg/alembic/` in the wheel.
  `maintenance._alembic_config()` resolves that installed package resource, supplies the escaped
  configured URL, and runs Alembic in a worker thread.
- **The adoption floor is final.** An unstamped existing database must pass through release 0.14.x.
  Current code does not inspect, repair or stamp it.
- **Startup is read-only.** `verify_db()` checks the stamped revision through packaged Alembic
  metadata. It never creates tables, stamps versions or runs release tasks. Worker commands use the
  same check.
- **Schema changes are revision-only.** An autogenerate drift guard requires Alembic head and
  `SQLModel.metadata` to match exactly. `reset_db()` uses `create_all` only to build a missing test
  schema and stamps it directly at head.
- **A missing encryption key fails loudly on a real database.** If `TREG_SECRET_KEY` is empty and
  `database_url` is not SQLite, `verify_db()` raises. On SQLite development it logs a warning.

For PostgreSQL, migrations set bounded lock and statement timeouts. A contended ordinary DDL
migration must fail cleanly rather than queue production traffic behind an exclusive lock: while an
`ALTER TABLE` waits for its `ACCESS EXCLUSIVE` lock, every later query on that table queues behind
it, so the 5 s `lock_timeout` is the longest stall a deploy may inflict and no revision may raise it
for an `ALTER`. A deploy waits longer by retrying instead: `maintenance` re-runs `alembic upgrade
head` up to `LOCK_RETRY_ATTEMPTS` times after a lock timeout, pausing a jittered few seconds between
attempts so the table's short transactions can drain, and `env.py` commits each revision on its own
(`transaction_per_migration`) so a retry resumes at the revision that timed out. Any other error
fails the deploy at once. Concurrent index creation or removal may use longer bounded timeouts in
its own revision: its lock permits ordinary reads and writes, although it conflicts with other
maintenance. Such a revision restores the caller's timeouts and handles interruption: invalid builds
must be rebuilt, and a partially completed removal must be retryable. Revision `0061` removes only
ordinary indexes with verified unique-constraint replacements; its rollback recreates those indexes
concurrently and requires enough disk space for them. Revision `0072` builds
`ix_callrecord_call_ref` where a database stamped at the baseline never got it; a build on a large
table outlasts any fixed statement timeout, so it lifts `statement_timeout` for that one statement
(the lock timeout stays bounded) and the deploy's own pre-deploy limit bounds it. Deploy such a
revision in a quiet window: the build reads the whole table while traffic keeps writing.

Hot-table ALTERs stay cheap to retry when they sit in their own revision, add only nullable columns
without defaults (metadata-only in PostgreSQL) and leave backfills and `NOT NULL` to later steps.

Deploy schema changes before starting application or worker code that expects the new revision. A
platform whose scheduled workers update independently must sequence them accordingly.

## Database pools

`POOL_SPECS` in `infra/db.py` defines three PostgreSQL pools:

| pool | maker | serves |
|---|---|---|
| `api` | `session_maker` | request handlers |
| `admin` | `admin_session_maker` | `/admin/*` handlers |
| `background` | `background_session_maker` | audit, archive and other background work |

Each class can exhaust only its own slots. Minor pools deliberately have no overflow because an
unbounded escape hatch defeats the bulkhead. A new class of work gets an appropriate pool, not only
a module-local semaphore. `tests/test_db_pool_isolation.py` pins the routing.

Every process opens its own pools and runs its own in-process background tasks. Calculate the full
connection peak from pool capacities, process count and the maximum number of overlapping instances
during a rolling deployment. Keep that peak below the database connection ceiling with headroom.

`TREG_DB_POOL_OVERRIDES` accepts entries such as
`"admin.pool_size=4,background.pool_size=12"` and patches `POOL_SPECS` at startup. Unknown pools,
unknown field names, non-integers and out-of-range values are logged and skipped. In particular,
SQLAlchemy treats some zero and negative values as unlimited, so the parser must not pass them
through. Live values and production sizing belong in the private operator runbook.

`bootstrap.pool_gauge` samples `infra/db.pool_snapshot()` and emits per-pool peak, capacity and
headroom. Resize from measurements, one pool at a time. A larger pool does not repair a slow scan,
lock queue, disk-bound database or missing index.

Web roles and worker commands also run local money-transaction diagnostics independently of the
PostHog key. Verify fresh `money_trace_gauge` records for each deployed build and process, including
tracking counts, sample age and drop counters. `money_txn_slow` and `money_txn_end` correlate backend
PIDs with application transaction IDs and stages; join within their timestamp intervals because
connections are reused. Read the coverage and loss limitations in
[data-model](../architecture/data-model.md#product-analytics-writer-analyticspy) before interpreting
missing records. Deployment requires no schema migration or new database privileges.

All pools use `pool_pre_ping=True`, `pool_recycle=300` and `pool_timeout=5`. A request that gets no
slot in time receives `503 {"treg_saturated": true}` with `Retry-After: 2` through
`bootstrap_handlers._pool_saturated`. A `/call/` holds no connection during the upstream round trip.

SQLite aliases all three makers to one engine. It has no pool to protect and file-level write locks
it cannot share, so separate engines would only manufacture lock failures. Tests pin routing rather
than SQLite isolation.

### Dashboard reports

Team dashboard reports that aggregate whole windows of the audit and ledger tables
(`/orgs/{id}/usage`, `/orgs/{id}/usage/by-tag`, `/orgs/{id}/agents/observed`) take
`get_report_session` instead of `get_session`. It opens `read_session_maker`: the replica when
`TREG_READ_DATABASE_URL` is set, otherwise the API pool. Either way:

- at most `_REPORT_SLOTS` (2) reports run at once per process; a report that cannot get a slot
  within `_REPORT_WAIT_S` (1 s) is refused before checkout;
- each report transaction sets `statement_timeout` (`_REPORT_STATEMENT_TIMEOUT_MS`, 15 s) on
  PostgreSQL, and the report as a whole runs under `_REPORT_DEADLINE_S` (15 s), so a report of
  several sub-limit statements cannot hold its connection longer either. The deadline is an
  `asyncio.timeout` around the dependency's yield: FastAPI runs a dependency's body and the endpoint
  in one task, and cancelling an in-flight asyncpg query cancels it on the server;
- both refusals answer `429 {"report_busy": "busy" | "timeout"}` with `Retry-After: 2` through
  `bootstrap_handlers._report_busy`: the service is up, the report should be retried later. They are
  not `treg_saturated` and record no `db_pool` fault.

Authorization finishes on `get_session` first; `require_member` commits, so that session holds no
connection while the report runs. On the API pool the gate caps reports at two of the process's
API slots; `/call/` keeps the rest. Reports on the replica see replication lag.
`tests/test_report_session.py` pins which routes take the report session. The dashboard runs the
per-tag reports one at a time and retries a 429 with backoff (`withReportRetry` in
`frontend/src/api.ts`); a report that still fails shows its error instead of being hidden.
The gate bounds a slow report but cannot make it fast: the usage counts and the observed-agents
roster read the daily usage rollup (`treg-worker usage rollup`, below) instead of the audit
table's 30-day window.

### Optional read replica

`TREG_READ_DATABASE_URL` adds a datasource exposed as `read_session_maker`. Its supported drivers
match the project's SQLite / PostgreSQL support: `sqlite+aiosqlite` and `postgresql+asyncpg`.
Bare `postgres://` and `postgresql://` URLs normalize to `postgresql+asyncpg`, as for the primary;
other read URL drivers fail settings validation. This does not add support for MySQL or other
SQLAlchemy dialects. When empty, the read maker aliases `session_maker`, with the primary's existing
transaction behavior (including writes) and no additional pool. Connections are opened only when used.

A configured URL always gets an independent engine, even when it names the primary database:

- PostgreSQL uses a `read` pool defaulting to two connections with no overflow;
  `TREG_DB_POOL_OVERRIDES` accepts `read.pool_size` and `read.max_overflow`. Each connection sets
  `default_transaction_read_only=on` through asyncpg's `server_settings`.
- SQLite retains the driver's default pool behavior and sets `PRAGMA query_only=ON` on every new
  read connection, including after reconnection. The primary's connections remain writable. For an
  existing file, `sqlite+aiosqlite:///file:replica.db?mode=ro&uri=true` additionally opens the file
  read-only and fails if it is missing. A plain SQLite URL retains SQLite's usual file-opening
  behavior, including creating a missing file; `query_only` guards SQL changes, not file creation.
  A separate in-memory SQLite URL starts empty and cannot serve as a copy of the primary.

These connection settings guard accidental writes; they are not an authorization boundary and can
be disabled by deliberate SQL. Use a physical replica/read-only database role or filesystem access
controls as appropriate. Connection/query failures propagate without primary fallback.

`/admin/errors` uses this datasource for evidence and org names. Authorization stays on the admin
primary pool and releases its connection before the report begins. With no read URL configured,
`get_admin_read_session` preserves the original admin pool rather than using the general reader's
API-pool fallback. Configured reader failures never retry on the primary.
Before enabling a hosted cutover, verify the running process has the intended read URL, the target
is a readable replica with the required table grants, and replication lag is acceptable. Configuring
a URL also moves the dashboard reports above and no other report. Removing it returns `/admin/errors`
to the admin primary pool and the dashboard reports to the gated API pool.

`pool_snapshot()` includes a separate `read` entry for a configured PostgreSQL datasource; SQLite
engines are omitted as for the primary. `connection_budget()` describes only the primary pools;
budget the read pool against its target database, including process count and deployment overlap.
If both URLs target the same server, add both budgets against that server's limit.
`dispose_engine()` also disposes the read engine. Schema upgrades, startup verification and test
schema resets continue to target the primary.

This datasource is opt-in infrastructure; `/admin/errors` and the dashboard reports above use it.
Further callers must tolerate replica lag and keep writes, cursor advancement and concurrency
control on the primary. Operators must provision and synchronize a compatible schema and data;
setting a URL does not establish replication, translate dialect-specific queries, or migrate any
Cron job's workload. Configure it in a private local `.env` or the hosting service's environment;
`.env.example` contains no secrets.

## Configuration (`config.py`)

`Settings` uses the `TREG_` prefix, reads `.env`, and is cached by `get_settings()`.

- `database_url` defaults to local SQLite. A validator rewrites bare `postgres://` and
  `postgresql://` URLs to `postgresql+asyncpg://`.
- `secret_key` is the Fernet key. An empty value creates an ephemeral key suitable only for local
  development.
- `public_url` builds OAuth callbacks and public links. Self-hosters must set it to their canonical
  external URL.
- `admin_token` is the cross-tenant super-admin bearer. Empty disables this environment path. Keep
  it long and secret.
- OAuth provider clients use `<provider>_client_id` and `<provider>_client_secret` settings. Empty
  credentials make that provider unconfigured instead of failing halfway through consent.
- `oauth_review_pending` is a comma-separated set of provider-registry review keys. Its hosted value
  is operational state and is maintained privately.
- `paused_providers` is a comma-separated set of provider service ids this deployment cannot serve
  right now, for example because its own app lost upstream access. Empty (the default) pauses
  nothing. `paused_provider_messages` is an optional JSON object `{service: message}`; malformed
  JSON fails at boot. Removing an id restores the provider with no reconnect. Its hosted value is
  operational state and is maintained privately. See `interface/api.md` § `503 provider_paused`.
- `promo_grant_micro` controls the once-per-verified-user signup grant. Zero pauses new automatic
  grants without changing existing balances.
- `blocked_email_domains` is the complete comma-separated blocklist. Empty blocks no domains. It is
  a sign-in and team-creation speed bump, not a substitute for suspending an existing abusive user.
- `run_proof` gates release of a shared key to the root-installed local runner. Empty refuses shared
  local runs while leaving owner-key runs available.
- `proxy_ssrf_check` enables the call-time SSRF guard and is on by default.
- `claude_connector_enabled` gates the catalog-only connector at `/mcp/v2/`.
- `connect_demo_enabled` gates the developer OAuth test page. Leave it off on public deployments.
- `intercom_app_id` and `intercom_secret` enable optional support chat. Empty disables it.
- `resend_api_key` and `email_from` enable transactional email. The sender must use a domain verified
  with the operator's mail provider.

Registry-owned OAuth applications, advertising conversion credentials and platform provider keys are
optional deployment capabilities. Their names and binding behavior are documented with their owning
subsystems. Credentials must stay in the deployment secret store. `TREG_PLATFORM_PROVIDERS` is the
shared-serving allow-list. Most providers also require a configured platform key. A live-verified
free endpoint declared `platform_auth: anonymous` needs only the allow-list because treg injects no
provider credential.

## Optional money admission

Root Web requests have separate, default-disabled [published Org pause, rate and concurrency
admission](../architecture/org-flow-control.md), enabled with `TREG_ORG_FLOW_CONTROL_ENABLED`
and read through the shared Redis destination. A full snapshot's non-null
`concurrency_limit.max_in_flight` controls shared root slots; `rate_limit:{rps,burst}` controls a
shared token bucket. Each null field removes that limit. Rate and concurrency checks are atomic:
a refusal consumes neither token nor slot. This does not change money admission or financial
cleanup below, and requires no additional setting or database migration.

The old `TREG_ORG_CONCURRENCY_*` configuration no longer controls concurrent request admission.
There is no automatic migration or static fallback. Review old values and already published
non-null rate and concurrency values before deploying the new behavior. Workers with the flag
already enabled will begin enforcing published rate values after this deployment. Any equivalent
dynamic rules must be separately approved, saved and published, and the shared flag enabled, through an
authorized rollout. Saving alone does not activate rules; this code change performs none of
those operational actions. See [concurrency migration](../architecture/org-concurrency.md#configuration-migration-and-rollback)
for the sequence and the fixed lease/retry behavior.

Restore published Redis keys before enabling workers; Redis connectivity, initialization and
timeout failures allow new root calls, including failures during combined rate/slot admission.
Missing keys clear local rules; malformed or conflicting publications retain the last valid rate
and concurrency limits and original pause expiry. Cache saturation allows uncached Orgs.
Per-worker refresh is eventual, not an all-worker acknowledgment. See the flow-control fragment
for recovery limits.
Bucket and slot keys are shared across configuration versions; rule updates do not reset live
owners or grant a fresh burst. Accepted rate tokens are not refunded on request failure,
cancellation or an ambiguous Redis reply. Disabling the flag bypasses new checks while accepted
requests finish their existing cleanup.

`TREG_MONEY_ADMISSION_ENABLED` defaults to `false`. Enabling it requires a shared Redis-compatible
store at `TREG_KV_URL`; all participating web and money-worker processes must use the same store
and rollout settings. No database migration or additional database privileges are required.

- `TREG_MONEY_ADMISSION_ORG_IDS` is a JSON array of positive Org IDs. A non-empty array enables
  admission only for those orgs; `[]` covers all orgs when the feature is enabled.
- `TREG_MONEY_ADMISSION_WAIT_S` accepts values greater than 0 through 120 seconds, defaults to
  5 seconds, and bounds the complete local/Redis acquisition attempt. Local-lock waiting uses
  the same budget as Redis waiting. Cleanup is separately bounded by the KV operations. Exhausting this
  budget rejects `reserve` before its funds transaction with HTTP 429 `money_admission_timeout`
  and a jittered `Retry-After`: an integer from `TREG_MONEY_ADMISSION_RETRY_AFTER_S` through twice
  that value (inclusive). The retry floor defaults to 1 second and accepts integers from 1 to 60.
  The old `TREG_ORG_CONCURRENCY_RETRY_AFTER_S` name remains a validation alias for this money
  setting; migrate any custom value to the money-specific name. It does not configure the
  published concurrency guard, whose contention guidance is fixed at one second.
  All other operations, including settlement and release, keep waiting without a database
  connection up to `max(TREG_MONEY_ADMISSION_WAIT_S, 2 * TREG_MONEY_ADMISSION_LEASE_S)`, never more
  than half of `TREG_HOLD_GRACE_S`, and only then fall back to the database path. Redis infrastructure errors retain
  the database fallback for every
  operation. A missing or non-positive
  org on those operations refuses before checkout, and that refusal is not an HTTP 429.
- `TREG_MONEY_ADMISSION_LEASE_S` defaults to 15 seconds. The owner renews while acquiring or using
  its leases and deletes only leases with its token. Expiry, lost renewal or a Redis outage can
  admit overlapping database transactions, so existing database locks remain authoritative.
  The lease starts on acquisition and renews at TTL/3; it need not exceed the pre-acquisition wait.
  Twice this value is also the settlement admission wait (up to half the hold grace), so raising
  it lengthens how long a settlement queues for the lease before falling back.

Allowing a 90–120 second admission wait does not guarantee an end-to-end request can wait that
long. It changes no default or running configuration. Budget for earlier request stages, every
admission scope (reserve and settle can each wait), upstream work, database execution, retries,
and cancellation/lease cleanup. Longer queues retain more pending requests and can increase
latency or client retries without increasing a hot org's transaction throughput.

Before opting into a longer wait, review these independent limits:

| Boundary | Existing behavior and compatibility |
|---|---|
| Redis commands and renewal | `infra.kv` keeps 1-second socket timeouts, 1.5-second lease attempts, and a 2.5-second per-key renewal/release retry budget. Renewal is also bounded by the earliest confirmed lease expiry. Optional counters/health probes retain their dedicated 200 ms deadline. None scales with the queue wait. |
| Plain call / upstream | `bootstrap._lifespan` configures `call_timeout_s` (default 180 seconds) on the upstream HTTP client. This is an HTTP I/O timeout, not a total request deadline or a budget for admission plus execution. |
| Hold age | `domain.money.hold_ttl_s` remains `call_timeout_s + hold_grace_s` (defaults: 180 + 60 = 240 seconds). Pre-reserve admission precedes hold creation, but later admission consumes hold age. A slow upstream plus 90–120 seconds of settlement waiting can exceed the stale-hold cutoff and race a reaper; pending async-task holds are excluded. Review hold lifetime before enabling longer waits. |
| CLI / local proxy | `cli._client` has a 190-second read timeout; `localproxy.treg_client` defaults to 120 seconds. A full 120-second queue leaves no local-proxy execution headroom; even the CLI can time out across reserve, upstream, and settlement waits. |
| MCP | `mcp._api` declares a 120-second HTTPX timeout, but its in-process `ASGITransport` does not enforce a network read deadline. This is not an end-to-end guarantee: the external MCP client and hosting path have independent limits. |
| Async settlement worker | `application.asynctasks.settle_due` has a 30-second processing deadline that starts before the claim and provider poll, including admission and settlement; its separate task-claim lease is 60 seconds. It cannot spend 90–120 seconds in admission within that deadline. Expanding it would require coordinating claim ownership and execution headroom. |
| Hub / other bounded callers | Hub recipe wall deadlines and per-call script limits include earlier execution; browser onboarding aborts after 60 seconds, and Arena applies its own run/attempt deadlines. They can cancel before the admission budget expires. |
| Rate limiter | `infra.upstream.limiter.DEFAULT_MAX_WAIT_MS` remains 2,000 ms, independent of money admission. |
| Render / edge / external clients | Their effective request, idle, and cancellation limits are outside this setting. Verify the actual hosting path and caller budget with execution headroom before rollout; accepting 120 in Settings does not certify a 120-second request through them. |

Deploy compatible code first with admission disabled to collect the same eligible-operation
baseline, then opt in a small Org set. Compare `money_admission_gauge` wait and end-to-end durations,
fallback/lease-loss counters, and the existing pool, money-operation and database-failure signals
under comparable load before expanding. Whole-session serialization can reduce hot-org throughput
even when it improves connection occupancy, so reduced lock-wait time alone is not rollout
acceptance. Defaults are acquisition bounds, not workload sizing recommendations. Disable the
feature to restore the original database admission path; mixed
settings or old writers weaken isolation but do not remove accounting's database protection.
There is no global money concurrency cap, and unrelated orgs can still collectively fill a pool.
See [money](../architecture/money.md#optional-admission-before-the-settlement-session) for exact
operation coverage and [data-model](../architecture/data-model.md#product-analytics-writer-analyticspy)
for aggregate boundaries and interpretation.

`tests/test_money_admission_postgres.py` uses real disposable loopback PostgreSQL and Redis via
`TREG_TEST_DB_URL` and `TREG_TEST_KV_URL`; without these it skips instead of contacting a service.
The PostgreSQL CI job supplies both services. `scripts/bench_money_admission.py` compares disabled
and enabled synthetic settlement workloads using the same variables, with database names required
to start with `treg_admission_test`. It resets that disposable schema. Its timings are local
synthetic evidence, not a production replay or a guarantee of deployed throughput. Benchmark
clients use separate Redis connections and local mutex maps within one event loop; this exercises
Redis contention without a shared local mutex hiding it, but is not an operating-system
multi-process load or failure test.

## Safe local mode

`single_user` and `single_user_token_file` support `curl {BASE}/selfhost.sh | sh`. The default serve
pre-phase idempotently creates the local identity and personal team and writes a stable token with
mode 0600. The token is re-minted only if its file is deleted.

This behavior is gated by `single_user_ok`, which requires both a local SQLite database and a
loopback `public_url`. A stray `TREG_SINGLE_USER=true` on a public PostgreSQL deployment therefore
does nothing.

`email_dev_mode` is similarly guarded. A login code is exposed only when the flag is true and the
database is local SQLite. Hosted deployments must still leave it false.

## Web service and generic Render example

The redesigned homepage ships to all homepage visitors; its rollback, like the Dashboard's, is a
code revert or a deploy of the previous build.

`GET /app`, the catalog pages and shared links all serve the Vite-built Vue application. There is
no frontend switch to configure; a Dashboard rollback is a deploy of the previous build. See
`frontend/README.md`.
The frontend is authored in `frontend/` within the same repository. `GET /` retains the existing
landing behavior. Dashboard assets, tutorials, agent files and installer assets ship with the wheel.
Hosted-page MP4 demos remain in Git checkout deployments but are excluded from published wheels and
source archives; a server installed from PyPI serves the product surfaces without those optional
marketing videos.

Run `bash scripts/build-dashboard.sh` before building a distributable Python package. Hatch's
build hook rejects a wheel or sdist without the dashboard entry and includes the generated assets;
editable installs remain Python-only. Node and npm are build tools, not runtime services.
`TREG_FRONTEND_DEV=true` serves the authored entry with Vite scripts on local port 5173 and is
accepted only with SQLite and a loopback public URL. `scripts/dev-local.sh up` manages both processes.
For browser previews from another device, build the dashboard and start or restart the local stack
with `TREG_FRONTEND_DEV=false`; compiled assets then use the same origin on port 18790.
The browser tests' server (`scripts/frontend-e2e-server.sh`) runs on `TREG_E2E_PORT`, which
`frontend/playwright.config.ts` sets to a port the OS reports free at the start of each run, so runs
in parallel worktrees or beside a `treg serve` proxy (default 18791) never share a port.

A feature flag set in the calling shell (`TREG_HUB_ENABLED=1 scripts/dev-local.sh up`) does not
reach the server on its own: the script starts the server inside a tmux session, and a tmux session
inherits the **tmux server's** environment, not the calling client's — the server would start and
answer every hub route `404` with no error. `dev-local.sh` expands a fixed passthrough list
(the hub, table and onboarding flags) in its own process and bakes the value into the command string
tmux runs; add a flag to that list to pass another one through. Keys never go through that list:
they go in `scripts/.dev-home/dev-keys.env`, which the server sources. The script's own three env vars
(`TREG_EMAIL_DEV_MODE`, `TREG_CONNECT_DEMO_ENABLED`, `TREG_DATABASE_URL`) are appended last so they
always win over anything passed through.

Per-IP limits need a client address the client cannot write. Behind Cloudflare set
`TREG_CLIENT_IP_HEADER=cf-connecting-ip`; behind other proxies set `TREG_TRUSTED_PROXY_HOPS` to the
number of proxies that append to `X-Forwarded-For` (default 1; 0 when clients connect directly). Each
process logs `event=client_ip_sources` once with the address headers its proxies deliver (names
and counts only), and `event=client_ip_header_missing` when the configured header does not arrive.
See [multi-tenancy](../architecture/multi-tenancy.md), "Client address".

A deployment that installs a policy extension into the server environment (an extra `pip install`
after the build) should set `TREG_REQUIRED_EXTENSIONS` to its entry-point name, so a build that
lost it fails to start instead of serving without it. See
[policy extensions](../architecture/policy-extensions.md).

[`deploy/render.example.yaml`](../../../deploy/render.example.yaml) is a generic self-hosting example.
It creates one web service and one PostgreSQL database, builds with
`bash scripts/build-web.sh` (frontend build followed by the locked Python install), runs
`python -m treg upgrade` before serving,
starts `python -m treg`, and checks `/meta`. Copy it into the operator's own deployment repository and
change resource names, region, plans, public URL and integrations.

### Build from the lock file

A deployment built from a checkout must install the dependency set `uv.lock` records, which is the
set CI tests (`uv sync --locked` in `.github/workflows/ci.yml`). `pip install ".[server]"` does not
read the lock: it resolves every open range in `pyproject.toml` afresh, so a build made after a
dependency publishes a breaking release ships that release with no commit in this repository, and
the suite that passed on the lock proves nothing about it. `--locked` refuses a stale lock instead
of resolving, which makes a forgotten `uv lock` a failed build rather than a silent drift; `--no-dev`
leaves the test tooling out; `--active` installs into the virtual environment the platform already
activated for the start command. Every process that shares a database (web service and scheduled
workers) must build the same way, or they run different dependency sets against one schema.

Render adds `uv` to its Python runtime when `uv.lock` is present at the service root, but at an
older version than `[tool.uv] required-version` accepts; set `UV_VERSION` on the service to a
version that satisfies it. Upgrading a dependency is a `uv lock --upgrade-package <name>` (or
`uv lock --upgrade`) commit that CI tests before it can reach a build.

Installing the published wheel (`pip install "tools-registry[server]"`) is a different path: a wheel
carries no lock, so that operator pins versions in their own requirements file.

The published source archive is built by Hatchling from the checkout. The Hatch target exclusions in
`pyproject.toml` keep local linked worktrees, root-level working plans and previews, evidence,
databases, environment files and hosted-page MP4 demos out of public artifacts. Release validation
inspects both archive contents; a clean Git diff alone is not sufficient.

The example is deliberately not the treg.to production Blueprint. The hosted topology and settings
are private operational state.

## Audit and archive back-pressure

`audit.record_call` enqueues rows off the request path. One batching writer per process drains them
through the background pool. `_MAX_PENDING` bounds queued rows; overload sheds audit work instead of
growing memory without limit. A rejected batch retries row by row so one malformed row costs only
that row. Unknown model fields are dropped with an error log rather than silently discarding the
whole record.

Archive recording has independent task-count and byte limits. `_MAX_PENDING_BYTES` bounds bodies
retained for database writes, and `archive_r2_max_pending_bytes` separately bounds object-store work.
These are admission budgets, not an RSS ceiling: SDK buffers, compression and mandatory terminal
evidence require additional memory headroom.

Spooled settlement evidence (`spooled_response`, proxy-model.md) writes large metered answers to
the process temp directory, not RAM. `TREG_SPOOL_BUDGET_BYTES` (default 512 MiB per process) bounds
that disk use, `TREG_SPOOL_MAX_BYTES` (64 MiB) one answer, `TREG_SPOOL_PARSE_CONCURRENCY` (2) the
transient parse memory (up to about three times the body each), and `TREG_SPOOL_DIR` overrides the directory.
Size the budget against the host's ephemeral disk: a platform that evicts an instance over its
local-storage allowance turns an oversized budget into a restart. Production observations and incident history live in
the private [database-capacity runbook](https://github.com/superdesigndev/treg-internal/blob/main/docs/production/database-capacity.md).

## Hosted feature switches

Several settings alter optional shared-service behavior without changing the underlying public
contract:

- `TREG_PLATFORM_DAILY_CAP_USD` is the default per-UTC-day shared-provider spend limit for a team
  that has not set its own; 0 (the default) is no limit. A team's own setting wins in either
  direction.
- `TREG_OAUTH_BILLED_PROVIDERS` names OAuth providers whose upstream bill lands on the registry
  operator. Own-app connections are never metered by this switch.
- `TREG_ROUTED_DISCOVERY` controls whether discovery leads callers to routed capability tools. It
  does not delete or make existing endpoint IDs uncallable.
- `TREG_PLATFORM_PROVIDERS` enables use of configured platform credentials.
- `TREG_OVERFLOW_MODE` and `TREG_OVERFLOW_DAILY_BUDGET_USD` control same-vendor overflow. See
  [capacity](capacity.md).
- `TREG_SEARCH_EXPERIMENT` (`off` | `shadow` | `interleave`) runs the
  [discovery experiment](../architecture/search-experiment.md) on the MCP search tools; it is also
  its kill switch. Needs `TREG_TYPESAFE_API_KEY`; `TREG_TYPESAFE_TIMEOUT_S` bounds what the judge
  may add to a search. Off by default, and off whenever the key is empty.
- `TREG_FIND_ENGINE` (`v1` | `v2` | `shadow`) picks what `/catalog/find` answers with (see
  [find](../architecture/find.md)); `shadow` serves v1 and logs v2 beside it, at a second judge
  request per find. Rolling back is setting it to `v1`.
- `TREG_FIND_EMBED_API_KEY` turns on v2's semantic channel; with the default OpenRouter
  `TREG_FIND_EMBED_URL` an empty value uses `TREG_PLATFORM_KEY_OPENROUTER`. Card vectors are cached in
  the archive's object store when archive R2 is on, else rebuilt by each process.

Exact treg.to values, funded accounts and rollout instructions live in the private
[provider-capacity runbook](https://github.com/superdesigndev/treg-internal/blob/main/docs/production/provider-capacity.md).

## Worker commands

`treg-worker` is a console script in the `[server]` extra. It hosts scheduled maintainer commands
without importing the heavy database stack into the light `treg` CLI.

- `treg-worker capacity sweep` collects provider capacity and writes snapshots.
- `treg-worker overflow verify` obtains route evidence and spends real money when configured.
- `treg-worker overflow sync` derives enabled overflow routes from current evidence.
- `treg-worker asynctasks settle` completes durable holds for asynchronous upstream operations.
- `treg-worker arena insights` folds new audit rows into the rolling Arena aggregate
  (`--max-seconds`, default 110, bounds one pass; schedule it every two minutes).
  It requires the archive object-store settings when R2 reads are enabled, opens the same client
  lifecycle as the web service, and flushes read analytics before exiting. The same scheduled
  command refreshes Web Arena's saved Battle totals when Web Arena is enabled and its reviewed
  benchmark is published; that full refresh is gated to once every 30 minutes.
- `treg-worker showcase activity` adds the new calls of each source an approved app uses (its team,
  or its API keys) to the /showcase leaderboard rows, one per UTC week (schedule it hourly: a newly
  approved app joins the activity boards at the next run; hosted only, so a self-hosted registry need
  not schedule it). Each run reads only calls after each source's cursor, over `callrecord`'s
  (org_id, id) or (org_id, api_key_id, id) index in small batches, up to a cap per run; a busy
  source's first four weeks are counted over several runs.
- `treg-worker rankings refresh` rebuilds the public /rankings boards from the per-day endpoint
  stats (`endpointdaystat`, which `treg-worker catalog stats` writes) and replaces the one saved
  row (schedule it hourly, as its own job: a failed run only leaves the rankings a run older, never
  the catalog's reliability numbers). Until its first run the page says the rankings are being
  prepared. Each run reads the counts of about thirty days and the first values of the last seven
  days' latency samples, never `callrecord`.
- `treg-worker media sweep` deletes public pictures no team, person or app names any more (schedule
  it daily; it reads only `img/` in the public bucket and skips files younger than a day).
- Pictures (avatars, /showcase icons and screenshots) are off until `TREG_MEDIA_STORE_ENDPOINT`, its
  two keys, `TREG_MEDIA_STAGING_BUCKET` and `TREG_MEDIA_PUBLIC_BUCKET` are set (SECURITY.md, "Picture
  storage"); pages then show initials. `TREG_MEDIA_BASE_URL` names the public bucket's domain; empty,
  treg serves the bucket at `/media-files/` itself. Switching it later needs no data change.
- `treg-worker catalog stats` folds new audit rows into per-endpoint, per-day reliability buckets
  (`--max-rows`, default 500,000, bounds the append pass; schedule it every few minutes), then
  repairs marked endpoint/day buckets. `--max-dirty-buckets` (20), `--max-repair-seconds` (60) and
  `--bucket-timeout-seconds` (10) bound repair attempts; zero buckets or zero repair seconds pauses
  repairs. The elapsed-time budget excludes append folding and queue maintenance. Each failed
  bucket rolls back and retries after a delay; JSON results include `repaired`, `repair_failed` and
  `repair_pending`. A persistently failing bucket must be investigated rather than retried with
  unlimited time. Web cache refreshes read only summaries: missing, incomplete or stale worker
  evidence gives unmeasured reliability, with existing cache entries expiring normally.
  Apply additive revision `0074` before the new writers. Retire all old audit-producing processes
  before starting historical correction, including Web processes and in-flight `asynctasks settle`
  jobs; wait for their work to finish, and require new writers for subsequent jobs. Web shutdown drains
  audit; settlement jobs do not guarantee an audit drain on exit, and audit remains best effort.
  The updated worker automatically queues legacy async buckets from `endpointdaystat` and repairs
  them across bounded runs. Setting a repair budget to zero pauses recounts only; ordinary folding
  still runs and can mark newly created buckets reconciled. Async observations remain unavailable
  until their applicable buckets are reconciled; no live aggregate fills this gap. If old writers
  run after reconciliation, their unmarked changes require another historical correction before
  trusting the summaries.
- `treg-worker catalog requeue-async-stats` previews the existing mutable async day buckets in the
  current UTC observation window. After all earlier writers have stopped, run it once with
  `--apply` to reset their reconciliation flags and enqueue full repairs atomically. Save the JSON
  output as the applied target manifest; apply selects targets afresh rather than applying an
  earlier preview. Keep the existing scheduled `catalog stats` command unchanged. The enqueue
  reads the small summary table; the normal worker later reads each marked
  source day within its existing budgets. There is no schema migration or automatic requeue.

  ```bash
  treg-worker catalog requeue-async-stats
  treg-worker catalog requeue-async-stats --apply > catalog-async-repair.json
  treg-worker catalog repair-status --manifest catalog-async-repair.json
  ```

  Use the unedited manifest against the same database. `repair-status` is read-only and returns
  exit code 0 when all still-in-window targets are reconciled and have no dirty marker, 1 while
  any target is pending or missing, and 2 for an invalid manifest. Expired targets are listed as
  `aged_out` rather than claimed as repaired. A default preview cannot be used as completion
  evidence. Appending new calls cannot satisfy a reset flag; only a full repair restores it.
  Repeating `--apply` starts another requeue, so do not add it to a recurring schedule. Active-day
  buckets can return to `pending` after reconciliation because new terminal results enqueue more
  corrections. That status combines unreconciled buckets and already-reconciled buckets with a
  marker. Initial repair coverage, writer versions and worker health must be verified before
  switching a reader; fresh same-day markers do not by themselves mean the initial repair failed.
  For the summary-only reader cutover, deploy the worker with successful-repair age advancement
  first and verify a successful repair pass, then deploy the Web reader. This change requires no
  migration or scheduled-command change. A retained marker can be normal on an active bucket:
  successful full repair advances its age bound to the source scan start, while failed or rolled
  back repairs leave it aging. Check `caught_up`, `repair_failed`, cursor age and marker age;
  `repair_pending > 0` alone does not indicate failure.

  The reader logs `endpoint stats fold unavailable` when it cannot publish trustworthy evidence.
  Consider a sustained-warning alert, scoped to the Web service, such as the following Loki
  condition with a ten-minute pending period (replace the selector with your service label):

  ```logql
  sum(rate({service_name="<web-service>"} |= "endpoint stats fold unavailable" [5m])) > 0
  ```

  Correlate this with repair failures and worker lag; it is an optional-statistics warning, not
  a failed provider call. This is alerting guidance, not a provisioned alert rule. There is no
  reader feature flag or live-query fallback. Rollback requires redeploying the previous known
  working Web build, which may restore expensive live aggregates; keep the audit writers and
  statistics worker running. No schema downgrade is required.
- `treg-worker spend rollup` folds new ledger settle entries into per-key, per-tool, per-day spend
  (`--max-rows`, default 50,000, bounds one pass; schedule it every few minutes, after the web
  service runs a build with revision 0070). It reads the primary database in small batches, and
  its first run backfills 31 days over as many runs as `--max-rows` needs. The Team page's "Spent"
  column and the Usage page's spend chart say spend history is being prepared until it has caught up once.
- `treg-worker usage rollup` folds new audit rows into per-member, per-tool, per-day call counts
  (`--max-rows`, default 500,000, bounds one pass; schedule it every few minutes, after the web
  service runs a build with revision 0088). It walks `callrecord` by primary key on the primary
  database, in short batches, and its first run backfills 31 days over as many runs as
  `--max-rows` needs. Until it has caught up once the Usage page says counts are being prepared and
  the observed-agents roster is empty. If it stops, the reports keep answering, reading the rows
  after its cursor live, and slow down as that tail grows. Deleting the cursor row
  (`callusagecursor`) returns the reports to the live audit-table aggregate; suspend the cron
  first to stay there, because its next run starts a new cursor, clears the old rollup rows in the
  same transaction and backfills again.
- `treg-worker jev xboost` runs the `/jev` launch-radar demo once a day: it calls treg's own `/call/` API
  with `TREG_JEV_TREG_TOKEN` (a member token of the demo team, so the spend is an ordinary bill) and jev
  through the Vercel AI Gateway (`TREG_AI_GATEWAY_API_KEY`), and stores the run under Ephemeral for the page.
  Both variables also belong on the web service, which needs them for the visitor judge endpoint.
- `treg-worker admin purge-evidence` blanks failed-call evidence past the 14-day retention window
  (`--batch-size`, default 5000, rows per transaction; schedule it daily). `GET /admin/errors` is
  read-only and already withholds evidence past the window, so an unscheduled purge keeps the old
  bytes in the database but never shows them.
- `treg-worker admin purge-ad-clicks` deletes email-to-click stash rows whose `updated_at` is past
  the 90-day window ([ads conversions](../architecture/ads-conversions.md)); schedule it daily.
  Without it, expired rows stop being read but stay in the database.
- `treg-worker hub check [--only <tool_id>] [--json]` runs every live [hub](../architecture/hub.md)
  tool's `check.json` once, as its maker (spends the maker's balance at step prices); a no-op with
  exit 0 when `hub_enabled` is off. Schedule it every 6 hours; health is derived from the last three
  runs.

The two analytics commands exist so that no web process aggregates the audit table beside the
money path; `callrecord` is read only through the persisted cursors they own. Workers call
read-only `verify_db()` before work and must run against a compatible schema. They need only the
credentials and configuration required by their job (the two analytics commands need only the
database URL). Hosting schedules, service wiring and manual production procedures belong in the
private deployment runbook.


Managed-key rollout uses revisions `0034` through `0036`. Apply the key controls and generation
before the separate concurrent Activity index build. The index migration allows 180 seconds for
lock waits and 600 seconds per statement, then restores 5/120 seconds. A failed concurrent build
can leave an invalid index; retrying `0036` removes and rebuilds only that invalid index.
Do not deploy server code older than `0034` after key disable or revoke state has been recorded.
The package version follows current main; this branch does not publish a release.

For hosted rollout, release and verify the compatible CLI before deploying the managed-key server.
The served installer installs from PyPI, so changing the server alone does not make `treg update`
install the new client. Old browser login and saved-token calls remain usable; affected email and
team-change requests receive an update instruction before their local state can be replaced.
