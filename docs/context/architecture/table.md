---
title: The table layer - one call, answered as rows and columns (`/table/`)
status: shipped
sources:
  - src/treg/domain/table/__init__.py
  - src/treg/application/table.py
  - src/treg/routers/table.py
  - src/treg/routers/call.py
  - src/treg/call_surface.py
  - src/treg/config.py
  - src/treg/domain/identity/mcp_oauth.py
  - src/treg/application/auth.py
  - src/treg/routers/auth.py
  - tests/test_table.py
  - tests/test_table_oauth.py
related:
  - architecture/proxy-model.md
  - architecture/hub.md
  - architecture/catalog.md
  - interface/api.md
---

# The table layer

`* /table/{rest:path}` takes exactly the request `/call/<tool id>` takes (method, query, body, key,
`Idempotency-Key`) and answers the same call as `{shape, columns, rows, ...}`, for clients that want
a grid: the Google Sheets add-on first. It is a **sibling** of `/call/`, not an option on it:
`/call/` passes every query parameter through to the provider (a `?format=` would reach it), and it
must return the provider's answer unchanged (AGENTS.md, non-negotiable 4).

## One call road, two endings

`routers.call.run_call_surface` is the whole call road for every call surface; `call_tool` and
`table_tool` differ only in the `finish` they pass. `finish` runs inside the same `try` as the call,
so a fault while converting is audited and its idempotency claim released like any other.

`table_tool` passes `headers=application.table.plain_headers(...)`: the caller's headers with
`Accept-Encoding: identity`, because the table must parse the body. A provider that answers gzip or deflate anyway is decoded (`_decoded`); any other encoding
reads as a `raw` table.

`/table/` is in `call_surface._CALL_SURFACES` (surface label `table`), so its refusals get
`X-Treg-Error: 1` and the audit fallback, and in `bootstrap._DATAPLANE_ROUTE_KEYS`. The demo lockdown
(`domain.identity.access`) allows only `/call/` for non-read methods, so `/table/` stays out of demo
orgs.

## Money

Nothing here holds, charges or refunds. `execute_call` returns with the call already settled (its
cost is on the answer as `X-Treg-Cost-Micro`), so a conversion that fails can only make the view
`raw`: it can never charge twice or leave a hold open. The table answer carries the call's own
`X-Treg-*` headers (call id, cost, served-by, `X-Treg-Idempotent-Replay`), and the same
`Idempotency-Key` returns the same table for nothing. An answer over `MAX_TABLE_BYTES` is `raw` and
`truncated: true`.

An upstream non-2xx keeps its status and answers `{error: "upstream_error", upstream_status,
body_excerpt}`. A treg refusal (`CallFailure`) is raised exactly as on `/call/`.

## The flag

`application.table.enabled_for(slug, email)`: `table_enabled`, then `table_teams` / `table_users`
(either list lets a caller in; both empty means every team); off by default. A caller
outside answers a plain 404 **returned, not raised**: a raised 404 on a call surface is stamped and
audited as a refusal, and with the flag off the route must leave no row. `llms.txt` and `skill.md`
do not mention `/table/` while the flag is off.

## "Sign in with treg" (the add-on's OAuth client)

The add-on is a public client with PKCE: `client_id` `mcp_oauth.SHEETS_CLIENT_ID` (`treg-sheets`),
scope `treg:table`, resource `mcp_oauth.sheets_resource_url()` (`<public_url>/table`). It is not a
table row: `application.auth._sheets_client` builds it from `sheets_redirect_uris` (exact match, one
per Apps Script project), and it exists only while `table_enabled` is on and a URI is set.
`_client_resource` gives it that resource and nothing else, and refuses that resource to every other
client, so an MCP token never works here and this token never works on MCP.

- **The token works only on the table routes.** `routers.table.table_caller` accepts `Authorization:
  Bearer` on `/table/*`, `/table-columns/*` and `/table-account`, checks the audience, and presents
  the person to `require_member` as a short-lived identity, the way MCP exchanges its own token
  (`mcp._internal_auth`). Every other route ignores a Bearer header, so the token gets 401 there. The
  catalog search and endpoint pages need no key. The `Authorization` header is removed before the
  call (`plain_headers(drop_authorization=True)`): it is treg's token, not the provider's.
- **The grant is the person's, not one team's.** The consent page (`routers.auth._sheets_consent_page`)
  has no team picker; the token carries the person's first team as a default, and each request picks
  a team with `X-Treg-Org`, checked by `require_member` (membership, role, suspension) every time. A
  refresh whose default team the person left moves the default to another of their teams; only
  leaving every team ends the grant. `GET /table-account` lists every team the OAuth sign-in may use
  (respecting SSO scope and required SSO), or the key's one team with a team key.

Lifetimes and refresh rotation are the MCP ones (`mcp_oauth.ACCESS_TTL_SECONDS`, `REFRESH_TTL_S`). Sign-out is `POST /oauth/revoke`.

## The free column preview (`GET /table-columns/<tool id>`)

A client shows a tool's columns before anyone pays (the add-on's tool card and fill mode). The same
key, the same flag and team lists as `/table/`, but no provider call, no money and no audit row, so
it is **not** a call surface. A separate path, not `/table/...?preview=1`: `/table/` passes every
query parameter to the provider. `application.table.preview` answers `{shape, columns,
column_source, tables?}` with no rows (`domain.table.columns_only`):

- a routed job: its contract. A list job converts a sample of items, taken from the first child in
  `routed_children` that has a saved example (`domain.table.sample_items`), so the preview shows the
  mapped columns first, then that provider's own fields. It also answers `coverage` (`_coverage`):
  per output field, how many children's adapters map it. The cheapest provider answers first, so a
  field only some providers give can come back empty; the add-on ticks only fields every provider
  fills;
- a hub tool: its manifest's output fields;
- any other catalog endpoint: its saved example answer (`_example_body`, the file name from the loaded
  catalog row, never from the request) through the same converter;
- none of these: 404 `{error: "no_preview"}`.

## The converter (`domain.table`)

Pure and stdlib-only (import-linter contract "Table domain is a pure stdlib leaf"). `to_table` never
raises: anything it cannot shape is `raw`. `application.table.table_answer` chooses the source of
columns (`column_source`):

- **contract**: a routed job. Columns follow the capability contract's `output` order, then
  `served_by`; a miss has no row. A contract with a required `list` field is one row per item; the
  items are the contract's row shape (people.search `row`), or the provider's own objects where an
  adapter has no row map, so `LIST_MAPS` (data) maps common names to fixed columns first and every
  other field follows under its own name. The free preview runs a child's saved example rows
  through the same row map, so its columns match a live answer.
- **hub**: an id not in the catalog that `hub.tool_for` resolves for this caller, read after the
  answer is read (non-negotiable 3). Columns are the manifest's output fields, one row.
- **generated**: anything else. `_find_tables` collects every list of objects (looking inside
  `ENVELOPE_KEYS` wrappers): none is `flat`, one is `list`, two or more is `nested` with `tables`
  and a `summary` that also fills `columns`/`rows`, so a client that reads only those still gets a
  grid.

A field whose name starts with `_` (`_treg`, a provider's `_note`) is never a column; a routed
answer's `_treg` goes into the reply's `_treg` with `call_id` and `cost_micro`.

**Trap:** the saved example answers (`src/treg/catalog/examples/`) end every long list with the
string `"… N more item(s) truncated"` (written by `scripts/catalog_verify.py`). A list with that
marker is not "all objects", so the converter skips it (`_TRUNCATION_MARK`); real answers never
carry it.
