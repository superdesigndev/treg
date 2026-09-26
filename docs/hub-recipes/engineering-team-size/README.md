# engineering-team-size

Estimates how many engineers a company employs, from its domain. Ported from a public
cost-ordered ladder: a free identity resolution first, then the cheapest provider that answers.

1. `pdl.x.company-clean` (free) resolves the domain to a canonical LinkedIn company URL: the
   join key for the next tier, because a domain alone matches the wrong entity often enough.
2. `crustdata.companies.enrich` with `fields: ["headcount"]`, by that URL, then by domain:
   `headcount.by_role_absolute.Engineering`, the company total, and the Engineering YoY growth.
3. `pdl.companies.enrich`, only when tier 2 gave nothing: `employee_count_by_class.research_and_development`.
   That class field is present only on PDL plans that carry it; on a key without it this tier
   answers nothing and the run reports `no_estimate`.

`method` picks the ladder: `cost_optimized` (default), `crust_only`, `pdl_only`, or `validate`
(run every tier and report the disagreement). A zero count is never trusted as "no engineers".
`confidence` is `strong` when two tiers agree within 25%, `medium` with one answer, `low` when a
review reason fired (density over 60% or 100% of headcount, providers disagree, no canonical URL),
`none` without an estimate. `review_reasons` and `provider_attempts` show why.

Priced `per_call`: $0.15 to the maker per successful run, on top of the provider fees (CrustData
about $0.60 a match; PDL enrich $0.38 when it runs).
