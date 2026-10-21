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

## Agent setup status

`loadOrgAdmin` loads the full Team roster on entry and after management actions. Setup cards use
`pollAgentConnected` instead: one `GET /orgs/{org_id}/agents/{user_id}/connection?api_key_id=...`
after each three-second pause, at most forty attempts per visit. The next pause starts only after
the previous request settles; polling never reloads the roster, usage, or observed-agent aggregates.

Both the Members card and the API Keys rotation card track the newly issued key. A previous key's
check-in does not satisfy a replacement's setup. The status endpoint requires an admin in the
selected org, validates the active key's agent membership, and uses an indexed `EXISTS` against
`CallRecord` for that org and key. The full agent roster keeps its lifetime identity history.

`TeamPage` resumes unfinished detection on mount and aborts it on unmount. Closing or replacing a
card, switching teams, and unmounting the controller also cancel it. A request captures its card,
team, and polling session; a late response cannot mark a replacement card connected or restart a
cancelled loop. Authentication or missing-key errors stop detection and remove an inaccessible
credential from the setup card; transient failures retry within the same attempt limit.

Credential writes have a different lifetime from these reads. `credentialIssues` holds one
unacknowledged issuance per org in the dashboard instance's memory, keyed by org ID and identified
by its operation object. Creating an Agent, either rotation control, and additional-key creation
retain successful results even after navigation. `newAgent` / `newApiKey` expose only the active
org's result. A second issuance in the same org waits for acknowledgement, including when the
first POST is pending across a switch; other orgs can issue independently. This state machine
does not abort writes or add automatic mutation retries. The shared `api.ts` transport still has
its existing encoded retry for a string-body request rejected by a 403 HTML edge response; that
behavior is unchanged. An uncertain write reports that it may have completed.

While an issuance needs attention, the API Keys menu disables state-changing actions and explains
the retained-result guard. `requestKeyAction`, `confirmKeyAction`, and `keyAction` also check it
defensively and report the same reason. If the guard becomes active with a confirmation already
open, the dialog stays open with its confirm button disabled; it does not imply the action ran.

`resumeCredentialIssue` checks a retained key before revealing it on receipt or return: the
lightweight connection endpoint for Agents, active key metadata for additional keys, and the
current derived bearer for Default keys. A revoked/inaccessible result is cleared with a notice;
a transient read failure preserves it and offers a read-only retry. Read sessions are guarded and
aborted on navigation so an older check cannot erase or replace a later result. A key can still be
changed in another session after its most recent check; this is not continuous validity monitoring.

The setup card's Done / I've updated action clears the retained secret. There is no separate
Agent-token cache, local/session storage, or persisted queue. Logout and application teardown
clear all retained issuances; closing/reloading the browser tab or losing the POST response before
it is received cannot recover a hash-stored key. The UI tells the user to save it before closing.

## Spend per API key

Team -> API Keys has a "Spent last 30 days" column, from `GET /orgs/{id}/api-keys/spend`, which
reads only the daily spend rollup; amounts show to the cent (cut, never rounded up), here and everywhere
on the Usage Spend card (its total, the chart's axis and tooltip, the Other list and the ranking).
Where an amount can be pointed at, `components/ui/Money.vue` shows its exact micro-USD figure at
once on hover or keyboard focus (the column, the "No API key" total, the card total, the ranking
and the Other list); the chart's own tooltip follows the pointer, so it shows the short figure. Admins also see the team's "No API key" total, with an ⓘ that
says where such charges come from (`NO_KEY` in `state/constants.js`). Until the rollup has caught
up once, every cell shows "Preparing…" with "Spend history is being prepared. It will appear here within a few days." on hover. The page never labels
anything as calls: one call can make several charges.

Activity -> Usage opens with a Spend card from `GET /orgs/{id}/usage/spend`: bars by day, week or
month (`SpendBars.vue`), stacked by API key or, within one provider, by tool, with filters in
`SelectMenu` dropdowns and its own `DateRangePicker`, whose `min` keeps every pick inside the
rollup's history ("History starts on ..."). The card loads on its own, never waits for the usage
counts below it, and says "Spend history is being prepared. It will appear here within a few days." like the API Keys tab until the rollup has
caught up once.

The rest of Usage loads in parallel with the card, each section on its own and with its own
error: the counts (`GET /orgs/{id}/usage`: the stat cards, By member, Top tools, Per day) and each
"Spend by <tag>" section. The 7/30/90-day buttons reload only those, not the card. Top tools and
Per day show ten rows at a time (Per day newest first), paged in the browser: one answer already
holds every row, so a page turn costs no request.

Activity and Team carry their tab in the address (`state/navigation.js`, `parseTabHash`):
`#activity/calls`, `#activity/usage`, `#orgs/members`, `#orgs/keys`, `#orgs/billing` and the rest,
and the Calls tab also its key filter, by id: `#activity/calls?key=7`. The spend chart's key filter
stays inside its card and out of the address, since it filters the chart only, not the tab. A reload
or a shared link opens the same tab and key; changing a tab or key rewrites the current history entry, so Back leaves the page. An API
key's Activity button opens `#activity/calls?key=<id>`. The old `#usage` and `#billing` links still
land, and the address then names the tab. Opening Activity without a tab still lands on Calls.

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
- For `tiktok.tiktok.video.publish.init` the tool drawer's button reads "Publish a video" and the
  Try drawer is TikTok's required post screen (`TikTokPostForm.vue`, `isTikTokPost`), with no tabs,
  endpoint id or JSON body; the agent line stays in the tool drawer. An account picker when more than one connection
  can publish, a video that previews from the local file, then the Content Sharing Guidelines
  settings with nothing preset. After Post the browser PUTs the bytes straight to TikTok's upload
  URL (`*.tiktokapis.com`, which allows any origin); treg relays only creator_info, init and status
  on the chosen connection's tool, so the video never reaches treg.
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

## Showcase tab and pictures

The Team page has a Showcase tab (owners and admins, `#orgs/showcase`): the team's /showcase
leaderboard entries with their review status, "N of 5 used", apply and edit with an icon and up to
four screenshots, consent checkboxes and withdraw; a 409 keeps the draft and offers a reload. Team
settings sets the team's avatar and the account menu's "Your avatar" dialog the person's own,
each from a round + on the avatar's corner (`AvatarEdit.vue`). Uploads are raw bodies to
`POST /images/{kind}`; with uploads off (`/auth/me` `uploads: false`) the controls are hidden.
`Avatar.vue` shows an avatar or initials wherever a team or person is named. See [showcase](../architecture/showcase.md).
