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
- **Alembic is authoritative:** migration scripts ship inside `src/treg/alembic/` in the wheel.
  `maintenance._alembic_config()` resolves that installed package resource, supplies the escaped
  configured URL, and runs Alembic in a worker thread.
- **The adoption floor is final.** An unstamped existing database must pass through release 0.14.x.
  Current code does not inspect, repair or stamp it.
- **Startup is read-only.** `verify_db()` checks the stamped revision through packaged Alembic
  metadata. It never creates tables, stamps versions or runs release tasks. Worker commands use the
  same check.
- **Schema changes are revision-only.** An autogenerate drift guard requires Alembic head and
  `SQLModel.metadata` to match exactly. `reset_db()` uses `create_all` only for fast test isolation
  and stamps that test schema directly at head.
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
fails the deploy at once. The one sanctioned exception to the 5 s cap is `CREATE INDEX CONCURRENTLY`
in its own revision: its lock blocks nobody while it waits, so such a revision owns its longer
timeouts and must detect and rebuild an invalid index left by interruption.

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

All pools use `pool_pre_ping=True`, `pool_recycle=300` and `pool_timeout=5`. A request that gets no
slot in time receives `503 {"treg_saturated": true}` with `Retry-After: 2` through
`bootstrap_handlers._pool_saturated`. A `/call/` holds no connection during the upstream round trip.

SQLite aliases all three makers to one engine. It has no pool to protect and file-level write locks
it cannot share, so separate engines would only manufacture lock failures. Tests pin routing rather
than SQLite isolation.

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

`pool_snapshot()` includes a separate `read` entry for a configured PostgreSQL datasource; SQLite
engines are omitted as for the primary. `connection_budget()` describes only the primary pools;
budget the read pool against its target database, including process count and deployment overlap.
If both URLs target the same server, add both budgets against that server's limit.
`dispose_engine()` also disposes the read engine. Schema upgrades, startup verification and test
schema resets continue to target the primary.

This datasource is opt-in infrastructure: no application query or worker currently uses it.
Adopting callers must tolerate replica lag and keep writes, cursor advancement and concurrency
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
- `promo_grant_micro` controls the once-per-verified-user signup grant. Zero pauses new automatic
  grants without changing existing balances.
- `blocked_email_domains` is the complete comma-separated blocklist. Empty blocks no domains. It is
  a sign-in and team-creation speed bump, not a substitute for suspending an existing abusive user.
- `run_proof` gates release of a shared key to the root-installed local runner. Empty refuses shared
  local runs while leaving owner-key runs available.
- `run_allowed_bins` is the command allowlist for server-side CLI execution.
- `run_rlimits`, `run_cpu_seconds` and `run_fsize_mb` bound server-run resource use where POSIX
  resource limits are available.
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

The redesigned homepage ships to all homepage visitors independently of Dashboard rollout.
Its rollback requires a code rollback/revert; the Dashboard master switch does not change it.

`GET /app` selects either the frozen legacy artifact or the Vite-built Vue application.
The rollout defaults to legacy. Set `TREG_DASHBOARD_ROLLOUT_ENABLED=true` with a JSON array in
`TREG_DASHBOARD_ROLLOUT_USER_IDS` for an account allowlist, then increase
`TREG_DASHBOARD_ROLLOUT_PERCENT` from zero. Disabling the master switch forces legacy, including
allowlisted accounts. Environment changes require restarting Web processes, not rebuilding assets.
Both frontends ship together. Anonymous catalog, shared-link and sign-in entries have no account
bucket and move only at 100%; below it, or with the switch off, they remain legacy.
See `frontend/README.md` for the full rollout and retirement contract.
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
evidence require additional memory headroom. Production observations and incident history live in
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
- `treg-worker catalog stats` folds new audit rows into per-endpoint, per-day reliability buckets
- `treg-worker jev xboost` runs the `/jev` launch-radar demo once a day: it calls treg's own `/call/` API
  with `TREG_JEV_TREG_TOKEN` (a member token of the demo team, so the spend is an ordinary bill) and jev
  through the Vercel AI Gateway (`TREG_AI_GATEWAY_API_KEY`), and stores the run under Ephemeral for the page.
  Both variables also belong on the web service, which needs them for the visitor judge endpoint.
  (`--max-rows`, default 500,000, bounds one pass; schedule it every few minutes). The catalog keeps
  computing observations live until this command has caught up once, so it can be scheduled after
  the application deploys, and a self-hosted registry that never schedules it loses nothing.

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
