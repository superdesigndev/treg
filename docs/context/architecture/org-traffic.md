---
title: Process-local organization traffic logs
status: shipped
sources:
  - src/treg/infra/org_traffic.py
  - src/treg/infra/structured_logging.py
  - src/treg/bootstrap.py
  - src/treg/routers/call.py
  - src/treg/routers/table.py
  - src/treg/mcp.py
  - src/treg/application/call/types.py
  - src/treg/application/call/service.py
  - tests/test_org_traffic.py
  - tests/test_org_concurrency.py
related:
  - architecture/org-flow-control.md
  - architecture/composition.md
  - architecture/proxy-model.md
---

# Organization traffic logs

`org_traffic.RequestLogMiddleware` emits one application-owned `request` JSON line per `/call/`,
`/catalog/call/` and `/table/` root, and each all/dataplane worker emits an `org_traffic_top`
summary per window. The Uvicorn access log cannot replace it: it has no resolved Caller and never
sees MCP, whose adapters reach these routes through an in-process HTTP client.

## Logging setup

`structured_logging.get_logger` gives the named `treg.org_traffic`, `treg.org_flow_control` and
`treg.capacity_events` loggers a plain stderr handler only when they have none, so MCP's root
console formatter neither wraps nor duplicates them. Levels stay inherited and root logging is
untouched, which means root handlers, filters and formatting do not apply to these streams:
customize them on the named loggers, and keep each JSON record on one physical line.

## What a request line may carry

Attribution is the trusted `Caller` (`org_id`, `api_key_id`, null without a managed key or
resolved auth), never a caller-supplied header. Target, provider, credential tier and refusal kind
are scalars `CallContext` already resolved, copied with `describe`; no lookup is added just to fill
a log, and unresolved fields stay null. A routed or hub root names no provider, since its children
are separate calls. Overflow logs the answer the caller got (`platform-overflow`), not the direct
attempt. `call_ref` is this root context's id; an idempotent replay logs its own `call_ref` and the
original id in `replay_of`.

Never add paths, query strings, headers, IPs, user agents, names, emails, credential values,
bodies or exception text, and never add counter dimensions from these fields.

`mcp_adapter` labels `_call_impl` and `_call_media_impl`; the underlying HTTP root owns the single
request line and counter. Adapter preflight exits and eager MCP 401 challenges log a null org and
are not aggregated. Children (routing, verification, overflow, hub steps) never count as roots;
direct `/hub/run`, provider-resource, arena and background work are out of scope.

## Window semantics

`run_call_surface` registers the arrival right after caller resolution, before
[Org admission](org-flow-control.md), so `requests` includes guard rejections and excludes
authentication time. In-flight and latency end when the root ASGI call returns or raises, after
body transmission, upstream close and awaited financial cleanup. The middleware adds no awaits,
buffering or tasks and re-raises the original exception or cancellation.

Arrivals belong to their start window; `completed`, status counts and latency to their completion
window, so `completed` is the denominator for rates and may exceed `requests`. A stream that
already sent 200 keeps status 200 and logs outcome `error` or `cancelled`; status is null when
cancelled before any response.

The summary is one reference-counted event-loop timer shared by overlapping app lifespans, with no
Redis, DB or metrics backend; it emits even when empty, can drift under loop stalls
(`window_end_unix_s` shows it) and loses the partial window on exit. It ranks the top orgs by
arrivals, keeps live requests across windows (a carried request can show zero arrivals but a peak),
and tracks a bounded number of orgs without evicting live ones. Any nonzero `overflow_*` total
means the summary is partial; request lines still name every org. `p95_ms` is the nearest-rank p95
reported as its power-of-two histogram bucket's upper bound, so it can overstate by up to 2x.

## Reading the logs

Find the orgs driving arrivals, peaks or 503s in recent `org_traffic_top` lines across all
instances, then filter `request` and the admission events by `org_id`. PIDs are only unique with
the platform's instance identity, and worker windows are not aligned. Cross-worker sums of top-N
counts are lower bounds; summed per-worker peaks are an upper bound on global concurrency. **Never
add or average p95s**: use `handler_ms` from request lines. These are handler measures, not DB
connection occupancy, provider fanout or authentication latency.
