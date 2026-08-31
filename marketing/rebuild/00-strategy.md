# "Rebuild X" — the outbound cluster, measured

Research: `marketing/_research-ai-outbound-playbook-2026-08-27.md`. Volumes: DataForSEO Google Ads, US, pulled 2026-08-27 through the catalog (`dataforseo.google.keywords.volume`, one $0.09 call for 100 terms; SERPs $0.002 each). KD from `dataforseo.google.keywords.ideas`.

## What the numbers say

| Term | Vol/mo | CPC | Comp | Read |
|---|---|---|---|---|
| gtm engineer / gtm engineering | 3,600 | $9.32 | LOW | Biggest informational term in the set; nobody in our sources targets it |
| crustdata | 2,400 | $31.43 | LOW | Brand; has an API. Comparison mention only |
| ai sdr | 1,900 | $92.30 | MED | −46% YoY. Category distrust on Reddit. Target with "build your own", not a listicle |
| clay pricing | 1,900 | $4.45 | MED | KD 3. The cost-per-row page, not the alternative page |
| ai lead generation (+ tools) | 1,600 + 480 | $54 / $47 | MED / LOW | Head terms; the recipes shelf as a whole targets these |
| gojiberry ai | 1,600 | $3.48 | MED | The no-API, subscription-only LinkedIn-intent tool. "gojiberry" (110K) is the fruit |
| trigify | 720 | $3.54 | MED | Same category, same page |
| clay alternative(s) | 480 + 480 | $36.72 | MED | **KD 0.** SERP = listicles + r/gtmengineering + HN "should I build one" |
| zoominfo / apollo alternative | 480 / 390 | $58 / $54 | MED | Mention inside the Clay page; don't build separate pages yet |
| clay api | 390 | $4.38 | LOW | Developers looking for what Clay doesn't sell |
| intent data / providers | 390 / 210 | $151 / $146 | MED | Bombora's SERP; enterprise buyers. One section, not a page |
| buying signals / intent signals | 260 / 170 | $12 / $71 | LOW / MED | The "stack the signals" page |
| clay vs apollo / zoominfo | 260 / 140 | $45 / $23 | MED | Comparison rows inside the Clay page |
| email verification api | 210 | $20.73 | LOW | Already a catalog page (`Email verification API: {n} verifiers compared`) — link to it |
| linkedin scraper / profile scraper / post scraper | 1,000 / 210 / 110 | $21 / $12 / $13 | MED | Scraper wording wins again. The LinkedIn-intent page carries these |
| waterfall enrichment | 140 | $26 | MED | SERP is Clay, Apollo, ZoomInfo, FullEnrich, Hunter — every vendor explains it, none prices it |
| predictleads / teamfluence / seltz ai | 170 / 90 / 50 | — | — | Mention as providers / comparisons |

**Dead, don't target:** "rebuild clay", "build your own clay", "clay clone", "cheap clay alternative", "signal based outbound" (10), "waterfall enrichment api" (10), "open source clay alternative" (10 — but its SERP is where YALC, OpenClay, Eigent live, so the Clay page should say "open source" once), every "{thing} api" phrasing except email verification. Buyers type "scraper", "alternative", "pricing", "vs".

## The cluster

Five pages, one shelf (`/rebuild/`), one repurposed article, and links from the five live use-case pages.

| # | Page | Primary term | Secondary | File |
|---|---|---|---|---|
| 1 | Rebuild Clay | clay alternative (480, KD 0) | clay pricing, clay api, clay vs apollo, waterfall enrichment, open source clay alternative | `01-rebuild-clay.md` |
| 2 | Rebuild Gojiberry / Trigify (LinkedIn intent) | gojiberry ai (1,600) | trigify, teamfluence, linkedin post scraper, buying signals, intent signals | `02-rebuild-linkedin-intent.md` |
| 3 | Rebuild an AI SDR | ai sdr (1,900) | ai sdr tools, ai sdr open source, build ai sdr | `03-rebuild-ai-sdr.md` |
| 4 | The GTM engineering stack, priced | gtm engineering (3,600, LOW) | gtm engineer, gtm agent, outbound agent | `04-gtm-engineering-stack.md` |
| 5 | The join problem (repurposed) | — (editorial; the X Article + DDoDS shape) | intent signals, hiring signals | `05-join-problem.md` |

Every page is a **recipe** — a runnable script with real endpoint ids, real prices and a run receipt — and every page says plainly what treg does not do (send, warm, route, de-anonymize) and where the catalog has a gap.

## Linking map (existing → new, new → existing)

| Existing page | Link to | Anchor |
|---|---|---|
| `/use-cases/lead-enrichment-for-ai-agents/` (p2) | Rebuild Clay | "the same waterfall, as a Clay replacement" |
| `/use-cases/company-research-for-ai-agents/` (p5) | Rebuild Clay · Join problem | "funding + people in one pass" |
| `/use-cases/social-creator-trends` (p3) | Rebuild LinkedIn intent | "the same post/profile tools, pointed at buyers" |
| `/use-cases/company-buying-signals` (p5) | Rebuild LinkedIn intent · Rebuild AI SDR | "signals → outreach" |
| Catalog pages: Email finder API / Email verification API / People search API / Person & company enrichment API | Rebuild Clay | provider tables cite them |
| Agent pages (`/for/claude-code`, `/for/cursor`, Claude MCP) | every rebuild page | install block per agent |
| `skill.md`, `llms.txt` | `/rebuild/` index | one line: "recipes that rebuild Clay, Gojiberry, an AI SDR on the catalog" |

Reverse links: each rebuild page's provider table links to the matching catalog comparison page; each "what we don't do" section links to the use-case page that covers the adjacent job.

## Honesty rules carried into every draft

- treg **compares** providers; the script does the waterfall. Never "routes", never "fails over".
- Findymail/Fiber misses bill at full rate until `_observed_cost_micro` is fixed — "a miss costs nothing" only for providers where it's measured true (tomba, hunter, leadmagic).
- Catalog gaps stated on the page: website → markdown scraping; keyword-level LinkedIn engagement monitoring; the *likers* of a post (the post endpoint returns commenters with URLs and only a like count); visitor de-anonymization; sending.
- Scripts use the real parameter shapes from `catalog get` (checked 2026-08-27 for tomba, findymail, hunter, leadsforge, leadmagic, icypeas people search, scrapecreators post). Icypeas's email finder/verifier are async and stay out of sync loops. Response paths (`.data.email`, `.contact.email`, `.items[0]`) still need confirming against a live run before publish.
- Vendor benchmarks cited as vendor benchmarks (Woodpecker 3.43%, Expandi 10.3%, Instantly 17–18%).
- No Clay price numbers we haven't verified; use their public credit model and the G2 quote Explorium cites ("stated 11 credits/row, actual 25").
