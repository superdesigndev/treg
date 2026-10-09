---
title: The web dashboard (served from FastAPI)
status: shipped
sources:
  - frontend/src/App.vue
  - frontend/src/views.ts
  - frontend/src/state/controller.js
  - frontend/src/state/catalog.js
  - frontend/src/state/navigation.js
  - frontend/src/state/boot.js
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

## Views

| View | URL | Page |
|---|---|---|
| Getting started (default) | `/app#start` | `GettingStartedPage.vue` |
| Catalog; a platform or comparison | `#catalog`, `#platform/<slug>[/<key>]`; public `/catalog[/<slug>[/<key>]]` | `CatalogPage.vue`, `PlatformPage.vue` |
| Connections; a provider | `#connections`; `/app/marketplace/<service>` | `ConnectionsPage.vue`, `ProviderPage.vue` |
| Your own tools | `#tools`, `#secrets`, `#resources` | `ToolsPage.vue`, `SecretsPage.vue`, `TeamResourcesPage.vue` |
| A shared skill or tool | `/app/skills/<name>`, `/app/tools/<name>` | `DetailPage.vue` |
| Activity and its tabs | `#activity` (Usage for an admin, Calls for a member), `#activity/usage`, `#activity/calls`, either with `?key=<api key id>`; old `#usage` | `ActivityPage.vue` |
| Team and its tabs | `#orgs` (the tab open last), `#orgs/members`, `/keys`, `/projects`, `/policy`, `/billing`, `/settings`; old `#billing` | `TeamPage.vue` |
| Hub, Referrals, Admin; find a tool | `#hub`, `#referrals`, `#admin`; public `/search` | the matching page, `SearchPage.vue` |

## Rules every change keeps

- **A hash view is in both whitelists**, `viewFromHash()` (`state/catalog.js`) and the `popstate` list
  (`state/boot.js`). A click works without them; reload and Back silently do not.
- **Only Activity and Team carry a tab in the address.** `parseTabHash` (`state/navigation.js`) reads
  `#activity/<tab>[?key=<id>]` and `#orgs/<tab>` for load, Back and the page preloader (`views.ts`,
  which calls it before the app exists, so it is a plain function); every other hash, the catalog's
  slashed `#platform/…` included, never reaches it. A tab or key change replaces the current history
  entry (`syncTabUrl`, watched in `controller.js`), so Back leaves the page rather than replaying tabs.
  A key is addressed by its id: names repeat and change on rename.
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
- **Activity reads one feed.** `/activity` merges calls, server runs and local runs on the server
  and pages them by one cursor, `(created_at, source, id)`, so Load older never lands rows above
  ones already shown; `/calls` and `/runs` stay for the CLI and API callers.
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
  tools for a job is [find](../architecture/find.md): the answer's events carry `engine`, `reason`
  and `platform`, and `search_answered` sends `engine`, `reason` and `platform_choice` to PostHog. A
  job's fit says whether it is the job's or one vendor's (`fit_from`), a folded job reads "4 of 30
  providers", and an empty answer says whether treg lacks it (`gap`, with a request link) or the text
  did not read as a job.
- The Catalog box (`CatalogSearch.vue`) asks the finder when typing pauses (700 ms) on two
  characters or more. That auto answer is a section above the still-filtered shelves (a platform
  or provider name filters those shelves using both provider IDs and display names). While it reads,
  or when it is `none` or empty, it is one line. Enter (or the suggestion row) asks for the full
  answer (`findFull`): shelves unfiltered and lit where it landed.
- A find result opens its job (`findOpen`): the job's comparison on its platform when that shelf
  compares it, else the tool in the drawer; from the Catalog page the shelf loads first and the
  comparison replaces its history entry. Back returns to the Catalog page with the box, the answer
  and the filtered shelves as they were: the first entry of a page opened at `/catalog` (or
  `/catalog/<slug>`) has no history state, so popstate resolves it from the path.
- A shelf's own box answers from that shelf only. An empty answer there is one line, "Nothing in
  <platform> for …", and "Search all tools" (`findEverywhere`) moves to the Catalog page with the box
  prefilled and the same words asked unscoped. "Request it" appears only on an unscoped gap (and on
  v1's empty answers, which carry no reason).
- Every catalog surface (shelf, comparison, provider page, Catalog index) sends one event set from
  `state/catalogEvents.js`: `catalog_platform_viewed`, `catalog_comparison_viewed`,
  `catalog_tool_opened`, and `catalog_action` for try, copy, connect, own key and docs.

## Tutorial, top bar, referrals

- `tutorial.js` is the only interactive source of the CLI tutorial (its prose mirrors: AGENTS.md),
  rendered by the Help view and `/tutorial`, served `no-cache` and versioned by its own mtime.
  `tour/tour.js` drives the dashboard tour.
- The top bar is words only; GitHub, Discord and X sit in the account menu; the balance shows cents,
  never rounded up. Its sides never shrink below their content, so the nav scrolls rather than overlaps.
- **Referral** leads to the friend credit and the invite-only affiliate tier. The link is never gated
  behind paying, and opening `GET /referrals` runs the payout sweep ([money](../architecture/money.md)).
