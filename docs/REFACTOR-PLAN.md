# treg Architecture Refactor

[中文版本](REFACTOR-PLAN-CN.md)

> treg holds credentials on behalf of callers, invokes external APIs, and charges for calls when needed.
> This refactor does not change the product. It splits the 12,000-line `api.py` into modules with explicit
> boundaries so that parallel development, provider onboarding, and traffic growth do not obstruct one another.

The following remain unchanged: Python; one self-hostable package; Postgres for the hosted service; a proxy
that only injects credentials and faithfully relays external APIs; and a ledger for every balance change.

## 0. Non-negotiables

Everything else in this document is guidance; these six are the contract. When any passage seems to conflict
with them, they win.

1. A hold is settled or released exactly once — on every path, including timeout, cancellation, and exceptions.
2. Zero database connections are held while an upstream request is in flight.
3. The relay stays faithful: credential injection and transport headers are the only rewrites.
4. Balances change only through money's five entries: grant · topup · reserve · settle · release.
5. The dataplane write allowlist (§1.6) is exhaustive; a write outside it is a defect, not a judgment call.
6. Each PR carries one kind of risk: movement, behavior change, or migration — never mixed.
   *Amendment (approved 2026-08-26):* a refactor PR may additionally carry at most three `fix:` commits
   for small pre-existing defects - never folded into a move or extraction commit, placed before any move
   touching the same region, bounded blast radius, no client-parsed API surface change, same-commit
   regression test (E2E on Postgres when adjacent to concurrency or money), line anchors re-resolved after
   each fix. Money semantics, API surface, and concurrency architecture still require an independent PR.
   Every PR description carries an "Intentional behavior changes" section exhaustively listing each fix
   commit (or stating there are none); everything not listed there must be behavior-identical.

## 1. Target structure

Use a four-layer modular monolith. The layers are code boundaries, not four services. The default deployment
remains one process. Workloads may be deployed separately later when their traffic or risk profiles require it.

### 1.1 Interface layer: `routers/`

Routers translate HTTP or MCP requests into application inputs and convert results into responses. They do not
introduce business rules.

| Module | Responsibility |
|---|---|
| `web/` | Landing pages, SEO pages, tutorials, and other presentation-only routes |
| `auth/` | User login, CLI pairing, and treg OAuth authorization-server endpoints |
| `orgs` `resources` `connections` `billing` `catalog` `admin` `onboard` | HTTP entry points for each feature |
| `call` | Accept `/call/`, build the call input, and invoke `application.call` |
| `mcp` | MCP protocol entry point |

Phase 1 does not require DTOs everywhere or a rewrite of existing serialization. Completion means routers are
thin and no new decisions, query orchestration, or money logic are added to them during migration.

### 1.2 Application layer: `application/`

This layer owns use-case sequencing, transaction boundaries, and failure compensation.

| Module | Responsibility |
|---|---|
| `call` | Resolve target, authorize, reserve funds, relay, and settle from the result |
| `signup` | First login, team creation, and promotional credit grant |
| `connect` | OAuth/key connection, verification, resource discovery, and tool creation |
| `billing` | Create top-ups, handle Stripe webhooks, and initiate credit posting |
| `onboard` | Landing-page sandbox, demo teams, and onboarding seed data |

Only multi-step, compensating, or cross-domain workflows belong here. A single-domain CRUD mutation calls a
public domain command directly instead of receiving an empty application wrapper.

Before moving files, define the stage contract for `application.call`: inputs, outputs, failure modes,
transaction scope, and how each failure releases or settles a hold. Preserve three hard constraints:

- No database connection is held while an external API request is in flight.
- A hold is settled or released exactly once, including on timeout, cancellation, and exceptions.
- The result is a framework-neutral streaming object,
  `UpstreamResponse(status, raw_headers, body_stream, close)`. Infra creates it, application attaches billing
  and compensation, and the router wraps it in Starlette `StreamingResponse`. `close` always releases the
  upstream connection. Metered calls that require buffering still return the same object shape.

Session discipline applies to every use case, not only `call`: the application use case opens the session
and is the only place that commits; domain functions never commit or roll back; infra provides the session
factory. A function that commits mid-flow silently breaks compensation, and no import rule can catch it —
this line is held by review and architecture tests.

### 1.3 Domain layer: `domain/`

This layer contains business rules that can be explained and tested independently.

| Module | Responsibility |
|---|---|
| `identity` | Caller identity, tokens, roles, and authorization |
| `governance` | Teams, members, projects, deny rules, usage limits, and budgets |
| `connections` | Credential state, OAuth refresh rules, and connection health |
| `tools` | Team-owned tools, credential binding, and skill/bundle rules |
| `catalog` | Catalog endpoints, capability taxonomy, pricing, and credential selection |
| `capacity` | Platform vendor-account capacity: policies, snapshots, burn forecasting, exhausted state |
| `money` | Balances, holds, settlement, release, top-ups, and reconciliation rules |

`ledger` lives inside `money`. Call workflows use only `grant()`, `topup()`, `reserve()`, `settle()`, and
`release()` and never write ledger tables directly. The Stripe SDK belongs in an infra adapter, not in money.

Domain modules do not import each other by default. Cross-domain composition belongs to the application
layer; cross-domain data needs use the sanctioned shared table reads or values passed in as parameters, so
each domain stays explainable and testable alone. Three directed exceptions are enumerated:
`governance → identity`, `tools → connections`, and `capacity → catalog` (read-only). `identity` and `money`
are leaves and import no sibling. Each import-linter contract activates as its boundary comes into
existence (stage 1: CLI lightness and ledger→audit; stage 2: routers→api; the domain matrix as domains are
extracted), and the matrix fixes the stage 3 extraction order: leaves move first.

### 1.4 Infrastructure layer: `infra/`

This layer integrates with databases and external systems.

| Module | Responsibility |
|---|---|
| `db` | Database engine, sessions, and Alembic migrations |
| `crypto` | Local Fernet or hosted KMS encryption |
| `upstream` | Shared httpx client, faithful streaming relay, and SSRF protection |
| `ratestore` | Local database or Redis implementation for rate limits and short-lived state |
| `email` `stripe` | Resend and Stripe adapters |

Apply dependency inversion only at narrow boundaries with external or multiple implementations: `crypto`,
`ratestore`, `email`, `stripe`, and `upstream`. Do not add a repository abstraction over the whole database in
phase 1. Domain and application functions may continue to receive `AsyncSession`.

### 1.5 Composition and shared code

- `bootstrap/` is the only place that knows concrete implementations. It owns `create_app(role=...)`, wiring,
  background tasks, and startup checks.
- `config` exposes validated configuration only.
- `audit` is best-effort operational logging and never carries correctness-critical money work.
- `analytics` is read-only and never controls business flow.
- The system retains one physical schema and may share ORM models for reads.

Shared reads do not imply shared writes. Before refactoring, create a table ownership register. Each table has
one writer module while cross-domain reporting may read multiple tables. Enforce this through public mutation
commands, architecture tests, and review where import rules are insufficient.

Record three known exceptions explicitly: `org` belongs to governance, but only money writes
`org.balance_micro` and auto-top-up fields; `secret` belongs to connections, but the call runtime may persist an
OAuth token refresh; and cross-cutting audit writes `callrecord` while domains only read it.

### 1.6 Call runtime boundary

The call runtime may read membership, deny rules, credentials, Catalog prices, and balances. Its writes are
limited to an enumerated allowlist: `reserve/settle/release`, idempotency claims and release, OAuth refresh,
audit and telemetry, first-call markers, tag-budget accounting, and platform-account capacity marks.
Everything else is forbidden. It does not
depend on management routes, login pages, OAuth consent, or Stripe top-up flows. This allowlist is the dataplane
contract and must be enforced by architecture tests. It governs the request path; startup-sequence writes
(schema migration, data backfills such as `_backfill_provider_extra_tools`) are listed separately in the role's
startup manifest and are not covered by this allowlist. That carve-out is transitional and expires at stage 5:
when execution switches to Alembic, startup data backfills move into the release pipeline, role startup
manifests contain no writes, and the carve-out retires. Until then, no new write may be added to a startup
manifest on the strength of this exception.

`create_app(role="all" | "dataplane" | "control")` fixes three lists for every role: mounted routes, background
tasks, and startup checks. Roles prepare the package for later deployment separation but do not require it now.

Server-side CLI execution at `/run` is not dataplane work. It starts subprocesses, consumes CPU, and requires a
sandbox, giving it a different scaling and security profile from a thin relay. In phase 1, exclude `/run` from
the dataplane and keep it on `control` or `all`, while reserving a dedicated `runner` role. Independent runner
deployment and container isolation remain phase 2 work. Shared deny, limit, and ACL gates remain domain rules.

### 1.7 Automated architecture rules

1. Routers call application use cases or public domain read/mutation commands only.
2. Application calls domain rules and uses external capabilities through narrow ports.
3. Domain does not depend on routers, application, or concrete external SDKs.
4. Only `money` writes ledger tables.
5. Only a table's owner writes it; cross-domain reads are allowed.
6. The lightweight CLI installation cannot import server-only heavy dependencies.
7. Tests fix each app role's routes, background tasks, and startup behavior.
8. E2E tests, not import rules alone, fix relay fidelity, DB connection discipline, and settlement correctness.
9. Domain modules import only the enumerated intra-domain edges; all other cross-domain composition happens
   in application.

### 1.8 Cross-cutting conventions

- **Read models.** Cross-domain read-only queries get an explicit home: a domain may expose public read
  models that join across tables (`reconcile` is the precedent). They never mutate, and routers still
  contain no query orchestration.
- **treg-originated errors.** Application and domain raise framework-neutral semantic error types; only
  routers translate them into HTTP shapes. The error vocabulary lives in the OpenAPI snapshot, so refusals
  (402/429/503) keep one machine-actionable shape instead of per-module dicts.
- **Cross-domain reactions.** One domain reacting to another's event composes synchronously in application;
  anything that must survive a restart goes through an explicit outbox (the adsconv pattern). No implicit
  in-process event bus, and never audit — it drops rows.

## 2. System topology and long-term principles

The layers define code organization. The following topology defines how the system handles sources and
workloads with different scaling and risk profiles.

### 2.1 One Call Kernel

Team-owned tools and the platform Catalog are two supply sources, not two calling systems. Both converge on one
Call Kernel for identity, authorization and budget checks, credential selection and injection, faithful relay,
billing, idempotency, and audit. They differ only in source, credential ownership, and pricing policy.

### 2.2 Deployment roles follow workloads

Keep one package and one modular monolith, but define four workload profiles:

- `control`: login, teams, tools, connections, Catalog administration, and top-ups.
- `call` (currently named `dataplane` in code): HTTP/MCP calling, relay, and settlement.
- `runner`: controlled CLI subprocess execution with independent sandboxing, concurrency, and resource limits.
- `worker`: durable background work that should not block requests, including verification, sync, and outbox delivery.

Phase 1 may run only `all`, `dataplane`, and `control`, but ownership and startup manifests should already follow
these four profiles. Later separation becomes a configuration and capacity decision, not a domain redesign.

Before any role runs separately or a second replica appears, three preconditions hold: background tasks are
idempotent, singleton tasks take a DB/ratestore lock, and every in-process cache declares its invalidation
story. Until then, multi-instance concerns are not solved.

### 2.3 Catalog is a supply chain

The official flow is: submit configuration -> static validation -> real verification -> evidence generation ->
publish a versioned snapshot -> Call Kernel consumes only published snapshots. Drafts under verification never
enter the hot path. Rollback operates on snapshot versions rather than live configuration edits.

### 2.4 Platform-level Call Kernel constraints

- Fairness and admission control (per-org/token/provider limits with backpressure) are a later concern; no
  stage in this refactor implements them.
- Customer-owned and platform-managed credentials are separate security domains with independent budgets,
  quotas, rotation, circuit breakers, and incident isolation.
- Background outcomes have three reliability levels: money, authorization, credentials, and idempotency are
  strongly consistent; verification, synchronization, and important notifications are durable and retryable;
  general analytics and operational audit may be lossy. In-memory lossy queues cannot carry the first two.

These are long-term boundaries, not a requirement to add Kafka, split the database, or adopt microservices now.

## 3. Catalog onboarding and verification

Routine providers should primarily submit configuration and pass one automated verification flow. The precise
model is configuration-driven for common cases, with reviewed named plugins for non-standard protocols, not
"fully declarative" or "zero Python."

> **Upstream supply note (2026-08).** A separate private repository, Catalog Hunter, is being introduced:
> weekly provider discovery, admin approval of subscriptions, platform API-key sharing, and export into this
> repository's catalog. `catalog_store`'s data shape is the alignment contract for that export (conversion is
> acceptable). Re-scope stage 6 once the Hunter export format is visible — discovery/approval work listed
> below may move there, leaving treg with ingest, verification, and serving. Evaluate §3.3 (phase-2 vendor
> credential channel) against Hunter's key-sharing flow before building either.

### 3.1 Catalog and provider configuration

- `catalog/<service>.yaml` records endpoints, capabilities, parameters, prices, price sources, and review dates.
  A price without a source cannot be published.
- `capabilities.yaml` provides a cross-provider taxonomy. `fx.yaml` records FX and credit pricing.
- `providers/<service>.yaml` requires a versioned schema, strict field allowlist, and startup validation.
- YAML may reference allowlisted named plugins, but cannot embed arbitrary code or network behavior.

### 3.2 Automated verification

One command confirms that a clearly invalid key is rejected by the real API, runs a safe successful example with
test credentials, reconciles declared pricing with metering, and produces a timestamped redacted report.
Providers cannot self-assert `verified: true` in a pull request.

Every `test_request` declares whether it is read-only, worst-case cost and timeout, allowed hosts, and the
isolated test account. Generic automation must not run endpoints that write data, send messages, place ads, or
have unbounded cost.

Continuous checks include:

- An in-process call-state matrix covering per-call, success-only, and usage pricing, plus failure, idempotency,
  concurrency, relay fidelity, and DB connection discipline.
- Provider-specific cost-response parsing tests.
- A nightly call to the cheapest safe endpoint using isolated credentials, global/provider budgets, and breakers.
- An explicit stale marker when a price has not been reviewed for more than 90 days.

### 3.3 Catalog phase 2: treg-managed verification credentials

Phase 1 favors maintainer-created test accounts funded with small provider-issued credits. If a key must be
transferred, use a private channel, environment variables only, immediate revocation, and never fork-based CI.

Phase 2 — providers storing verification keys as treg connections so the pipeline calls real APIs without
plaintext exposure — is an idea, not part of this refactor. Its flow and security preconditions live in
`docs/ideas/catalog-phase2-managed-verification-credentials.md`; nothing there is a completion condition here.

This refactor does not add Catalog hot reload, automatic provider selection, or automatic failover. treg exposes
comparison data and the caller chooses the provider.

## 4. Refactor sequence

Build the safety net first, then migrate complete use cases. A "pure move" is an intention not to change behavior,
not a reason to skip verification. FastAPI registration order, startup side effects, dependency overrides, and
background tasks can all change while moving files.

| Stage | Work | Exit criteria |
|---|---|---|
| 0. Safety net | Call-state matrix, key user journeys, Postgres CI | Existing tests green; current routes, OpenAPI, and startup behavior recorded |
| 1. Composition boundary | Add `create_app()`, bootstrap, import-linter, and Alembic baseline | Default behavior unchanged; role manifests tested; baseline matches current schema; HEAD rewriting and OpenAPI customization live in the factory |
| 2. Presentation and interfaces | Move pages, Catalog API, and admin/reconcile routers | Route snapshot and registration order unchanged; no new business rules in routers |
| 3. Identity and control | Extract identity, then auth, governance, resources, and connection use cases; dissolve the transitional `routers/dependencies.py` into `domain/identity` and delete the module | One complete use case per move; the same E2E journey passes before and after; no transitional dependency module remains |
| 4. Main call flow | Define `CallContext` and narrow ports, then split `application.call` by stage | All 26 scenarios unchanged; zero DB connections during relay; no dangling holds |
| 5. Database migration | Switch execution to Alembic and remove handwritten startup migrations | New DB, old SQLite, old Postgres, rolling deploy, and rollback verified; an N-1 client compatibility window (old CLI ↔ new server) declared before execution switches; startup data backfills moved into the release pipeline so no role's startup manifest writes data (retiring the §1.6 carve-out) |
| 6. Catalog phase 1 | Provider config, verification reports, and continuous checks | Schemas, safety limits, real-call budgets, and evidence complete; use an onboarding freeze or dual-read period; update `docs/VENDORS.md`, `/vendor-listing`, and the dashboard modal together |

Stage 1 creates and validates an Alembic baseline but does not blindly stamp unknown production databases.
Execution changes only in stage 5. Hosted deployments migrate through the release pipeline; self-hosted installs
retain a one-command upgrade.

Between stages 1 and 5, every schema change must update both the legacy startup migration and an Alembic revision.
CI proves both paths create identical fresh schemas. A real control/call deployment split also requires
expand/contract migrations so adjacent versions can share the database. Until then, roles mean the code can start
separately, not that independent deployment is fully supported.

End every stage with a low-cost live check of login, connection, call, charge, and top-up.

## 5. Existing server module destination map

`api.py` is split according to section 1 and is not repeated below.

| Current file | Destination | Main caller | Role | Background work | Writes |
|---|---|---|---|---|---|
| `proxy.py` | `infra/upstream`, producing `UpstreamResponse` | `application.call` via port | dataplane | None | None |
| `injectors.py` | `infra/upstream` credential injection | relay | dataplane | None | None |
| `ledger.py` | internal to `domain/money` | money only | both | None | creditblock, hold, ledgerentry, `org.balance_micro` |
| `billing.py` | Stripe SDK -> `infra/stripe`; orchestration -> `application.billing` | billing router, webhook | control | None | via money; org auto-top-up fields |
| `reconcile.py` | `domain/money` read-only reporting | admin router | control | None | None |
| `catalog_store.py` | `domain/catalog` | Catalog routes, call | both | None | None |
| `endpoint_stats.py` | `domain/catalog` success/latency observations | Catalog views | both | None | None, reads callrecord |
| `oauth_providers.py` | phase 6 YAML plus named `domain/connections` plugins | connections, read-only call binding | both | None | None |
| `oauth.py` refresh | `domain/connections` | call, connect | both | None | secret refresh allowlist item |
| `mcp.py` | `routers/mcp` | None | dataplane | None | None |
| `mcp_oauth.py` | `domain/identity` grant/refresh family | auth issues, MCP validates | both | None | oauthclient, oauthgrant, oauthrefresh |
| `session.py` | `domain/identity` browser sessions | auth, web; MCP token validation shares its signing key | both | None | None |
| `runner.py` | `application/run` server CLI execution | `/run` router | control in phase 1; runner later | subprocess per run | runrecord via audit |
| `sandbox.py` `demo.py` `pubfeed.py` | separate `application/onboard` sandbox, seed, and pubfeed modules | onboard, web | control | in-memory pubfeed SSE | demo org/user markers |
| `adsconv.py` | `application/adsconv` outbox and uploader | lifespan | control worker only | drain about every 300s | adconversion, org attribution |
| `referrals.py` | independent `domain/referrals` | routers, signup | control | None | referral, referral code; credit via `money.grant` |
| `health.py` | credential health -> connections; SSRF check -> upstream | routers, relay | both | None | `secret.last_error` |
| `agent_pages.py` | `routers/web` rendering helper | web only | control | None | None |
| `audit.py` | cross-cutting best-effort operations log | all | both | in-process queue | callrecord, runrecord, searchmiss |
| `analytics.py` | cross-cutting external analytics | all | both | flush queue | PostHog only |
| `ratestore.py` | `infra/ratestore` port and two implementations | identity, governance | both | None | ephemeral state |
| `db.py` | `infra/db` | bootstrap | both | None | schema, owned by Alembic after stage 5 |
| `crypto.py` | `infra/crypto` port and implementations | connections, tools, call | both | None | None |
| `email.py` | `infra/email` | signup, connect, governance | control | None | None |
| `models.py` | shared schema with writes governed by ownership | all | both | None | None |
| `config.py` | cross-cutting validated config | all | both | None | None |
| `__main__.py` | bootstrap | None | None | None | None |
| CLI files | Lightweight client, outside server layers; no server-heavy imports | terminal users | None | None | None |

Multi-step login, CLI pairing, and OAuth authorization-server flows become application auth use cases. `signup`
covers only first-login team creation and the grant. Every stage's checkpoint-0 design note must reconcile the
modules it touches against this map (and against the route and startup snapshots) before code moves, and record
omissions here.

## 6. Terminology

| Term | Plain meaning |
|---|---|
| modular monolith | One deployable unit whose code is divided by explicit module boundaries |
| wheel | A Python installation package |
| faithful relay | Preserve requests and responses except transport headers, treg control headers, and credential injection |
| control plane | Login, configuration, management, and top-up functions |
| data/call plane | The runtime that accepts, checks, relays, and settles calls |
| pipeline / callpipe | The processing flow from call arrival through settlement |
| ledger | The record of every balance change |
| gateway / adapter | An implementation that connects an internal port to Stripe, Redis, KMS, or another external system |
| schema | Database structure or the allowed fields in a configuration file |
| bogus-key test | Verify that the real external API rejects a clearly invalid test key |
| canary | A real call with minimal traffic and a strict budget |
| hot path | Code traversed by every call and most sensitive to stability and latency |
| pure move | Change code location without intending to change externally visible behavior |
