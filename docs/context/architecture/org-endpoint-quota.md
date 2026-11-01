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
exact trusted Org ID and canonical catalog endpoint ID. It applies to every credential tier.
There is no wildcard, automatic replenishment, or time window. This is protective admission,
not money or the provider's credits. Changing a limit preserves `used`; reset is explicit.
While Redis returns a valid rule, `limit=0` refuses new logical calls after the management write.
Calls already admitted continue. Clearing a rule keeps a disabled tombstone and its count.
Unknown Redis state does not block a call, including one that was previously blocked, but a
confirmed exhausted rule still does. This temporary protection is not a durable spending cap.

The shared call application covers REST, catalog call, table and MCP, plus routed children and
hub steps that enter `execute_call`. Intake returns stored idempotent responses before admission.
Ordinary catalog calls check after authorization, canonical resolution, resource validation and
money admission. A cache hit consumes one call immediately before serving the stored answer.
On a cache miss, relay checks JSON, credential injection and SSRF before quota admission immediately
before sending. Overflow checks its route, budget, child reservation and request preparation first.
On the direct path, shared provider-rate observation follows quota admission, so a quota refusal
does not consume the observed rate window or appear as a provider attempt without an answer.
Balance, daily/tag cap, resource and other pre-dispatch refusals consume no quota. A quota rejection
releases any money hold and unused overflow budget; a successful Redis admission followed by
cancellation or transport failure can still consume a unit without a completed provider request.
Public-resource authorization may read provider metadata before quota admission. That lookup
creates no Treg money hold or team charge; its provider-side price must be verified separately.
The allowance gates the target's execution, not all preliminary provider I/O.

Routed parents defer their allowance until a child can actually run or serve a cache hit. One Lua
operation checks all unadmitted parent targets plus the child. Any confirmed exhausted rule rejects
the batch without incrementing a counter. Otherwise valid enabled rules increment together; unknown
fields remain untouched and do not mask a known refusal elsewhere in the batch. A local child
rejection cannot spend the parent's allowance. Direct retries and overflow remain the same
logical call, and shared `quota_admissions` deduplicates already admitted targets. Separate hub steps
and requests get separate contexts. A routed or arena submission's internal polls copy the admitted
set and mark only their exact poll target as already admitted; they perform no quota I/O or counting.
Caller-initiated polls remain ordinary endpoint calls. This exemption has no caller-controlled flag.

Hub recipe IDs are not quota targets. Their catalog child calls are guarded, but the hub wrapper
retains its existing error and billing contract: it can translate a child refusal to 424 or
continue an `allow_fail` step, charging other completed steps and an applicable maker price.
The refused endpoint itself is neither executed nor charged. A hub run is not a routed endpoint.

An exhausted rule returns HTTP 429 with `detail.error=org_endpoint_quota_exceeded` and zero
call cost. It is not a supplier 429, has no automatic refill time, and cannot trigger overflow.
A routed child exhausted allowance skips that candidate without spending the provider-error retry
budget; another unrestricted candidate may run. A routed parent's exhausted allowance stops its
whole route. When no target has a confirmed exhausted rule, Redis uncertainty allows the call and
marks its targets admitted in the logical-call context, so a retry does not try to consume the same
allowance again. A known quota refusal during
optional verification is reported as `unchecked`, preserving the successful find and its normal
charge. Redis uncertainty permits the child to run and does not produce a call error.
An optional route upgrade re-asks with another identity as a new logical invocation, so it checks
the routed allowance again; a refusal preserves the first answer without charging for the upgrade.
Existing deferred holds are released when the primary route refuses.
Once an async submission has a task ID, poll errors retain the existing pending response and durable
worker settlement ownership. Accepted calls are never refunded to the quota after upstream failure,
cancellation or a lost Redis response.

## Store contract

Disabled by default. `TREG_ORG_ENDPOINT_QUOTA_ENABLED=true` requires an explicit
`TREG_ORG_ENDPOINT_QUOTA_REDIS_URL`; it does not inherit `TREG_KV_URL` or use a process-local
counter fallback. All enforcing instances and the private management API must point at the same
Redis database. There is no persistent target registry or deployment allowlist: an operator can set
an exact new target through the management API. The private admin uses its existing full machine
authentication; ordinary business keys cannot manage allowances.

When enabled and healthy, an eligible logical catalog call makes one Redis round trip even when
its target has no rule. Parent/child targets are checked together. Admission adds no database commit
when the request transaction is already closed. Redis I/O is bounded to one second. A transport
failure or timeout opens a five-second circuit breaker in that process; calls during the cooldown
proceed without Redis I/O. Afterward one request probes recovery while concurrent requests still
proceed without waiting. A failed probe starts another cooldown. Healthy requests remain concurrent.
Requests already in flight before the first failure, and recovery probes, can still wait one second.
Each worker discovers failures and probes recovery independently; the single-probe guarantee is
per process, so the first concurrent requests across workers can each incur that delay. During
recovery, one worker may enforce surviving rules while another still bypasses them in its cooldown.
The breaker stores only availability state, never rules or counters. A valid Lua response indicating
missing or malformed state does not open it, so one damaged rule cannot bypass another healthy rule.
Cancellation does not open the breaker and propagates through normal hold cleanup.

`analytics.capture_quota_fail_open` accumulates unconfirmed logical admissions, including breaker
bypasses, into one bounded 60-second window per process. The existing analytics flusher emits one
`org_endpoint_quota_fail_open` event when the window is due, even if calls stop; graceful shutdown
flushes a final partial window. It uses the server identity and standard telemetry build metadata.
Sum `admissions` to observe the volume, not event count. A parent/child batch is one admission;
`target_count` sums target entries across batches, not distinct targets. Logical retries and response
replays do not add to the totals.

`target_examples` contains the first ten distinct trusted `{org_id, endpoint_id}` pairs observed in
the window; `targets_truncated` records whether another distinct pair was omitted. These bounded
examples are neither a random sample nor a complete target list. They identify calls allowed
without confirmed quota state, not proof that a rule existed or would have refused them: Redis
uncertainty prevents that determination. No call ID, credentials or request/response content is sent.
These in-memory telemetry totals are not an enforcement counter. Telemetry disabled, abrupt process
exit, queue pressure or delivery failure can lose counts and examples. The warning remains limited
to once per minute. No dashboard, alert or production telemetry setting is created by this change.

One hash without an application TTL, `treg:org-endpoint-quota:v1`, contains `__schema=1` and fields
`rule:<org_id>:<endpoint_id>`. Each rule is exactly:

```json
{"version":1,"limit":100,"used":0,"enabled":true}
```

All numeric fields are integers at most 2147483647; version starts at one and the others at zero.
The admission Lua script validates the namespace, absence of expiry, and complete rule before
checking `used < limit` and incrementing in the same atomic operation. A healthy namespace with
an absent rule is unconfigured and allows the call. Missing namespace/schema, malformed state,
missing count or Redis I/O failure is unknown. Unless another target has a confirmed exhausted rule,
the application allows the call without repairing that state or claiming its counter is zero. Valid
rules in a partially unknown batch still increment atomically. The rule and count share one field. The runtime never
creates, expires or deletes rules. An ambiguous Redis response may have consumed a unit even though
the application cannot confirm it; the call proceeds, and its internal retries do not consume again.

Management uses a required unique operation ID, expected version and reason. It writes the new
rule and `op:<operation_id>` audit/idempotency record with one Redis HSET inside Lua. Audit records
include the authenticated actor/source, exact target, action, reason, before/after state and
Redis time. Consumption changes `used` without changing the management version, so a concurrent
limit edit preserves the count seen atomically at the edit. Retrying the same operation returns
its saved result, not a second reset; reusing an ID for different content or a stale version
conflicts. Operation results describe the original change; query the rule for its current count.
These records have no TTL. No best-effort audit queue is used for management writes. Their audit
and idempotency guarantees last only while the Redis records survive; accepted data loss can erase
rules, counts and operation history together.

## Activation and recovery boundary

Rules may be lost during Redis failure, eviction or restoration. Persistence, backups and avoiding
eviction reduce that risk, but are not prerequisites for this temporary guard. A Redis acknowledgement
does not prove a later failover retained the write, and a valid stale snapshot cannot reveal missing
admissions. There is no reliable ban guarantee while Redis is unavailable or rule data has been lost.
If only the connection failed and the rules and counts survived, their limits resume after a
successful recovery probe when the cooldown expires. The outage does not clear or reset them.
Calls allowed without a confirmed admission are not backfilled after recovery, although an ambiguous
write may already have counted one.

The management API remains strict about its own writes: a Redis outage or uncertain mutation returns
an error, never a false success or automatic command retry. Reads of a completely absent namespace
report no rule/operation without creating state. An explicit `set` with `expected_version=0` can
create a new namespace, its rule and operation record atomically. Existing malformed or partially
damaged state is not silently repaired, and existing counters are never implicitly reset.

After confirmed loss, an operator or authorized automation must inspect the current state and set new
limits. Old counts and audit records are not reconstructed. During a normal uncertain write, query
or retry the same operation UUID and payload; after confirmed history loss, treat a replacement
rule as a new intended operation. In-memory routing deduplication never substitutes for the shared
count across requests or instances.

Deploy readers before enabling the private writer, then enable enforcement on every call worker
before creating a protective rule. An old or disabled worker cannot enforce it. Disabling the
feature bypasses all allowances. No production configuration, Redis initialization or rule
application is performed by the code change itself.
