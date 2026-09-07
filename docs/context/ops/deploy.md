---
title: Running & deploying the server
status: shipped
sources:
  - pyproject.toml
  - src/treg/__main__.py
  - src/treg/maintenance.py
  - src/treg/alembic/env.py
  - src/treg/worker.py
  - src/treg/web/selfhost.sh
  - src/treg/config.py
  - src/treg/infra/db.py
  - src/treg/email.py
  - src/treg/audit.py
  - scripts/dev-local.sh
  - render.yaml
related:
  - ops/capacity.md
  - architecture/data-model.md
  - architecture/ads-conversions.md
  - foundation/charter.md
---

# Running & deploying

## Entry point (`__main__.py`)
`python -m treg upgrade` runs the explicit release phase. `maintenance._upgrade_schema()` runs
`alembic upgrade head` for an empty or stamped database. A non-empty unstamped database is now refused
without writes: adoption ended with 0.14.x, so the operator must install
`tools-registry[server]==0.14.*`, run `python -m treg upgrade` there once, then continue onward. A
database stamped at a revision this build does not know is also refused because explicit upgrade may
not cross the rollback floor. The ordered, idempotent release-task registry runs only after schema
success; it currently contains the provider companion-tool backfill and never provisions a local user.

The default `python -m treg` serve path runs that same upgrade phase and then
`api._bootstrap_single_user()`, then disposes the async engine inside the pre-serve event loop before calling
`uvicorn.run("treg.api:app", host="0.0.0.0", port=int($PORT or 18790))` (`--reload` optional). It honors
`$PORT` (Render/Heroku route + health-check that port). `python -m treg keygen` prints a Fernet key for
`TREG_SECRET_KEY` without importing the server maintenance stack. `treg.api:app` is
`bootstrap.create_app(role="all")`.

The FastAPI lifespan calls read-only `verify_db()`. It refuses an unstamped database or a known
revision behind head, directing raw-ASGI operators to `python -m treg upgrade`. An unknown-newer
revision means this code is older than the schema: startup warns and serves because additive revisions
tolerate rollback; a revision marked `contract = True` is the documented hard rollback floor. Every
role startup manifest is schema-write-free. Render runs upgrade as `preDeployCommand`; the existing
`startCommand: python -m treg` repeats the fast idempotent release phase for self-hosted parity.

Both pre-Uvicorn entry paths dispose the engine before their `asyncio.run()` loop closes: the default
serve path does so after single-user bootstrap, and `python -m treg upgrade` does so after all release
tasks. The next event loop therefore creates fresh pooled connections instead of receiving connections
bound to a closed maintenance loop. Calling `maintenance.upgrade()` directly does not dispose the engine.

## Schema upgrade safety
- **Alembic is authoritative:** migration scripts ship inside `src/treg/alembic/` in the wheel.
  `maintenance._alembic_config()` resolves that installed package resource, supplies the escaped
  configured URL, and runs Alembic in a worker thread so its internal event loop never nests inside
  the maintenance loop.
- **The adoption floor is final:** an unstamped existing database must pass through release 0.14.x.
  Current code does not inspect, repair, or stamp it.
- **Startup is read-only:** `verify_db()` checks the stamped revision through packaged Alembic metadata.
  It never creates tables, stamps versions, or runs release tasks. Worker commands use the same check.
- **Schema changes are revision-only:** an autogenerate drift guard requires Alembic head and
  `SQLModel.metadata` to match exactly. `reset_db()` uses `create_all` only for fast test isolation and
  stamps that test schema directly at head.
- **Fails loud on a missing key + real DB:** if `TREG_SECRET_KEY` is empty and `database_url` isn't
  SQLite, `verify_db` raises (an ephemeral key would make every stored secret undecryptable after a
  restart — silent total loss). On SQLite dev it only logs a warning.
- **Three pools, one database (the bulkhead).** `POOL_SPECS` in `infra/db.py` is the whole policy:

  | pool | maker | slots | serves |
  |---|---|---|---|
  | `api` | `session_maker` | 5 + 10 | every request handler, via `get_session` or directly |
  | `admin` | `admin_session_maker` | 3 + **0** | `/admin/*` only, via `get_admin_session` |
  | `background` | `background_session_maker` | 8 + **0** | audit (one batching writer), archive writes (two), ads worker, the observation reader, the error-evidence sweep |

  Each class of work can exhaust only its own slots. Before this there was ONE pool of 15, and on
  2026-09-03 a single admin browser tab polling `/admin/archive/panel` (every 5 s, no in-flight
  guard, fanning a `/admin/archive/keys` request out per endpoint row) held a third of it for two
  hours; ~10,300 real `/call/` requests were refused as saturated. Neither sizing nor a semaphore
  would have prevented it — a semaphore bounds only the module that remembers to take one
  (`audit.py` did, `archive.py` did not), while a pool bounds every module routed to it. Overflow
  is **0** on both minor pools for the same reason: it is the escape hatch a bulkhead must not have.

  **Every number above is PER PROCESS, and the reference deployment runs two.** Render sets
  `WEB_CONCURRENCY=2` on the web service's 2c-4g plan (not a dashboard variable - injected at
  runtime, and absent on the crons) and uvicorn honors it: the boot log shows `Started parent
  process` then two `Started server process` lines. Each worker opens its own three pools, runs its
  own copy of every in-process background task (ads, archive refresh, prune, the gauge), and a
  rolling deploy runs two instances for about a minute. So the budget is
  `per_process × 2 workers × 2 instances` against `max_connections` (103 on the 1c-2g plan), and
  `infra/db.connection_budget` logs it at boot:

  | specs | per process | per instance | deploy peak | 103? |
  |---|---|---|---|---|
  | code defaults 15 + 3 + 13 (until 2026-09-07) | 31 | 62 | **124** | over |
  | code defaults 15 + 3 + 8 (since 2026-09-07) | 26 | 52 | 104 | over by one |
  | dashboard override 15 + 2 + 4 | 21 | 42 | 84 | fits |

  That is the post-mortem of the 2026-09-04 defaults: `background = 13` did not overload the
  database, it opened 124 connections at every deploy and restart until the override cut it to 84.
  Every earlier passage in this file that multiplied by two instances only was counting half the
  connections. Any resize must clear the deploy-peak column first; within it there are 2 spare
  per process today (23 → 92). Batching the audit writer (4 → 1) and halving the archive semaphore
  (4 → 2) on 2026-09-07 cut the derived `background` from 13 to 8, so a pool of 6 now serves every
  consumer but two archive writers at once and still fits (15 + 2 + 6 = 23 → 92); the way to more
  is `WEB_CONCURRENCY=1`, a larger database plan, or a pooler -
  not a bigger number in the override.

  Two sizing rules, both learned by getting them wrong first:

  - **`background` is derived, not chosen.** `BACKGROUND_CONSUMERS` lists everything that can hold
    one of its slots at the same moment and the pool is `sum()` of it. Sizing below real demand
    makes the bounds fight: the loser waits `pool_timeout` then drops its row — for audit, the
    failure evidence a burst just produced. The first cut sized it as `audit(4) + archive(4)` and
    shipped a test asserting exactly that, which passed while four more consumers drew on the same
    slots (`adsconv` holds one across two Google round trips, the pruner across a whole sweep). Add
    a background consumer, add it to that dict.
  - **A handler that opens a SECOND session needs a spare slot.** At `admin=2`, `/admin/errors`
    nesting the retention sweep inside itself consumed the entire pool. That sweep now runs on
    `background` (it is a sweep, not a read) and is single-flighted so N concurrent readers cannot
    run N bulk UPDATEs; `admin` is 3.

  `get_admin_session` is a separate dependency callable rather than a flag because FastAPI caches
  dependencies per request BY IDENTITY: `require_superadmin` names the same one its handlers do, or
  an admin request would check out one connection from each of two pools.

- **`TREG_DB_POOL_OVERRIDES`** (`"admin.pool_size=4,background.pool_size=16"`) patches `POOL_SPECS`
  at startup. Pool sizing can only be validated in production — too small and real traffic gets
  503s, too large and the bulkhead is decorative, and no test distinguishes them — so a wrong number
  must be a dashboard edit, not a deploy. Unknown pools, unknown FIELD names, non-integers and
  out-of-range values are each logged and skipped: this knob gets reached for mid-incident, so a
  typo must neither stop the server booting nor pass silently and leave the operator believing they
  resized something. The range check is not pedantry — SQLAlchemy reads `pool_size=0` and
  `max_overflow=-1` as **unlimited**, and `pool_size=0` sets `_max_overflow=-1` too, so `-1` typed
  to mean "no overflow" would uncap connections against the ~100 ceiling (`max_connections` on the
  1c-2g plan reads **103**): the 2026-08-15 outage, entered through the knob added to prevent
  outages.

  **The live value outlives the code, so read it before trusting `POOL_SPECS`.** The reference
  deployment ran `admin.pool_size=2,background.pool_size=4` from before the 2026-09-04 bulkhead
  work until 2026-09-05 — pinning both minor pools BELOW the defaults that work had just raised
  (`admin` to 3, `background` to the derived 13), including the exact `admin=2` whose post-mortem
  is two bullets up. A `background` of 4 against 7 consumers needing 13 (8 since 2026-09-07) does
  not 503; it silently drops audit rows. The knob being a dashboard edit rather than a deploy is what makes it useful
  mid-incident and what lets it survive the fix. Today it reads
  `admin.pool_size=2,background.pool_size=4` - and those two entries are no longer "stale": with
  two uvicorn workers (§ above) they are what keeps a rolling deploy at 84 connections instead of
  124, so removing them is not a cleanup, it is the 2026-09-04 outage again. `api.pool_size=10` was
  added on 2026-09-05 and removed on 2026-09-06: against a database that is waiting on DISK (below),
  five more slots meant five more readers of the same cold pages, and the worst hour on record
  (2,136 pool faults at 11:00, on a third of the previous day's traffic) followed.
- **The pools are measured, not argued about: `db_pool_gauge`.** `bootstrap.pool_gauge` samples
  `infra/db.pool_snapshot()` once a second and emits one PostHog event a minute per instance:
  `<pool>_peak` (most connections that pool had checked out in the minute), `<pool>_capacity`
  (`pool_size + max_overflow`) and `<pool>_headroom`. Telemetry, not a database consumer, so it is
  not in `ROLE_BACKGROUND_TASKS` and runs in every role. Read it like this: a pool whose peak sits
  at capacity is one whose waiters are timing out (`api`: `503 treg_saturated`; `background`: an
  audit or archive row dropped after `pool_timeout`); a pool whose peak never nears capacity is
  holding connections nothing uses. **Resize from the gauge, never from the arithmetic** - the
  arithmetic got both minor pools wrong once each (above), and the 2026-09-05 `api` raise made the
  saturation it meant to fix worse. The protocol: one pool at a time, one override at a time, each
  setting across at least one full daily peak (the 01:00-04:00 UTC batch window), judged by the same
  hour on consecutive days on three numbers - db_pool faults, `/call/` 503 rate, and the gap between
  `tool_called` events and `callrecord` rows (dropped audit). A change that raises the 503 rate at
  equal traffic is reverted, not tuned around.

    ```
    SELECT toStartOfHour(timestamp) h, max(toFloat(properties.background_peak)) bg_peak,
           any(properties.background_capacity) bg_cap, max(toFloat(properties.api_peak)) api_peak
    FROM events WHERE event = 'db_pool_gauge' AND timestamp > now() - INTERVAL 2 DAY
    GROUP BY h ORDER BY h DESC
    ```
- **No statement timeout yet.** The pools bound how many connections a class of work can hold, not
  how long a query may run; `alembic/env.py` still has the only timeouts in the app. Adding per-pool
  `statement_timeout` is deliberately a SEPARATE change: it is a behavior change on every query,
  it can only be validated against production data volumes, and it would land on reads already
  known to be far over any sane bound (the 2026-09-03 admin report measured 133 s, and
  `admin_stats` / `admin_users` / `admin_tools` still read their tables unbounded). Splitting it
  out keeps "the bulkhead broke something" and "a timeout broke something" distinguishable.
- **Other Postgres pool hygiene:** `pool_pre_ping=True`, `pool_recycle=300`, and `pool_timeout=5` on
  every pool. A request that gets no slot in 5 s is answered `503 {"treg_saturated": true}` with
  `Retry-After: 2` (`bootstrap_handlers._pool_saturated`) instead of SQLAlchemy's default 30 s wait
  and an anonymous 500. A `/call/` holds no connection during its upstream round trip —
  `call_tool` commits before `relay()` (`require_member` commits before it returns the `Caller`, so
  the request-scoped session is idle by then); holding one there deadlocked 15 concurrent calls for
  30 s on 2026-08-24 (see [proxy-model](../architecture/proxy-model.md) § Connection discipline).
- **The bulkhead isolates CONNECTIONS, not the database's CPU** — and that is why "the API's 15
  slots are plenty" was wrong for a year. Three pools stop `admin` and `background` work from
  taking `api`'s slots; they do nothing about the fact that all three share ONE Postgres with one
  vCPU. A query that scans `callrecord` makes every ordinary 3 ms request query queue behind it,
  so `api` checkouts stretch from milliseconds to seconds and 15 slots empty. Measured 2026-09-05:
  db_pool faults ran all day (peak 714 in the 14:00 hour) while non-`/call/` routes sat at p50 3 ms
  and only ~22 requests were in flight — an order of magnitude below what the pool arithmetic says
  it should take, because the pool was never the constraint. The scans were: 2.94M-row / 1.68 GB
  `callrecord` taking 80,932 sequential scans for 27 BILLION tuples, plus 1.60 BILLION tuples read
  through `ix_callrecord_endpoint_id_id` because no index carried `created_at` (revision 0020
  adds the pairs). **Reach for a pool size only after ruling out a scan;** raising it buys headroom
  and hides the cause.

  That diagnosis was half right. Re-measured 2026-09-06 with wait events instead of response
  times: 88 % of active backends sat in `IO DataFileRead` / `IPC BufferIO` (waiting for a page, or
  for ANOTHER backend reading the same page), 18 of 879 samples were on CPU. The database is not
  CPU-bound; it is a 512 MB buffer cache in front of 35 GB, and the query holding the pool was
  not on `callrecord` at all: 674 of 879 active samples were `ledger.spent_today` on
  `ledgerentry`, the fail-closed daily cap that runs inside EVERY metered call's reserve
  transaction on an api-pool connection, scanning the whole platform's day because no index paired
  `org_id` with `created_at` (revision 0021 adds it; `ledgerentry` had read 6.5 BILLION heap
  blocks, four times `callrecord`). Whenever a large scan evicts the day's ledger pages - the
  30-day observation refresh, the `/billing` page's backward index walk, the per-call
  `idempotentcall` sweep, a concurrent index build - every in-flight `spent_today` stalls together
  for tens of seconds, and 20 slots are gone. 0021 fixed light orgs and `/billing` only: the two
  orgs writing half the day sit on every page of the day and the planner kept walking it, so
  revision 0022 moved the cap to a counter on the org row (one primary-key read) and 0023 gave the
  per-user cap its triple on `callrecord`. Two lessons: **sample `wait_event_type`, not
  latency**, and on a disk-bound database a bigger pool is more contention, not more throughput.
- **SQLite aliases all three to one engine.** It has no pool to protect and file-level write locks
  it cannot share, so three engines against one file would only manufacture "database is locked".
  Tests therefore pin the ROUTING (which maker each module reaches for), not the isolation.

## Config (`config.py`)
`Settings` (pydantic-settings, env prefix `TREG_`, reads `.env`), cached via `get_settings()`:
- `database_url` — default `sqlite+aiosqlite:///./treg.db` (SQLite dev, Postgres on Render, same code).
  A `field_validator` rewrites a bare `postgres://` / `postgresql://` URL → `postgresql+asyncpg://`, so
  Render's `fromDatabase`-injected URL works unedited (the async engine needs the asyncpg driver).
- `db_pool_overrides` — `"admin.pool_size=4,background.pool_size=12"`, patching `POOL_SPECS` at
  startup; empty uses the defaults. Bad entries are logged and skipped. See § Three pools above.
- `oauth_review_pending` — comma-separated provider-registry review keys. The reference deployment
  starts with `instagram-login,page-messages`. Remove `page-messages` after Page messaging approval;
  set an explicit empty value after direct Instagram approval. Restart the service after each change.
- `secret_key` — the Fernet key; empty → an ephemeral key is minted at startup (secrets won't survive a
  restart). See [auth-secrets](../architecture/auth-secrets.md).
- `public_url` — default `https://treg.to`; the reference deployment is cut over in STAGES —
  render.yaml deliberately still sets the OLD domain so the migration code deploys inert, and the
  flip (then email, then client releases) each land as their own change. Both prod hostnames stay
  valid forever (`config.PUBLIC_HOST_ALIASES`); marketing pages 301 old→canonical only. Verify each
  phase with `scripts/smoke-domain.sh pre|post`. Self-hosters set
  `TREG_PUBLIC_URL`. Used to build the OAuth callback URI.
- `api_token` — a bootstrap caller token (MVP leftover; per-user tokens are the real auth).
- `topup_min_usd`, `topup_default_usd`, `topup_presets`, `topup_bonus_tiers`, `topup_default_cap_usd`
  — Stripe top-up amounts in whole USD. The reference defaults are a $10 minimum, $10 first default,
  presets of $10, $50, $100 and $200 (plus "Other"), bonus tiers `{10: 0, 50: 5, 100: 10, 200: 15}`
  (percent of a MANUAL top-up granted as a separate `bonus` block), and a $50 cap on the preselected
  amount's climb after each payment; `billing_state` publishes presets, tiers and the per-org default
  to the dashboard.
- `admin_token` — the cross-tenant **super-admin** bearer (`TREG_ADMIN_TOKEN`); empty disables the env
  path (only `is_superadmin` users reach `/admin`). Keep it long + secret. See
  [super-admin](../architecture/super-admin.md).
- **Registry OAuth-marketplace apps** — treg's OWN approved OAuth clients, so a member can connect a
  provider without registering an app themselves. `google_client_id`/`_secret` backs both Google login
  AND the Google registry connects (Search Console / Analytics / Business Profile) via `/oauth/callback`
  — register both redirect URIs. Google **Ads** is special: `google_ads_client_id`/`_secret` is a
  DEDICATED client in its own Cloud project (a developer token is welded to one project), plus
  `google_ads_developer_token` (treg's token from OUR approved manager account, injected on every Ads
  call as a **platform binding** — see [proxy-model](../architecture/proxy-model.md)). The other
  providers each take a `<name>_client_id`/`_secret` pair: `linkedin_*`, `slack_*`, `x_*`, `tiktok_*`
  (separate sandbox vs prod app), `meta_*` (Facebook Pages, Meta Ads, and Instagram Page tools),
  and `instagram_*` (the separate Instagram App ID and secret for direct Instagram Login), and the
  Advertising OAuth platforms `microsoft_ads_*`, `snapchat_ads_*`, `tiktok_ads_*`, `pinterest_*` (all
  unset by default, so those providers ship **unconfigured** until a deployment registers a dev app).
  Empty for a provider ⇒ it lists as **unconfigured** rather than failing part-way through a consent.
- **Ad conversion tracking** — `google_ads_customer_id` (the target Ads account) and
  `ads_conv_refresh_token` (treg's OWN long-lived refresh token for the Data Manager uploader,
  minted once, out of band, by an operator via the OAuth playground with scope
  `https://www.googleapis.com/auth/datamanager`, then pasted in as a platform setting). **Both**
  must be set or the whole feature is off (`adsconv.enabled()`): the capture script is empty,
  attribution cookies are ignored, conversions are not queued, and the background uploader is not
  started. The refresh token is exchanged for an access token against the SAME client it was issued
  against — `google_ads_client_id`/`_secret` above, reused here rather than duplicated — never
  against `google_client_id` or a customer's own OAuth app; the exchanged access token is cached in
  process memory with its expiry so the uploader (which drains every ~300s) doesn't re-exchange on
  every pass. `google_ads_developer_token` is NOT part of this feature — Data Manager has no
  developer-token header at all; that setting only backs the read-side Ads catalog calls through
  `oauth_providers.GOOGLE_ADS`. For manager-account auth, set optional
  `google_ads_login_customer_id` to the manager MCC id; direct client auth leaves it empty. This is
  a PLATFORM credential, deliberately separate from a customer's `google-ads` OAuth connection
  (which grants only `adwords`, never `datamanager` — that scope would otherwise show up on every
  customer's consent screen for a permission only treg's own marketing uploader uses). Self-hosters
  and the test suite carry zero ad-conversion machinery by default. See
  [ads-conversions](../architecture/ads-conversions.md).
- **Landing live-wire (optional):** `demo_stripe_key` (`TREG_DEMO_STRIPE_KEY`, a Stripe **sandbox
  restricted** key) powers the landing sandbox's ONE real upstream call — a sandbox call to the exact
  seeded `stripe` tool relays for real with this key injected; the key exists in no sandbox org. Empty ⇒
  every sandbox call synthesizes, exactly as before the wire existed. `demo_stripe_webhook_secret`
  (`TREG_DEMO_STRIPE_WEBHOOK_SECRET`, `whsec_…`) signs the landing payments feed; empty ⇒ `POST
  /stripe/webhook` is off (`404`, so a deploy without it exposes no unauthenticated POST surface). See
  [api](../interface/api.md).
- **Frictionless local mode** (`single_user`, `single_user_token_file`): `curl {BASE}/selfhost.sh | sh`
  brings up a registry on the caller's own machine that they are **already signed into** — no account,
  email or password. The default serve pre-phase calls `_bootstrap_single_user()`, which idempotently creates the
  `you@local.treg` owner + `personal` team and writes the token (0600) for the installer to hand to the
  CLI; the token is **stable across restarts** (re-minted only if the file is deleted), and `dashboard()`
  attaches a session when there is none. It adopts an org **only through a membership this identity
  already has** — never by looking one up by the slug `personal`. On a database that is not fresh (a
  restored dump, a hosted registry run locally) that lookup joined a team belonging to someone else **as
  owner**, and an owner is exempt from every ACL (round-4 finding #5). A new team therefore takes a free
  slug via `_unique_slug` (`personal-2`, …) instead of colliding; a fresh box still gets the clean name.
  Gated by **`single_user_ok`**, which mirrors `expose_dev_code`:
  it demands a **local sqlite** DB **and** a **loopback `public_url`**, so a stray `TREG_SINGLE_USER=true`
  on a real deploy does nothing. A no-login dashboard on a public host would hand over the whole registry.
- `github_client_id` / `github_client_secret` / `session_secret` — GitHub OAuth login for the dashboard
  (`TREG_GITHUB_*`, `TREG_SESSION_SECRET`); empty hides the GitHub button. Callback must be
  `<public_url>/auth/github/callback`. See [dashboard](../interface/dashboard.md).
- `email_dev_mode` — default **False** (returning the OTP in the response is an unauth account-takeover
  vector in prod). When true, `/auth/email/start` returns + logs the 6-digit code so dummy emails are
  testable without a mail sender; when false, the code is **emailed via Resend** (see below). Enable it
  **only** on a trusted dev box (`TREG_EMAIL_DEV_MODE=true`); real deploys must not. **Double guard:** the
  code is exposed only through `Settings.expose_dev_code`, which requires `email_dev_mode` **and** a
  **local sqlite** `database_url` — so even a stray `TREG_EMAIL_DEV_MODE=true` on Postgres (a real deploy)
  can never leak a login code.
- `blocked_email_domains` (`TREG_BLOCKED_EMAIL_DOMAINS`, default empty) - the WHOLE email-domain
  blocklist: comma-separated domains refused at every identity door and at both team-creating doors
  (`POST /users` and `POST /orgs`). There is no list in the code, so **this variable is the only
  thing standing between a bulk-registration run and the promo grant** — an empty value blocks
  nothing. Example: `example-one.io,example-two.net`. Case-insensitive; a listed domain also blocks
  its subdomains; a leading `@` or `.` and surrounding whitespace are tolerated; a dotless entry
  (`com`) is ignored so one typo cannot refuse every address on earth. Edit it in the Render
  dashboard the moment a new domain appears; changing it restarts the service. Existing accounts on
  a listed domain must be suspended separately (`/admin`); the list only stops new sessions and new
  teams, not tokens already issued. Each block writes one
  `event=signup_blocked_domain door=... domain=...` log line, so a burst is countable. See
  [multi-tenancy](../architecture/multi-tenancy.md).
- `run_proof` (`TREG_RUN_PROOF`) — the **isolated-runner proof** for `treg run --local`. A local run whose
  grant would return a secret the caller does **not** own (a shared-key tool a member may run but not read)
  must present this value in the `X-Treg-Run-Proof` header — a value held **only** by the root-installed
  `treg-run` runner, never by the member. Empty = shared-key local runs are refused (runs against a
  secret the caller owns still work). To enable shared local runs, set it on the server **and** install it
  via `treg setup-local-run --run-proof`. See [local-run](../architecture/local-run.md).
- `run_allowed_bins` (`TREG_RUN_ALLOWED_BINS`) — the **command allow-list** for `treg run --server`. The
  server executes an entrypoint only if it is a catalog-known CLI (stripe/gh/vercel/…) **or** named in this
  comma-separated list — so a member cannot ask the server to run `bash`/`python` and execute arbitrary
  code as the server user. Extend it as new CLIs are approved.
- `run_rlimits` (`TREG_RUN_RLIMITS`, default **true**) + `run_cpu_seconds` (`TREG_RUN_CPU_SECONDS`,
  default 300) + `run_fsize_mb` (`TREG_RUN_FSIZE_MB`, default 100) — the **resource-limit sandbox** for
  `treg run --server` (`runner._rlimit_preexec`): every run's child gets a CPU-seconds cap, a max-file-size
  cap, and core dumps disabled, so a runaway/hostile CLI can't exhaust the host. A no-op where the POSIX
  `resource` module is unavailable. No address-space/process-count cap (would break Go CLIs / is per-uid).
  This is the **DoS** half of the sandbox; full filesystem/network isolation needs a **container deploy**
  (a planned follow-up — the current Render runtime is the native Python one, which can't run it).
- `proxy_ssrf_check` (`TREG_PROXY_SSRF_CHECK`) — the **call-time SSRF guard** on the proxy: resolve the
  upstream host and refuse an internal/private target. **On by default**; only the test suite disables it
  (its upstream is an in-process ASGI transport, not real DNS).
- `claude_connector_enabled` (`TREG_CLAUDE_CONNECTOR_ENABLED`) — enables the catalog-only Claude
  connector at `/mcp/v2/`. The default is false. Keep it false during normal deployment. Set it to
  true for a controlled test window. Set it back to false to disable V2 without changing the existing
  `/mcp/` connector.
- `connect_demo_enabled` (`TREG_CONNECT_DEMO_ENABLED`) — enables the developer OAuth test page and
  callback at `/connect-demo`. The default is false, and both routes return 404 when it is false. The
  local development script enables it. Staging can enable it explicitly for controlled tests; leave
  it false in production.
- `intercom_app_id` / `intercom_secret` (`TREG_INTERCOM_APP_ID` / `TREG_INTERCOM_SECRET`) — support
  chat via the **Intercom Messenger** (treg's own workspace). Empty app_id = the widget is OFF
  everywhere — `/meta`
  serves `""` and every page's loader stays inert, so self-hosters ship no third-party chat. The
  app_id is public; the secret signs `user_hash` (identity verification) and never reaches the browser.
- `resend_api_key` / `email_from` — transactional email via **Resend** (`src/treg/email.py`): the OTP
  sign-in code + team invitations. Empty key = no real send (dev mode still returns the code; prod
  without a key silently skips — best-effort, never breaks the flow). `email_from` **must** be a
  Resend-verified domain — `treg.to` is **verified** (DKIM + SPF records on the treg.to zone;
  `treg.superdesign.dev` stays verified as a fallback), so the default is `no-reply@treg.to`. **On Render:** set
  `TREG_RESEND_API_KEY`, optionally `TREG_EMAIL_FROM`, and leave `TREG_EMAIL_DEV_MODE` false.

## Web dashboard
`GET /` serves the single-file dashboard (`src/treg/web/index.html`) same-origin; the whole `web/` dir
(incl. `tutorial.js` at `/tutorial.js` and `tutorial.html` at `/tutorial`) ships in the wheel because it
lives inside the `treg` package (the `packages` inclusion covers non-.py assets). See
[dashboard](../interface/dashboard.md).

## Current hosting (shipped)
Deployed on **Render** at `https://treg.to` (with `treg.superdesign.dev` attached as the legacy
alias — never remove it: installed CLIs/skills point there with tokens) via the Blueprint below (one web service + a
managed Postgres). The Fernet key lives only in the service's environment — **back it up**; losing it
makes every stored secret unrecoverable. For local dev, `scripts/dev-local.sh up` runs the server with
its own sqlite DB and email dev mode.

## Render (Blueprint)
`render.yaml` at the repo root deploys the whole thing as **one web service + a managed Postgres**
(region `oregon`): `buildCommand: pip install ".[server]"` — the base install is the **CLI only**, so the
server deploy needs the `[server]` extra (FastAPI/DB/crypto); the wheel ships every web asset via the package.
`preDeployCommand: python -m treg upgrade`, `startCommand: python -m treg`, and health check on `/meta`.
The pre-deploy migration must succeed before Render replaces the serving instance; the start command
repeats the idempotent phase for installations without a pre-deploy facility. The DB URL is auto-wired via `fromDatabase`
(config's validator adds the asyncpg driver). Secrets are **dashboard-managed** (`sync: false` — the
Fernet key, session/admin tokens, GitHub OAuth pair, Resend key, and the optional landing live-wire pair
`TREG_DEMO_STRIPE_KEY` + `TREG_DEMO_STRIPE_WEBHOOK_SECRET`); `TREG_PUBLIC_URL`,
`TREG_EMAIL_DEV_MODE=false`, and `TREG_EMAIL_FROM` are set inline. `asyncpg` is a dependency (Postgres
async driver, alongside `aiosqlite`).

**Fresh-Postgres verified:** an empty database runs the pure Alembic chain to head. Existing unstamped
deployments are refused and must pass through the 0.14.x adoption release.
**Timestamps must be naive UTC:** the datetime columns
are `TIMESTAMP WITHOUT TIME ZONE`, and asyncpg rejects tz-aware values, so `models._now()` returns naive
UTC (SQLite is lax and hid this; it only bites on Postgres — the deploy target).

**Migration portability.** Every production schema change is an Alembic revision exercised on SQLite
and in the serial Postgres CI migration set. `env.py` bounds Postgres lock and statement wait time so
a contended migration fails before it queues the serving database behind DDL.

**Audit back-pressure (`audit.py`).** Audit rows are written off the request path (fire-and-forget):
`record_call` appends to an in-process queue and ONE writer task per process drains it `_BATCH` rows
per INSERT on a **background**-pool connection (since 2026-09-07; before that four writers each took
one row per session, which cost four slots per process for millisecond inserts). A burst can therefore
never starve real requests, only other background work. Two limits still apply: a loop-bound semaphore
holds the writer to `_MAX_CONCURRENT_WRITES` (1), which keeps `drain()` deterministic on SQLite, where
all three makers share one engine, and under an extreme burst `_enqueue` **sheds** load — it drops any
audit row past `_MAX_PENDING` queued rows rather than let the queue grow without bound. A batch the
database refuses is retried row by row, so one bad row costs one row. Audit must never OOM or wedge the
server. Shedding is the *only* loss that should ever happen: `record_call` splats its telemetry dict
into `CallRecord(**fields)`, so a key with no matching column used to raise inside `_write`, where the
except swallowed it, and the whole row disappeared — a telemetry field deployed one commit ahead of its
migration would have silently emptied the table. `_known_fields` now drops unknown keys (logging which
ones), and `_write`'s swallow logs the traceback at **ERROR** — as does the back-pressure shed. The level
is the whole point: `FaultCaptureHandler` starts at ERROR, so at WARNING a lost row reached container
stdout and nothing else, and the only way to learn audit was dropping was to already suspect it and go
grep. **A quiet audit table is now a bug you can alert on**, not one you find out about weeks later.

**Archive memory bound (`archive.py`).** Each pending archive recording holds its `body` bytes in a
task closure — up to `_MAX_PENDING` (512) tasks × `archive_max_body_bytes` (2 MB) = 1 GB worst case.
After #363 reduced `_MAX_CONCURRENT_WRITES` from 4 to 2, backlog built faster than it drained under
heavy `/call` + MCP traffic, and the 2026-09-07T00:43:06Z OOM killed the web service at 4 GB.
`_MAX_PENDING_BYTES` (256 MB) now caps total body bytes in pending work: `record()` sheds when
EITHER the task count OR the bytes threshold is exceeded. The done callback releases bytes when a
task completes; a regression test pins the bound.

The proxy is thin and IO-bound (a relay, low CPU/memory), so cheap machines scale it.

## The per-org daily spend cap

`TREG_PLATFORM_DAILY_CAP_USD` (default **$100**) is the per-org, per-UTC-day ceiling on tier-4 spend
*and* the ceiling a team may raise its own `Org.daily_cap_micro` to. The effective cap is the lower of
the two (`api._effective_daily_cap`).

It was $5, which is a sane blast radius for one team and fatal for a platform running its whole
customer base through a single org — they hit it on day one. Raising it per-team is now a `PATCH
/orgs/{id}/settings` a team can make itself up to the ceiling, so onboarding a high-volume builder is
a conversation rather than an env-var edit that lifts the rail for every team at once.

It is a **blast-radius limit**, not a billing control: it exists because auto-top-up refills the
balance, so the balance alone is not a ceiling against a runaway agent or a mispriced catalog entry.
Enforced fail-closed.

## X pay-per-use billing (switch ON since 2026-08-18)

`TREG_OAUTH_BILLED_PROVIDERS` — comma-separated providers whose upstream bill lands on TREG's
developer app (X moved to pay-per-use: the app owner pays per call, whoever's token made it). A
provider named here has its registry-connect calls metered against the org balance, `tier: oauth`
in the ledger. Empty = those calls are free (the pre-2026-08-18 behaviour). Currently `x`.
BYO-app connections are never metered. Ongoing spend is visible in the reconcile reports under
`tier: oauth`; the burn from the free period is only in console.x.com.

## Routed discovery — the steering switch (2026-08-29)

`TREG_ROUTED_DISCOVERY` (`on` by default, `off` to disable) decides whether DISCOVERY steers to
`treg.<capability>` rows. Off, search and the platform browse view stop showing them and
`/skill.md` + `/llms.txt` stop teaching them — while the endpoints stay generated, priced,
`catalog get`-able and callable by id, so agents that already hold one keep working. A dashboard
flip, no redeploy, like `TREG_PLATFORM_PROVIDERS` and `TREG_OVERFLOW_MODE`.

It exists because two questions are separate: whether the router ANSWERS well (measured — the
people-search bench puts the routed path at parity with the hand-written policy it replaces) and
whether every agent should be LED to it by default (only traffic answers that). Flip it rather than
reverting the feature.

## Market data platform keys (2026-08-16)

Five more `TREG_PLATFORM_KEY_*` env vars beside the originals, and the providers must ALSO be in
`TREG_PLATFORM_PROVIDERS` (both halves, or tier 4 refuses):

- `TREG_PLATFORM_KEY_COINGECKO` — PRO key; billed $0.00029/credit
- `TREG_PLATFORM_KEY_MARKETSTACK` — billed $0.000999/call against a 10,000/mo vendor cap
- `TREG_PLATFORM_KEY_FINNHUB`, `_TWELVEDATA`, `_TIINGO` — FREE-tier keys serving $0 trial pools,
  capped per team per day from fx.yaml (`treg_trial`). These are free accounts: if one is
  terminated, the pool dies gracefully (calls refuse, nothing bills) — replace the key or demote
  the provider to own-key-only by removing it from the allow-list.

## Enrichment platform keys (2026-08-20)

Seven more slots (`TREG_PLATFORM_KEY_COMPANYENRICH`, `_OCEANIO`, `_PREDICTLEADS` — base64 of
`api_key:api_token` —, `_FINDYMAIL`, `_BRANDDEV`, `_ICYPEAS`, `_LEADSFORGE`), all UNFUNDED at merge:
declared in `render.yaml` and `config.py` so tier 4 can be turned on per provider by funding the
account, setting the env var, and adding the service to `TREG_PLATFORM_PROVIDERS`. Ocean.io stays
refused even with a key until `fx.yaml` gets a real `usd` rate (its plan price is not machine-readable).
Tomba has NO slot on purpose: its data routes need a key+secret header pair and the platform-binding
path injects one value — wire a paired platform binding before offering tomba on tier 4.

## Creator-data platform key (2026-08-21)

`TREG_PLATFORM_KEY_INFLUENCERSCLUB` — the dashboard API key (a JWT), sent as `Authorization: Bearer`.
Declared in `config.py` and `render.yaml`; the catalog's 12 priced routes are platform-eligible
(`fx.yaml` $0.598/credit = our own $299/mo-for-500-credits plan, bought 2026-08-21; the public
page's "as low as $0.23" is the top of the volume slider, not what we pay. Every per-route credit
count was observed live). The account is FUNDED: set the env var and add `influencersclub` to
`TREG_PLATFORM_PROVIDERS`. Mind the 60s gateway 504 on cold enrichment calls: under `per_success`
settlement a 504 relays as a failure and settles at 0, but the vendor charged two of ours — a small,
bounded leak on the 0.03 tier, worth watching in the first reconcile report.

## Crustdata and Aviato platform keys (2026-08-25)

`TREG_PLATFORM_KEY_CRUSTDATA` and `TREG_PLATFORM_KEY_AVIATO` are funded pay-as-you-go keys. Add both
services to `TREG_PLATFORM_PROVIDERS` to serve them on tier 4. Crustdata's platform binding also
injects the provider metadata pin `x-api-version: 2025-11-01`; Aviato uses its normal Bearer header.
The `fx.yaml` rates are the replacement costs configured on the accounts: Crustdata $150/500 credits
($0.30), Aviato $10/1,000 credits ($0.01). Crustdata settles from `X-Credits-Used`; Aviato fixed and
conditional prices are derived from the authenticated rate card plus request/response shape.

## Exa platform key (2026-08-27)

`TREG_PLATFORM_KEY_EXA` is Jason's own Exa API key (dollar-metered, $20 signup credit + $10/month
free tier; top up on the Exa dashboard). Add `exa` to `TREG_PLATFORM_PROVIDERS` to serve its nine
routes on tier 4. Binding is the plain `x-api-key` header; no `fx.yaml` row because Exa prices in
USD. Platform billing settles every call from the response's `costDollars.total`, so a 20-result
search or a contents call with three content types bills exactly what Exa charged, not the catalog
base. Verified on the dev server before merge: reserve $0.007 → settle $0.009 on a 12-result search.

## TubeAlfred platform key (2026-09-06)

`TREG_PLATFORM_KEY_TUBEALFRED` is a Bearer API key with TubeAlfred's `youtube.read` scope plus
`billing.read` for the free capacity sweep. It is an unfunded slot at merge: a new account starts
with 100 credits, then the public Creator subscription
replenishes 5,000 credits for $5/month ($0.001/credit in `fx.yaml`). Add `tubealfred` to
`TREG_PLATFORM_PROVIDERS` only after the account is funded. The 15 curated routes use per-success
prices; standard calls cost one credit and non-empty comment/reply pages cost the fixed 20-credit
minimum. TubeAlfred publishes no machine-readable rate card, so watch the provider balance and the
reconcile report when the key is first enabled.

## Worker commands and scheduled settlement

`treg-worker` (console script, `[server]` extra) hosts the scheduled maintainer commands -
`capacity sweep` and `overflow verify --all` (see `ops/capacity.md`). `render.yaml` describes only the
first as a cron service (`treg-capacity-sweep`, hourly), with the DB URL, Fernet key and every
`TREG_PLATFORM_KEY_*` pulled from the web service via `fromService`.

> **`render.yaml` is not applied (checked 2026-09-02).** No Render Blueprint is registered for this
> repo - the web service, both cron jobs and the database were made in the dashboard, the web
> service has auto-deploy off, and the live environment has drifted far from the file (87 variables
> live vs 24 in the file; `TREG_OVERFLOW_MODE` is `on` live and `off` in the file). Treat the file
> as a statement of intent until a Blueprint is registered - and register one only after the file
> has been reconciled to the live services, because a Blueprint sync overwrites what it manages.
> The overflow re-verify cron (`treg-overflow-verify`, Mondays 06:00 UTC, dashboard-made, command
> `treg-worker overflow verify --all` since 2026-09-02) is deliberately absent from the file for
> that reason.

**Running the overflow routine by hand** (until a Blueprint schedules verify → sync): two one-off
jobs on the verify cron service, in order - `render jobs create <cron-id> --start-command
"treg-worker overflow verify --all"`, then the same with `overflow sync` - and read the
`verified N, failed N, inconclusive N, aggregator errors N, skipped N` line of the first and the
`enabled` count of the second. Verify only
stamps; sync is what opens routes.

Aggregator keys
(`TREG_OVERFLOW_KEY_ORTHOGONAL` / `_MONID`) are dashboard-managed on the web service and flow the same
way. `TREG_OVERFLOW_MODE` (`off` default | `shadow` | `on`) and `TREG_OVERFLOW_DAILY_BUDGET_USD` (20)
govern the overflow child cycle (`ops/capacity.md`); the keys serve nothing while the mode is `off`.

`treg-worker asynctasks settle` is the second cron command. `treg-asynctasks-settle` runs every two
minutes with the database URL, provider allow-list, and MiniMax/OpenRouter/Replicate platform keys
inherited from the web service. It never originates money; it only completes request-path holds.
Candidates are claimed by conditional update only after acquiring a concurrency slot. Each claim
has a 60-second lease and an attempt-version fence; processing is bounded to 30 seconds and network
polling to 10 seconds. Consecutive errors back off to 15 minutes. Caller terminal polls can complete
the original hold before cron. Migration `0019` adds the failure counter with a server default;
run the normal pre-deploy upgrade before either the web service or cron uses the new code.

## A `src/treg/infra/db.py` change needs a Postgres-shaped deploy plan

SQLite cannot catch this class: it has no connection pool and no lock queue. Two rules, both from the
2026-08-15 outage (an ALTER on `callrecord` queued behind live traffic, every new query queued behind
the ALTER, both instances starved, and the shared Postgres stayed wedged until a database restart):

- Alembic migrations run with `lock_timeout = 5s` and `statement_timeout = 120s` (set in `env.py`,
  Postgres only). A contended
  deploy therefore FAILS CLEANLY — prod keeps serving the old code — and the right response is to
  redeploy at a quieter moment, not to raise the timeout.
  **The one sanctioned exception is `CREATE INDEX CONCURRENTLY`, and only in its own revision.**
  The 5 s floor exists because an `ALTER` takes `ACCESS EXCLUSIVE`: it queues behind live traffic
  and every new query then queues behind IT — the 2026-08-15 wedge. A concurrent index build is not
  in that class. Its `SHARE UPDATE EXCLUSIVE` conflicts with neither `SELECT` nor
  `INSERT`/`UPDATE`/`DELETE`; it blocks no reads or writes while it builds, and a statement WAITING
  for it holds nothing and blocks nobody. What it does contend with is **autovacuum**, which takes
  the same lock and runs constantly on a large, write-heavy table — 0020 died on
  `LockNotAvailableError` in 5 s against exactly that, on the first try, at 00:37 UTC. Such a
  revision raises both timeouts inside its `autocommit_block` and restores `env.py`'s values before
  the block ends; it must not raise them for anything else in the same revision.
  **A killed concurrent build leaves an INVALID index** — present in `pg_class`, unusable by the
  planner, and never repaired — so a rebuilt-by-hand `IF NOT EXISTS` silently skips it and the scan
  it was meant to remove stays, with nothing failing. 0020's first attempt left exactly that. Such a
  revision therefore checks `pg_index.indisvalid` per index: valid ⇒ skip, invalid ⇒ drop
  concurrently and rebuild, absent ⇒ build.
  **Merging a revision breaks the crons before the web deploy applies it.** The three cron services
  auto-deploy from `main` while the web service does not, so between the merge and the pre-deploy
  they run new code against the old schema and `verify_db` refuses them (`Database schema revision
  N is behind this build`). It is bounded and self-correcting, and it cannot be rolled back by
  pinning a cron to the old commit — Render refuses `deploys create --commit` on a cron job. Deploy
  the web service IMMEDIATELY after merging a revision, or revert the merge.
- The pools are per instance and a rolling deploy runs two: keep the SUM of `pool_size +
  max_overflow` across every entry in `POOL_SPECS` such that DOUBLE it stays under the database
  plan's connection ceiling. A guard test pins this and counts all three deliberately — splitting
  one pool into three protects the API and is also a way to walk back into this outage.
- A new class of work gets a POOL, not a semaphore: "the maker you import decides what you can
  exhaust" replaces "the author remembers to bound themselves", which is the rule that failed.
  `tests/test_db_pool_isolation.py` pins which maker each module reaches for.

If a deploy fails with a lock timeout in the logs, that is the mechanism working. If the database
itself stops accepting connections, restart the POSTGRES resource, not the web service — an app
restart cannot release server-side slots (learned the hard way).
