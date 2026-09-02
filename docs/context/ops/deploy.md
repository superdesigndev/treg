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
- **Postgres pool hygiene:** for non-SQLite URLs the async engine adds `pool_pre_ping=True`,
  `pool_recycle=300`, sizing (`pool_size=5`, `max_overflow=10` — per instance; a rolling deploy runs two
  against a basic-plan Postgres ceiling of ~100, the 2026-08-15 outage) and `pool_timeout=5`. A request
  that gets no slot in 5 s is answered `503 {"treg_saturated": true}` with `Retry-After: 2` (api.py
  `_pool_saturated`) instead of SQLAlchemy's default 30 s wait and an anonymous 500. 15 slots is plenty
  because a `/call/` holds no connection during its upstream round trip — `call_tool` commits before
  `relay()`; holding one there deadlocked 15 concurrent calls for 30 s on 2026-08-24 (see
  [proxy-model](../architecture/proxy-model.md) § Connection discipline).

## Config (`config.py`)
`Settings` (pydantic-settings, env prefix `TREG_`, reads `.env`), cached via `get_settings()`:
- `database_url` — default `sqlite+aiosqlite:///./treg.db` (SQLite dev, Postgres on Render, same code).
  A `field_validator` rewrites a bare `postgres://` / `postgresql://` URL → `postgresql+asyncpg://`, so
  Render's `fromDatabase`-injected URL works unedited (the async engine needs the asyncpg driver).
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

**Audit back-pressure (`audit.py`).** Audit rows are written off the request path (fire-and-forget), and
each write opens a DB connection from the small pool **shared** with real requests. Two limits keep
best-effort logging from starving that pool: a loop-bound semaphore caps concurrent audit writes at
`_MAX_CONCURRENT_WRITES`, and under an extreme burst the writer **sheds** load — it drops any audit row
past `_MAX_PENDING` rather than let the pending set grow without bound. Audit must never OOM or wedge the
server. Shedding is the *only* loss that should ever happen: `record_call` splats its telemetry dict
into `CallRecord(**fields)`, so a key with no matching column used to raise inside `_write`, where the
except swallowed it, and the whole row disappeared — a telemetry field deployed one commit ahead of its
migration would have silently emptied the table. `_known_fields` now drops unknown keys (logging which
ones), and `_write`'s swallow logs a warning with the traceback. **A quiet audit table is now a bug you
can see in the logs**, not one you find out about weeks later.

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

## Worker commands and the capacity cron (2026-08-28)

`treg-worker` (console script, `[server]` extra) hosts the scheduled maintainer commands — today
`capacity sweep` (see `ops/capacity.md`) and `retention purge`. `render.yaml` runs capacity sweep as
the cron service `treg-capacity-sweep` every hour, with the DB URL, Fernet key and every
`TREG_PLATFORM_KEY_*` pulled from the web service via `fromService` — so a new platform key is added
in ONE place. Aggregator keys (`TREG_OVERFLOW_KEY_ORTHOGONAL` / `_MONID`) are dashboard-managed on the
web service and flow the same way. `TREG_OVERFLOW_MODE` (`off` default | `shadow` | `on`) and
`TREG_OVERFLOW_DAILY_BUDGET_USD` (20) govern the overflow child cycle (`ops/capacity.md`); the keys
serve nothing while the mode is `off`.

### Retention purge

`treg-worker retention purge [--days 90] [--batch 5000] [--no-vacuum] [--dry-run]` deletes
`CallRecord`, `RunRecord`, and `SearchMiss` rows older than the retention window. See
`architecture/data-model.md` § "Retention purge". Run it manually first to clear the backlog, then
weekly via a Render cron (start command: `treg-worker retention purge`). Only the DB URL and Fernet
key are required — no platform keys.

**One-off prod invocation after merge:** shell into the Render web service (or create a one-off
job) and run `treg-worker retention purge --dry-run` first to confirm the expected delete count,
then `treg-worker retention purge` to execute. For a large backlog, batch size 5000 keeps each
transaction short.

## A `src/treg/infra/db.py` change needs a Postgres-shaped deploy plan

SQLite cannot catch this class: it has no connection pool and no lock queue. Two rules, both from the
2026-08-15 outage (an ALTER on `callrecord` queued behind live traffic, every new query queued behind
the ALTER, both instances starved, and the shared Postgres stayed wedged until a database restart):

- Alembic migrations run with `lock_timeout = 5s` and `statement_timeout = 120s` (set in `env.py`,
  Postgres only). A contended
  deploy therefore FAILS CLEANLY — prod keeps serving the old code — and the right response is to
  redeploy at a quieter moment, not to raise the timeout.
- The pool is per instance and a rolling deploy runs two: keep `pool_size + max_overflow` such that
  DOUBLE it stays under the database plan's connection ceiling. A guard test pins this.

If a deploy fails with a lock timeout in the logs, that is the mechanism working. If the database
itself stops accepting connections, restart the POSTGRES resource, not the web service — an app
restart cannot release server-side slots (learned the hard way).
