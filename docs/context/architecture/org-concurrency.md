---
title: Optional organization concurrent-request admission
status: shipped
sources:
  - src/treg/infra/org_concurrency.py
  - src/treg/infra/kv.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - src/treg/mcp.py
  - tests/test_org_concurrency.py
  - tests/test_org_concurrency_redis.py
related:
  - architecture/proxy-model.md
  - architecture/mcp-oauth.md
  - architecture/money.md
  - ops/deploy.md
---

# Organization concurrent-request admission

`routers.call.run_call_surface` optionally admits one root request after trusted `Caller`
resolution and context creation, before `execute_call` can reserve, enter money admission or call
a provider. `/call/`, `/catalog/call/`, `/table/` and the MCP adapters using these routes share the
gate. Routing, verification, overflow and hub children call the application directly, so they do
not consume additional root slots. Authentication DB work still precedes this gate; it cannot
protect the identity lookup from saturation. This is a root-request bound, not a bound on provider
fanout or database connections. It applies to configured orgs' own-key calls as well as shared-key
calls without changing their billing behavior.

**Excluded entry points:** direct `POST /hub/run` and `/orgs/{org_id}/provider-resources` requests
do not enter `run_call_surface`; both can perform provider work outside this guard. The MCP
`resources_list` tool uses the provider-resources route and is also excluded. Arena/background
work invoking `execute_call` directly is outside the guarded roots. This setting therefore does
not cap every Org Web request or all provider work. A hub invocation through `/call/` is covered
by its one root slot, while a direct `/hub/run` invocation is not. Expanding these boundaries is
a separate change.

`org_concurrency.acquire` returns immediately on contention with HTTP 429,
`Retry-After`, and `detail.error=org_concurrency_limit`. Existing HTTP exception handling still
adds `X-Treg-Error`, `X-Treg-Call-Id` and an audit refusal. `mcp._call_impl` and
`mcp._call_media_impl` preserve the header as `retry_after` in their structured result; the
refusal body also includes integer `retry_after_s`. Retry-After is guidance, not a promise of an
available slot. There is no waiting queue or requests-per-minute quota.

Contention emits `org_concurrency rejected org_id=<trusted Caller.org_id> limit=<effective limit>
status=429`. Logging failure does not change the refusal. Process-local
[Org traffic logs](org-traffic.md) include this request in their arrival/completion counters.

## Configuration and rollback

All settings are environment variables, read at process startup. The defaults leave admission
disabled and the org map empty. Unconfigured and exempt orgs never contact Redis for this guard.
Use identical settings and the same Redis database on every Web worker/instance serving traffic.

Template only: substitute real positive integer IDs and limits after operator review; the angle
bracket placeholders below are deliberately not valid JSON values. Do not copy them verbatim.

```sh
TREG_ORG_CONCURRENCY_ENABLED=false
TREG_ORG_CONCURRENCY_LIMITS='{"<ORG_ID>":<CONCURRENT_REQUEST_LIMIT>}'
TREG_ORG_CONCURRENCY_EXEMPT_ORG_IDS='[<EXEMPT_ORG_ID>]'
TREG_ORG_CONCURRENCY_REDIS_FAILURE=<fail_open_or_fail_closed>
TREG_ORG_CONCURRENCY_LEASE_S=30
TREG_ORG_CONCURRENCY_RETRY_AFTER_S=1
# TREG_KV_URL points to the existing shared Redis database.
```

The exemptions default to `[]`. Org IDs and limits must be positive integers; limits cannot be
strings, booleans or fractions. Lease duration is 3–300 seconds, retry guidance 1–60 seconds.
Enabling without a Redis URL or an explicit failure policy fails settings validation. There is
no process-local concurrency fallback. Operators must choose one policy before enabling:

- `fail_closed`: new requests for configured, non-exempt orgs receive HTTP 503 with
  `detail.error=org_concurrency_unavailable` and retry guidance when admission cannot be confirmed.
- `fail_open`: those requests proceed without a confirmed slot during Redis failure, sacrificing
  the concurrency cap for availability. This is never selected by default.

The same explicit policy covers Redis client initialization failures as well as command failures.

Rollback sets `TREG_ORG_CONCURRENCY_ENABLED=false` and applies the normal deployment process.
Already admitted requests finish their existing renewal/cleanup lifecycle. No key deletion or DB
migration is needed. Rolling enablement, disabling, exemption or limit changes are not atomic
across processes: inconsistent settings or traffic still reaching an old/disabled worker cannot
enforce a deployment-wide cap. Lowering a limit does not evict already accepted work.

## Ownership and lifetime

`kv.RedisStore.acquire_slot` uses one atomic Lua operation and a sorted set per org, shared across
API keys and workers. Each request owns a random token with a Redis-time expiry; the script
removes expired members, checks cardinality and inserts a new owner only below the configured
limit. The key expires at its latest owner's deadline, so a shorter new lease cannot expire
another owner's slot. This namespace is separate from money's admission leases.

`Slot._renew` runs every third of a lease. `renew_slot` extends only a still-live token and never
recreates one after expiry; `release_slot` removes only its own token. Ambiguous acquisition and
cancellation join the bounded Redis attempt and attempt owner-safe cleanup. A lost release reply
may leave occupancy until its lease expires, temporarily denying otherwise admissible requests.
Each Redis operation has a one-second outer deadline and reuses the existing `kv.store()` client.

The slot remains held through application execution, `/table/` conversion, stream transmission,
upstream close and synchronous financial cleanup. `_LeasedStreamingResponse` also releases on
send errors and disconnects before body iteration. Cleanup is joined through repeated task
cancellation and Starlette cancellation scopes. Ordinary completion, exceptions and cancellation
all release the same owner. An async provider submission releases its Web slot once that response
and cleanup end; the provider's remote job does not retain a Web slot between polls.

## Lease failure limits

Expiration is recovery for an unresponsive owner, **not proof the request finished**. A process
pause, Redis outage, eviction, restart, failover losing acknowledged writes, or network partition
can let a successor acquire while old work continues. There is no provider/database fencing here,
so a strict cap cannot be guaranteed under these conditions, even with `fail_closed` for new
admissions. Renewal uncertainty logs `org_concurrency lease_uncertain` and explicitly reports
that the cap is not guaranteed. Acquire/release failures log `acquire_unavailable` /
`release_unavailable` without request contents, credentials or ownership tokens.

Accepted work and financial cleanup continue after lease loss regardless of the new-admission
failure policy; the guard neither cancels them nor alters money locks, reserve or settlement.
Retry or emergency hard-fencing policies require separate design and approval.

`test_org_concurrency` covers policy, configuration, HTTP/MCP, streaming and cleanup.
`test_org_concurrency_redis` uses disposable loopback Redis via `TREG_TEST_KV_URL`, including
independent clients/processes, contention, expiry and replacement-owner safety. The Redis-backed
CI job includes both files. No production traffic or credentials are needed.
