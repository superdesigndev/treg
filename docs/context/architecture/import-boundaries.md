---
title: Enforced import boundaries
status: shipped
sources:
  - pyproject.toml
  - .github/workflows/ci.yml
  - src/treg/application/__init__.py
  - src/treg/application/call/__init__.py
  - src/treg/application/call/authorize.py
  - src/treg/application/call/idempotency.py
  - src/treg/application/call/overflow.py
  - src/treg/application/call/route.py
  - src/treg/domain/catalog/routing/__init__.py
  - src/treg/application/call/intake.py
  - src/treg/application/call/resolve.py
  - src/treg/application/call/reserve.py
  - src/treg/application/call/settle.py
  - src/treg/application/call/evidence.py
  - src/treg/application/call/service.py
  - src/treg/application/call/types.py
  - src/treg/client_identity.py
  - src/treg/domain/__init__.py
  - src/treg/domain/governance/__init__.py
  - src/treg/domain/governance/access.py
  - src/treg/domain/governance/budgets.py
  - src/treg/domain/governance/publicdemo.py
  - src/treg/domain/governance/teams.py
  - src/treg/domain/governance/usage.py
  - src/treg/domain/identity/__init__.py
  - src/treg/domain/money/__init__.py
  - src/treg/domain/capacity/__init__.py
  - src/treg/infra/upstream/__init__.py
  - src/treg/infra/upstream/injectors.py
  - src/treg/infra/upstream/relay.py
  - src/treg/infra/upstream/aggregators/__init__.py
  - src/treg/infra/upstream/limiter.py
  - tests/test_call_architecture.py
  - tests/test_import_lightness.py
related:
  - architecture/composition.md
  - architecture/money.md
  - interface/cli.md
---

# Enforced import boundaries

Import Linter reads the contracts under `tool.importlinter` in `pyproject.toml`. The main CI `test`
job installs the hand-maintained lock with `uv sync --frozen`, then runs
`uv run --frozen lint-imports` before the test suite. Keeping the check in that job reuses the
development environment and avoids a second install for a fast static architecture check.
The separate `test-postgres` job runs its database-sensitive subset serially against Postgres 16;
it uses unbuffered Python output and a 15-minute job budget so a slow test remains diagnosable. The
subset includes agent attribution, credential health, local-run reporting and ads-conversion coverage
so naive-UTC assumptions are exercised by asyncpg rather than hidden by SQLite's permissive adapter.

Stage 1 activated the first two contracts:

- The explicit lightweight CLI module list cannot directly import any server-extra package, including
  FastAPI, SQLModel, SQLAlchemy, Alembic, database drivers, MCP, Stripe, or cryptography. Imports guarded
  by `TYPE_CHECKING` are excluded globally because they cannot load at runtime. Indirect imports are
  allowed by this contract because optional proxy dependencies may appear in lazily executed internal
  modules; the named CLI modules themselves must remain free of direct server imports.
- `treg.domain.money` cannot import `treg.audit`. Money correctness never flows through the best-effort audit
  path, whose writes may be shed under load.

Stage 2 adds a third contract: the complete `treg.routers` package cannot import `treg.api`, directly or
indirectly. `as_packages = true` makes the source cover every current and future router submodule.
`api.py` remains the compatibility exporter and ordered route-table host, so the allowed direction is
API to routers.

Stage 3 adds domain contracts as packages appear. The complete `treg.domain.identity` package cannot import
`treg.api`, `treg.routers`, or `treg.application`. Identity now owns session signing and validation,
MCP token and grant-family primitives, and caller/access resolution as a leaf. Sibling-domain
edges are added when the sibling appears; identity therefore also forbids governance. Governance may
import identity but cannot import the API, routers, or application layer. Future sibling contracts remain
absent until their packages exist, so no placeholder domain makes a future boundary look active.
Governance owns shared tool/project ACLs, tag-budget rules, and public-demo rate policy. The package also
forbids direct FastAPI and Starlette imports; semantic policy errors are translated by each HTTP interface.
The call application package owns the framework-neutral staged use case: request intake, idempotency
state, target resolution, marketplace pricing, authorization, reservation, relay orchestration, and
finalization. The HTTP adapter captures a `CallInput`, translates typed failures, and wraps the returned
`UpstreamResponse`. Client-name normalization lives in a neutral leaf so the application path does not
load the Request-aware caller metadata adapter.

Two runtime contracts keep that boundary executable. `treg.application.call` cannot import the legacy
API, bootstrap, routers, FastAPI, or Starlette. `treg.infra.upstream` cannot import those HTTP adapters
or frameworks. Direct imports of the application-owned request and response DTOs remain the port shared
by the use case and relay. Mutation tests inject representative forbidden edges and assert detection.
The same test module also pins the money transaction boundary: all five `domain/money` primitives
(the reserve/settle/release staged bodies plus `grant` and `topup`) are scanned for `db.commit` /
`db.rollback` and must stage only, with a mutation self-check that an injected commit is detected;
the lazy stale-hold reap keeps its documented independent committing boundary.

Two direct edges are precise exceptions. `cli.ensure_proxy_dependency` imports `cryptography` only after
the user invokes the optional proxy feature and offers to install the proxy extra first.
`localrun.render_grant` imports SQLModel only when the server executes the grant path. Import Linter treats
function-local imports as ordinary direct edges, so both appear in `ignore_imports`; unmatched ignores are
errors, ensuring a removed or renamed edge cannot leave a stale exception behind.

An ignore covers an entire module edge and therefore cannot detect someone moving either lazy import to
module scope. `tests.test_import_lightness` closes that gap by starting an isolated Python subprocess,
importing every lightweight module, and asserting that no server dependency root appears in `sys.modules`.
Base dependencies such as httpx and questionary remain allowed.

The capacity domain (`treg.domain.capacity`, plan step B) is a leaf like identity: it cannot import
`treg.api`, `treg.routers`, `treg.application`, `treg.bootstrap`, `treg.audit`, FastAPI or Starlette.
It reads config and writes only its own tables and ratestore keys, from worker-profile commands
(`treg-worker`, a separate console script so the light `treg` CLI never gains a DB import). The call
application imports the capacity domain inward (`resolve` → `view`, `settle` → `signatures`/`marks`);
the domain never imports back; `application.call.overflow` composes the capacity domain, the
aggregator envelopes and the money primitives, and the aggregator adapters stay pure envelope code; `application.call.route` composes the pure
`domain.catalog.routing` package (contracts, adapters, ranking) with the call use case itself. The
aggregator envelopes live under `treg.infra.upstream.aggregators` and inherit the upstream contract
(no HTTP adapters, no routers); the capacity domain's `verify` module may import them because they are
pure envelope code, not a web framework.
