-- Actionable errors in the call events (docs/context/architecture/call-events.md, "Failed calls").
--
-- Failed calls by error class, refuser (treg or the upstream), tier and endpoint, with per-class
-- totals; `root_calls` leaves out routed children, checks and polls. Caller-side classes are left
-- out unless `include_caller_side` is true.
--
-- An event has only `status_code`, `refused_by` and `credential_tier`, so this reading is coarser
-- than the `call_error` log line, which also reads the failure kind and the provider's documented
-- error code. Where they differ (here / log line):
--   - a 429 because treg's spend-cap store did not answer: `caller_balance_or_cap` /
--     `treg_capacity`
--   - a 403 for an operator's quota: `caller_policy` / `treg_policy`
--   - a provider's 400 documented as an empty account: `caller_input` / a provider account class
--   - a 402 on treg's pay-per-use OAuth app: the team's account / treg's
--   - treg's relay-stage refusals without a `refused_by` (declared rate, failed injection, refused
--     address, buffer limit): `upstream` / `treg`
--   - a routed parent the provider refused (`route_caller_fault`): `treg` / `upstream`; for the
--     treg-or-upstream split of routed calls read the log line
--   - a 404 for want of a credential: `not_found`, left in / the team's or treg's provider account
--
-- DuckDB over the Iceberg catalog attached as `treg`. Replace the fixed UTC bounds; keep the window
-- bounded, since a day costs the same scan whatever the filters select.

WITH params AS (
    SELECT TIMESTAMP '2026-01-01 00:00:00' AS since,
           TIMESTAMP '2026-01-02 00:00:00' AS until,
           false AS include_caller_side
),
failed AS (
    SELECT DISTINCT ON (event_id)
           event_id, status_code, refused_by, credential_tier, provider, endpoint_id, parent_ref
    FROM treg.analytics.call_events, params
    WHERE event_time >= params.since AND event_time < params.until
      AND event_kind IN ('call', 'async_submit')
      AND status_code >= 400
      AND NOT coalesce(is_eval, false)
      AND endpoint_id IS DISTINCT FROM 'smoke.test'
),
classified AS (
    SELECT *,
           CASE WHEN refused_by IS NULL THEN 'upstream' ELSE 'treg' END AS refuser,
           CASE
               WHEN refused_by IN ('balance', 'cap') THEN 'caller_balance_or_cap'
               WHEN refused_by = 'policy' THEN 'caller_policy'
               WHEN refused_by IN ('retired', 'paused') THEN 'treg_policy'
               WHEN refused_by = 'request' THEN 'caller_input'
               WHEN refused_by = 'capacity' THEN 'treg_capacity'
               WHEN refused_by = 'resolution' THEN 'not_found'
               WHEN refused_by = 'auth' THEN 'caller_auth'
               WHEN refused_by IS NOT NULL THEN 'other'
               WHEN status_code IN (401, 402, 403) AND credential_tier IN ('credential', 'tool')
                   THEN 'caller_provider_account'
               WHEN status_code IN (401, 402, 403) THEN 'our_provider_account'
               WHEN status_code IN (400, 405, 409, 413, 415, 422) THEN 'caller_input'
               WHEN status_code IN (404, 410) THEN 'not_found'
               WHEN status_code IN (408, 504) THEN 'timeout'
               WHEN status_code = 429 THEN 'rate_limit'
               WHEN status_code >= 500 THEN 'upstream_error'
               ELSE 'other'
           END AS error_class
    FROM failed
)
SELECT CASE WHEN grouping(endpoint_id) = 1 THEN 'total' ELSE 'endpoint' END AS level,
       error_class,
       error_class IN ('caller_balance_or_cap', 'caller_policy', 'caller_provider_account') AS caller_side,
       refuser, credential_tier, provider, endpoint_id,
       count(*) AS calls,
       count(*) FILTER (WHERE parent_ref IS NULL) AS root_calls
FROM classified, params
WHERE params.include_caller_side
   OR error_class NOT IN ('caller_balance_or_cap', 'caller_policy', 'caller_provider_account')
GROUP BY GROUPING SETS ((error_class, refuser),
                        (error_class, refuser, credential_tier, provider, endpoint_id))
ORDER BY level DESC, calls DESC
LIMIT 200;
