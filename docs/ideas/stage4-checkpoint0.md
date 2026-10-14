# Stage 4 checkpoint 0: application.call contract and split plan

Status: design draft for review. This file is a review artifact only and must not be committed.

Basis for this note:

- current `refactor/phase3-identity` HEAD `b924ced`
- `docs/REFACTOR-PLAN.md` sections 1.2, 1.6, 4, and 5
- current `src/treg/api.py` at 4,676 lines
- the shipped context fragments for API, proxy, money, auth/secrets, multi-tenancy, and landing sandbox
- `src/treg/bootstrap.py`, `proxy.py`, `injectors.py`, `ledger.py`, `oauth.py`, `health.py`
- the 31 tests under `tests/callmatrix/` and the current pool-discipline tests

Line numbers below are against `b924ced`. Code work must start only after PR #210 is merged, on a new
branch, and must re-resolve every line range against that merge result before cutting symbols. Symbol names,
not these temporary line numbers, remain the migration anchors.

## 0. Executive recommendation, frozen decisions, and amendment proposal

Split Stage 4 into two stacked PRs.

- **4a, control-plane perimeter:** move app-wide middleware ownership, billing, balance, referrals, onboarding,
  landing sandbox management, and their HTTP routes. Use the Stage 3 A/B pattern. The call-specific sandbox
  execution branch remains for 4b.
- **4b, call kernel:** freeze and implement `CallContext`, move the call route and helpers, establish the
  governance/money/connections/upstream owners, and split the use case into resolve, authorize/caps, reserve,
  relay, and settle/release stages.

This split is sound. It keeps Stripe, onboarding HTML/JSON translation, demo seeding, and public-feed state out
of the call-kernel review. It also makes the 4b diff easier to audit for the three hard constraints. PR 4b should
be based on 4a, not developed independently.

I recommend two small expansions to the proposed 4a boundary:

1. Move `/referrals` and `/referrals/code` in 4a as their own concern. They are interleaved with the billing
   block, billing invokes referral payout, and leaving them in `api.py` would preserve a control-only bridge
   through the exact area 4a is clearing. Their final router is `routers/referrals.py`, not
   `routers/billing.py`.
2. Repay the existing `resources_routes._enforce_sandbox_cap` bridge in 4a. The policy belongs to a small
   governance sandbox policy owner, while `application.onboard` sequences mint/reset/export journeys.

### 0.1 Frozen checkpoint decisions

1. **Intake is a real pre-resolve stage.** Preserve tag-budget preflight, idempotency claim, resolve, then the
   remaining authorization in their current chronological order. A literal resolve-first rewrite is rejected.
2. **Dataplane auto-top-up stays unchanged.** `billing.maybe_schedule_autotopup()` performs only an in-memory
   eligibility check and `create_task` on the request path. The derived task opens its own session for writes.
   This is an existing behavior omitted from the plan's §1.6 enumeration, not permission for a new write. The
   final architecture test enumerates it as a dataplane-derived background write and the affected context
   fragment records it. An outbox would change behavior and is outside Stage 4.
3. **DB outage semantics stay unchanged.** With an available DB, every in-process terminal path closes its hold
   before completing. With an unavailable DB or process death, the committed hold plus lazy reaper is the
   guarantee. The response must not block on finalization beyond the current retry behavior.
4. **Cancellation compensation is an approved, pre-4b fix.** A cancelled metered call releases with
   `reason=call_cancelled`. It lands in an independent small PR before 4b, not as a 4b commit. Its regression is
   an E2E cancellation of a real-client, funded metered call and must run on Postgres; SQLite-only coverage does
   not prove asyncpg cancellation behavior. This PR hardens the invariant against which every 4b commit is
   evaluated and remains independently bisectable.

### 0.2 Proposed amendment to plan section 0.6: refactor-time fix policy

This text is an explicit amendment proposal for owner approval, not an inference from the frozen plan. Small
fixes discovered during refactoring are allowed under these guards:

- never fold a fix into an A pure-move or B boundary-extraction commit; use an independent `fix:` commit with a
  regression test
- place the fix before any move touching the same region
- limit it to a bounded blast radius, no client-parsed API surface change, and a same-commit regression test
- allow at most three fix commits per PR
- after each fix lands, re-resolve every source line anchor; no later commit message may cite a pre-fix line
  number
- require an E2E regression on Postgres for a fix adjacent to concurrency or money

Money semantics, API surface changes, and concurrency architecture still require an independent PR. Every Stage
4 PR description contains an **Intentional behavior changes** section exhaustively listing each fix commit, or
explicitly states that there are none. This is the sole behavior-change list: everything not enumerated there is
required to be behavior-identical.

### 0.3 Typed failure policy

Failure types are keyed by mechanism. A separate `blame` field is assigned at one code point and has exactly
these values:

- `caller`
- `treg`
- `upstream`
- `org_connection`

Section 2.7 freezes the detailed pins. In particular, the 502 family uses stable mechanism kinds rather than
`detail` prefixes. Header enrichment is not part of the Stage 4 contract.

## 1. Scope and ownership

### 1.1 PR 4a: control-plane perimeter

#### Onboarding and landing sandbox symbols from `api.py`

| Current lines | Symbols | Destination |
|---:|---|---|
| 1001-1032 | `OnboardIn`, `onboard_demo`, `onboard_skip`, `onboard_reset` | A: `routers/onboard.py`; B: orchestration in `application/onboard` |
| 1039-1041 | `SANDBOX_HIT_NS`, `SANDBOX_RATE_MAX`, `SANDBOX_RATE_WINDOW_S` | `application/onboard` rate-limit policy |
| 1064-1070 | `_enforce_sandbox_cap` | A: shared move; B: `domain/governance/sandbox.py` with a semantic limit error |
| 1097-1112 | `demo_sandbox_mint` | A: `routers/onboard.py`; B: `application/onboard` |
| 1116-1122 | `demo_sandbox_live` | `routers/onboard.py`, calling a read-only onboard query |
| 1127-1143 | `stripe_webhook` | A: `routers/onboard.py`; B: landing-demo webhook use case under `application/onboard` |
| 1147-1153 | `landing_stripe_feed` | `routers/onboard.py`; framework-neutral feed stays behind an HTTP SSE adapter |
| 1157-1164 | `demo_sandbox_skill` | A: `routers/onboard.py`; B: `application/onboard` |
| 1168-1174 | `skill_samples` | A: `routers/onboard.py`; B: onboard sample query |
| 1178-1190 | `skill_install` | Router validates HTTP/query shape; application produces script text |
| 1193-1224 | `TeammateIn`, `onboard_seed_tool`, `onboard_accept_teammate` | A: `routers/onboard.py`; B: `application/onboard` |

The public-demo call limiter at 1045-1061 is not part of 4a. It is execution policy and moves in 4b. Likewise,
the sandbox branches at 4374-4401 and `_relay_live_demo` at 4168-4186 remain call-kernel work.

The current root modules are mixed:

- `demo.py` is control-only seed/reset behavior and can move mechanically into the `application/onboard`
  package in 4a.
- `pubfeed.py` is control-only process state used by the landing webhook/SSE journey and can move mechanically
  into `application/onboard` in 4a.
- `sandbox.py` is not wholly control-only. Mint, GC, export, and sample generation are onboard work, but
  `is_sandbox`, `is_live_tool`, `visitor_name`, and especially `synthesize` are consumed by `/call`. Do not move
  the whole module behind `application.onboard` and make `application.call` import a sibling use case. Split its
  control functions in 4a and leave compatibility exports until the call-side functions receive their 4b owner.

#### Billing, balance, and referral symbols from `api.py`

| Current lines | Symbols | Destination |
|---:|---|---|
| 1237-1291 | `org_balance` | A: `routers/billing.py` `balance_router`; B: money read query called by the router |
| 1298-1309 | `TopupIn`, `AutoTopupIn` | `routers/billing.py` |
| 1312-1327 | `_billing_org`, `_return_base` | HTTP/auth pieces stay in router; framework-neutral org gate/result inputs go to application |
| 1331-1344 | `billing_get` | A: router; B: `application.billing.get_state` |
| 1348-1371 | `billing_topup` | A: router; B: `application.billing.start_topup` |
| 1375-1406 | `billing_autotopup` | A: router; B: `application.billing.configure_autotopup` |
| 1410-1422 | `billing_history` | A: router; B: application query using money rows and Stripe document port |
| 1426-1439 | `billing_portal` | A: router; B: `application.billing.create_portal` |
| 1447-1468 | `my_referrals` | A: `routers/referrals.py`; B: referral summary/sweep use case |
| 1472-1482 | `mint_referral_code` | `routers/referrals.py`, calling the referral domain command |
| 1486-1509 | `billing_stripe_webhook` | A: router; B: signed-event application use case |

Registration order is preserved with separate attachment points, not one regrouped include:

1. `balance_router` replaces the current `org_balance` position before `org_routes.tag_controls_router`.
2. `billing_router`, `referrals_router`, and the billing webhook block are appended in their present order after
   `tag_controls_router` and before `member_management_router`.
3. `onboard_router` replaces positions 98-109 as one continuous route block.

The existing `billing.py` mixes Stripe SDK calls with application sequencing. The target is not a rename of that
mixture:

- `infra/stripe.py` owns the Stripe SDK, signature verification, and network calls.
- `application/billing.py` owns top-up, auto-top-up, portal/history composition, webhook sequencing, and its
  transaction boundaries.
- `domain/money` remains the only writer of money tables.
- `treg.billing` remains a temporary compatibility facade for existing imports and tests until a separately
  approved deletion stage. New runtime code imports the owner directly.

Moving the old module wholesale into `application.billing` may be used as an A commit, but the very next billing
boundary commit must remove the concrete Stripe SDK from the application layer. Do not leave that intermediate
shape at the end of 4a.

### 1.2 PR 4b: call kernel symbols from `api.py`

#### HTTP exit and shared call gates

| Current lines | Symbols | Destination |
|---:|---|---|
| 633-650 | `_pool_saturated` | app-wide HTTP handler owned by bootstrap; delegates call bookkeeping through the call service or a narrow callback |
| 653-678 | `_stamp_call_exit` | call-owned finalization and exit metadata in `application.call`; bootstrap's handler adapter applies HTTP headers |
| 681-691 | `_refusal_kind` | `application.call` refusal classification, called by the bootstrap adapter |
| 694-711 | `_mark_treg_own_errors` | app-wide HTTP handler owned by bootstrap; call-specific stamping is injected from `application.call` |
| 888-931 | `_tool_allowed`, `_require_tool_access`, `_project_allowed`, `_tool_usable`, `_require_tool_use` | A: `domain/governance/access.py`; B: semantic denial instead of `HTTPException` |
| 983-986 | `_now_ms` | call clock helper, preferably injected monotonic clock in `application.call` |
| 1045-1061 | `PUBLIC_DEMO_HIT_NS`, `PUBLIC_DEMO_RATE_MAX`, `PUBLIC_DEMO_RATE_WINDOW_S`, `_enforce_public_demo_ip_cap` | `domain/governance/publicdemo.py`, consumed by call authorize/caps |
| 1078-1089 | `_enforce_daily_cap` | `domain/governance/usage.py`, consumed by call and both run tiers |

`_id_out_of_range` at 627-630 is generic HTTP behavior, not call work. It moves with the app-wide handlers only
if 4a performs a bootstrap handler cleanup. It is not part of `application.call`.

#### Resolve and marketplace pricing

| Current lines | Symbols |
|---:|---|
| 2066-2189 | `_resolve_call` |
| 2210-2255 | `_catalog_endpoint_for`, `_enforce_catalog_status`, `_marketplace_secret` |
| 2259-2287 | `MarketplaceCall` |
| 2293-2345 | `_PLATFORM_PAGE_DEFAULT`, `_PLATFORM_PAGE_MAX`, `_LIMIT_PARAMS`, `_body_limit`, `_platform_estimate_micro` |
| 2355-2550 | `_usd_to_micro`, `_truthy`, `_json_object`, `_input_count`, `_credit_modifiers`, `_marketplace_pricing`, `_oauth_billed_provider`, `_billed_endpoint_match`, `_post_has_link`, `_oauth_billed_estimate` |
| 2553-2692 | `_billed_marketplace`, `_params_hash`, `_platform_bindings`, `_platform_offer`, `_capability_alternatives`, `_marketplace_no_credential` |
| 2695-2720 | `_VALID_PERCENT_ESCAPE_RE`, `_marketplace_upstream` |
| 2723-2754 | `_enforce_capability_pin` |
| 3110-3178 | `_resolve_marketplace_call` |
| 3181-3187 | `_may_have_body` |
| 3191-3242 | `catalog_endpoint_access` |

These land in the resolve and authorize modules of the `application.call` package. The HTTP endpoint
`catalog_endpoint_access` gets a thin `routers/call.py` wrapper. Catalog data remains behind a narrow read port;
the call application must not import Catalog management routes.

#### Intake, caller metadata, idempotency, and budgets

| Current lines | Symbols |
|---:|---|
| 2757-2759 | `IDEMPOTENCY_WINDOW_S`, `IDEMPOTENCY_HEADER`, `_IDEM_MAX_KEY` |
| 2767-2793 | `META_HEADER`, `_META_MAX_KEYS`, `_META_MAX_HEADER`, `_META_MAX_VALUE`, `_META_VALUE_RE`, `DEFAULT_PRIMARY_DIM`, `_MAX_TAG_VALUES`, `CallMeta`, `_NO_META` |
| 2796-2907 | `_tag_telemetry`, `_validate_tag_pair`, `_parse_call_meta`, `_primary_dim_of`, `_budget_dims_of` |
| 2910-3107 | `_idempotency_key`, `_IDEM_SCOPE_SEP`, `_scoped_idempotency_key`, `_idem_display`, `_request_fingerprint`, `_replay_idempotent`, `_release_idempotent_claim`, `_claim_idempotent`, `_store_idempotent` |
| 3246-3376 | `_effective_daily_cap`, `_enforce_trial_allowance`, `_enforce_platform_daily_cap`, `_month_start_utc`, `_resolve_tag_budget`, `_tag_budget` |
| 3388-3467 | `_enforce_tag_budgets` |

The HTTP parsing half of `_parse_call_meta` becomes a router adapter over raw header values. The policy and
budget primitives belong to `domain/governance/budgets.py`. Idempotency is application.call state with its own
short transactions. Audit telemetry construction remains application sequencing over an audit port.

#### Reserve, response evidence, relay support, and finalization

| Current lines | Symbols |
|---:|---|
| 3470-3530 | `_platform_reserve` |
| 3541-3564 | `_NOT_THE_CALLERS_FAULT`, `_platform_billable`, `_PLATFORM_BODY_MAX` |
| 3569-3616 | all error-evidence limits and regex/header constants |
| 3619-3808 | `_secret_renderings`, `_safe_secret_renderings`, `_basic_credential_parts`, `_decode_error_body`, `_caller_request_snippet`, `_redact_snippet` |
| 3812-4015 | `_brightdata_record_count`, `_observed_cost_micro` |
| 4018-4088 | `_buffer_response`, `_peek_stream_head`, `_error_response_evidence` |
| 4091-4137 | `_platform_settle` |
| 4140-4165 | `_record_first_call` |
| 4168-4186 | `_relay_live_demo` |
| 4193-4598 | `call_tool` |

The large provider-cost and evidence functions are not router logic. They land under `application.call.settle`
and `application.call.evidence`. The live-demo adapter lands beside the upstream adapter, with its sandbox-only
selection remaining application policy. `call_tool` first moves mechanically to `routers/call.py`, then becomes
a thin adapter that builds `CallInput`, invokes the use case, and wraps `UpstreamResponse`.

The following nearby symbols are explicitly outside 4b:

- capability-pin CRUD at 1534-1625 remains a control/catalog concern; only call-time pin consumption moves
- `_require_local_run` at 940-945 remains a local-run HTTP guard
- `GrantIn` and `RunReportIn` at 959-966, plus local grant/report handlers at 1667-1851, remain
  `application.run` work
- call/run activity reads at 1859-1991 remain a later audit/read-router cleanup
- `/run` and `RunIn` at 4602-4663 remain `application.run`
- `LOCAL_ORG_NAME` and `_bootstrap_single_user` at 419-473 remain bootstrap startup work because the startup
  manifest invokes them
- generic meta/providers/tool-request routes remain outside Stage 4

### 1.3 Middleware ownership

All three middleware implementations should move out of `api.py` in 4a and be owned by bootstrap:

| Current lines | Symbols | Owner |
|---:|---|---|
| 488-534 | `_LEGACY_HOSTS`, `_REDIRECT_PATHS`, `_REDIRECT_ALWAYS`, `_LegacyHostRedirectMiddleware` | `bootstrap_http.py` or `bootstrap/middleware.py` |
| 537-561 | `_SecurityHeadersMiddleware` | same |
| 564-624 | `_BODY_ENC_HEADER`, `_decode_request_body`, `_BodyDecodeMiddleware` | same |

`bootstrap.create_app` already owns their concrete registration and exact order. These are app-wide ASGI
policies, not route handlers or use cases. Keeping the implementation beside the composition root removes the
factory's dependency on `api.py` for assembly details. If converting `bootstrap.py` into a package creates more
compatibility churn than value, use `bootstrap_http.py`; `treg.bootstrap:create_app` must remain unchanged.

The app-wide exception handlers follow the same ownership rule. `_pool_saturated` and `_mark_treg_own_errors`
are registered by bootstrap at `bootstrap.py:443-445`, so their HTTP adapter implementations live beside the
composition root. `_stamp_call_exit` and `_refusal_kind` are call-specific behavior owned by
`application.call`. Bootstrap wires them into the handlers through the constructed call service or a narrow
framework-neutral callback. Neither direction requires `application.call` to import bootstrap or Starlette.

## 2. CallContext and application.call contract

### 2.1 Package and narrow ports

Recommended final package:

```text
src/treg/application/call/
  __init__.py       execute_call and public contract
  types.py          CallInput, CallContext, UpstreamResponse, failures
  intake.py         metadata, tag preflight, idempotency
  resolve.py        own-tool and marketplace resolution/pricing
  authorize.py      ACL, deny, member/public-demo caps, capability pins
  reserve.py        spend caps, reservation sequencing, money error translation
  relay.py          refresh, upstream invocation, buffering/peek policy
  settle.py         billability, observed cost, settle/release, first-call marker
  evidence.py       redaction and failure evidence
```

Narrow ports, supplied by bootstrap or small adapters:

- `SessionFactory`: opens a fresh `AsyncSession` for one bounded DB phase
- `UpstreamPort`: sends a framework-neutral request and returns `UpstreamResponse`
- `OAuthRefreshPort`: performs the external token refresh without a DB session
- `MoneyPort`: public `reserve`, `settle`, and `release` commands from `domain.money`
- `RateStorePort`: public-demo rate state
- `AuditPort` and `AnalyticsPort`: best-effort observation, never money correctness
- `Clock`: monotonic duration plus UTC boundaries
- catalog/provider/config read ports where a concrete global is currently read

Bootstrap creates the call service after `app.state.http` exists and stores it on app state. The router adapts
HTTP input and calls that service. `application.call` imports no FastAPI, Starlette, or concrete httpx client.

### 2.2 Input

The router builds this framework-neutral input:

```python
@dataclass(frozen=True)
class CallInput:
    method: str
    raw_rest: str
    raw_headers: tuple[tuple[bytes, bytes], ...]
    query_items: tuple[tuple[str, str], ...]
    raw_query: str
    body: RequestBody
    caller: CallerSnapshot
    client_ip: str

class RequestBody(Protocol):
    def stream(self) -> AsyncIterator[bytes]: ...
    async def read(self) -> bytes: ...
```

`RequestBody` is replayable after `read()`. That preserves the current idempotency and small-error-body paths
without passing a Starlette `Request` inward. `raw_rest` is derived from ASGI `raw_path` in the router so encoded
slashes remain byte-faithful. `raw_headers` and `query_items` preserve duplicates and order.

`CallerSnapshot` contains only values needed after the request session is released: membership/user/org ids,
email, role, access lists, public-demo/demo flags, org slug, balance-policy fields, and first-call state. No ORM
lazy load is allowed after an application DB phase ends.

### 2.3 Internal mutable context

`CallContext` is internal state for one invocation, not an HTTP request object:

```python
@dataclass
class CallContext:
    input: CallInput
    call_ref: str
    meta: CallMeta
    idempotency: IdempotencyClaim | None = None
    target: ResolvedTarget | None = None
    marketplace: MarketplaceCall | None = None
    credentials: tuple[CredentialSnapshot, ...] = ()
    reservation: Reservation | None = None
    finalization: FinalizationState = FinalizationState.NONE
    audit: AuditSnapshot | None = None
```

The context stores copied values, not live ORM instances. `Reservation` uses `call_ref` as the known hold id and
has an in-process state transition `NONE -> PENDING -> OPEN -> FINALIZING -> FINALIZED`. The DB hold claim remains
the cross-process authority. A duplicate settle/release sees no hold and moves no money.

Set `PENDING` before awaiting reserve. Run the reserve commit itself under cancellation shielding so its outcome
is known before compensation decides whether to release by `call_ref`. If reserve committed, run release under
shielding; if it rolled back, there is no hold to release. Shielding reduces the ordinary task-cancellation
window but cannot make a DB network failure unambiguous or survive process death. In those cases the committed
hold and reaper remain the recovery guarantee.

### 2.4 Output

The only success/replay/synthetic result shape is:

```python
@dataclass
class UpstreamResponse:
    status: int
    raw_headers: tuple[tuple[bytes, bytes], ...]
    body_stream: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]
```

Rules:

- `close` is idempotent and always closes the underlying upstream response exactly once.
- The body iterator has a `finally` that invokes `close`. The router also installs `close` as the response
  background action. Either path may fire first; idempotence makes disconnect, full consumption, and exceptions
  safe.
- Metered responses are drained before settlement and returned as a one-chunk body iterator with a no-op/already
  completed close. Buffering retains at most `_PLATFORM_BODY_MAX = 8 MiB`: an oversized body keeps the current
  truncated prefix and rewrites `content-length` to the buffered length (`api.py:4027-4041`). Unmetered responses
  retain streaming.
- Peeking at an unmetered failure wraps the original iterator and transfers the same close callback.
- The router constructs Starlette `StreamingResponse`, copies `raw_headers`, and translates semantic failures.
  No Starlette response object crosses into application or infra.

### 2.5 Chronological stages and current line ranges

The frozen chronological contract includes a small **intake** step before the five plan stages. This preserves
existing behavior.

| Stage | Current body lines | Work | DB boundary |
|---|---:|---|---|
| intake | 4199-4247 | call id, raw path, metadata validation, tag blocked/call-cap preflight, tag-budget auto-row creation, idempotency replay/claim | one or more short transactions; `_tag_budget(create=True)` at 4223 may create and commit at 3375; no hold exists; claim is committed or rolled back explicitly |
| resolve | 4249-4290, 2066-2720, 3110-3178 | own tool or catalog endpoint, provider/credential tier, upstream URL, pricing estimate, consumed params | read transaction; copied result; close before next external wait |
| authorize/caps | 4291-4298, plus capability pin inside current resolution | tool/project ACL, deny rule, member cap, public-demo cap, and call-time capability pin | bounded read/rate-state work; no money hold on refusal |
| reserve | 4300-4465, 3246-3467 | trial/platform/tag spend caps inside `_platform_reserve` at 3482-3484, evidence-safe request snapshot, sandbox terminal short circuit at 4374-4401 between authorization and credential loading, credential load, marketplace/oauth-billed classification and estimate after `_billed_marketplace` at 4427, reserve | spend gates remain in their current reserve sequence; reservation is the last pre-network mutation; application commits and closes its session |
| relay | 4466-4504, 4168-4186, current `proxy.relay` | refresh stale OAuth token, inject, SSRF check, send, stream or buffer/peek | zero checked-out DB connections during every external request and response-body read |
| settle/release | 4505-4598, 3541-4165 | release on faults, settle/release by status/cost, evidence, audit, first-call marker, idempotency store/release | each correctness-critical write has its own short application-owned transaction |

The current source intentionally runs the tag blocked/call-count gate before idempotency replay, may create a
tag-budget row during that preflight, and claims an idempotency key before resolving a tool. Do not reorder those
operations merely to obtain a visually pure resolve-first pipeline. Intake is the frozen pre-resolve stage. A
literal resolve-first implementation is rejected because it changes which error wins and whether a blocked
caller can receive a cached response.

### 2.6 Transaction and compensation contract

General rules:

1. The application use case opens every session and is the only layer that commits or rolls back.
2. Domain money functions mutate the supplied session but do not commit.
3. A session never crosses a call to Stripe, OAuth token endpoint, provider relay, DNS/network adapter, or body
   stream.
4. Reservation is committed before the first external byte. Settlement/release opens a new session.
5. Idempotency claim release is independent of money finalization and runs on every unsuccessful exit.
6. Cancellation cleanup is shielded. Ordinary cancellation must not interrupt upstream close, hold release, or
   idempotency release halfway through.

OAuth refresh currently violates rule 3: `oauth.ensure_fresh` holds `db`, calls the token endpoint, then persists.
The target sequence is:

1. copy encrypted refresh inputs/version under a short DB transaction and close it
2. call the token endpoint through `OAuthRefreshPort` with no DB connection
3. reopen a transaction and conditionally persist against the old ciphertext/version
4. copy the winning credential value and close before provider relay

Preserve the current single-flight lock and cross-process conditional-update semantics. A refresh failure still
persists `last_error`, then releases an existing hold as `call_failed_502`.

#### Failure and money table

| Failure/outcome | Hold action | Idempotency action | Upstream close |
|---|---|---|---|
| malformed metadata, auth, resolve miss, retired endpoint, pre-reserve ACL/deny/cap refusal | none | release a claim if one exists | none |
| cancellation after idempotency claim, before reserve | none | release claim under shield | none |
| insufficient balance during reserve | reserve transaction rolls back; no hold | release claim | none |
| cancellation while reserve commit outcome is uncertain | idempotent release by known `call_ref` under shield | release claim under shield | close if created |
| post-reserve SSRF refusal | release without charge using the mapped call-failure reason | release claim | none; no provider request is sent |
| OAuth refresh, injection, connect, read timeout, or upstream stream failure before a complete answer | `release(reason=call_failed_<status>)` | release claim | exactly once |
| unexpected application/infra exception after reserve | `release(reason=call_crashed)` | release claim | exactly once |
| caller/task cancellation after reserve | `release(reason=call_cancelled)` | release claim | exactly once |
| upstream 2xx under current `_platform_billable` rule | settle observed cost or estimate fallback | store metered replay after settlement | exactly once before/after buffered body |
| billable upstream 4xx | settle observed cost or estimate fallback | release the idempotency claim immediately; do not store replay; label is reusable | exactly once before/after buffered body |
| upstream 5xx | `release(reason=provider_failed_<status>)` | failed label remains reusable | exactly once |
| other non-billable upstream status | `release(reason=not_billable_<status>)` | preserve current reusable-label behavior | exactly once |
| unmetered response | no hold | drop/store according to current idempotency behavior | body iterator or background closes it |

The finalizer must consume the in-memory reservation once, but it must mark it `FINALIZED` only after the DB
hold claim has reached a terminal result. `domain.money` already has the correct cross-process primitive:
`_claim_hold` uses a conditional delete, so racing settle, release, cancellation, and reaper cannot move money
twice.

The DB-failure behavior is frozen:

- Under an available DB, every in-process terminal path must leave no open hold before it completes.
- On process death or an unavailable DB, no request handler can guarantee an immediate write. The committed
  hold is the durable recovery record and the lazy reaper eventually releases it.

Current `_platform_settle` retries one pool timeout, logs, returns the response, and leaves the hold for the
reaper. Stage 4 preserves that behavior. It must not block a response until finalization succeeds or claim that
shielding eliminates the DB/network and process-death cases.

### 2.7 Framework-neutral failures

Application and domain code raise typed semantic failures keyed by mechanism, carrying stable HTTP translation
data and a late-bound `blame: caller | treg | upstream | org_connection`. The contract has six pins:

1. **Provider responses are data.** Every provider response, including 4xx and 5xx, is faithfully forwarded and
   is never represented as an exception. Typed failures exist only when treg refuses the call or a provider does
   not produce a complete response.
2. **Blame is a field, not an inheritance root.** One mapping point assigns it. OAuth refresh failure is
   `org_connection`; idempotency waiting 409 and mismatch 422 share a mechanism but map to different blame.
3. **502 mechanisms are explicit.** Their stable kinds and compatibility mappings are defined together:

   | Kind | Blame | Ledger release reason | HTTP status | `X-Treg-Error` during B commits |
   |---|---|---|---:|---|
   | `refresh_failed` | `org_connection` | `call_failed_502` | 502 | literal `1` |
   | `injection_failed` | `treg` or `org_connection`, assigned by the single mapping point | `call_failed_502` | 502 | literal `1` |
   | `ssrf_refused` | `treg` | `call_failed_502` | 502 | literal `1` |
   | `connect_failed` | `upstream` | `call_failed_502` | 502 | literal `1` |
   | `read_timeout` | `upstream` | `call_failed_502` | 502 | literal `1` |
   | `stream_interrupted` | `upstream` | `call_failed_502` | 502 | literal `1` |

   This table is the single source used to derive ledger reason, HTTP status, and error headers so the three
   surfaces cannot drift.
4. **SSRF is its own kind.** It is a post-reserve treg refusal, always releases the hold, never charges, and
   never reaches the provider.
5. **Billability and blame remain independent.** `_platform_billable` plus
   `_NOT_THE_CALLERS_FAULT` answers who is charged; blame answers who must repair the failure. Once a provider
   has responded, no caller-visible money error may replace that response. Settlement overage is absorbed by
   ledger policy and must never become a 402 after relay.
6. **Compatibility headers remain literal.** During B commits `X-Treg-Error` remains the literal `1`.
   Enriching it with a kind or adding `X-Treg-Refused-By` changes a client-parsed API surface and requires a
   separate ruling. `refused_by` is derived from the typed failure and must remain identical for every existing
   status.

Example mechanism types include `ResolutionFailed`, `AuthorizationFailed`, `CapacityExceeded`,
`InsufficientFunds`, `IdempotencyFailed`, `GatewayFailed(kind, ...)`, and
`CallPoolSaturated(retry_after=2)`. The router maps them to the existing status, detail, JSON key order,
`Retry-After`, `X-Treg-Call-Id`, and `X-Treg-Cost-Micro`. Domain packages do not import `HTTPException`.

## 3. Section 5 destination-map reconciliation

### 3.1 `proxy.py -> infra/upstream`

Current mismatch:

- `proxy.relay` accepts FastAPI `Request` and returns Starlette `StreamingResponse`.
- It imports concrete `httpx`, `crypto`, models, injectors, settings, and lazily imports root `health`.
- Upstream close is hidden in a Starlette `BackgroundTask` rather than exposed in the planned result.

Target:

- `infra/upstream/relay.py` accepts the raw request DTO and credential snapshots, uses the shared httpx client,
  and returns `UpstreamResponse`.
- HTTP adaptation and Starlette wrapping live in `routers/call.py`.
- The SSRF dependency points to `infra/upstream/ssrf.py`, not root `health`.
- `treg.proxy` may remain a direct re-export facade during compatibility, but no new runtime owner imports it.

This is an A pure move followed by a B contract conversion. Do not combine the framework removal with the file
move.

### 3.2 `injectors.py -> infra/upstream`

The code is already framework-neutral and is suitable for a byte-level move to
`infra/upstream/injectors.py`. It is consumed by relay, health probes, sandbox synthesis, resource validation,
and `localrun.py`.

The destination map omits one important packaging constraint: `localrun.py` is a lightweight CLI module and
imports `_token_from_json` at module import time. Therefore `infra.upstream.__init__` must not eagerly import the
relay, FastAPI, SQLAlchemy, SQLModel, or server models. `httpx` itself is an allowed base dependency. Either
`localrun.py` imports the leaf `infra.upstream.injectors` module directly, or the existing `treg.injectors`
facade remains lightweight. The runtime light-import subprocess test and import-linter contract must prove this.

### 3.3 `ledger.py -> domain/money`

The map is directionally correct. Current mismatches are:

- the file is still at the root
- public operations commit and sometimes roll back internally
- root billing, referrals, signup, org reads, call, and tests import the old path

Recommendation: move `ledger.py` in 4b, but as a dedicated early commit before call-stage extraction. Use a
`domain/money` package and preserve every money symbol byte-for-byte in the A commit, with only module/import
adjustments and a thin `treg.ledger` compatibility facade. Update the import-linter money contract to target the
real owner.

Do not combine this move with removing commits. The 4b transaction B commit converts the call-facing reserve,
settle, and release path to application-owned transactions and proves balance/blocks/holds/ledger invariants
under sequential, concurrent, failed, and cancelled calls. This scope includes `reap_stale_holds`, whose only
call site is inside `ledger.reserve` at line 227. Lazy reap retains its durable transaction boundaries before the
reservation balance gate: every reaped release commits independently, both from the other reaped releases and
from the new reserve outcome, so a later 402 and the rollback at line 240 cannot undo stale-hold refunds. The
application-owned reserve orchestration therefore gives each stale release its own short committed phase, then
runs the new reservation in a separate transaction.

Grant and top-up transaction conversion, including the required signup, referrals, and billing caller changes,
is named follow-up debt `money-funding-transactions`. Repay it after 4b and before Stage 5; do not broaden the 4b
architecture test to pretend those callers have already moved. The 4a billing boundary prepares that later
repayment.

Money discipline remains absolute: all symbols in the A move are byte-identical, `domain.money` is the only
writer of `CreditBlock`, `Hold`, `LedgerEntry`, `TagSpend`, and money-owned `Org` fields, and neither application
nor routers issue direct money-table updates.

### 3.4 `oauth.py` refresh -> domain/connections

Current mismatch:

- no `domain/connections` package exists
- `ensure_fresh` combines refresh rules, concrete httpx I/O, row refresh, conditional persistence, commits, and
  in-process locking
- call, connection health, connect journeys, and server/local run all consume it

Target split:

- pure expiry/refreshability and refresh-state transition rules in `domain/connections/refresh.py`
- conditional secret persistence command in the same domain owner, without commit
- external token endpoint exchange behind `OAuthRefreshPort`, implemented in infra
- sequencing in `application.call`, `application.connect`, and later `application.run`

The section 5 map omits the concrete token-endpoint adapter destination and the run/health consumers. Record
those in context updates when code ships. Keep `treg.oauth` compatibility exports because tests and lazy
`localrun` imports use that path.

### 3.5 `health.py` SSRF portion -> upstream

`safe_webhook_url` at 28-53 and `host_is_public` at 56-73 move byte-identically to
`infra/upstream/ssrf.py`. The rest of `health.py` is credential-health behavior and does not move with them.

The map's "SSRF check -> upstream" destination is correct, but its caller description is incomplete. These
guards are also used by resource registration, signup alert URLs, OAuth client metadata validation, and
connection health. Compatibility re-exports from `health.py` avoid a broad one-commit monkeypatch break, while
new owners import `infra.upstream.ssrf` directly.

Keep the current synchronous DNS semantics during the pure move. Any later nonblocking resolver or IP-pinning
change is a separate security change, not Stage 4 cleanup.

### 3.6 Additional map omissions found

- `sandbox.py` is marked control in section 5, but `/call` uses it in the dataplane for sandbox detection,
  synthesis, and the live-wire fingerprint. It must be split by workload rather than moved wholesale.
- `billing.maybe_schedule_autotopup` is invoked by the dataplane reserve path. The request path performs an
  in-memory check and schedules a derived background task; that task performs Stripe and DB work through its own
  session. This existing behavior is a §1.6 enumeration omission and remains unchanged.
- Ratestore hit accounting writes for public-demo and sandbox limits at 1055-1058 are reached from 4297-4298 and
  4381-4382. These existing dataplane-derived writes remain unchanged.
- `_record_first_call` at 4159-4161 calls `adsconv.queue`, which inserts an `AdConversion` outbox row. This
  existing dataplane-derived write remains unchanged.
- `injectors.py` has a light-CLI consumer, so the infra package must preserve a light leaf import.
- OAuth refresh has call, connect, health, local-run, and server-run consumers, not only call/connect.

The final architecture allowlist test explicitly names auto-top-up, both ratestore hit paths, and the
`AdConversion` outbox write. Their current-state context fragments record them when implementation lands. They
do not authorize additional dataplane writes. Do not edit the frozen plan during this checkpoint; the approved
discrepancies belong in current-state fragments or a separately requested plan amendment.

## 4. Stage 3 debt repayment

### 4.1 `org_routes` eight transitional bindings

The eight current bindings are split between 1516-1517 and 3380-3385:

```text
org_routes._META_MAX_KEYS
org_routes._validate_tag_pair
org_routes._primary_dim_of
org_routes._budget_dims_of
org_routes._effective_daily_cap
org_routes._tag_budget
org_routes.PUBLIC_DEMO_RATE_MAX
org_routes.PUBLIC_DEMO_RATE_WINDOW_S
```

The first six assignments are at 3380-3385; the two public-demo constant bindings are at 1516-1517. Repay all
eight in the first governance-boundary series of 4b. The first six land in
`domain/governance/budgets.py`. `routers/orgs.py` and `application.call.authorize/reserve` import that owner
directly. The module owns budget dimensions, validation, effective caps, and budget-row reads/mutations because
section 1.3 assigns usage limits and budgets to governance. It must not import routers, application, or `api.py`.

`PUBLIC_DEMO_RATE_MAX` and `PUBLIC_DEMO_RATE_WINDOW_S` land with the underlying `PUBLIC_DEMO_*` constants and
limiter in `domain/governance/publicdemo.py`. Both the org control view and call authorization import that owner;
the call-side ratestore hit remains an explicitly allowed dataplane-derived write.

An A commit moves the existing symbols byte-identically. A B commit replaces HTTP exceptions with semantic
governance errors and makes commit ownership explicit. Remove the eight assignments only when both consumers use
the owner. Add or extend the governance import-linter contract if needed.

### 4.2 `resources_routes` ACL bridges

Current bindings at 1632-1633:

```text
resources_routes._tool_usable
resources_routes._require_tool_use
```

Repay them in the same early 4b governance-boundary series, before moving `_resolve_call`. Move the complete ACL
family `_tool_allowed`, `_require_tool_access`, `_project_allowed`, `_tool_usable`, `_require_tool_use` to
`domain/governance/access.py`. Resources, application.call, and both run paths import it directly. A B commit
introduces a semantic access error and leaves HTTP translation at each interface.

There is one related bridge not named in the task but already marked for Stage 4 at 1634:
`resources_routes._enforce_sandbox_cap`. Repay it in 4a to `domain/governance/sandbox.py`; otherwise the stated
4a onboard extraction is incomplete.

## 5. Commit sequence and verification

Every A commit is byte-level movement. Existing decorators, comments, docstrings, variable names, folding, and
function bodies remain identical. New module docstrings/imports and unavoidable relative-path anchors are the
only candidate exceptions and require advance review. Every B commit is a behavior-equivalent boundary
extraction with no new business rule.

### 5.1 PR 4a proposed commits

1. `refactor(bootstrap): move app-wide HTTP middleware`
   - A-only pure move of the three middleware families.
   - Verify four snapshots byte-identical, middleware unit tests, legacy-host E2E, body encoding E2E, full
     SQLite, drift.
2. `refactor(onboard): move onboarding routes`
   - A move of 1001-1032 and 1193-1224 into `routers/onboard.py`, preserving the original attachment point.
   - Verify symbol text, decorators, monkeypatch targets, onboarding E2E, four snapshots, full SQLite.
3. `refactor(onboard): extract onboarding application journeys`
   - B thin-router extraction, application-owned sessions/commits.
   - Verify seed, reuse, skip, reset, teammate acceptance, failure text/status, SQLite plus PG subset.
4. `refactor(onboard): move landing sandbox routes`
   - A move of sandbox mint/live/webhook/feed/export/sample/install routes and rate constants.
   - Verify symbol text, route order, webhook signature/replay tests, sandbox E2E, snapshots.
5. `refactor(onboard): move sandbox management primitives`
   - A-only byte move of control-side `demo.py`, `pubfeed.py`, and sandbox management symbols behind compatibility
     facades. Call-side sandbox symbols remain at their current owner until 4b.
   - Verify every symbol byte-identical, old/new identity, live-wire call tests, and light imports.
6. `refactor(onboard): extract sandbox management journeys`
   - B application boundary and typed errors; repay the sandbox-cap bridge without mixing its mechanical move.
   - Verify sandbox never-network tests, cap/TTL/rate tests, live-wire tests, light-import contract.
7. `refactor(billing): move balance and billing routes`
   - A move with separate original-position routers.
   - Verify all moved symbols byte-identical, Stripe webhook route order, monkeypatch target scan, snapshots.
8. `refactor(billing): move billing orchestration`
   - A move of current root billing symbols behind compatibility facade. No transaction or SDK behavior change.
   - Verify symbol text, old/new import identity, billing suite, referrals suite, ledger suite.
9. `refactor(stripe): isolate the Stripe adapter`
   - B narrow infra port and application-owned orchestration. No SDK call, error, idempotency, or commit order
     changes.
   - Verify checkout, portal, documents, signed webhook, redelivery, auto-top-up concurrency and failure tests.
10. `refactor(billing): extract billing application journeys`
   - B thin routers, typed errors, session ownership, analytics/referral sequencing.
   - Verify full billing/referral journeys, PG webhook/idempotency subset, four snapshots, full SQLite, lint.
11. `refactor(referrals): move referral routes and domain primitives`
    - A-only route and domain move, preserving compatibility exports and original attachment points.
    - Verify byte identity, referral reward hold/sweep, top-up interaction, and import contracts.
12. `refactor(referrals): extract referral application journeys`
    - B thin-router and application-owned summary/sweep sequencing.
    - Verify referral payout timing, redelivery, billing interaction, SQLite and PG journeys.

If review prefers fewer commits, combine only independent A moves whose symbols share one destination. Do not
combine an A move with a transaction, external-I/O, or error-translation B change.

### 5.2 PR 4b proposed commits

The approved cancellation fix is not in this list. Its independent E2E-on-Postgres PR must merge before 4b is
cut, and all line anchors below are re-resolved afterward.

1. `test(call): freeze the application call contract`
   - Add DTO/port contract tests and failure/finalization tables before moving behavior.
   - Verify no route or snapshot change.
2. `refactor(governance): move call access and budget policies`
   - A byte move for the Stage 3 bridge families, with all public-demo policy constants and limiter placed in
     `domain/governance/publicdemo.py`.
   - Verify AST/text equality, bridge identity until cutover, governance contracts, resource/run/call ACL tests.
3. `refactor(governance): expose framework-neutral call policies`
   - B semantic errors and commit ownership; delete the eight org and two resource ACL assignments. The sandbox
     bridge was repaid in 4a, bringing the Stage 3 debt total to eleven.
4. `refactor(money): move the ledger into domain money`
   - Dedicated A byte-level move with old-path facade and updated import-linter contract.
   - Verify every ledger symbol text, old/new object identity, all money/referral/billing/call tests, PG races.
5. `refactor(call): move the call HTTP surface`
   - A move of `call_tool`, `catalog_endpoint_access`, and their attachment block to `routers/call.py`.
     Bootstrap owns the app-wide handlers and wires call-specific exit classification from `application.call`.
     Temporary collaborators may be injected from owners, never imported from `api.py`.
   - Verify moved text/decorators, all four snapshots, `treg.api` re-export identity, monkeypatch scan.
6. `refactor(call): move intake and idempotency helpers`
   - A-only byte move, preserving tag-row creation, claim order, and compatibility exports.
   - Verify helper text/identity, malformed metadata precedence, replay/claim races, and snapshots.
7. `refactor(call): extract intake and idempotency`
   - B use-case extraction with bounded application-owned transactions.
   - Verify malformed metadata precedence, replay/claim races, label release on every pre-relay failure.
8. `refactor(call): move target resolution helpers`
   - A-only byte move of resolution, marketplace pricing, and capability-pin consumption helpers.
   - Verify text/identity, encoded paths, pricing fixtures, and compatibility imports.
9. `refactor(call): extract target resolution`
   - B framework-neutral result and error translation.
   - Verify own-tool ambiguity, catalog fallthrough, encoded path, provider tier selection, access preflight.
10. `refactor(call): move authorization helpers`
   - A-only byte move for ACL, deny, member/public-demo gates. Spend caps remain reserve helpers.
   - Verify text/identity, original gate order, public-demo ratestore writes, and snapshots.
11. `refactor(call): extract authorization`
   - B typed policy boundary for ACL, deny, member/public-demo gates and pin consumption.
   - Verify refusal status/detail/audit, no upstream hit, no hold, and preserved gate order.
12. `refactor(call): move reservation and spend-cap helpers`
   - A-only byte move of `_platform_reserve`, trial/platform/tag spend gates, and their call-facing collaborators.
   - Verify helper text/identity, 402 payloads, cap order, and auto-top-up scheduling.
13. `refactor(call): make reservation application-owned`
   - B transaction change limited to call reserve. The application commits reserve and closes the session before
     external work. Each lazy stale-hold release remains an independent committed phase before the balance gate;
     grant/top-up transaction conversion remains the named debt in section 3.3.
   - Run pool-discipline and the full callmatrix on SQLite and Postgres. Retarget every moved `treg.api`
     monkeypatch or assert old/new collaborator identity so the pool tests cannot guard an unused object.
   - Also verify exact concurrent balance gate, rollback behavior, 402 payload, and unchanged auto-top-up task.
     Seed an expired hold, force the following reserve to return 402, and assert the stale-hold refund remains
     committed while the failed new reservation leaves no hold.
14. `refactor(upstream): move relay, injectors, and SSRF guards`
   - A pure moves with compatibility facades and lightweight injector leaf.
   - Verify byte equality, relay fidelity suite, light CLI import test, SSRF tests, old/new identity.
15. `refactor(connections): move OAuth refresh rules`
    - A-only byte move with old-path facade and all existing consumers on the same objects.
    - Verify text/identity, refresh races/failures, and health/connect/local-run imports.
16. `refactor(connections): separate OAuth refresh I/O from persistence`
    - B port extraction. Preserve single-flight, CAS, `last_error`, and all refresh consumers.
    - Verify refresh races/failures, health/connect/local-run callers, and no DB checkout during token HTTP.
17. `refactor(call): return framework-neutral upstream responses`
    - B conversion of infra relay and router wrapping. Preserve raw header order/duplicates, cookies, HEAD/bodyless
      content length, streaming, CSP/nosniff, body framing, and close semantics.
    - Run pool-discipline and the full callmatrix on SQLite and Postgres. Retarget the moved `treg.api` relay
      monkeypatches or assert old/new object identity before accepting green tests.
18. `refactor(call): move settlement and evidence helpers`
    - A-only byte move of observed cost, billability, buffering, evidence, and finalization helpers.
    - Verify text/identity, 8 MiB truncation and content-length rewrite, evidence redaction, and snapshots.
19. `refactor(call): make settlement application-owned`
    - B one-owner finalizer and application-owned settle/release commits. Cancellation behavior is already frozen
      by the prerequisite fix PR and is not changed here.
    - Run pool-discipline and the full callmatrix on SQLite and Postgres. Retarget settlement monkeypatches from
      `treg.api` or prove compatibility exports are the same objects.
    - Also verify every billability class, timeout/connect/stream errors, cancellation, double-finalize race,
      DB-outage response behavior, and no holds while the DB is available.
20. `refactor(call): complete the staged use case`
    - B thin router and `CallContext` orchestration, delete compatibility bindings, relocate first-call/audit logic.
    - Verify 31 callmatrix tests unchanged, pool discipline, full SQLite, targeted PG, snapshots, lint, drift.
21. `test(architecture): enforce the call runtime boundary`
    - Activate application/domain/infra import contracts, startup-manifest separation, and a dataplane write
      allowlist that explicitly includes the auto-top-up task, public-demo/sandbox ratestore hits, and
      `_record_first_call` AdConversion outbox write.
    - The domain-money no-commit architecture test covers only call reserve/settle/release, including reserve's
      lazy stale-hold reap. Grant/top-up and the affected signup/referrals/billing transaction conversion remain
      `money-funding-transactions`, due after 4b and before Stage 5.
    - Verify mutation tests make each contract fail when a forbidden edge/write/commit is injected.

For every commit:

- use `uv run --frozen`; never touch `uv.lock`
- four JSON snapshots must be byte-identical and must not be regenerated to pass
- use a checkout-private `TMPDIR` for the full SQLite suite
- run `lint-imports` and `drift.sh`
- update current-state context fragments in the same committed change, with no change narrative
- keep docs/ideas and REFACTOR-PLAN files untracked

## 6. Stop-line conditions

Stop and ask for a ruling if any of these occurs:

1. **Intake order.** Stop if a stage split would move tag blocking, call caps, metadata validation, tag-row
   creation, or idempotency claim/replay relative to resolution. Intake-first is already frozen; literal
   resolve-first is not an available implementation choice.
2. **Dataplane-derived writes.** Auto-top-up remains an in-memory check plus background task with its own session.
   Ratestore public-demo/sandbox hits and first-call AdConversion outbox insertion also remain. Stop if extracting
   them would change scheduling, persistence, request latency, or add any write not explicitly enumerated.
3. **DB-outage finalization.** With an available DB, terminal paths close holds before completion. With an
   unavailable DB or process death, a committed hold plus reaper is the guarantee and the response keeps current
   nonblocking finalization behavior. Stop if an extraction would block the response or weaken reaper recovery.
4. **Cancellation prerequisite.** The independent cancellation-fix PR must merge and pass the funded metered
   cancellation E2E on Postgres before 4b starts. Stop if any 4b commit changes `call_cancelled` behavior or
   invalidates that test.
5. **Capability pins.** Keep capability-pin CRUD in `api.py` for the catalog stage, per the Stage 3 ruling.
   Move only `_enforce_capability_pin` as a call-time consumer. Stop if avoiding a reverse import appears to
   require moving CRUD or defining a new catalog write owner now.
6. **Sandbox ownership.** Do not put call synthesis/live-wire selection behind `application.onboard`, and do not
   label the whole sandbox module control-only. Stop if a pure move cannot split control and execution symbols
   without changing behavior.
7. **OAuth refresh ordering.** Preserve reserve-before-refresh, refresh failure release, process lock, CAS write,
   and `last_error` timing. Stop if zero-DB-during-network requires changing which credential wins a race.
8. **Ledger transaction conversion.** 4b changes transaction ownership only for call reserve/settle/release.
   Reserve includes lazy stale-hold reap; every stale release must commit independently before the balance gate
   so a later 402 cannot roll back any refund. Stop if an extraction combines those transactions or requires
   silently converting grant/top-up or their signup/referrals/billing callers. That work belongs to
   `money-funding-transactions` after 4b and before Stage 5.
9. **Settlement versus streaming.** Metered responses remain buffered and unmetered responses remain streamed.
   Do not stream metered bodies before settlement or buffer successful unmetered bodies.
10. **Upstream close on disconnect.** Stop if the framework adapter cannot prove exactly-once `close` for full
    reads, partial reads, exceptions, and cancellation.
11. **Dataplane write allowlist.** Any observed write outside reserve/settle/release, idempotency, OAuth refresh,
    audit/telemetry, tag-budget accounting, approved capacity marks, public-demo/sandbox ratestore hits,
    `_record_first_call` through the AdConversion outbox, and the approved auto-top-up background task stops the
    move.
12. **Role manifests.** `/run` remains excluded from dataplane; no route regrouping may change role route keys,
    startup checks, or background tasks.
13. **Framework-neutral boundary.** Stop if `application.call` or `infra.upstream` needs FastAPI/Starlette types,
    or if a router starts owning money, retry, compensation, or pricing decisions.
14. **Light CLI.** Stop if moving injectors/oauth compatibility paths makes importing any lightweight CLI module
    load Pydantic, FastAPI, SQLAlchemy, SQLModel, Alembic, or crypto implementations. `httpx` remains allowed.
15. **Compatibility surface.** Stop on any required route key/function-name change, `treg.api:app` change,
    snapshot change, monkeypatch target that cannot be redirected safely, or old public import deletion.
16. **Refactor-time fixes.** Stop if a discovered issue exceeds the section 0.2 guards, would become a fourth fix
    commit in one PR, or touches concurrency/money without an independent PR and a Postgres E2E regression.
    Re-resolve all line anchors after any approved fix before continuing movement.

## 7. Exit criteria and proof plan

### 7.1 The 31 callmatrix tests remain unchanged

Current inventory is 30 matrix cases in `test_matrix.py` plus the signup-to-topup recovery journey in
`test_journey.py`. They already prove:

- own-tool GET/POST relay and platform/own-credential selection
- balance, member cap, deny, ACL, public-demo refusal
- idempotent replay, collision, mismatch, and failure release
- provider 4xx/5xx billability, read/connect/stream failures
- observed/malformed/compressed cost handling
- duplicate query order, encoded slash, cookie scrubbing
- concurrent balance competition and typed pool saturation
- response, ledger, audit, upstream hit, and empty-hold books for completed calls

Keep all 31 test functions and expected values unchanged. Run them on SQLite and Postgres. Adding new tests outside
the matrix is allowed; do not rewrite the matrix to match a refactor.

### 7.2 Zero DB connections during upstream flight

Existing coverage is useful but insufficient:

- `test_call_pool_discipline` samples `pool.checkedout()` when `relay` is entered for metered and own-key calls.
- the 20-call barrier test proves the current pool does not deadlock.
- `tests/test_call_pool_discipline.py` currently patches collaborators through `treg.api` at lines 65, 129,
  and 151. After each relevant move, retarget those patches or assert the compatibility export is the same
  object. A green test aimed at an unused old object is not evidence.

Missing proof:

1. A PG test with a delayed upstream and pool instrumentation that records every checkout/checkin event between
   `relay_started` and `relay_finished`, including response-body draining, and asserts the count remains zero.
2. The same test for OAuth refresh HTTP, because the current implementation performs that network request while
   holding its session.
3. A single-slot PG pool test with N concurrent metered calls and a gated provider, asserting all complete at
   provider speed and every hold closes.
4. An unmetered streaming test that keeps the body open while a separate DB request succeeds through the only
   pool slot.

The test must inspect the engine actually bound to the application call service, restore the bind in `finally`,
and dispose the temporary engine. A mutation that removes the pre-relay commit or reintroduces DB-backed refresh
during network must fail it.

### 7.3 No dangling holds and exactly-once finalization

Existing coverage is useful but incomplete:

- every `assert_outcome` checks `holds == []`
- timeout, connect error, metered stream interruption, provider failures, reported cost, and concurrency are
  covered
- ledger `_claim_hold` already makes settle/release cross-process idempotent

Add these tests:

1. **Cancellation E2E:** fund a real metered call, block the provider after reserve, cancel the client task, then
   assert the upstream response/client is closed, the balance is restored, zero holds remain, exactly one release
   entry exists, its reason is `call_cancelled`, and the idempotency label is reusable. This uses a real client
   against Postgres and lands in the independent pre-4b fix PR.
2. **Cancellation at reserve commit:** gate the commit, cancel at the uncertain boundary, and assert release is
   safe whether the reserve transaction committed or rolled back. This also lands in the independent pre-4b fix
   PR and must exercise asyncpg, not only aiosqlite.
3. **Double-finalizer adversarial test:** race success settlement against cancellation/release for one call id.
   Assert one terminal ledger entry, one balance effect, and no `TagSpend` disagreement.
4. **Unexpected exception after reserve:** inject a non-HTTP exception in refresh, relay, buffer, and evidence
   preparation, and assert immediate release plus upstream close.
5. **Close contract:** count `close` calls for successful streaming completion, partial consumer disconnect,
   metered buffering, timeout while reading, and cancellation. Every case is exactly one.
6. **Settlement pool retry/outage:** preserve the existing one-time pool retry test. For persistent DB failure,
   assert the response retains current nonblocking behavior and the committed hold remains durable for the
   reaper.
7. **Architecture transaction test:** reject domain-owned commit/rollback only for the call-facing
   reserve/settle/release path and verify each application phase closes its session before invoking an external
   port. Treat reserve's lazy reap as in-scope, assert every stale release has an independent application-owned
   commit, and prove a later 402 rollback cannot undo any reaped release. Do not assert that grant/top-up and
   their signup/referrals/billing callers have already converted.

The final Stage 4 live check remains login, connection, call, charge, and top-up. For the call leg, use a real
streaming upstream and inspect the DB pool during flight. For charge/top-up, verify the call's hold reaches one
terminal ledger action and that the control-plane top-up path still credits exactly once.

## 8. Review checklist

The revised checkpoint freezes or requests owner approval for these implementation gates:

- 4a/4b split, with referrals and sandbox-cap debt in 4a
- intake as the pre-resolve stage; no literal resolve-first rewrite
- spend caps inside reserve, while authorize owns ACL/deny/member/public-demo and call-time pin policy
- provider responses as faithfully relayed data; mechanism-keyed failures with a single late-bound `blame`
- stable 502 kind/release/status/header mapping, SSRF as a post-reserve nonbillable refusal, and no 402 after a
  provider response
- metered replay only for 2xx; charged 4xx remains immediately reusable and is not added to replay
- persistent DB failure retaining the current response behavior and durable hold/reaper guarantee
- the independent pre-4b cancellation-fix PR using `reason=call_cancelled`, with real-client Postgres E2E proof
- dataplane-derived write enumeration: auto-top-up task, public-demo/sandbox ratestore hits, and first-call
  AdConversion outbox insertion
- eleven Stage 3 bridge debts: eight org bindings, two resource ACL bindings, and the sandbox-cap bridge
- app-wide middleware and `_pool_saturated`/`_mark_treg_own_errors` handlers under bootstrap, with call-specific
  `_stamp_call_exit`/`_refusal_kind` supplied by the call owner
- ledger dedicated A move in 4b; no-commit enforcement limited to call reserve/settle/release; named
  `money-funding-transactions` repayment after 4b and before Stage 5
- strict A then B commit separation, dual-engine pool/callmatrix gates and live monkeypatch targets at the three
  riskiest boundaries
- sandbox module split, light injector packaging, capability-pin CRUD remaining for the catalog stage, and the
  explicit excluded local-run/startup symbols
- owner approval of the proposed plan §0.6 amendment in section 0.2: no more than three fix commits per PR,
  exhaustive PR behavior-change list, post-fix line-anchor refresh, and PG E2E for concurrency/money-adjacent
  fixes

No code, tracked docs, lockfile, or commit work begins until this checklist is reviewed and PR #210 is merged.
