---
title: Enforced import boundaries
status: shipped
sources:
  - pyproject.toml
  - src/treg/domain/plugins/__init__.py
  - .github/workflows/ci.yml
  - src/treg/application/__init__.py
  - src/treg/application/call/__init__.py
  - src/treg/application/call/access.py
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
  - src/treg/domain/connections/__init__.py
  - src/treg/domain/connections/authorization.py
  - src/treg/domain/connections/oauth_flow.py
  - src/treg/domain/connections/refresh.py
  - src/treg/domain/money/__init__.py
  - src/treg/domain/feedback/__init__.py
  - src/treg/domain/asynctasks/__init__.py
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

The contracts are `[tool.importlinter]` in `pyproject.toml`, which is the complete list; CI runs
`uv run --locked lint-imports` in the `test` job. AGENTS.md states the layering and the sanctioned
domain edges. This fragment records only what the contracts and tests cannot say.

## Lightweight CLI

- The lightweight CLI contract forbids *direct* server-extra imports from the named modules.
  Indirect imports are allowed because optional proxy dependencies load lazily inside internal
  modules; `TYPE_CHECKING` imports are excluded because they never load at runtime.
- The two lazy, function-local server imports (`treg.cli -> cryptography` for the optional proxy,
  `treg.localrun -> sqlmodel` for the server-side grant path) are in `ignore_imports`, with unmatched
  ignores raised as errors so a renamed edge cannot leave a stale exception.
- An ignore covers a whole module edge, so it cannot catch either import moving to module scope.
  `tests/test_import_lightness.py` closes that gap by importing every lightweight module in a fresh
  subprocess and asserting no server dependency root is loaded.
- `treg.domain.asynctasks` is a stdlib-only leaf shared with the light CLI (`cli.await_async_task`
  uses its terminal classification) so the CLI awaiter and the settlement worker cannot disagree
  about what "done" means. It is on the lightness list as well as under its contract.
- The R2 SDK (`obstore`) ships only in `[server]`; `infra.object_store.open_r2` loads it lazily.

## Why some contracts look stricter than needed

- `treg.domain.money` cannot import `treg.audit`: money correctness must never flow through the
  best-effort audit path, which drops rows under load.
- The catalog contract forbids `treg.oauth_providers` because that module imports
  `domain.connections`, and the indirect-import allowance would otherwise let the catalog reach a
  sibling domain unseen. Provider display names arrive through
  `bootstrap.configure_provider_labels` instead.
- `treg.domain.plugins` holds a provider's pure functions that catalog rows name
  ([catalog-schema](catalog-schema.md)). Replay runs them, so its contract forbids every I/O path:
  models, infra, config and the network, database and storage clients. The catalog domain calls
  them when a row's outcome rule names one (`domain.catalog.outcome`), the one sanctioned edge into
  this domain.
- Domain contracts exist only for packages that exist; do not add placeholder domains to make a
  future boundary look active.

## Guards in tests

`tests/test_call_architecture.py` pins what import-linter cannot: the call runtime's write
allowlist, and that the money primitives used inside a call transaction never `db.commit` or
`db.rollback` (the lazy stale-hold reap keeps its own committing boundary). Both carry mutation
self-checks. The allowlist includes the archive body PUT and the post-relay provider-resource
ownership writes because each must be made by the call that received the provider's successful
response, outside any DB transaction.

The `test-postgres` CI job runs a database-sensitive subset on Postgres because SQLite's permissive
adapter hides naive-UTC datetime mistakes that asyncpg rejects.
