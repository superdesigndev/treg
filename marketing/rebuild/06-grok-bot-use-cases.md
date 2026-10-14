---
page_id: r6
slug: /agents/grok-bot (existing) + /workflows/* (existing structure)
seo_terms:
  primary: "grok bot lead generation"     # null volume = too new to measure (Grok Bot shipped 2026-08-11); passes all 7 emerging-term gates
  secondary:
    - "grok bot use cases"                 # live autocomplete #3 for "grok bot "
    - "grok bot for sales"
    - "grok bot research"
status: agent page extended 2026-08-28 (workflows section + 3 Grok-specific FAQ entries). The X-complaint recipe below stays a draft — see the provider note.
---

# Grok Bot use cases, mapped onto the structure that exists

The site already has the right containers. Every Grok Bot use case is either a **job** (one call,
`/use-cases/<job>`, listed on `/agents/grok-bot` under "The menu" with a category prompt) or a
**workflow** (several jobs, one prompt, a receipt, `/workflows/<slug>`, now listed on every agent
page under "Workflows"). Nothing new to build for the map itself; the work is which workflows get a
real run.

## The map

| Use case people ask for (X, this week) | Structure | Jobs it chains | Run status |
|---|---|---|---|
| **Lead generation** — "find customers with Grok Bot" (@luismbat 4.5M views, @kristaletz 2.85M) | workflow | companies.search → people.search → people.email.find → people.email.verify → companies.news | **Live**: `/workflows/find-and-verify-a-lead-list`, receipt 2026-08-26, $3.62 / 50 companies |
| Lead generation from X complaints — "people complaining about competitors / looking for an alternative" (the @luismbat recipe, Amplemarket in the enrich slot) | workflow | x.search.posts → x.user.profile → people.email.find → people.email.verify | **Draft, blocked**: see provider note |
| Prospect research — funding, headcount, hiring, tech stack, news by domain | jobs | companies.enrich · companies.funding · companies.jobs · companies.tech_stack · companies.news | Live as jobs; the join-problem workflow (`05-join-problem.md`) needs its run |
| People research — profile, recent posts, work email | jobs | linkedin.user.profile · linkedin.user.posts · people.email.find | Live as jobs |
| Market research — who is hiring, what employees say, competitor ads | jobs (category "Market research" prompt already on the agent page) | jobs.search · employee reviews · ads.library | Live as jobs |
| SEO research — keyword volume, who ranks, Search Console | jobs (category "Search & rankings") | keywords.volume · serp.organic · search-console.performance | Live as jobs |
| Creator / social research — creators by keyword, a video's comments, a post's engagers | jobs (category "Social listening") | creators.search · youtube.comments · linkedin.post | Live as jobs |
| Directory business — "curated data on a website" (@startupideaspod, 104K views) | workflow candidate | local businesses by keyword+location → reviews → enrich | Not run |
| Review intelligence — a business's reviews, reply to your own | jobs | reviews · google-business-profile | Live as jobs |

## What shipped today (in the discovery PR)

- `/agents/*` pages (all five) gain a **Workflows** section listing every workflow with its step
  count, plus the `.md` twin. `/agents/grok-bot` now links the lead-gen workflow directly.
- `AGENTS["grok-bot"].faq` gains three Grok-specific entries: lead generation (points at the
  workflow), research (the jobs by category), and what it cannot do yet (no sending; LinkedIn post
  search is public posts via Google's index, reactions come a page at a time; website reading is
  named-field extraction, not raw markdown).

## Provider note — why the X-complaint recipe is not a workflow yet

Ran step 1 today: `tikhub.x.twitter-web-fetch-search-timeline` with `"looking for an alternative
to" hubspot`, `search_type=Latest`, $0.001. Five posts came back, dated 2016–2024 (not latest),
four of them vendors promoting HubSpot alternatives. A receipt built on that would say "5 posts,
0 buyers". Either the provider's Latest mode is not latest, or the complaint phrasing needs the
X API's recent search (`x.x.search-posts-recent`, own key, 44% observed ok rate). Re-run with a
narrower query and the X-API sibling before writing the page; if it still returns vendors, the
honest version of this workflow starts from competitor-post **commenters** on LinkedIn instead.

## The emerging-term page

"grok bot lead generation" has no measured volume yet (17 days old) and a soft SERP (x.ai
announcement, a day-old YouTube, an r/AI_Agents thread, two setup guides). The page that targets it
is **the existing lead-list workflow**, retitled for the term when it has an agent-specific
rendering, or the X Article in `marketing/_grok-bot-treg-2026-08-28.md` linking to it. Day-7 /
day-21 on exact-head position; autocomplete rechecked weekly.
