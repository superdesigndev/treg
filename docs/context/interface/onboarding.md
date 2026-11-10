---
title: Onboarding - first run in the dashboard and `treg onboard`
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

## Dashboard: the first team is made, never asked for

For a non-onboarded session with no team and no pending invite, `maybeOnboard()` calls `autoTeam`,
which posts `POST /onboard/team` (`application/onboard/first_run.py`). The server claims the
account's one first run by flipping `User.onboarded`, so a second tab gets `created: false` and
makes nothing; a refused create hands the claim back so a retry can make it. The team goes through
the ordinary signup door (`signup.create_org`), so signup credit, utm, ads attribution and referral
behave as for a team the user named. Declining an invite on first run also goes through `autoTeam`.

When `created: true`, the welcome dialog opens over the view the user landed on; Getting started
carries the same agent picker and setup line for anyone who skipped it. A use-case landing's
`/app?ref=<page>` is stashed in `localStorage['treg-ref']` because sign-in reloads the page, and
`_welcomeAgentFromRef` preselects that agent (unknown refs are ignored).

Getting started shows the team's signed Default key, which is revealable again on purpose: the
server derives it from signed identity, team and Default generation. Additional and agent keys are
random secrets shown once. Rotating the Default changes only this team's generation.

## CLI: `treg onboard`

Four paths (`_PATHS`): **catalog** (default when interactive), **setup**, **access**, **demo**.
Non-interactive runs pick by team state: a team with tools -> access, an empty team you admin ->
setup, else catalog. After a first human `treg login`, `_maybe_offer_onboarding` offers it once
(TTY only); a decline posts `/onboard/skip`.

- **catalog** needs no team, secret or tool. It asks `/catalog/endpoints/<id>/access` how that
  exact call would be served and branches on the answer instead of hardcoding an endpoint: treg's
  key (price shown and confirmed), the team's own credential (not metered), or a dead-end naming
  the command that fixes it, with nothing called. A server with no platform providers enabled
  (`TREG_PLATFORM_PROVIDERS` empty, the default) sends every user without a key to the dead-end.
- **setup** imports the cwd `.env` and the chosen skill folders in one pass deduped by skill name.
  "This project" is hidden when the cwd is `/`, `$HOME` or its parent (`_is_rootish`), so setup
  never sweeps a whole account.
- **access** installs selected team skills and makes one test call. It never pulls keys.
- **demo** is illustrative: no team is created and nothing is uploaded. The one real call it makes
  uses a tool the active team already has; the teammates it shows are labelled examples.
  `--mode quick` maps to this path.

## Demo teams (legacy seed)

`application/onboard/demo.py` still seeds a real `Org.demo=True` team with fake `User.demo=True`
teammates on `demo.treg.local` and an `echo` tool. `POST /onboard/demo`, `/onboard/seed-tool` and
`/onboard/accept-teammate` have no first-party caller left; `POST /onboard/reset` (Help's "Remove
demo teammates", `treg onboard --reset`) deletes the caller's demo orgs through
`cascade_delete_org` and sweeps orphaned demo users. While demo rows can exist, these guards stay:

- `auth_email_start` refuses any `@demo.treg.local` address, so a fake teammate is never a login.
- Invites to `@demo.treg.local` send no email.
- `admin_stats` excludes demo users, demo orgs and everything scoped to them.
- A new demo team counts against the owned-team cap (`require_owned_team_slot`).
