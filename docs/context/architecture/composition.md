---
title: Application composition and deployment roles
status: shipped
sources:
  - src/treg/bootstrap.py
  - src/treg/bootstrap_handlers.py
  - src/treg/bootstrap_http.py
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

`bootstrap.create_app(role)` is the FastAPI composition root. `api.py` hosts the ordered route table,
attaches concern routers at compatibility-sensitive registration points, and calls the factory once at
EOF so the deployed `treg.api:app` import path remains the default `all` role.

The factory owns concrete assembly: the three core pure-ASGI middleware registrations, the optional
V2 path normalizer, five exception handlers, static mounts, optional MCP mounts and lifespans,
GET-to-HEAD widening, the OpenAPI wrapper that hides
implied HEAD operations, shared HTTP client creation, startup work, shutdown drains, and the Ads
conversion worker. Registration order is compatibility behavior. The four stage-0 snapshots stay
byte-identical for `role="all"` unless that composition intentionally changes.

For every role, the factory wires the Catalog observation port to one process-local
`CachedEndpointObservationReader` backed by short `session_maker` reads. `all` and `dataplane`
lifespans inject that exact instance into both mounted MCP catalog surfaces; the HTTP catalog routes
and the observed-stats prose pages (use-case and workflow) on `all` and `control` read the instance
from app state. This keeps one cache and one refresh Task per
process even when HTTP and MCP search concurrently. The refresh Task starts lazily on a miss rather
than appearing in the role's always-running background-task manifest. The lifespan still owns it:
shutdown first unbinds it from MCP, then calls `aclose()`, which refuses new refreshes and cancels the
shared Task before database and HTTP resources disappear.

`bootstrap_handlers.py` owns the app-wide pool-saturation and HTTP-exception adapters. The composition
root supplies the call-specific `_stamp_call_exit` callback from `routers/call.py` before registration;
the callback owns call ids, refusal classification, audit fallback, and idempotency-label release.

`bootstrap_http.py` owns the app-wide middleware implementations. The middleware stack is
`_BodyDecodeMiddleware` -> `_SecurityHeadersMiddleware` ->
`_LegacyHostRedirectMiddleware` -> routes/mounts. All three are pure ASGI. The security wrapper adds
headers at `http.response.start` with case-insensitive setdefault semantics, and the redirect wrapper
either sends the same 301/302 response as before or calls its child directly. Keeping
`BaseHTTPMiddleware.call_next()` out of this stack matters for streaming and disconnects: an MCP
client may close while its stateless transport terminates without sending a response, which is a
normal end to an already-dead connection rather than a server 500.

Pure ASGI does not make a genuine missing-response defect silent. Uvicorn's
`RequestResponseCycle.run_asgi` checks an app that returns while the connection is still live, logs
`ASGI callable returned without starting response.`, and sends a 500. It skips that error only when
the protocol has already marked the client disconnected, when no response can be delivered. Response
completion also remains responsible for Starlette background tasks: the `/call` relay's
`StreamingResponse` runs `BackgroundTask(upstream_resp.aclose)` after its body, and an assertion test
pins that the shared httpx connection is released exactly once. Removing the two AnyIO memory-stream
hops changes streaming backpressure and scheduling but not interruption semantics, which the
callmatrix stream-failure case pins.

## Role manifests

Every created app exposes `app.state.role_manifest` with explicit `routes`, `background_tasks`, and
`startup_checks` lists. `tests/test_app_roles.py` pins all three lists for every role, while the call
architecture test separately pins the dataplane/control startup split and background-task ownership.

| Role | HTTP routes and mounts | Background tasks | Startup checks |
|---|---|---|---|
| `all` | The complete surface, including `/run`, static files, `/mcp`, and the flagged `/mcp/v2` | Ads conversion worker when enabled | Read-only DB verify, HTTP client, enabled MCP lifespans |
| `dataplane` | `/call/{rest:path}`, `/catalog/call/{rest:path}`, MCP mounts, and their resource metadata; no `/run`, static files, docs, or OpenAPI | None | Read-only DB verify, HTTP client, enabled MCP lifespans |
| `control` | Everything except the calling surfaces; includes OAuth issuance, `/run`, and static files | Ads conversion worker when enabled | Read-only DB verify, HTTP client |

No role lifespan writes schema, performs a data backfill, or provisions the local single user. The explicit
`python -m treg upgrade` release phase owns content-driven backfills; the default `python -m treg`
serve path adds single-user provisioning before Uvicorn starts. Raw ASGI operators must run the
upgrade command separately on every release. `verify_db()` only checks revision compatibility and the
Fernet-key guard; the exact startup manifests are pinned to a read-only allowlist.

MCP is calling traffic (the refactor plan's role table assigns `mcp.py` to the dataplane), so a future
dataplane deployment serves agents on both entry points. OAuth token issuance - consent pages and the
`/oauth/*` endpoints - stays on control; the MCP surface only validates tokens, which is a read.
`domain.identity.session` is therefore a both-role primitive: control signs browser and identity
tokens, while both roles share its signing-key validation through `domain.identity.mcp_oauth`.

`_CONTROL_ROUTE_KEYS` and `_DATAPLANE_ROUTE_KEYS` assign every `api.router` route to exactly one
owner. App creation fails on an unclassified, stale, duplicate, or multiply-owned key, so adding a
route cannot silently expand the dataplane. Role separation is preparatory in stage 1; only the
`all` role is deployed.

`TREG_CLAUDE_CONNECTOR_ENABLED=true` adds `/mcp/v2` and starts its lifespan. When the flag is false
or missing, only the team `/mcp` mount starts. The nested V2 mount is registered first so the
parent `/mcp` mount cannot consume it.

When V2 is enabled, `NormalizeDirectoryMCPPath` rewrites the exact `/mcp/v2` path to `/mcp/v2/`
before route matching. Claude can remove the final slash from a custom-connector URL. Both spellings
must stay on the V2 transport and OAuth audience.

## Route cloning

Each factory call must produce an independent app whose dependency overrides belong to that app.
`_include_routes` therefore shallow-clones every `APIRoute`, points its dependency override provider
at the new FastAPI instance, and rebuilds its request handler. This also avoids the internal
`_IncludedRouter` wrapper added by the current FastAPI `include_router()` implementation, which would
otherwise change route inspection and the committed surface snapshot.

Public routes added since: `/{INDEXNOW_KEY}.txt` (`indexnow_key`, `routers/web.py`) — the IndexNow
key file; listed in the ownership table beside `/sitemap.xml`. See `interface/seo.md` § IndexNow.
