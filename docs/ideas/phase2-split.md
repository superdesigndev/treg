# Phase 2 presentation split design

Status: Checkpoint 0 draft. This file is a review artifact only and must not be committed.

Source of truth read for this design:

- `docs/REFACTOR-PLAN.md` Stage 2
- `docs/REFACTOR-PLAN.md` section 1.1
- the current `src/treg/api.py`, `src/treg/bootstrap.py`, role manifests, and four surface snapshots
- the relevant `docs/context/` fragments for API, dashboard, SEO, landing, composition, Catalog, and super-admin

The intended change is definition movement only. Handler bodies, dependency order, response types,
error handling, route names, route paths, methods, OpenAPI visibility, and registration order stay
unchanged. `treg.api:app` and the three role manifests stay unchanged.

## 1. Route inventory

The positions below refer to the current `api.py` before any Phase 2 movement. Snapshot positions refer
to `tests/snapshots/routes.json`, where positions 0 to 2 are FastAPI's built-in OpenAPI and docs routes.
For stacked decorators, the order listed is the actual snapshot order, not top-to-bottom decorator text.

### 1.1 `routers/catalog.py`

This module owns the open endpoint-Catalog JSON API. The first five routes form one uninterrupted
registration block at snapshot positions 5 to 9 and `api.py` lines 461 to 656.

| Snapshot | Route | Handler | Current position and continuity |
|---:|---|---|---|
| 5 | `GET /catalog/platforms` | `catalog_platforms` | lines 461 to 464, first route in the Catalog JSON block |
| 6 | `GET /catalog/platforms/{slug}` | `catalog_platform` | lines 467 to 521, immediately follows |
| 7 | `GET /catalog/search` | `catalog_search` | lines 537 to 592, immediately follows after its helper |
| 8 | `GET /catalog/endpoints/{endpoint_id}` | `catalog_endpoint` | lines 595 to 644, immediately follows |
| 9 | `GET /catalog/examples/{endpoint_id}` | `catalog_example` | lines 647 to 656, closes the block |

Catalog helpers that move with these handlers:

- `_provider_display`
- `_platform_rows`
- `_observed_or_empty`

The HTML routes `GET /catalog` and `GET /catalog/{slug}` go to `routers/web.py`, not this module.
They are presentation pages even though they render Catalog data.

One path-shaped exception needs explicit review approval:

- `GET /catalog/endpoints/{endpoint_id}/access`, handler `catalog_endpoint_access`, currently lines
  10372 to 10424 and snapshot position 203, stays in `api.py` for Stage 4. Despite its URL and GET
  method, its body executes the call credential ladder through `_resolve_call`,
  `_marketplace_secret`, `_platform_offer`, `_platform_estimate_micro`, and
  `_enforce_catalog_status`. Moving it without importing `treg.api` would require either moving call
  business helpers early or adding mutable callback wiring. Both expand this pure presentation move
  into the call boundary. The proposed classification is therefore "call access preflight", not the
  open Catalog API. If Stage 2 is intended to include this route solely because of its prefix, stop
  after this design review and revise the plan before code changes.

Also not part of the endpoint-Catalog API:

- `GET /providers.json` stays in `api.py`. It is the credential/provider registry consumed by
  `treg upload`, not the endpoint Catalog described by `catalog_store`.
- `POST /tool-requests` stays in `api.py`. It is a mutation and a later Catalog onboarding use case.

### 1.2 `routers/web.py`

This module owns presentation-only pages, SEO documents, bundled static responses that are registered
as routes, and the public skill documents. It uses three registration blocks because the current route
table interleaves web with auth and OAuth concerns.

#### Web block A: Catalog and SEO pages

This is one uninterrupted registration block at snapshot positions 10 to 18 and current lines 899 to
1923. It immediately follows the Catalog JSON block and immediately precedes `POST /tool-requests`.

| Snapshot | Route | Handler | Current position |
|---:|---|---|---|
| 10 | `GET /catalog` | `catalog_index` | lines 899 to 952 |
| 11 | `GET /catalog/{slug}` | `catalog_page` | lines 955 to 1021 |
| 12 | `GET /agents/{agent}` | `agent_page` | lines 1114 to 1320 |
| 13 | `GET /agents/{agent}.md` | `agent_page` | same stacked handler |
| 14 | `GET /use-cases/{category}/{job}` | `use_case_job_page` | lines 1383 to 1768 |
| 15 | `GET /use-cases/{category}/{job}.md` | `use_case_job_page` | same stacked handler |
| 16 | `GET /use-cases` | `use_cases_hub` | lines 1771 to 1812 |
| 17 | `GET /catalog.css` | `catalog_css` | lines 1815 to 1821 |
| 18 | `GET /docs` | `docs_page` | lines 1858 to 1923 |

The load-bearing ordering is preserved inside this block: all JSON Catalog routes at positions 5 to 9
remain before `GET /catalog/{slug}` at position 11. `_CATALOG_RESERVED` remains as the second guard.

#### Web block B: main site and bundled documents

This is one uninterrupted registration block at snapshot positions 35 to 62 and current lines 2791 to
3237. It remains between the auth invite confirmation route and `POST /oauth/register`.

| Snapshot | Route | Handler |
|---:|---|---|
| 35 | `GET /` | `landing` |
| 36 | `GET /app` | `dashboard` |
| 37 | `GET /app/marketplace/{service}` | `dashboard_marketplace` |
| 38 | `GET /app/skills/{name}` | `dashboard_skill_page` |
| 39 | `GET /app/tools/{name}` | `dashboard_tool_page` |
| 40 | `GET /llms.txt` | `llms_txt` |
| 41 | `GET /robots.txt` | `robots_txt` |
| 42 | `GET /sitemap.xml` | `sitemap_xml` |
| 43 | `GET /install.sh` | `install_sh` |
| 44 | `GET /selfhost.sh` | `selfhost_sh` |
| 45 | `GET /quickstart.md` | `quickstart_md` |
| 46 | `GET /tutorial.md` | `tutorial_md` |
| 47 | `GET /tutorial-import-shell.md` | `tutorial_import_shell_md` |
| 48 | `GET /tutorial-access.md` | `tutorial_access_md` |
| 49 | `GET /vendor-listing.md` | `vendor_listing_md` |
| 50 | `GET /vendor-listing` | `vendor_listing_md` |
| 51 | `GET /integrate.md` | `integrate_md` |
| 52 | `GET /skill.md` | `skill_md` |
| 53 | `GET /favicon.ico` | `favicon` |
| 54 | `GET /favicon.svg` | `favicon` |
| 55 | `GET /tutorial.js` | `tutorial_js` |
| 56 | `GET /legal.css` | `legal_css` |
| 57 | `GET /terms` | `terms_page` |
| 58 | `GET /privacy` | `privacy_page` |
| 59 | `GET /adtrack.js` | `adtrack_js` |
| 60 | `GET /resources` | `resources_page` |
| 61 | `GET /usecase.css` | `usecase_css` |
| 62 | `GET /use-cases/{slug}` | `use_case_page` |

The stacked decorator order for `vendor_listing_md` and `favicon` is copied exactly so the route
snapshot order does not flip.

#### Web block C: public skill documents and pages

OAuth discovery and the OpenAI challenge remain in `api.py`. The following block stays immediately
after `GET /.well-known/openai-apps-challenge` and before `GET /auth/cli-token`, at snapshot positions
72 to 79 and current lines 3988 to 4055.

| Snapshot | Route | Handler |
|---:|---|---|
| 72 | `GET /.well-known/skills/index.json` | `well_known_skills_index` |
| 73 | `GET /.well-known/skills/treg/SKILL.md` | `well_known_skill_md` |
| 74 | `GET /connect-demo` | `connect_demo_page` |
| 75 | `GET /connect-demo/callback` | `connect_demo_callback` |
| 76 | `GET /help` | `support_page` |
| 77 | `GET /contact` | `support_page` |
| 78 | `GET /support` | `support_page` |
| 79 | `GET /tutorial` | `tutorial_page` |

The `support_page` stacked decorator order is copied exactly.

#### Presentation-adjacent routes intentionally left out

- `GET /meta` stays in `api.py`. It is a runtime capability/configuration API for the dashboard,
  not a page renderer.
- `GET /login`, both `/auth/invite-signin` methods, GitHub and Google login pages, and both
  `/oauth/authorize` methods stay with auth for Stage 3.
- `GET /.well-known/oauth-protected-resource` and
  `GET /.well-known/oauth-protected-resource/mcp`,
  `GET /.well-known/oauth-authorization-server`, and
  `GET /.well-known/openai-apps-challenge` stay with OAuth/MCP protocol handling.
- `GET /demo/sandbox/live`, `GET /landing/stripe-feed`, `GET /demo/sandbox/skill`,
  `GET /skills/samples`, and `GET /skills/{name}/install.sh` stay with onboarding/demo. They expose
  data or generated onboarding artifacts, not standalone page rendering.

### 1.3 `routers/admin.py`

The admin module has two include points because five mutation routes remain between the read blocks.

#### Admin block A: cross-tenant reads

This is one uninterrupted registration block at snapshot positions 186 to 193 and current lines 8939
to 9185.

| Snapshot | Route | Handler |
|---:|---|---|
| 186 | `GET /admin/stats` | `admin_stats` |
| 187 | `GET /admin/orgs` | `admin_orgs` |
| 188 | `GET /admin/orgs/{org_id}` | `admin_org_detail` |
| 189 | `GET /admin/users` | `admin_users` |
| 190 | `GET /admin/tools` | `admin_tools` |
| 191 | `GET /admin/calls` | `admin_calls` |
| 192 | `GET /admin/errors` | `admin_errors` |
| 193 | `GET /admin/health` | `admin_health` |

Important existing behavior: `admin_errors` is a GET route but not database read-only. It calls
`_purge_expired_error_evidence`, which updates expired evidence and commits on its own session. The
route and helper move together with no logic change. In this design, "read route" means the existing
GET admin surface, not a promise of zero writes.

#### Admin block B: reconciliation and referral report

This is one uninterrupted registration block at snapshot positions 199 to 202 and current lines 9280
to 9362. It remains after the five admin mutations.

| Snapshot | Route | Handler |
|---:|---|---|
| 199 | `GET /admin/reconcile/drift` | `admin_reconcile_drift` |
| 200 | `GET /admin/reconcile/spend` | `admin_reconcile_spend` |
| 201 | `GET /admin/reconcile/repeats` | `admin_reconcile_repeats` |
| 202 | `GET /admin/referrals` | `admin_referrals` |

The read-specific helpers and constants `_tally`, `_ERROR_EVIDENCE_TTL_DAYS`,
`_ERROR_EVIDENCE_EXPIRED`, and `_purge_expired_error_evidence` move with the admin handlers.

### 1.4 Everything that stays in `api.py`

These concerns are explicitly out of Stage 2, even when an individual route uses GET:

- auth and identity: GitHub, Google, email OTP, CLI pairing/tokens, invite sign-in, treg OAuth
  authorization server, grants, and token revocation
- orgs and governance: org CRUD, invites, members, roles, agents, projects, pins, deny rules, settings,
  budgets, usage, and public tokens
- resources and connections: secrets, tools, bundles, skills, OAuth providers/connections, and health
- onboarding and demo: demo teams, sandbox, feed, sample skills, seed-tool, and teammate acceptance
- billing and referrals: balance, top-up, auto-top-up, history, portal, Stripe webhooks, member referral
  views, and referral code creation
- audit views: calls, call detail, and runs
- call and run: `/call/{rest:path}`, `/run`, call resolution, marketplace credential ladder, holds,
  relay, settlement, and server execution
- admin mutations: `admin_set_superadmin`, `admin_suspend_user`, `admin_delete_user`,
  `admin_suspend_org`, and `admin_delete_org`
- assembly: middleware, exception handlers, startup helpers, `router`, the compatibility `app`, and
  the final `create_app()` call

No path, method, handler name, or bootstrap ownership key in these areas changes.

## 2. Registration-order preservation

Using one `include_router(web.router)` at the end is not acceptable. It would group concerns by module
and reorder the current interleaving. It could also move `/catalog/{slug}` ahead of its JSON siblings.

Each target module therefore exports multiple ordered `APIRouter` blocks:

```text
routers.catalog
  public_router             snapshot 5..9

routers.web
  catalog_pages_router      snapshot 10..18
  site_router               snapshot 35..62
  public_docs_router        snapshot 72..79

routers.admin
  reads_router              snapshot 186..193
  reports_router            snapshot 199..202
```

`api.py` attaches each block exactly where the corresponding decorators currently execute. Existing
`api.py` routes remain between those attachment points. Attachment uses a tiny helper that appends the
block's existing `APIRoute` objects in order to `api.router.routes`. It does not call FastAPI's
`include_router()`, because the installed FastAPI version wraps included routes in `_IncludedRouter`;
Stage 1 already avoided that wrapper in bootstrap because it changes route inspection and therefore the
committed surface. Direct ordered attachment is composition only and preserves each route object's name,
endpoint, dependency graph, and decorator-produced order.

The order assertions are:

1. render all four snapshots in memory and compare their bytes to the committed files, without writing
   them;
2. assert the Catalog subsequence is exactly JSON routes, then `/catalog`, then `/catalog/{slug}`;
3. retain `tests/test_app_roles.py` unchanged, proving every route key is still classified and the three
   role manifests are unchanged.

No snapshot regeneration command is part of this stage.

## 3. Shared helper inventory and destinations

### 3.1 HTTP dependencies

`routers/admin.py` needs `require_superadmin`; the excluded Catalog access route is the only selected
Catalog-shaped route that would need `require_member`. Web pages need session identity lookup. Importing
these from `treg.api` is forbidden, so the first commit extracts the existing dependency family, with
unchanged bodies, into `src/treg/routers/dependencies.py`:

- `Caller`
- `_membership_by_token`
- `_user_from_session`
- `_user_from_identity_token`
- `_resolve_org`
- `_role_at_least`
- `_is_machine_email`
- `require_identity`
- `require_member`
- `require_superadmin`

`api.py` imports and re-exports these names so existing internal references and test imports continue to
work. This is not the Stage 3 auth use-case extraction. It only gives interface modules a cycle-free home
for the already-existing FastAPI dependencies.

### 3.2 Shared HTTP and time helpers

The following helpers are used by both moved web handlers and handlers that remain in `api.py`:

| Existing dependency | Destination | Reason |
|---|---|---|
| `_WEB_DIR`, `_LOGO_DIR`, `_MEDIA_DIR`, `_TOUR_DIR`, `_VENDOR_DIR` | `routers/web.py`, re-exported by `api.py` | presentation asset ownership moves with web; bootstrap and tests retain compatibility imports |
| `_esc_html` | `routers/web.py`, re-exported by `api.py` | rendering helper; remaining auth HTML handlers use the re-export |
| `_is_https` | `routers/dependencies.py`, re-exported by `api.py` | shared cookie security decision used by auth and web |
| `_user_from_session` | `routers/dependencies.py`, re-exported by `api.py` | shared auth dependency used by web and auth routes |
| `_local_owner` | `routers/web.py` | used only by dashboard rendering; `_bootstrap_single_user` remains in `api.py` |
| OAuth-return cookie read/write helpers and referral cookie helpers used by landing/dashboard | `routers/dependencies.py`, re-exported by `api.py` | avoids web importing auth implementation and keeps paired cookie validation in one place |
| `_utcnow_naive`, `_as_naive` | a small neutral `src/treg/timeutil.py`, re-exported by `api.py` | admin reads and many remaining handlers share the exact timestamp convention |
| `get_session`, `session_maker` | remain in `db.py`, imported directly | already cycle-free infrastructure helpers |

The extraction commit must preserve compatibility for the currently exercised names
`api._WEB_DIR`, `api._usd_short`, `api._platform_rows`, `api.use_case_job_page`,
`api._utcnow_naive`, and `api.Caller` by importing aliases into `api.py`. Re-exporting does not put
route definitions back in `api.py`.

### 3.3 Catalog and rendering helpers

- `_provider_display`, `_platform_rows`, and `_observed_or_empty` live in `routers/catalog.py`.
- `routers/web.py` imports those helpers from its sibling `routers.catalog`, never from `treg.api`.
- All helpers from `_CATALOG_RESERVED` through the server-rendered Catalog, agent, use-case, sitemap,
  and docs rendering blocks move to `routers/web.py` with their handlers. This includes `_page`,
  `_spa_catalog_page`, `_usd_short`, `_price_label`, `_catalog_mtime`, `_USE_CASES`, and
  `_SITEMAP_PAGES`.
- `api.py` re-exports only names with current compatibility usage. It does not duplicate bodies.

### 3.4 Admin helpers

`routers/admin.py` imports models, SQLAlchemy expressions, `get_session`, `session_maker`, settings,
`reconcile`, and the extracted `require_superadmin` directly. The admin handler bodies and the error
retention helper move unchanged. Mutation-only helpers such as `_is_last_active_superadmin` and
`_cascade_delete_org` stay in `api.py`.

## 4. Circular-import avoidance

The invariant is stronger than "no module-level import": no module anywhere under `treg.routers` may
import `treg.api`, directly or indirectly through an adapter that merely hides the edge.

The import direction after this stage is:

```text
api.py
  -> routers.dependencies
  -> routers.catalog
  -> routers.web -> routers.catalog
  -> routers.admin -> routers.dependencies
  -> bootstrap.create_app() at EOF

bootstrap.py
  -> api.py only inside create_app(), as it does today
```

Router modules import models, config, DB helpers, catalog storage, reconcile, session, and other
existing lower-level modules directly. `api.py` remains the compatibility exporter and the ordered
route-table host. Router modules never reach back into it.

This also explains why `catalog_endpoint_access` is proposed to stay put: its current dependencies are
defined by the call section of `api.py`, and moving the route before the call application boundary exists
would violate this import direction or force call logic into a presentation-only PR.

## 5. Import Linter

The final commit can activate the contract because the proposed dependency extraction removes all router
to API edges:

```toml
[[tool.importlinter.contracts]]
name = "Routers do not depend on the legacy API module"
type = "forbidden"
source_modules = ["treg.routers"]
forbidden_modules = ["treg.api"]
```

The exact `as_packages` setting will be verified against import-linter 2.13 before editing the config so
the contract covers every current and future module under `treg.routers`. The contract must catch direct
and indirect edges. No ignore is planned. `unmatched_ignore_imports_alerting = "error"` remains enabled.

This stage does not add the future routers to application to domain layering contract because the
application and domain packages have not yet been migrated.

## 6. Commit plan and gates

The suggested four commits remain the cleanest risk split:

1. `refactor(routers): extract shared HTTP dependencies`
   - add the package skeleton and move only shared helpers/dependencies;
   - keep every route definition in `api.py`;
   - preserve compatibility re-exports;
   - update context fragments and generated context map.
2. `refactor(routers): move catalog routes`
   - move the open Catalog JSON block and its helpers;
   - keep its attachment immediately before web block A;
   - update API/Catalog fragments.
3. `refactor(routers): move web presentation routes`
   - move the three web blocks and their rendering helpers;
   - attach them at the three original registration points;
   - update API/dashboard/SEO/landing/composition fragments.
   - Stage 3/4 follow-up: change `docs_page` to use `request.app`, then delete the transitional
     `web_routes.app = app` binding at the end of `api.py`.
4. `refactor(routers): move admin read routes`
   - move admin blocks A and B around the unchanged mutation block;
   - add the routers-to-api import-linter contract;
   - update super-admin/import-boundaries/API fragments.

After each commit:

- run the four snapshot assertions without regenerating files;
- verify `git diff -- tests/snapshots/` is empty;
- run `uv run --frozen pytest -q`;
- run `uv run --frozen lint-imports` once router modules exist;
- run `bash .agents/skills/tools-registry-context/scripts/drift.sh`;
- update the applicable `docs/context/` fragments and regenerate their map in the same commit;
- stop for review before starting the next commit.

Postgres is not added as a per-commit gate in this design because the requested Stage 2 gate specifies
the full SQLite suite and all moved behavior is route definition movement. Any existing CI Postgres
subset still runs normally on the eventual PR.

## 7. Review decisions required before code

1. Confirm that `catalog_endpoint_access` stays with the call flow until Stage 4, rather than being
   classified by URL prefix as Stage 2 Catalog API.
2. Confirm that `admin_errors` moves with admin GET routes despite its existing retention-update side
   effect. No behavior change is proposed.
3. Confirm the narrow classifications that leave `/meta`, `/providers.json`, OAuth discovery/challenge,
   and onboarding-generated skill responses in `api.py` for their later concern moves.
