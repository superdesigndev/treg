---
title: The proxy - faithful credential-injecting relay + tool resolution
status: shipped
sources:
  - src/treg/infra/upstream/relay.py
  - src/treg/jsontext.py
  - src/treg/infra/upstream/ssrf.py
  - src/treg/api.py
  - src/treg/bootstrap_http.py
  - src/treg/application/call/authorize.py
  - src/treg/application/call/idempotency.py
  - src/treg/application/call/intake.py
  - src/treg/application/call/resolve.py
  - src/treg/application/call/reserve.py
  - src/treg/application/call/settle.py
  - src/treg/application/call/evidence.py
  - src/treg/application/call/service.py
  - src/treg/application/call/types.py
  - src/treg/application/asynctasks.py
  - src/treg/application/call/keyslots.py
  - src/treg/alembic/versions/0069_platform_key_slot.py
  - tests/test_platform_accounts.py
  - src/treg/client_identity.py
  - src/treg/call_surface.py
  - src/treg/sandbox_identity.py
  - src/treg/domain/governance/access.py
  - src/treg/domain/governance/publicdemo.py
  - src/treg/domain/governance/usage.py
  - src/treg/routers/call.py
  - src/treg/application/hub/limits.py
  - tests/test_ssrf_public_addresses.py
  - tests/test_call_application_contract.py
  - tests/test_call_cancellation.py
  - tests/test_call_response_limits.py
  - tests/test_error_capture.py
  - tests/test_marketplace_call.py
  - tests/test_oauth_billed.py
  - tests/test_passthrough.py
  - tests/test_tag_billing.py
  - tests/test_tag_billing_adversarial.py
  - tests/test_call_architecture.py
  - tests/test_asynctasks.py
  - tests/test_relay_content_length.py
related:
  - architecture/data-model.md
  - architecture/auth-secrets.md
  - architecture/ads-conversions.md
  - architecture/hub.md
  - architecture/org-flow-control.md
  - architecture/org-endpoint-quota.md
---

# Proxy and call execution

`application.call.service` orchestrates resolution, authorization, reservation, relay and
finalization. `application.call.resolve` selects the target and credential;
`infra.upstream.relay.relay` only injects credentials and forwards bytes; treg never models an
upstream API. The contract itself is AGENTS.md non-negotiable 4; this fragment maps it to code.

Two operator admissions sit on this road and are specified elsewhere:
[published Org flow control](org-flow-control.md) runs in `run_call_surface` after identity and
before money admission (root calls only; routing children take no extra slot or token), and the
[exact org/endpoint allowance](org-endpoint-quota.md) runs inside the call application after
canonical resolution and money admission, immediately before a cache hit is returned or a
direct/overflow request is sent.

Named catalog calls with authorization metadata select the provider and grant method before
comparing hosts, so two providers sharing one API host (Facebook and Instagram on
`graph.facebook.com`) resolve to different tools; the relay stays provider-agnostic.

A live-verified free `GET` catalog row can declare `platform_auth: anonymous`. A team tool or team
credential still wins. Otherwise `_anonymous_offer` requires the deployment's provider allow-list
and creates an unmetered virtual tool with no bindings; `relay()` then injects nothing and strips or
rewrites no caller header. The catalog price is free only when the caller supplies no provider
credential header. No provider-specific anonymous path rules exist in the resolver or relay.

Archive participation (own-key recording, rollout, `tool_called` cache fields) is
[archive](archive.md)'s subject; own-tool calls never touch the archive.

## The faithful-relay contract

Everything not listed here is relayed verbatim: method, path, every query pair including
duplicates, caller headers, caller cookies, body bytes.

Inside `relay()`:

1. **Hop-by-hop headers** (`_HOP_BY_HOP`, plus any header the caller's `Connection` header names,
   RFC 7230) are dropped and re-derived per hop. One value is carried, not re-derived: when the
   caller declared a `Content-Length` for a body, the same value is set upstream. Without it httpx
   frames a streamed body `Transfer-Encoding: chunked`, which Meta's Graph API edge does not read
   (the body arrives empty). A caller who streamed chunked stays chunked. A request body is carried
   only when the caller sent one, so a GET gets no chunked frame.
2. **treg's control and edge headers** are dropped: every `x-treg-*` header by prefix (so a future
   control header is covered without being listed), `_CONTROL` (ngrok, `x-forwarded-*`,
   `x-real-ip`, `forwarded`, `via`, Cloudflare's `cf-*` family, `true-client-ip`, `cdn-loop`) and
   whatever `TREG_CLIENT_IP_HEADER` names. The MCP layer re-sends the caller's address in that
   header on in-process calls; a Cloudflare-fronted provider answers any request still carrying it
   with 403 error 1000. `_scrub_treg_cookies` removes treg's own cookies (`treg_session`,
   `treg_oauth_state`) from `Cookie`, keeping the rest: the dashboard's `credentials:'include'`
   Try-it would otherwise leak the session.
3. **`Accept-Encoding`** becomes `identity` when the caller sent none (the body is relayed raw with
   `aiter_raw`, and httpx would otherwise add its own `gzip`). With `force_identity` it is
   overridden to `identity` wherever the application will read the body: metered settlement, an
   owned free poll, and an archivable own-key answer.
4. **Query pairs consumed into catalog path placeholders** (`drop_params`) are not also sent as
   query.
5. **The injected credential(s).** Each binding overwrites only its target header, query parameter
   or top-level JSON field. A JSON binding is the only case that buffers and reserializes the body:
   formatting changes, non-ASCII is emitted as UTF-8, duplicate keys fail closed, and it must never
   be used where the upstream signs or hashes the raw body. On a connection to treg's own Meta app
   the credential also carries `appsecret_proof`, replacing any caller value
   ([auth-secrets](auth-secrets.md)).

Before `relay()`, on the platform tier only (`service`):

6. `relay.scope_shared_idempotency_key` replaces the caller's `Idempotency-Key` with a digest of
   (org, label, and the full enforced pin for a pinned agent). Every org shares one provider account
   on treg's key; a provider honoring the header would otherwise return org A's job to org B, and
   `resource_ownership.produces` would record it as B's. treg's own idempotency table already
   replays the caller's answer, so nothing is lost.
7. `relay.identify_as_treg` replaces the caller's `User-Agent` with `config.TREG_USER_AGENT`.
   Providers' Cloudflare rules answer library defaults (`Python-urllib`) with 403 error 1010, and
   the account seen there is treg's. The shared client, the async-task poller and the capacity
   collectors send the same header by default.
8. `relay.tag_shared_client` sets the header the provider file names in `client_header` to
   `relay.shared_client_code`: `t_` + the first 24 hex characters of
   HMAC-SHA256(`TREG_CLIENT_ID_SECRET`, provider + team id), replacing a caller value under that
   name. The provider can split usage per customer on treg's account without learning the team, and
   two providers' codes for one team differ. Unset secret or no declaration: nothing changes.
9. A catalog `ceiling_param` is appended to `UpstreamRequest.query_items` (see non-negotiable 4);
   `relay()` forwards it like any other pair.

A team's own key relays `Idempotency-Key`, `User-Agent` and the client header verbatim.

On the response, `relay()` drops hop-by-hop headers (keeping `Content-Length` on HEAD/204/304) and
any `Set-Cookie` for treg's own cookie names (fixation), and adds `X-Content-Type-Options: nosniff`
and `Content-Security-Policy: sandbox` so a browser navigating to `/call/...` never runs upstream
HTML under treg's origin. Every other upstream header, including repeated `Set-Cookie`, is relayed.

### Mechanics a refactor must keep

- Request headers are rebuilt into an `httpx.Headers` multidict from the raw pairs, preserving
  duplicates; injection assigns only the named header.
- The query is merged onto the URL with `copy_add_param`, never passed as `params=`: httpx replaces
  a URL's existing query whenever `params` is given, even empty, stripping a catalog path's own
  query. `tests/test_relay_path_query.py` pins it.
- The path comes from `request.scope["raw_path"]`, not Starlette's decoded path param, so
  percent-encoding survives (npm's scoped publish `PUT /@scope%2fname` 404s if `%2f` is decoded).
- The body streams (`content=request.stream()`), never buffered, except for a JSON binding and for
  `X-Treg-Body-Encoding`: `_BodyDecodeMiddleware` (`bootstrap_http.py`) decodes a base64/gzip body
  before `relay()`, so the upstream still receives plaintext ([api](../interface/api.md)).
- The upstream call uses the shared long-lived `httpx.AsyncClient` (`app.state.http`); keepalive is
  the biggest latency win.
- `relay()` returns a framework-neutral `UpstreamResponse`. The router's body wrapper and
  background task share one idempotent close, so full reads, disconnects, stream errors and
  cancellation close the upstream exactly once.
- Every JSON re-encode goes through `treg/jsontext.py`: `json.loads` accepts an escaped lone UTF-16
  surrogate that a plain `ensure_ascii=False` dump then refuses after the upstream answered.
  `dumps`/`dumps_bytes` fall back to ASCII escapes only for such a value. MCP is the one lossy
  surface: its pydantic serializer cannot write the escape, so `mcp._body` replaces an unpaired
  surrogate with U+FFFD rather than lose a paid answer.

### Bindings

`relay()` loops `tool.bindings` and calls `injectors.inject(...)` per binding, so one request may
carry several credentials. A `format` with no `{secret}` renders literally, which lets a binding
stamp a provider protocol constant (a required API-version header); it still references a secret
for validation and lifecycle and overwrites a caller value. A binding with `platform_setting`
instead of `secret_id` injects one of treg's own credentials from settings (the Google Ads developer
token), never stored in the org's secret store so a tenant cannot read or extract it; a missing
setting is a `502` (`this server has no <setting> configured`). Tier 4 copies the provider's
constant `required_headers` bindings, so a version pin is identical on own-key and platform calls.

Binding validity is checked at registration (`_validate_bindings`, also run by `register_skill`):
known injector, same-org secret, a `format` that renders with only `{secret}`, non-empty names, and
no duplicate target within one location. A call-time injector `ValueError` or an
`httpx.RequestError` becomes an audited `502`.

The Google Ads conversion uploader (`adsconv.py`) spends treg's platform connection but never
reaches `relay()`; see [ads-conversions](ads-conversions.md).

### Error bodies and evidence

Before a 4xx/5xx body reaches the caller, an idempotent replay or the evidence row,
`evidence.redact_error_response` decodes it (gzip/deflate, bounded; own-key calls mirror the
caller's `Accept-Encoding`) and replaces every spelling of every injected credential
(`_secret_renderings`, every account's value of a platform setting) with `***`. Masking is exact
only: the admin-log pattern nets would rewrite ordinary fields like `"key": "uniqueId"`. A
normalized re-check that still finds a credential, a credential that cannot be rendered, or a body
that cannot be decoded replaces the whole body with a `response_redacted` error. An unchanged body
keeps its bytes and encoding; a masked one is re-sent identity-encoded with a fresh
`Content-Length`. 2xx bodies are untouched.

A failed unmetered response is read whole (`_read_whole_if_small`, up to 8 MiB) so masking sees
every byte; one too large is replaced on a credentialed call and streamed untouched
(`_peek_stream_head` keeps a head for evidence) only when nothing was injected. Caller request
bodies on unmetered paths are kept for evidence only when `Content-Length` is declared and within
`evidence._ERROR_CALLER_BODY_MAX`; larger or chunked uploads keep streaming. Redaction order,
access and retention: [data-model](data-model.md#audit-writer-auditpy).

## Connection discipline: a call in flight holds no DB connection

Resolution, authorization and reservation own short sessions. `service._execute_call` commits the
request's secret-loading session before reservation and again before relay or the live demo's
network call: **zero checked-out request connections during upstream I/O**, including
rate-smoothing waits. Settlement, first-call recording and idempotency storage use their own
sessions; `expire_on_commit=False` keeps loaded objects usable. OAuth refresh separates DB work
from token-endpoint I/O. Holding the request transaction through relay deadlocks a saturated pool
(each request holds a slot while its settlement waits for another);
`tests/test_call_pool_discipline.py` checks occupancy at the network boundary. Pool sizing:
[deploy](../ops/deploy.md).

## Tool resolution (`application.call.resolve`)

`* /call/{rest:path}` -> `routers.call.call_tool()` -> `routers.call.run_call_surface()` (shared
with `/catalog/call/` and `/table/`, which differ only in `finish`: see [table](table.md)) ->
`service.execute_call()` -> `resolve_call_target(...)` -> `ResolvedTarget(tool, upstream)`. Each
resolution use case owns and closes its read session.

Resolution is scoped to the caller's org (`Tool.org_id == org_id`), so orgs may reuse tool names and
hosts, and only same-org secrets load. Two shapes:

- **URL-passthrough:** `rest` is the real upstream URL. `_normalize_scheme()` restores the `https://`
  a path param collapses to `https:/`. The tool is matched by host (`Tool.host`), then the longest
  `base_url` prefix at a **path-segment boundary** (`.../v1` must not match `.../v10/...` and inject
  the wrong credential). Lengths compare rstripped, so a trailing-slash duplicate is a real `409`.
  On a tie, a registry-provider-backed tool (binding to a `Secret` with a `provider`) beats a
  hand-registered one; only a genuine ambiguity `409`s, naming every caller-usable colliding tool
  and pointing at `/call/<name>/<path>`. No match is `404`.
- **Named:** `rest = "<tool>/<path>"`, looked up by `Tool.name`; upstream = `base_url + path`. No path
  relays the base URL itself without a trailing slash (Stripe `404`s `/v1/charges/`).

Passthrough candidates are filtered by the caller's ACL (`_tool_usable`) **before** the tiebreak, so
a tool the caller cannot see can neither cause a `409` nor win; this only narrows, and whatever
resolves still passes `_require_tool_use`. When ACL alone removed every match the answer is `403`
naming only the host the caller supplied, never the hidden tool.

A named miss falls through, in order: an exact catalog endpoint, then, only for a top-level call
(`child_of is None`), a hub tool (`<team-slug>.<name>`). An own tool or catalog id always wins; a
hub tool never shadows either. Hub runs take a per-team `hub_limits.slot()` and refuse with
`429 hub_busy` past `MAX_RUNS_PER_TEAM` rather than queue ([hub](hub.md)).

Named-miss diagnostics, all on the error path:

- `<catalog-id>/<path>` (the own-tool shape on a catalog id) answers `400` naming the endpoint's
  parameter slots and the `--query` form.
- A dotted name sharing its first segment with a caller-usable own tool gets `hint` and
  `did_you_mean` pointing at `/call/<tool>/<path>`; for a real catalog id this is attached only if
  the credential ladder also dead-ends. Provider-local catalog near-id matching takes precedence.
- A credential-ladder 404, or a 410 tombstone without `superseded_by`, includes
  `_capability_alternatives(ep)`: same-capability endpoints, cheapest first, marked callable on
  treg's key or own-credential-only. Synchronous, no DB reads; it suggests, never substitutes.

`* /catalog/call/{rest:path}` (catalog-only MCP surfaces, gated on `claude_connector_enabled`) sets
`catalog_only` and accepts only an exact catalog endpoint id, never calling `resolve_call_target`,
so a private team tool or passthrough path cannot shadow the catalog entry. Everything after
resolution is shared.

### Catalog targets

A live catalog row enters `_resolve_marketplace_call` and its credential ladder.
`_marketplace_upstream` percent-encodes placeholder values but preserves a value already containing
a valid `%HH` escape (an encoded Search Console property id must not become `%253A`). Raw `@` stays
literal: it is legal in a path segment and some email-path APIs reject `%40`. Slashes, query and
fragment delimiters and invalid `%` stay escaped. A catalog `host` on a provider that opted into
`catalog_targets` must resolve through `OAuthProvider.profile_for_catalog_host` to an exact
approved HTTPS base URL, which keeps its path prefix and supplies the binding; anything else is a
treg 502 before reserve, so catalog data cannot redirect an injected credential.

A `retired`/`broken` tombstone is refused with 410, its `status_note` and optional
`superseded_by`, before credentials are selected (audited `refused_by=retired`). An org's own tool
named like the old id already resolved above and is not shadowed.

`strict_query` rows (`_enforce_catalog_query`) reject bodies, undeclared or duplicate query
parameters, missing required inputs and unsupported enum values; declared path placeholders pass
and are consumed. `body_allowlist` rows (`_enforce_catalog_body`) reject undeclared top-level JSON
fields, missing required fields, invalid declared scalars and arrays outside their declared
cardinality or item enum (an array of objects may require all or any named item fields; an omitted
optional array is allowed). Both apply on every tier and never to raw own-tool relays.

### Authorization and deny rules

After resolution `application.call.authorize` runs tool/project ACL, deny, member cap and
public-demo gates in that order, with no hold and no upstream access, and closes its session before
reserve.

`_enforce_deny` matches the **resolved** upstream against the org's `DenyRule` rows, so both call
shapes are equally gated, and the relay never follows redirects, so a blocked host is not reachable
through a 3xx. A provider's old `base_url` stays in `legacy_base_urls`: the call is also checked as
if sent there, and catalog tier 1 still finds a team tool registered on it. Path matching is
anchored at a segment boundary. Rules apply to every role including owner (a guardrail, not a
permission tier). A rule with `project_id` fires only on that project's tools; host/path/method,
member and project are ANDed, each NULL-means-any. `_deny_match` is pure and mirrors
`localrun.check_deny` (argv instead of URL).

### Call-time SSRF guard

Just before `send`, `relay()` re-resolves the upstream host (`host_is_public`, gated by
`proxy_ssrf_check`) and refuses with `502` if any address is non-public: a `base_url` public at
registration can later resolve to `169.254.169.254` or localhost. Registration
(`ssrf.safe_webhook_url`) also normalizes numeric IP encodings (`2130706433`, `0x7f000001`,
`127.1`) through `inet_aton` before the literal-IP block. Targets must be globally routable
unicast. CGNAT `100.64.0.0/10` is internal service space (Tailscale, WireGuard overlays, Fly.io,
some Kubernetes pod CIDRs, Alibaba Cloud's metadata endpoint `100.100.100.200`). NAT64 prefixes
mapping non-global IPv4 do not make a target public; `64:ff9b::/96` stays blocked. A narrow
resolve-vs-connect race remains; closing it needs a custom transport pinning the resolved IP.

## Responses and diagnostic evidence

Treg refusals carry `X-Treg-Error: 1`. Application failures map to
`caller | treg | upstream | org_connection` blame; provider failures remain response data. Header
contracts: [api](../interface/api.md).

`_audit` records each attempt and mirrors it through `_tool_called_props` / `analytics.capture`
(never params or bodies). `domain.catalog.results.classify` shares hit/miss rules between telemetry
and cache admission and inspects only already-buffered bodies
([archive result admission](archive.md#result-admission)). Overflow keeps both attempt rows under
one call reference but emits one product event (`defer_analytics`).

Exceptional exits are recorded once, guarded by the audit marker:

| Exit | Event |
|---|---|
| Pool timeout before caller identity | `call_intake_failed`, surface only, no team/target |
| Pool timeout after identity | `tool_called`, `outcome=gateway_failed`, `failure_kind=db_pool` |
| Unexpected exception while awaiting `execute_call` | `tool_called`, `failure_kind=unexpected_exception`; Starlette still returns the bare 500 |

Exceptions outside `execute_call` and body-stream failures after the handler returns are outside
this contract. Never relabel a stream failure as a new 500 after headers were sent.

## Resolution and relay guards

The guards against a caller-controlled upstream are the path-segment-boundary prefix match and ACL
filtering in [Tool resolution](#tool-resolution-applicationcallresolve), deny rules evaluated on
the resolved upstream, the response headers in the [contract](#the-faithful-relay-contract), and
the [call-time SSRF guard](#call-time-ssrf-guard) with its address-space list.
`injectors._token_from_json` rejects a non-string field rather than inject garbage, and
`health._probe` skips a dangling binding.

## Routed endpoints - the resolve stage short-circuit

A `kind: routed` row (`treg.<capability>`, [catalog](catalog.md) § Routing) never reaches the
credential ladder itself. `service._execute_call` hands it to `application/call/route.py`, which
runs each child endpoint through this same use case as a child `CallContext` (`call_ref`
`{parent}:r{n}`), so ladder, reserve, relay, capacity, overflow, audit and cancellation apply per
child. Settlement does not: a child that would charge appends a `DeferredSettle` to the parent's
`deferred_settles` and leaves its hold open; a child that is not billable or settles at a
provider-reported zero releases at once so it never blocks the next child's reserve. The parent
closes every child hold exactly once with `settle.close_deferred(charge=...)`: settle each billable
child as its own settle would have, or release all because a failed routed call charges nothing.
A crash before that leaves holds to the reaper, which releases in the caller's favor.
`route.child_outcome` reads each child's answer and is shared with
[replay](replay.md). The parent assembles `{output, raw, _treg}` and owns the idempotency label.
A pending outcome (`202`, `X-Treg-Route-Outcome: pending`) sends no `X-Treg-Cost-Micro`.

## Several treg-owned accounts behind one provider

`TREG_PLATFORM_ACCOUNTS` adds accounts behind a provider as JSON from provider to labelled
accounts: `{"contactout": {"b": {"platform_key_contactout": "..."}}}`. The deployment's own
`platform_key_<provider>` is the account labelled `primary`, and every reader outside the call path
(balance collectors, archive refresh, overflow verification) uses only it. An account names every
credential that differs per account. `keyslots.check_accounts` refuses to start the API or the
workers that call providers on an account the call path cannot use (primary key unset, no pasted
platform key, or a missing paired setting). Rows, buckets and telemetry (`tool_called.key_slot`)
name the label, never a credential, so reordering accounts or regenerating a key changes nothing.

`application/call/keyslots.py` picks the account:

1. A call acting on a task, result or managed resource uses the account that created it
   (`key_slot` on `AsyncTaskRecord`, `AsyncResourceRecord`, `ProviderResource`; NULL means
   `primary`). A label configured for the provider in this process but no longer listed is refused
   with 410 `platform_account_retired` before reserve, and the async worker releases that task's
   hold. A process holding none of the provider's further accounts answers 503
   `platform_account_unavailable`, which the worker backs off on, so a worker that lost its
   environment never releases a task it could poll once restored. Objects on different accounts:
   409 `platform_account_split`.
2. A call carrying `Idempotency-Key` uses the account the org-scoped key hashes to, so a retry
   reaching the provider lands where it can be recognized. The idempotency claim cannot remember the
   account: a failed call deletes its claim, and that is exactly the retry that reaches the
   provider.
3. Otherwise an account not out of capacity whose smoothing bucket would wait least, ties rotating.
   Rules 1-2 run during resolution (`pinned`); rule 3 runs right before relay (`assign`) and takes
   the bucket's token with no await between, so a burst that resolved together still spreads.

A non-primary call carries `account: <provider>/<label>` on its platform binding; the relay injects
that account's values or fails `injection_failed`, never substituting another account. The primary
carries no `account` and uses the provider's own bucket, so a one-account provider is unchanged.

Capacity is per account: a signature strikes and locks only the account that answered
(`marks.lock_key(..., slot)`, `<key>#<label>` for non-primary), a 2xx clears only its own strikes,
and a probe goes to the locked account. A call is refused before reserve only when its pinned
account, or every account of an open call, is out. The sweep reads only the primary, so another
account's lock lifts at expiry or through its own probe.

## Platform capacity: refuse before reserve

Platform-tier request guards (`_enforce_platform_request`, catalog `platform_request`) run after a
platform offer is selected and before reserve. They bound treg's exposure on its shared accounts:
explicit bounded record counts, exact pricing selectors (a singleton-enum body field that picks a
table price row must equal its value; duplicate JSON keys are rejected so validation and upstream
cannot disagree), and outright refusal of options whose charge or result arrives later at a
caller's webhook, outside settlement's reads. Own keys are unaffected: the provider, not treg,
bears that account's exposure.

Tier 4 spends treg's own vendor account, which can be empty. After `_platform_offer` says yes,
`_resolve_marketplace_call` consults the in-process capacity view (`domain.capacity.view`, loaded
from ratestore before the session opens). Two sources mark a call exhausted: the sweep's
`capacity:state:<provider>` and the call path's lock `capacity:lock:<key>`. Then it raises
`CallFailure("provider_capacity", 503, blame="treg")` **before any hold exists**, with `resets_at`
when known and `_capability_alternatives`; treg names options, never chooses (AGENTS.md). Audit
`refused_by="capacity"`, cost 0. A stale, empty or "ok" view never refuses. A free route
(`marks.spends_balance` false) counts only its endpoint's own quota lock: balance readings and
locks neither refuse it, admit it as a probe, nor take its 2xx as recovery (`metered=False`), since
a vendor answering free routes would otherwise erase every paid route's strike.

The breaker (`domain.capacity.marks`) opens slowly and closes fast. After a tier-4 answer >= 400,
`settle._note_capacity_signal` classifies it (`domain.capacity.signatures.classify`). A `balance` or
`quota` signature is a strike; a second strike inside the window, far enough after the first that a
concurrent burst counts once, with no 2xx between, locks the provider (balance) or only the
endpoint (quota; allowances are per operation). While locked, resolve admits one real call per
process per interval as a probe (`MarketplaceCall.probe_lock_id`); its 2xx clears exactly that lock.
A sweep `exhausted` reading admits the same probe, and its 2xx lifts the reading until the next
sweep (`marks.clear_sweep_state`, conditional on `observed_at`). Hold lengths are capped whatever
`retry-after` says; the sweep never writes this namespace. These writes run on their own short
session after settle closed the hold (`capacity_exhausted_mark` in
`tests/test_call_architecture.py`).

A burst or unknown 429 only logs (smoothing handles it). An `edge_block` (the vendor's CDN
answered: `cf-mitigated`, an HTML block page, 1xxx problem-JSON) strikes nothing, because one
caller's request shape must not take the provider away from every team. An `unrecorded` signal (a
4xx matching no row whose body still names credits/quota/balance) emits a `capacity` `unrecorded`
event ([capacity](../ops/capacity.md)): the tripwire for a vendor dialect missing from the table.
Tiers 1/2 never consult the view; an org's own key running dry is relayed unchanged, as is the
vendor's 402 on this call. The protection is for the next caller.

## Burst smoothing on treg's own keys

Many callers share one platform key, so tier 4 makes its own bursts. Two bounded mechanisms in
`service._execute_call` run after the DB phase and before relay, platform tier only, never a
refusal (the shared rate window in [capacity](../ops/capacity.md) is the refusing gate):

1. **Spacer** (`infra/upstream/limiter.py`): one call per `window_s / limit` per bucket, a token
   bucket of capacity one, because a burst of `limit` at t=0 is legal for a classic bucket and
   exactly what a sliding-window provider 429s. A call waits at most `DEFAULT_MAX_WAIT_MS`, then
   proceeds; the hold is placed, so the org pays latency, never money. The limit comes from
   `view.rate_limit` (`CapacityPolicy.rate_limit`); an endpoint with its own upstream allowance
   (`policy._ENDPOINT_RATE_LIMITS`, `view.rate_bucket`) is its own bucket, as is each non-primary
   account. In-process because its wait needs no round trip, so each replica adds its own
   allowance; the shared window holds the declared rate across replicas and refuses, with the typed
   `provider_rate_limited` 429, only on buckets listed in `TREG_PROVIDER_RATE_ENFORCE`.
2. **One `retry-after` re-send** on a tier-4 429 classified `burst` with `retry-after <=
   SMOOTHING_RETRY_MAX_S`, for a body-less GET/HEAD only: close, sleep, resend the identical
   request once on the same hold, settle on the second answer. The re-send asks the shared window
   after the sleep; on a full enforced bucket it is not sent and the first 429 stands. A quota 429, an unknown 429, a POST
   or a second 429 is relayed as is. No other retries (401/402/5xx).

Both show as `X-Treg-Smoothed: wait=<ms>` and/or `retry=1` on the metered response.

## Overflow - the child cycle (off by default)

**Overflow = the same vendor endpoint, another account of ours.** When a tier-4 call fails on
treg's key for a treg-side reason (a balance/quota signature, a burst 429 smoothing could not
absorb, or treg's own rate window refusing it before it was sent, forced trigger `burst`) and the worker has an enabled `OverflowRoute`, `application.call.overflow.maybe_overflow`
runs a child cycle after the primary's settle released its hold:

1. Pick a route from `domain.capacity.routes_view`, skipping an aggregator marked unhealthy or
   without a key; check `OverflowSpend` against `overflow_daily_budget_usd` on a short session.
2. **Child hold**, own id `{call_ref}:overflow`, through the ordinary `_platform_reserve`. Never the
   parent's id: release-by-id is a conditional claim, and `_finish_cancelled_call` releases both ids
   exactly once.
3. One aggregator run with **no DB open**: `infra.upstream.aggregators.<name>.build` wraps the
   caller's original query and buffered body; the key (`Settings.overflow_key_<name>`) is never
   logged.
4. `parse` yields vendor status, body and the real in-band cost; `_platform_settle(child,
   observed_override=cost, overflow_spend=...)` charges exactly the aggregator's price and folds the
   day's spend into the same transaction (`overflow_spend_in_settle`, the one allowlisted overflow
   write).
5. The vendor's body is the answer, with `X-Treg-Served-Via: overflow:<name>`, the child's
   `X-Treg-Cost-Micro` and the parent's `X-Treg-Call-Id`; the child's audit row has
   `credential_tier="platform-overflow"`. MCP carries the same disclosure as `served_via` (an MCP
   client never sees headers). Money: [money](money.md) § Overflow money.

When the resolver already knows the account is out and a route is on, the ladder skips the direct
attempt (`MarketplaceCall.skip_direct`): no parent hold, straight to the child. Cancellation checks
the authenticated caller's org before releasing the child hold.

Routes are normally admitted by the aggregator-to-direct price ratio; routes in
`routes.request_priced` (a flat aggregator fee against a per-row direct price) are admitted by an
absolute fee ceiling instead, with no per-request check, and settle once at the aggregator's price
even for an empty page.

**An aggregator failure is data.** Its own 401/402/403 or a malformed envelope releases the child
and marks `overflow:<name>` unhealthy for everyone. A relayed vendor answer the signature table
reads as that vendor's own out-of-credit or quota dialect (`VENDOR_DRY`) marks only
`overflow:<name>:<provider>`, so one vendor's cap never takes the others offline. Both are
temporary marks, and the caller gets the typed `provider_capacity` 503; a second aggregator is never
tried on the same call. The aggregator's per-request refusal (`contract`: its stricter schema, or a
bare 400/422/404 with no vendor data) is request-scoped: release, charge nothing, mark nothing; the
vendor's own answer stands, and on the skip-direct ladder the caller gets the 503 naming the
refusal, never the aggregator's envelope dressed as the vendor's. `malformed` is only for what is
not an envelope at all (non-JSON, 5xx, transport error): reading a validation 400 as `malformed`
takes the aggregator offline for every org.

**Shadow mode** (`TREG_OVERFLOW_MODE=shadow`) calls the aggregator, logs status, shape and cost and
records the cost in `OverflowSpend` (treg pays, budget-bounded); the caller still gets the vendor's
error and pays nothing.

Never on tiers 1/2, a caller-caused 4xx, a 401, a timeout, PUT/PATCH/DELETE, a route the worker has
not enabled, or a team that opted out (`Org.platform_overflow_disabled`) - checked before any
aggregator is contacted, on both entry points.

## Asynchronous submissions on the call path

A catalog endpoint with an `async` descriptor resolves like any other (`resolve.py` freezes a
`settlement_basis` and the descriptor on the `MarketplaceCall`). A metered 2xx from it is
**deferred** (`application.asynctasks.defer_submission` writes the pending row, hold left open)
unless `_submission_rejected` finds no accepted submission (not JSON, `expect` failed, no task id),
which settles at once at the provider's reported charge else zero, or `terminal_on_submission`
finds a success word in the submission, which settles like a synchronous call. A persistence
failure releases the hold with an alert. The caller receives the provider's body either way; a
"still working" submission is not archived. `routers/call._attach_async_descriptor` adds
`X-Treg-Async`, also on an idempotent replay, so a retried `--await` polls the running task.
Settlement: [money](money.md).

Tier 4 only: endpoints used as an async descriptor's poll or fetch utility accept only task/result
ids present in an `AsyncTaskRecord` for the caller's org and provider, with the frozen
endpoint/parameter rule. Legacy pairs use catalog `resource_ownership` and `AsyncResourceRecord`
(`requires` may name a body parameter, so a POST status utility can authorize an opaque id). Formal
submissions mirror their ids into that table too, so removing a live descriptor reference cannot
make its utility fail open. Consumers whose producer provenance is not modeled are `platform_blocked`
(own key only). Unknown and cross-org ids get the same 403 with no provider contact. Fetch-mode
result ids are learned from an authorized successful poll or the worker's terminal response. With a
pin, `_enforce_platform_async_ownership` adds `pinned_tag_predicates`; every pin must match the
submission snapshot and an unknown or foreign id is 404 before relay
([multi-tenancy](multi-tenancy.md#caller-tags-and-pinned-read-scopes)). Own keys keep the
faithful relay: those ids belong to the caller's own account.

Durable shared-account objects use the catalog `managed_resource` contract and `ProviderResource`.
Create relays with no DB connection, then commits ownership before the provider id is returned; a
persistence failure triggers best-effort provider deletion and a treg 502 without the id. Update
changes local state only after upstream success. Delete authorizes active or tombstoned ownership,
treats an owned upstream 404 as deleted, then tombstones, so retries are safe. A managed `use`
declaration may admit provider-public ids through a bounded GET predicate: cross-org and tombstoned
ids are denied without upstream I/O, and wholly unassigned ids are verified after the DB phase and
before reserve. Platform tier only.

An owned platform status poll with an explicit free price and zero estimate
(`MarketplaceCall.free_owned_poll`) takes no reservation or settlement of its own; its buffered
response feeds `observe_owned_poll`, which learns fetch ownership and finalizes the original task on
terminal 2xx evidence. Missing required usage leaves the hold pending; settlement errors keep the
provider response for cron recovery. Polls never use or populate the archive and release any
caller idempotency label so the next poll sees a changed status. They are audited as
`kind=async_poll` at zero cost and excluded from `/calls` before pagination; terminal evidence is
archived under the submission's call id.

## Complete downloads and bounded response evidence

`MarketplaceCall.streamable_free_result` marks platform fetch utilities with an explicit free price,
zero estimate, a required fetch ownership rule and no submission, owned poll or produced-resource
evidence. Their successful GETs bypass `_buffer_response` (ownership is still checked before relay)
and stream in full with headers (including Range) through the router's close-once lifecycle. Audit
size is unknown, not zero; nothing is archived or kept for replay, and the claim is released.

Other responses needing settlement or ownership evidence have a hard 8 MiB complete-body budget:
`_buffer_response` raises `GatewayFailed(response_buffer_limit)` on the first overflowing chunk,
before any success headers, and closes the upstream in `finally`. The hold and claim are released,
the caller gets a 502 at zero cost blamed on treg, and no prefix reaches settlement, archive or
replay. Own-key streams are outside this limit. `tests/test_call_response_limits.py` covers both
HTTP hops.

### Spooled evidence for inline media

Some providers return generated media inside JSON with the usage meters after it (Gemini
`generateContent`: base64 image and `thoughtSignature`, tens of MB at high resolution). Streaming
would mean settling after the response starts; a larger buffer would hold that per call in a web
process. Such an endpoint declares `spooled_response: true`, and `_read_evidence` hands its metered
2xx to `_spool_response`:

- The body streams into an unlinked `tempfile.TemporaryFile` under `spool_max_bytes` and the
  per-process `spool_budget_bytes`. A declared `Content-Length` is claimed whole before the first
  read, so concurrent answers are admitted or refused whole rather than all stalling half-read;
  otherwise the claim grows per chunk. Crossing either limit raises `response_buffer_limit` before
  headers (the budget refusal says it is temporary), closing the upstream in `finally`.
- The file is parsed once in a worker thread, at most `spool_parse_concurrency` at a time. Only the
  paths settlement reads survive (`_spool_evidence_paths`: each usage term's top-level object and
  the `expect` leaf at its original place) and become the `body` for usage settlement, result
  classification and capacity signatures. A non-object body yields empty evidence; a `usage` basis
  then settles at its reserve.
- Settlement runs before the response starts; the router relays the file in chunks with the
  provider's headers and a recomputed `content-length`. Close returns the budget, and
  `weakref.finalize` does so for a response dropped unclosed.
- A spooled body is never archived or stored for replay; the audit row records the full size.
  Non-2xx answers, own-key calls and routed children keep their existing paths.
  `tests/test_call_spool.py` covers the lifecycle.
