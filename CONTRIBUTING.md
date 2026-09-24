# Contributing to tools-registry

Thanks for your interest!

## Getting set up

```bash
git clone https://github.com/superdesigndev/treg
cd treg
bash scripts/build-dashboard.sh # Node 22.12+ and npm; compile the browser app
uv sync                     # install deps (uv >= 0.12, pinned in pyproject - https://docs.astral.sh/uv/)
uv run --with pytest-xdist pytest -n auto -q   # daily local default (same shape as CI)
```

No `.env` needed for dev — every setting has a working default (ephemeral encryption key, local
sqlite). The `TREG_*` knobs for persistence / a real deployment are documented in the README's
**Configuration** section (and `docs/context/ops/deploy.md`).

A one-command local stack is in `scripts/dev-local.sh` (`up` / `logs` / `cli` / `reset`).
It starts Python on :18790 and Vite on :5173; open `http://localhost:18790/app`.
`TREG_DEV_DB=/absolute/path/to/dev.db` selects a separate local database without resetting another.

The Dashboard source is in `frontend/src/`: `.vue` pages, components and dialogs, feature use cases
in `state/`, and a typed JSON client in `api.ts`. `frontend/index.html` is only the document entry.
Run `npm --prefix frontend test` for transport tests and `npm --prefix frontend run test:e2e` for
browser flows against a disposable SQLite server. Install Chromium first with
`cd frontend && npx playwright install chromium`.

Before `uv build`, run `bash scripts/build-dashboard.sh`. The wheel and sdist include the resulting
assets; installing a published package needs no Node runtime. Editable Python installs do not
require a frontend build, so CLI and background-worker development remains independent.

## Project layout

- `src/treg/` — the app (FastAPI API, proxy, CLI, runners). The **design docs** in `docs/context/` explain
  each subsystem and cite the source files they cover — read the relevant fragment before changing code.
- `tests/` — the test suite (pytest). New behavior needs a test.
- `docs/context/` — per-subsystem design fragments (the source of truth for "how it works").

## Making a change

1. Branch off `main`.
2. Match the surrounding style — plain Python with type hints, no new dependencies without a reason.
   Commit messages follow Conventional Commits (`feat(scope): …`, `fix: …`, `docs: …`).
3. Add or update tests; run `uv run --with pytest-xdist pytest -n auto -q` (all green).
   Serial `uv run --frozen python -m pytest -q` is for debugging one test or order.
   The Postgres CI job must stay serial (`reset_db()` drops tables on a shared database).
4. If you changed a subsystem, update its fragment in `docs/context/` in the same PR.
5. Open a PR. CI runs the tests + a secret scan; a maintainer reviews.

## What to work on

The roadmap lives at the end of [`README.md`](README.md) (MCP support, finer permission tiers,
key-management hardening). Bug reports and small fixes are welcome anytime; for a larger feature,
open an issue first so we can agree on the approach before you invest in it.

## Reporting bugs / security issues

Normal bugs → a GitHub issue. **Security vulnerabilities → see [SECURITY.md](SECURITY.md)** (report
privately, never in a public issue).

## Code of conduct

Be kind and constructive. Harassment or personal attacks are not tolerated; maintainers may remove
comments or contributors that cross that line.
