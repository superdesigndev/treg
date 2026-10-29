---
title: Onboarding — first run (dashboard + CLI)
status: shipped
sources:
  - src/treg/application/auth.py
  - src/treg/application/onboard/__init__.py
  - src/treg/application/onboard/demo.py
  - src/treg/application/onboard/first_run.py
  - src/treg/web/sitetrack.js
  - src/treg/cli.py
  - src/treg/routers/auth.py
  - src/treg/routers/onboard.py
  - frontend/src/state/onboarding.js
  - frontend/src/pages/GettingStartedPage.vue
related:
  - interface/api.md
  - interface/cli.md
  - interface/dashboard.md
  - architecture/data-model.md
---

# Onboarding

A brand-new user's fastest path to *believing* treg ("call a real API with no key on your machine")
is to **do it** on a team that's already alive. So onboarding hands them a **team they own**,
seeded with teammates, a working tool, and a real audit trail — one backend brain, two faces.

## Retired demo teams - `src/treg/application/onboard/demo.py`

Onboarding once seeded a REAL org owned by the caller, marked `Org.demo=True`, with roster-only fake
teammates (`demo=True` users on the unusable domain **`demo.treg.local`**, `DEMO_DOMAIN`, which
**cannot log in**; see the OTP guard below), an `echo` tool and sample activity. Nothing seeds those
teams any more; what remains is the way out. `reset(db, owner)` deletes every demo org the caller owns
through the shared `cascade_delete_org` (`domain/governance/teams.py`, no private table list), drops
demo-teammate memberships from the caller's REAL teams too, and sweeps any demo user left with zero
memberships.

**CLI onboarding is 4 paths** (`cmd_onboard` → `_dispatch_onboard`, one of `_run_catalog`/`_run_setup`/
`_run_access`/`_run_demo`; `_PATHS = {1:catalog, 2:setup, 3:access, 4:demo}`).

**Path 1, the catalog, is the default** and the only one that needs nothing at all — no team, no
secret, no registered tool. `_catalog_pick` offers four jobs (a TikTok profile, backlinks, subreddit
posts, a work email) or free text, searches `/catalog/search`, and takes the top hit. `_run_catalog`
then asks `/catalog/endpoints/<id>/access` **how that exact call would be served** and branches on
the answer rather than assuming: `platform` → state the price, confirm, call, show the balance;
`credential`/`tool` → call it and say plainly that your own key is not metered; anything else → an
honest dead-end naming the one command that fixes it, and **nothing is called**.

That last branch is not hypothetical: with `TREG_PLATFORM_PROVIDERS` unset (as in production today)
every user lands there. Asking the server instead of hardcoding a demo endpoint is what lets the same
path become the price-and-call flow the moment the switch is flipped — and stops the walkthrough
rotting when the catalog moves. A TTY run opens with a one-second `_splash` decrypt
animation (the wordmark reveals behind a ░▒▓ wavefront; any key skips; off-TTY / `NO_COLOR` / dumb
terminals never see it), then `_pick_path` presents an **arrow-key menu** (`_menu` — ↑↓/jk move, ↵ confirm,
1-9 jump-pick; where raw-key mode is unavailable it falls back to questionary). The **interactive default is
the catalog**; the smart org-based default (a team with tools → **Use your team's tools**, an empty team
you admin → **Share your own**, else **the catalog**) applies **only non-interactively**. Menu labels:
**Call something now** · **Share your own keys & skills** · **Use your team's tools** · **See how it works**:
- **Setup** (`_run_setup`, path `setup`) — first asks **"Import skill/secret from where?"** via `_menu`:
  this project / global agent folders / both / an **other project repo** typed inline (a `_menu` type-in row
  with fish-style folder autosuggestion — → / tab accept the ghost completion). "This project" is hidden
  when the cwd is root-ish (`_is_rootish` — `/`, `$HOME`, `/Users`) so Setup can't sweep the whole account;
  a typed path that isn't a directory re-prompts via `questionary.path`. `--source local|global|both` skips
  the question; non-TTY or `--yes` never prompts and keeps the local scan, falling back to global only when
  the project has nothing to share. Global = `agents.detect_installed()` → each agent's `global_dir()`
  (`~/.claude/skills`, `~/.codex/skills`, …), kept only when it actually holds skills. Then imports the cwd
  `.env` (API keys — local scope only; global folders carry no project `.env`) then ALL chosen skill folders
  in **one deduped pass** (`_import_skills` takes a list of dirs — the cwd's top-level skills + every
  agent's project dir `.claude/skills`/`.agents/skills`/… from the `agents` registry, plus the chosen
  global dirs — deduped by skill NAME so a mirror-installed skill isn't prompted twice), with `--no-oauth`
  (no forced browser consent) and a batched `POST /health/run` (surfaces `N healthy · M unchecked (no
  probe)`), then a **✓ Done** hand-off pointing at the team's skills + secret vault (`{base}`). Missing
  skill creds are prompted, not skipped.
- **Connect existing tool-registry** (`_run_access`, path `access`) — lists the team's tools + skills,
  multi-selects skills to `skill install` (one call → one summary; kept skills surfaced), then one no-key
  test call (`_onboard_test_call`, prefers a probe/example path so it hits a REAL endpoint). Never pulls keys.
- **Demo** (`_run_demo`) — a purely **illustrative** walkthrough: **no team is created, nothing is uploaded**
  (avoids the "real data in a throwaway demo org" trap entirely). Four beats: ① `_demo_scan_preview`
  ("Auto-discover local skills & env" — read-only, "this is just a DEMO, nothing is uploaded") → ② "Share
  credentials & skills with your team" (example roles owner/admin/member/viewer) → ③ `_demo_teammate_call` —
  auto-picks ONE **real** callable tool the active team has (excludes `echo`), **displays the real upstream
  URL** (`treg call https://api.resend.com/domains`) so it's unmistakably a real API but **executes via the
  tool-name form** (reliable; the host-passthrough form can be ambiguous with duplicate hosts); Stripe
  example if none → ④ `_demo_call_log` — an illustrative ledger: the call you just made plus example
  teammates on YOUR email domain (so they read as real). The `echo` tool and the old seed-a-team flow are gone.

`application.onboard` owns the session and commit boundary for each onboarding journey. The router
keeps identity dependencies and HTTP error translation; skip and reset run in short use-case-owned
sessions.

## Endpoints (`routers/onboard.py`, all identity/member-scoped)

- `POST /onboard/skip` → sets `onboarded=True` without seeding (dismiss, don't re-offer).
- `POST /onboard/reset` → `demo.reset`.
- `POST /onboard/team` (`require_identity`) → `application.onboard.first_run.first_team`: a new
  account's first team, made for it; see the dashboard face below.
- `GET /auth/me` returns `onboarded`; `GET /orgs` rows carry `demo`. `create_invite` **skips the Resend
  email** for `@demo.treg.local` invitees.
- **Guards:** `auth_email_start` refuses any `@demo.treg.local` email (400) — fake teammates are never
  a login. `admin_stats` excludes the whole demo footprint (demo users, demo orgs, and everything
  scoped to them) so platform totals stay honest.
- **Schema:** `User.onboarded` / `User.demo` / `Org.demo` (see [data-model](../architecture/data-model.md);
  schema changes are Alembic revisions under `src/treg/alembic/`).

## CLI face (`treg onboard`)

Colourful (ANSI truecolor, Ledger palette; suppressed off-TTY / under `NO_COLOR`). `cmd_onboard` first
ensures an active org (`_pick_active_org` — the flows need an identity token so requests carry
`X-Treg-Org`; a per-org agent token is org-bound), plays `_splash` (skipped for scripted `--path`/`--yes`
runs), then routes through `_pick_path` → `_dispatch_onboard`. Slow steps (team lookup, scans, network
fetches) show a `_spinner`; `_onboard_active_org` caches its `/orgs` result in `_ORG_CACHE` so a single run
never re-fetches. Shared drawing helpers: `_section` dividers, `_brand`, `_cmd` (shows the actual command),
`_kv`, `_tip` amber asides, `_ok`.

The three paths are **Setup / Connect / Demo** (see the path descriptions above): **Setup** and **Connect**
do real work against the active team; **Demo** (`_run_demo`) is illustrative — no team is created, nothing
is uploaded — four beats (`_demo_scan_preview` → roles → `_demo_teammate_call` a real no-key call when a
callable tool exists → `_demo_call_log`), then `_demo_next_steps`.

After a first **human** `treg login`, `_maybe_offer_onboarding` prompts `[Y/n]` then `_pick_path` +
`_dispatch_onboard` — **TTY-only / CI-safe**; a decline posts `/onboard/skip` so it never re-asks.

## Dashboard face (`frontend/src/state/onboarding.js`, `GettingStartedPage.vue`)

A new account is never asked to name a team. Boot reads `onboarded` from `/auth/me`; for a
non-onboarded session with **no team yet** and no pending invite, `maybeOnboard()` calls `autoTeam`,
which posts `POST /onboard/team`. The server (`first_run.first_team`) claims the account's one first
run by flipping `User.onboarded` (a second tab gets `created: false` and makes nothing), then makes
the team through `signup.create_org` under the first of `first_run.team_names` the registry accepts:
a work address's company (the domain's main label: mail.acme.com and acme.co.uk are Acme), else the
address's first name when it reads as one (john.doe@ is "John's team"), else "My team". Signup
credit, utm stamping, ads attribution and referral therefore behave as for a team the user named; a
refused create hands the claim back so a retry can make it. The dashboard then fires
`onboarding_team_created` (`auto: true`) and the browser-side Google Ads conversion, and the user
stays on the view they landed on: Getting started (`#start`, the default for a signed-in arrival
with no deep link or hash) or the platform, provider or run page a deep link opened. Getting started
says "We made your team <name>. Rename it any time." once, linking to Team. Declining an invite on
first run goes through `autoTeam` too.

When `autoTeam` made the team (`created: true`), the welcome modal (`WelcomeDialog.vue`) opens over
that view with three steps: pick your agent (`onboarding_agent_picked`), the per-agent setup line with
the team's key, and the Try it out cards (a first prompt to copy). Skip or Escape fires
`onboarding_finished` with the step and lands on Getting started unless the user is on a platform page.
Getting started carries the same agent picker, setup line and Try it out cards for anyone who skipped. A use-case landing's CTA (`/app?ref=grokbot`
from `/grokbot`'s "Setup treg") **preselects that agent**: the `ref` param is stashed in
`localStorage` (`treg-ref`) because sign-in reloads the page, and `_welcomeAgentFromRef` consumes it
when `autoTeam` runs (unknown refs are ignored). The picked agent itself persists as `treg-agent` (a
`welcome.agent` watcher; `_restoreAgent` at boot) so Getting started keeps showing the same steps
after a reload.
**"Remove demo"** (`resetDemo` → `/onboard/reset`) remains in Help. A clay **`demo` chip** marks a demo org.

Getting Started's key is the active team's signed Default key. It is intentionally revealable again:
the server derives it from signed identity, team, and Default generation, while additional and agent
keys are random secrets whose plaintext is shown once and then discarded. A disabled Default hides
the token and exposes only **Enable key**; rotating it changes this team's generation and revealable
token without changing another team's key.
