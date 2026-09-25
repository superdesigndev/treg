# Dashboard

This app lives in the public treg repository and is served by the existing Python Web service.
There is no separate production frontend server.

- `src/App.vue`: application shell and conditional page/dialog mounts.
- `src/pages/`: catalog, getting started, activity, tools, team, referrals and help.
- `src/components/` and `src/dialogs/`: shared navigation and overlays.
- `src/state/`: existing Options API use cases, grouped by feature, plus initial state and boot.
- `src/api.ts`: same-origin JSON transport, session expiry and edge-encoding behavior.
- `src/styles/`: base styling. Redesign styles and artwork are shared from `src/treg/web/media/redesign/`.

This is an incremental extraction. The old use cases still share per-application state through
`state/context.ts`; their JavaScript and the shared onboarding widgets are not fully typed.
New isolated components should use typed props and events. Existing hash navigation and deep links
remain in the navigation/catalog/details modules; this change does not replace their URL contract.
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

Browser tests start their own server on :18791 with a disposable database and no dotenv file.
They use full Chromium in headless mode so back/forward cache restoration is exercised.
`PLAYWRIGHT_CHANNEL=chrome` can use an installed Chrome for local checks.

Builds also copy the npm-installed Vue global runtime and license for the standalone Arena page;
these generated files are packaged but never committed. Page runtime versions
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
