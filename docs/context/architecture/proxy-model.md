---
title: The proxy — faithful credential-injecting relay + tool resolution
status: shipped
sources:
  - src/treg/infra/upstream/relay.py
  - src/treg/infra/upstream/ssrf.py
  - src/treg/api.py
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
  - src/treg/client_identity.py
  - src/treg/call_surface.py
  - src/treg/sandbox_identity.py
  - src/treg/domain/governance/access.py
  - src/treg/domain/governance/publicdemo.py
  - src/treg/domain/governance/usage.py
  - src/treg/routers/call.py
  - tests/test_call_application_contract.py
  - tests/test_call_cancellation.py
  - tests/test_error_capture.py
  - tests/test_marketplace_call.py
  - tests/test_oauth_billed.py
  - tests/test_passthrough.py
  - tests/test_tag_billing.py
  - tests/test_tag_billing_adversarial.py
  - tests/test_call_architecture.py
  - tests/test_asynctasks.py
related:
  - architecture/data-model.md
  - architecture/auth-secrets.md
  - architecture/ads-conversions.md
  - foundation/charter.md
---

# Proxy and call execution

`application.call.service` orchestrates resolution, authorization, reservation, relay and
finalization. `application.call.resolve` selects the target and credential;
`infra.upstream.relay.relay` only injects credentials and forwards bytes.

Named catalog calls with authorization metadata select the provider and grant method before
comparing hosts. This separates Facebook and Instagram tools sharing `graph.facebook.com`;
the resulting tool enters the same relay without provider-specific relay logic.

## The faithful-relay contract
`relay()` alters **only three things**; everything else is verbatim (method, path, all query params
incl. duplicates, headers, cookies, body bytes):
1. **hop-by-hop transport headers** - `_HOP_BY_HOP` (host, content-length, connection, keep-alive, te,
   trailers, transfer-encoding, upgrade, proxy-*); re-derived per hop or the stream corrupts.
2. **treg's control/infra + edge forwarding headers** - `_CONTROL` (`x-treg-token`, `x-treg-org`,
   `ngrok-skip-browser-warning`, `x-forwarded-*`, `x-real-ip`, `forwarded`, `via`), dropped via
   `_DROP_REQUEST = _HOP_BY_HOP | _CONTROL`, so none leaks upstream. `_scrub_treg_cookies` also strips
   treg's own cookies (`treg_session`, `treg_oauth_state`) from the Cookie header - the dashboard's
   `credentials:'include'` Try-it would otherwise leak our session token - while keeping other cookies.
3. **the injected credential(s)** - each binding overwrites only its target header/param.

> **What treg keeps from a call.** Successes retain no content: the relay forwards bytes and the audit
> row records status, size and timing. A **failed relayed call** - platform, own-key, or plain own-tool
> - is the exception: `CallRecord.error_request` / `error_response` retain a redacted, truncated copy
> of what the caller sent and what the provider (or treg-side 502) answered. Without it a failure is a
> bare status code: `path` holds the catalog URL rather than the caller's parameters and `params_hash`
> is one-way. `application.call.settle` buffers metered responses with `_buffer_response`, while
> `_peek_stream_head` reads
> only the first 8 KiB of a failed unmetered response and replays every consumed byte before the rest
> of the original iterator, preserving status, raw headers, streaming, and the upstream-close task.
> Caller bodies on unmetered paths are cached only when `Content-Length` is declared and at most 64
> KiB; large/chunked uploads stay streaming and retain only their query-param half. See
> [data-model](data-model.md) for the redaction order, admin-only access, and retention.

Faithfulness mechanics inside `relay()`:
- request headers rebuilt from `UpstreamRequest.raw_headers` into an `httpx.Headers` multidict (preserves
  duplicate headers / cookies); injection (`headers[name] = v`) overwrites only the named one.
- query as the router-captured ordered pairs in `UpstreamRequest.query_items` (keeps duplicate keys
  like `?tag=a&tag=b`).
- path rebuilt from `request.scope["raw_path"]` (in `call_tool`), not Starlette's URL-decoded path
  param - percent-encoding survives to the upstream (npm's scoped publish `PUT /@scope%2fname` 404s
  if `%2f` is decoded to a literal slash).
- body streamed via `content=request.stream()` (stream, never buffer). Exception: a caller may
  base64/gzip-encode the body with `X-Treg-Body-Encoding` to slip SQL/HTML past a hosting-edge WAF;
  `_BodyDecodeMiddleware` (in api.py) then buffers + decodes it *before* `relay()` runs, so the relay
  still forwards the real plaintext bytes verbatim upstream. See [api](../interface/api.md).
- upstream call uses the **shared** `client` (the long-lived `httpx.AsyncClient` at `app.state.http`,
  created in `lifespan` - keepalive is the biggest latency win).
- the infra relay returns framework-neutral `UpstreamResponse(status, raw_headers, body_stream, close)`.
  The router wraps it in `StreamingResponse` and copies every upstream response header (incl. multiple
  `Set-Cookie`) minus `_DROP_RESPONSE`. Its body wrapper and background task share the same idempotent
  close operation, so full reads, partial disconnects, stream errors, and cancellation close the upstream
  response exactly once.

A request may carry several credentials: `relay()` loops `tool.bindings` and calls
`injectors.inject(headers, params, binding, crypto.decrypt(secret.value))` per binding.
Bindings can also stamp provider protocol constants: a format with no `{secret}` renders literally
(Crustdata's required API-version header is the first registry use). It still carries the same secret
reference for binding validation and lifecycle, and the assignment overwrites a caller-supplied value.
This is generic binding behavior, not an upstream-specific branch in the relay.

**Platform bindings - injecting treg's OWN credential.** A binding with a `platform_setting` key (instead
of a `secret_id`) injects one of treg's own credentials read from `get_settings()` - the Google Ads
developer token is the case that exists. The value never lives in the org's secret store, so a tenant
can't read it or extract it through a local run; a missing setting is a clean `502`
(`this server has no <setting> configured`). Used by the OAuth-marketplace auto-provisioner for a provider
that needs a second credential treg holds centrally, and by tier-4 catalog calls. Tier 4 also copies
the provider's constant `required_headers` bindings, so Crustdata's `x-api-version: 2025-11-01` pin is
identical on BYOK and platform-key calls (see [api](../interface/api.md)).

A separate case that looks similar but is NOT a platform binding: the Google Ads **conversion**
uploader (`adsconv.py`) also spends treg's own platform connection, but it is not a caller-issued
`/call/` request at all, so it never reaches `relay()` or `infra/upstream/injectors.py` - it reads the platform org's
stored OAuth secret directly and builds its own headers. See [ads-conversions](ads-conversions.md).

**Accept-Encoding is normalized to `identity`** when the caller sent none. `relay()` streams the upstream
body raw (`aiter_raw`), so if the caller doesn't ask for compression httpx would otherwise add its own
`Accept-Encoding: gzip` and hand a plain HTTP client / agent compressed bytes it never requested. Asking
for `identity` keeps what the caller receives matching what the caller requested.

## Connection discipline: a call in flight holds no DB connection

Resolution, authorization and reservation own short sessions. `service._execute_call` commits
the request's secret-loading session before opening reservation, and again before relay or the
live demo's network call. The invariant is **zero checked-out request connections during upstream
I/O**, including rate-smoothing waits.

Settlement, first-call recording and idempotency storage use their own sessions. A commit does not
invalidate loaded objects because the session makers use `expire_on_commit=False`.
OAuth refresh likewise separates DB reads/writes from token-endpoint network I/O.

Keeping the request transaction open through relay can deadlock a saturated pool: each request
holds a slot while its settlement waits for another. `tests/test_call_pool_discipline.py` checks
pool occupancy at the network boundary and covers concurrent calls. Pool sizing, timeouts and
the separate API/admin/background pools are specified in [deploy](../ops/deploy.md).

## Tool resolution (`application.call.resolve`)
`* /call/{rest:path}` → `routers.call.call_tool()` → `application.call.service.execute_call()`
→ `resolve_call_target(...)` returns a framework-neutral
`ResolvedTarget(tool, upstream)`. Each resolution use case owns and closes its read session.
**Both shapes are scoped to the caller's org** (`Tool.org_id == org_id`), so two
orgs resolve independently and may reuse a tool name or upstream host; the use case then loads only
same-org secrets. After resolution `application.call.authorize` runs tool/project ACL, deny, member-cap,
and public-demo gates in that order, with no money hold or upstream access. Its short session closes before
the reserve stage; `-1`/default member caps add no query. Two resolution shapes:

`* /catalog/call/{rest:path}` is the narrower entrance used by catalog-only MCP surfaces.
`routers.call.call_catalog_endpoint` sets `request.state.catalog_only` (gated on
`claude_connector_enabled`) and then enters the same `call_tool` handler; the flag travels on
`CallInput` into `execute_call`. Resolution accepts only an exact catalog endpoint id and never calls
`resolve_call_target`, so a private team tool or arbitrary passthrough path cannot shadow the catalog
entry. Everything after catalog resolution stays shared: credentials, ACLs, deny rules, caps,
cancellation cleanup, metering, audit, idempotency, and faithful relay.

- **URL-passthrough (agent-native):** `rest` is the real upstream URL (`/call/https://api.intercom.io/me`).
  `_normalize_scheme()` restores the `https://` a path param collapses to `https:/`. The tool is resolved
  by **host** (`_host_of()` = `urlsplit(...).netloc`, matched against the indexed `Tool.host`) then the
  **longest `base_url` prefix**; a tie → `409`, no match → `404` (or `403` when the caller's ACL is the
  only thing that removed the match - see below).
- **Named:** `rest = "<tool>/<path>"` (`rest.partition("/")`), looked up by `Tool.name`; upstream URL =
  `base_url + path`. **No path → the base URL itself, without a trailing slash** - a tool pinned to a
  full resource (`.../v1/charges`) must relay as-is, since Stripe `404`s `/v1/charges/`.

Named misses also inspect the org's caller-usable own tools on the error path. When a dotted operation
name shares its provider/first segment with one (for example `google-analytics.report` beside the
connected `google-analytics` tool), the 404 carries `hint` plus `did_you_mean` and points at
`/call/google-analytics/<path>`. If that dotted name is a real catalog endpoint, the hint follows the
catalog fall-through and is attached only if the marketplace credential ladder also dead-ends. Catalog
near-id matching remains provider-local and takes precedence for genuine misspellings.

If both shapes miss with 404, a dotted target gets one final lookup in the endpoint catalog. A live
row enters `_resolve_marketplace_call` and its credential ladder. `_marketplace_upstream` fills catalog
path placeholders by percent-encoding raw values, but preserves a value containing a valid `%HH` escape;
this prevents an already encoded Search Console property id such as `sc-domain%3Aexample.com` becoming
double-encoded as `%253A`. Literal/invalid percent signs remain encoded. A `retired`/`broken` tombstone is
instead refused with 410, its `status_note`, and its optional `superseded_by`, before credentials are
selected or the relay can run; the refusal is audited as `refused_by=retired`. This ordering is
deliberate: an org's own tool named exactly like the old catalog id already resolved above and is not
shadowed, while URL passthrough has no catalog-id shape to catch accidentally.

**Capability alternatives.** A 410 tombstone without `superseded_by`, or a credential-ladder
404, includes `_capability_alternatives(ep)`: curated same-capability endpoints, cheapest first,
marked as callable on treg's key or requiring an own credential. Marked catalog rows are excluded.
The helper is synchronous and performs no DB reads; measured reliability belongs to catalog
inspection. It suggests alternatives without substituting one. Endpoints with no capability
cannot participate.

**ACL-filtered candidates.** `_resolve_call` takes the **caller** and filters passthrough candidates by
`_tool_usable` (project scope AND the per-tool list) **before** the longest-prefix tiebreak. A same-host
tool the caller cannot use must not be able to cause a `409` - or win the tiebreak - for someone who
cannot even see it in `list_tools`. This only NARROWS the candidate set, so it can never grant access:
whatever resolves still passes `_require_tool_use`. The named shape needs no filter (it resolves one
tool, then the gate runs).

**ACL-only misses return 403.** Resolution retains unfiltered host matches to distinguish
an absent tool (404) from candidates removed solely by ACL (403). The latter names only the host
the caller supplied, never an inaccessible tool.

**Policy deny (`_enforce_deny`, `_deny_match`).** After resolution and the tool ACL, the resolved
upstream is matched against the org's `DenyRule` rows (org-wide + the ones aimed at this caller) →
`403` naming the rule. Evaluating the **resolved** upstream is what makes both call shapes equally
gated - a caller cannot dodge a rule by switching to URL-passthrough - and the relay does not follow
redirects, so a blocked host is not reachable via a 3xx bounce. The path match is anchored at a
segment boundary (`/v1/charges` must not match `/v1/chargesX`), the same trap `_resolve_call` guards.
It applies to **every role including owner** (a guardrail, not a permission tier) and to both run
tiers, where the tool's own `base_url` host stands in for the request path. `_deny_match` is pure, so
it unit-tests without a DB - mirroring `localrun.check_deny`, which is the same idea one layer down
(argv instead of URL). Zero rules = one indexed query and no behavior change. A rule may also carry a
`project_id`: it then fires only on calls through that project's tools (every enforcement point has a
resolved Tool by then, so `_enforce_deny` takes `tool.project_id`); an org-wide-tool call is never
caught by a project rule. The three scope axes - host/path/method, member, project - are ANDed and
each is NULL-means-any.

## Responses and diagnostic evidence

Treg refusals on both call surfaces carry `X-Treg-Error: 1`. Mechanism-keyed application
failures map to `caller | treg | upstream | org_connection` blame; the HTTP adapter preserves
status and detail. Provider failures remain response data. Header contracts are documented in
[the API fragment](../interface/api.md).

After credential refresh and relay, `_audit` records the attempt and mirrors it through
`_tool_called_props` / `analytics.capture`. Provider identity is the catalog provider or the
own tool's upstream host. Analytics includes outcome, status, timing, cost, call reference,
capacity/cache/smoothing signals and user-agent attribution, never params or bodies.

Overflow retains both attempt rows under the same call reference, but emits one product event for
the final answer. `defer_analytics` holds the parent's event until the child succeeds or the
parent's answer stands. Failed-request redaction and retention belong to
[data-model](data-model.md#audit-writer-auditpy).

Exceptional exits are recorded once, guarded by the audit marker:

| Exit | Event |
|---|---|
| Pool timeout before caller identity | `call_intake_failed`, surface only, no team/target |
| Pool timeout after identity | `tool_called`, `outcome=gateway_failed`, `failure_kind=db_pool` |
| Unexpected exception while awaiting `execute_call` | `tool_called`, `failure_kind=unexpected_exception`; Starlette still returns the bare 500 |

Before target resolution, target/provider identity remains null. Exceptions outside `execute_call`
and body-stream failures after the handler returns are outside this compensation contract.
Never relabel a stream failure as a new 500 after response headers have already been sent.

## Resolution and relay guards

**Resolution + error hardening:** the URL-passthrough prefix match respects a **path-segment boundary**
(`norm == base` or `base + "/"`), so `.../v1` no longer matches `.../v10/...` and inject the wrong
credential; the longest-prefix tiebreak compares rstripped lengths (a trailing-slash duplicate is a real
`409`, not a silent winner). When two same-host tools still tie on prefix length, `_resolve_call`
**prefers the registry-provider-backed tool** (one whose binding points at a `Secret` with a `provider`)
over a hand-registered one that often holds a stale credential - a `409` there would break exactly the
agent-facing URL-passthrough callers who never typed a tool name; only a genuine ambiguity (neither or
both provider-owned) still `409`s. That 409 names every caller-usable colliding tool and directs the
caller to the unambiguous `/call/<name>/<path>` form. Binding validity is checked at **registration** (`_validate_bindings` rejects
an unknown `injector` and a cross-org/dangling `secret_id`; `register_skill` runs the same gate), and
`call_tool` translates a call-time injector `ValueError` and an upstream `httpx.RequestError` into a
`502` instead of an unhandled 500 (and audits the failed attempt, not just successes). A binding
`format` is validated to render with only `{secret}` and `name`/`secret_field` to be non-empty strings;
duplicate `location:"query"` binding names are rejected (they'd silently overwrite each other).
`health._probe` skips a dangling binding rather than `KeyError`-ing the whole run.

**Relay security + faithfulness (bug-hunt):** the response side strips a `Set-Cookie` for treg's own
cookie names (an upstream must not overwrite `treg_session`/`treg_oauth_state` - fixation) and adds
`X-Content-Type-Options: nosniff` + `Content-Security-Policy: sandbox` (a browser navigating to `/call/…`
must not execute upstream HTML/JS under treg's authenticated origin). It keeps `Content-Length` on a
bodyless reply (HEAD/204/304), only carries a request body when the caller sent one (no bogus chunked
frame on a GET), and honors headers a peer marks hop-by-hop via its `Connection` header (RFC 7230).
`injectors._token_from_json` rejects a non-string field value instead of injecting garbage.

**Call-time SSRF guard (DNS-rebinding defence).** Just before the upstream `send`, `relay()`
re-resolves the upstream host (`infra.upstream.ssrf.host_is_public`, gated by the `proxy_ssrf_check` setting) and
refuses with a `502` if any resolved address is internal (loopback/private/link-local/reserved/multicast).
This catches the case where a `base_url` was public at **registration** but its DNS now points at an
internal target like `169.254.169.254` or localhost - the registration-time check alone can't stop a name
that resolves differently later. Registration itself (`infra.upstream.ssrf.safe_webhook_url`, re-exported
by `health` and reused for `base_url`)
also rejects numeric IP encodings - decimal/hex/octal/short forms like `2130706433` / `0x7f000001` /
`127.1` are normalized via `inet_aton` and re-checked, so they can't sneak past the literal-IP block.
(A narrow resolve-vs-connect race remains; pinning the resolved IP would need a custom transport.)

> Why relay instead of modeling the upstream: [foundation/charter.md](../foundation/charter.md).

## Routed endpoints - the resolve stage short-circuit

A catalog row with `kind: routed` (`treg.<capability>`, generated - `architecture/catalog.md`
§ Routing) never reaches the credential ladder itself. `service._execute_call` hands it to
`application/call/route.py`, which builds the plan and runs each child endpoint through **this same
use case** as a child `CallContext` (`call_ref` `{parent}:r{n}`), so every rule below - ladder,
reserve, relay faithfulness, capacity, overflow, settle, audit, cancellation - applies per child
unchanged. The parent only assembles `{output, raw, _treg}` and owns the idempotency label.

## Platform capacity: refuse before reserve (plan step D)

Tier 4 spends treg's own vendor account, and that account can be empty. `_resolve_marketplace_call`
asks, after `_platform_offer` says yes: is this call **exhausted** in the in-process capacity view
(`domain.capacity.view`, loaded from ratestore on a 60 s TTL by `resolve_marketplace_target` before
its session opens)? Two sources say so: the sweep's `capacity:state:<provider>` (a balance API that
read zero) and the call path's own lock `capacity:lock:<key>` (below). If so it raises
`CallFailure("provider_capacity", 503, blame="treg")` - **before any hold exists** - whose body carries
`resets_at` when known and the same-capability alternatives from `_capability_alternatives`. treg still
does not choose for the caller (charter): it names the options. The audit row is `refused_by="capacity"`,
`X-Treg-Error: 1`, cost 0. A stale, empty or "ok" view never refuses; only a confirmed signal does.

The call path's breaker (`domain.capacity.marks`) opens slowly and closes fast. After a tier-4
answer ≥ 400, `settle._note_capacity_signal` runs `domain.capacity.signatures.classify` on the
vendor's status/headers/body. A `balance` or `quota` signature is a **strike**; the second strike
within 10 min, at least 15 s after the first (a burst of concurrent calls hitting the same empty
instant is one strike), with no 2xx in between, **locks** - the provider for a balance signature, only the
endpoint for a quota one (allowances are per operation). While locked, `resolve` admits one real
call per process per minute as a **probe** (`MarketplaceCall.probe_lock_id`, `probe` on the
`tool_called` event); its 2xx clears exactly that lock (`settle._note_capacity_recovery`, conditional
on the lock id), any other answer leaves it. A guessed hold lasts 1 h, a vendor-stated reset at
most 6 h whatever `retry-after` said, and
the sweep never writes this namespace. Both writes run on their own short session **after** the
settle closed the hold, never during flight, and are the dataplane writes this feature adds
(`capacity_exhausted_mark` in `tests/test_call_architecture.py`). A burst 429 (`retry-after ≤ 60 s`)
or an unknown one only logs; step D′ smooths those. An `edge_block` (the vendor's CDN answered, not
the vendor: `cf-mitigated`, its HTML block page, or its 1xxx problem-JSON) strikes nothing: one caller's request shape must not take the provider away from every
other team. The kind rides the `tool_called` event as `capacity_signal`. Tiers 1/2 resolve earlier
and never consult the view: an org's own key running dry is the org's own answer, relayed
unchanged. The vendor's 402 on THIS call is also relayed unchanged - the protection is for the
next caller.
An `unrecorded` signal - a 4xx no row matched whose body still names credits/quota/balance -
is neither a strike nor a mark: it logs `unrecorded capacity-looking …` with the phrase and rides
`tool_called` as `capacity_signal=unrecorded`, the tripwire for a vendor whose out-of-credit answer is
not in the table yet (how Apollo's 422 went unseen on 2026-09-01).

## Burst smoothing on treg's own keys (plan step D′)

Many callers share one platform key, so tier 4 makes its own bursts: leadsforge 429'd 27% of its
calls, crustdata 34%, with `retry-after` headers nobody downstream could act on. Two bounded
mechanisms in `service._execute_call`, both **after the DB phase ended and before the relay** (the
pool-discipline rule holds through the wait; proven by test), both platform-tier only, neither ever a
refusal:

1. **Spacer** - `infra/upstream/limiter.py`: one call per `window_s / limit` per provider (a token
   bucket of capacity one - a burst of `limit` at t=0 is legal for a classic bucket and exactly what a
   sliding-window provider 429s). A call that would exceed the rate waits ≤ 2 s (`DEFAULT_MAX_WAIT_MS`),
   then proceeds regardless; the hold is already placed, so the org pays latency, never money. The
   limit comes from the capacity view (`view.rate_limit`: published by the sweep from
   `CapacityPolicy.rate_limit`, with the verified defaults - leadsforge 120/min, leadmagic 300/min,
   crustdata 30/min, tikhub 30/s - before the first sweep). In-process on purpose: a second replica
   doubles the effective rate, and the `rate_pressure` alert (step C) is the answer to that, not a
   shared counter on the request path.
2. **One bounded `retry-after` re-send** - on a tier-4 **429** classified `burst` with
   `retry-after ≤ 5 s` (`SMOOTHING_RETRY_MAX_S`), for a **body-less GET/HEAD only**: close the first
   response, sleep, send the identical `UpstreamRequest` once more on the same hold, settle on the
   second answer. A quota-429 (lusha "Daily", hunter "per billing period", any `retry-after` > 60 s), an
   unknown 429, a POST, or a second 429 are relayed as is. The "no retries" rule for 401/402/5xx stands.

Both are visible: `X-Treg-Smoothed: wait=<ms>` and/or `retry=1` on the response (metered exit only).
No audit column yet - `smoothed_ms` would be an ALTER on the hot `callrecord` table, a migration-class
change kept out of this behaviour PR.

## Overflow - the child cycle (plan step E; off by default)

**Overflow = the same vendor endpoint, another account of ours.** When a tier-4 call fails on treg's
own key for a treg-side reason - a balance/quota signature, a burst-429 smoothing could not absorb -
and the worker has an enabled `OverflowRoute` for the endpoint, `application.call.overflow.
maybe_overflow` runs a **child cycle** after the primary's settle released its hold:

1. Route from the in-process route view (`domain.capacity.routes_view`, Orthogonal first), skipping
   an aggregator marked unhealthy (`overflow:<name>` in the capacity view) or without a key; budget
   check against `OverflowSpend` (`overflow_daily_budget_usd`, $20/aggregator/day) on a short session.
2. **Child hold**, own id `{call_ref}:overflow`, through the ordinary `_platform_reserve` (tag
   budgets, daily cap, trial allowance apply; an empty balance is the normal 402). Never the parent's
   id: release-by-id is a conditional claim and `_finish_cancelled_call` releases both ids exactly once.
3. One aggregator run with **no DB open**: `infra.upstream.aggregators.<name>.build` wraps the
   caller's original query + buffered body; the key comes from `Settings.overflow_key_<name>` and is
   never logged. Monid's async runs are polled (bounded).
4. `parse` → vendor status + body + the real in-band cost. `_platform_settle(child,
   observed_override=cost, overflow_spend=(aggregator, cost − treg's direct price))` charges **exactly
   the aggregator's price, 0% markup**, and folds the day's spend delta into the same transaction -
   the one allowlisted overflow write (`overflow_spend_in_settle`).
5. The vendor's body goes back as the answer, `X-Treg-Served-Via: overflow:<name>`, `X-Treg-Cost-Micro`
   the child's charge, `X-Treg-Call-Id` the parent's. Two audit rows share the `call_ref`: the primary
   attempt with its real status and the child with `credential_tier="platform-overflow"`.

When the resolver already knows the account is out (the exhausted view) **and** a route is on, the
ladder skips the direct attempt entirely (`MarketplaceCall.skip_direct`): no parent hold, no vendor
402, straight to the child - the plan's tier 4b.

**An aggregator failure is data.** Its own 401/402/403 or a malformed envelope releases the child
hold and marks `overflow:<name>` unhealthy for everyone; a relayed vendor answer the signature table
reads as that vendor's own out-of-credit or quota dialect (`VENDOR_DRY`: a 402, Apollo's 422 through
Orthogonal's dry Apollo account, a period 429) releases the child hold and marks
`overflow:<name>:<provider>` only - one vendor's cap never takes the others offline. Either mark
lasts 15 minutes, and the caller gets the typed `provider_capacity` 503 with alternatives; a second aggregator is never tried on the same call. Its
stricter-schema refusal (`contract`) releases the child and lets the vendor's own answer stand.

**Shadow mode** (`TREG_OVERFLOW_MODE=shadow`): the aggregator is called, status / shape / cost logged
and the probe's cost recorded in `OverflowSpend` (treg pays, budget-bounded) - the caller still gets
the vendor's own error and is charged nothing. This is the week the plan requires before routes serve.

Never on tiers 1/2, a caller-caused 4xx, a 401, a timeout, PUT/PATCH/DELETE, a route the worker has
not enabled, or a team that opted out (`Org.platform_overflow_disabled`, `treg org overflow off`) -
checked before any aggregator is contacted, on both entry points.

## Control-header isolation

`proxy._is_dropped_request_header` strips every request header beginning with `x-treg-`,
including future control headers. `_CONTROL` lists the non-prefixed infrastructure headers.
This prevents caller metadata, runtime identity and routing controls from reaching providers;
tests include an invented prefix-matching header so the guarantee cannot regress to an enumeration.

## Asynchronous submissions on the call path

A catalog endpoint carrying an `async` descriptor resolves like any other (`resolve.py` freezes a
`settlement_basis` with `when: terminal` and the descriptor on the `MarketplaceCall`). In
`service._execute_call`, a metered 2xx from such an endpoint is **deferred**
(`application.asynctasks.defer_submission` writes the pending row and leaves the hold open) unless
`_submission_rejected` says the body is not an accepted submission (not JSON, `expect` rule failed,
no task id), in which case it settles at zero at once; a persistence failure releases the hold with
an alert. `routers/call._attach_async_descriptor` adds `X-Treg-Async` (the effective descriptor) to
the response, also on an idempotent replay, so a retried `--await` polls the task already running.
The settlement itself is the money fragment's subject.

Tier-4 calls add two checks before reserve and relay that deliberately do not apply to BYOK. First,
a body field used as a table-pricing discriminator and declared as a singleton catalog enum (for
example OpenRouter's `model`) must equal that fixed value; strict JSON parsing rejects duplicate keys
that could make validation and upstream interpretation disagree. Second, endpoints referenced as an
async descriptor's poll or fetch utility accept only task/result ids present in an
`AsyncTaskRecord` for the caller's org and provider, with the same frozen endpoint/parameter rule.
Legacy async pairs use catalog `resource_ownership` metadata and `AsyncResourceRecord` for the same
check without changing their existing settlement behavior. Formal submissions mirror their
poll/fetch ids into that table too, so removing a live descriptor reference cannot make its utility
fail open; pre-migration pending rows still authorize through their frozen descriptor.
Extended task consumers whose producer provenance is not modeled are catalogued as BYOK-only via
`platform_blocked`, rather than accepting an unverifiable task id on the shared account.
Unknown and cross-org ids receive the same 403 without contacting the provider. Fetch-mode result ids
are learned from an authorized successful poll or from the worker's terminal response. BYOK keeps its
faithful-relay semantics because those ids belong to the caller's own provider account.

An owned platform status poll with an explicit free price and zero estimate takes the
`MarketplaceCall.free_owned_poll` branch. It bypasses a new poll reservation and settlement while
buffering the response for `observe_owned_poll`, which learns fetch ownership and finalizes the
original task on terminal 2xx evidence. Missing required usage leaves the hold pending; settlement
errors preserve the provider response for cron recovery. Polls read the live provider, do not use or
populate replayable cache, and release any caller-supplied idempotency label so the next poll
can observe a changed status. Successful and failed polls retain diagnostic audit rows with
`kind=async_poll` and zero charged cost; `/calls` excludes them before pagination. The original
submission shows the shared finalizer's settlement state and result in Activity. Terminal evidence
is archived under that submission's call id, not the poll's id.
