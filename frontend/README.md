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
The deprecated, frozen rollback artifact lives in `src/treg/web/dashboard-legacy/` and exists
only during rollout. `frontend/` is the only maintained Dashboard source. Do not backport features
or routine fixes, or refresh the snapshot when syncing main. The server selects the frontend by
authenticated user ID.

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

Builds also copy the npm-installed Vue global runtime and license for standalone pages and the
legacy snapshot; these generated files are packaged but never committed. Page runtime versions
must match the npm lockfile. Three.js and Lenis on the landing page use pinned CDN URLs.

Builds generate `src/treg/web/dashboard/`, which is ignored by Git and included in wheels/sdists.
Do not edit generated files. Distributable package builds fail if these assets are absent; editable
Python installs and background workers do not require Node. The Web build script is
`scripts/build-web.sh`, which compiles the app and retains the locked Python installation.

## Gradual rollout

The homepage (`/`) uses the new landing page for all visitors; it has no experiment or rollout
switch. The settings below apply only to the Dashboard, catalog and shared-link entries.
Disabling Dashboard rollout does not revert the homepage.

Production defaults to the frozen legacy Dashboard frontend. Configure the Web service:

- `TREG_DASHBOARD_ROLLOUT_ENABLED=true` enables rollout; `false` forces legacy for everyone.
- `TREG_DASHBOARD_ROLLOUT_USER_IDS='[123,456]'` is the JSON array of allowed numeric user IDs.
- `TREG_DASHBOARD_ROLLOUT_PERCENT=0` starts with only the allowlist. Increase toward 100 to
  include stable account buckets; email changes, team switches and browser changes do not reshuffle them.

Anonymous visitors (including the public catalog and token-only browsers) stay on legacy.
The one exception is `/search`, which exists only in this frontend: it is served to every visitor
while rollout is enabled and returns 404 when rollout is disabled.
After browser sign-in, the reload selects the account's frontend. All dashboard, catalog and
shared-link entries use the same selection and private, no-store HTML. Frontend selection grants
no API permissions. Legacy JavaScript is frozen under its own revision-qualified asset URLs.
PostHog is not involved. Environment changes require a process restart/rolling deployment;
rollback needs no frontend rebuild. Existing tabs switch on reload, and configuration changes
also change the app-version stamp so open tabs can offer a refresh.

The local dev script enables 100% for signed-in accounts by default. Override its rollout variables
to rehearse production settings.

## Retire the deprecated Dashboard

Legacy is temporary, not a permanently supported version. Remove it in a follow-up change once
the new Dashboard is validated at full account rollout and the release no longer needs the frozen
fallback. Setting the percentage to 100 is not retirement: anonymous and token-only visitors still
use legacy under the current policy.

- Route every Dashboard entry to the compiled app, including anonymous catalog, shared links,
  token-only entries and sign-in. Verify those flows and authenticated account flows in the browser.
- Remove `src/treg/web/dashboard-legacy/`, `/app/legacy/assets/{path:path}`, the account-selection
  branch, all three `TREG_DASHBOARD_ROLLOUT_*` settings and their app-version stamp inputs.
- Remove obsolete rollout tests, local defaults and packaging checks; retain coverage for the
  surviving entry routes, sessions and compiled assets. Update build/deployment documentation and
  remove the retired settings from the private deployment configuration in a paired change.
- Remove the deprecation instructions from `AGENTS.md` and context docs once removal ships.
  Deployment rollback remains the recovery path after the in-process fallback is removed.
