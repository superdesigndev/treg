# AI outbound in 2026 — the playbook people run, what Reddit asks, and what treg.to should publish

Research date: 2026-08-27. Sources: X (OpenCLI, 6 queries + threads of @itsalexvacca, @fivosaresti, @IAmAaronWill, @aryanXmahajan), Reddit (OpenCLI, 6 queries, 12 threads read with comments), YouTube (yt-dlp, 5 queries, 8 full transcripts), web (8 playbook articles incl. Eric Nowoslawski / Explorium / SyncGTM / Salesforge / Koka Sexton / Yalc / SalesRobot / Growth Unhinged). LinkedIn covered via the web playbooks and the LinkedIn-specific X/YouTube systems (the LinkedIn MCP isn't configured on this machine).

---

## 1. The system everyone runs (the 2026 skeleton)

Eight YouTube systems, eight web playbooks and the top X operators converge on the same nine steps. Where they disagree is the orchestrator, not the shape.

| Step | What it is | Who says it | Tools they name |
|---|---|---|---|
| 0. Context layer | ICP, exclusions, offer, email framework in a `CLAUDE.md` (<250 lines) + `icp/`, `messaging/` markdown | SyncGTM, Salesforge, Growth Unhinged, Lead Gen Jay ("strategy skill runs before scraping") | Claude Code, Notion |
| 1. Signal in | Hiring (esp. SDR/AE/VP Sales), funding, job/leadership change, tech-stack change, news, site visitors, LinkedIn engagement | Everyone. "Outbound stopped paying for volume, started paying for homework" | PredictLeads, Clay, RB2B, Apify LinkedIn Jobs, Crustdata |
| 2. Small list | 50–250 (Growth Unhinged), 300–1,000 (SyncGTM), 20–30 accounts (Koka). Lists < 30 days old | @itsalexvacca: "write the entry rule first — 5 to 10 signals must be true" | Apollo search (via Apify because "export is too expensive"), Sales Nav |
| 3. Qualify before you spend | AI yes/no + reason; drops ~40% of list, "2–3x reply rate" | Lead Gen Jay, Apollo, Explorium scoring weights | Claude/GPT |
| 4. Waterfall enrich + verify | 6 providers vs 1 = 50–70% → 85–95% email coverage; verify everything; bounce < 1% | SyncGTM, Eric N., Reddit | Findymail, Hunter, LeadMagic, Anymail, MillionVerifier |
| 5. Research → one-signal personalization | Scrape site/posts → LLM abstract → LLM **fills a fixed template**, never writes whole emails (A/B-ability). ≤85 words, no links in email 1 | Saraev, Clarence Nap, SyncGTM. Dissent: Marc ("hyper-personalization can backfire") | Perplexity Sonar ("way cheaper than a Clay agent"), Firecrawl, Exa |
| 6. Send from real infra, never from the agent | Secondary domains, SPF/DKIM/DMARC, 2–4 week warmup, 30–40/inbox/day, cancel a domain at < 0.7% reply | Every source | Instantly, Smartlead, Lemlist, Email Bison |
| 7. LinkedIn inside the caps | 100 invites / rolling 7 days, 20–25/day, new accounts 5–10/day for 4 weeks, note < 300 chars, acceptance > 25–40% or you're throttled. Comment-first warming is the 2026 move; browser-click automation gets whole user bases banned | Yalc, SalesRobot, Saraev | PhantomBuster, HeyReach, Unipile API, Claude-in-Chrome |
| 8. Reply classification, human conversation | interested / not now / OOO / referral / unsubscribe / angry → tag CRM, pause sequence. Reply within a minute "≈4x conversion" | Explorium, Yalc, Saraev | Claude, Supabase vectors |
| 9. Weekly loop | Reply rate by segment, top/bottom first lines, 20–50 simultaneous tests, control inboxes to tell "me vs the platform" | Eric N., SyncGTM, r/Coldemailing | Sheets/Supabase |

**The orchestrator war (this is the story for treg.to):**
- **n8n/Make** (Saraev ×2, Clarence Nap, three of the Reddit "my system" posts) — visual, Sheets as the database, polling loops for async scrapers.
- **Claude Code + skills** (Lead Gen Jay, @fivosaresti "we took every UI out of our outbound stack", @itsalexvacca "21 MCP connections run our GTM from one terminal", Eric N. "2M lines in 45 days") — the fastest-growing camp. Lead Gen Jay explicitly built a "Clay killer" = Perplexity + Claude + own DB. Growth Unhinged: mature teams are moving **MCP → API/CLI** because MCP is 10–32x more expensive in tokens.
- **Claude Cowork + vendor MCPs** (Automate with Marc, the $10K LinkedIn video) — Apollo/Clay/HubSpot/Lemlist connectors, no code.
- **All-in-one** (Apollo, AI SDRs) — Reddit is openly hostile: "AI SDR is a bullshit category, $45M later still can't sell"; "over half of buyers churn inside 90 days".

**What Clay's moat actually is, in the operators' own words:** Eric Nowoslawski keeps Clay for "150+ pre-negotiated data providers — one datapoint from HG Insights without an HG contract" and "automatic API queuing/rate-limit handling across 50 tables". r/gtmengineering: "Clay is great because of their data sources all coming together in one place… what data sources do you use?" That is, word for word, the catalog's pitch — one token, many providers, per-call, no contracts — and Clay's price ("exorbitant", "50–70% cheaper" is the winning alt pitch) is the wedge.

**Numbers that recur** (vendor benchmarks, quote with attribution): cold-email reply 5.1% (2024) → 3.43% (2026, Woodpecker); signal-triggered 4–8% vs generic 1–3%; the 5% who personalize every email get 17–18%; LinkedIn reply 10.3% vs email 5.1% (Expandi); Gmail spam-complaint cap 0.3% and it's **cumulative**, "one bad AI blast poisons a domain you warmed for months"; B2B data decays ~2%/month; Apollo rows: ~66% have LinkedIn URL, ~77% have email, ~25% of sites unscrapable, so plan 10K rows → ~2K deliverable.

**Copy-paste assets the sources already show (we can do these better with real calls):**
- SyncGTM's `CLAUDE.md` ICP block and 3-line email framework (hook/problem/CTA, ≤85 words).
- Explorium's `SCORING_WEIGHTS` dict (funding_90d 30, sales_hiring_spike 25, eng_hiring_spike 20, intent 20, technographic 15, leadership_change 10; threshold 40).
- Saraev's icebreaker prompt ("Spartan, paraphrase never quote, shorten company names, never 'love your website'") and his few-shot layout.
- Clarence Nap's hiring-signal template ("Saw you've got some SDR roles open…").
- Koka's two-sentence signal email ("Saw your comment on the pipeline-velocity post this week. Are you solving that now, or researching how others handle it?").
- SalesRobot's five LinkedIn scripts (connect / follow-up / job change / funding / post engagement).
- Eric N.'s Friday domain-health cron and "guess 6 permutations → verify → keep the valid one" email finder.
- Explorium's `0 2 * * * claude -p "…"` cron pattern and reply-classification JSON schema.

---

## 2. What Reddit is actually asking (ranked)

1. **"Is cold email / outbound still working in 2026?"** — 6+ threads; r/LeadGeneration's top thread (282 comments). Answer people upvote: yes, for Google-Maps-scraped local businesses without a website, 100–200 personalized emails → 3–5 replies; "anyone can blast 10k, few can send 500 hyper-relevant ones".
2. **"How do I make AI emails not sound like AI?"** — 5 threads. Best answer (r/SaaS, 363 pts): feed 50+ hand-written examples, "copy style not content", < 100 words, pre-filter generic LinkedIn posts, A/B the prompt (30% swing). "Identical sentence rhythm across 1,800 emails — nobody replies to tell you, they just stop replying."
3. **"Do AI SDRs actually book meetings?"** — 5 threads; deeply skeptical. One honest founder funnel: 1,842 contacts → 11.6% reply → 52 booked → 31 held. "The tool is 30%. The list filter and the first line are the other 70%."
4. **"Which tool do I start with — enrichment, scoring or sequencing?"** — "Everyone wants to automate a process they have no idea how to do without AI." Answer: source → enrich → qualify → CRM check → draft → human review → send → track; do 20 by hand first.
5. **"Is Clay worth it / cheaper Clay alternative?"** — 3 threads, 40–63 comments each; purely price-driven ("the pricing disturbs me the most"; GTM lead at a $200M-ARR company: "way overpriced").
6. **"Which data source is accurate — Apollo, Clay, Sales Nav, ZoomInfo?"** — "Apollo is honestly trash, under 10% valid phone numbers"; "ZoomInfo and Apollo lack up-to-date email verification and proper buying signals… I was learning to build middleware myself with Claude and n8n."
7. **"Where do I store leads between n8n runs?"** — dedicated thread; both big n8n posts use Google Sheets as the source of truth.
8. **"How do I handle replies — classify or keep human?"** — 3 threads.
9. **"Which sender accepts CSV with custom variables?"** / **"How do I verify emails inside the workflow?"** — 2 each.
10. **"Can you share the JSON?"** — asked ~7× in one thread; the "DM me" non-answer is resented. **Actually shipping the workflow is the whole differentiator.**
11. **"How many LinkedIn accounts / per-seat cost?"** — HeyReach $590/mo/sender vs Unipile €5/account.
12. **"How do I tell whether a reply-rate drop is me or Google/Microsoft?"** — the control-inbox answer.
13. "How do I find clients for my n8n/automation agency?" (924-pt r/AI_Agents thread) — the freelancer audience is huge and broke.

Pain points with the loudest quotes: saturation ("reply rates 3–5% → 1–2% because everyone runs the same stack sending 'hi {firstName} I noticed {scraped_intent_signal}'"), domain burn, tool sprawl ("so many tools… confused which to start with"), Clay cost, data rot, and Reddit's own vendor pollution ("it is his vibecoded SaaS and he's namedropping it here").

---

## 3. Where treg.to fits — the gaps nobody explains how to fetch

The web playbooks all say "go get X data" and stop. Checked against the catalog today:

| The playbook says | Nobody says how | treg.to has it (cheapest working option, no key needed) |
|---|---|---|
| "Posted SDR/BDR roles in last 90 days" | ✗ | `apify.linkedin.search.jobs` $0.001 · `leadmagic.x.jobs-search-v3` $0.025 · `predictleads.companies.job_openings` $0.04 |
| "Raised Series B/C in last 18 months" | ✗ | `aviato.companies.funding_rounds` $0.01 · `predictleads.companies.financing_events` $0.04 · `predictleads.financing.discover` (who-just-raised feed) |
| "Doesn't yet use Salesforce" | ✗ | `tomba.companies.tech_stack` $0.0089 · `predictleads.technologies.users` (reverse lookup) $0.04 |
| "Research their last 3 LinkedIn posts" | ✗ | `tikhub.x.linkedin-web-v2-get-user-posts` $0.001 · `scrapecreators.linkedin.user.profile` $0.00188 (1,997 samples, 99.9%) |
| "Who engaged with the competitor's post" | ✗ | `scrapecreators.x.v1-linkedin-post` $0.00188 (likes, comments) — partial |
| "Scrape X + LinkedIn with Apify, Claude scores each lead" (@IAmAaronWill) | — | `scrapecreators.x.user.posts` $0.00188 · `tikhub.x.user.posts` $0.001 |
| "Find the VP Sales at each company" | — | `icypeas.people.search` $0.00038 · `findymail.search.employees` $0.0198 · `leadmagic.x.role-finder` $0.05 |
| Waterfall email finder | — | tomba $0.0089 → icypeas $0.019 → findymail $0.0198 → hunter $0.0245 → leadsforge/leadmagic $0.025 (6 providers, one token) |
| Verify before send | — | icypeas $0.0019 · leadmagic $0.00625 (2,997 samples) · hunter $0.01225 |
| Google Maps "niche + city, no website" | — | `dataforseo.x.business-data-business-listings-search-live` $0.013 |
| Scrape the prospect's website → markdown | ✗ | **Catalog gap.** Nothing Firecrawl/Jina-shaped; `dataforseo.x.on-page-content-parsing` is the nearest. Worth a `catalog_request`, because 3 of 8 YouTube systems have this step. |
| Website visitor de-anonymization | ✗ | Not in catalog (RB2B/Warmly); leave to own-key tools. |

Caveats to keep the pages honest (CLAUDE.md rules): treg **compares** these providers side by side, it does not waterfall/route automatically — the article's script does the waterfall in 6 lines, which is exactly the point. Findymail/Fiber misses bill at full rate until `_observed_cost_micro` is fixed (see memory) — say "a miss costs nothing" only for providers where it's true.

---

## 4. Articles to write on treg.to (copy, paste, run)

Format rule for every one: the article **is** a runnable file. One `SKILL.md` or one bash/Python script that calls `/call/` with a treg token, real endpoint ids, real prices, a real run's output pasted in, and the tuned prompt inline. This answers Reddit's #10 ("share the JSON") in a way vendors refuse to. Each should end with the exact cost of the run shown.

**Tier 1 — matches the biggest asked questions and the strongest catalog coverage**

0. **"The join problem, solved with three catalog calls"** — the framing from Akshay Pachaar's Seltz-sponsored X Article *Build a Multi-Agent GTM Intelligence System* (Aug 24, 172 likes / 28K views; [blog mirror](https://blog.dailydoseofds.com/p/build-a-multi-agent-gtm-intelligence)): the trigger (new VP, funding closed) and the person's record live in different places, so a snippet-returning search API forces search → fetch → parse per company and per person; a full-record API turns the join into a merge. Three agents: Signal Hunter (news) → People Enricher (career record) → Outreach Strategist (merge, rank, first line). Rebuild it on `predictleads.financing.discover` / `apollo.companies.news` for the trigger, `icypeas.people.search` + `scrapecreators.linkedin.user.profile` for the record, `tikhub` posts for the hook — **with prices**, which Seltz's piece never shows. Keep their honest "when to chain with open web search" section; ours is "when to use your own key".

1. **"The Clay-free waterfall: find and verify a work email across 6 providers for $0.01–0.025"** — the `email.find` cascade (tomba → icypeas → findymail → hunter → leadmagic), verify, stop at first hit. Reddit #5/#6/#9; the "150 providers behind one key" moat, done in a 40-line script. Compare to Clay credits per row. Reuses the shipped `/use-cases/lead-enrichment-for-ai-agents/` proof.
2. **"Hiring-signal outbound in one SKILL.md: companies that posted SDR roles this week → the VP Sales → verified email → templated first line"** — Clarence Nap's $35K/mo system, rebuilt on `apify.linkedin.search.jobs` + `icypeas.people.search` + email waterfall + Saraev's icebreaker prompt. The single most-cited signal across all sources.
3. **"Who just raised: a Monday cron that pulls last week's funding rounds and writes the entry-rule scorecard"** — `predictleads.financing.discover` / `aviato` + Explorium's scoring weights as a real Python dict + `0 2 * * * claude -p`. @itsalexvacca's "write the entry rule first" as the frame.
4. **"Score before you send: the 40-line lead qualifier (and why it 2–3x'd replies while dropping 40% of the list)"** — Lead Gen Jay's qualification prompt + tech-stack check (`tomba.companies.tech_stack`) + LinkedIn profile pull; output a yes/no/reason CSV. Reddit #2/#4.
5. **"LinkedIn post → first line: personalize 500 emails from each prospect's last 3 posts for under a dollar"** — the r/SaaS 363-pt method (`tikhub` posts $0.001 + GPT-4o-mini + "copy style not content" with 25+ examples). Pure copy-paste; the generic-post pre-filter included.

5b. **"Stack the signals: the $0→$2M compliance playbook, as a scoring script"** — from [@pierreeliottlal's post](https://x.com/pierreeliottlal/status/2092548845255209294) (Gojiberry founder, Aug 26: 203 likes, 30K views, **549 bookmarks**). A GDPR/SOC 2 vendor's whole GTM: (1) keywords buyers engage with when compliance becomes urgent, (2) monitor ICP people engaging with that content, (3) filter role/size/geo, (4) enrich, (5) contact while fresh, (6) monitor competitor company pages, founders, salespeople, employees — ICP engagers there are the warmest, (7) monitor Seed/Series A raises because questionnaires follow growth, (8) **stack**: fit → +topic engagement → +follows competitor → +just raised = call now. "Fit tells you who CAN buy, intent tells you who might buy NOW." Ours: the stack as an additive score with real calls behind each term — `predictleads.financing.discover` (7), `scrapecreators.x.v1-linkedin-post` engagers on competitor posts (6, partial), `tikhub` person posts (2, partial), `icypeas.people.search` + waterfall (3–4). Honest gap: keyword-level LinkedIn engagement monitoring (step 2) isn't in the catalog — file it; Gojiberry's moat is exactly that feed.

6. **"Local-business outbound that still works in 2026: Google Maps → no website → owner email → 100 a day"** — the r/LeadGeneration top answer, on `dataforseo` business listings + email find. Cheap, beginner, huge Reddit audience.
7. **"The 2026 outbound stack, mapped: what each tool does, which ones are one API call, and what it costs per lead"** — the nine-layer breakdown (@itsalexvacca's frame) with a price-per-1,000-leads table computed from the catalog. The comparison page Reddit #4/#6 keep asking for; honest about what treg doesn't do (send, warm, de-anon).
8. **"n8n vs Claude Code for outbound: the same hiring-signal pipeline built both ways"** — the orchestrator war, one HTTP node vs one skill, MCP vs CLI token cost. Growth Unhinged's "MCP → CLI" data point.
9. **"Where to store leads between runs: SQLite + a dedupe key, in 20 lines"** — Reddit #7; answers the "never re-contact the same lead" problem every system hits.
10. **"Reply classifier: six labels, one prompt, webhook to your sequencer"** — Explorium's schema, as a runnable script. Reddit #8.

**Tier 3 — LinkedIn and X specifically**

11. **"The comment-first LinkedIn play: pull the engagers on three competitor posts, score them, connect to 20 a day"** — `scrapecreators.x.v1-linkedin-post` + profile pull + SalesRobot's scripts + the 100/week caps stated plainly. Honest that treg reads LinkedIn; sending stays in HeyReach/Unipile/your hands.
12. **"Score X followers as leads: scrape a competitor's audience's posts, let Claude rank them, DM the top 50"** — @IAmAaronWill's viral inbound/outbound split on `scrapecreators.x.user.posts`.
13. **"Control inboxes: a 12-line weekly check that tells you whether Google moved or you did"** — the r/Coldemailing insight nobody has written up; pulls per-domain reply rates from Instantly/Smartlead as own-key tools.

**Don't write:** "best AI SDR tools 2026" listicles (the audience distrusts the category and it's vendor-saturated), anything promising automatic routing/failover, "how to X with ChatGPT" (dead per the pSEO teardown), and PDF-style "ultimate guides" — Lead Gen Jay's audience calls the perceived value of those "trash".

**Distribution loop:** each article's script doubles as (a) a `SKILL.md` installable via `install.sh`, (b) the answer to the matching Reddit thread (post the actual JSON/script — the one thing OPs there never do), (c) an X post in the @fivosaresti "here's the cheat sheet" format, which is what performs in this niche.

---

## 5. Where articles of that quality live (and what to copy from each)

Searched X Articles, the web and GitHub for the hands-on, code-included, honest-caveat shape of the Seltz piece. Four publisher shapes exist; none is written from the "many providers, one token, per-call price" angle.

**A. Sponsored newsletter walkthroughs (the Seltz shape).** Daily Dose of DS (Avi Chawla + Akshay Pachaar, 100K+ readers) runs a *[Hands-on] … explained with code* series where a vendor sponsors one build: Seltz (GTM), Mistral OCR, "Audio RAG with 200x cheaper vector DB", "Semantic code navigation cuts agent tokens 36%", "Query billion-row Postgres". Format: problem framing → 3-agent architecture → CrewAI/MCP code → Streamlit demo → Lightning Studio repo → "when to use something else" → "thanks to X for sponsoring". Akshay's follow-up tweet ("I just built my own multi-agent GTM research assistant — it finds the reason to reach out before it writes a single message", 87 likes) is the distribution post. **Action:** price the slot; the brief writes itself from article #0.

**B. Vendor engineering blogs with real code.**
- Firecrawl — *How to Build an AI SDR that Researches Companies in Real Time* (Jun 2026): Python, four stages (search → schema extract → write → batch to 50 concurrent), Pydantic schema, cites Belkins/Woodpecker with links and flags vendor-sourced stats as "directional". Also *Bulk Sales Lead Extractor in Python*, *Complete Guide to Data Enrichment*, n8n templates. This is the bar for code quality.
- Explorium — *How to build an Outbound Agent with Claude Code* (May 2026): CLAUDE.md, researcher + writer agents, five-stage pipeline, 8-point production audit (HMAC, DLQ, ICP gating), 30/60/90 roadmap, G2 quotes against Apollo/Clay ("credits per row vary 100% from stated"). Heavier on positioning; its "$18K SDR → $200 agent" framing is the one being shared.
- FoxReach — *How to Build an AI SDR: 2026 Build Guide*: the 8-stage closed loop table (source → enrich → research → draft → send → triage → hand off → feedback); "you own the brain, rent the hands"; sending backend as typed tools with JSON schemas. Best architecture prose of the set.
- The Signal Club's Eric Nowoslawski profile, SyncGTM, Salesforge (already in §1) — operator-voice, fewer code blocks.

**C. Open-source GTM skill packs for Claude Code (the fastest-moving shape).**
| Repo | ★ | What it is | Why it matters |
|---|---|---|---|
| `gtmagents/gtm-agents` | 393 | Claude Code plugin marketplace of GTM agents; "generate 100 leads in 5 minutes" use-case docs | The distribution model — a `/plugin marketplace add`; treg's `skill.md` could ship as one |
| `oneshot-agent/oneshot-gtm` | 308 | GTM agent for technical founders, pay-per-result, signed receipts, CLI + local dashboard | Pay-per-result is our billing story told by someone else |
| `Othmane-Khadri/YALC-the-GTM-operating-system` | 288 | "The open-source Clay alternative": MIT, CLI-first, runs in Claude Code, enrichment and workflows in markdown you own, "you pay providers direct" | **Directly adjacent.** YALC 1.0 needs a key per provider; treg is one token for all of them. A "run YALC on treg" article or PR is the highest-leverage integration on this list |
| `AIDevGTM/gtm-cofounder` | 248 | #1 Product of the Day; strategy-side skills (positioning, first users, pricing) | Not data; skip |
| `LeadMagic/gtm-skills` | 45 | 206 SKILL.md skills incl. `outbound-stack`, `prospecting-stack`, `rb2b-outbound-triggers`, `cold-email-strategy`, with QA scripts | LeadMagic is a catalog provider (`leadmagic.people.email.find`, 3,130 samples). Their skills call LeadMagic directly; a fork that calls `/call/` gets a waterfall for free |

**D. The X Article format itself.** Long-form X Articles with a code walkthrough and a "here's the cheat sheet" hook are what perform in this niche this month: @fivosaresti (Claude Code outbound cheat sheet), @itsalexvacca (nine-layer GTM stack, 21 MCPs), @harsehaj ("how I built an SEO/AEO blog engine" as a Browserbase intern, 1,199 likes), @_avichawla ("Cut agent tokens 2.7x", 165K views). The pattern: one specific build, numbers, a diagram, the repo link, an honest limits section.

**What none of them do, which is our opening:** show the price of every call, compare providers for the same step side by side, and let the reader run it without signing up for four vendors. Every article above ends with "get an API key from X, Y and Z".

---

## 6. What to do with it — one build, five outputs

The principle: **build one recipe for real, get a receipt, then reuse that single run everywhere.** The receipt (the terminal showing three `/call/`s, their prices, the merged output) is the asset nobody else in §5 can produce, because they don't have per-call prices or six providers behind one token.

### The build (week 1): the join-problem recipe, run for real
- One `SKILL.md` + one script: Signal Hunter (`predictleads.financing.discover` or `apify.linkedin.search.jobs`) → People Enricher (`icypeas.people.search` → `scrapecreators.linkedin.user.profile` → email waterfall → verify) → Strategist (merge, rank, one-signal first line with Saraev's prompt).
- Run it on 20 real companies. Save: the exact commands, every call's price, total cost, the ranked output, what missed. That run is the receipt.
- Fix or caveat before publishing: the Findymail/Fiber miss-billing (say "a miss costs nothing" only where true); file `catalog_request` for website→markdown scraping so the recipe can research a homepage.

### Output 1 — X (Jason's account first, @treg second)
| Post | Format | Model it copies |
|---|---|---|
| **The join problem, with a receipt** | X Article + hook tweet: "Cold outreach fails on timing, not wording. The trigger and the person live in different records. Here's the join in 3 calls, $0.04 per lead, receipt attached." Terminal screenshot of the run, diagram of the three agents, repo link, honest "when to use your own key" | Akshay's Seltz piece; @fivosaresti's cheat-sheet close ("bookmark + send to your team") |
| **The price-receipt series** (weekly) | One image: one outbound step, all providers side by side — price, measured success %, median ms, samples. "Email verification: 5 providers, $0.0019 to $0.0123, which one your agent should pick." No other account can post this table | @itsalexvacca's nine-layer stack, made numeric |
| **Value replies with the script** | Reply to the viral outbound posts (@IAmAaronWill's inbound/outbound split, @itsalexvacca's entry rule, @aryanXmahajan's AI SDR) with the 10-line version that does their step, prices inline. The paused "X value replies" loop is the vehicle | The one thing Reddit OPs won't do: share the actual code |
| **"Clay's moat in 40 lines"** | Quote Eric N.'s "150 pre-negotiated providers" line, show the waterfall script, the cost per row vs Clay credits | The Clay-alternative threads (40–63 comments each) |
| **YALC-on-treg** | Joint announce with the YALC maintainer once the PR lands: "the open-source Clay alternative now runs on one token" | Open-source cross-post; both audiences |

Rules from the teardown: first tweet carries the number and the receipt, not the pitch; never claim routing; "genuinely" is fine, em-dashes are the tell.

**The format that gets bookmarked** (Pierre's post, 549 bookmarks on 203 likes): a named outcome in line one ("$0 to $2M ARR with one play"), a concrete niche so it feels real (GDPR/SOC 2 → US companies), eight numbered steps, one thought per line, no image, the product named once in the last three lines, "try it free" as a self-reply. Bookmarks, not likes, are the metric for playbook posts — write for the save.

### Output 2 — treg.to articles: a `/recipes/` shelf
- **Page type:** `/recipes/<slug>`, server-rendered from the catalog like `/use-cases/` (`agent_pages.py` `_USE_CASES` pattern, so a new recipe lands in the sitemap for free). Prices and success rates pulled live from the catalog, so the page never goes stale.
- **Fixed anatomy** (copy Firecrawl's rigor, Akshay's framing, FoxReach's loop table): (1) the Reddit question, quoted; (2) the join — which records live where; (3) the calls, with a provider table for each step; (4) the receipt — a real run, total cost; (5) the prompt, inline; (6) "when to use your own key / when to use open web search"; (7) install: `treg skill install <slug>` or the `SKILL.md` download.
- **Order of publication** = §4 Tier 1: join problem → email waterfall → hiring signal → who-just-raised cron → lead qualifier → LinkedIn posts → first line. One a week; the daily "treg.to SEO pages" loop keeps shipping catalog pages underneath.
- **Agent × recipe pages** already have the machinery (`/for/claude-code`, `/for/cursor` agent pages): each recipe gets an install block per agent, which is Pipedream's "agent × platform" win without the empty matrix.
- Each recipe also lands in `src/treg/web/skill.md` / `llms.txt` as a one-line "recipes" index — agent-facing files are the front door.

### Output 3 — distribution beyond our own channels
- **Sponsor the Daily Dose of DS slot.** The brief is article #0; ask for the same anatomy (3 agents, CrewAI or Claude Agent SDK, Streamlit, hosted repo) with treg as the retrieval tool. Fits the creator-sponsorship criteria (workflow builders, rank on views).
- **YALC PR:** a treg provider adapter so "you pay providers direct" becomes "one token". Highest-leverage integration found; 288★, MIT, runs in Claude Code.
- **Fork `LeadMagic/gtm-skills` `prospecting-stack`** to call `/call/` — LeadMagic is already a catalog provider, so this is a waterfall upgrade of their own skill; offer it upstream.
- **List treg in `gtmagents/gtm-agents`** as a marketplace plugin (`skill.md` already exists).
- **Answer the five Reddit threads** (r/MarketingAutomation "where to start", r/gtmengineering Clay alternatives ×2, r/n8n "where to store leads", r/SaaS personalization) with the recipe script, not a link — via the paused Reddit karma loop, one value comment per thread.

### Sequence
Week 1 build + receipt → week 2 X Article + recipe page #1 + YALC PR opened → week 3 price-receipt series starts, Reddit answers, DDoDS outreach → weekly thereafter one recipe, one receipt image, replies as they come. Measure with the existing GSC+Ads loop: impressions on `/recipes/`, first-call rate from those pages (the funnel still needs instrumenting — see memory).

---

## 7. Measured: where this fits the SEO strategy (DataForSEO via the catalog, 2026-08-27, ~$0.16)

Full table and linking map: `marketing/rebuild/00-strategy.md`. Drafts: `marketing/rebuild/01–05`.

**Winnable, worth a page:** "clay alternative(s)" 480+480/mo, CPC $36.72, **KD 0** (SERP = listicles, an r/gtmengineering thread, an HN "should I build one" post) · "clay pricing" 1,900 (KD 3) · "clay api" 390 · "gtm engineering / gtm engineer" 3,600 LOW competition, untargeted by anyone in our sources · "gojiberry ai" 1,600 + "trigify" 720 + "teamfluence" 90 — the no-API subscription LinkedIn-intent tools have real brand volume · "ai sdr" 1,900 (−46% YoY, CPC $92) with "ai sdr open source" showing a bare GitHub repo at #1 · "linkedin scraper" 1,000 / "linkedin profile scraper" 210 / "linkedin post scraper" 110.

**Dead:** "rebuild clay", "build your own clay", "clay clone", "signal based outbound" (10), "waterfall enrichment api" (10), "open source clay alternative" (10), and every "{x} api" phrasing except "email verification api" (210). Same lesson as the memory note: buyers type "scraper", "alternative", "pricing", "vs".

**The shelf:** `/rebuild/` — Rebuild Clay (clay alternative + pricing + api + waterfall) · Rebuild Gojiberry/Trigify (LinkedIn intent, with the keyword-monitoring gap stated) · Build your own AI SDR (the loop; sending stays in your sequencer) · The GTM engineering stack, priced (the 3,600 term) · The join problem (repurposed; doubles as the X Article and the DDoDS brief). Every page is a recipe with a run receipt; every existing use-case page links to the rebuild that extends it, and every rebuild's provider table links back to the catalog comparison pages.

---

## Raw material
Scratchpad: `x_opencli.txt`, `x_threads.txt`, `x_users.txt`, `reddit.txt`, `reddit_threads.txt`, `yt/*.txt` (8 transcripts), `c_p1..8.md` (web pages). Agent Reach v1.5.0 is current.
