# tools-registry (`treg`) - guide for every coding agent

<!-- Editors: CLAUDE.md imports this file; Codex and Cursor read it directly. -->

treg is the tool catalog for an agent: one base URL, one token, and the agent can call a curated
catalog of external endpoints plus its own team's tools without ever holding an API key. The
load-bearing mechanic is a proxy that makes the caller's **real upstream request**, injects the
credential server-side and relays the answer verbatim. We never model an upstream API.

## Non-negotiables

Everything else in this file is guidance; these are the contract, and they win over any other passage.

1. A team's own key always wins over treg's, is never metered, and is never routed or overflowed.
2. A hold (the balance `reserve` sets aside for one call) is settled or released exactly once, on
   every path: timeout, cancellation and exceptions included.
3. Zero database connections are held while an upstream request is in flight. This is why
   `reserve` and `settle` are two transactions; never merge them.
4. Plain `/call/` is a faithful relay: the injected credential and the transport headers listed in
   `src/treg/infra/upstream/relay.py` are the only rewrites. Never add upstream-specific modeling
   or body buffering. Routed endpoints and overflow wrap the child's answer and say so; they never
   alter it.
5. Balances change only through money's five entries: grant, topup, reserve, settle, release.
   There is deliberately no refund or adjustment entry; an ops correction is a grant.

**Changing any invariant in this file means editing this file in the same PR.** Routed endpoints
and overflow once shipped with every other doc updated while this file still said "no router";
agents then built against a constitution that was wrong.

## Where the truth lives

- **Design docs are fragments.** `docs/context/` holds one per subsystem, each naming its
  `src/treg/*` sources in frontmatter; `docs/context/README.md` is the generated index and
  `docs/context/foundation/charter.md` the start. Read the fragment before changing an area (the
  `tools-registry-context` skill in `.agents/skills/` loads it).
- **Before pushing:** `bash .agents/skills/tools-registry-context/scripts/drift.sh` maps changed
  sources to fragments. Update them and commit the docs **in the same commit as the code**.
- **Agent-facing files are the product's front door**, not documentation: `src/treg/web/llms.txt`
  and `src/treg/web/skill.md` (installed into every agent by `install.sh`). They, `README.md` and
  this file must agree on how treg works; a behavior change asks whether all four move.
- **Three files move together** or they drift: `src/treg/web/tutorial.js` (the only interactive
  source) and its hand-kept prose mirrors `src/treg/web/tutorial.md` and `docs/TUTORIAL.md`.
- `README.md` is the overview and quickstart, `USAGE.md` the CLI reference, `CONTRIBUTING.md` the
  dev setup, `SECURITY.md` required reading before touching the proxy, runners, auth or secrets.

## Architecture

### Four layers

`routers/` -> `application/` -> `domain/` -> `infra/`. Imports point inward only.

| Layer | Owns | Never |
|---|---|---|
| `routers/` | HTTP and MCP translation in, response shape out | business rules, query orchestration, money |
| `application/` | use-case sequencing, transaction boundaries, compensation, cross-domain composition | empty wrappers around one-domain CRUD |
| `domain/` | rules explainable and testable alone: `identity`, `governance`, `connections`, `tools`, `catalog`, `capacity`, `money` | routers, application, concrete SDKs |
| `infra/` | DB engine and sessions, crypto, upstream relay and SSRF, ratestore, email, Stripe | decisions |

- Domains do not import each other, with three sanctioned edges: `governance -> identity`,
  `tools -> connections`, `capacity -> catalog` (read-only). `identity` and `money` are leaves.
  import-linter enforces the layering (`[tool.importlinter]` in `pyproject.toml`, run by CI);
  `docs/context/architecture/import-boundaries.md` explains each contract.
- `bootstrap.py` alone knows concrete implementations. `api.py` is the legacy `all`-role
  entrypoint, not where logic goes. `audit.py` is best-effort and drops rows under load, so nothing
  that must persist goes through it; `analytics` is read-only.

### Writes

- **Session discipline.** The application use case opens the session and is the only place that
  commits; domain functions never commit or roll back. A commit mid-flow silently breaks
  compensation, and no import rule can catch it. Money's public `reserve`, `settle` and `release`
  commit by design; a few other domain commits remain. Do not add another; move one out when you
  touch it.
- **Table ownership.** One writer module per table; cross-domain reads are fine. Three recorded
  exceptions: only money writes `org.balance_micro` and the auto-top-up fields; the call runtime
  may persist an OAuth token refresh into `secret`; audit writes `callrecord`, domains only read it.
- **The call runtime is self-contained.** `src/treg/application/call/` depends on no management
  code (routes, login, OAuth consent, Stripe top-up), reads only membership, deny rules,
  credentials, catalog prices and balances, and writes only what `tests/test_call_architecture.py`
  allowlists (the ledger entries, idempotency claims, OAuth refresh, audit and telemetry, first-call
  markers, tag budgets, capacity marks, overflow spend). Extend the test's allowlist in the same PR
  as any new write, and expect the reviewer to ask why.
- **Money.** Everything is **integer micro-USD** - never floats, never cents. The Stripe SDK lives
  only in `infra/stripe.py`, orchestration in `application/billing.py`, and `reconcile.py` is
  read-only. See `docs/context/architecture/money.md`.

### Security guards that look redundant on purpose

`expose_dev_code` (dev OTP only on a local sqlite database, `config.py`), the call-time SSRF check
(`infra/upstream/ssrf.py`), the fail-loud missing-Fernet-key check in `verify_db`, and the
`treg run` allow-list and rlimits (`runner.py`). Read the fragment before touching any of them.

## Development

```bash
uv run --with pytest-xdist pytest -n auto -q   # daily local default (same shape as CI)
uv run --frozen python -m pytest -q            # serial: debugging one test, or order
uv run treg --help                             # the CLI from this checkout
uv run python -m treg                          # the server
uv run lint-imports                            # the import-linter contracts (CI runs this too)
scripts/dev-local.sh up                        # live dev stack on :18790 with its own sqlite DB
```

xdist is pulled via `--with`, not the lockfile — same as CI. The Postgres CI job
(`test-postgres`) must stay serial: every worker would share one database while
`reset_db()` drops tables.

- **Dependencies change through `uv add` or `uv lock`, never by hand.** `pyproject.toml` pins
  `required-version` so an old uv refuses to run instead of rewriting `uv.lock`; CI uses `--locked`.
- **The package is split.** The base install is the light CLI; the FastAPI/DB stack is the
  `[server]` extra, the certificate authority is `[proxy]`. Never import a heavy dependency at the
  top of a CLI-path module; the "Lightweight CLI modules" import-linter contract lists them and
  fails the build.
- **The dashboard** (`src/treg/web/index.html`) is a single-file Vue app with no build step, so a
  broken view name fails silently. Verify in a browser.
- **Schema.** Alembic owns it (`src/treg/alembic/versions/`); every schema change is a revision.
  Startup only verifies the revision and refuses to boot when behind; migrations run only via
  `python -m treg upgrade`.

## Working agreement

- Keep the suite green; add tests for new behavior. Conventional Commits (`feat(scope): ...`,
  `fix: ...`, `docs: ...`); one logical change per commit; the PR says what changed and why and
  names the fragments it updated.
- `/mcp/` and `/mcp/v2/` differ on purpose. A change to either or to shared MCP code is reviewed
  against both; do not unify them in passing.

## When writing user-facing copy

One concept, one word. Settled deliberately - mixed vocabulary is how the old framing creeps back.

| Thing | Word |
|---|---|
| what an agent calls | **a tool** |
| the public half | **the catalog** |
| the team's half | **your own tools** (your keys and skills) |
| the server itself | **registry**, and only for that |

**Do not** call either half a *vault*, a *marketplace*, or *the registry*. Say what the agent can
now do, not what we store. Never use a count of endpoints or providers in this file; the catalog
changes weekly and every stale number is a lie.

**Do not document what is not built.** An agent that believes a feature exists fails in a way
nobody can debug. Provider choice is the easiest thing to overstate: treg compares providers, and
chooses only in the two disclosed cases of non-negotiable 4.
