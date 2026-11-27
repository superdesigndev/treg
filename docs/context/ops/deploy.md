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
  - scripts/bench_money_scaling.py
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
serves `treg.api:app` on `$PORT` (default 18790). On a database stamped at a revision this build
does not know (`maintenance.SchemaNewerThanBuild`) and that reads as a later revision of this
project (all digits and above this build's head, `db.stamped_newer_than_build`) it logs a warning,
skips the release tasks and serves anyway, as the ASGI entry (`verify_db`) does: a newer release
whose upgrade committed one revision and then failed on the next leaves such a stamp, and the old
release must still restart on it. Any other unknown stamp (another Alembic project's id, a typo)
is still refused. Both pre-Uvicorn paths dispose the async engine
before their `asyncio.run()` loop closes, so the serving loop never receives connections bound to a
closed loop; calling `maintenance.upgrade()` directly does not dispose it. `python -m treg keygen`
prints a Fernet key for `TREG_SECRET_KEY`.

Settings come from the environment and `.env`. With `TREG_AWS_SECRET_ID` set to a Secrets Manager
ARN, startup also reads that secret, a JSON object keyed by the same `TREG_*` names, and its values
win over the environment; a secret that cannot be read stops startup. The region is the ARN's, and
boto3 takes credentials from the standard `AWS_*` variables.

Stored credentials move to KMS in three deploys ([auth & secrets](../architecture/auth-secrets.md),
Encryption): `TREG_KMS_KEY_ID` on every process that reads credentials, then `TREG_KMS_ENCRYPT_NEW`
on all of them, then `treg-worker admin reencrypt-credentials` (`--dry-run` first) until it reports
nothing left to re-seal. The KMS key stays as long as any row is sealed with it.

Rotating `TREG_SECRET_KEY`: deploy `old,new` to every process that decrypts, so processes
overlapping a rolling deploy can open what updated ones write; then deploy `new,old` everywhere;
run `treg-worker admin rotate-fernet` until it reports nothing left (it covers Arena payloads and
any stored credential still on Fernet); then set every process to `new` alone. Keep the old key
out of the environment but retrievable for as long as a database backup from before the rotation
can still be restored.

The lifespan and every worker command call read-only `verify_db()`: an unstamped database or a known
revision behind head is refused; an unknown newer revision (code older than schema: all digits and
above this build's head) warns and serves, because additive revisions tolerate rollback; any other
unknown stamp is refused. Startup never creates tables, stamps or runs release
tasks. If `TREG_SECRET_KEY` is empty on a non-SQLite database, `verify_db()` raises.

## Schema upgrade safety

- **Rollback floors.** A revision marked `contract = True` is a hard floor: never run older code
  against it. `0034` (managed keys) is one because old code cannot enforce stored disable or revoke
  state. `0078` drops `onboardingprofile`; code older than it touches that table only while a
  `TREG_ONBOARDING_V2*` setting is on, so keep those off on any rollback past it. `0106` rebuilds
  `agentpayer` with a per-network key; code older than it writes that table only while
  `TREG_AGENT_PAYMENTS_ENABLED` is on, so keep it off on any rollback past it. `0114` removes
  queued call charges whose call is already settled, because a fold now applies every row it
  claims. With the database at `0114`, code may go back no further than the build whose charge
  writer takes back a re-queued settled call; that build and later ones run unchanged on what
  is left. An earlier writer can queue such a row again, and `0114`, already recorded, does not
  run on the next upgrade. If an earlier writer has run, then before a build with the new fold
  starts: stop writes, let transactions in flight end, and run the revision's statement by hand
  (`DELETE FROM callcharge WHERE EXISTS (SELECT 1 FROM ledgerentry WHERE ledgerentry.call_id =
  callcharge.call_id AND ledgerentry.org_id = callcharge.org_id AND ledgerentry.kind =
  'settle')`).
- **Rolling back past a revision.** A build cannot downgrade a revision it does not ship, and its
  `python -m treg upgrade` refuses a newer stamp, so a rollback deploy whose release phase runs
  `upgrade` fails there until the schema is back at that build's head. Downgrade first, with the
  newer build (a running instance of it, or a checkout of its commit pointed at the database):
  `alembic downgrade <older build's head>`. An upgrade that committed some revisions and then
  failed leaves the old build serving on a newer stamp (`verify_db` warns) with no running
  instance that has those downgrades; rolling forward resumes at the revision that failed. A
  revision that only drops a constraint the old code never relied on can stay applied: revert the
  code with a forward commit that keeps the revision file, never by deleting it.
- **Revision-only schema.** An autogenerate drift guard requires Alembic head and
  `SQLModel.metadata` to match exactly. `reset_db()` uses `create_all` only to build a missing test
  schema and stamps it at head.
- **Deploy schema first**, then application and worker code that expects the new revision. A
  platform whose scheduled workers update independently must sequence them accordingly.

**Release order with scheduled workers.** Every `treg-worker` command refuses a database behind its
build (`verify_db()`), so a scheduled service that redeploys on its own the moment a revision is
merged fails every run until the web upgrade has applied it, and keeps failing if that upgrade
refused. Turn automatic deploys off for scheduled services and redeploy them from the release
script, only after the web pre-deploy upgrade passed:

1. Merge; the web service builds and its pre-deploy step runs `python -m treg upgrade`.
2. If the upgrade refused (lock timeout budget spent, or the long-lock-holder check), stop: the
   database is unchanged or stamped part-way, and both the old web code and the old scheduled
   workers keep running against it. Fix the cause and redeploy the web service.
3. Once the upgrade passed and the new web code serves, redeploy each scheduled service.

**PostgreSQL lock queueing.** While an `ALTER TABLE` waits for its `ACCESS EXCLUSIVE` lock, every
later query on that table queues behind it. So migrations set `lock_timeout` to
`TREG_MIGRATION_LOCK_TIMEOUT_S` (2 s, at most 5 s), the longest stall a deploy may inflict, and no
revision may raise it for an `ALTER`. The value stays above PostgreSQL's default `deadlock_timeout`
(1 s): that is when a waiting `ALTER` makes an ordinary autovacuum on the table give up its lock,
and a wait of exactly 1 s can time out at that same instant. `env.py` sets the timeouts again after
every revision (`on_version_apply`), so an index revision that restores fixed values does not
lengthen the next revision's wait. A deploy waits longer by retrying instead: `maintenance` re-runs
`alembic upgrade head` up to `TREG_MIGRATION_LOCK_RETRY_ATTEMPTS` times (30) after a lock timeout,
within `TREG_MIGRATION_LOCK_RETRY_BUDGET_S` (600 s),
pausing a random time between half and all of `TREG_MIGRATION_LOCK_RETRY_PAUSE_S` (2 s) so short
transactions drain, and `env.py` commits each revision on its own (`transaction_per_migration`) so a
retry resumes at the revision that timed out. Any other error fails the deploy at once.

**Refusing behind a long lock holder.** Retrying helps only against holders that let go soon.
PostgreSQL never cancels an anti-wraparound autovacuum for a waiting `ALTER`, and an open
transaction keeps its locks until it ends; behind either, every attempt repeats the stall. So
before every attempt (the first and each retry), `maintenance._refuse_behind_long_lock_holder`
reads `pg_locks` and `pg_stat_activity` (read-only, PostgreSQL only) for the tables the pending
revisions name (string literals in their files, docstrings excluded), together with their
partitions (`pg_partition_tree`) and TOAST tables (`reltoastrelid`), and refuses at once, naming
the session, when an anti-wraparound autovacuum or a transaction older than
`TREG_MIGRATION_LOCK_HOLDER_MAX_XACT_S` (30 s) holds a conflicting lock on one of them. Only
conflicting modes count: a pending revision that plainly changes rows takes ROW EXCLUSIVE, so
readers and autovacuum do not stop it; any other revision is taken to need ACCESS EXCLUSIVE,
which every mode blocks. `maintenance._takes_access_exclusive` reads a revision fail-closed: it
is data-only only when every call in `upgrade()` and the module functions it calls is
`op.execute` of literal UPDATE/INSERT/DELETE/SELECT SQL, `op.bulk_insert`, a
`sa.text`/`sa.table`/`sa.column` expression or another module function, and no string there or
in a module constant names a DDL keyword (`DO` included) anywhere once SQL comments are removed.
Any other call (`op.get_bind()`, an imported helper, a class, `sa.Index(...).create`) means ACCESS
EXCLUSIVE. Table names are read from the revision's strings, docstrings and error messages left
out. Nothing ran after the check; wait for the holder (never cancel an anti-wraparound vacuum) or
end the transaction, then deploy again. To go ahead behind the bounded lock wait for this one
deploy, set `TREG_MIGRATION_LOCK_HOLDER_CHECK_SKIP` to the head revision id the refusal names: it
applies only while that revision is the head and stops by itself when a later one ships (a value
that no longer matches is ignored with a warning). `TREG_MIGRATION_LOCK_HOLDER_CHECK=false` still
turns the check off, for this one deploy only, then remove it; every upgrade logs a warning
while either bypass is in force. `pg_locks` and `pg_class` are readable by every role, but to a role without
`pg_read_all_stats` PostgreSQL shows another role's sessions, autovacuum included, with NULL
`backend_type`, `xact_start` and `query`. The check keeps those rows: one holding `SHARE UPDATE
EXCLUSIVE` on a table whose `age(relfrozenxid)` or `mxid_age(relminmxid)` has reached
`autovacuum_freeze_max_age` (or its multixact twin; a lower per-table reloption wins) is taken as
an anti-wraparound autovacuum and refused; any other hidden holder is logged as a warning naming
its pid, lock and table, and the upgrade continues, bounded by the lock wait. Granting
`pg_read_all_stats` to the migration role gives the check full sight.

**Retry time budget.** Beside the attempt count, retries stop once the next pause would pass
`TREG_MIGRATION_LOCK_RETRY_BUDGET_S` (600 s) since the first attempt, with an error naming the
setting; revisions committed before the timeout stay applied. The budget keeps the whole retry
loop well inside a platform's pre-deploy time limit even when each attempt runs long revisions
before it times out.

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
only its own slots: a pool's cap is `pool_size + max_overflow`, and both are bounded, so overflow
stays inside the bulkhead. A new class of work gets an appropriate pool, not only a module-local
semaphore. `tests/test_db_pool_isolation.py` pins the routing.

Every process opens its own pools. Compute the connection peak from pool capacities, process count
and the maximum overlap during a rolling deploy, and keep it below the database ceiling with
headroom. The two halves of a capacity cost differently. A pool opens connections lazily, not
at startup, and keeps up to `pool_size` of them open after they are returned: once a process has
used that many at once, it holds them while idle, and old processes still hold theirs during a
rolling deploy. Overflow connections close when returned, so they cost only while busy, at the
price of a new connection per burst. Moving capacity from `pool_size` to `max_overflow` lowers
what stays open after traffic subsides; the full-load peak is still `pool_size + max_overflow`
per pool. `connection_budget()` reports both, per process and per instance
(`per_instance`, `retained_per_instance`).

During a rolling deploy both generations hold connections, each with its own specs. Budget the
overlap as a sum, not a doubling: `old per-process figure x old process count + new per-process
figure x new process count`, with process count = instances x workers. The first deploy that
changes `TREG_DB_POOL_OVERRIDES` still runs the old processes on the old specs, so only later
deploys see the new figure on both sides. Use the capacity for the worst case and the retained
figure for what an idle overlap keeps open. `TREG_DB_POOL_OVERRIDES` (`"admin.pool_size=4,background.pool_size=12"`) patches
`POOL_SPECS` at startup; invalid entries are logged and skipped, and zero or negative values must
never pass through because SQLAlchemy treats some as unlimited. `bootstrap.pool_gauge` emits
per-pool peak, capacity and headroom: resize from measurements, one pool at a time. A larger pool
does not repair a slow scan, lock queue or missing index.

A connection the pool closes or terminates has its socket aborted afterwards if it is still open
(`_abort_socket_after_close`). asyncpg marks a connection closing before its graceful close waits
on anything, and its abort does nothing once that mark is set, so a close interrupted by a
cancellation (a request cancelled again while its checkout pre-ping fails) used to leave the
socket open with nothing owning it: the server kept the session `idle in transaction` after the
pre-ping's `BEGIN` until the process exited, holding one of the database's connection slots. Such
a session has no xid and no snapshot (`backend_xid` and `backend_xmin` are null), so it does not
hold back `CREATE INDEX CONCURRENTLY`; for a build that waits, look for conflicting locks on the
table or a session with an old `backend_xmin`. `tests/test_db_cancelled_close.py` pins the abort
against a real server.

`TREG_BACKGROUND_DATABASE_URL` sends the background pool through a transaction-pooling PgBouncer
in front of the same database. The API and admin pools, the read pool, migrations (`maintenance.py`
reads `_db_url`) and startup checks keep `TREG_DATABASE_URL`. Only the background pool may go
there: PgBouncer reports a full server pool only by closing the client connection after
`query_wait_timeout`, which asyncpg raises as `ConnectionDoesNotExistError`, the same error as a
dropped connection and far later than `pool_timeout`, so a request pool would wait instead of
answering the typed 503; and PgBouncer refuses the read pool's `default_transaction_read_only`
startup parameter. Startup (`verify_background_database`) refuses the URL when it reaches another
database, role, schema or server, or when asyncpg's prepared statements reach the server under
their own names: asyncpg prepares every statement, so the pooler must track prepared statements
(PgBouncer 1.21 or later, `max_prepared_statements` above 0). Behind it the pool's retained
connections are held by PgBouncer, so `connection_budget()` leaves the pool out: the database sees
the transactions in flight plus the server connections PgBouncer keeps until its idle timeout,
which count against the ceiling together with the direct pools. Keep the background cap times
every process that can run at once, both deploy generations included, below the pooler's server
pool, so a background checkout never waits there for a server. Nothing survives a transaction
there: session-level state (session advisory locks, a plain `SET`, `LISTEN`, temporary tables) is
allowed only where `tests/test_db_pooler.py` lists it.

A request that gets no slot within `pool_timeout` receives `503 {"treg_saturated": true}` with
`Retry-After: 2` (`bootstrap_handlers._pool_saturated`). The database refusing a new connection
(asyncpg `TooManyConnectionsError`, including "remaining connection slots are reserved", and
`CannotConnectNowError`, raw or inside a SQLAlchemy `OperationalError` or `InterfaceError`, the
`DB_BUSY_WRAPPERS`) is the same condition and gets the same response; `infra/db.py` `DB_BUSY_ERRORS`,
`is_db_busy` and `answers_db_busy` name it. Any other error type, even one whose cause is a refused
connection, stays a server error the call router stamps itself. A settle that meets it retries
once on a fresh session, like a pool timeout, because the error arises before any statement runs.
SQLite aliases all three makers to one
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
first, and with no read URL it keeps using the admin pool. `/admin/archive` and
`/admin/reconcile/*` read the same datasource through `get_admin_report_session`: one report per
process, a 60-second statement limit, the same fallback to the admin pool (where that limit is
what protects the primary). Further callers must tolerate replica lag
and keep writes, cursor advancement and concurrency control on the primary. `connection_budget()`
covers only primary pools: budget the read pool against its own target, and add both when they share
a server. Schema upgrades, startup verification and test resets target the primary.

### Connection budget

Per process, the primary connections are the sum over `api`, `admin` and `background` of
`pool_size + max_overflow` (`connection_budget()["per_process"]`). The ceiling to stay under is
`per_process * processes alive at once`, where processes alive is `WEB_CONCURRENCY * instances`, doubled
for the rolling-deploy overlap (`deploy_peak`), plus every worker, cron and admin process, all measured
against the database `max_connections` minus its reserved slots. Pools open lazily and a healthy
database holds far fewer than the capacity, but a slow database keeps connections checked out and the
peak approaches it. Do not shrink the `api` overflow to fit: a smaller api pool turns a short database
slowdown into saturation 503s. Before adding web workers or instances, put a connection pooler in
transaction mode between the services and the database (the code uses only `SET LOCAL` and
transaction-level advisory locks; asyncpg then needs its statement caches off). `python -m treg
upgrade` and Alembic use session-level settings and must keep a direct database URL, never the pooler's.

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
- `TREG_PARAM_CHECK` is `log` (the default; any value but `refuse` reads as it) or `refuse`. Log
  mode relays a catalog request that breaks a declared type, enum or range as before and logs a
  `catalog_parameter_would_refuse` line; read those lines before turning on `refuse`, which answers
  them 400 ([proxy model](../architecture/proxy-model.md#catalog-request-checks)).

Credentials stay in the deployment secret store.

**Public host aliases.** `TREG_PUBLIC_HOST_ALIASES` is the complete JSON list of extra hostnames
accepted for social-login callbacks, MCP hosts, origins and token audiences, the hub self-relay
guard, and the host of invite-email links and Stripe return URLs; setting it replaces the list.
Those links never take any other `Host`: `routers/auth_helpers.trusted_public_base` falls back to
`TREG_PUBLIC_URL`, so a forged header cannot send an invite token elsewhere. It does not change `TREG_PUBLIC_URL`, canonical links or
provider-connect callbacks. Register each alias's `/auth/google/callback` and
`/auth/github/callback` with the OAuth clients first, and list it in Turnstile's widget hostnames
(a non-empty `TREG_TURNSTILE_HOSTNAMES` overrides the server-side fallback and must list every
host). Apply alias configuration before deploying code that consumes it.

**Client address.** Per-IP limits need an address the client cannot write. Behind Cloudflare set
`TREG_CLIENT_IP_HEADER=cf-connecting-ip`; otherwise set `TREG_TRUSTED_PROXY_HOPS` to the number of
proxies appending to `X-Forwarded-For` (0 when clients connect directly). Each process logs
`event=client_ip_sources` once and `event=client_ip_header_missing` when the configured header does
not arrive ([multi-tenancy](../architecture/multi-tenancy.md), "Client address"). For an authorized
security test or a probe that must not be throttled, list its addresses in
`TREG_RATE_LIMIT_EXEMPT_IPS` for the engagement and remove them after; the setting is read at startup.

**Call events** ([call-events](../architecture/call-events.md)) need `TREG_CALL_EVENTS_URL` and
`TREG_CALL_EVENTS_HMAC_SECRET` on every process that serves calls or settles async tasks. Keep the
secret stable: changing it breaks every join across the change.

**Pairwise comparisons** ([feedback](../architecture/feedback.md), "Pairwise comparisons") run as
`treg-worker compare collect`, meant for an hourly cron job with the archive and platform-key
settings of the web service; every run also removes rows a team deletion missed, so that latency is
one run. Sampling is off unless `TREG_COMPARE_CAPABILITIES` lists a capability and
`TREG_COMPARE_MAX_PER_DAY` and `TREG_COMPARE_DAILY_USD` are above 0; the call path then marks sampled calls (`TREG_COMPARE_RATE`) in `TREG_KV_URL`'s store, which the
job and the web service must share. `TREG_COMPARE_LOOKBACK_HOURS` is at most 2, the marker's life,
so the job must run at least that often. Each run makes at most `TREG_COMPARE_MAX_PER_RUN` calls on
treg's key, sequentially; a day's spend is at most `TREG_COMPARE_DAILY_USD` of B's catalog prices,
no request above `TREG_COMPARE_MAX_B_MICRO`, only where that price is the request's maximum cost.
B is chosen per call among the capability's vendors and the calls bypass the shared provider
limiter: check every vendor of a capability for price and capacity before listing it, and exclude
any that must not be asked (`TREG_COMPARE_EXCLUDE`).
Each run prints one JSON line; `skip_reasons` counts, per reason, the candidates that stopped
before a call (`no_marker`, `a_not_archived`, `b_unavailable`, ...), so a run
that calls nobody says where its candidates stopped.
Migration 0137 moves the day's spend onto `compareattemptday.spent_micro` and fills each existing
day from its stored comparisons; a job of an earlier build adds nothing to it, so no earlier job
may run once that migration has: pause the cron before the web deploy that runs it and resume it
on the new build. The fill cannot see what a team deleted earlier that day cost: when the day's
`attempts` exceed its stored `callcomparison` rows, keep `TREG_COMPARE_MAX_PER_DAY=0` until the
next UTC day.
Rollback is `TREG_COMPARE_MAX_PER_DAY=0` with the cron kept: it then calls nobody and only
removes orphaned rows, which removing the cron would stop.

**Provider client codes.** A provider file's `client_header` is filled only once
`TREG_CLIENT_ID_SECRET` is set on the processes that relay calls. Turn it on after the provider is
ready to read the header, and keep the secret stable: changing it gives every team a new code at
every provider.

**Agent payments.** `TREG_AGENT_PAYMENTS_ENABLED` lets a caller with no treg credential pay a
catalog call with MPP or x402. It needs `TREG_AGENT_PAYMENTS_SECRET` (32+ characters), at least one
of `TREG_AGENT_PAYMENTS_MPP_ENABLED` / `TREG_AGENT_PAYMENTS_X402_ENABLED`, and each enabled rail's
recipient address; the defaults are testnets. The web app and `treg-worker agentpay reconcile`
refuse to start with one error naming every missing setting; other workers start without them, so
the payment secrets belong on those two, and the Stripe key also wherever
`treg-worker agentpay stripe-address` runs. A value of the wrong type still stops every process.
On mainnet the x402 facilitator is Coinbase's
(`TREG_AGENT_PAYMENTS_X402_FACILITATOR_URL=https://api.cdp.coinbase.com/platform/v2/x402`) and needs
`TREG_AGENT_PAYMENTS_X402_CDP_API_KEY_ID` and `_SECRET`, plus Base mainnet USDC as
`TREG_AGENT_PAYMENTS_X402_ASSET` with `TREG_AGENT_PAYMENTS_X402_ASSET_NAME="USD Coin"`.
`TREG_AGENT_PAYMENTS_SETTLEMENT` is `wallet` (the recipients are treg's own wallet) or `stripe` (they
are Stripe deposit addresses from `treg-worker agentpay stripe-address`; needs
`TREG_AGENT_PAYMENTS_STRIPE_SECRET_KEY`, the key of the Stripe account with stablecoins enabled, or
`TREG_STRIPE_SECRET_KEY` when that is the same account, and an explicit
`TREG_AGENT_PAYMENTS_TEMPO_CURRENCY`). Tempo's hosted RPC takes `TREG_AGENT_PAYMENTS_TEMPO_RPC_URL`
with `TREG_AGENT_PAYMENTS_TEMPO_RPC_API_KEY`. x402 on Base mainnet also needs its own
`TREG_AGENT_PAYMENTS_X402_RPC_URL` (a paid node: the reconciler reads it at
`TREG_AGENT_PAYMENTS_X402_BLOCK_TAG`, default `finalized`). Keyless traffic is capped by
`TREG_AGENT_PAYMENTS_MAX_PAYMENTS_PER_IP_MINUTE`, `TREG_AGENT_PAYMENTS_MAX_QUOTES_PER_IP_MINUTE`
(unpaid requests) and `TREG_AGENT_PAYMENTS_MAX_VERIFICATIONS_MINUTE` (payment and proof checks
that reach a chain or facilitator, across all clients). The x402 sign-in-with-x proof names `TREG_PUBLIC_URL`, so it must be the
origin agents call. See [agent payments](../architecture/agent-payments.md).

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

`TREG_MONEY_EMPTY_MARK_TTL_S` (default `0`, off) is the lifetime in seconds of the balance-empty
mark: a team refused for its balance is refused again from a mark in the key-value store, before
the admission lease and the database, until a credit clears the mark or it expires
([money](../architecture/money.md#the-balance-empty-mark)). It needs no migration and is safe to
turn off at any time: marks already written expire on their own. Use a few seconds (10 at
most); the value is also the longest a team can keep being refused after money arrives if a
clear is lost. Set it on
every process that moves money (web and workers) with the same `TREG_KV_URL`: a process with it
off credits a balance without clearing the mark, which then waits out its lifetime.

`TREG_HOLDLESS_FREE_ORG_IDS` (default `[]`) lists orgs whose untagged free platform calls skip
the $0 hold, and with it money admission ([money](../architecture/money.md), "holdless").
`TREG_HOLDLESS_FREE_ALL_ORGS` (default `false`) does the same for every org except those in
`TREG_HOLDLESS_FREE_EXCLUDED_ORG_IDS`, which never skip the hold. To put one org back on the hold
path, add it to `TREG_HOLDLESS_FREE_EXCLUDED_ORG_IDS`; removing it from the list works only while
the all-orgs switch is off. No migration is needed.

`TREG_MONEY_ALLOWANCE_ORG_IDS` (default `[]`) lists orgs whose eligible metered calls spend from
a standing allowance instead of one hold per call ([money](../architecture/money.md), "Allowance");
`TREG_MONEY_ALLOWANCE_ALL_ORGS=true` puts every org on it instead, and
`TREG_MONEY_ALLOWANCE_EXCLUDED_ORG_IDS` (default `[]`) keeps the orgs it lists off either way, so
one team is taken off without turning the rest back. Every listed org's calls that keep their own
hold are counted by reason in one `allowance_skips` line per reason a minute per process; the orgs
in `TREG_MONEY_ALLOWANCE_ORG_IDS` also get `allowance_skipped` lines of their own. The allowance needs `TREG_KV_URL` and PostgreSQL, and is ignored on SQLite. `TREG_MONEY_ALLOWANCE_CHUNK_MICRO` is what a refill brings the counter up
to, `TREG_MONEY_ALLOWANCE_FRESH_S` how long a refill keeps admitting calls, and
`TREG_MONEY_ALLOWANCE_LINGER_MS` how long finished calls wait to share one insert.
`TREG_MONEY_ALLOWANCE_REFILL_WINDOW_S` (default `0`, off) sizes a refill to what the team took
from its counter over that many seconds instead (at most twice what it took since the last
refill), never below the chunk; the allowance hold then carries at most
`TREG_MONEY_ALLOWANCE_REFILL_MAX_MICRO`, at most `TREG_MONEY_ALLOWANCE_REFILL_BALANCE_PERCENT` of
the team's balance and at most that share of what its daily cap has left, and a call on the
exact path that the balance alone cannot pay for gets the counter back first when it holds
enough to make up the difference. A window longer
than `TREG_MONEY_ALLOWANCE_FRESH_S` buys nothing more. Processes started with it back at `0`
refill to the chunk again, with nothing else to undo, and processes of different settings can
run side by side. With the switch off, removing an id
returns that org's new calls to the hold path at once; no migration is needed. With the switch
on, removing an id only stops its own skip lines: the org stays on, and counted, until it is excluded. The allowance's
background loop (`application/allowance_sweep.py`) applies queued charges to the ledger and
closes idle allowances. It runs in the `all` and `control` roles and is one more consumer of
the `background` pool, which is one connection larger per process for it. At most
`TREG_MONEY_ALLOWANCE_FOLD_SHARDS` processes (default 4) fold at once, each over its own share of
the teams; raise it when the queue's oldest charge keeps growing and the database has room, or
set `1` to go back to one process folding every team. The listing it folds by needs revision
`0126`, so a build from before it cannot be deployed again once `0126` has run (its `upgrade`
refuses the newer schema): withdraw the shares with `1`, or with a forward release that reverts
the code and keeps the revision; request processes do
not fold, so a deployment that turns the allowance on must run at least one process in one of
those roles. A queued charge is applied as it stands: a fold does not ask the ledger whether
its call was settled before. What makes that true is the charge writer, which takes back a
charge it re-queued for a settled call, so an upgrade to this behaviour from a build whose
writer did not do that is done in this order: stop every process of the earlier build and let
its transactions in flight end; run `python -m treg upgrade` (revision `0114` removes any such
row left queued); start the new build. The order matters: an earlier process still serving
calls after `0114` has run can queue another such row, the revision does not run a second
time, and the new build's fold would charge that call twice. Where every process already runs
a build with the writer's cleanup, no such row can be queued any more and the upgrade needs no
stop. The
allowance trusts the store to keep the writes it acknowledged for as long as the server
runs. A server that starts again (a restart that dropped its last writes, a restore from a
snapshot, a promoted replica) is recognised by its `run_id`: every counter built before is
discarded and rebuilt by the next refill. Nothing needs doing then, on two conditions.
Every process that serves calls runs a build with this check: an earlier one does not look
at the run and spends from a counter that went back until a newer process touches it. And
the store runs the counter's scripts, which call `INFO`: it must permit `INFO` inside a
script (some managed offerings refuse it there whatever the ACL grants) and allow it to the
store's user (ACL category `@dangerous`); where it does not, no counter operation answers
and every call takes the hold path. After a restart a team whose credit was all in its hold
is refused until the idle close returns it, since no refill can add the chunk a new counter
is built from. While processes of an earlier build share the store with newer ones, a
counter an earlier process builds is discarded by the next newer one that touches it: each
time, a chunk waits in the hold for the idle close. A freeze an earlier process wrote is
kept.

Not recognised: a server that goes back to older data WITHOUT starting again (a reload in
place; a server that kept running, took older data from another primary and became primary
again), and a restart while processes of an earlier build still serve calls. Either can
admit the same money twice, with no bound on amount or time while the allowance stays on,
and what a team's credit cannot cover is the platform's cost. After such an event, take every
team off: `TREG_MONEY_ALLOWANCE_ALL_ORGS=false` and `TREG_MONEY_ALLOWANCE_ORG_IDS=[]` together
(emptying the list alone leaves every team on while the switch is on). Put a team back, or turn
the switch on again, only when all of these can be seen:

- every process that serves calls has restarted with every team off (the settings are read
  per process, so one still running the old value keeps admitting from the counter);
- calls admitted before that have ended and their charges are written. A process that still
  runs logs when it could not confirm call charges and when they are written; the team having
  no `callcharge` rows shows only that the queue in the database is empty. A process that
  exited with unconfirmed charges took them with it, and no later log or row shows that:
  compare the team's calls with its `settle` entries for the interval;
- the team has no `hold` row with `allowance` set (the idle sweep closes it once no refill has
  stamped it for the hold lifetime plus its margin);
- the team's counter in the store has been retired or has expired. With every team off no
  call reaches a refill, so nothing else removes it; the sweep retires it after closing the
  hold, and that write is not retried. A counter still there when the team is back on is
  replaced by the first refill, which does not count what it shows.

When only some teams' counters were affected, list those teams in
`TREG_MONEY_ALLOWANCE_EXCLUDED_ORG_IDS` instead and remove them from
`TREG_MONEY_ALLOWANCE_ORG_IDS`, with the same restart of every process that serves calls and the
same checks for each before taking it out of the exclusion list. An exclusion wins over the
switch and the list, but a process built before the exclusion list existed reads only
`TREG_MONEY_ALLOWANCE_ORG_IDS`, and keeps admitting a team still named there.

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

`scripts/bench_money_scaling.py` takes the same two services and the same database-name guard. It
runs the reserve and the settle of each call from separate OS processes and prints, per process
count, one team's throughput, latency and database statements per call (BEGIN and COMMIT
included), then checks the ledger.
Use it to see whether a change lets a team's throughput grow with processes; its timings are
local synthetic evidence too.

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
new CLI ships only after that CLI is released. After a release is on PyPI, set `TREG_CLI_LATEST_VERSION` to it so
older CLIs are told to update (a Python version only: a 1.x value has every Python CLI update into the
Python CLI again); set `TREG_CLI_MIN_VERSION` when a server change stops an older CLI
from working.

**The binary CLI** is released by pushing a `v1.2.3` tag (`v1.3.0-beta.1` for beta), which runs
the `CLI release` workflow; it needs the `cli-release` environment's `MINISIGN_SECRET_KEY` (an
unencrypted `minisign -G -W` key) and the `TREG_UPDATE_KEY` variable (its public key line). A
channel whose version the operator names (`TREG_CLI_BETA_VERSION` for beta,
`TREG_CLI_STABLE_VERSION` for stable) moves only when the operator moves it; one
left empty, or at a 0.x value, follows the newest GitHub release
([cli-binary](../interface/cli-binary.md)). A download mirror needs the `cli-mirror` environment
(variables `R2_ENDPOINT` and `R2_BUCKET`, secrets `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` for
an S3-compatible bucket served publicly) and `TREG_CLI_MIRROR_BASE_URL` naming where that bucket's
`cli/v{version}` is served; copy any release published before with the `CLI mirror` workflow, and
name a version on a channel only once its copy is there.

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
  before a historical correction, or their unmarked changes need another one. `repair_pending > 0`
  on active buckets is normal.
- **`rankings refresh`** rebuilds /rankings from `endpointdaystat`, as its own job so a failure only
  leaves rankings a run older.
- **`capacity sweep`**, **`overflow verify`** (spends real money) and **`overflow sync`**: see
  [capacity](capacity.md); verify must be followed by sync. The sweep reads
  `TREG_PLATFORM_OPENROUTER_MANAGEMENT_KEY` and `TREG_PLATFORM_FISHAUDIO_WORKSPACE_ID` when those
  balances should be observed.
- **`asynctasks settle`** completes durable holds for asynchronous upstream operations. Where
  teams spend from an allowance it needs `TREG_KV_URL` too: a task that settles past its hold
  freezes its team's counter, and without the store the job cannot. Every worker command
  waits a bounded time before it exits for the freezes it owes (a hub check settles child calls
  too), waking any that were pausing before another attempt, and says on stderr how many it
  lost.
- **`billing reconcile`** checks the top-ups Stripe collected against the ledger
  ([money](../architecture/money.md), "Reconciliation against Stripe"); run it at least daily with an
  overlapping `--hours`. It exits 1 on a gap and posts to `TREG_OPS_SLACK_WEBHOOK_URL` when set.
- **`arena insights`** needs the archive object-store settings when R2 reads are on.
- **`showcase activity`** is needed only where the showcase leaderboard is served.
- **`media sweep`** deletes unreferenced public pictures (only `img/`, older than a day). Pictures are
  off until `TREG_MEDIA_STORE_ENDPOINT`, its keys, `TREG_MEDIA_STAGING_BUCKET` and
  `TREG_MEDIA_PUBLIC_BUCKET` are set (SECURITY.md, "Picture storage"); `TREG_MEDIA_BASE_URL` empty
  means treg serves the bucket at `/media-files/`.
- **`idempotency prune`**: hourly. Deletes expired retry answers for every team and drops a live
  row's copy of an answer the archive holds (`--skip-trim` keeps copies). On PostgreSQL a second
  instance answers `busy` and does nothing (a session advisory lock on a connection of its own, so
  a one-connection API pool is enough); on SQLite there is no such lock and the scheduler must keep
  to one run at a time. The trim stops after `--trim-max-seconds` (1500 s), also inside a page,
  and the next run resumes after the last row it decided (`Ephemeral` namespace `idempotency`,
  saved after every committed page, never by `--dry-run`; `--trim-after` overrides). Exits nonzero
  only on an incomplete prune; a negative `--trim-after` or a time budget that is not a finite
  positive number is refused before anything runs.
- **`archive retain`**: does nothing until `TREG_ARCHIVE_RETENTION_ENABLED=true`; `--dry-run` counts
  what it would delete either way. Each run stops at `TREG_ARCHIVE_RETENTION_MAX_SECONDS` and the
  next resumes from its cursor (also after `TREG_ARCHIVE_RETENTION_MAX_KEYS` deletions); a second
  instance answers `busy` and does nothing (a session advisory lock: connect the worker to
  PostgreSQL directly, not through a transaction-mode pooler); run one at a time, with the archive object-store settings
  when R2 writes are on. Objects of deleted rows go in a later run, once
  `TREG_ARCHIVE_RETENTION_OBJECT_GRACE_S` (900 s) has passed ([archive](../architecture/archive.md),
  "Row retention").
- **`admin purge-evidence`** and **`admin purge-ad-clicks`**: daily. Without them expired rows stop
  being shown or read but stay in the database.
- **`hub check`** runs each live [hub](../architecture/hub.md) tool's `check.json` as its maker
  (spending the maker's balance); health is derived from the last three runs.
- **`jev xboost`** needs `TREG_JEV_TREG_TOKEN` (a member token, so its spend is an ordinary bill) and
  `TREG_AI_GATEWAY_API_KEY`, also set on the web service for the visitor judge endpoint.
- **`agentpay reconcile`** resolves agent payments whose settlement went unanswered (older than
  `TREG_AGENT_PAYMENTS_RECONCILE_MIN_AGE_S`, default 300) from the chain, and with settlement
  `stripe` records settled payments to Stripe. Run it every few minutes when agent payments are on;
  it exits at once when they are off. **`agentpay stripe-address --network tempo|base`** creates one
  reusable Stripe crypto deposit address and prints it (setup for settlement `stripe`; run once per
  network).
