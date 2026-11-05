---
title: MCP - the front door for assistants, and treg as an OAuth authorization server
status: shipped
sources:
  - src/treg/application/auth.py
  - src/treg/mcp.py
  - src/treg/domain/identity/health.py
  - src/treg/domain/identity/mcp_oauth.py
  - src/treg/domain/identity/session.py
  - src/treg/domain/identity/access.py
  - src/treg/domain/identity/api_keys.py
  - src/treg/alembic/versions/0082_oauth_sso_provenance.py
  - src/treg/routers/api_keys.py
  - src/treg/routers/auth.py
  - src/treg/web/claude-connector.html
  - src/treg/web/connect-demo.html
  - tests/test_mcp.py
  - tests/test_mcp_oauth.py
  - tests/test_mcp_directory.py
  - tests/test_marketplace_call.py
related:
  - architecture/auth-secrets.md
  - architecture/proxy-model.md
  - architecture/money.md
  - interface/api.md
---

# MCP

The doors an assistant comes through (ChatGPT, Claude, Claude Code, Cursor, anything speaking the
Model Context Protocol). One deployment, database and enforcement layer serve both:

- `/mcp/` is the team MCP surface: catalog, team tools and imported skills.
- `/mcp/v2/` is the catalog-only Claude directory surface. It cannot list or call team-owned tools,
  including a team tool whose name matches a catalog endpoint.

## Why V2 exists

The team `/mcp/` must keep its general `call` tool for catalog endpoints, team tools and skills,
and treg cannot classify the side effects of an arbitrary private tool, so that surface cannot give
Claude a precise read-versus-write signal without breaking its contract. V2 accepts only curated
catalog ids and splits calls by the catalog's HTTP method, so Claude can treat GET, HEAD and OPTIONS
as reads and ask approval for POST, PUT, PATCH and DELETE. `/mcp/v2/` is the second transport
surface; **connector release V1** is the first product release submitted to Claude's directory.

`NormalizeDirectoryMCPPath` makes `/mcp/v2` and `/mcp/v2/` the same V2 resource: a real Claude
custom-connector flow strips the trailing slash, and without normalization the request falls through
to the team `/mcp` mount and receives the wrong OAuth resource identity.

`TREG_CLAUDE_CONNECTOR_ENABLED` (default false) controls the V2 mount, lifespan, resource metadata,
new V2 grants and the catalog-only call route (404 when off). `/mcp/` does not depend on it.

## MCP surface maintenance contract

The two surfaces have different contracts but must not have different implementations of shared
behavior.

| Area | Must stay shared | Intentional difference |
|---|---|---|
| catalog search | loading, ranking, price view, near misses, the process observation cache and refresh Task used by HTTP | next-call guidance and event source; V2 drops rows on its hidden platforms |
| endpoint details | the `/catalog/endpoints/{id}` route and error shape | client attribution; V2 refuses an endpoint on its hidden platforms and leaves out the row's `note` (`_SurfacePolicy.endpoint_note`) |
| calls | request assembly, credentials, policy, limits, idempotency, relay, errors, metering, audit | team MCP accepts team tools; V2 accepts catalog ids only, splits read/write methods, refuses its hidden platforms and shows no invitation |
| balance | team selection, grant labels, balance route, error shape | client attribution |
| catalog requests | `/tool-requests`, rate limit, field limits, caller IP | event source and client attribution |
| transport | host checks, compression, cache headers, eager auth, static capabilities | V2 has a separate audience, metadata path, scope marker and Claude browser origin |
| lifecycle | the transport factory and `mcp_lifespan` | V2 mount and lifespan depend on its feature flag |

`_SurfacePolicy` holds the values that differ inside shared tool behavior. Public tool registrations
stay separate because names, descriptions, schemas and safety annotations are public contracts.
`build_mcp_app` refuses a server and OAuth resource-version pair naming different surfaces.

When either surface or shared MCP code changes, review both; tests must compare the shared behavior
and prove the listed differences. Keep these V2 properties fixed unless a new directory review
approves a contract change:

- `/mcp/v2/` is the stable URL, and `/mcp/v2` resolves to the same resource.
- The tool list is the six tools the listing was submitted with: `catalog_search`, `catalog_get`,
  `catalog_call_read`, `catalog_call_write`, `balance`, `catalog_request`. Adding a tool changes the
  listing; do it with a resubmission, not in passing.
- V2 does not list, describe or call image, video, voice or music generation
  (`_DIRECTORY_HIDDEN_PLATFORMS`): the listing states the server does not use AI models to generate
  images, video or audio. The team `/mcp/` keeps them.
- V2 accepts catalog ids only, never team tools or passthrough paths.
- Read and write calls stay separate, and their annotations match their method classes.
- V1 and V2 OAuth audiences do not cross.
- The feature flag controls the V2 mount, metadata, new grants, call route and lifespan.
- V2 titles, descriptions, schemas, annotations and neutral review copy stay under test. Each tool
  repeats its exact display title in `annotations.title`; Anthropic's submission portal validates the
  annotation title independently of the top-level title.

`tests/test_mcp.py` protects team MCP behavior. `tests/test_mcp_directory.py` protects V2 and calls
both URLs with the same inputs to compare search, prices, details, balance, call results, errors,
hints and attribution. `tests/test_marketplace_call.py` proves the catalog-only route cannot be
shadowed by a same-named team tool. A change is incomplete if only one MCP test file is reviewed.

## Team MCP at `/mcp/`

`tools/list` is the registry of tools. Notes the code cannot say:

- **Deliberately not one tool per provider.** Thousands of endpoints as thousands of MCP tools would
  bury the client's tool list and force a reconnect whenever the catalog grew. `catalog_search` plus
  `call` covers all of it and stays the same size.
- The `hub_*` tools exist only on `/mcp/`; `_HubToolsGate` hides them unless the hub is enabled for
  the caller. `catalog_search` merges visible listed hub tools (`kind: "hub"`) and `call` accepts a hub
  tool id. `/mcp/v2/` never registers them. See [hub](hub.md).
- `call_media` and `resources_list` exist only on `/mcp/`.
- `feedback` and `review` exist only on `/mcp/`, so V2 never renders the invitation `/call/` decided
  on (`_SurfacePolicy.invites`); `/call/` still decides and records it ([feedback](feedback.md)).
- **OpenAI clients** (`openai-mcp/` for ChatGPT and its plugin reviewer, `codex-mcp-client/` for Codex,
  by User-Agent) are not offered `review`: `_OpenAIClientGate` drops it from `tools/list` and the
  instructions, and their call results carry no invitation, because that review reads
  agent-initiated rating as analytics collection.

**Annotations.** `call` is destructive, open-world and non-idempotent: treg relays to somebody else's
API and cannot know whether that endpoint charges, writes or deletes; claiming otherwise would be a
guess presented as fact. `feedback`, `review` and `catalog_request` are harmless writes on treg
itself, so non-destructive; on the team MCP they are open-world because their text leaves the
caller's account for the treg team (a review's reason may be quoted publicly), which is how the
Codex plugin review reads that hint.

**`catalog_request`** relays to `POST /tool-requests` so rate limits and field caps live in one place,
carrying the caller's address as `_bearer` resolved it (`client_ip.py`, never client-written
`X-Forwarded-For`); otherwise every MCP caller would share one rate-limit bucket.
`catalog_search` reads the catalog in-process, so it logs its own `SearchMiss` (`source="mcp"` or
`"claude-connector"`) and runs the [discovery experiment](find.md) with the caller's
`Context`; the anonymous HTTP route is not part of that experiment.

**Provider authorization remediation.** Provider OAuth stays a human browser action. A catalog-call
preflight can return a structured 428 body naming the missing grant, capability, scopes, CLI command
and dashboard action; `call`, `catalog_call_read` and `catalog_call_write` preserve it so the agent
can tell the human what to authorize and retry. No MCP response includes a credential. Structured
results preserve HTTP `Retry-After` as `retry_after` (including `call_media`), so clients can retry an
[org admission](org-flow-control.md) refusal without seeing headers.

## Fixed surface - no change subscriptions

The MCP surface is static at runtime: catalog refreshes change the data `catalog_search` and
`catalog_get` return, never `tools/list`, and treg publishes no change events. `server/discover`
therefore advertises `tools.listChanged`, `prompts.listChanged`, `resources.listChanged` and
`resources.subscribe` all false, and `subscriptions/listen` is refused with `METHOD_NOT_FOUND`.

MCP SDK 2.0 installs `subscriptions/listen` and derives all four flags from its presence, with no
public switch. `_StaticSurfaceCapabilities` corrects both through the SDK's public server-middleware
seam (rewriting the discover result and vetoing listen) and deliberately does not mutate the SDK's
private handler registry. This keeps clients from opening idle SSE streams.

## In-process, not over the network

Tools reach the rest of treg through `httpx.ASGITransport` against treg's own app: a real request
through the real routes without a socket. The enforcement rules (deny rules, pins, per-member tool
access, the credential ladder, metering) live in those routes; a path that read the database
directly would be a second implementation of each, drifting quietly. Those requests carry the
caller's credential (for an OAuth bearer, the identity it is exchanged for), so the call tools
refuse an `endpoint_id` with an empty, `.` or `..` segment at any depth of percent-encoding (a
trailing slash excepted): httpx collapses dot segments, so `../orgs/...` would otherwise reach a
management route. `_call_url` also checks the final path stays under the call route. The catalog is the one
exception, read straight from `catalog_store` for speed, not permission.

The in-process client stamps `X-Treg-Client: mcp` or `claude-connector` (attribution, never a gate)
and forwards `X-Treg-Mcp-Client` (`clientInfo.name`) and `X-Treg-Mcp-User-Agent` as `_bearer` read
them. `tool_called` and `call_intake_failed` record those only when `X-Treg-Client` names an MCP
surface, so a direct `/call/` cannot pose as an MCP host. The stateless transport keeps no
`initialize`, so `clientInfo` is known only from protocol revisions that carry it on every request;
older clients are told apart by User-Agent. The internal request keeps httpx's own User-Agent (what an
own-key upstream sees), and relay drops every `x-treg-*` header.

`X-Treg-Meta` ([money](money.md)) is read off the MCP transport and forwarded, deliberately not a
tool argument: a model asked to pass a customer id will omit it somewhere in a chain, and a billing
figure you cannot reconcile is worse than none. It is also in `call`'s extra-header filter, so a
model-supplied value cannot contradict the transport one. `authorization_method` is a tool argument
mapped to the internal `X-Treg-Authorization-Method` header, which caller headers cannot override and
which is not relayed.

## `call` speaks every request shape the CLI does

`params` keeps its method-based role (query on GET, JSON body on POST). Explicit slots cover what it
cannot express:

- `query` is always the query string. A team-tool list sends repeated keys (a dict would collapse
  them); a catalog array follows the endpoint's `input.queryArrayEncoding` (`json`, `comma`,
  `repeated`). Composable with `body` on a POST.
- `body` is always the body: object or array as JSON; a string sent raw with `content_type` (sniffed
  as JSON when it parses). A body implies POST.
- `headers` adds upstream headers; treg's own auth and routing headers are filtered, and injected
  credentials always win server-side.
- `form` scalar fields and base64 `uploads` (capped at 30 MiB before the internal request) build a
  multipart body without changing the JSON contract.
- An inline `?a=b` in a passthrough URL is pulled out and merged: httpx drops a URL's query string
  whenever `params=` is passed.
- `params` claiming the same position as an explicit slot is refused loudly, never merged.
- **Query values are spelled the way the wire spells them**: `true`/`false` (Python's `str(True)` is
  `"True"`, which upstreams documenting a boolean reject), nested values as compact JSON, `None`
  omitted. A mangled flag can silently move a caller from a free mode to a paid one.
- `call` resolves the team the way `balance` does (`_resolve_org`) before anything is spent, so a
  multi-team identity gets its pinned or active team, or an error naming the teams, instead of
  `/call/`'s header hint an MCP caller cannot act on.

**Undeclared arguments are refused.** The SDK ignores keys a tool does not declare, and for `call`,
`call_media`, `catalog_call_read` and `catalog_call_write` a dropped key is a wrong request (`data`
instead of `body` sent an empty POST). `_UnknownCallArguments`, a `tools/call` middleware on both
surfaces, refuses it before anything is sent and names the accepted arguments.

**`idempotency_key`** is the caller's: the same key on a repeated call replays the stored response
without reaching the provider or charging (`replayed: true`). Header values are ASCII, so a non-ASCII
key is percent-encoded (a digest when too long), always mapping one key to one header
(`_header_idempotency_key`). It is deliberately not derived server-side: two identical searches an
hour apart are new work, and a server-invented key would quietly serve stale data. Reuse for a
different request is refused. See `architecture/money.md`.

**Overflow disclosure.** An MCP client never sees `X-Treg-Served-Via`, so `_call_impl` lifts it into
`served_via` with a one-line `hint` (`cost_usd` is then the relay's real price), identically on both
surfaces. `catalog_get` carries `overflow_price_usd`, `overflow_price_unit` and `overflow_via` so the
price told before the call includes the relay's (`architecture/money.md` § Overflow money).

## Output schemas: every field optional and nullable

Each tool declares its output schema (ChatGPT's connector review asks for it).

- **Every field is optional** (`total=False`): the SDK validates a strict schema on the way out, so a
  required field turns the first `{"error": ...}` into an opaque tool failure.
- **Every field is nullable** (`| None`): the SDK serializes through the TypedDict-derived model,
  which fills every absent key as `null` in `structuredContent`, and a strict client validating
  against `type: string` refuses the whole answer with -32602. FastMCP's own client is lenient, so the
  suite validates real `structuredContent` with `jsonschema` playing the strict client.
- A field whose payloads vary in shape is `Any`, not a union (`call.body`;
  `catalog_get.example_response`, an array for list-shaped providers).

## Responses are gzip-compressed at the origin

A managed edge may Brotli-compress large responses, and at least one real client stack (httpx with
brotlicffi) dies mid-decode and hangs to its timeout. Managed edges can ignore `Cache-Control:
no-transform`; the header stays (`NoTransformResponses`, outermost so 401 challenges carry it), but
the working fix is `GZipMiddleware` inside `build_mcp_app`: an edge does not recompress a response
already carrying `Content-Encoding: gzip`. Post-deploy check whenever this path changes: a large
authenticated `catalog_get` over `/mcp/` with `Accept-Encoding: br, gzip` must return
`content-encoding: gzip`.

## Authentication: eager, every request

`RequireAuthForProtectedTools` answers any uncredentialed id-bearing JSON-RPC request (`initialize`,
`tools/list`, `tools/call`) with 401 and `WWW-Authenticate: Bearer scope="..." resource_metadata="..."`.
The header is the point: an error inside a 200 tells a program nothing, and a tool function can set
neither status nor header. It is eager because every treg tool needs auth: an open `initialize` let
clients show "Connected" and never prompt, while the MCP spec's flow challenges the first request so
OAuth runs before the session proceeds. Only notifications and `ping` pass without a credential;
`.well-known/*` discovery is separate GET routes. Any non-POST request is challenged the same way and
otherwise refused with 405 `Allow: POST`, since the stateless transport has no session for a GET
stream. Protected-resource metadata is also served at the trailing-slash paths RFC 9728 derives from
`/mcp/` and `/mcp/v2/`, since some clients do not follow redirects during discovery.

Two cases. **No credential**: the plain challenge. **A dead access token** (a bearer whose unverified
`typ` claims to be treg's access token but fails `read_access_token`): 401 with
`error="invalid_token"` (RFC 6750 §3.1), the code an OAuth client runs its refresh grant on. Access-token
validation is stateless (HMAC + expiry), so the transport can afford it. Anything needing the
database (a per-org or identity token) passes through for the tool to validate: its holder has no
refresh grant to run, and judging it here would be a second authentication implementation. The
transport's DNS-rebinding host check (421) and Origin check (403) sit behind this middleware, so auth
never masks them.

Both surfaces accept an active managed key as a bearer; tools pass it to the API, so disable and
revoke apply on the next tool call. Static tool schemas may be listed before a non-OAuth bearer is
validated; that grants no data or call access. A `scp=bootstrap` login token cannot call either
surface. MCP OAuth tokens are typed, short-lived bridge credentials with separate V1 and V2 audiences
and never resolve through an `ApiKey` row.

The middleware buffers the POST body only long enough to classify the request, then replays the
consumed messages and delegates later `receive()` calls to the original channel. It never
manufactures `http.disconnect`: a synthetic disconnect cancels live downstream work on any long
response. A genuine client disconnect mid-read is replayed unchanged.

# treg as an authorization server

Elsewhere treg speaks OAuth as a client; here it issues tokens. `routers.auth` owns HTTP translation
and consent rendering; `application.auth` sequences registration, authorization, code exchange,
refresh rotation and replay, revocation and grant-team changes, owning every session and commit;
`domain.identity.mcp_oauth` owns token and resource validation and grant-family primitives.
`domain.identity.health` aliases the root credential-network safety module so both import paths share
one module object and monkeypatch target.

The same authorization server serves one non-MCP client, "treg for Sheets" (`treg-sheets`), with its
own resource (`<public_url>/table`) and scope, a grant that belongs to the person rather than one
team, and a token accepted only on the table routes. See [table](table.md).

## The `aud` claim carries the weight

A client sends `resource=<the mcp url>` and the server copies it into the token's audience. Without
checking it, a token granted to another MCP server would work here and spend the user's balance on a
server they never consented to. `read_access_token` therefore takes `expected_audience` as a required
argument with no default, and a test asserts that omitting it raises. `/oauth/authorize` refuses a
`resource` treg does not serve up front: accepting one mints a valid but useless token whose failure
surfaces later as "not signed in".

V2 advertises `treg:directory` as a fallback resource marker: hosted Claude can keep the V2 challenge
scopes while omitting the RFC 8707 `resource` parameter, and `_effective_mcp_resource` then selects
the V2 audience. An explicit resource always wins; everything else defaults to V1.

Each version has its own audience set (`mcp_resource_audiences(version)`: `public_url` plus
`public_host_aliases`), so a grant survives a host change or rollback, and host and slash aliases
normalize within a version but never across: a V1 token fails on `/mcp/v2/` and vice versa. Refresh
reissues `row.resource`, so a grant keeps its consented audience; `normalize_resource()` heals slash
spellings at every store, mint and comparison.

## Two doors in, one row out

`OAuthClient` holds both DCR clients (RFC 7591, `POST /oauth/register`, Claude Code and most clients)
and CIMD clients (the `client_id` is an https URL treg fetches, which ChatGPT uses). Supporting one
and not the other locks out a family of clients, and the gap hides because the client you test with
does not need the other path.

The CIMD fetch is fenced because the URL is caller-chosen: https only, no redirects, a public address
at connect time, bounded time and size, reusing `health.safe_webhook_url` and `health.host_is_public`.
The document must claim its own URL, or a document hosted anywhere could assert someone else's
`client_id` and inherit their consent. `redirect_uris` match exactly, never by prefix
(`https://good.test/cb.evil` starts with the registered value).

## The consent screen is the security boundary

It is the only place a human sees what they grant, so it says it in words (*this spends the team's
balance*, *uses the keys your team registered, without seeing them*) and warns when a client
registered itself, because DCR is open to any name. It carries the team picker with each team's
balance: a person in several teams is asked, not guessed at, and showing the balance prevents picking
an empty team.

Approval is a same-origin POST (a GET that granted access could be triggered by any navigating page),
and the page inherits `X-Frame-Options: DENY`. `Origin: null` is accepted only when `Sec-Fetch-Site`
corroborates it: browsers report an opaque origin after some redirect chains, such as a sign-in
bounce.

When a team requires enterprise SSO, normal-login consent cannot create a grant for it. SSO consent
is scoped to its own team (Sheets requests that pick a team per call included);
`OAuthCode.sso_org_id` and `OAuthGrant.sso_org_id` carry that proof through exchange and refresh to
`require_member`. A grant created before enforcement has no proof and stops working on the enforced
team at use and refresh. An SSO-scoped grant cannot be moved to another team.

### …but the choice must stay visible and reversible afterwards

Decided-once must not become invisible and permanent: when the CLI and an OAuth client use different
identities, an agent cannot tell a plausible slug from the intended team.

- **`balance` and `my_tools` label the grant** with `team_name` (a slug alone cannot be sanity-checked)
  and `identity`, the account the grant belongs to. If that identity's own `/orgs` does not list the
  team, the answer says so. The how-to-move hint is added only for an OAuth caller; a header token
  carries its own team.
- **The team can be moved without re-consenting.** It lives on the family's `OAuthGrant` row
  (`current_org_id`), so `POST /oauth/grants/{family}/team` (`treg mcp use-team`) is a row update the
  next refresh picks up within one access-token lifetime. Only the grant's own user may move it, and
  only to a team they belong to: a grant never reaches further than consent would have offered. A
  refresh itself cannot change teams.

Lifecycle rules:

- **Family authority is separate from token provenance.** `OAuthGrant.current_org_id` is the one
  mutable answer that `_family_org`, listing and refresh read; `OAuthRefresh.org_id` never changes, so
  a retired-token replay is audited against the team that token named. `OAuthGrant.granted_at` is the
  consent time, so rotation never makes an old authorization look new. A refresh racing a move cannot
  revert it.
- **Live means non-retired and non-expired** (`_refresh_is_live`) for refresh, listing and moves.
- **A grant dies with the membership it was consented under**; otherwise it would keep minting tokens
  and spring back if the membership were restored.
- **A rolling deploy cannot strand a family without authority.** An old binary can still issue only
  `OAuthRefresh` rows, so `_ensure_grant` reconstructs a missing `OAuthGrant` from the oldest refresh
  token (concurrency-safe upsert, oldest consent time) before refresh, listing and moves.
- **Deleting any team in a family's history revokes the whole family.**
  `cascade_delete_org` (`domain/governance/teams.py`) collects families through both
  `current_org_id` and the immutable `OAuthRefresh.org_id`; otherwise deleting a former team erases
  the retired row that recognizes a replay while leaving a live token elsewhere.
- **"Not your team" and "no such team" answer identically** (404), so the route never reveals whether
  a slug exists.

## Codes and refresh

Authorization codes are single-use and deleted before validation, not flagged: holding one while
checking leaves a window where two redemptions both read it. Let the database arbitrate.

Refresh tokens rotate, and the retired row is kept so a replay is recognizable: a deleted token looks
merely unknown, a retired one proves a spent credential was reused. A retrying client and a thief are
then indistinguishable, so the whole `family_id` is revoked. Being wrong that way costs one sign-in;
the other way costs somebody's balance.

## Tokens are exchanged, not forwarded

`_internal_auth` validates an OAuth access token and presents it onward as a short-lived, typed
`aud=identity` token for its user, pinned to the grant's org, with its explicit expiry enforced. That
keeps OAuth inside `mcp.py` instead of teaching `require_member` a third token type, and means
`_resolve_org` must honour the pinned team. For a native identity bearer, `_internal_auth` surfaces
the signed team pin via `read_identity_claims`; it never treats a typed browser session as a bearer.
Test the flow end to end: claims validation can pass while tools still forward the raw bearer.

## Sign-in mid-authorization

A signed-out visitor to `/oauth/authorize` has the destination parked in a short-lived HttpOnly
cookie and is sent to `/?signin=oauth`, which opens the sign-in modal without creating a sandbox
session; the query parameter is only a UI cue and carries no OAuth data. Every sign-in door resumes
the parked request. The cookie stores a relative path and honours only `/oauth/authorize`, so it
cannot become a general post-login redirect.

## `/connect-demo`

A page that plays a third-party app and runs the whole flow (register, consent popup, exchange, tool
calls) using only public endpoints; being on treg's domain gives it nothing. `TREG_CONNECT_DEMO_ENABLED`
gates the page and its callback and defaults off, so production returns 404. The visible log never
shows a token prefix, and disconnect revokes the grant. It exists because a failure inside an agent
client surfaces as a shrug.

## What is deliberately NOT here

- **No per-provider MCP tools.** See above.
- **No provider choice of its own.** treg publishes facts and calls what it is told. It chooses a
  provider only in the two disclosed cases of AGENTS.md non-negotiable 4: a routed `treg.<capability>`
  endpoint, and overflow when treg's own account is out. Both wrap the child's answer and say so.
- **No second copy of the enforcement rules.** Everything goes through the API's own routes.

## Two ways to authenticate: OAuth, or a header

OAuth is the click-to-connect path. The headless path, and the installer's default, is a team-pinned
key as an `Authorization: Bearer` header. `treg mcp install` (`mcp_install.py`) registers the server
into each supported agent with that header: Claude Code via `claude mcp add --scope user` (user-global;
it owns its format and redacts the token), Cursor and opencode via their documented JSON, Codex as one
`[mcp_servers.treg]` table in `~/.codex/config.toml`. The stdlib has no TOML writer, so
`_write_toml_agent` cuts out any previous table as text, appends a fresh one, and writes only when
`tomllib` parses it back to exactly that entry. Hermes (YAML, a server-only dependency) and OpenClaw
are reported with the exact manual step, never written.

- **Codex gets the token inline** (`http_headers = { "Authorization" = "Bearer ..." }`), never via
  `bearer_token_env_var`: a Codex app restarted without the variable gets a 401 and silently drops
  every treg tool. An `X-Treg-Token` header does not work there (the MCP surface answers 401 and starts
  OAuth).
- **The token is verified against `/auth/me` before anything is written**, as `treg login --token`
  does. Otherwise a bad token fans out into every agent and surfaces later as provider-looking errors,
  because catalog reads still work. A signed `scp=bootstrap` token is recognized locally and refused
  without writes (the server stays authoritative). `install_mcp(only=[])` means none, not all.
- **Why a header works although treg advertises OAuth:** a client falls back to OAuth discovery only
  on a 401, and treg returns 200 for a valid header. The key carries its team and needs no second
  header. `curl {BASE}/install.sh | sh -s -- --token <key>` installs, signs in and runs
  `treg mcp install` in one paste.
- **`treg mcp install --oauth` repairs a dead saved header.** Session-secret rotation,
  `revoke-tokens` and Default-key rotation kill the bearer written into each agent while the CLI
  recovers with `treg login`, and Claude Code and Codex refuse OAuth fallback while
  `headers.Authorization` is set. `--oauth` needs no valid token: it rewrites every managed entry to
  the URL only (Claude without `--header`, JSON without `headers`, Codex without `http_headers`) and
  says how each client authorizes. `_resolve_org` names this repair on the unpinned 401 and on a
  pinned `/orgs` 401; a 200 that does not list the team keeps the "no longer available" wording.

A builder proxying MCP sets `X-Treg-Meta` once per session on their own HTTP client; the model never
sees it.
