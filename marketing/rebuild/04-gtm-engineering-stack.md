---
page_id: r4
slug: /rebuild/gtm-engineering-stack/
seo_title: "GTM Engineering Stack 2026: Nine Layers, Priced Per Call | treg.to"     # 61
meta_description: "What a GTM engineer actually runs in 2026 — signal, list, enrich, research, write, send, LinkedIn, triage, analytics — with the tools per layer and what each call costs."  # 168 → trim
h1: "The GTM engineering stack, priced"
seo_terms:
  primary: "gtm engineering"             # 3,600/mo, LOW competition, CPC $9.32
  secondary:
    - "gtm engineer"                     # 3,600
    - "gtm agent"                        # 170
    - "outbound agent"                   # 70
    - "clay vs apollo"                   # 260
capabilities: [all outbound capabilities]
links_out: [/rebuild/clay/, /rebuild/linkedin-intent-signals/, /rebuild/ai-sdr/, /use-cases/lead-enrichment-for-ai-agents/, /use-cases/company-research-for-ai-agents/]
links_in: [/resources, /for/claude-code, skill.md]
status: draft 2026-08-27 · price table to be regenerated from the catalog at build time
---

# The GTM engineering stack, priced

"GTM engineer" is the job that appeared when outbound moved from seats to scripts. The stack has settled into nine layers; an agency that runs outbound for 100+ AI companies describes it as "21 MCP connections from one Claude Code terminal." What no stack post shows is the price of each layer per lead. This one does, from the catalog's measured rate card, with the honest column for what still needs a subscription.

## The nine layers

| Layer | Job | Subscription tools people name | Per-call, one token | Per lead |
|---|---|---|---|---|
| 1. Signal | Who's in market this week | PredictLeads, Common Room, RB2B, Warmly, Gojiberry, Trigify | job postings `apify.linkedin.search.jobs`; funding `aviato` / `predictleads`; post engagers `scrapecreators` | $0.001–0.04 |
| 2. List | The right person at each account | Apollo, Sales Nav, Crustdata, Prospeo | `icypeas.people.search`, `findymail.search.employees`, `leadmagic.x.role-finder` | $0.0004–0.05 |
| 3. Enrich | Verified email, phone, firmographics | Clay, FullEnrich, ZoomInfo | six email finders + three verifiers ([the waterfall](/rebuild/clay/)) | $0.01–0.03 |
| 4. Research | What they said, what they run | Firecrawl, Exa, Perplexity, Claygent | LinkedIn posts `tikhub`, company page `scrapecreators`, tech stack `tomba` | $0.001–0.009 · website scrape: gap |
| 5. Write | The first line and the sequence | Claude, GPT | your model | — |
| 6. Send | Domains, warmup, rotation, replies | Instantly, Smartlead, Lemlist, Email Bison | own-key tool, never metered | — |
| 7. LinkedIn | Connect, message, inside 100/week | HeyReach, Expandi, Unipile, PhantomBuster | read-only here; send on your key | — |
| 8. Triage | Classify replies, pause, hand off | Claude | your model | — |
| 9. Analytics | Reply rate by segment, domain health | Sheets, Supabase | own-key Search Console / Ads when the loop includes SEO | — |

**Data layers 1–4 together: roughly $0.02–0.10 per fully researched lead**, visible before the call. Layers 5–9 are where the money and the risk live, and the catalog does not pretend to own them.

## The orchestrator question

Three camps, all using the same data layer:

- **n8n / Make** — visual, Sheets as the database, polling loops for async scrapers. The Reddit "here's my system" posts.
- **Claude Code + skills** — the fastest-growing. "We took every UI out of our outbound stack," says one agency; Growth Unhinged reports mature teams moving MCP → CLI because MCP costs 10–32x the tokens. YALC (MIT) is the open-source Clay of this camp.
- **Cowork + vendor MCPs** — Apollo, Clay, HubSpot, Lemlist connectors, no code.

The catalog is the same in all three: `treg call` from a shell, an HTTP node in n8n, or the MCP server in Claude Code. Pick by what your team can debug at 2am.

## The rules the operators agree on

- Write the entry rule before you buy a tool: how many signals must be true, and how recent, before an account enters a send. Five to ten.
- Micro-campaigns of 50–250 contacts, lists under 30 days old.
- Qualify before you spend on enrichment; it drops ~40% of the list and doubles replies.
- One signal per email. The LLM fills a template.
- Cancel a domain at <0.7% reply. Keep two control inboxes running a known-good sequence so you can tell whether Google moved or you did.
- LinkedIn: 100 invites per rolling week, comment-first warming, acceptance above 25–40% or you're throttled.

## What this stack does not include

Sending, warming, visitor de-anonymization, website-to-markdown (catalog request open), keyword-level LinkedIn monitoring (same). Anyone selling "one agent does all nine layers" is selling layers 5–9 on a data layer they don't price.

Related recipes: [Rebuild Clay](/rebuild/clay/) · [Rebuild Gojiberry](/rebuild/linkedin-intent-signals/) · [Build your own AI SDR](/rebuild/ai-sdr/) · [The join problem](/rebuild/join-problem/)
