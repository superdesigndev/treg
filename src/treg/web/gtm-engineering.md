# The GTM engineering playbook (2026), with Claude Code and AI agents

Updated 26 Nov 2026: sections marked "From practitioners" added from about 1,280 LinkedIn posts, X posts and YouTube
videos from the two months before, plus the transcripts of the 20 most-watched videos; giveaway posts left out. They
are other people's advice, not our runs. Sections marked "From open-source skills" distil ten public skill libraries;
their numbers are theirs. Three studies of our own added 8 Oct 2026 (chapters 8, 9 and 10). The page: {BASE}/gtm-engineering

Sixteen chapters, from defining your ICP to rolling automation out safely. Each starts from a problem GTM
engineers post about on Reddit and LinkedIn, then gives the play, a prompt to run in your agent, and the rule
to keep, and for every data step, what happened when we ran it. The data steps were run on one ICP: US B2B software, 51-200 staff, selling to
sales, marketing, revenue or growth teams or running outbound themselves; the buyer is marketing or growth. Six recorded runs, 502 logged calls,
$5.03 metered in total. Process chapters (sending, CRM join, rollout) are method, not runs.

Set up once, in Claude Code, Codex or Cursor: `set up treg - {BASE}/llms.txt`

## What is GTM engineering?

GTM engineering is the practice of designing, building and maintaining systems that help sales and marketing teams turn their go-to-market strategy into repeatable workflows. Examples include account targeting, lead enrichment, scoring, signal monitoring, research and inbound routing. A GTM engineer connects data and tools, tests the workflows and measures their effect on pipeline. The work often overlaps with RevOps, sales ops and engineering. This playbook shows how to build those workflows with Claude Code or Codex, with human review before outbound messages are sent.

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
- The agent writes confident nonsense about your own company: chapter 3.
- Everyone who likes a post lands in a sequence, customers included: chapter 9.
- A campaign is dead and nobody knows which part failed: chapter 12.

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

### Keep a tool only if

From practitioners, Nov 2026 (their posts and videos, not our runs).

Teams start with two tools and end with 29. (Jani Vrancsik)

- **The agent can run it.** One instruction, no clicking. (Alex Vacca)
- **It has an API or a CLI.** A screen that needs manual uploads is no use to an agent. (Tim Yakubson's guest)
- **It fixes a problem you have today.** Not a shiny one. (Tim Yakubson's guest)
- **It earns its cost.** See chapter 15. (Noëmie J.)
- **You could not build it in an afternoon.** Buy the parts, write the glue. (Asteroid)

## Write your ICP as fields a provider can filter on

Split the ICP into filters (country, headcount, category, funding, technology) and checks (judgement per row). Write
the buyer as a function, not a title. Keep exclusions explicit.

Prompt: "Using treg, turn the ICP in icp.md into filters for two company-search providers. Use their free count
endpoints only. For each provider show the exact filter object and the count it returns."

Rule: if a part of the ICP cannot be a filter, it is a check, and it runs before any expensive step.

### Start from your deals

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **Won against lost.** Export both. Ask the agent what separates them. (Fivos Aresti, Tom Grainger)
- **Who stays.** Rank segments by how long customers stay, not by how many came in. (Patrick Spychalski)
- **Who is out.** Write the exclusions down. (Alex Vacca)
- **Their words.** Pull the pains that repeat from your last 20 sales calls. (@coldemailchris)
- **One list per pain.** A title and an industry say little about the problem. (Alex Vacca)

Prompt: "Read closed-won.csv and closed-lost.csv from our CRM export. List the fields that separate the two groups, with the count on each side. Propose filters, checks and exclusions for icp.md, and mark every line that rests on fewer than ten deals."

### Find the pattern in your customers

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Best quarter against worst.** Rank customers by value. Compare only the top and bottom quarter. Drop anything that looks the same in both. (Anthropic's plugins)
- **Five criteria at most.** "Five is usually enough to find look-alikes; eight finds nobody." (Anthropic's plugins)
- **Under ten customers?** Say there is no pattern yet. (Anthropic's plugins)
- **Hard or soft.** Ask of each criterion: they match everything except this, do you still reach out? Yes means it is a preference, not a filter. (Cold Outbound Skills)

## Count the market for free before you pay for a single row

Recorded 30 Sep 2026, US, 51-200 staff, free counts: CompanyEnrich B2B+SaaS 10,403; the same with funded 2024 or later
1,357; the same with funding round series_a 25 (each adds one filter to the first). Dropleads: industry "IT & Services" 15,873; keyword "saas" 5,395; industry
"Computer Software" 21. Apollo's Series A filter matched 958 (a paid list page, 23 Sep).

Rule: no list is bought until two free counts roughly agree and ten sample rows look right.

### Keep a market map you own

From practitioners, Nov 2026 (their posts and videos, not our runs).

One table, kept by you, with every company in your market. A bought list is the same list your competitors bought.

- **Many sources.** Two databases, lookalikes (chapter 4), keyword search. (Fivos Aresti)
- **Go where buyers list themselves.** Maps for local firms, store databases for shops, listing sites for estate agents, marketplaces for brands. (Michael Saruggia)
- **Fix the labels.** Industry tags are self-reported. Re-label rows with a cheap model. (Matt Lucero)
- **Code touches the rows.** The agent plans; code dedupes and formats. Do not have the agent read rows one by one. (Taylor Haren)
- **One row per domain.** Keep the source on each (chapter 14).
- **No database has them?** Search the web for who owns the company. One sender reports a 78% hit rate; his figure. (@jacobGTM)
- **Segments.** Split by trigger. One campaign per segment. (Deepika)
- **Coverage.** How much of the map have you reached? (Tom Grainger)
- **Refresh.** Re-score every 90 days. (@coldemailchris)

Prompt: "Using treg, build tam.csv for the ICP in icp.md: pull companies from two company-search providers and from lookalikes of the accounts in best-accounts.csv, dedupe on domain, and keep which source found each row. Show the count from each source, the overlap and the cost before you pull any rows."

### Check the count before you trust it

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Pull wide, judge every row.** List the industry labels your customers actually carry. Pull each one. Ask one yes-or-no question per row. One plain banking filter returned 16,895 companies; about 10,500 were not banks. (Cold Outbound Skills)
- **Score against a public register.** Where one exists, match your list to it. Their filter-only list found 79% of registered banks and 31% of registered investment advisers; the judged list found 88% and 92%. (Cold Outbound Skills)
- **Two counts can agree and still differ.** Two sources returned 74 and 60 companies for the same filter, with none in common. Join sources for discovery; do not stop at the first. (Cold Outbound Skills)
- **Coverage gap or small market?** Count with and without the tech filter. Under a tenth means the source lacks the data, not the market. (Explorium's skills)
- **Noise check.** When the count looks off, read about 25 rows. At 20% noise or more, fix the filters before you use the count. (Explorium's skills)

## Write down the rules the agent follows, and who can stop it

At a 50% ICP threshold 27 of 48 companies passed; at 60%, 14 would have. Put thresholds, drops, spend caps and the
owner of the stop in a rules file.

Rule: every threshold is a number in the prompt, every drop has a reason, and one person owns the stop.

### Give the agent a company brain

From practitioners, Nov 2026 (their posts and videos, not our runs).

Rules say what the agent may do. A brain says what your company knows. Without one, the agent "writes confident nonsense". (Alex Vacca)

1. **Text files in a private repo.** Everyone can pull it; every change has a history. No keys in it.
2. **Start with one decision.** The one your team keeps explaining, such as who gets an email this week.
3. **Four verdicts.** Exclude, hold, suppress, proceed, each with a reason. No evidence means hold. A customer or open deal means suppress.
4. **Facts.** True things the agent may say about you. Nothing else is claimed. (Greg Isenberg)
5. **Voice.** Posts you like, posts you hate, words you never use. (Greg Isenberg)
6. **Source order.** CRM first, recent calls second, old decks last.
7. **Real questions.** List the questions the business actually asks. (Justin Joyce, Cloudflare)
8. **A map.** One short file, read every session, that points to the rest.
9. **Two folders.** Company memory is read-only. Each person has their own notes. (Clay's agent team)
10. **No self-edits.** The agent proposes a change; a person confirms it.
11. **Five known accounts.** Write the verdict you expect. Run each three times. A split verdict is a missing rule.

worksheet: brain/

```
CLAUDE.md         the map: what each file is for; read first
icp.md            filters, checks, exclusions (chapter 1)
rules.md          thresholds, spend cap, who can stop it
verdicts.md       exclude / hold / suppress / proceed, with reasons
facts.md          what the agent may say about us
voice.md          posts we like, posts we hate, words we never use
objections.md     objections in the buyer's words, from calls
sources.md        who wins when two sources disagree
questions.md      what the business actually asks
tests/known-5.md  five accounts and the verdict we expect
```

### Two lines to add to rules.md

- **Approval by action.** Updates run. Creates, deletes and sends ask first. (Nic Siegle, Dust, Clay's agent team)
- **One key per person, with a spend alert.** One team burned $8,000 on a single task before anyone saw it. (Alex Vacca)

Brain steps from Alex Vacca and Greg Isenberg; Dima Bilous calls the same thing a context layer. Not one of our recorded runs.

### Let corrections change the rules

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Twice is a rule.** "An override that happens twice is a criteria change, not an exception." (Anthropic's plugins)
- **Write who you turn away.** Keep "does not take on" and "refer to" next to the fit criteria. (Anthropic's plugins)

## Lookalikes of your best accounts are candidates, not leads

Recorded 30 Sep 2026: 71 unique lookalikes from 3 best-fit accounts ($0.006), enriched ($0.20, four providers,
cheapest first; the one cheap enrichment comes first because lookalike rows are too thin to judge), same model check
at 50%: 8 of 71 passed (11%), and only 5 of those were in the 51-200 band. 13 of 71 were in the band; 53 had 50 staff
or fewer.

Rule: a lookalike goes through the same size filter and ICP check as any other row.

### Lookalikes need a judge

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Raw output is a candidate list.** In one measured run 40% of it was usable. (Cold Outbound Skills)
- **Say why the seed fits.** Two to five things you can test, then judge every row against them. (Cold Outbound Skills)
- **Describe the company, not your product.** Or you get your competitors. (Cold Outbound Skills)
- **Feed winners back in.** Stop when a round adds under 2 to 3% new companies. (Cold Outbound Skills)
- **No similarity tool?** Rebuild the seed as filters: industry, size, country, its main tools. Relax them in a fixed order. (Explorium's skills)

## Qualify on the fields you already have before any expensive lookup

Recorded 23 Sep 2026: 50 companies, 48 with a usable domain, 27 passed the ICP check (jev on the team's own key, unmetered), 21 dropped before any
paid lookup, 20 verified deliverable. $2.33 metered, $0.12 per deliverable lead; enriching all 48 would have cost an
estimated $4.12. Details: {BASE}/workflows/find-and-verify-a-lead-list

Rule: no expensive lookup (people, emails, news) runs on a row that has not passed the check; if a row lacks the
fields to judge, one cheap enrichment comes first.

### Tune the check before you scale it

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Ten at a time, with a person.** Correct the verdicts. Lock the prompt after two rounds with no corrections. (Cold Outbound Skills)
- **"Could not look" is not "found nothing".** A failed page load never removes a row. (Cold Outbound Skills)
- **Missing is not a no.** No size data is not a reason to reject. (Cold Outbound Skills)
- **A tech filter is a candidate list.** In one test 4 of 10 companies from a platform filter were not on that platform. Confirm on the live site. (Cold Outbound Skills)
- **No match, no row.** If an email does not resolve to a person, fail it. Do not fall back to a name search. (Explorium's skills)

## Which people search APIs work inside an AI agent? Search by role, not "decision makers"

Recorded 30 Sep 2026 on 8 accounts: a "decision makers" endpoint returned 45 senior people at 5 accounts, mapped by hand: 10 sales,
revenue and partnerships; 7 ops and strategy; 6 product, engineering and design; 5 customer success; 5 people and HR;
4 finance; 4 founders and assistants; 1 growth; 3 unclear. No title contained "marketing". 3 accounts were
rate-limited. Routed people search by title "marketing" returned 20 people at all 8 accounts, $0.00
metered (names and titles; contact details are a separate paid step).

Rule: search for the buyer's function; a "decision makers" list is a map of the company, not your committee.

### Check open deals for missing people

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **Compare.** People in the CRM against people you have actually talked to. (Nic Siegle, Dust)
- **Fill the gap.** Have the agent find the missing roles on each open deal. (Olga Andrienko)

### Who to pick

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **By size.** Under 50 staff: the founder. 50 to 250: the head of the function. 250 to 2,000: a director and a hands-on manager. Above that: never the C-suite cold. (Goose skills)
- **Three per account at most.** Never two peers on one team: "they compare inboxes". (Goose skills)
- **A champion has done something.** An intro, inside information, a meeting pushed through. A title is not enough. (Anthropic's plugins)
- **Title traps.** "Contains CTO" matches "Director of Sales". Pair every title with a seniority. (Clay's plugin)

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

### Two habits for the waterfall

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **Verify between providers.** Check each answer before you try the next one. (Taylor Haren)
- **One column per provider.** Keep every answer, then pick one final email. (Alex Vacca)

### Keep guesses out of the send column

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Two columns.** One holds only addresses that passed a checker. Guesses sit in a second column with a status. (Goose skills)
- **Label every row.** Verified, inferred or unknown. Inferred rows are approved as their own group. (Anthropic's plugins)
- **Clean before you pay.** Remove role addresses and throwaway domains first. (Explorium's skills)
- **Grade the list.** Duplicate emails under 1%. One or two people per company. Names that look real on 95% of rows. A failing grade means rebuild, not send. (Cold Outbound Skills)

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

### Signals from your own audience

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **First-party.** They already know you: site visits, product use, likes and comments. (Michel Lieben)
- **Second-party.** They met you elsewhere: through a partner, or at a company they just left.
- **Third-party.** Public events: hiring, funding, ads, new tools. The run above is this kind only.

### Post engagers: check before you sequence

1. **Remove.** No company, wrong size, competitors, customers, open deals, blocklist, partners. Fail one and the row is out. (Kenny Damian)
2. **Score.** Was the post on your subject? Yours or a competitor's?
3. **Route.** Your post on your subject: a rep, now. A competitor's: a LinkedIn sequence. Your other posts: nurture. Score zero: nowhere.
4. **Test.** Follow test contacts through every check before a real send.
5. **On treg.** Checked 26 Nov 2026 on one public post: the first 50 reactions and 38 comments came back with name, headline and profile link, for $0.003. Give the full post link.

### More signals worth building

- **"Not now."** Pull the exact condition from the call (a headcount, a round, a hire, a date). Check weekly if it is met. (Othmane Khadri)
- **Champion moved.** Re-check past customers for job changes on a schedule. Confirm live first (our study). (Michael Saruggia)
- **Warm intros.** Sort target accounts by who on your team knows someone there. Draft an ask they can forward. (Alex Lindahl)
- **Job post text.** Read what the posts say, not just that they exist. (Patrick Spychalski)
- **Events.** Find posts that name the event, keep the people really going, ask to meet there. (Patrick Spychalski)
- **Their ads.** A live ad is a filter and a first line. (Jono Catliff's guest)
- **Never lost.** Not-now goes to nurture, a no-show is rebooked in 24 hours, closed-lost is revisited in 90 days. (Jan Rasmussen)

Prompt: "Using treg, pull everyone who reacted to or commented on the posts in posts.txt (one LinkedIn post link per line). Enrich each person's company. Drop anyone with no company, outside the size range in icp.md, at a customer or open deal in crm.csv, at a competitor in competitors.md, or on blocklist.csv, and log the reason for each drop. Show me the tiers for the rest before anything is sent. Show the cost before you start."

### Guards for each signal

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Every signal expires.** One library drops a job post after 45 days and funding after 90. (Goose skills)
- **A role start is not a hire.** Of 276 people with a new role, 78 (28.3%) had moved inside the company. Never write "hired". (Cold Outbound Skills)
- **Same event.** "A senior job post in the last 30 days" must be one post, not an old senior post plus a new junior one. (Clay's plugin)
- **Silence can be a gap.** A watch skips people with no profile link and reports no error. Measure how many it can see. (Clay's plugin)
- **Does the headline name them?** Industry-wide news gets pinned on the wrong company. (Explorium's skills)
- **Funding.** Equity rounds only, under 12 months old. Quote the amount, never the date. Databases ran 3 to 4 months behind. (Cold Outbound Skills)
- **An advisory seat is not a move.** Check the new company fits before you act on "champion moved". (Clay's plugin)
- **Post engagers: plan for the loss.** One run kept 1 usable prospect in 17. (Cold Outbound Skills)
- **One library refuses engagers.** Its rule: "never scrape who liked or commented on posts". (Goose skills)

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

### Check the score before you trust it

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **Known tens and zeros.** Score 10 to 20 accounts you know are perfect and 10 to 20 you know are wrong. Perfect should score top, wrong should score zero. (Patrick Spychalski)
- **Back-test.** Build the score from won and lost deals, then test it on deals it has not seen. (Clay's agent team)
- **Fewer alerts.** Weight signals. Reps mute a busy channel. (Patrick Spychalski)

### Make the score honest

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Gates before scores.** "A gated-out row never appears with a low score." (Goose skills)
- **Did it separate anything?** If top and bottom differ by under 10 points in 100, say so and drop the score. (Anthropic's plugins)
- **Missing is not zero.** Leave the part empty and spread its weight over the rest. (Explorium's skills)
- **Distance, not pass or fail.** In the size band 20 points, one band off 12, two off 4. (Explorium's skills)
- **Decay.** Full weight for 14 days, 0.7 to day 30, 0.4 to day 60, 0.2 to day 90, then nothing. (Explorium's skills)
- **Negative points.** A competitor, a student address. (gtm-engineering-skills)

These libraries set hiring and funding weights by assertion. Our own study found hiring did not separate won from lost, so test a weight before you use it (how).

## Let the agent research. Let a person write, or at least approve.

23 Sep run: 19 companies had a news event; scored as a first line on a 0 to 3 scale, 4 of 19 were decent or better
(mean 1.79).

Rule: nothing is sent that a person has not read.

### The handoff

From practitioners, Nov 2026 (their posts and videos, not our runs).

Vendors that sold fully automatic AI SDRs spent 2026 adding people back. The agent is no longer the hard part. The handoff is. (Nick Abraham, Chris Muldoon)

- **When** does an account pass from the agent to a person?
- **What** does the person know when they pick it up?
- **Who** checks the agent got the facts right?

### What to approve

- **Reject** compliments and guesses. (Dan Theirl)
- **Approve** one real detail: a role they just posted, a tool they just switched to. (Dan Theirl)
- **Template plus blanks.** A person writes the fixed part. The agent fills only the blanks. (Patrick Spychalski)
- **Copy what worked.** Export your sent messages and ask what the replied-to ones share. One founder found each was under 30 words. (Asteroid)
- **An inbox, not a thread.** Drafts in one long chat thread get lost. (LangChain)
- **Remove the reviewer last.** One team waited until its best rep "couldn't improve it anymore". (Vercel's COO at SaaStr)

### What an approval covers

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Only what was seen.** A yes covers the drafts in front of the person, "and nothing that arrives later". (Anthropic's plugins)
- **Four separate yeses.** The list, the copy, the send, the CRM write. (Anthropic's plugins)
- **Swap test.** Swap two rows' first lines. If both still fit, they are generic. (Goose skills)
- **Facts must be on the page.** Every fact in the copy appears word for word in the source. Check it with code, not a second model. (Cold Outbound Skills)
- **Two bars for a claim.** 70% of rows usable; 90% correct among rows that make a claim. (Cold Outbound Skills)
- **No hook? Say so.** "An honest cold email at 40 words outperforms a fake-warm one at 120." (Anthropic's plugins)
- **Five rows by hand.** Re-check five random rows against their sources on every run. Report how many failed. (Goose skills)
- **Machine tells.** "I hope this helps", "not X but Y", ideas in threes. (humanizer)

## Sending infrastructure and replies: what practitioners recommend

Not treg. Conservative volume, bounces and complaints watched per domain; classify replies, route with context, a
person approves, outcome written back to the row.

### Sending: settings people use

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **Secondary domains.** Never the main one. SPF, DKIM and DMARC set. Warm up first. (@coldemailchris)
- **One or two inboxes per domain.** Send under their limit. (Matt Lucero)
- **15 to 25 emails a day per inbox.** The range we saw. (Michael Knapp, Patrick Spychalski)
- **Pause at 2% bounces.** Many tools default to 5%. (Alex Vacca)
- **Under one spam complaint per 1,000.**  (Taylor Haren)
- **Retire weak domains.** One agency drops any domain under a 1% reply rate. (@coldemailchris)
- **Split by mail provider.** Behind a security gateway, "delivered" may mean unseen. Phone those. (@coldemailchris)
- **Re-verify old lists.** Six months is old (our study). (@coldemailchris)

### Copy: what one data set says

Taylor Haren counted meetings per 100,000 sends across 12.2 million emails. His figures, his clients. (Taylor Haren)

- **Outcome, not features.** About three times the meetings.
- **Three lines.** Problem, outcome, ask. Under 50 words.
- **Plain subject.** Three words or fewer. Teasers get opens, not meetings.
- **Casual.** Formal emails booked nothing in his data.

### Offer and testing

- **The offer beats the wording.** Test offers first. (@coldemailchris)
- **Lower the risk.** A free pilot, a sample or a guarantee. (Matt Lucero, Jono Catliff's guest)
- **Proof from their niche.** A named case study, tied to revenue. (Jono Catliff's guest)
- **Many variants.** About 1,000 sends each before you judge. (@coldemailchris)
- **Judge late.** One sender saw 53% of meetings arrive in weeks three and four. (Taylor Haren)
- **Count meetings, not replies.** One subject line got 30 positive replies and no meetings. (Taylor Haren)

### Find where it breaks

| What you see | What to fix |
|---|---|
| Not delivered | Sending |
| Delivered, no replies | The list or the message |
| Replies, no meetings | The offer or the ask |
| Meetings, no deals | Qualification |

The four-step check is from Deepika's roadmap video.

### Handling replies

- **Stop everywhere.** One reply or one booked meeting stops every sequence on every channel. (Alex Vacca, Everett Berry, Clay)
- **A named owner.** In the channel the rep already watches, with context and a phone number. (@coldemailchris)
- **Fast.** One sender replies within a minute, with two time slots and a calendar link. (Jono Catliff's guest)
- **Phone every interested reply.** Do not just email back. (Matt Lucero)
- **Route replies home.** If you send from other domains, replies must still reach the rep. (Everett Berry, Clay)

### Where senders disagree

We found no agreement on these. Test them on your own list.

- **Warm-up.** Some call it essential; one says it only marks you.
- **Links and open tracking.** Some turn both off; one sends HTML with links.
- **Personal first lines.** Some swear by them; one saw no difference.
- **The ask.** One data set favours asking for the meeting; others never ask on the first email.
- **Follow-ups.** One email to the whole market every month or two, or a short sequence.
- **Free audits.** Some lead with one; others say buyers see a sales call.
- **Precision.** Huge senders profit from loose lists. Our runs are on a market of a few thousand companies, where a burned list is costly. (Hans Dekker)

### When numbers drop

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Read the bounce code.** 5.1.1: bad address, re-verify the list. 5.7.26: your authentication, fix DNS and keep the domain. Blocklist text: the domain is burned. Any 4.x: slow down, never cancel. (Cold Outbound Skills)
- **Enough data first.** 200 sends before you judge a reply rate. 50 before a bounce rate. 30 days before a new domain. (Cold Outbound Skills)
- **Is it a drop?** Only if this week is under half the four-week average. (Cold Outbound Skills)
- **Before you cancel a domain.** Run a placement test. Over 90% in the inbox means the problem is targeting, offer or copy. (Cold Outbound Skills)
- **Keep spares.** Half your daily volume in reserve. A new domain needs about six weeks, so buy ahead. "Cancelling into a shortfall trades a bad reply rate for no sends at all." (Cold Outbound Skills)
- **Check every DKIM selector.** A default-only check reports a healthy set-up as broken. (Cold Outbound Skills)

### Stop rules

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Opt-out first.** Check for it before anything else touches the message. Unclear counts as an opt-out. (Sales-Skills)
- **Out of office pauses.** A hard bounce marks the contact bad everywhere. A removal is permanent. (Anthropic's plugins)
- **Four unanswered, stop.** Never two messages inside 48 hours. (Anthropic's plugins)
- **One sequence at a time.** After one ends with no reply, wait 12 weeks. (Goose skills)
- **Count real replies.** Out-of-office is not a reply. Score the first reply per person. Hostile replies over 0.3% or unsubscribes over 2%: pause. (Cold Outbound Skills, Clay's plugin)
- **Empty blank? Split the list.** Give those rows their own copy. They are your control. (Cold Outbound Skills)
- **Before launch.** Send it to yourself. Read it on a phone. Check ten random rows for broken blanks. (gtm-engineering-skills)
- **The law.** In the US: a postal address and an opt-out honoured within 10 business days, business email included. In the UK and EU: a written legitimate-interest test. One skill's summary, not legal advice. (gtm-engineering-skills)

The skill libraries disagree with each other and with the senders above: 30 or 30 to 50 emails a day per inbox, a bounce pause at 3% or 5%, first emails of 50 to 120 words, tests of 100 to 2,000 sends. Their numbers are rules of thumb unless a count is given.

## Enrich an inbound lead from its email domain and route it in seconds

Recorded 30 Sep 2026, 20 domains not looked up before: median 2.2 s (p90 4.5 s), $0.053 total, three providers. Fill:
employees 20/20, location 20/20, industry 16/20, description 13/20. Repeat lookups of already-enriched domains: median
1.15 s, about a fifth of the price. This measures the lookup, not a full routing flow.
`treg call treg.companies.enrich --method POST --data '{"domain":"acme.com"}'`

Rule: every inbound lead is enriched and routed before a person opens it.

### More from inbound

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **Short form.** Ask for name and work email. Enrich the rest. Only if enrichment fills nearly every row. (Patrick Spychalski)
- **Leads nobody works.** Point the agent there first, with one number to move. (a Series A founder, LangChain)
- **Be the answer AI gives.** Buyers ask an assistant before they reply. Comparison pages, how-to pages and plain-text docs get quoted. See our AEO guide ({BASE}/aeo). (Tom Grainger, @irabukht on Railway)

### Sort, then route

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Four buckets, a draft for each.** Hot. Qualified. Unclear: ask the one question that settles it. Out of scope: an honest no and a referral. (Anthropic's plugins)
- **Fit and urgency do different jobs.** "Fit determines what to say; urgency determines how fast a human sees it." (Anthropic's plugins)
- **Ownership first.** An existing account goes to its owner. Then region, size, industry, round-robin. Name the rule used. (Anthropic's plugins)
- **A fallback owner.** So no lead dead-ends when enrichment fails. (gtm-engineering-skills)
- **Priority by share.** Top 20% same day; the next quarter within 48 hours. (Anthropic's plugins)
- **Flood guard.** Several near-identical inquiries are a form loop or spam. No drafts; leave them out of "send all". (Anthropic's plugins)
- **Two numbers.** Time from inquiry to a draft someone could approve, and how long the oldest draft has waited. (Anthropic's plugins)

## Tag every row, so you can tell which list, signal and provider paid off

Store per row: source list, signal, the provider that found the email, the verifier's verdict. Join to CRM outcomes
monthly.

Rule: a row without its source and provider does not go into a sequence.

### Keep the CRM clean

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **Agent fields.** Give agents their own CRM fields, apart from the ones people fill. (Everett Berry, Clay)
- **Log everything.** Every agent action goes to the CRM. Anything that needs a person goes to chat. (Asteroid)
- **Lost deals.** Have an agent write up why each one was lost. (Vercel's COO at SaaStr)
- **Clean on a copy.** Try a clean-up on a list you do not care about first. (Patrick Spychalski)

### Rules for an agent writing to the CRM

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Read, show, write, re-read.** Show before and after for the changed fields only. Never tidy other fields on the way. (Anthropic's plugins)
- **Never overwrite a filled field** with a different value unless a person confirms. (Sales-Skills)
- **Never auto-create a deal.** Match in order: the one named, the contact's only open deal, a close match, then ask. (Anthropic's plugins)
- **Blank is not "not checked".** Say which. (Anthropic's plugins)
- **Dedupe keys.** Email for people, domain for companies. Name plus company goes to review. Log every merge. (gtm-engineering-skills)
- **Suppress forever, forget the person.** Keep only a hash of the email, and check it before any paid lookup. (Goose skills)
- **Clean on a copy.** Keep the raw file. Tag bad rows with a reason; do not delete them. (Explorium's skills)

## The metric stack: system health, performance, efficiency

System health: counts agree, enrichment fill rate, share deliverable. Performance: replies, meetings, opportunities
by source, signal and tier. Efficiency: cost per usable result ($0.12 per deliverable lead in our run), share dropped
before paying (21 of 48), time to first touch (2.2 s median to enrich a new domain).

### Someone owns the bill

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **No metric, no launch.** If you cannot say how you will measure it, it is not ready. (LangChain)
- **Cost per job.** If a job costs more than it earns, fix it or stop it. (Greg Isenberg)
- **Quality.** The share of drafts a person edits or cancels. (LangChain)
- **Cheapest step that works.** Code for rules, a model for judgement, a browsing agent only when needed. (Michael Saruggia)
- **Big model plans and checks, small models do the volume.** One team tried a cheaper model as the checker and its answers got worse. (Greg Isenberg, Rippling)
- **Refresh by field.** Headcount often, headquarters rarely. (Everett Berry, Clay)
- **Watch the fan-out.** One agent per lead adds up. Good and bad set-ups differ in cost by orders of magnitude. (Clay's agent team)
- **Acted on?** A report nobody acts on is not worth building. (Rippling)
- **Who watches?** Name the person who tracks tools, credits and spend. (Noëmie J.)

### Tests that mean something

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **One variable.** Write what counts as a win before launch. Start both arms the same day. Read at day 21. (Cold Outbound Skills)
- **Size by the lift you expect.** From 1% to 2% needs about 500 sends per arm. From 1% to 1.5%, about 2,000. (Cold Outbound Skills)
- **Opens lie.** Privacy features inflate them. Count replies and meetings. (gtm-engineering-skills)
- **Watches cost two ways.** Per record checked or per event found. Narrow the list before you slow the schedule. (Clay's plugin)

## Roll automation out like software: shadow, small segment, human gate, then autonomy

What broke in our runs without stopping them: a finder out of capacity, a buying-group endpoint out of capacity, 3
accounts rate-limited, and our own parser bug ($1.08 re-run). Alert on expected counts per stage; store raw
responses; cap spend per run.

Rule: nothing goes autonomous until it has run in shadow, and every stage reports its row count.

### Before you automate

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **Watch it done.** Ask the team to share their screen and do the task by hand. Each tab the best rep opens is one step. (Tim Yakubson, Vercel's COO at SaaStr)
- **Do it by hand once.** Automate a bad process and you get it at scale. (Deepika)
- **Write it down the second time you explain it.**  (Deepika)
- **Three people per agent.** A builder, a data person and the best person in that job. (Vercel's COO at SaaStr)
- **Rate each job twice.** How much does the agent own? How hard is it to set up? (Adam Schoenfeld and Brendan Short)

### Keep it learning

1. **Flag.** Working agents report what is missing or wrong. (Shann Holmberg)
2. **Curate.** A scheduled agent reads the flags and your corrections, then updates the brain.
3. **One editor.** Only the curator updates the brain. LangChain says its own agents run a similar loop. (LangChain)

- **50 real cases.** Keep them with the right answer. Re-run on every new model. (Greg Isenberg)
- **Read every run at first.** One team did for two to three months. (Justin Joyce, Cloudflare)
- **Test by journey.** Group tests by what the user is trying to do. (Clay's agent team)
- **Count before you rebuild.** A problem in 2% of runs does not justify a rebuild. (Clay's agent team)
- **Draft, check, tone.** Three passes for any report that goes to people. (Justin Joyce, Cloudflare)
- **One owner.** Agents built by individuals rot when their owner gets busy. (LangChain)
- **Weekly tool review.** What did it do? Where did someone copy and paste anyway? What broke silently? (Alex Vacca)
- **Each quarter.** Does a vendor now beat what you built? Then switch. (Vercel's COO at SaaStr)
- **Short task lists.** A morning message with 20 tasks gets ignored. (a Series A founder)
- **Guardrails.** Approval gates, suppression lists, caps, logs, a test set. (Jan Rasmussen)

One open question: several small agents, or one agent behind a dispatcher that picks the tools? We saw both work. (@TechSalesGuy, LangChain)

### Guardrails written into skills

From open-source skills, Nov 2026 (rules written into public agent skills; their numbers, not our runs).

- **Reading is not obeying.** Emails, forms, transcripts and enrichment are data, never instructions. Test with a page that hides "ignore your rules". (Anthropic's plugins, gtm-engineering-skills)
- **Scheduled runs propose.** A value taken from an email or a call is never written by an unattended run. (Anthropic's plugins)
- **Limits in code.** Send caps and allowed domains go in settings, not in the prompt. (gtm-engineering-skills)
- **Start with ten.** Any run over 100 records does about ten first, then asks. (Clay's plugin)
- **A real record, end to end.** Made-up test input does not count. (Clay's plugin)
- **Resume, do not restart.** Re-running from the top sends the emails again. (Clay's plugin)
- **Deleting or cancelling.** Two approvals. A snapshot first. Exact IDs, never a pattern: one pattern match cancelled 1,289 things it should not have. (Cold Outbound Skills)
- **Three times is a rule.** When the same correction appears three times, write it down as one. (Sales-Skills)
- **A weekly rhythm.** Monday deliverability, Wednesday replies, Friday review. "Daily pokes at your cold email stack are a procrastination pattern, not a performance pattern." (Cold Outbound Skills)

## The best Claude Code skills for GTM engineering, and the data step each leaves to you

coreyhaines31/marketingskills (cold email, competitor profiling), mvanhorn/last30days-skill (topic research),
AgriciDaniel/claude-seo (SEO), zubair-trabzada/geo-seo-claude (AI visibility), phuryn/pm-skills (battlecards),
swan-gtm/gtm-skills (account research), Cold-IQ/ColdIQ-s-GTM-Skills (outbound plays, triggers, email templates; added
26 Nov 2026, no licence shown on GitHub), anthropics/knowledge-work-plugins (inbound sorting, routing, deal review, CRM
hygiene; Apache-2.0), growthenginenowoslawski/coldoutboundskills (sending, deliverability, list building; MIT),
superdesigndev/treg (buyer signals). Not yet run with treg end to end.

### How to write a skill

From practitioners, Nov 2026 (their posts and videos, not our runs).

- **One skill per job.** Small and boring beats one giant prompt. (Greg Isenberg)
- **Record, then write.** Do the job once out loud. Have the agent write the steps, the checks and the mistakes. (Greg Isenberg)
- **Save what worked.** Turn each finished campaign into a skill. (Michael Saruggia, Tim Yakubson's guest)
- **One skill per event.** Signup, demo booked, site visit. (Asteroid)
- **Plan, then code.** One skill asks and plans in plain words. A second runs the code. (Taylor Haren)
- **Worth a skill?** More than twice a week, same steps, a result you can describe, a small mistake is safe, a teammate would use it. Fewer than three: not yet. (gtm-engineering-skills)

## Clay alternatives for GTM engineers who work in Claude Code or Codex

Keep Clay for a shared visual table. Work in the agent with a per-call data layer when you want the playbook in
prompts. Cost per correct email on the same 292 people: see the waterfall chapter above.

### Newer tools: three checks

From practitioners, Nov 2026 (their posts and videos, not our runs).

Several per-call and open-source tools sold as Clay for agents launched in October and November 2026, and Clay opened its own agent in beta on 19 Nov. We have not measured them, so they are not in the table.

- **Match rate** against a list you know.
- **Cost per correct result,** not price per credit.
- **Repeat lookups.** Is the same record billed twice? It matters once an agent re-checks all day. On treg a repeat company lookup cost about a fifth (chapter 13). (Petra Hajal)

## Every recorded run behind this playbook

Studies: job changes against five databases (7 Oct, $11.10), hiring and news before a raise (6 Oct, $13.65 including
building its samples), found against deliverable (7 Oct, $0.65); each on its own sample. Runs on one ICP: 30 Sep playbook runs ($2.70, including a $1.08 re-run after our own parsing bug), 23 Sep lead list ($2.33), 16 Sep work-email bench, and the workflows at {BASE}/workflows

## Glossary and questions

ICP, TAM, check, waterfall, catch-all, signal, live read, tier, shadow mode, company brain, suppress, first-party
signal: defined on the page.

How do I become a GTM engineer? Learn in this order: sending, lists, enrichment, signals, offers and copy, replies, then agents in Claude Code or Codex. Run a real campaign at each stage. Build one small tool for your own job and show a recording of it. Hiring managers in late 2026 check judgement, guardrails and cost sense before tool knowledge.

How accurate is contact data after someone changes jobs? Not very, in the first month: 68% of database records on
average did not show the new employer (study above). Confirm live before you write.

Do hiring or news signals predict that a startup is about to raise? Hiring did not in our test, and news ran the other
way (study above). Act on the announcement, and test timing signals on your own deals.

## How to become a GTM engineer, and how teams hire one

From practitioners, Nov 2026 (their posts and videos, not our runs).

The most-watched videos are roadmaps. The most-shared hiring posts say the bar moved.

### What to learn, in order

| Stage | What to learn | Here |
|---|---|---|
| 1 · Sending | Domains, inboxes, SPF, DKIM, DMARC, warm-up | chapter 12 |
| 2 · Lists | Exports, directories, deduping | chapter 2, chapter 4 |
| 3 · Enrichment | Waterfalls, catch-alls, stale data | chapter 7, chapter 8 |
| 4 · Signals | ICP checks, hiring, funding, scoring | chapter 5, chapter 9, chapter 10 |
| 5 · Offers and copy | Offer per segment, short emails, variants | chapter 11, chapter 12 |
| 6 · Replies | Sorting, routing, phone, LinkedIn | chapter 12 |
| 7 · Agents | Skills, context files, call transcripts, reports | chapter 3, chapter 16, appendix A |

Order from @coldemailchris's six-month plan, twelve stages grouped into seven. His advice: "Run a real campaign at every stage."

### How to start

- **Build one small tool for your own job.** An agent that researches an account is enough. (Hlib Storchak)
- **Show it.** Send a recording of what you built and the results. Not a CV. (Hlib Storchak, Nic Siegle, Dust)
- **Learn the other side.** From sales, learn the tools. From tech, learn to sell. (Hlib Storchak)
- **Agency first.** Many clients, many problems, fast. (Hlib Storchak)
- **Sales basics.** Expect to be asked how sales-led differs from product-led. (Tim Yakubson)
- **Hunt the job like outbound.** Apply widely. Research and message the hiring contact at the two you want most. (Tim Yakubson's guest)
- **Agree the scope.** Or the job becomes dashboards. (Hlib Storchak)

### What hiring managers check

- **Judgement.** Give a real problem and solve it together. Do they ask before building? Explain out loud? Say "I am not sure"? (Petra Hajal)
- **Not the tool.** A tool is learned on the job. Knowing why an account belongs on the list is the job. (Ricky Pearl)
- **Unasked clean-up.** Test briefs are vague on purpose, like a client's. Do you fix a messy name without being told? (Tim Yakubson's guest)
- **Guardrails and cost.** New on the list of what Clay screens for. (Clay's hiring guide)

Posts and videos read on 26 Nov 2026. Figures are the authors' own; we have not checked them.
