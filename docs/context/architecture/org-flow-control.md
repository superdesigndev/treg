---
title: Published organization pause, rate and concurrency admission
status: shipped
sources:
  - src/treg/infra/org_flow_control.py
  - src/treg/infra/structured_logging.py
  - src/treg/infra/kv.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - tests/test_org_flow_control.py
  - tests/test_org_flow_control_reader.py
  - tests/test_org_flow_control_redis.py
  - tests/test_org_rate_limit_redis.py
  - tests/fixtures/org-flow-control/generate.ts
related:
  - architecture/org-concurrency.md
  - architecture/money.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

# Published organization pause, rate and concurrency admission

Exact one-time org/endpoint call allowances use a separate
[quota store](org-endpoint-quota.md). These temporary allowances allow calls on unknown Redis
state, including previously restricted targets; they do not repair missing counters. The root-only,
expiring and cached admission policies below do not apply to them.

`org_flow_control.check` reads operator-published Redis snapshots at
`routers.call.run_call_surface`, after trusted Caller/context resolution and before the optional
Org rate/concurrency gate, money admission, body consumption or upstream work. It applies to the
same root surfaces as [Org concurrency](org-concurrency.md): `/call/`, `/catalog/call/`, `/table/`
and MCP adapters using them. Direct hub runs, provider-resource requests and application/background
calls remain outside this gate. Authentication queries happen before it.

One reader check returns the effective snapshot for **pause, rate and concurrency**. An active
pause rejects immediately. Otherwise `org_concurrency.acquire` applies a non-null
`rate_limit` and/or `concurrency_limit.max_in_flight`. When rate is configured, one Redis Lua
operation checks the token bucket and optional shared slot limit before consuming either.
Each null field disables only that limit, with no static fallback. A snapshot observation is
not confirmation that every worker has applied it.

## Configuration and publication contract

Disabled by default; enabling requires an explicit valid Redis `TREG_KV_URL` pointing at the same
database as the publisher. There is no local-store substitute and no PostgreSQL read on this path.

```sh
TREG_ORG_FLOW_CONTROL_ENABLED=false
TREG_ORG_FLOW_CONTROL_REFRESH_S=2
# Set TREG_KV_URL privately before enabling.
```

The refresh interval is 0.2-10 seconds. Reads are lazy per active Org/worker; a quiet worker does
not poll or acknowledge a version. A request after the interval performs one bounded refresh;
there is no background monitor or publisher.

The key is `treg:traffic-control:org:<org_id>`, containing the complete JSON envelope:

```json
{
  "org_id": 42,
  "version": 1,
  "config": {
    "schema_version": 1,
    "pause_until": null,
    "rate_limit": null,
    "concurrency_limit": null
  }
}
```

All fields are required. Unknown fields, duplicate keys, wrong Org IDs, unsupported versions,
coerced/bool numbers, invalid limits and non-UTC pause timestamps are rejected. The reader does
not write, expire or delete published keys. A publisher must use a higher version to change or
restore content; clearing a pause means publishing `pause_until: null` in a new full version.
Clearing rate or concurrency means `rate_limit: null` or `concurrency_limit: null`; pausing
expires at its absolute UTC time independently of those fields. A configuration save alone does
not publish or activate that version. Review existing non-null rate and concurrency values
before deploying workers that enforce them with this switch enabled. See
[concurrency migration](org-concurrency.md#configuration-migration-and-rollback)
for the removed environment settings and authorized rollout sequence.

The synthetic fixtures in `tests/fixtures/org-flow-control/` capture the reference publisher's
actual `publish` serialization, covering disabled controls, fractional-second UTC timestamps,
populated limits and a resume. The consumer's strict validation is unchanged.

## Atomic rate and concurrency admission

A non-null `rate_limit` contains `rps` (positive, finite requests per second, allowing fractions)
and `burst` (a positive integer token capacity), for example `{"rps":2,"burst":4}`. Each
accepted root request consumes one token; tokens refill continuously up to `burst`.
The bucket is shared across the Org's API keys and
workers at `treg:org-rate:v1:<org_id>`, independently of configuration version. A missing bucket
starts full. A version change, pause or concurrency update does not grant a new burst.

The Redis operation uses `TIME` at microsecond resolution. It refills an existing bucket using
its stored prior rate, then clamps tokens to the incoming burst and records the incoming rate.
Those metadata and refill updates also occur on refusal, without spending a token or adding a
slot. The script admits only when both configured limits pass, then spends one token and, when
configured, inserts the uniquely owned slot. A concurrency refusal never spends a rate token;
a rate refusal never creates a slot. There is no waiting queue.

Bucket expiry is no earlier than the time it would become full, including any delay from Redis
clock rollback. If a safe TTL cannot be represented for a very small rate, the bucket remains
persistent rather than expiring early. `rate_limit: null` bypasses the bucket; it does not reset
it. Workers can still apply an older valid cached configuration during the existing refresh
window; the bucket adds no second version authority or all-worker activation barrier.

Rate tokens account for admission attempts that passed these checks. They are not refunded
after application failure, cancellation or an ambiguous Redis reply; a lost reply may therefore
consume a token without running the call. This avoids making cancellation or retry a quota bypass.
Any possibly acquired concurrency owner is still cleaned up through the existing owner-safe path.

## Refusals and failures

A future `pause_until` returns HTTP 429, `Retry-After` rounded up from the remaining seconds,
and `detail.error=org_traffic_paused` with `config_version`. UTC expiry is checked on every
request, independently of refresh timing. Normal call-ID, error-header and refusal-audit handling
is retained; MCP call/media responses retain the retry guidance.

Only a valid, unexpired pause, exhausted rate bucket or contention under a valid published
concurrency limit rejects new roots. Rate exhaustion returns HTTP 429 `org_rate_limit` and
`Retry-After` equal to the ceiling of the token deficit divided by `rps` (plus any retained
clock-rollback delay), at least one second and capped at 2147483647 seconds. Concurrency
contention returns HTTP 429 `org_concurrency_limit`, `Retry-After: 1`. Both include
`retry_after_s` and `config_version`;
retry guidance does not reserve future capacity. Refresh failures follow these policies:

| Refresh result | Admission behavior |
| --- | --- |
| Redis connectivity, initialization or timeout failure | Allow, bypassing cached pause, rate and concurrency until a subsequent refresh. Retain the snapshot for recovery and version comparison. |
| Healthy missing key | Clear the local snapshot and treat the Org as unconfigured version zero. |
| Invalid JSON, wrong type, oversized or undecodable data | Retain the previous valid snapshot, including its rate/concurrency limits and original pause expiry; allow if none exists. |
| Lower version or changed content at the same version | Retain the previous valid snapshot, including its rate/concurrency limits and original pause expiry. |

`kv.FlowControlReadFailure` distinguishes invalid data from transport unavailability; `None`
means missing and a string is a bounded payload. A cancelled read propagates cancellation without
changing shared snapshot, availability or refresh deadline. The next caller or waiting request
can refresh immediately; cancellation never becomes an HTTP 503.

Snapshot refreshes and combined rate/slot Redis I/O have a 200 ms deadline. Per-Org single-flight
and a 32-read semaphore bound refresh work; failures are cached until the next interval.
The worker has at most 4096
LRU entries, protecting active pauses, configured rate/concurrency limits and entries with active
callers/waiters. If no entry can be evicted, the uncached Org is allowed without adding an entry
or reading Redis, with a bounded capacity warning. No DB connection is held while Redis is read.
Rate/slot initialization, acquisition and timeout failures also allow new work without a confirmed token or concurrency
slot; only an explicit configured-limit refusal rejects. No Redis failure is converted into
an HTTP 503 by these guards.

Already accepted work is never checked again: streams, settlement, release and cancellation
cleanup finish through their existing paths. Disabling the flag uses the normal deployment
process and does not cancel accepted work or erase Redis data.

## Evidence and recovery boundaries

Single-line `org_flow_control_version_observed`, `org_flow_control_paused` and
`org_flow_control_config_unavailable` records carry worker PID, trusted Org, call reference,
version, the compatibility field `capability=pause`, and
`capabilities=["pause","rate","concurrency"]`. Version observations include `max_in_flight`
and a `rate_limit` object, each null when disabled. These capabilities describe
the reader's supported admission checks, not successful slot acquisition or acknowledgment by
all workers. Records omit raw configuration, credentials and exception text.
They use the scoped plain handler described in [Org traffic logs](org-traffic.md), inherit
existing logger levels and preserve explicitly configured named handlers. Root logging is not
changed; root handler routing and filters do not apply to the default event sink.
Pause events and refresh warnings are each limited to one per Org every five seconds; the next
event reports `suppressed_since_last_log` for that event category, possibly spanning versions or
failure reasons. Counts remaining when an entry is evicted or a worker exits are lost. Capacity
warnings are limited to one per worker every five seconds. Ordinary request logs still describe
each request's outcome; sampled pause events are not a complete refusal count.
Observation is per worker and on demand, not a heartbeat or a complete worker-acknowledgement set.
Rate refusals emit `org_flow_control_rate_limited` with trusted Org, configuration version,
`rps`, `burst`, retry guidance and a null call reference. These structured single-line events
are bounded to one per worker every five seconds with `suppressed_since_last_log`. This
aggregation spans Orgs and versions; use request logs and their 429 outcomes for full refusal
counts.

Version protection is only as durable as the resident cache. Restart, eviction and a healthy
missing key forget history. A cold missing key cannot distinguish an unconfigured Org from lost
Redis data. Restore the
published keys before enabling/re-enabling workers; the reader cannot detect an older valid
Redis backup after losing its own watermark.
Redis bucket loss, eviction, restart or failover can similarly restore a full burst; this is an
availability-oriented guard, not a durable billing quota.
