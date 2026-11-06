---
title: The API — the only brain (FastAPI)
status: shipped
sources:
  - src/treg/application/key_spend.py
  - src/treg/routers/media.py
  - src/treg/web/sitetrack.js
  - src/treg/api.py
  - src/treg/bootstrap_handlers.py
  - src/treg/bootstrap_http.py
  - src/treg/call_surface.py
  - src/treg/caller_metadata.py
  - src/treg/client_identity.py
  - src/treg/application/auth.py
  - src/treg/application/provider_resources.py
  - src/treg/application/call/access.py
  - src/treg/application/call/authorize.py
  - src/treg/application/call/idempotency.py
  - src/treg/application/call/intake.py
  - src/treg/application/call/resolve.py
  - src/treg/application/call/reserve.py
  - src/treg/application/call/settle.py
  - src/treg/application/call/evidence.py
  - src/treg/application/call/service.py
  - src/treg/application/call/types.py
  - src/treg/infra/upstream/relay.py
  - src/treg/application/connect.py
  - src/treg/application/onboard/__init__.py
  - src/treg/application/onboard/sandbox.py
  - src/treg/application/referrals.py
  - src/treg/application/signup.py
  - src/treg/routers/__init__.py
  - src/treg/routers/activity.py
  - src/treg/routers/admin.py
  - src/treg/routers/auth.py
  - src/treg/routers/auth_helpers.py
  - src/treg/routers/billing.py
  - src/treg/routers/call.py
  - src/treg/routers/catalog.py
  - src/treg/routers/connections.py
  - src/treg/routers/onboard.py
  - src/treg/routers/orgs.py
  - src/treg/routers/provider_resources.py
  - src/treg/routers/api_keys.py
  - src/treg/routers/resources.py
  - src/treg/routers/referrals.py
  - src/treg/routers/signup_cookies.py
  - src/treg/routers/web.py
  - src/treg/domain/identity/access.py
  - src/treg/domain/identity/api_keys.py
  - src/treg/domain/governance/teams.py
  - src/treg/domain/governance/access.py
  - src/treg/domain/governance/budgets.py
  - src/treg/domain/governance/publicdemo.py
  - src/treg/domain/governance/sandbox.py
  - src/treg/domain/governance/usage.py
  - src/treg/domain/identity/mcp_oauth.py
  - src/treg/domain/identity/session.py
  - src/treg/sandbox.py
  - src/treg/sandbox_identity.py
  - src/treg/timeutil.py
  - src/treg/domain/catalog/store.py
  - src/treg/email.py
  - src/treg/runner.py
  - src/treg/ratestore.py
  - tests/test_cli_pairing.py
related:
  - interface/cli.md
  - architecture/proxy-model.md
  - architecture/auth-secrets.md
  - architecture/ads-conversions.md
---

# The API

The route table, request and response models live in the running server's OpenAPI (`/openapi.json`,
rendered at `/docs`; FastAPI's console is at `/docs/api`). This fragment keeps only the contracts
and guards the schema cannot show. CLI usage is in `USAGE.md`.

## Composition

`api.router` preserves public registration order while concern routers contribute ordered route
blocks; `bootstrap.create_app()` assembles them into FastAPI roles, and `api.app` stays the
backward-compatible `all` role. The lifespan runs read-only `verify_db()`, owns the shared
`httpx.AsyncClient` at `app.state.http`, starts the background workers, and drains audit on
shutdown. Content-driven companion backfills run only in `python -m treg upgrade`, never in an app
lifespan. `/docs` is a server-rendered reference built from `app.openapi()`; HEAD widening is
explained in [seo](seo.md).

## Auth

`require_member()` resolves a membership token, a team Default key, a legacy identity bearer plus
`X-Treg-Org`, or a session cookie plus `X-Treg-Org`, and returns a `Caller`. A 401 keeps `detail`
and adds a top-level `hint`, because scripts and older CLIs print it verbatim. Open by design:
`POST /invites/accept`, `GET /oauth/callback` (protected by `state`). Identity and
session validation live in `domain.identity.session`, authorization in `domain.identity.access`;
org scoping and roles are in [multi-tenancy](../architecture/multi-tenancy.md).

- Each identity dependency commits its read-only transaction before the handler runs, so a use
  case can open its own session without waiting behind the request's pool slot. This relies on
  every session maker setting `expire_on_commit=False`.
- Sandbox identities (the closed landing sandbox) are refused centrally in
  `domain/identity/access.py` (`_refuse_sandbox_visitor`, 403); never re-add per-route exceptions.
  `POST /demo/sandbox` answers 410.
- **Public demo tokens** (`POST /orgs/{id}/public-token`, owner-only; re-POST rotates) are
  viewer-role tokens on a can't-log-in identity. Lockdown is centralized in the auth deps: below
  admin on a `public_demo` org, `require_member` allows only `/call/*` and GET/HEAD/OPTIONS, so
  routes added later stay frozen, and `require_identity` refuses the token entirely. Its calls are
  metered per client IP (`_enforce_public_demo_ip_cap`).
- An agent token is refused by `require_identity` (so it cannot mint an org it owns), but
  `GET /auth/me` returns its `org_id`/`org`/`role` so `/orgs/{id}/...` commands work for it.
- Token audiences are not interchangeable: sessions carry `aud=session` and expiry, identity keys
  `aud=identity`; bootstrap credentials add `scp=bootstrap` and a seven-day expiry and cannot reach
  team resources even with `X-Treg-Org`; Default keys carry `scp=team`, an authoritative org and
  a generation. MCP's internal exchange token is the deliberate 120-second identity exception.
  `token_version` revokes permanent keys (missing `tv` means zero). See
  [auth-secrets](../architecture/auth-secrets.md).
- Every identity door (GitHub, Google, email OTP, inbox-only invite link) reuses one first-proof
  provisioning command and creates no automatic org. Suspended users and addresses on
  `TREG_BLOCKED_EMAIL_DOMAINS` (subdomains included) are refused at every door with the same 403.
  `expose_dev_code` permits OTP disclosure only on guarded local SQLite. `TREG_FIXED_LOGIN_CODES`
  issues a configured code hash for an account with no inbox (a directory reviewer's demo
  account); limits are unchanged. The invite code is join-only; the separate `email_token`
  authenticates once and never appears in a JSON response.
- `require_managed_cli`'s 426 update response carries `X-Treg-Error: 1` and `no-store`, and runs
  before any login or team mutation.

### CLI pairing

`POST /auth/cli/start` mints a login id and a four-character code shown only in the terminal,
never in a URL. `POST /auth/cli/approve` requires the session, the same-origin CSRF check, the
matching code and membership in the chosen team; `GET /auth/cli/poll` hands over the Default key
exactly once (only a seven-day bootstrap credential when no team was chosen).

State lives in the database `Ephemeral` table so every worker and instance shares it. Approve
locks the pairing row with a no-op update (works on PostgreSQL and SQLite), then commits the
encrypted result and any Default-key creation together; wrong attempts commit their decrement
without extending expiry and the final miss deletes the row. Poll consumes with `DELETE RETURNING`
and returns credentials only after commit; a pending or unknown poll never writes. Consumption is
at-most-once, not guaranteed delivery: a lost response means a new login. Store failures return a
generic non-cacheable 503 with no local fallback. All workers need the same database and secrets.

## Managed API keys

A human membership has one signed, team-specific Default key; rotation bumps that row's signed
generation, so only the prior token for that team stops. Default keys can be disabled but not
revoked. Additional and Agent keys are random, hash-stored secrets; rotation creates a new row and
keeps the hidden predecessor for audit. Revoking an Agent key removes its machine membership and
all its keys; revoking a human key never removes the human membership. Only creation and permitted
rotation return a secret, and every credential response is `Cache-Control: no-store`. Billing,
balance and daily-cap checks always use the resolved membership, not the key.

`GET /orgs/{id}/api-keys/spend` and `GET /orgs/{id}/usage/spend` read only the daily spend rollup
([money](../architecture/money.md), "Spend per API key"); `ready` is false with no numbers until it
has caught up once, and `charges` counts settle entries, not calls. A member sees only their own
keys; the `unattributed` line is admin+.

Agents (`POST /orgs/{id}/agents`): re-POSTing the same name rotates the token and leaves omitted
fields as they are, so a rotation never changes limits. `connected` checks are one indexed
existence query scoped to the live key, never a history scan; an old key's history cannot confirm
a new key. Revoking an agent sweeps its deny rules and idempotent replay cache before deleting the
membership.

## Teams and governance

- `POST /orgs` refuses when the caller already owns `teams.MAX_OWNED_TEAMS`.
- `DELETE /orgs/{id}` requires `?confirm=<slug>` (422 without). It sits one path segment above
  every other org route, so a client normalizing `..` turns `DELETE /orgs/{id}/<x>/..` into it
  before the request leaves; server-side validation cannot see that. Keep user-supplied values out
  of path segments for the same reason.
- Deleting a project removes its id from every member's `project_access`, leaving `[]` (NULL would
  mean every project).
- An all-empty deny rule is refused (it would freeze the org); a full URL is reduced to its host.
- **Daily call cap**: `governance/usage.enforce_daily_cap` runs in `/call/`'s authorization gate and
  at the top of `grant_local_run`. It takes the slot with one conditional UPDATE
  (`take_daily_slot`): the WHERE is the check and the SET is the count, so the cap is exact under
  concurrency and refused events are not counted. `-1` skips it; a database error fails open (a
  courtesy limit, not a money gate). `used_today` (the members list and `/usage/me`) reads the
  usage rollup and the rows after its cursor; `set_member_cap` copies the journal count
  (`count_today`) onto the counter when a member goes from unlimited to capped. Never put the
  journal count in the gate or on a page: it scans the member's whole day.
- Dashboard reports (`/usage`, `/usage/by-tag`, `/agents/observed`) run on the gated report
  session and answer `429 {"report_busy": ...}` with `Retry-After: 2`, never `treg_saturated`
  ([deploy](../ops/deploy.md#dashboard-reports)).

## Own tools, secrets and runs

- Secret values are never returned (`_secret_view`, `_tool_view`, `_bundle_view`), except through
  the audited local-run grant. `delete_secret` and `delete_bundle` refuse a secret still referenced
  by an HTTP binding or a `cli.inject` entry outside what is deleted.
- **Owner-only binding**: binding or injecting a secret requires owning it
  (`_require_secret_ownership`); only admin/owner may wire a teammate's secret, which stops a member
  laundering a key into a tool they control and extracting it. `update_tool` grandfathers secrets
  already on the tool, so only newly added bindings are checked. The skill importer runs the same
  checks.
- **No SSRF at registration**: `_require_public_base_url` rejects loopback, private, link-local and
  metadata hosts including numeric IP encodings; the proxy re-resolves at call time anyway
  ([proxy-model](../architecture/proxy-model.md)).
- `POST /tools/{name}/grant`: returning a secret the caller does not own requires `X-Treg-Run-Proof`
  matching `TREG_RUN_PROOF`, held only by the isolated runner; a refusal writes a `DENY` audit row.
  See [local-run](../architecture/local-run.md).
- `POST /run` returns 503 unconditionally (AGENTS.md); `_tool_view` reports
  `server_runnable: false`.

## Connections and OAuth

`POST /oauth/start` runs BYO or registry mode. Registry mode refuses a pasted-secret provider (422
`pasted_secret_provider`, naming `POST /connections/token`) and stashes every per-provider auth
quirk on the `PendingOAuth` so the callback exchanges the code exactly as the consent URL was built.
`redirect_uri` is pinned to treg's own callback (consent-phishing guard). `connection_id` targets
one existing connection to reconnect or widen instead of adding an account. The callback
auto-provisions the provider tool; its `examples` stamp only verified core endpoints, because an
example promises the call works. `TREG_OAUTH_REVIEW_PENDING` and Instagram's strategies are in
[instagram-oauth](../architecture/instagram-oauth.md); an unmet grant fails before relay with a
typed 428 naming the required method, scopes and the CLI/dashboard action.
`X-Treg-Authorization-Method` selects one of an endpoint's declared methods and is stripped before
the relay.

`POST /connections/token` verifies a pasted credential against the provider's probe before storing
it ([auth-secrets](../architecture/auth-secrets.md)). `DELETE /connections/{id}` removes the
auto-provisioned tool and drops the dead binding from user-built tools, leaving their other
bindings. `PATCH /connections/{id}` renames the account, which is the tool name agents call: the
secret, main and companion tools and `tool_access` lists move in one transaction, the old name
stops resolving (no alias), and a taken name or a live hub recipe `uses` naming the old tool is a
409 (a published version is never rewritten). `PATCH /secrets/{id}` refuses renaming a provider
connection for the same reason.

`GET /orgs/{id}/provider-resources?source=platform` returns only rows the org owns; the
dashboard's Team resources tab uses it so BYOK account objects are never shown as treg-owned. With
`source=auto` a team Fish credential relays Fish's own voice list, and `X-Treg-Resource-Source`
says `byok` or `platform`. The raw `/call/fishaudio.voices.list` remains a faithful BYOK relay.

## The call surface

`* /call/{rest}` captures a framework-neutral `CallInput` for `application.call.service.execute_call`
([proxy-model](../architecture/proxy-model.md)). `_execute_call` tries an own tool, a catalog
endpoint, then only when both miss a hub tool (`<team-slug>.<name>`, 429 `hub_busy` over the
per-team slot; [hub](../architecture/hub.md)). `* /catalog/call/{rest}` (hidden from public
OpenAPI) requires an exact catalog id and skips own-tool resolution, so a same-named team tool
cannot shadow the catalog endpoint; from the credential ladder on it is the same call.

A pool with no slot within 5 s answers `503 {"treg_saturated": true}` with `Retry-After: 2`
(`_pool_saturated`), still captured as a `db_pool` fault and a `tool_called` event, and releases an
acquired idempotency label so a retry is not a false 409. A `platform_auth: anonymous` endpoint
relays through a virtual tool with no bindings when no team credential exists: no platform key, no
balance entry, caller headers preserved, so a caller-supplied provider key can still be charged by
the provider.

### Headers treg adds

| Header | Meaning |
|---|---|
| `X-Treg-Call-Id` | On every relayed response and every refusal treg raises before the relay, including the saturation 503 (`_stamp_call_exit`); the audit row's join key. An unexpected fault is Starlette's bare 500 with no id, but the row and `tool_called` are still recorded. |
| `X-Treg-Cost-Micro` | The metered charge; on an async submission the reservation; explicit `0` for a reserved call that failed before a provider answer (the balance can lag it if the release fails, [money](../architecture/money.md)) and for owned free polls. Absent on own-key and own-tool calls. |
| `X-Treg-Error: 1` | treg's own refusal on `/call/`, `/catalog/call/`, `/table/`, so a caller can tell treg's 404 from the vendor's. Status and body untouched. |
| `X-Treg-Async` | The effective async descriptor as compact JSON, attached before streaming; provider bytes are not read. |
| `X-Treg-Served-Via` | `overflow:<aggregator>` when served through an overflow relay ([proxy-model](../architecture/proxy-model.md), Overflow); MCP results carry `served_via`. |
| `X-Treg-Upstream-Failed` | `<json_path>=<value>` on a metered 2xx failing the endpoint's `expect` rule. Body untouched, settlement unchanged, never archived or counted found. |
| `X-Treg-Capacity: exhausted` | A platform-tier answer the capacity table reads as treg's own account out, set from the same signal that strikes the breaker. Absent on own keys, treg's refusals, overflow, replays. |
| `X-Treg-Smoothed` | `wait=`, `retry=1` or `share_wait=` when treg spaced or re-sent a platform call ([capacity](../ops/capacity.md)). Informational. |
| `X-Treg-Cache`, `X-Treg-Fetched-At`, `X-Treg-Age` | An archive hit ([archive](../architecture/archive.md)). |

### Typed refusals

| Status, `detail.error` | Contract |
|---|---|
| 503 `provider_capacity_unavailable` | treg's own account cannot serve now; refused before any hold, names `alternatives` and `resets_at`, never calls one for the caller. A team's own key is unaffected. `platform_overflow: false` in team settings opts into this instead of an overflow relay. |
| 503 `provider_paused` | `TREG_PAUSED_PROVIDERS` lists the provider; refused before any hold, token refresh or upstream request, on catalog ids, connection tools and URL passthrough alike. Discovery leaves its endpoints out, connect routes refuse, existing connections are never changed (listed with `paused: true`), and `GET /meta` carries `paused_providers`. |
| 429 `org_*` | Published org flow-control refusals ([org-flow-control](../architecture/org-flow-control.md)). |
| 429 `share_busy` | Waited too long for a fair-share place; not charged. |
| 429 `provider_rate_limited` | On a bucket listed in `TREG_PROVIDER_RATE_ENFORCE`, the shared rate window is full ([capacity](../ops/capacity.md)); refused before it is sent, not charged, `detail` carries `provider`, `endpoint_id`, `retry_after_s`, with `Retry-After` the same. An enabled overflow route may serve it instead. A routed job skips the candidate without spending its error budget and answers this itself only when every candidate was skipped. |
| 429 `money_admission_timeout` | Money admission exhausted its reserve wait; no hold, no upstream request, `Retry-After` jittered between `money_admission_retry_after_s` and twice that. Not stored under `Idempotency-Key`, so the same key can retry. |
| 402 `route_max_cost` | `X-Treg-Route-Max-Cost: <usd>` would be exceeded by the reserve with margin; no charge. No default on a direct call (unlike routed calls); non-numeric is 400. Where the catalog declares `ceiling_param`, treg forwards the remaining ceiling net of margin unless the caller sent that parameter (AGENTS.md non-negotiable 4). |
| 502 `response_buffer_limit` | Response evidence passed the 8 MiB settlement buffer, or for `spooled_response` the per-body or per-process spool budget (the latter is temporary and worth retrying). Not charged; hold and idempotency claim released. Spooled answers are never stored for replay. |
| 404/409 resolution | A named miss resembling a usable own tool returns `hint` and `did_you_mean`; a URL-passthrough tie is 409 naming the tools and the `/call/<name>/<path>` escape hatch. |

The MCP `call` result mirrors these as fields (`upstream_failed`, `whose_error`, `hint`,
`retry_after`); only treg's own `insufficient_balance` is ever described as the team's balance.

### `Idempotency-Key` on `/call/`

```
Idempotency-Key: <caller's label>        replay if this label was already answered
X-Treg-Idempotent-Replay: true           on a replayed response
X-Treg-Cost-Micro: 0                     a replay charges nothing
X-Treg-Original-Cost-Micro: <charge>     what the first call cost (or reserved)
```

422 when a key is reused for a different request, 409 while the first call is in flight. Over MCP
it is the `idempotency_key` argument and a replay carries `replayed: true`. Storage rules:
[money](../architecture/money.md).

### Caller tags, budgets and isolation

`X-Treg-Meta: customer=cust_8123, feature=email-finder` is set from the builder's backend, never
by a model. Up to 5 pairs, keys `[a-z0-9_]{1,32}`, values up to 128 chars, header up to 512 bytes;
values containing `@` are refused because tags land in an append-only ledger. Any violation is a
422 before relay, so it costs nothing and does not burn an `Idempotency-Key`.

`PUT /orgs/{id}/budgets/{dim}/{val}` is an upsert that leaves unsent fields alone. Tag refusals
carry nothing about the team (no balance, slug or top-up link), because a builder shows them to
its own users: `403 tag_blocked`, `429 tag_spend_cap_reached`, `429 tag_call_cap_reached`. Caps
are advisory; the balance is the hard limit. `usage/by-tag` reconciles:
`attributed_micro + unattributed_micro == total_micro`.

A token minted with `--pin customer=cust_A` beats the header (mismatch is 403), and call history,
archived results, call references, runs and shared-provider async ownership require every pinned
tag; foreign and unattributed ids answer 404. Tag for counting, token for control. See
[multi-tenancy](../architecture/multi-tenancy.md).

### Call inspection

`GET /calls` excludes `kind=async_poll` in SQL before pagination; it is analytics, not an
invoice. `/calls` never selects failure evidence, and `GET /calls/{call_ref}` defers those columns
with lazy loading forbidden: failure evidence is admin-only. Async status and final charge come from
`async_task_app.views_for`. `GET /calls/{id}/result` is described in
[archive](../architecture/archive.md).

## Request transport

**`X-Treg-Body-Encoding`** (`base64`, `gzip`, `base64+gzip`): CDN WAFs 403 bodies that match
injection signatures, which skill recipes and proxied calls legitimately carry. The pure-ASGI
`_BodyDecodeMiddleware` restores the bytes and fixes `content-length` before routing, so both
Pydantic routes and the relay see plaintext; malformed is a clean 400. After delivering the
decoded body it delegates later `receive()` calls to the original channel and forwards only a
real `http.disconnect`. The CLI and local proxy use it automatically on a WAF 403.

**Legacy hosts** (`config.LEGACY_PUBLIC_HOSTS`): `_LegacyHostRedirectMiddleware` 301s GET/HEAD
marketing pages to `public_url` only for anonymous visitors (`treg_session` is host-scoped). The
auth entries in `_REDIRECT_ALWAYS` redirect unconditionally with a 302, because each parks a
host-scoped cookie the continuation must read and one-shot OAuth parameters must not be cached.
Everything else is served in place on both hosts forever: installed CLIs and skills point at the
legacy host, HTTP clients strip `Authorization` on cross-host redirects, and
`curl {BASE}/install.sh | sh` runs without `-L`. The legacy names also stay in MCP's transport
allow-lists and the OAuth token-audience set (old grants keep their audience for life). Never
retire the legacy host.

**Security headers**: `_SecurityHeadersMiddleware` uses `setdefault`, so the `/call` proxy's
stricter CSP wins. An oversized all-digit id is a 404, not a 500; malformed URLs are 422/400.
`auth_logout` rejects a cross-site `Origin`; `_same_origin` is the shared CSRF check for every
cookie-authenticated mutation.

## Public pages and agent files

- `/llms.txt`, `/skill.md`, `/quickstart.md`, `/tutorial.md`, `/vendor-listing` and
  `/install.sh` are templated with `{BASE}` from the serving host, so a self-hosted registry
  advertises itself. `/.well-known/skills/index.json` reads its `description` from the skill's
  frontmatter at request time, never a second copy ([skill](skill.md)).
- `/privacy` is the URL given to OAuth providers at app verification; never rename it.
- `/use-cases/{slug}`: an unknown slug is a 404, not the SPA, so a typo in a live ad is visible,
  and a signed-in visitor is not redirected to `/app`. The HTML is generated from
  `marketing/landing/*.md`; never hand-edit it. `/resources` is the only page linking them.
- `/media` (page assets) serves unversioned scripts, styles and text as `no-cache` and images,
  video and fonts with a one-day max-age; `/vendor/` serves npm-pinned runtimes as immutable.
- `GET /meta` exposes only public configuration; empty third-party keys mean self-hosted pages
  load neither PostHog nor Intercom. `intercom_secret` never leaves the server: `GET /auth/me`
  carries its HMAC of the email as `intercom_user_hash`.
- `/sitetrack.js` sets the first-party `treg_utm` first-touch cookie read at team creation;
  `/adtrack.js` is empty when conversion tracking is off, so unconfigured deployments set no
  advertising cookies ([ads-conversions](../architecture/ads-conversions.md)).
- Feedback, media hosting, pictures and the showcase are owned by their fragments:
  [feedback](../architecture/feedback.md), [media](../architecture/media.md),
  [showcase](../architecture/showcase.md).

## OAuth and MCP

treg is the OAuth authorization server for its own MCP endpoint
([mcp-oauth](../architecture/mcp-oauth.md)). `/mcp/v2/` and its protected-resource metadata exist
only when `TREG_CLAUDE_CONNECTOR_ENABLED=true` (otherwise 404, and new V2 grants are refused).
Approval at `/oauth/authorize` is a POST, never a GET.

## Referrals

**`require_identity`, not `require_member`.** A referral belongs to a person
(`User.referral_code`); the reward lands in the oldest org they own, so nothing here is scoped by
`X-Treg-Org`.

**`GET /referrals` returns the referred person's full email**, so it is scoped in the query itself
(`referrer_user_id == caller.id`), never filtered afterwards, and pinned by a test. `privacy.html`
discloses this.

**`/?ref=CODE` is the one query string the landing route serves.** `GET /` otherwise treats any
query string as the SPA's. A `ref` alone or with only `utm_*` tags counts as parameterless, and the
code is parked in `treg_ref` (httponly, lax, 30 days), revalidated on read because a cookie is
attacker-supplied and reaches a query.

Redemption happens at team creation, right after the signup credit grant and with the same
swallow-and-log posture: a referral must never be why someone cannot make a team. A failed grant
rolls the session back, expiring every tracked object, so team creation reads its response fields
before granting and redemption refreshes `user`/`org` first.

## Billing

`GET /billing/history`: top-up amounts come from credited ledger entries, SSO amounts and invoice
documents from Stripe; a Stripe document failure never removes ledger-backed rows.
`POST /billing/topup` accepts `checkout_source`; it and the `treg_entry_surface` cookie are
normalized to a fixed allowlist and reach only Stripe metadata, analytics and ledger provenance,
never the amount or authorization.
