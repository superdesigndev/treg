---
title: Landing sandbox backend - closed
status: closed
sources:
  - src/treg/api.py
  - src/treg/routers/onboard.py
  - src/treg/routers/web.py
  - src/treg/domain/identity/access.py
  - frontend/src/state/boot.js
  - frontend/src/components/SignedOutPage.vue
  - src/treg/web/install.sh
related:
  - interface/dashboard.md
  - interface/onboarding.md
  - interface/api.md
---

# Landing sandbox backend

> **Front-end entry removed.** The logged-out SPA no longer renders the sandbox studio or its coach
> tour, stores `localStorage['treg-sbx']`, or calls `POST /demo/sandbox`. A use-case CTA arriving at
> `/app?ref=<page>` keeps the parameter for attribution, strips it as a one-shot parameter, and opens
> the sign-in modal. Plain logged-out `/app` still redirects to `/`. `SignedOutPage.vue` (`/app` while
> signed out) is now a plain "Sign in to treg" page plus the share-link gate — the marketing hero,
> key-leak explanation and footer CTA it used to render were dropped from this component along with
> the sandbox studio; that marketing content now lives on the public landing page (`/`) instead.

> **Closed.** `POST /demo/sandbox` returns 410 and mints nothing. Authentication refuses every
> `@sandbox.treg.local` visitor with 403 ("the demo sandbox is closed"), centrally in
> `domain/identity/access.py`: `_membership_by_token` checks the token's user, and
> `_user_from_identity_token` / `_user_from_session` check theirs, so `require_member`,
> `require_identity` and every direct resolver inherit it. The sandbox was an anonymous identity on
> production guarded by per-route exceptions; one missed route (`POST /onboard/demo`) let a visitor
> mint a real team. The sandbox's backend (the throwaway team, the synthesized calls, the Stripe
> test-charge relay, its caps and reaper) is removed; what stays is the 410 at `POST /demo/sandbox`,
> the central refusal above, and the reserved `sbx-` slug prefix, which matter while visitor rows
> from before the closure remain in a database. Leftover sandbox orgs are deleted deliberately with
> the shared `cascade_delete_org`.

## CLI installer
`GET /install.sh` (`install_sh`) serves `src/treg/web/install.sh`, `{BASE}`-templated like `llms.txt`
(so it targets whatever host is live — a dev box or the real domain). The script now installs from
**PyPI** (`SRC="tools-registry"`, the light CLI package; the FastAPI/DB server stack is the separate
`tools-registry[server]` extra for self-hosters) via `uv tool install --force` → `pipx install --force` →
`pip3 install --user --upgrade`, then runs `treg config --base-url {BASE}`. The uv/pipx paths pin the
supported interpreter range (`PYREQ`, kept in sync with `requires-python` in `pyproject.toml`): without
it uv resolves against its *default* interpreter — its own managed Python first — and a machine whose
default falls outside the range fails resolution instead of picking (or auto-downloading) a compatible
one. The pip3 path cannot pick an interpreter at all, so it checks `pip3 --version` against the range
first and, outside it, prints which Python pip3 belongs to plus the uv one-liner instead of letting
pip fail with its "Ignored the following versions" wall (a real user hit exactly that). It also
installs the official
**tools-registry skill** into every detected agent via `treg skill bootstrap` (Claude Code, Cursor, Codex,
Gemini, Copilot, OpenCode, Windsurf …), together with every other skill in
`/.well-known/skills/index.json` (the workflow skills such as `make-ugc` and `lead-signals`; best-effort,
and an index name must be a plain slug before it becomes a folder), falling back on older CLIs to a Claude-only drop that curls
`{BASE}/skill.md` into `~/.claude/skills/treg`. Because the package is public on PyPI,
`curl … /install.sh | sh` now works for anyone with no repo/git access needed. The **Getting started**
dashboard view (`view==='start'`) surfaces this install command + `treg login`/`onboard`/`add`/`call` and
links to the tutorial and `/llms.txt`; `llms.txt` gives the same install command in its setup paragraph.
