---
title: Process-local organization traffic logs
status: shipped
sources:
  - src/treg/infra/org_traffic.py
  - src/treg/bootstrap.py
  - src/treg/routers/call.py
  - src/treg/routers/table.py
  - src/treg/mcp.py
  - tests/test_org_traffic.py
  - tests/test_org_concurrency.py
related:
  - architecture/org-concurrency.md
  - architecture/composition.md
  - architecture/proxy-model.md
---

# Organization traffic logs

`org_traffic.RequestLogMiddleware` emits one application-owned `request` JSON line for each
`/call/`, `/catalog/call/` and `/table/` request. The existing Uvicorn transport access log has no
resolved Caller and is bypassed by MCP's in-process HTTP client; Render edge logs are outside the
application's control. This deliberately small application log covers both HTTP and MCP roots.
Its own plain stream handler keeps each JSON record on one physical line, bypassing the MCP SDK's
wrapping console formatter without changing other loggers.
It contains only a fixed surface/adapter label, worker PID, trusted `Caller.org_id`, status,
outcome and handler duration. No paths, query strings, headers, IPs, user agents, names, emails,
keys, request/response bodies or exception text are added. Unresolved authentication logs
`org_id: null`; caller-supplied org headers are never used for attribution.

`mcp_adapter` labels `_call_impl` and `_call_media_impl` (including the directory adapters). Their
underlying HTTP root owns the single request log and counter; successful MCP envelopes add neither.
Adapter preflight exits and eager MCP HTTP 401 challenges log a null org and are not aggregated:
they have not entered the shared handler with a trusted Caller. Feature-disabled HTTP surfaces can
log their resolved org but do not increment handler arrivals. Routing, verification, overflow and
hub children invoke the application directly and never increment these root counters. Direct
`/hub/run`, provider-resource, Arena and background work are outside this scope.

## Window and lifetime

`run_call_surface` calls `identify(..., enter=True)` immediately on entry after caller resolution,
before context creation and Org concurrency admission. `requests` counts these arrivals, including
guard rejections. Authentication/adapter preflight time is excluded. The in-flight gauge and
latency end when the root ASGI invocation returns or raises, after body transmission, upstream
close, and awaited financial cleanup. The middleware adds no awaits, buffering, response wrapping
or cleanup tasks and re-raises the original exception/cancellation.

`completed`, `status_429`, `status_503` and the latency histogram count terminal exits in the
**completion** window, even when arrival was in an earlier window. Thus `completed` is the latency
and status-rate denominator; it may exceed `requests`. Cancellation and errors are terminal exits
too. The status is the HTTP status already started, otherwise 500 for an unhandled exception, or
null when cancelled before any response. A failed stream that already started 200 keeps status 200
and logs `outcome: error` or `cancelled`; no synthetic HTTP status is sent.

Each all/dataplane worker emits one `org_traffic_top` line every 60 seconds, even when empty, from
one reference-counted event-loop timer owned by its app lifespans. Overlapping lifespans share it;
the last shutdown cancels it. There are no periodic asyncio tasks, Redis/DB calls, metrics backends,
endpoints or settings. Observability failures are best effort and cannot replace a response,
refusal or exception. As with any event-loop timer, stalls can delay the nominal 60-second window;
`window_end_unix_s` and successive log times expose this delay. Process exit loses the partial window.

`Counters.snapshot` ranks by arrivals (ties: ascending org ID), emits at most ten rows, resets
window counters and removes inactive orgs. It preserves actual live requests across windows and
seeds each retained org's next `in_flight_max` with that live count. A carried request can therefore
appear with zero arrivals and a nonzero peak/completion count.

Storage is bounded to `MAX_ORGS=4096` identities and 33 counters per org, never individual latency
samples. New identities beyond the cap are untracked until space becomes available at rollover;
live identities are never evicted. Any nonzero `overflow_requests`, `overflow_in_flight` or
`overflow_completed` explicitly means the summary is partial, including when untracked work
crosses windows. These three overflow totals retain no additional org identities.
Per-request logs still contain every resolved org, including overflowed orgs. The Top 10 is exact
among tracked orgs; use the request logs if overflow occurs.

`p95_ms` is the nearest-rank 95th percentile of terminal handler durations, reported as the
histogram bucket's **upper bound**: 1, 2, 4, ... 2^31 milliseconds. It can overestimate by up to a
factor of two. A rank in the last overflow bucket, or no completions, returns null. This method
uses constant work and storage per request, and includes cancelled/failed requests.

## Reading the logs

Illustrative synthetic examples (normal logger prefixes omitted):

```text
request {"worker_pid":123,"surface":"call","adapter":"call","org_id":42,"status":503,"outcome":"completed","handler_ms":800.2}
request {"worker_pid":123,"surface":"table","adapter":null,"org_id":null,"status":401,"outcome":"completed","handler_ms":null}
org_concurrency rejected org_id=42 limit=8 status=429
org_traffic_top {"worker_pid":123,"window_s":60,"overflow_requests":0,"overflow_in_flight":0,"overflow_completed":0,"top":[{"org_id":42,"requests":120,"completed":118,"in_flight_max":8,"status_429":3,"status_503":2,"p95_ms":1024}],"window_end_unix_s":1800000060.0}
```

Look at one or two recent `org_traffic_top` intervals across all service instances/workers to find
the orgs driving arrivals, peaks or 503s, then filter `request` / `org_concurrency rejected` by
`org_id`. Use the log platform's instance/process identity together with `worker_pid`; PIDs alone
are not unique across containers. Align windows by timestamp (worker windows are not synchronized).
Sum requests, completions and status counts for the same org over the selected windows. Top-10
truncation means these cross-worker sums are lower bounds and can miss an org below every worker's
Top 10; request logs provide complete attribution. Summing per-worker peaks is an upper bound on
simultaneous global concurrency, since the peaks need not coincide. **Do not add or average p95s**:
a global percentile needs the underlying durations (available as `handler_ms` in request logs) or
merged histograms, which this summary does not export. These are call-handler measures, not DB
connection occupancy, provider fanout or end-to-end authentication latency.
