---
title: Application composition and deployment roles
status: shipped
sources:
  - src/treg/bootstrap.py
  - src/treg/bootstrap_handlers.py
  - src/treg/bootstrap_http.py
  - src/treg/worker.py
  - src/treg/infra/money_trace_runner.py
  - src/treg/infra/loop_stall.py
  - src/treg/infra/money_admission_reporting.py
  - src/treg/call_surface.py
  - src/treg/application/connect.py
  - src/treg/domain/identity/mcp_oauth.py
  - src/treg/domain/identity/session.py
  - src/treg/routers/admin.py
  - src/treg/routers/auth.py
  - src/treg/routers/billing.py
  - src/treg/routers/call.py
  - src/treg/routers/connections.py
  - src/treg/routers/onboard.py
  - src/treg/routers/orgs.py
  - src/treg/routers/resources.py
  - src/treg/routers/referrals.py
  - src/treg/routers/web.py
  - scripts/dump_surface.py
  - tests/test_app_roles.py
related:
  - architecture/import-boundaries.md
  - interface/api.md
  - architecture/mcp-oauth.md
  - ops/deploy.md
---

# Application composition

`bootstrap.create_app(role)` is the composition root; `api.py` calls it once so the deployed
`treg.api:app` import path stays the `all` role. Installed [policy extensions](policy-extensions.md)
load last, so an extension's `install` sees the finished app.

Registration order (middleware, exception handlers, mounts, routes) is part of the compatibility
surface: `scripts/dump_surface.py` snapshots it, and a change to the snapshot for `role="all"` must be
intentional.

## Roles

`app.state.role_manifest` lists each role's routes, background tasks and startup checks;
`tests/test_app_roles.py` pins them. `dataplane` serves the calling surfaces (`/call/`,
`/catalog/call/`, [`/table/`](table.md)) and MCP; `control` serves everything else, including OAuth
issuance; `all` serves both. Every `api.router` route belongs to exactly one of
`_CONTROL_ROUTE_KEYS` or `_DATAPLANE_ROUTE_KEYS`, and app creation fails on an unclassified, stale,
duplicate or doubly owned key, so a new route cannot silently widen the dataplane.

MCP is calling traffic and stays on the dataplane; it only validates tokens. Token issuance (consent
pages, `/oauth/*`) stays on control. `domain.identity.session` is therefore used by both roles.

No role lifespan writes schema, backfills data or provisions the local single user. Backfills run in
`python -m treg upgrade`; `python -m treg` adds single-user provisioning before Uvicorn starts. An
operator serving the raw ASGI app must run the upgrade command on every release.

## Lifespan

- Shutdown cancels and awaits every started background worker before draining Arena, audit and
  analytics or closing the shared HTTP client, so database rollback and close finish before
  event-loop teardown.
- The catalog's endpoint-observation reader is one process-local `CachedEndpointObservationReader`
  per app, shared by the HTTP catalog routes and both MCP surfaces, reading through
  `background_session_maker`. It never awaits its source on the request path (a miss returns empty and
  schedules a refresh), so `callrecord` aggregates stay off the API pool.
- When the archive uses R2, object-store configuration is validated before DB verification and does
  no object I/O at startup (see [archive](archive.md)). Tests inject a store through
  `create_app(..., archive_object_store=...)`.
- `infra.money_trace_runner` runs on every role regardless of analytics settings; it stops after
  money-producing background work drains. Its records are defined in
  [data-model](data-model.md#product-analytics-writer-analyticspy).
- `infra.loop_stall` runs in every web worker's lifespan. The loop only stamps a heartbeat and,
  when a beat comes `STALL_SECONDS` after the last, queues that gap: every such gap is one
  `loop_stall_end`, measured on the loop's own clock (the stall plus at most one heartbeat), even
  when nothing saw it in progress. A daemon thread writes `loop_stall` with the loop thread's stack
  once the stamp is `STALL_SECONDS` old. Lines go straight to the stderr descriptor, not through
  `logging`, so a full pipe blocks only that thread and holds no lock that request logs or
  interpreter shutdown need. It does not use the process-wide faulthandler timer. A watcher that
  woke late (C code held the GIL) marks its stack `stack_reliable: false`. It never raises into
  startup or shutdown.

## HTTP middleware

Every app-wide middleware in `bootstrap_http.py` is pure ASGI. Do not reintroduce
`BaseHTTPMiddleware.call_next()`: an MCP client may disconnect while its stateless transport ends
without a response, which must stay a normal end of a dead connection rather than a 500. Uvicorn
still sends a 500 when an app returns without starting a response on a live connection, so pure ASGI
does not hide a genuine missing-response bug. `_DevTitleMiddleware` is registered only when
`Settings.local_dev` is true; the surface snapshot omits it.

`call_surface.split_call_path` lets the pool-saturation and HTTP-exception handlers in
`bootstrap_handlers.py` treat `/call/`, `/catalog/call/` and `/table/` alike (call id, audit,
idempotency release). A pool timeout before caller identity is resolved emits `call_intake_failed`
rather than a `tool_called` failure, so it does not change the admitted-call population.

## MCP V2 path

`TREG_CLAUDE_CONNECTOR_ENABLED=true` mounts `/mcp/v2`, registered before `/mcp` so the parent mount
cannot consume it. Claude can strip the final slash from a custom-connector URL, so
`NormalizeDirectoryMCPPath` rewrites exactly `/mcp/v2` to `/mcp/v2/` before route matching; both
spellings must reach the V2 transport and OAuth audience.

## Route cloning

Each `create_app` call must produce an independent app whose dependency overrides belong to it.
`_include_routes` shallow-clones every `APIRoute`, points its override provider at the new app and
rebuilds its handler. FastAPI's `include_router()` is avoided because its `_IncludedRouter` wrapper
would change route inspection and the surface snapshot.
