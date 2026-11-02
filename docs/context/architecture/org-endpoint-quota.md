---
title: Exact organization and endpoint call allowances
status: shipped
sources:
  - src/treg/infra/org_endpoint_quota.py
  - src/treg/application/call/service.py
  - src/treg/application/call/types.py
  - src/treg/application/call/route.py
  - src/treg/application/call/async_bridge.py
  - src/treg/application/call/overflow.py
  - src/treg/application/arena.py
  - src/treg/infra/upstream/relay.py
  - src/treg/bootstrap.py
  - src/treg/config.py
  - src/treg/analytics.py
  - tests/test_org_endpoint_quota_redis.py
  - tests/test_org_endpoint_quota_call.py
  - tests/test_org_endpoint_quota_hub.py
  - tests/test_org_endpoint_quota_async.py
  - tests/test_org_endpoint_quota_best_effort.py
  - tests/test_org_endpoint_quota_breaker.py
  - tests/test_org_endpoint_quota_telemetry.py
  - tests/test_org_endpoint_quota_rollup.py
  - tests/test_org_endpoint_quota_rate_observe.py
  - tests/test_org_endpoint_quota_local_rejections.py
  - tests/test_org_endpoint_quota_route.py
  - tests/test_relay_quota_admission.py
  - tests/test_call_architecture.py
related:
  - architecture/proxy-model.md
  - architecture/org-flow-control.md
  - architecture/money.md
---

# Exact organization and endpoint call allowances

`service._admit_org_endpoint` enforces an operator-configured, one-time call allowance for an
exact trusted Org id and canonical catalog endpoint id, on every credential tier. There is no
wildcard, replenishment or time window, and it counts admitted logical calls, not money or
provider credits. Changing a limit preserves `used`; reset is explicit. `limit=0` refuses new
calls once Redis returns that rule; calls already admitted continue. Clearing a rule keeps a
disabled tombstone and its count. Unknown Redis state never blocks a call (even a previously
blocked target), but a confirmed exhausted rule does: this is temporary protection, not a durable
spending cap.

## Where admission happens

The shared call application covers REST, catalog call, table and MCP, plus routed children and hub
steps entering `execute_call`. Stored idempotent responses return before admission.

- Ordinary calls admit after authorization, canonical resolution, resource validation and money
  admission. A cache hit consumes one call immediately before serving. A miss admits inside relay
  after JSON, credential injection and SSRF checks, immediately before sending; overflow first
  checks its route, budget, child reservation and request preparation.
- On the direct path the shared provider-rate window is asked first: a rate refusal spends no
  quota, and a quota refusal gives the window member back and is not counted as a provider attempt.
- Balance, cap, resource and other pre-dispatch refusals consume no quota. A quota refusal
  releases any money hold and unused overflow budget.
- Public-resource authorization may read provider metadata before admission (no treg hold or
  charge); the allowance gates the target's execution, not all preliminary provider I/O.
- Accepted calls are never refunded to the quota after upstream failure, cancellation or a lost
  Redis reply.

Routed parents defer their allowance until a child can run or serve a cache hit. One Lua call
checks all unadmitted parent targets plus the child: any confirmed exhausted rule rejects the batch
without incrementing; otherwise valid enabled rules increment together, and unknown fields neither
increment nor mask a known refusal elsewhere. A local child rejection cannot spend the parent's
allowance. Retries and overflow stay one logical call: `quota_admissions` in the call context
deduplicates admitted targets, including targets let through on Redis uncertainty. Internal polls
of a routed or arena submission mark only their exact poll target as admitted and do no quota I/O;
caller-initiated polls are ordinary calls. This exemption has no caller-controlled flag.

Hub recipe ids are not quota targets. Catalog child calls are guarded, and the hub keeps its own
contract: a child refusal becomes 424 or an `allow_fail` skip, other completed steps and the maker
price are charged, and the refused endpoint is neither executed nor charged.

An exhausted rule returns HTTP 429 `detail.error=org_endpoint_quota_exceeded` with zero cost. It
is not a supplier 429, carries no refill time and never triggers overflow. A routed child's
exhausted allowance skips that candidate without spending the provider-error retry budget; a
parent's stops the whole route and releases deferred holds. A refusal in optional verification is
reported as `unchecked`, keeping the find and its charge. A route upgrade is a new logical
invocation and checks again; its refusal keeps the first answer uncharged. Once an async
submission has a task id, poll errors keep the pending response and worker settlement ownership.

## Store contract

Disabled by default. `TREG_ORG_ENDPOINT_QUOTA_ENABLED=true` requires an explicit
`TREG_ORG_ENDPOINT_QUOTA_REDIS_URL`; it does not inherit `TREG_KV_URL` and has no process-local
fallback. Every enforcing worker and the private management API must use the same Redis database.
Only the private admin, on full machine authentication, manages allowances; ordinary business keys
cannot.

The private admin writes and this runtime reads; the layout is a wire contract between them. One
hash without TTL, `treg:org-endpoint-quota:v1`, holds `__schema=1` and fields
`rule:<org_id>:<endpoint_id>`, each exactly:

```json
{"version":1,"limit":100,"used":0,"enabled":true}
```

All numbers are integers at most 2147483647; `version` starts at one, the others at zero. Rule and
count share one field. The admission script validates namespace, absence of expiry and the complete
rule, then checks `used < limit` and increments atomically. A healthy namespace with no rule allows
the call. A missing namespace or schema, malformed state or Redis failure is unknown: unless another
target has a confirmed exhausted rule, the call is allowed without repairing state or assuming zero.
The runtime never creates, expires or deletes rules.

Management writes take a required unique operation id, expected version and reason, and write the
rule plus an `op:<operation_id>` audit and idempotency record in one HSET inside Lua. Consumption
changes `used` without bumping `version`, so a concurrent limit edit preserves the count. Retrying
an operation returns its saved result; reusing an id for different content or a stale version
conflicts. A Redis outage or uncertain mutation is an error, never a false success or automatic
retry. `set` with `expected_version=0` may create the namespace; existing damaged state is never
silently repaired and counters are never implicitly reset.

## Availability

Redis I/O per eligible logical call is one round trip, bounded at one second, with no DB commit.
A transport failure or timeout opens a short per-process circuit breaker: calls during the cooldown
skip Redis, then one request probes recovery while others proceed without waiting. The breaker
holds only availability state. A valid Lua reply reporting missing or malformed state does not open
it, so one damaged rule cannot bypass a healthy one; cancellation does not open it either. Each
worker breaks and recovers independently, so during recovery workers may disagree.

`analytics.capture_quota_fail_open` aggregates calls allowed without confirmed quota state into one
periodic `org_endpoint_quota_fail_open` event per process (sum `admissions`, not events). It names
calls let through on uncertainty, not proof any rule would have refused them, and is lossy, never
an enforcement counter.

## Recovery boundary

Rules, counts and operation history can be lost together on Redis failure, eviction or restore; a
Redis acknowledgement does not prove a failover kept the write. There is no ban guarantee while
Redis is unavailable or data is lost. If only the connection failed, limits resume after a
successful probe; calls allowed meanwhile are not backfilled. After confirmed loss, an operator
inspects state and sets new limits; old counts and audit records are not reconstructed. During a
merely uncertain write, retry the same operation id and payload.

Deploy readers before enabling the private writer, and enable enforcement on every call worker
before creating a protective rule: an old or disabled worker cannot enforce it, and disabling the
feature bypasses all allowances.
