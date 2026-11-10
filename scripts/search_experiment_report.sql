-- The discovery experiment, read from the database (application/search_experiment.py).
--
-- The outcome of a search is what the caller did next: a `call` by the same team and person, within
-- ten minutes, to an endpoint that was on the page. `searchlog.shown` is the page as served, with
-- each row's owner; `callrecord` (audit) carries the call. No labels anywhere — the join IS the label.
--
-- Run against the read replica:  psql "$TREG_READ_DATABASE_URL" -f scripts/search_experiment_report.sql
-- `-v mode=v2` reads another mode's arms, `-v window='7 days'` another window (the defaults below
-- apply only where the variable was not given). Postgres only (jsonb functions). Every block is
-- read-only.

\if :{?window}
\else
\set window '30 days'
\endif
\if :{?followup}
\else
\set followup '10 minutes'
\endif
\if :{?mode}
\else
\set mode 'interleave'
\endif

-- 1. Volume and health: how many searches, how often the pages differ, what the judge cost.
--    `differs` is the population the experiment can say anything about; an identical page is a
--    query the judge did not change. Read this block first — a low `differs` share means little to
--    win, a high `judge_error` share means the effect below is really a fallback rate.
SELECT mode, arm,
       count(*)                                        AS searches,
       round(100.0 * avg(differs::int), 1)             AS differs_pct,
       round(100.0 * avg((baseline_total = 0)::int), 1) AS baseline_empty_pct,
       round(100.0 * avg((judge_error IS NOT NULL)::int), 1) AS judge_error_pct,
       -- a judge answer served from the in-process cache costs 0 ms; the latency is the live one's
       percentile_cont(0.5) WITHIN GROUP (ORDER BY judge_ms) FILTER (WHERE judge_ms > 0)  AS judge_ms_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY judge_ms) FILTER (WHERE judge_ms > 0) AS judge_ms_p95,
       sum(judge_tokens_in)                            AS judge_tokens_in
FROM searchlog
WHERE created_at > now() - :'window'::interval
GROUP BY 1, 2 ORDER BY 1, 2;

-- 2. Conversion per arm, stratified by whether the LEXICAL page was empty.
--    The two strata are two claims: on an empty baseline any conversion is recall the judge added;
--    on a non-empty baseline the question is ranking. The pure arms give the absolute rates; the
--    interleave arm's rate is a sanity check (it should sit between them, never below both).
WITH pages AS (
  SELECT s.id, s.arm, s.org_id, s.user_email, s.created_at, (s.baseline_total = 0) AS baseline_empty,
         ARRAY(SELECT jsonb_array_elements(s.shown::jsonb)->>0) AS shown_ids
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = :'mode' AND s.org_id IS NOT NULL
),
converted AS (
  SELECT p.id, bool_or(c.id IS NOT NULL) AS converted
  FROM pages p
  LEFT JOIN callrecord c
    ON c.org_id = p.org_id AND c.user_email = p.user_email
   AND c.created_at BETWEEN p.created_at AND p.created_at + :'followup'::interval
   AND c.endpoint_id = ANY (p.shown_ids)
  GROUP BY p.id
)
SELECT p.arm, p.baseline_empty,
       count(*) AS searches,
       sum(cv.converted::int) AS converted,
       round(100.0 * avg(cv.converted::int), 1) AS conversion_pct
FROM pages p JOIN converted cv USING (id)
GROUP BY 1, 2 ORDER BY 2, 1;

-- 2b. Conversion by JOB, per arm and verdict (`v2` mode, where every served row carries its job as
--     `shown`'s third element). A v2 answer's hint sends an agent to catalog_get and to any vendor
--     of a fitting job, on the page or not, so counting only calls to rows on the page undercounts
--     it; here a call converts when the called endpoint does a job the page showed. Which job an
--     endpoint does is read from the rows that carried it on any page in the window. The verdict
--     on a `baseline` row is v2's reading of a query that caller answered from the lexical page:
--     a `none` there that still converted is a false none, read directly.
WITH jobs AS MATERIALIZED (
  SELECT DISTINCT e->>0 AS endpoint_id, e->>2 AS capability
  FROM searchlog s, jsonb_array_elements(s.shown::jsonb) e
  WHERE s.created_at > now() - :'window'::interval AND s.mode = :'mode'
    AND jsonb_array_length(e) > 2 AND e->>2 IS NOT NULL
),
pages AS (
  SELECT s.id, s.arm, coalesce(s.verdict, 'v1') AS verdict, s.org_id, s.user_email, s.created_at,
         (s.baseline_total = 0) AS baseline_empty,
         ARRAY(SELECT DISTINCT e->>2 FROM jsonb_array_elements(s.shown::jsonb) e
               WHERE jsonb_array_length(e) > 2 AND e->>2 IS NOT NULL) AS shown_jobs
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = :'mode' AND s.org_id IS NOT NULL
),
converted AS (
  -- one correlated probe per page, on the (org, email, created_at) index, like 2c; a hash of
  -- callrecord against the jobs table would scan the window's calls once per block instead
  SELECT p.id, EXISTS (
    SELECT 1 FROM callrecord c JOIN jobs j ON j.endpoint_id = c.endpoint_id
    WHERE c.org_id = p.org_id AND c.user_email = p.user_email
      AND c.created_at BETWEEN p.created_at AND p.created_at + :'followup'::interval
      AND j.capability = ANY (p.shown_jobs)) AS converted
  FROM pages p
)
SELECT p.arm, p.verdict, p.baseline_empty,
       count(*) AS searches,
       sum(cv.converted::int) AS converted,
       round(100.0 * avg(cv.converted::int), 1) AS job_conversion_pct
FROM pages p JOIN converted cv USING (id)
GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

-- 2c. After an empty answer (verdict none), did the caller call anything in the catalog at all?
--     The page was empty, so there is no row to credit; a call right after a `none` is the agent
--     finding its way by other means, which a true gap would not allow.
WITH empties AS (
  SELECT s.id, s.arm, s.verdict, s.org_id, s.user_email, s.created_at
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = :'mode' AND s.org_id IS NOT NULL
    AND s.verdict LIKE 'none%'
)
SELECT arm, verdict, count(*) AS searches,
       round(100.0 * avg((EXISTS (SELECT 1 FROM callrecord c WHERE c.org_id = e.org_id AND c.user_email = e.user_email
                                   AND c.created_at BETWEEN e.created_at AND e.created_at + :'followup'::interval))::int), 1)
         AS called_anyway_pct
FROM empties e GROUP BY 1, 2 ORDER BY 1, 2;

-- 3. Interleaving credit (the paired comparison). For each interleaved search whose caller went on
--    to call a shown endpoint, the point goes to the ranker that put it there — only where the
--    pages DISAGREE: a row only one page carried, or one they ranked differently (the higher rank
--    wins). Rows both pages carried at the same rank are ties and count for nobody. Under the null
--    hypothesis the two credits are a fair coin; the last column is the two-sided binomial z.
WITH il AS (
  SELECT s.id, s.org_id, s.user_email, s.created_at,
         ARRAY(SELECT jsonb_array_elements_text(s.baseline_ids::jsonb)) AS base_ids,
         ARRAY(SELECT jsonb_array_elements(s.judged::jsonb)->>0)        AS judged_ids,
         ARRAY(SELECT jsonb_array_elements(s.shown::jsonb)->>0)         AS shown_ids
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = :'mode' AND s.arm = 'interleave'
    AND s.judged IS NOT NULL AND s.differs AND s.org_id IS NOT NULL
),
first_call AS (
  SELECT DISTINCT ON (il.id) il.id, il.base_ids, il.judged_ids, c.endpoint_id
  FROM il JOIN callrecord c
    ON c.org_id = il.org_id AND c.user_email = il.user_email
   AND c.created_at BETWEEN il.created_at AND il.created_at + :'followup'::interval
   AND c.endpoint_id = ANY (il.shown_ids)
  ORDER BY il.id, c.created_at
),
credited AS (
  SELECT id,
         array_position(base_ids, endpoint_id)   AS rank_base,
         array_position(judged_ids, endpoint_id) AS rank_judged
  FROM first_call
),
points AS (
  SELECT CASE
           WHEN rank_base IS NOT NULL AND rank_judged IS NULL THEN 'baseline'
           WHEN rank_judged IS NOT NULL AND rank_base IS NULL THEN 'judged'
           WHEN rank_base < rank_judged THEN 'baseline'
           WHEN rank_judged < rank_base THEN 'judged'
           ELSE 'tie'
         END AS credit
  FROM credited
)
SELECT sum((credit = 'judged')::int)   AS judged_wins,
       sum((credit = 'baseline')::int) AS baseline_wins,
       sum((credit = 'tie')::int)      AS ties,
       (SELECT count(*) FROM il)       AS interleaved_searches_with_disagreement,
       round(
         ((sum((credit = 'judged')::int) - sum((credit = 'baseline')::int))
         / sqrt(nullif(sum((credit IN ('judged', 'baseline'))::int), 0)))::numeric, 2) AS z
FROM points;

-- 4. Re-query rate: a second search by the same caller within two minutes with no call in between
--    is a page that did not do its job. Lower is better; compare across arms, and read a v2 `none`
--    with 2c: an answer that tells the agent to stop lowers this number whether or not it was right.
WITH s1 AS (
  SELECT s.*, lead(s.created_at) OVER (PARTITION BY s.org_id, s.user_email ORDER BY s.created_at) AS next_search
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = :'mode' AND s.org_id IS NOT NULL
)
SELECT arm, coalesce(verdict, 'v1') AS verdict, count(*) AS searches,
       round(100.0 * avg((next_search IS NOT NULL AND next_search < created_at + interval '2 minutes'
              AND NOT EXISTS (SELECT 1 FROM callrecord c WHERE c.org_id = s1.org_id AND c.user_email = s1.user_email
                              AND c.created_at BETWEEN s1.created_at AND s1.next_search))::int), 1) AS requery_pct
FROM s1 GROUP BY 1, 2 ORDER BY 1, 2;
