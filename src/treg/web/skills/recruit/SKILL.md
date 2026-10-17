---
name: recruit
description: Use when the user wants candidates for a role: they paste a LinkedIn job post (or a job description) and want the people who fit it, ranked, with a reason for each and a personal email and phone to reach them. Also for "source candidates", "find people for this job", "build a shortlist", or turning a job post into a contactable candidate list.
---

# Recruit

A job post in, a ranked shortlist out: people who fit, one line on why, and a way to reach them.
Every stage is counted and every contact lookup leaves a vendor trail, written to a run file.

Runs on treg. If it isn't set up (`treg --version` fails), read {BASE}/llms.txt and follow it to
install and sign in, then come back here.

## The run

1. **Read the job.** `treg catalog search "linkedin job details"`, then call the best row with the
   post URL. No post URL: use the text the user pasted.
2. **Extract criteria** from the post, yourself: title variants (the title as posted plus two to
   four names the same job goes by), must-have skills, seniority, locations (and whether remote is
   fine), and the hiring company, which is always excluded. Show the criteria to the user in five
   lines before spending; adjust if they object.
3. **Search** with the routed people search, one call per title variant (and per location when there
   are several):

   ```
   treg call treg.people.search --data '{"title":"Staff Backend Engineer","location":"Berlin, Germany","country":"DE","keywords":["go","kafka"],"limit":25}'
   ```

   `limit` is the price dial: most providers bill per row. Start at 25 per query; raise it only when
   the funnel comes out thin. Read `_treg.ignored_filters`: a provider that could not apply a filter
   answered a looser question, so those rows get the hard filters below with no benefit of the doubt.
4. **Dedupe** by LinkedIn URL (lowercase, no query string, no trailing slash); rows without one by
   name + company.
5. **Hard filters.** Drop, and record a reason for each drop: works at the hiring company, wrong
   location, seniority too far off, title from another job family, no profile URL to contact from.
6. **Rank**, yourself: read each survivor against the criteria and write one line, in the user's
   words, on why they fit ("8 yrs Go at a payments scale-up, led a Kafka migration"). No line you
   can write, no rank. Keep the top N (default 20).
7. **Evidence** (optional, cheap), for the top `E` of them (default 10). Gather cited facts, then
   re-score: a claim the evidence backs moves a candidate up, one it contradicts moves them down.
   Cap each candidate's evidence spend (default $0.05) and stop at the cap. Skip a source when the
   person has nothing for it (no GitHub handle, no posts); never guess a handle.

   | Evidence | Call | Use it for |
   | --- | --- | --- |
   | Recent LinkedIn posts | `anyapi.linkedin.profile_posts_full` with `{"url": "<profile>", "limit": 5}` (or `_thin`, whichever `treg catalog get` prices lower) | what they work on now, in their words |
   | GitHub | `treg catalog search "github user profile"` for the user, then `crawl4ai.github.repo.profile` on their top repos | languages, stars, repos that match the job's skills |
   | Talks and writing | `anyapi.youtube.search` and `anyapi.google.scholar`, query `"<name>" <topic>` | talks and papers on the job's topic; keep only results that name them |
   | Open to move | `datagma.people.job-change.detect` (`fullName`, `companyName`, `jobTitle`); tenure from the search row; `akta.companies.news` once per current company, shared by everyone there | a recent move (less likely to move again), long tenure, layoffs or a funding gap at their company |

   Read each answer before you cite it: a search result that does not name the person is not
   evidence. The posts feed mixes in other people's posts; count only items whose `author.handle`
   is the profile's own, from the last six months. The job-change check answers a `status`
   (`NO_NEW_COMPANY` means still there). When a source refuses (a provider out of capacity), skip
   it for the run; its refusal names alternatives, which you may use if they fit under the cap. `treg catalog get <id>` first if a call is new to you: the catalog changes, and the
   price and parameters on the row win over this table.
8. **Contact**, for the top N only:

   ```
   treg call treg.people.email.find.personal --data '{"linkedin_url":"…"}' --header 'X-Treg-Route-Verify: true' --header 'X-Treg-Route-Max-Cost: 0.30'
   treg call treg.people.phone.find --data '{"linkedin_url":"…"}' --header 'X-Treg-Route-Verify: true' --header 'X-Treg-Route-Max-Cost: 0.30'
   ```

   Both are routed: treg tries providers cheapest per hit first, most misses are free, and the
   verify header checks a hit in the same call (the mailbox; for a phone, that the line is live).
   Send only the LinkedIn URL: it is what every provider takes. Never re-send a lookup for the same
   person; every hit bills, repeats included.
9. **Hand back** the shortlist and the run file, and state coverage plainly: "214 found, 61 kept,
   top 20: 14 emails, 9 live mobiles, $4.12".

## The run file

Write `recruit-run-<YYYYMMDD-HHMM>.json` next to the user's work, and update it after every stage,
so a stopped run still says what it did. Money comes from the answers, never from a price list:
`_treg.charged_micro` on each call (micro-USD), `_treg.tried` for the vendors asked, and
`_treg.verification.cost_micro` for the check.

```json
{
  "job": {"url": "…", "title": "…", "company": "…", "company_domain": "…", "location": "…"},
  "criteria": {"titles": ["…"], "skills": ["…"], "seniority": "senior", "locations": ["…"],
               "remote_ok": false, "exclude_companies": ["…"]},
  "stages": [
    {"stage": "job",     "calls": 1, "charged_usd": 0.0},
    {"stage": "search",  "calls": 6, "rows": 214, "charged_usd": 0.0},
    {"stage": "dedupe",  "in": 214, "out": 163},
    {"stage": "filter",  "in": 163, "out": 61,
     "reject_reasons": {"hiring company": 9, "location": 52, "seniority": 27, "job family": 14},
     "reject_samples": [{"name": "…", "linkedin_url": "…", "reason": "location: Munich"}]},
    {"stage": "rank",    "in": 61, "out": 20},
    {"stage": "evidence", "people": 10, "cap_usd": 0.05, "charged_usd": 0.0,
     "by_source": {"linkedin_posts": {"tried": 10, "found": 7, "charged_usd": 0.0}}},
    {"stage": "contact", "people": 20, "emails": 14, "phones": 9, "charged_usd": 0.0}
  ],
  "candidates": [
    {"rank": 1, "name": "…", "title": "…", "company": "…", "location": "…", "linkedin_url": "…",
     "why_fit": "…", "score_before": 7, "score_after": 8,
     "evidence": [
       {"type": "linkedin_posts", "claim": "…", "source": "anyapi.linkedin.profile_posts_full",
        "url": "…", "charged_usd": 0.0}
     ],
     "evidence_usd": 0.0,
     "email": {"value": "…", "verdict": "valid"},
     "phone": {"value": "…", "verdict": "live"},
     "reach": [
       {"lookup": "email", "endpoint_id": "…", "vendor": "…", "outcome": "miss", "charged_usd": 0.0},
       {"lookup": "email", "endpoint_id": "…", "vendor": "…", "outcome": "hit", "charged_usd": 0.0},
       {"lookup": "email_check", "endpoint_id": "…", "verdict": "valid", "charged_usd": 0.0},
       {"lookup": "phone", "endpoint_id": "…", "vendor": "…", "outcome": "hit", "charged_usd": 0.0},
       {"lookup": "phone_check", "endpoint_id": "…", "verdict": "live", "charged_usd": 0.0}
     ]}
  ],
  "totals": {"charged_usd": 0.0, "emails": 14, "phones": 9}
}
```

- `evidence` holds only what a call returned: `type` (`linkedin_posts`, `github`, `talks`,
  `papers`, `job_change`, `tenure`, `company_news`), a one-line `claim`, the `source` endpoint id
  (`search_row` for tenure), the `url` when there is one, and that call's `charged_usd`. A shared
  company-news call is charged once, to the first candidate who used it.
- `reach` is one entry per row of `_treg.tried` (`endpoint_id`, `provider` as `vendor`, `outcome`,
  `charged_micro` / 1e6 as `charged_usd`), then one for `_treg.verification` when a check ran
  (`served_by`, `verdict`, `cost_micro`). A skipped attempt keeps its `outcome: skipped`.
- `email` / `phone` are `null` on a miss. `verdict` is the check's own word: an email `valid`,
  `invalid`, `catch_all`, `risky` or `unknown`; a phone `live`, `dead` or `unknown`. A live line is
  not proof it is this person's number; never call a phone "verified".
- `reject_samples`: up to five per reason, so the reasons can be checked by eye.
- `totals.charged_usd` is the sum of every `charged_usd` above: job, search, evidence and contact.

## Before the first list

Say once: contacting people in the EU and UK needs a lawful basis, and anyone who asks to be left
alone is dropped for good. Then hand over the list; the user decides how to use it.
