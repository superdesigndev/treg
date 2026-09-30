---
title: The web dashboard (served from FastAPI)
status: shipped
sources:
  - frontend/src/App.vue
  - frontend/src/views.ts
  - frontend/src/state/controller.js
  - frontend/src/styles/base.css
  - frontend/e2e/layout.spec.ts
  - src/treg/web/media/redesign/dashboard.css
  - src/treg/routers/web.py
  - src/treg/web/tutorial.js
  - src/treg/web/tour/tour.js
related:
  - interface/api.md
  - interface/seo.md
  - architecture/catalog.md
  - architecture/search-experiment.md
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

## Views

| View | URL | Page |
|---|---|---|
| Getting started (default) | `/app#start` | `GettingStartedPage.vue` |
| Catalog; a platform or comparison | `#catalog`, `#platform/<slug>[/<key>]`; public `/catalog[/<slug>[/<key>]]` | `CatalogPage.vue`, `PlatformPage.vue` |
| Connections; a provider | `#connections`; `/app/marketplace/<service>` | `ConnectionsPage.vue`, `ProviderPage.vue` |
| Your own tools | `#tools`, `#secrets`, `#resources` | `ToolsPage.vue`, `SecretsPage.vue`, `TeamResourcesPage.vue` |
| A shared skill or tool | `/app/skills/<name>`, `/app/tools/<name>` | `DetailPage.vue` |
| Activity, Team, Hub, Referrals, Admin; find a tool | `#activity`, `#orgs`, `#hub`, `#referrals`, `#admin`; public `/search` | the matching page, `SearchPage.vue` |

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
- **Prices come from the server's price objects**; the client holds no FX constant.
- **New tables use `components/ui/table`**; the sheet's old table rules are `:where()`-scoped.
- **`e2e/layout.spec.ts`** fails on text painted over text or past its row, a page scrolling
  sideways, or overlapping top-bar items, at desktop and phone width.

## Catalog, Connections, a provider

- **Two questions, two pages.** The Catalog says what an agent can call; Connections says whose
  credential it calls with. They, a platform shelf, a provider's page, Your own tools and Activity
  share one look: a display-font title, sections under a rule, a filter row (`FilterBox`, a
  segmented switch), things as cards, and lists as `DataTable` on a card surface.
- **A card lifts only when it opens something** (a link or button); a panel, form or figure stays put.
- **Every credential for a catalog provider is a Connections card**: a connection, or a secret named
  exactly for a pasted-key provider, which the credential ladder uses the same way. Secrets lists
  only what the team's own tools use. Telling the two apart compares lists, so `loadConnections`
  fetches `/connections` and `/secrets` together.
- **Status comes from the server** (expiry, health, `needs_extra_credential`, an unchosen resource);
  the status is a soft pill and the fix is the card's primary button.
- **Only providers this deployment can connect are offered** (`configured`), account ones first.
  Labels come from `oauth_providers.listing()`; templates never name a provider.
- **A pasted key is one per team per provider**, so a second replaces it. OAuth consent opens in a
  popup the page polls (`/oauth/status/{state}`). Bring your own key, from anywhere, lands here.

## Platform shelf, comparison, drawer

- A shelf ranks everything by 30-day observed calls, whether one provider serves it or several: its
  six most used are cards of one height, the rest one list, and account plumbing shows its first six.
  With no counts yet the cards read Featured, not Most used. A compared job's card carries its short
  title (`capability_titles`, [catalog](../architecture/catalog.md)), the full description on hover.
- A comparison leads with Auto-route when a routed tool exists; its counts are shown in bands, never exact.
- The tool drawer is not modal; its primary button is the agent's next step (`drawerNext`). Finding
  tools for a job is [search-experiment](../architecture/search-experiment.md).
- The `catalog-v2` experiment shows the old ledger (`LegacyPlatformPage.vue`) to its control arm; it
  ends by deleting that page, `state/catalogExperiment.js` and the arm checks.

## Tutorial, top bar, referrals

- `tutorial.js` is the only interactive source of the CLI tutorial (its prose mirrors: AGENTS.md),
  rendered by the Help view and `/tutorial`, served `no-cache` and versioned by its own mtime.
  `tour/tour.js` drives the dashboard tour.
- The top bar is words only; GitHub, Discord and X sit in the account menu; the balance shows cents,
  never rounded up. Its sides never shrink below their content, so the nav scrolls rather than overlaps.
- **Referral** leads to the friend credit and the invite-only affiliate tier. The link is never gated
  behind paying, and opening `GET /referrals` runs the payout sweep ([money](../architecture/money.md)).
