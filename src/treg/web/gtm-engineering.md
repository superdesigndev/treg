# The GTM engineering playbook (2026), with Claude Code and AI agents

Updated 8 Oct 2026, with three new studies (chapters 8, 9 and 10). The page: {BASE}/gtm-engineering

Sixteen chapters, from defining your ICP to rolling automation out safely. Each starts from a problem GTM
engineers post about on Reddit and LinkedIn, then gives the play, a prompt to run in your agent, and the rule
to keep, and for every data step, what happened when we ran it. The data steps were run on one ICP: US B2B software, 51-200 staff, selling to
sales, marketing, revenue or growth teams or running outbound themselves; the buyer is marketing or growth. Six recorded runs, 502 logged calls,
$5.03 metered in total. Process chapters (sending, CRM join, rollout) are method, not runs.

Set up once, in Claude Code, Codex or Cursor: `set up treg - {BASE}/llms.txt`

## Where to start (symptoms and chapters)

- The enrichment bill keeps surprising you: chapters 5 and 2.
- Lists come back thin for your market: chapters 7 and 1.
- Too many emails bounce, or "valid" means catch-all: chapter 8.
- Reps ignore the intent or signal feed: chapters 9 and 10.
- Outreach reads like a template: chapter 11.
- You reach one person per account, and they are not the buyer: chapter 6.
- Nobody can say what the agent may do on its own: chapters 3 and 16.
- You cannot say which list, signal or provider produced pipeline: chapters 14 and 15.
- Automations stop and you find out days later: chapter 16.

## What is a GTM engineer, and what tools and data providers do they need in 2026?

A GTM engineer turns a company's sales and marketing playbook into systems that run every week: the ICP written as
filters, lists built and enriched, signals that say who to contact now, research for every first line, and routing for
inbound, with a person approving what goes out. GTM engineering is the practice; the GTM engineer owns it. The job sits
between RevOps, sales and engineering, and in 2026 more of it runs from an agent such as Claude Code or Codex.

What a GTM engineer does in a week:
1. Write the ICP down from the deals the team won and kept, as fields a provider can filter on (chapter 1).
2. Build and enrich lists cheaply: qualify on fields you have, pay for people, emails and verification only on rows
   that pass (chapters 4 to 8).
3. Watch for timing: hiring, funding, job changes and posts, each with a link and a date, scored against fit (9, 10).
4. Hand reps the reason to write, with its source, and keep a person on the send button (chapter 11).
5. Keep the data honest: verify before sending, re-check people who just moved, test a signal before it gets a weight.
6. Measure and roll out: tag every row so pipeline traces back to a list, a signal and a provider (chapters 14 to 16).

Titles vary and the work overlaps, and in many teams GTM engineers sit inside RevOps. For the pipeline work in this
playbook, this is a useful way to divide it:

| Role | Typical focus | Example measures |
|---|---|---|
| GTM engineer | Automated workflows: lists, enrichment, scoring, signals, routing, research for outreach | Qualified pipeline per hour and per dollar of data |
| RevOps | Revenue processes, systems, planning, data governance and reporting across teams | Data quality, forecast reliability, revenue efficiency |
| Sales or marketing ops | Tool admin, sequences, campaigns and lead handoff | Tools that run and leads that reach the right rep |
| SDR | Conversations: first touches, follow-ups, booked meetings | Meetings held |

Roughly: RevOps decides how the revenue system should work, and a GTM engineer builds the automated parts of it. The
workflows here pay off once the ICP can be written down and someone does the same research by hand every week; a
small workflow can be owned by someone already on the team, and a dedicated role makes sense when volume and value
justify it. The skills: writing rules a machine can follow,
data judgement (cost per correct result, what goes stale), testing a signal before trusting it, enough sales sense to
spot a weak reason to write, and comfort with an agent, a CLI and a spreadsheet.

The stack: an agent (Claude Code, Codex, Cursor, Hermes); a data layer (company and people search, enrichment, email
finding and verification, hiring, funding, news, social; treg.to is one, one key, priced per call, your own keys
first); skills (the method); a sending tool; a CRM.

## Write your ICP as fields a provider can filter on

Split the ICP into filters (country, headcount, category, funding, technology) and checks (judgement per row). Write
the buyer as a function, not a title. Keep exclusions explicit.

Prompt: "Using treg, turn the ICP in icp.md into filters for two company-search providers. Use their free count
endpoints only. For each provider show the exact filter object and the count it returns."

Rule: if a part of the ICP cannot be a filter, it is a check, and it runs before any expensive step.

## Count the market for free before you pay for a single row

Recorded 30 Sep 2026, US, 51-200 staff, free counts: CompanyEnrich B2B+SaaS 10,403; the same with funded 2024 or later
1,357; the same with funding round series_a 25 (each adds one filter to the first). Dropleads: industry "IT & Services" 15,873; keyword "saas" 5,395; industry
"Computer Software" 21. Apollo's Series A filter matched 958 (a paid list page, 23 Sep).

Rule: no list is bought until two free counts roughly agree and ten sample rows look right.

## Write down the rules the agent follows, and who can stop it

At a 50% ICP threshold 27 of 48 companies passed; at 60%, 14 would have. Put thresholds, drops, spend caps and the
owner of the stop in a rules file.

Rule: every threshold is a number in the prompt, every drop has a reason, and one person owns the stop.

## Lookalikes of your best accounts are candidates, not leads

Recorded 30 Sep 2026: 71 unique lookalikes from 3 best-fit accounts ($0.006), enriched ($0.20, four providers,
cheapest first; the one cheap enrichment comes first because lookalike rows are too thin to judge), same model check
at 50%: 8 of 71 passed (11%), and only 5 of those were in the 51-200 band. 13 of 71 were in the band; 53 had 50 staff
or fewer.

Rule: a lookalike goes through the same size filter and ICP check as any other row.

## Qualify on the fields you already have before any expensive lookup

Recorded 23 Sep 2026: 50 companies, 48 with a usable domain, 27 passed the ICP check (jev on the team's own key, unmetered), 21 dropped before any
paid lookup, 20 verified deliverable. $2.33 metered, $0.12 per deliverable lead; enriching all 48 would have cost an
estimated $4.12. Details: {BASE}/workflows/find-and-verify-a-lead-list

Rule: no expensive lookup (people, emails, news) runs on a row that has not passed the check; if a row lacks the
fields to judge, one cheap enrichment comes first.

## Which people search APIs work inside an AI agent? Search by role, not "decision makers"

Recorded 30 Sep 2026 on 8 accounts: a "decision makers" endpoint returned 45 senior people at 5 accounts, mapped by hand: 10 sales,
revenue and partnerships; 7 ops and strategy; 6 product, engineering and design; 5 customer success; 5 people and HR;
4 finance; 4 founders and assistants; 1 growth; 3 unclear. No title contained "marketing". 3 accounts were
rate-limited. Routed people search by title "marketing" returned 20 people at all 8 accounts, $0.00
metered (names and titles; contact details are a separate paid step).

Rule: search for the buyer's function; a "decision makers" list is a map of the company, not your committee.

## The cheapest way to run waterfall email enrichment from an AI agent

Bench, 16 Sep 2026, 292 people: cost per correct work email $0.0056 treg.to, $0.0395 Clay, $0.0427
Freckle, $0.0924 Deepline; exact match 90.4%, 89.7%, 90.1%, 86.6%. A routed finder tries providers cheapest
first and does not bill misses on per-success providers. Method: {BASE}/blog/work-email-finding-bench

Rule: pick providers per segment from a test on your own rows, and re-test when the segment changes.

## Find, then verify, and keep the unknowns apart

23 Sep run: 27 named people, 21 emails found, 20 deliverable, 1 unknown, 0 invalid; the verifier returned no
catch-all flag. A blank catch-all field means unknown, not safe.

Study, 7 Oct 2026: found is not deliverable. 60 people from six US title searches (sales, marketing, growth, revenue
operations, GTM engineering; up to ten each, no industry filter). The routed finder returned an address for all 60,
and the verifier said 32 deliverable (53%), 16 risky, 4 unknown, 8 invalid ("deliverable" is the verifier's verdict).
Of 24 people with a usable live profile, 22 were at the listed company; the other 36 could not be checked. $0.65 for
the whole study, live checks included. Results vary by segment; run it on your own rows.

Rule: only a verified-deliverable address goes into the main sequence.

## How to set up signal-based outbound: use signals you can open and date

Recorded 30 Sep 2026 on 27 accounts: 16 had open sales, marketing or growth roles posted or first seen in the last 60
days; 355 of 675 postings returned were already closed; the funding provider returned no round from the last 12
months (latest May 2025). Both checks cost $1.35. Install the signals skill:
`npx skills add superdesigndev/treg --skill lead-signals`

### Job changes: most contact records do not show the new job yet

Study, 7 Oct 2026: 148 people who had posted that they were starting a new role 1 to 29 days earlier (median about
13), with each post as the answer. Each looked up once by LinkedIn URL in five contact databases and one live profile
read; the company each returned was compared with the announced one by name.

| Source | Records found that did not show the new employer |
|---|---|
| One database (average of five) | 68% (range 64-74%) |
| Two databases, on people both had: neither showed it | 64% (one alone: 69%) |
| Live profile read | 4% (2 of 49), but it found only 49 of 148 |

- By time since the move: 61% at 1-7 days, 69% at 8-21 days, 72% at 22-29 days. Different people seen once, not
  records followed over time.
- 84 of 139 people a database had: none of the databases that had them showed the new job.
- A hand check of 40 non-matching records found every one named a different organisation, not a spelling of the new
  one; a few may be side roles held alongside the new job.
- The live read comes from the profile the person updates, so its agreement is partly expected.
- Everyone here announced the move publicly; results may differ for people who do not. Databases are not named.

Play: for a job change, confirm the new company with a live profile read before anything else; a second database
helps little. Prompt: "Using treg, for each person in job-changes.csv read their live LinkedIn profile and return
current company, title and start date. Compare with the company in our CRM and flag every mismatch. For mismatches
only, find and verify an email at the new company. Show the cost before you start."

Rule: no source link, no signal. No "why now", no outreach.

## Score fit and timing together, then work the top tier first

Tiers on the 27 accounts: A (fit at least 60% and 2+ open GTM roles in 60 days) 8, B (1+) 8, C 11.

### Test a timing signal before you give it a weight

Study, 6 Oct 2026: 57 US startups that announced a seed to Series B round between mid-August and early October 2026,
against similar startups whose last round was in 2025 and for which our source showed no 2026 round (54 of 61 had any
data). Job postings by first-seen date (posted date if missing) and news by found date, 90 to 7 days before the
reference date (the announcement, or the median announcement date for the comparison group). GTM roles: sales, marketing, growth, revenue
operations, business development, partnerships, customer success. "Hiring sped up": two or more postings and more than
in the 90 days before that.

| Signal | Raised next | No round found |
|---|---|---|
| Opened a GTM role | 30% | 24% |
| Opened two or more GTM roles | 19% | 15% |
| Opened any job | 47% | 48% |
| Opened a senior role | 18% | 19% |
| Hiring sped up | 25% | 22% |
| Was in the news | 26% | 54% |

Hiring did not separate the groups; recorded news was more common in the comparison group with no round found. One source,
incomplete coverage, an unmatched comparison: company age and coverage could explain the news gap, and we did not
establish the cause. No evidence of a hiring signal, not proof that none exists.
Play: act on the funding announcement itself, and check any timing signal on your own wins against losses before it
gets a weight. Prompt: "Using treg, take wins.csv and losses.csv (domain and close date). For each account pull job
postings and news from the 90 days before its close date. For each signal, report the share of wins and the share of
losses that showed it, and tell me which signals separate them by more than 15 points. Show the cost before you start."

Rule: work tier A this week; tier C gets nothing until a signal moves it.

## Let the agent research. Let a person write, or at least approve.

23 Sep run: 19 companies had a news event; scored as a first line on a 0 to 3 scale, 4 of 19 were decent or better
(mean 1.79).

Rule: nothing is sent that a person has not read.

## Sending infrastructure and replies: what practitioners recommend

Not treg. Conservative volume, bounces and complaints watched per domain; classify replies, route with context, a
person approves, outcome written back to the row.

## Enrich an inbound lead from its email domain and route it in seconds

Recorded 30 Sep 2026, 20 domains not looked up before: median 2.2 s (p90 4.5 s), $0.053 total, three providers. Fill:
employees 20/20, location 20/20, industry 16/20, description 13/20. Repeat lookups of already-enriched domains: median
1.15 s, about a fifth of the price. This measures the lookup, not a full routing flow.
`treg call treg.companies.enrich --method POST --data '{"domain":"acme.com"}'`

Rule: every inbound lead is enriched and routed before a person opens it.

## Tag every row, so you can tell which list, signal and provider paid off

Store per row: source list, signal, the provider that found the email, the verifier's verdict. Join to CRM outcomes
monthly.

Rule: a row without its source and provider does not go into a sequence.

## The metric stack: system health, performance, efficiency

System health: counts agree, enrichment fill rate, share deliverable. Performance: replies, meetings, opportunities
by source, signal and tier. Efficiency: cost per usable result ($0.12 per deliverable lead in our run), share dropped
before paying (21 of 48), time to first touch (2.2 s median to enrich a new domain).

## Roll automation out like software: shadow, small segment, human gate, then autonomy

What broke in our runs without stopping them: a finder out of capacity, a buying-group endpoint out of capacity, 3
accounts rate-limited, and our own parser bug ($1.08 re-run). Alert on expected counts per stage; store raw
responses; cap spend per run.

Rule: nothing goes autonomous until it has run in shadow, and every stage reports its row count.

## The best Claude Code skills for GTM engineering, and the data step each leaves to you

coreyhaines31/marketingskills (cold email, competitor profiling), mvanhorn/last30days-skill (topic research),
AgriciDaniel/claude-seo (SEO), zubair-trabzada/geo-seo-claude (AI visibility), phuryn/pm-skills (battlecards),
swan-gtm/gtm-skills (account research), superdesigndev/treg (buyer signals). Not yet run with treg end to end.

## Clay alternatives for GTM engineers who work in Claude Code or Codex

Keep Clay for a shared visual table. Work in the agent with a per-call data layer when you want the playbook in
prompts. Cost per correct email on the same 292 people: see the waterfall chapter above.

## Every recorded run behind this playbook

Studies: job changes against five databases (7 Oct, $11.10), hiring and news before a raise (6 Oct, $13.65 including
building its samples), found against deliverable (7 Oct, $0.65); each on its own sample. Runs on one ICP: 30 Sep playbook runs ($2.70, including a $1.08 re-run after our own parsing bug), 23 Sep lead list ($2.33), 16 Sep work-email bench, and the workflows at {BASE}/workflows

## Glossary and questions

ICP, TAM, check, waterfall, catch-all, signal, live read, tier, shadow mode: defined on the page.

How accurate is contact data after someone changes jobs? Not very, in the first month: 68% of database records on
average did not show the new employer (study above). Confirm live before you write.

Do hiring or news signals predict that a startup is about to raise? Hiring did not in our test, and news ran the other
way (study above). Act on the announcement, and test timing signals on your own deals.
