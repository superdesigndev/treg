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
---

# Running & deploying

What every operator needs to run or self-host a registry. The hosted service's topology, live
settings, schedules and rollout state are private (`treg-internal`).

## Entry point (`__main__.py`)

`python -m treg upgrade` is the explicit release phase: `alembic upgrade head` for an empty or
stamped database, then the ordered, idempotent release tasks (never provisioning a local user). A
non-empty unstamped database is refused without writes: adoption ended with 0.14.x, so install
`tools-registry[server]==0.14.*`, run `python -m treg upgrade` there once, then continue. A database
stamped at a revision this build does not know is also refused, because an explicit upgrade may not
cross the rollback floor.

`python -m treg` runs the same upgrade phase, bootstraps the single local user when enabled, and
serves `treg.api:app` on `$PORT` (default 18790). Both pre-Uvicorn paths dispose the async engine
before their `asyncio.run()` loop closes, so the serving loop never receives connections bound to a
closed loop; calling `maintenance.upgrade()` directly does not dispose it. `python -m treg keygen`
prints a Fernet key for `TREG_SECRET_KEY`.

The lifespan and every worker command call read-only `verify_db()`: an unstamped database or a known
revision behind head is refused; an unknown newer revision (code older than schema) warns and serves,
because additive revisions tolerate rollback. Startup never creates tables, stamps or runs release
tasks. If `TREG_SECRET_KEY` is empty on a non-SQLite database, `verify_db()` raises.

## Schema upgrade safety

- **Rollback floors.** A revision marked `contract = True` is a hard floor: never run older code
  against it. `0034` (managed keys) is one because old code cannot enforce stored disable or revoke
  state. `0078` drops `onboardingprofile`; code older than it touches that table only while a
  `TREG_ONBOARDING_V2*` setting is on, so keep those off on any rollback past it.
- **Revision-only schema.** An autogenerate drift guard requires Alembic head and
  `SQLModel.metadata` to match exactly. `reset_db()` uses `create_all` only to build a missing test
  schema and stamps it at head.
- **Deploy schema first**, then application and worker code that expects the new revision. A
  platform whose scheduled workers update independently must sequence them accordingly.

**PostgreSQL lock queueing.** While an `ALTER TABLE` waits for its `ACCESS EXCLUSIVE` lock, every
later query on that table queues behind it. So migrations set a 5 s `lock_timeout`, the longest stall
a deploy may inflict, and no revision may raise it for an `ALTER`. A deploy waits longer by retrying
instead: `maintenance` re-runs `alembic upgrade head` up to `LOCK_RETRY_ATTEMPTS` times after a lock
timeout with a jittered pause so short transactions drain, and `env.py` commits each revision on its
own (`transaction_per_migration`) so a retry resumes at the revision that timed out. Any other error
fails the deploy at once.

- Hot-table ALTERs belong in their own revision, add only nullable columns without defaults
  (metadata-only in PostgreSQL), and leave backfills and `NOT NULL` to later steps.
- `CREATE/DROP INDEX CONCURRENTLY` permits ordinary reads and writes, so such a revision may use
  longer bounded timeouts; it restores the caller's timeouts, rebuilds an invalid index left by an
  interrupted build, and keeps a partial removal retryable. An index build on a large table may lift
  `statement_timeout` for that one statement (the lock timeout stays bounded); deploy it in a quiet
  window. On a busy database, an index removal can be done out of band with
  `DROP INDEX CONCURRENTLY IF EXISTS` first, so the deploy only records the revision.

## Database pools

`POOL_SPECS` in `infra/db.py` defines three PostgreSQL pools: `api` (request handlers), `admin`
(`/admin/*`) and `background` (audit, archive and other background work). Each class can exhaust
only its own slots. Minor pools deliberately have no overflow, because an unbounded escape hatch
defeats the bulkhead. A new class of work gets an appropriate pool, not only a module-local
semaphore. `tests/test_db_pool_isolation.py` pins the routing.

Every process opens its own pools. Compute the connection peak from pool capacities, process count
and the maximum overlap during a rolling deploy, and keep it below the database ceiling with
headroom. `TREG_DB_POOL_OVERRIDES` (`"admin.pool_size=4,background.pool_size=12"`) patches
`POOL_SPECS` at startup; invalid entries are logged and skipped, and zero or negative values must
never pass through because SQLAlchemy treats some as unlimited. `bootstrap.pool_gauge` emits
per-pool peak, capacity and headroom: resize from measurements, one pool at a time. A larger pool
does not repair a slow scan, lock queue or missing index.

A request that gets no slot within `pool_timeout` receives `503 {"treg_saturated": true}` with
`Retry-After: 2` (`bootstrap_handlers._pool_saturated`). SQLite aliases all three makers to one
engine: it has file-level write locks it cannot share, so separate engines would only manufacture
lock failures.

Web roles and workers emit local money-transaction diagnostics (`money_trace_gauge`,
`money_txn_slow`, `money_txn_end`) independently of the PostHog key; join `money_txn_*` records within
their timestamp intervals because connections are reused. Read the coverage limits in
[data-model](../architecture/data-model.md#product-analytics-writer-analyticspy) before
interpreting missing records.

A web worker whose event loop stops for seconds writes `loop_stall` to stderr with the loop thread's
stack while it is stopped, and `loop_stall_end` with the gap between heartbeats for every stall,
seen in progress or not (`infra/loop_stall.py`). A stack with `stack_reliable: false` was taken
after the GIL came back and does not name the holder.

### Dashboard reports

Team reports that aggregate whole windows of the audit and ledger tables (`/orgs/{id}/usage`,
`/orgs/{id}/usage/by-tag`, `/orgs/{id}/agents/observed`) take `get_report_session`, which opens
`read_session_maker` (the replica when configured, else the API pool). At most `_REPORT_SLOTS`
reports run per process; a report that cannot get a slot within `_REPORT_WAIT_S`, or exceeds
`_REPORT_STATEMENT_TIMEOUT_MS` per statement or `_REPORT_DEADLINE_S` overall, answers
`429 {"report_busy": "busy" | "timeout"}` with `Retry-After: 2`. That is not `treg_saturated` and
records no `db_pool` fault. The deadline is an `asyncio.timeout` around the dependency's yield:
FastAPI runs a dependency body and the endpoint in one task, and cancelling an in-flight asyncpg
query cancels it on the server.

Authorization finishes on `get_session` first (`require_member` commits), so that session holds no
connection while the report runs, and `/call/` keeps the rest of the API pool.
`tests/test_report_session.py` pins which routes take the report session. The dashboard runs per-tag
reports one at a time and retries a 429 with backoff. The gate bounds a slow report but cannot make
it fast; the usage counts read the daily usage rollup, not the audit table.

### Optional read replica

`TREG_READ_DATABASE_URL` adds a datasource exposed as `read_session_maker` (`sqlite+aiosqlite` or
`postgresql+asyncpg`; bare `postgres://` URLs normalize as for the primary). Empty, the read maker
aliases `session_maker`, writes included, with no extra pool. A configured URL always gets an
independent engine, even when it names the primary:

- PostgreSQL: a `read` pool (overridable as `read.pool_size` / `read.max_overflow`) whose
  connections set `default_transaction_read_only=on`.
- SQLite: `PRAGMA query_only=ON` on every new read connection. A plain URL still creates a missing
  file; `?mode=ro&uri=true` opens an existing file read-only. A separate in-memory URL starts empty.

These settings guard accidental writes; they are not an authorization boundary. Use a read-only
database role or filesystem controls. Failures propagate with no fallback to the primary. Setting a
URL does not establish replication; operators provision a compatible schema and data.

`/admin/errors` reads evidence from this datasource with a 3-second statement limit (it shares the
read pool with dashboard reports); authorization stays on the admin pool and releases its connection
first, and with no read URL it keeps using the admin pool. Further callers must tolerate replica lag
and keep writes, cursor advancement and concurrency control on the primary. `connection_budget()`
covers only primary pools: budget the read pool against its own target, and add both when they share
a server. Schema upgrades, startup verification and test resets target the primary.

## Configuration (`config.py`)

`Settings` uses the `TREG_` prefix, reads `.env`, and is cached by `get_settings()`. Settings whose
meaning is not obvious from the field:

- `public_url` builds OAuth callbacks and public links; self-hosters must set their canonical URL.
- `secret_key` empty creates an ephemeral key, fit only for local development.
- `admin_token` is the cross-tenant super-admin bearer; empty disables that path.
- Empty OAuth client credentials make a provider unconfigured instead of failing mid-consent.
- `paused_providers` / `paused_provider_messages` pause provider services this deployment cannot
  serve; malformed JSON fails at boot, and removing an id restores the provider with no reconnect
  (`interface/api.md` § `503 provider_paused`).
- `promo_grant_micro` = 0 pauses new signup grants without changing balances.
- `blocked_email_domains` is a sign-in and team-creation speed bump, not a substitute for suspending
  a user.
- `run_proof` empty refuses shared-key local runs while owner-key runs keep working.
- `connect_demo_enabled` gates the developer OAuth test page; leave it off on public deployments.
- `email_from` must use a domain verified with the operator's mail provider.
- `TREG_PLATFORM_PROVIDERS` is the shared-serving allow-list; most providers also need a platform
  key, but a `platform_auth: anonymous` endpoint needs only the allow-list.
- `TREG_PLATFORM_DAILY_CAP_USD` is the default per-UTC-day shared-provider limit for a team without
  its own (0 = none); the team's setting wins either way.
- `TREG_OAUTH_BILLED_PROVIDERS` names OAuth providers whose upstream bill lands on the operator;
  own-app connections are never metered by it.
- `TREG_ROUTED_DISCOVERY` steers discovery to routed tools without making endpoint ids uncallable.
- `TREG_SEARCH_EXPERIMENT` ([find](../architecture/find.md)) and
  `TREG_FIND_ENGINE` ([find](../architecture/find.md)) are their own kill switches.
- `TREG_REQUIRED_EXTENSIONS` names installed [policy extensions](../architecture/policy-extensions.md)
  so a build that lost one fails to start instead of serving without it.

Credentials stay in the deployment secret store.

**Public host aliases.** `TREG_PUBLIC_HOST_ALIASES` is the complete JSON list of extra hostnames
accepted for social-login callbacks, MCP hosts, origins and token audiences, and the hub self-relay
guard; setting it replaces the list. It does not change `TREG_PUBLIC_URL`, canonical links or
provider-connect callbacks. Register each alias's `/auth/google/callback` and
`/auth/github/callback` with the OAuth clients first, and list it in Turnstile's widget hostnames
(a non-empty `TREG_TURNSTILE_HOSTNAMES` overrides the server-side fallback and must list every
host). Apply alias configuration before deploying code that consumes it.

**Client address.** Per-IP limits need an address the client cannot write. Behind Cloudflare set
`TREG_CLIENT_IP_HEADER=cf-connecting-ip`; otherwise set `TREG_TRUSTED_PROXY_HOPS` to the number of
proxies appending to `X-Forwarded-For` (0 when clients connect directly). Each process logs
`event=client_ip_sources` once and `event=client_ip_header_missing` when the configured header does
not arrive ([multi-tenancy](../architecture/multi-tenancy.md), "Client address").

**Call events** ([call-events](../architecture/call-events.md)) need `TREG_CALL_EVENTS_URL` and
`TREG_CALL_EVENTS_HMAC_SECRET` on every process that serves calls or settles async tasks. Keep the
secret stable: changing it breaks every join across the change.

**Provider client codes.** A provider file's `client_header` is filled only once
`TREG_CLIENT_ID_SECRET` is set on the processes that relay calls. Turn it on after the provider is
ready to read the header, and keep the secret stable: changing it gives every team a new code at
every provider.

## Optional money admission

Org pause, rate and concurrency admission is separate: see
[org-flow-control](../architecture/org-flow-control.md) (its "Configuration, migration and
rollback" section covers enabling it, Redis failure behavior and the retired
`TREG_ORG_CONCURRENCY_*` settings).

`TREG_MONEY_ADMISSION_ENABLED` (default off) queues same-org balance work in a shared Redis lease
before the money session opens. Enabling it requires `TREG_KV_URL`, and every participating web
and money-worker process must use the same store and rollout settings; no migration is needed.
`TREG_MONEY_ADMISSION_ORG_IDS` selects an opt-in subset (`[]` = all orgs). Wait, lease and retry
semantics are in [money](../architecture/money.md#optional-admission-before-the-settlement-session).

`TREG_HOLDLESS_FREE_ORG_IDS` (default `[]`) lists orgs whose untagged free platform calls skip
the $0 hold, and with it money admission ([money](../architecture/money.md), "holdless"). Removing
an id restores the hold path for that org's new calls; no migration is needed.

Raising `TREG_MONEY_ADMISSION_WAIT_S` lengthens no other timeout: Redis command budgets, the
upstream `call_timeout_s`, hold age (`call_timeout_s + hold_grace_s`), CLI, local-proxy and MCP
client timeouts, the async settlement worker's processing deadline, hub and Arena deadlines and the
hosting edge all stay as they are, so a long queue can outlive its caller or its hold. Longer queues
hold more pending requests without adding a hot org's transaction throughput. Roll out by comparing
`money_admission_gauge` waits, fallback and lease-loss counters against the disabled baseline for a
small org set; disabling restores the database path, which remains authoritative throughout.

`tests/test_money_admission_postgres.py` and `scripts/bench_money_admission.py` need disposable
loopback services via `TREG_TEST_DB_URL` and `TREG_TEST_KV_URL` (skipped without them); the bench
requires database names starting `treg_admission_test` and resets that schema. Its timings are
local synthetic evidence, not deployed throughput.

## Safe local mode

`single_user` and `single_user_token_file` support `curl {BASE}/selfhost.sh | sh`: the serve
pre-phase creates the local identity and team and writes a stable 0600 token, re-minted only if
the file is deleted. Gated by `single_user_ok`, which requires a local SQLite database and a
loopback `public_url`, so a stray `TREG_SINGLE_USER=true` on a public deployment does nothing.
`email_dev_mode` exposes a login code only on local SQLite; hosted deployments leave it false.

## Web service and building

`GET /app`, the catalog pages and shared links serve the Vite-built Vue application; there is no
frontend switch, so a frontend rollback is a deploy of the previous build. Run
`bash scripts/build-dashboard.sh` before building a Python package: Hatch's build hook rejects a
wheel or sdist without the dashboard entry. Node and npm are build tools, not runtime services.
Hosted-page MP4 demos are excluded from wheels and sdists. The Hatch target exclusions in
`pyproject.toml` keep worktrees, plans, evidence, databases and env files out of public artifacts;
release validation inspects both archives, because a clean Git diff alone is not sufficient.

`TREG_FRONTEND_DEV=true` serves the authored entry through Vite on port 5173 and is accepted only
with SQLite and a loopback public URL (`scripts/dev-local.sh up` manages both). For previews from
another device, build the dashboard and run with `TREG_FRONTEND_DEV=false`. A feature flag set in the
calling shell reaches the dev server only through `dev-local.sh`'s passthrough list (tmux sessions
inherit the tmux server's environment); add a flag there to pass it through. The browser tests'
server runs on a port Playwright finds free each run (`TREG_E2E_PORT`).

[`deploy/render.example.yaml`](../../../deploy/render.example.yaml) is a generic self-hosting
example (one web service, one PostgreSQL database, build with `bash scripts/build-web.sh`, run
`python -m treg upgrade` before serving, health check `/meta`). It is not the hosted Blueprint.

### Build from the lock file

A deployment built from a checkout must install the dependency set `uv.lock` records, which is the
set CI tests (`uv sync --locked`). `pip install ".[server]"` does not read the lock: it resolves
every open range in `pyproject.toml` afresh, so a build made after a dependency publishes a breaking
release ships that release with no commit here, and the suite that passed on the lock proves nothing
about it. `--locked` refuses a stale lock instead of resolving, `--no-dev` leaves test tooling out,
and `--active` installs into the virtual environment the platform activated. Every process sharing a
database (web and scheduled workers) must build the same way. Render's bundled `uv` is older than
`[tool.uv] required-version` accepts; set `UV_VERSION` on the service. Upgrading a dependency is a
`uv lock --upgrade-package <name>` commit that CI tests first. The published wheel carries no lock,
so a wheel operator pins versions in their own requirements.

**Release order.** The served installer installs the CLI from PyPI, so a server change that needs a
new CLI ships only after that CLI is released.

## Audit, archive and spool back-pressure

`audit.record_call` enqueues rows off the request path; one batching writer per process drains them
through the background pool. `_MAX_PENDING` bounds queued rows, so overload sheds audit instead of
growing memory. A rejected batch retries row by row; unknown model fields are dropped with an error
log rather than discarding the record.

Archive recording has independent task-count and byte limits (`_MAX_PENDING_BYTES` for database
writes, `archive_r2_max_pending_bytes` for object-store work). These are admission budgets, not an
RSS ceiling: SDK buffers and compression need additional headroom.

Spooled settlement evidence (`spooled_response`, proxy-model.md) writes large metered answers to the
process temp directory: `TREG_SPOOL_BUDGET_BYTES` bounds disk use per process, `TREG_SPOOL_MAX_BYTES`
one answer, `TREG_SPOOL_PARSE_CONCURRENCY` transient parse memory (up to about three times the body
each), and `TREG_SPOOL_DIR` overrides the directory. Size the budget against the host's ephemeral
disk: a platform that evicts an instance over its local-storage allowance turns an oversized budget
into a restart.

## Worker commands

`treg-worker` (console script in the `[server]` extra) hosts scheduled commands without importing
the database stack into the light `treg` CLI. Each calls `verify_db()` first and needs only its own
job's credentials. `treg-worker --help` lists them; what follows is what a scheduler must know.

- **Analytics rollups.** `catalog stats`, `spend rollup` and `usage rollup` exist so no web process
  aggregates `callrecord` beside the money path; `callrecord` is read only through the cursors they
  own. Run them every few minutes. A first run backfills over as many runs as `--max-rows` needs.
  If `usage rollup` stops, reports read the tail after its cursor live and slow down as it grows;
  deleting `callusagecursor` returns reports to the live aggregate, but suspend the schedule first,
  because the next run clears the old rollup and backfills again.
- **`catalog stats`** also repairs marked endpoint/day buckets within `--max-dirty-buckets`,
  `--max-repair-seconds` and `--bucket-timeout-seconds` (zero pauses repairs, not folding). Web
  readers read only these summaries: missing or stale worker evidence shows reliability as
  unmeasured, and the reader logs `endpoint stats fold unavailable`. A persistently failing bucket
  needs investigation, not unlimited time. Writers older than the summary schema must be retired
  before a historical correction, or their unmarked changes need another one.
  `catalog requeue-async-stats --apply` is one-shot (never schedule it); save its output as the
  manifest for the read-only `catalog repair-status --manifest`, which exits 0 when every in-window
  target is reconciled, 1 while any is pending or missing, 2 for an invalid manifest. `repair_pending
  > 0` on active buckets is normal.
- **`rankings refresh`** rebuilds /rankings from `endpointdaystat`, as its own job so a failure only
  leaves rankings a run older.
- **`capacity sweep`**, **`overflow verify`** (spends real money) and **`overflow sync`**: see
  [capacity](capacity.md); verify must be followed by sync. The sweep reads
  `TREG_PLATFORM_OPENROUTER_MANAGEMENT_KEY` and `TREG_PLATFORM_FISHAUDIO_WORKSPACE_ID` when those
  balances should be observed.
- **`asynctasks settle`** completes durable holds for asynchronous upstream operations.
- **`arena insights`** needs the archive object-store settings when R2 reads are on.
- **`showcase activity`** is needed only where the showcase leaderboard is served.
- **`media sweep`** deletes unreferenced public pictures (only `img/`, older than a day). Pictures are
  off until `TREG_MEDIA_STORE_ENDPOINT`, its keys, `TREG_MEDIA_STAGING_BUCKET` and
  `TREG_MEDIA_PUBLIC_BUCKET` are set (SECURITY.md, "Picture storage"); `TREG_MEDIA_BASE_URL` empty
  means treg serves the bucket at `/media-files/`.
- **`admin purge-evidence`** and **`admin purge-ad-clicks`**: daily. Without them expired rows stop
  being shown or read but stay in the database.
- **`hub check`** runs each live [hub](../architecture/hub.md) tool's `check.json` as its maker
  (spending the maker's balance); health is derived from the last three runs.
- **`jev xboost`** needs `TREG_JEV_TREG_TOKEN` (a member token, so its spend is an ordinary bill) and
  `TREG_AI_GATEWAY_API_KEY`, also set on the web service for the visitor judge endpoint.
