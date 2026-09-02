---
title: Onboarding — the first-run demo team (dashboard + CLI)
status: shipped
sources:
  - src/treg/application/auth.py
  - src/treg/application/onboard/__init__.py
  - src/treg/application/onboard/demo.py
  - src/treg/cli.py
  - src/treg/routers/auth.py
  - src/treg/routers/onboard.py
  - src/treg/web/index.html
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

## The one brain - `src/treg/application/onboard/demo.py`

`provision(db, owner, team_name)` seeds a REAL org owned by the caller, marked `Org.demo=True`:

- **Fake teammates** (`TEAMMATES`): Ada·admin, Ben·member, Cora·viewer — roster-only `User` rows
  with `demo=True` on the unusable domain **`demo.treg.local`** (`DEMO_DOMAIN`). Reused across demo
  orgs (email is unique); they get a Membership but **no personal org** and **cannot log in** (see
  the OTP guard below).
- **A working tool** (`echo` → `postman-echo.com`) + its `echo-key` secret, so **Try-it / `treg call`
  returns 200 with the injected `Authorization: Bearer sk-demo-…`** — the aha.
- **Sample activity** (`SAMPLE_CALLS`): a few `CallRecord`s attributed to teammates so Activity is alive.

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

`provision` (full auto-seed) backs the Demo path. The narrower helpers — `seed_tool(db, org, owner_email)`
(adds the `echo` tool+secret, idempotent) and `accept_demo_invite(db, org_id, invite)` (creates the fake
teammate `demo=True` and accepts a pending invite; `GUIDED_TEAMMATE` = Alex Rivera, `alex@demo.treg.local`,
member) — were the dashboard stepper's backing; with the stepper removed they have no UI caller, though
their endpoints remain.

Idempotent — `existing_demo_org` reuses the caller's demo org instead of stacking. Marks
`owner.onboarded=True`. `reset(db, owner)` deletes every demo org the caller owns through the shared
`cascade_delete_org` (`domain/governance/teams.py`, no private table list), drops demo-teammate memberships from the caller's REAL teams too, and sweeps
any demo user left with zero memberships — a clean exit, no litter.

`application.onboard` owns the session and commit boundary for each onboarding journey. The router
keeps identity and role dependencies plus HTTP error translation; demo provisioning, skip/reset,
tool seeding, and teammate acceptance run in short use-case-owned sessions.

## Endpoints (`routers/onboard.py`, all identity/member-scoped)

- `POST /onboard/demo {team_name}` (`require_identity`) → `application.onboard.provision_demo`
  (CLI quick mode: full seed).
- `POST /onboard/seed-tool` (`require_member`, member+) → `application.onboard.seed_tool` into the active team.
- `POST /onboard/accept-teammate {email}` (`require_member`, admin+, demo-domain only) →
  `application.onboard.accept_teammate` — auto-joins the teammate the user just invited.
- `POST /onboard/skip` → sets `onboarded=True` without seeding (dismiss, don't re-offer).
- `POST /onboard/reset` → `demo.reset`.
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

## Dashboard face (`web/index.html`)

The old docked "Getting started" stepper (`onb.*` state, `.onb-panel`/`.onb-push`/`.onb-shift`) is
**removed** — its content had drifted from the product and it kept re-appearing after signup. First-run
now is a **four-step welcome modal**: boot reads `onboarded` from `/auth/me`; `maybeOnboard()` opens it
only for a non-onboarded session with **no team yet**. Step 0 names the team (`welcomeCreate` → `POST /orgs`,
marks onboarded via `/onboard/skip`); step 1 asks **which agent you're using** (picker with LobeHub icons,
skippable); step 2 shows the per-agent **setup block** — the setup line and the team+token as ONE
copyable unit (`welcomeSetupFull` copies the real token; `welcomeSetupMasked` renders it masked with a
Show/Hide-key toggle, `startTokenShow`); step 3 is **"Try it out"** — a "waiting for your agent" status (pulsing `.wc-waitdot`, no 🎉), the
same `tryExamples` copy cards AND the `tryOauth` connect chips as the Getting-started page, footer
**"Skip"** (`welcomeFinish`) + **"Browse all catalog →"** (`go('connections')`). Finishing (or skipping) lands on **`#start`** (Getting started) — also the default
view for ANY signed-in arrival at `/app` with no deep link or hash. `/onboard/seed-tool` and
`/onboard/accept-teammate` no longer have a dashboard caller (the CLI/demo paths don't use them either);
**"Remove demo"** (`resetDemo` → `/onboard/reset`) remains in Help. A clay **`demo` chip** marks a demo org.
