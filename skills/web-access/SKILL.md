---
name: web-access
description: Use only when the agent's own web tools can't do the job. Use what you already have first (built-in web search and fetch, a browser, MCP servers the user installed); come here when none of them covers it, such as a page they can't read, structured data from a platform they have no tool for (X, LinkedIn, Reddit, TikTok, Instagram, YouTube, Xiaohongshu, Amazon, app stores, Google Maps, papers), or when the user asks for treg. Not for localhost, staging, internal pages or public docs your own fetch can read. Starts free, and treg falls back to the next vendor when one fails.
metadata:
  summary: Reaches the web data your own tools can't. Pages a normal fetch can't read, and structured data from platforms like X, LinkedIn, Reddit, YouTube and Amazon, starting with free tools.
---

# Web access

One treg key reads the web: free search and page fetch first, and the rest of the catalog behind them for the
pages and platforms a free fetch can't reach. Nothing runs on this machine: no browser, no cookies,
no burner accounts. treg calls the vendor and, on a routed job, tries the next one when a vendor
comes back empty or blocked.

## When to use this

Your own tools come first. Whenever the built-in web search or fetch, a browser, or an MCP server the
user installed can do the job, use it and leave this skill alone. Come here only when none can:

- a page your own fetch can't read (blocked, needs JavaScript, rate-limited)
- structured data from a platform you have no tool for (posts, profiles, products, reviews, places)
- the user asked for treg

Never send localhost, staging, an intranet or anything behind the user's login through treg: it
cannot reach them, and they are not public.

Runs on treg. If it isn't set up (`treg --version` fails), don't install it on your own: tell the
user what it would add and ask. With their yes, follow https://treg.to/llms.txt to install and sign in.

## Pick the job

```
Know the URL?             → read it (free first)
Need to find pages?       → search (free) → read the 3–5 best results
A platform object?        → never scrape its HTML: use the platform's job
  (post, profile, video,     structured fields, fewer tokens, survives redesigns
   product, review, place)
A whole site?             → map it → read only the pages that matter
Need typed fields?        → structured extract with a JSON schema or an instruction
Need it recent or dated?  → news search, not web search
```

## Free first

Web search and page reads are free through TinyFish:

- `treg call treg.web.search --data '{"q":"…"}' --header 'X-Treg-Route-Prefer: tinyfish'`
- `treg call treg.web.extract --data '{"url":"…"}' --header 'X-Treg-Route-Prefer: tinyfish'`

Both are routed: when TinyFish comes back empty or blocked, treg tries the next vendor (Crawl4AI,
AnyAPI, …). You pay the vendor that answered plus any vendor before it that bills a miss (`treg catalog
get` says how many do); `X-Treg-Route-Max-Cost` caps the walk. To read up to ten URLs in one free call, call
`tinyfish.web.fetch` directly with `{"urls":[…]}`; it does not fall back.

Platform data (social, stores, reviews, maps) is not free: cents or fractions of a cent a call. Say so
before a batch.

## Jobs

Search the catalog for the words in the second column. The vendors are examples of who answers today;
take whatever the catalog ranks best now.

| You want | Search the catalog for | Example vendors | Notes |
| --- | --- | --- | --- |
| Web results | `treg.web.search` | TinyFish, Exa, Perplexity, Keenable | free with the TinyFish header |
| A page as markdown | `treg.web.extract` | TinyFish, Crawl4AI, AnyAPI | free first; blocked pages fall back |
| Typed fields from a page | `treg.web.extract.structured` | | send `url` plus a `schema` and/or `instruction` |
| Every URL on a site | `treg.web.map` | AnyAPI, Firecrawl, Tavily | add `q` to rank URLs for a topic |
| Many pages of a site | "crawl a website" | Firecrawl, Crawl4AI, Olostep | map first when you need only some pages |
| Recent news | `treg.web.search.news` | Serper, DataForSEO, Exa | dated results |
| Papers | "search research publications" | TinyFish, Exa, Serper | |
| X, LinkedIn, Reddit, TikTok, Instagram, Threads, Facebook | "x search posts", "linkedin company posts", "reddit subreddit posts", "tiktok video comments", … | TikHub, ScrapeCreators, HarvestAPI, AnyAPI | profiles, posts, comments, reactions as fields |
| YouTube, Bilibili | "youtube video comments", "youtube search videos" | Crawl4AI, TikHub, ScrapeCreators | |
| Xiaohongshu, Douyin, Weibo | "xiaohongshu user notes", "douyin search videos" | TikHub, JustOneAPI | |
| Stores | "amazon product detail", "amazon search products", "walmart search", "ebay search" | JustOneAPI, SerpAPI, Crawl4AI | price, rating, reviews as fields |
| Reviews | "app store reviews", "google play app", "trustpilot reviews", "glassdoor reviews", "google maps reviews" | Crawl4AI, Bright Data, LiteScrape | |
| Places | "google maps search", "local results" | DataForSEO, SerpAPI | |
| Google results as Google shows them | "google serp organic" | DataForSEO, Serper | ranks, snippets, features |

## Finding the endpoint

The catalog changes every week, so never trust a remembered endpoint id.

1. `treg catalog search "<the job in plain words>"`, not a vendor name.
2. Prefer a routed `treg.*` endpoint when there is one: the team's own keys first, then vendors in
   order, and most misses are free.
3. `treg catalog get <id>`: the price, the vendors behind a routed job, and each one's measured
   **hit rate** (it found something), not just works (it returned 200), and speed.
4. One small call first. Read `_treg.served_by`, `_treg.tried` and the reported charge, then scale.
5. Nothing fits: `treg catalog request "<what is missing>"`.

## Steering a routed job

Options ride as headers, never in the body:

| Header | Use it to |
| --- | --- |
| `X-Treg-Route-Prefer: <vendor>` | try that vendor first (free TinyFish, or the one that worked last time) |
| `X-Treg-Route-Exclude: <vendor>` | skip a vendor that gave you bad answers |
| `X-Treg-Route-Max-Cost: <usd>` | cap one call; uncapped, a miss can walk up to $1 of vendors |
| `X-Treg-Route-Waterfall: 0` | stop at the first miss when only one vendor's answer will do |

Batches: always send a `Max-Cost`.

## Habits that save money and tokens

- Read pages as markdown, and cut navigation and footers before they reach the context.
- A platform job returns fields; a scraped page of the same thing costs several times the tokens.
- Search, then read only the results you need. Never read a whole site to answer one question.
- Keep the source URL next to every fact you hand back.

## Recipes

- **Research a company:** web search → read its site, pricing and docs pages → news search for the last
  90 days → its LinkedIn company profile.
- **Read a docs site into context:** map the site with `q` set to the topic → read the top pages.
- **Watch a page for changes:** read it, keep a hash of the text, read it again next run and report the
  diff. Schedule it with the user's agent (Claude Code `/schedule` or `/loop`, cron for others).
- **Social listening:** search posts on X, Reddit and LinkedIn → read the comments on the best ones →
  summarise by theme with links.
- **Compare a product across stores:** search it on each store → product detail for the matches → one
  table of price, rating and review count.
- **Collect reviews:** app store and Google Play for apps, Trustpilot and Glassdoor for companies, Google
  Maps for places → quote the patterns, with counts.
- **Videos into notes:** YouTube search → video detail and comments → summarise.

## Check the setup

```
treg --version && treg balance
treg call treg.web.search --data '{"q":"treg web access"}' --header 'X-Treg-Route-Prefer: tinyfish'
```

A result with `_treg.served_by` and no charge means free search works. Something wrong:
`treg feedback "<what happened>"` reaches the team.

## Lines not to cross

Public data only. Never log in as the user, ask for their cookies or get round a paywall. Don't bulk
collect personal data about private individuals. When a vendor refuses a page, take the fallback or
tell the user; don't hammer it.
