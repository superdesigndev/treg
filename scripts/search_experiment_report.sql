-- The discovery experiment, read from the database (application/search_experiment.py).
--
-- The outcome of a search is what the caller did next: a `call` by the same team and person, within
-- ten minutes, to an endpoint that was on the page. `searchlog.shown` is the page as served, with
-- each row's owner; `callrecord` (audit) carries the call. No labels anywhere — the join IS the label.
--
-- Run against the read replica:  psql "$TREG_READ_DATABASE_URL" -f scripts/search_experiment_report.sql
-- Postgres only (jsonb functions). Every block is read-only.

\set window '30 days'
\set followup '10 minutes'

-- 1. Volume and health: how many searches, how often the pages differ, what the judge cost.
--    `differs` is the population the experiment can say anything about; an identical page is a
--    query the judge did not change. Read this block first — a low `differs` share means little to
--    win, a high `judge_error` share means the effect below is really a fallback rate.
SELECT mode, arm,
       count(*)                                        AS searches,
       round(100.0 * avg(differs::int), 1)             AS differs_pct,
       round(100.0 * avg((baseline_total = 0)::int), 1) AS baseline_empty_pct,
       round(100.0 * avg((judge_error IS NOT NULL)::int), 1) AS judge_error_pct,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY judge_ms) AS judge_ms_p50,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY judge_ms) AS judge_ms_p95,
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
  WHERE s.created_at > now() - :'window'::interval AND s.mode = 'interleave' AND s.org_id IS NOT NULL
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

-- 3. Interleaving credit (the paired comparison). For each interleaved search whose caller went on
--    to call a shown endpoint, the point goes to the ranker that put it there — only where the
--    pages DISAGREE: a row only one page carried, or one they ranked differently (the higher rank
--    wins). Rows both pages carried at the same rank are ties and count for nobody. Under the null
--    hypothesis the two credits are a fair coin; the last column is the two-sided binomial z.
--    Membership comes from the served page's per-row `owner` (baseline | judged | both), which is
--    exact for every row including routed parents; the rank comparison for a `both` row reads the
--    two page lists and calls it a tie when the judged rank is unknown (a routed parent on rows
--    written before parents were listed in `judged`).
WITH il AS (
  SELECT s.id, s.org_id, s.user_email, s.created_at,
         ARRAY(SELECT jsonb_array_elements_text(s.baseline_ids::jsonb)) AS base_ids,
         ARRAY(SELECT jsonb_array_elements(s.judged::jsonb)->>0)        AS judged_ids,
         s.shown::jsonb                                                 AS shown
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = 'interleave' AND s.arm = 'interleave'
    AND s.judged IS NOT NULL AND s.differs AND s.org_id IS NOT NULL
),
first_call AS (
  SELECT DISTINCT ON (il.id) il.id, il.base_ids, il.judged_ids, c.endpoint_id,
         (SELECT e->>1 FROM jsonb_array_elements(il.shown) e WHERE e->>0 = c.endpoint_id LIMIT 1) AS owner
  FROM il JOIN callrecord c
    ON c.org_id = il.org_id AND c.user_email = il.user_email
   AND c.created_at BETWEEN il.created_at AND il.created_at + :'followup'::interval
   AND c.endpoint_id IN (SELECT e->>0 FROM jsonb_array_elements(il.shown) e)
  ORDER BY il.id, c.created_at
),
points AS (
  SELECT CASE
           WHEN owner IN ('baseline', 'judged') THEN owner
           WHEN array_position(base_ids, endpoint_id) < array_position(judged_ids, endpoint_id) THEN 'baseline'
           WHEN array_position(judged_ids, endpoint_id) < array_position(base_ids, endpoint_id) THEN 'judged'
           ELSE 'tie'
         END AS credit
  FROM first_call
)
SELECT sum((credit = 'judged')::int)   AS judged_wins,
       sum((credit = 'baseline')::int) AS baseline_wins,
       sum((credit = 'tie')::int)      AS ties,
       (SELECT count(*) FROM il)       AS interleaved_searches_with_disagreement,
       round((
         (sum((credit = 'judged')::int) - sum((credit = 'baseline')::int))
         / sqrt(nullif(sum((credit IN ('judged', 'baseline'))::int), 0)))::numeric, 2) AS z
FROM points;

-- 4. Re-query rate: a second search by the same caller within two minutes with no call in between
--    is a page that did not do its job. Lower is better; compare across arms.
WITH s1 AS (
  SELECT s.*, lead(s.created_at) OVER (PARTITION BY s.org_id, s.user_email ORDER BY s.created_at) AS next_search
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = 'interleave' AND s.org_id IS NOT NULL
)
SELECT arm, count(*) AS searches,
       round(100.0 * avg((next_search IS NOT NULL AND next_search < created_at + interval '2 minutes'
              AND NOT EXISTS (SELECT 1 FROM callrecord c WHERE c.org_id = s1.org_id AND c.user_email = s1.user_email
                              AND c.created_at BETWEEN s1.created_at AND s1.next_search))::int), 1) AS requery_pct
FROM s1 GROUP BY 1 ORDER BY 1;

-- 5. The same numbers per CALLER. A few agents search hundreds of times an hour (one scanned the
--    catalog with 579 distinct queries in one hour, another looped 250 searches over 74 queries),
--    and in a small arm they ARE the arm: the raw rates in blocks 2 and 4 swung by 20 points as
--    those callers came and went. Averaging per caller first (every caller weighs one) and
--    dropping the three heaviest callers per arm are two different corrections; read both.
WITH pages AS (
  SELECT s.id, s.arm, s.org_id, s.user_email, s.created_at,
         ARRAY(SELECT jsonb_array_elements(s.shown::jsonb)->>0) AS shown_ids
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = 'interleave' AND s.org_id IS NOT NULL
    AND s.baseline_total > 0),
conv AS (
  SELECT p.arm, p.user_email,
         (EXISTS (SELECT 1 FROM callrecord c WHERE c.org_id = p.org_id AND c.user_email = p.user_email
                  AND c.created_at BETWEEN p.created_at AND p.created_at + :'followup'::interval
                  AND c.endpoint_id = ANY (p.shown_ids)))::int AS converted
  FROM pages p),
per_caller AS (SELECT arm, user_email, count(*) AS n, avg(converted) AS rate FROM conv GROUP BY 1, 2),
ranked AS (SELECT *, row_number() OVER (PARTITION BY arm ORDER BY n DESC) AS rk FROM per_caller)
SELECT arm, count(*) AS callers, max(n) AS heaviest_caller_searches,
       round(100.0 * avg(rate), 1) AS conversion_per_caller_pct,
       round(100.0 * sum(n * rate) FILTER (WHERE rk > 3) / nullif(sum(n) FILTER (WHERE rk > 3), 0), 1)
         AS conversion_without_top3_pct
FROM ranked GROUP BY 1 ORDER BY 1;

-- 6. Interleaving credit spread: the paired result is only as good as the number of DIFFERENT
--    callers behind it. `callers_favouring_judged` counts callers whose own points lean that way.
WITH il AS (
  SELECT s.id, s.org_id, s.user_email, s.created_at,
         ARRAY(SELECT jsonb_array_elements_text(s.baseline_ids::jsonb)) AS base_ids,
         ARRAY(SELECT jsonb_array_elements(s.judged::jsonb)->>0)        AS judged_ids,
         s.shown::jsonb                                                 AS shown
  FROM searchlog s
  WHERE s.created_at > now() - :'window'::interval AND s.mode = 'interleave' AND s.arm = 'interleave'
    AND s.judged IS NOT NULL AND s.differs AND s.org_id IS NOT NULL),
first_call AS (
  SELECT DISTINCT ON (il.id) il.id, il.user_email, il.base_ids, il.judged_ids, c.endpoint_id,
         (SELECT e->>1 FROM jsonb_array_elements(il.shown) e WHERE e->>0 = c.endpoint_id LIMIT 1) AS owner
  FROM il JOIN callrecord c
    ON c.org_id = il.org_id AND c.user_email = il.user_email
   AND c.created_at BETWEEN il.created_at AND il.created_at + :'followup'::interval
   AND c.endpoint_id IN (SELECT e->>0 FROM jsonb_array_elements(il.shown) e)
  ORDER BY il.id, c.created_at),
points AS (
  SELECT user_email, CASE
           WHEN owner IN ('baseline', 'judged') THEN owner
           WHEN array_position(base_ids, endpoint_id) < array_position(judged_ids, endpoint_id) THEN 'baseline'
           WHEN array_position(judged_ids, endpoint_id) < array_position(base_ids, endpoint_id) THEN 'judged'
           ELSE 'tie' END AS credit
  FROM first_call),
per_caller AS (SELECT user_email, sum((credit = 'judged')::int) AS j, sum((credit = 'baseline')::int) AS b FROM points GROUP BY 1)
SELECT count(*) AS callers_with_points,
       sum((j > b)::int) AS callers_favouring_judged, sum((b > j)::int) AS callers_favouring_baseline, sum((j = b)::int) AS even,
       max(j) AS most_judged_points_from_one_caller,
       round(100.0 * max(j) / nullif(sum(j), 0), 1) AS top_caller_share_of_judged_points_pct
FROM per_caller;
