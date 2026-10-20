---
title: Lens — native team brand research and answer-engine audits
status: built; behind TREG_LENS_ENABLED, disabled by default
sources:
  - src/treg/domain/lens.py
  - src/treg/application/lens.py
  - src/treg/application/lens_execution.py
  - src/treg/routers/lens.py
  - src/treg/models.py
  - src/treg/alembic/versions/0078_lens.py
  - src/treg/config.py
  - src/treg/api.py
  - src/treg/bootstrap.py
  - src/treg/routers/auth.py
  - src/treg/domain/governance/teams.py
  - frontend/src/pages/LensPage.vue
  - frontend/src/lens/state.ts
  - frontend/src/lens/client.ts
  - frontend/src/lens/types.ts
  - frontend/src/lens/LensResults.vue
  - frontend/src/lens/lens.css
  - frontend/src/components/DashboardNavigation.vue
  - frontend/src/state/boot.js
  - frontend/src/state/catalog.js
  - frontend/src/state/data.js
  - frontend/src/state/session.js
  - frontend/src/views.ts
  - frontend/src/App.vue
  - frontend/playwright.lens.config.ts
  - frontend/tests/lens.test.ts
  - frontend/e2e/lens.spec.ts
  - frontend/e2e-live/lens.spec.ts
  - frontend/package.json
  - .github/workflows/ci.yml
  - scripts/dev-local.sh
  - scripts/frontend-lens-e2e-server.sh
  - tests/lens_browser_server.py
  - tests/lens_upstream.py
  - tests/test_lens.py
  - tests/test_lens_execution.py
  - tests/test_lens_schema.py
  - THIRD_PARTY_NOTICES.md
  - .env.example
  - pyproject.toml
related:
  - interface/lens-api.md
  - interface/dashboard.md
  - architecture/composition.md
  - architecture/data-model.md
  - architecture/multi-tenancy.md
  - architecture/money.md
---

# Native Lens

`/app#lens` is a Vue dashboard feature backed by Python application/domain modules. This slice provides
multiple team-owned brand profiles, URL-led research, reviewed competitors and buyer questions,
manual audits, and saved answer/citation history. Scheduled monitoring, discovery tools, exports and
importing standalone Lens workspaces are outside this slice. The standalone project is attributed in
[`THIRD_PARTY_NOTICES.md`](../../../THIRD_PARTY_NOTICES.md).

## Enable and run locally

Lens uses the existing Python, Vue, database, encryption, session and provider setup; it adds no
dependencies or separate service. Follow [CONTRIBUTING](../../../CONTRIBUTING.md) for Python/uv,
Node/npm and tmux prerequisites. For the default local development database, from the repository root:

```bash
uv sync --locked
bash scripts/build-dashboard.sh
TREG_LENS_ENABLED=true scripts/dev-local.sh up
```

Open `http://localhost:18790/app#lens` and sign in using the ordinary dashboard flow. Restart an
already-running dev stack with the flag if necessary. `dev-local.sh` retains private development
encryption/session keys and starts the normal CLI maintenance/upgrade path with those keys and the
selected local database (`TREG_DEV_DB` overrides the default). Keep the keys stable with the database
because Lens payloads are encrypted. Raw ASGI/operator deployments need the normal
`python -m treg upgrade` release step with the same database and encryption key used by the server,
plus a rebuilt dashboard; the ASGI lifespan only verifies schema. Migration `0078` creates empty
Lens tables and leaves existing balances unchanged.

`Settings.lens_enabled` defaults to false. `/auth/me` reports `lens_enabled`; the navigation follows
it, and `/lens/*` returns 404 while disabled. Routes belong to the control/all roles. Enabling Lens,
viewing a profile, requesting an estimate, and saving a profile perform no paid calls. Research and
audits require separate explicit confirmation. Live execution needs the normal configured catalog
provider credentials or the team's own connected keys, and any applicable team credits.

## Setup and evidence

`LensPage` guides Website → Brand → Buyer questions. `public_url` accepts a bare domain or a public
HTTP(S) URL, strips query/fragment, and rejects credentials, local addresses and unsupported ports.
Research makes one `exa.web.answer` request; its suggested competitors/questions remain editable.
Questions are generated suggestions, not observed search demand. Manual profile setup is free.

`normalize_project` bounds brand/competitor names to 100 characters, descriptions to 1,200,
URLs to 2,048, competitors to eight and questions to eight × 500 characters. Duplicates are removed.
Profiles may contain no questions; an audit requires one to eight. `engines` contains one to four of
`chatgpt`, `perplexity`, `gemini`, `google`. ChatGPT, Gemini and Google AI Mode use Cloro web-interface
results; Perplexity uses DataForSEO's Sonar model API. Requests use United States/English settings.

`normalize_answer` retains bounded answer text, safe HTTP(S) citations and brand mentions. `position`
is an observed numbered-list position when available, otherwise the order of the tracked brands'
mentions in the answer; it is not an independently measured search rank. Empty/error answers are
excluded from visibility denominators. Results disclose their source kind and any truncation.

## Team permissions and storage

`require_access` scopes every object to the current `Caller.org_id`; another team's ID returns 404.
Unrestricted viewers may read. Member/admin/owner roles may create profiles, request quotes and
approve paid runs. Members edit profiles they created; admins/owners may edit any team profile.
Only a quote's creating user can start it. Restricted tool/project memberships, pinned tags and
public-demo callers cannot use Lens; demo teams cannot execute research or audits.

`application.lens` is the sole writer of `LensProject` and `LensRun`. Profiles and frozen
plans/results are encrypted with the deployment's existing Fernet key; no caller credential is
retained. `ORG_SCOPED_MODELS` deletes runs before profiles with the team. The server permits 50
projects, 100 quotes per rolling hour and three active runs per team. Project details return the
latest 50 non-quoted runs; stored history is not silently expired.

`createLensState` persists a draft, creation request ID and pending run ID through the dashboard
storage helper, scoped by signed-in user and team. It stores no authentication credential. Team
switches invalidate pending reads; separate project/run tickets also reject late history responses.
Before a paid POST, the browser verifies that storage accepted its recovery ID. If storage is
blocked or full, it sends no paid request; existing projects and history remain readable.
Unacknowledged profile saves retain the exact draft. A lost PUT response is checked against the
saved revision and canonical profile before a deliberate retry; conflicting edits require review.
Changing the setup URL retains the paid research receipt with its original website clearly labeled.

## HTTP contracts

The [Lens API contract](lens-api.md) describes the seven native endpoints, profile revision checks,
idempotent creation IDs, durable quotes, run states and result shapes. Authenticated requests use
existing dashboard sessions; no separate Lens API key is required.

## Billing, execution and recovery

All money is integer micro-USD. `lens_execution.quote` reads current pricing and access without
reserving funds or consuming daily call slots. Limits are 50,000 micro for research and 1,000,000
micro for an audit's reservation estimate. The quote note explains margin, usage-dependent settlement
and BYOK: a team's own key wins, is unmetered by treg, and may incur separate provider charges.
These are pre-reservation ceilings, not promises to rewrite provider settlement evidence.

`start` atomically claims the durable quote once. Each leg uses native `execute_call`, its normal
authorization, holds, settlement and stable run/leg idempotency key. There is no separate Lens
balance or fallback operator token. Up to four legs run concurrently, each with a 90-second timeout;
a run has a 900-second durable deadline. Database sessions close before provider I/O.

`persist` acknowledges a running marker/call reference before submission and saves returned receipts.
Unknown work stops later waves; requests already in flight can still finish. Browser disconnects
leave the owned task running. Shutdown interrupts and drains owners before closing the HTTP client.
A lost process is recovered when a read sees its expired deadline; row locking preserves receipts
written after an older read. Recovery never resubmits a paid request.

The browser saves `quote.id` before POST; after a lost acknowledgement it reads that run rather than
automatically POSTing again. Replaying a claimed quote only returns its current state. Interrupted
runs retain partial evidence and set `cost_known:false`, `total_cost_micro:null`; confirmed charges
remain in `known_cost_micro`. Review native Activity and billing before explicitly buying a new run.

## Verification

`tests/test_lens.py`, `test_lens_execution.py` and `test_lens_schema.py` cover native HTTP permissions,
claims/revisions, provider execution/settlement and schema/recovery. The Lens Playwright configuration
uses the real dashboard, authentication, database and billing path with only the upstream relay
replaced inside a disposable test server. Install Playwright's configured browsers, build the dashboard,
then run `npm --prefix frontend run test:e2e:lens`. Routine verification makes no paid provider calls.
