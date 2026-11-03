---
title: Org admission - published pause, rate and concurrency limits
status: shipped
sources:
  - src/treg/infra/org_flow_control.py
  - src/treg/infra/org_concurrency.py
  - src/treg/infra/structured_logging.py
  - src/treg/infra/kv.py
  - src/treg/config.py
  - src/treg/routers/call.py
  - src/treg/mcp.py
  - tests/test_org_flow_control.py
  - tests/test_org_flow_control_reader.py
  - tests/test_org_flow_control_redis.py
  - tests/test_org_rate_limit_redis.py
  - tests/test_org_concurrency.py
  - tests/test_org_concurrency_redis.py
  - tests/fixtures/org-flow-control/generate.ts
related:
  - architecture/org-endpoint-quota.md
  - architecture/org-traffic.md
  - architecture/money.md
  - architecture/proxy-model.md
  - ops/deploy.md
---

# Org admission - published pause, rate and concurrency limits

An operator publishes a per-Org snapshot to Redis; every Web worker reads it lazily and applies
**pause, rate and concurrency** to new root requests. This is an availability guard, not a
durable cap or a billing quota. Exact one-time org/endpoint call allowances are a separate
[quota store](org-endpoint-quota.md) with its own failure policy; nothing below applies to them.

## Where the gate sits

`routers.call.run_call_surface` calls `org_flow_control.check` and then `org_concurrency.acquire`
after trusted `Caller` and context resolution, before money admission, body consumption or
upstream work. It covers `/call/`, `/catalog/call/`, `/table/` and the MCP adapters that use them,
on every credential tier (own-key calls included, billing unchanged).

- Routed, verification, overflow and hub children call the application directly and consume no
  extra root slot or token; a hub run through `/call/` holds one root slot.
- Outside the gate: direct `POST /hub/run`, `/orgs/{org_id}/provider-resources` (and the MCP
  `resources_list` tool that uses it), and background/arena work calling `execute_call`
  directly. Expanding the boundary is a deliberate change, not a fix.
- Authentication DB work precedes the gate, so it cannot protect identity lookup from saturation.
  It bounds root requests, not provider fanout or database connections.

An active pause rejects first. Otherwise a non-null `rate_limit` and/or
`concurrency_limit.max_in_flight` is applied; each null field disables only that limit, with no
environment or process-local fallback. Already accepted work is never checked again: streams,
settlement, release and cancellation cleanup finish through their existing paths.

## Configuration, migration and rollback

```sh
TREG_ORG_FLOW_CONTROL_ENABLED=false   # default
TREG_ORG_FLOW_CONTROL_REFRESH_S=2     # 0.2-10
# Set TREG_KV_URL privately before enabling; the same Redis database as the publisher.
```

There is no local-store substitute and no PostgreSQL read on this path. Reads are lazy per active
Org and worker: a request after the refresh interval performs one bounded refresh, joined by
concurrent requests for that Org. There is no background poller, so a quiet worker never
observes or acknowledges a version.

The legacy `TREG_ORG_CONCURRENCY_*` settings configure nothing; only
`TREG_ORG_CONCURRENCY_RETRY_AFTER_S` survives, as an alias of money admission's
`TREG_MONEY_ADMISSION_RETRY_AFTER_S`. Old values are not converted: to roll out, publish a full
snapshot for each intended Org, then enable the flag. Saving a configuration does not publish it.
Review already published non-null rate and concurrency values before enabling, since enabled
workers enforce all three controls.

To lift one Org's limit, publish a higher version with that field null. To disable everything,
turn the flag off through a normal deploy; accepted work finishes, and no key deletion or
migration is needed. Enabling, disabling and refresh are not atomic across workers. Lowering a
limit stops new admissions until occupancy falls below it; it never evicts accepted work.

## Publication contract

The private admin publishes and this reader consumes; the schema is a wire contract between them.
`tests/fixtures/org-flow-control/` holds the reference publisher's actual serialization.

Key `treg:traffic-control:org:<org_id>`, value the complete envelope:

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

- `pause_until`: null or a UTC `Z` timestamp; expiry is checked on every request, independently
  of refresh timing.
- `rate_limit`: null or `{"rps": <positive finite number, fractions allowed>, "burst": <positive
  integer>}`.
- `concurrency_limit`: null or `{"max_in_flight": <positive integer>}`.

All fields are required. Unknown fields, duplicate keys, a wrong Org id, unsupported versions,
strings, booleans or fractions where an integer is required, and non-UTC timestamps are rejected.
The reader never writes, expires or deletes the key. Any change, including clearing a pause or a
limit, is a new full envelope at a higher version.

| Refresh result | Admission behavior |
| --- | --- |
| Redis connectivity, initialization or timeout failure | Allow, bypassing the cached snapshot until a later refresh; keep it for recovery and version comparison. |
| Healthy missing key | Clear the local snapshot; the Org is unconfigured, version zero. |
| Invalid JSON, wrong type, oversized or undecodable | Keep the previous valid snapshot (limits and original pause expiry); allow if none. |
| Lower version, or different content at the same version | Keep the previous valid snapshot. |

`kv.FlowControlReadFailure` separates invalid data from transport failure. A cancelled read
propagates cancellation without touching the shared snapshot or refresh deadline, so the next
caller refreshes immediately. The per-worker snapshot cache is a bounded LRU that never evicts an
entry with an active pause, a configured limit or live callers; when nothing can be evicted, the
uncached Org is allowed without reading Redis. No DB connection is held while Redis is read.

## Rate bucket

Each accepted root spends one token from `treg:org-rate:v1:<org_id>`, shared across the Org's API
keys and workers and independent of configuration version. Tokens refill continuously at `rps`
up to `burst`; a missing bucket starts full, and a version change, pause or concurrency update
grants no new burst. `rate_limit: null` bypasses the bucket without resetting it.

One Lua script, using Redis `TIME`, refills at the bucket's stored prior rate, clamps to the
incoming burst, records the incoming rate (also on refusal), and admits only when both rate and
concurrency pass: a concurrency refusal spends no token and a rate refusal creates no slot. There
is no waiting queue. The bucket never expires before it would be full; if no safe TTL is
representable it stays persistent.

Tokens are never refunded: not after application failure, cancellation or an ambiguous Redis
reply, so a lost reply can spend a token without running the call. Refunding would make
cancellation or retry a quota bypass.

## Concurrency slots

Slots live in one sorted set per Org, `treg:org-concurrency:v1:<org_id>`, shared across keys and
workers and separate from money's admission leases. Each request owns a random token with a
Redis-time lease; the script drops expired members and inserts the owner only below the limit.
The key is reused across configuration versions, so a rule update never resets occupied slots,
and it expires at its latest owner's deadline.

The owner renews every third of a lease; renewal extends only a still-live token and never
recreates an expired one, and release removes only its own token. Ambiguous acquisition and
cancellation join the Redis attempt and clean up by owner token; a lost release reply occupies the
slot until its lease expires. The slot is held through execution, `/table/` conversion, stream
transmission, upstream close and synchronous financial cleanup; `_LeasedStreamingResponse` also
releases on send errors and early disconnects. An async provider submission releases its slot
when that response ends; the remote job holds none between polls.

**Lease expiry is recovery for an unresponsive owner, not proof the request finished.** A process
pause, Redis outage, eviction, restart, failover losing acknowledged writes or a partition lets a
successor acquire while old work continues; there is no provider or database fencing, so a strict
cap cannot be guaranteed. Renewal uncertainty logs `org_concurrency lease_uncertain`. Accepted
work and financial cleanup continue after lease loss; the guard never cancels them or touches
money locks, reserve or settlement.

## Refusals and failures

Only a valid unexpired pause, an exhausted bucket, or contention under a published concurrency
limit rejects. All three are HTTP 429 with `Retry-After`, and a body carrying `retry_after_s` and
`config_version`:

| `detail.error` | `Retry-After` |
| --- | --- |
| `org_traffic_paused` | remaining pause seconds, rounded up |
| `org_rate_limit` | token deficit divided by `rps`, rounded up, at least 1 |
| `org_concurrency_limit` | 1 |

Normal call-id, `X-Treg-Error` and refusal-audit handling applies; `mcp._call_impl` and
`mcp._call_media_impl` carry the guidance as `retry_after`. Retry guidance reserves nothing.

Every Redis failure (initialization, read, acquisition, timeout) allows new work without a
confirmed token or slot; none becomes an HTTP 503.

## Evidence and recovery boundaries

Structured single-line events: `org_flow_control_version_observed`, `org_flow_control_paused`,
`org_flow_control_config_unavailable` and `org_flow_control_rate_limited`, plus
`org_concurrency rejected|acquire_unavailable|release_unavailable|lease_uncertain`. They never
carry raw configuration, credentials, tokens or exception text, and use the scoped handler in
[Org traffic logs](org-traffic.md). They are rate-limited with `suppressed_since_last_log`, so
they are not refusal counts; use request logs and their 429 outcomes for those. A version
observation is per worker and on demand, not an acknowledgement that every worker applied it.

Version protection lives only in the resident cache: restart, eviction or a healthy missing key
forgets it, and a cold missing key cannot tell an unconfigured Org from lost Redis data. Restore
published keys before (re-)enabling workers; the reader cannot detect an older valid backup. Redis
bucket loss, eviction or failover can likewise restore a full burst.
