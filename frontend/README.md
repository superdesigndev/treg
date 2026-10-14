# Dashboard

This app lives in the public treg repository and is served by the existing Python Web service.
There is no separate production frontend server.

- `src/App.vue`: application shell and conditional page/dialog mounts.
- `src/views.ts`: pages and dialogs as on-demand chunks, with route preload and idle prefetch.
- `src/pages/`: catalog, getting started, activity, tools, team, referrals and help.
- `src/components/` and `src/dialogs/`: shared navigation and overlays.
- `src/agent-setup/`: onboarding widgets shared with the standalone Enrich Arena page.
- `src/state/`: existing Options API use cases, grouped by feature, plus initial state and boot.
- `src/api.ts`: same-origin JSON transport, session expiry and edge-encoding behavior.
- `src/styles/`: base styling. Redesign styles and shell artwork are shared from `src/treg/web/media/redesign/`.
- `src/assets/`: artwork imported by components, hashed by Vite and served from `/app/ui/assets`.

This is an incremental extraction. The old use cases still share per-application state through
`state/context.ts`; their JavaScript is not fully typed.
New isolated components should use typed props and events. Register a new page or dialog in
`src/views.ts`: a static import from `App.vue` puts it back into the entry chunk every visitor
downloads. Existing hash navigation and deep links remain in the navigation/catalog/details
modules; this change does not replace their URL contract.
`frontend/` is the only Dashboard source: every entry, signed in or not, serves this compiled app.

## Develop

Install Node 22.12+ and npm, then run `scripts/dev-local.sh up` from the repository root.
Open `http://localhost:18790/app`; the Python response loads Vite modules from :5173 for hot updates.
The local-only `TREG_FRONTEND_DEV` switch cannot be used with PostgreSQL or a public hostname.
For preview from another device on the LAN, build first with `bash scripts/build-dashboard.sh`,
then run `TREG_FRONTEND_DEV=false scripts/dev-local.sh restart` (or `up` for a stopped stack).
Open the server on port 18790 using the host machine's LAN IP. This serves compiled assets from
the same origin; rerun the build after frontend changes. Loopback Vite hot updates are for
browsers on the development machine.

## Validate and package

From the repository root:

```sh
bash scripts/build-dashboard.sh
npm --prefix frontend test
cd frontend && npx playwright install chromium && cd ..
npm --prefix frontend run test:e2e
uv build
```

Test behavior, not template source strings, CSS class names or component arrangement. Keep transport
unit tests and HTTP rollout/packaging checks; use browser tests for user interactions.

## Tables

Every new table is built from `src/components/ui/table`: the shadcn-shaped primitives (`Table`,
`TableHeader`, `TableBody`, `TableRow`, `TableHead`, `TableCell`, …) and `DataTable`, which takes
column definitions (width, alignment, truncation, wrapping, sort, a heading tip, how the column shows
on a phone) and a `cell-<key>` slot per column. The page owns the rows and the sort. Do not lay out
rows as one CSS grid per row: each row sizes its own columns, so columns stop lining up and a long
value paints over its neighbour. A real table grows the column instead. The sheet's global
`table`/`th`/`td` rules skip `.ui-table`; the older tables still rely on them until they move over.

`e2e/layout.spec.ts` renders the main pages at desktop and phone width and fails on text painted over
other text, text past its row, or a page that scrolls sideways (`textCollisions`, `sidewaysCulprits`
in `e2e/helpers.ts`). Add a page with a table or cards to it.

Browser tests run against the **built** dashboard (`TREG_FRONTEND_DEV=false`), not the source: run
`bash scripts/build-dashboard.sh` after every frontend change, or the browser tests pass or fail on the
previous build. A regression test only counts once it has failed against a build without the fix.

Browser tests start their own server with a disposable database and no dotenv file, on a port
the OS reports free when the run starts, so parallel runs in other worktrees never collide
(`TREG_E2E_PORT` pins one).
They use full Chromium in headless mode so back/forward cache restoration is exercised.
`PLAYWRIGHT_CHANNEL=chrome` can use an installed Chrome for local checks.

Builds also copy the npm-installed Vue global runtime and license for the standalone Arena page;
these generated files are packaged but never committed. The onboarding widgets Arena shares with the
Dashboard live in `src/agent-setup/`: the Dashboard imports them, and `vite.agent-setup.config.ts`
compiles the same source into the generated `/agent-setup.js` classic script for Arena. The Dashboard
bundles Vue's runtime only; do not add runtime `template:` strings or alias `vue` to its compiler build. Page runtime versions
must match the npm lockfile. Three.js and Lenis on the landing page use pinned CDN URLs.

Builds generate `src/treg/web/dashboard/`, which is ignored by Git and included in wheels/sdists.
Do not edit generated files. Distributable package builds fail if these assets are absent; editable
Python installs and background workers do not require Node. The Web build script is
`scripts/build-web.sh`, which compiles the app and retains the locked Python installation.

## Serving and rollback

The server hands every Dashboard, catalog and shared-link entry the same compiled `index.html`,
as private, no-store HTML with `Vary: Cookie`. There is no in-process frontend switch: roll back a
Dashboard change by deploying the previous build. Open tabs compare the app-version stamp in `/meta`
and offer a refresh when a deploy changes the bundle.
