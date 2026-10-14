---
name: lead-signals
description: Use when the user wants to find buyers, warm or hot leads, or in-market accounts; asks about buying signals, intent signals or trigger events; wants to know who is complaining about a problem, engaging with a post, hiring for a role, changing jobs, raising money or adopting a tool; or wants to monitor a topic, a competitor or a list of accounts for people ready to buy.
---

# Lead signals

A signal is a public fact that makes a conversation timely: someone complained about the problem you
solve, engaged with a post about it, hired for it, raised money, switched tools.

Runs on treg. If it isn't set up (`treg --version` fails), read https://treg.to/llms.txt and follow it to
install and sign in, then come back here.

## The run

1. **Detect** raw signals for the user's buyer.
2. **Qualify.** Drop the noise; most runs discard more than half. A signal list is not a lead list.
3. **Contact.** Company → decision maker → work email, only for rows that survived.
4. **Hand back** a ranked list: person, company, the signal, *why now* in one line, source link, email
   if found. State coverage plainly: "1,180 engaged, 800 emails found".

Default for "find buyers for X" or "monitor Y": search posts about the pain on LinkedIn, X and Reddit →
read the comments and reactions on the best posts → keep people whose title fits → roll them up by
company and score → decision makers and emails at the top companies.

## Signals

The vendors are examples of who answers today, not a list to stick to: search, and take whatever
the catalog ranks best now.

| Signal | Search the catalog for | Example vendors | Keep | Drop |
| --- | --- | --- | --- | --- |
| Posts and complaints | "search linkedin posts", "search reddit posts", "search x posts" | HarvestAPI, AnyAPI, ScrapeCreators | first-person pain, last 7 days, names you or a competitor | vendors, recruiters, consultants selling to the same crowd |
| Post engagers | "linkedin post comments", "post reactions" | HarvestAPI, Fetchin | commented beats reacted; title fits | the author's colleagues, "great post" |
| Reviews | "google reviews" | Serper, AnyAPI | low stars about the job you do | reviews older than a quarter |
| Hiring | "job postings" | HarvestAPI, PredictLeads, Apollo | a role that implies your product, last 30 days, several roles | staffing firms, evergreen reposts |
| Job change | "job change" | Datagma, LeadMagic | moved in the last 90 days into a company that fits; a former user | moves inside the same group |
| Funding | "funding rounds" | PredictLeads, Aviato | last 90 days, stage fits your price | debt read as growth |
| Tech adoption | "technology users", "tech stack" | PredictLeads, Tomba | a tool added or removed recently: a competitor or an integration | tags every site has |
| Headcount | "headcount", "workforce" | CompanyEnrich, Akta | direction and rate in your buyer's team | jumps from an acquisition |
| News | "company news" | PredictLeads, Akta | launch, acquisition, exec hire, last 30 days | syndicated duplicates |
| Ads | "ads library" | Meta Ad Library, Adyntel | live campaigns: budget and an offer to position against | |

Anything else is a composition. A page change: scrape the page, keep a hash, compare next run. Custom
intent: a Google search such as `"migrating from <competitor>"`. Repo stars: GitHub with the team's own
token.

## Finding the endpoint

The catalog changes every week, so never trust a remembered endpoint id.

1. `treg catalog search "<the job in plain words>"`, not a vendor name.
2. Prefer a routed `treg.*` endpoint when there is one: the team's own keys first, then providers in
   order, and most misses are free.
3. `treg catalog get <id>`: the price, the **hit rate** (it found something), not just works (it
   returned 200), and the row's note.
4. One small call first. Read the reported charge and `_treg.served_by`, then scale.
5. Nothing fits: `treg catalog request "<what is missing>"`.

For emails use the routed finder, `treg.people.email.find`, and always send
`X-Treg-Route-Max-Cost: 0.05`; uncapped, a miss can walk up to the dearest provider.

## Score

Fit first (no fit, no row), then timing (fresher wins), then strength (two independent signals on one
account beat one). Every row carries its reason in the user's words.

## Keep watching

When the user wants it recurring:

1. Run once and report what it cost (sum the reported charges): that is the price of each run.
2. Keep `signals.csv` keyed by person URL, signal and source URL. Later runs report only new keys. The
   first run is the baseline; say so.
3. Schedule it with the user's agent: Claude Code `/schedule` (a cloud routine) or `/loop` (while the
   session is open), Codex `codex exec "<prompt>"` from cron, any other agent its own scheduler. The
   scheduled run needs `TREG_TOKEN` (an agent token from `treg org agent-new`) and a place for
   `signals.csv` it can read and write, such as a repo file or a sheet.

## Before the first list

Say once: outreach to EU and UK people needs a lawful basis, and suppression and unsubscribe lists
come first. Then hand over the list; the user decides how to use it.
