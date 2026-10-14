# company-consensus

One company's details from two independent sources, and whether they agree.

## What it does

1. Asks two providers (three if you name them) for the company at `domain`, all at the same time.
   Default: `thecompaniesapi` and `hunter`. `dropleads` is the third choice.
2. Maps each answer onto the same nine fields: name, domain, website, description, industry,
   employees, founded, location, linkedin_url.
3. Compares field by field. Exact fields (domain, founded, employees, linkedin, website) are compared
   in code; employee counts are compared by size band, so "8000" and "5k-10k" agree. Text fields
   (name, industry, location) are judged by Jev in one call: "do these two values describe the
   same thing?", each field on its own. "fintech" and "financial services" agree; "restaurants" and
   "software" do not.
4. Returns one merged `company` record (the first source's value where both exist), an `agreement`
   verdict per field (`agree`, `disagree`, `one_source`, `none`; `description` is `not_compared`), and a `confidence`: the share of
   compared fields that agree.

## What it costs

- The provider calls at cost: about $0.007 for the two defaults, $0.009 with dropleads.
- One judgment call: about $0.00002.
- Plus 30% of the provider calls as the tool's fee (`ctx.charge(fees * 0.3, ...)`), at most $0.05.

## Why use this instead of routing

`treg.companies.enrich` returns the first provider that answers and never asks a second. When you
need to know whether the record is right, not only that it exists, this is the second opinion.

## Limits

- A source that does not answer in 40 seconds is reported as `timed_out`; the others still count.
- With only one source answering there is nothing to compare: `confidence` is null and the summary
  says so.
- `judged_by` says whether Jev decided the text fields or a loose text match did (when the
  judgment call failed).
