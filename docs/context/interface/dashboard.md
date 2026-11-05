---
title: The web dashboard (served from FastAPI)
status: shipped
sources:
  - frontend/src/App.vue
  - frontend/src/pages/team/TeamShowcase.vue
  - frontend/src/pages/team/TeamAvatar.vue
  - frontend/src/dialogs/UserAvatarDialog.vue
  - frontend/src/components/ui/Avatar.vue
  - frontend/src/images.ts
  - frontend/src/views.ts
  - frontend/src/state/controller.js
  - frontend/src/state/agents.js
  - frontend/src/state/agentsComputed.js
  - frontend/src/state/credentialIssues.js
  - frontend/src/state/data.js
  - frontend/src/state/keys.js
  - frontend/src/state/session.js
  - frontend/src/state/team.js
  - frontend/src/pages/TeamPage.vue
  - frontend/tests/agents.test.ts
  - frontend/e2e/agents.spec.ts
  - frontend/src/state/catalog.js
  - frontend/src/state/boot.js
  - frontend/src/components/SignedOutPage.vue
  - frontend/src/styles/base.css
  - frontend/e2e/layout.spec.ts
  - src/treg/web/media/redesign/dashboard.css
  - src/treg/routers/web.py
  - src/treg/web/tutorial.js
  - src/treg/web/tour/tour.js
  - frontend/src/dialogs/TryEndpointDialog.vue
  - frontend/src/state/tryTool.js
  - frontend/src/dialogs/TikTokPostForm.vue
  - frontend/src/tiktokPost.ts
related:
  - interface/api.md
  - interface/seo.md
  - architecture/catalog.md
  - architecture/find.md
---

# Web dashboard

A Vue 3 app in `frontend/`: pages, dialogs and components as SFCs, Options API state split by feature
in `frontend/src/state/` and merged in `controller.js`. The code and its comments are the reference;
this page keeps what no single file shows. The look follows the root `design.md`.

## Build and serving

- `bash scripts/build-dashboard.sh` builds into the gitignored `src/treg/web/dashboard/`, which the
  wheel ships and refuses to build without. `scripts/dev-local.sh up` runs Python and Vite.
- `_dashboard_index` (`routers/web.py`) serves one entry for `/app`, shared links and `/catalog`,
  signed in or not, `no-store` with `Vary: Cookie`; hashed assets are immutable under `/app/ui/assets/`.
  `/meta`'s version is the bundle hash, so an open tab offers a refresh after a deploy.
- Each page and dialog is its own chunk (`views.ts`): the one for the opened URL loads during boot,
  the rest in idle time, so navigation never shows a blank frame. Vue's runtime is bundled, no CDN.
- `sitetrack.js` and `adtrack.js` load `defer` in `<head>` before the app, so attribution is captured
  before boot can redirect (`tests/test_adsconv.py`).

## Signed-out boot (`state/boot.js`)

A plain signed-out visit to `/app` `location.replace('/')`s to the landing. Exceptions open sign-in in
place: invites, OAuth returns, a shared `/app/skills/<x>` or `/app/tools/<x>` link (the share gate in
`SignedOutPage.vue`, which lands on the shared item after sign-in) and a use-case CTA `?ref=<page>`.
`ref` is stored as `treg-ref` (it survives the sign-in reload so Getting started can preselect the
agent the landing was about), and all one-shot parameters are stripped with `history.replaceState`
so a reload or shared URL does not replay them.

## Views

| View | URL | Page |
|---|---|---|
| Getting started (default) | `/app#start` | `GettingStartedPage.vue` |
| Catalog; a platform or comparison | `#catalog`, `#platform/<slug>[/<key>]`; public `/catalog[/<slug>[/<key>]]` | `CatalogPage.vue`, `PlatformPage.vue` |
| Connections; a provider | `#connections`; `/app/marketplace/<service>` | `ConnectionsPage.vue`, `ProviderPage.vue` |
| Your own tools | `#tools`, `#secrets`, `#resources` | `ToolsPage.vue`, `SecretsPage.vue`, `TeamResourcesPage.vue` |
| A shared skill or tool | `/app/skills/<name>`, `/app/tools/<name>` | `DetailPage.vue` |
| Activity, Team, Hub, Referrals, Admin; find a tool | `#activity`, `#orgs`, `#hub`, `#referrals`, `#admin`; public `/search` | the matching page, `SearchPage.vue` |

Activity and Team carry their tab in the address (`state/navigation.js`, `parseTabHash`:
`#activity/calls?key=7`, `#orgs/keys`, ...); a tab change rewrites the current history entry, so Back
leaves the page. Old `#usage` and `#billing` links still land.

## Rules every change keeps

- **A hash view is in both whitelists**, `viewFromHash()` (`state/catalog.js`) and the `popstate` list
  (`state/boot.js`). A click works without them; reload and Back silently do not.
- **Dialogs mount at the App root**, never inside a page (a nested one failed to render), with
  `v-dialog` and a label: focus in, Tab trapped, Escape closes unless the decision is required.
- **A late answer never overwrites a newer one**: a loader a newer call or a team switch can overtake
  takes a ticket (`state/tickets.js`) and checks it after every await.
- **A view renders nothing it cannot yet know**: empty states and zeros wait for their data.
- **Browser storage only through `state/storage.js`**, which never throws (Safari with site data blocked).
- **Templates cannot reach `location`**; navigation off the app goes through a method.
- **Destructive actions confirm inline**, never `confirm()`; `go()` disarms every confirm.
- **The public catalog is the same code**: `publicCatalog` hides member-only parts ([seo](seo.md)).
- **Prices come from the server's price objects**; the client holds no FX constant. Money shows to
  the cent, cut and never rounded up; `components/ui/Money.vue` reveals the exact micro-USD figure.
  Spend views never label charges as calls: one call can make several charges.
- **New tables use `components/ui/table`**; the sheet's old table rules are `:where()`-scoped.
- **Templates never name a provider**; labels come from `oauth_providers.listing()`, and only
  providers this deployment can connect (`configured`) are offered.
- **`e2e/layout.spec.ts`** fails on text painted over text or past its row, a page scrolling
  sideways, or overlapping top-bar items, at desktop and phone width.

## Agent setup and issued credentials

Setup cards detect a new agent with `pollAgentConnected`: one lightweight
`GET /orgs/{org_id}/agents/{user_id}/connection?api_key_id=...` per pause, bounded attempts, the next
pause only after the previous request settles, never reloading the roster. The check is for the
newly issued key (a previous key's check-in does not satisfy a replacement). Each request captures
its card, team and polling session, so a late response cannot mark a replacement connected or
restart a cancelled loop; leaving the card, switching teams or unmounting cancels it.

`credentialIssues` holds at most one unacknowledged issuance per org, in memory only (no storage, no
persisted queue), so a created key survives navigation until Done. A second issuance in the same org
waits for acknowledgement, including across a pending POST; while one needs attention, key actions
are disabled (and checked again in `requestKeyAction`, `confirmKeyAction`, `keyAction`). Writes are
never aborted or retried automatically; an uncertain write says it may have completed.
`resumeCredentialIssue` rechecks a retained key before revealing it again and clears a revoked one.
Logout and teardown clear everything; a lost POST response or closed tab cannot recover a
hash-stored key, and the UI says to save it before closing.

## Spend

`GET /orgs/{id}/api-keys/spend` (Team -> API Keys) and `GET /orgs/{id}/usage/spend` (Activity ->
Usage Spend card) read only the daily spend rollup; until it has caught up once they show
"preparing", and the card's `DateRangePicker` `min` keeps picks inside the rollup's history. The
card loads independently of the usage counts, and each Usage section loads and errors on its own.
Admins also see the team's "No API key" total (`NO_KEY` in `state/constants.js`).

## Catalog, Connections, a provider

- **Two questions, two pages.** The Catalog says what an agent can call; Connections says whose
  credential it calls with.
- **Activity reads one feed.** `/activity` merges calls, server runs and local runs on the server
  and pages them by one cursor, `(created_at, source, id)`, so Load older never lands rows above
  ones already shown; `/calls` and `/runs` stay for the CLI and API callers.
- **Every credential for a catalog provider is a Connections card**: a connection, or a secret named
  exactly for a pasted-key provider, which the credential ladder uses the same way. Secrets lists
  only what the team's own tools use. Telling the two apart compares lists, so `loadConnections`
  fetches `/connections` and `/secrets` together.
- **Status comes from the server** (expiry, health, `needs_extra_credential`, an unchosen resource).
- **A pasted key is one per team per provider**, so a second replaces it. OAuth consent opens in a
  popup the page polls (`/oauth/status/{state}`).

## Platform shelf, comparison, drawer, find

- A shelf ranks by 30-day observed calls; with no counts yet it says Featured, not Most used. A
  compared job's card carries its short title (`capability_titles`, [catalog](../architecture/catalog.md)).
- A comparison leads with Auto-route when a routed tool exists; its counts are shown in bands, never exact.
- The tool drawer is not modal; its primary button is the agent's next step (`drawerNext`).
- Finding tools for a job is [find](../architecture/find.md). The Catalog box (`CatalogSearch.vue`)
  asks the finder when typing pauses on two or more characters and shows that answer above the
  still-filtered shelves; Enter asks for the full answer (`findFull`). A find result opens its job
  (`findOpen`): the comparison on its shelf when that shelf compares it, else the tool drawer. Back
  returns to the Catalog page with the box, answer and shelves as they were; the first entry of a
  page opened at `/catalog[/<slug>]` has no history state, so popstate resolves it from the path.
  A shelf's own box answers from that shelf only. "Request it" appears only on an unscoped gap (and on
  v1's empty answers, which carry no reason).
- For `tiktok.tiktok.video.publish.init` the Try drawer is TikTok's required post screen
  (`TikTokPostForm.vue`, `isTikTokPost`) with the Content Sharing Guidelines settings and nothing
  preset. The browser PUTs the video straight to TikTok's upload URL (`*.tiktokapis.com`, which
  allows any origin); treg relays only creator_info, init and status, so the video never reaches treg.
- Every catalog surface sends one event set from `state/catalogEvents.js`.

## Tutorial, top bar, referrals, pictures

- `tutorial.js` is the only interactive source of the CLI tutorial (its prose mirrors: AGENTS.md),
  rendered by the Help view and `/tutorial`, served `no-cache` and versioned by its own mtime.
  `tour/tour.js` drives the dashboard tour.
- The top bar's sides never shrink below their content, so the nav scrolls rather than overlaps.
- **Referral** links are never gated behind paying, and opening `GET /referrals` runs the payout
  sweep ([money](../architecture/money.md)).
- The Team Showcase tab (`#orgs/showcase`, owners and admins) manages /showcase entries; a 409 keeps
  the draft and offers a reload. Avatars upload as raw bodies to `POST /images/{kind}`; with
  `/auth/me` `uploads: false` the controls are hidden. See [showcase](../architecture/showcase.md).
