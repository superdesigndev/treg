# Research with the treg CLI

The Semrush skills (https://github.com/semrush/skills) describe the method; treg supplies the data.
Every call below is billed to the team balance and prints its cost. Ask for only the rows you need,
start small, and expand after reading the first batch. Keep raw rows and your conclusions apart,
and say which source a number came from. Before relying on any endpoint, read it:
`treg catalog get <id>` shows its parameters, price and measured reliability.

Results (volumes, SERPs, AI answers) go to `.context/` or the private `treg-internal` repo, never
into a committed file in this repo.

## 1. Keyword demand (keyword-research)

Volume, difficulty, CPC and intent for many keywords in one call (billed per keyword returned):

    treg call seranking.google.keywords.volume --method POST --query source=us \
      --data '{"keywords":["clay alternative","clay pricing","clay mcp"]}'

Cross-check terms that come back zero or look low with Google Ads data (one call, up to 1,000
keywords):

    treg call dataforseo.google.keywords.volume --method POST \
      --data '[{"keywords":["openrouter pricing"],"location_code":2840,"language_code":"en"}]'

Ideas from a seed: `treg catalog get treg.google.keywords.ideas` (routed; it names the provider).

Method:
1. Seeds per product: "<X>", "<X> alternative(s)", "alternatives to <X>", "<X> competitors",
   "<X> vs", "<X> pricing", "<X> cost", "is <X> free", "<X> mcp", "<X> mcp server", "<X> api",
   "<X> open source", "<X> self hosted", "<X> review", plus category terms ("mcp gateway",
   "remote mcp server", "api marketplace", "unified api").
2. Intent: "alternative", "vs", "best" are commercial; "pricing", "cost", "free" are pricing
   intent; "mcp", "api" are feature intent. A brand that is also a common word (Locus, Clay,
   Arcade) has polluted volume: check the SERP before trusting it.
3. When two sources disagree, keep both with their source; do not pick the nicer one.
4. Low volume is not a reason to skip a page: AI answers matter for new categories.

## 2. Google results (serp-analysis)

Top results, People also ask, related searches and the AI Overview's cited domains:

    treg call dataforseo.x.serp-google-organic-live-advanced --method POST \
      --data '[{"keyword":"clay alternative","language_code":"en","location_code":2840,"depth":10}]'

Read `items[]` by `type`: `organic` (rank, domain, title), `people_also_ask`, `related_searches`,
`ai_overview` (its `references`). What to note: the winning format (list pages for "X
alternatives", head-to-head pages for "X vs Y"), the questions people ask, who the AI Overview
cites, and whether a forum or video block shows up.

## 3. AI answers (AI visibility)

What ChatGPT answers a logged-out user, with its sources:

    treg call cloro.ai-search.chatgpt.scrape --method POST \
      --data '{"prompt":"What is a good Clay alternative for an AI agent?","country":"US","include":{"markdown":true}}'

Other engines: `treg catalog search "google ai mode answer"` (Google AI Mode / AI Overview rows)
and `perplexity.web.answer.fast`. Use prompts that do not name treg as well as ones that do. Record
whether treg is named, its position, the treg URL cited, the competitors named and the cited
domains. Note the words the answer uses for the category; use them on the pages.

## 4. The competitor's own pages

WebFetch first. When a site blocks it: `treg catalog search "read a web page as markdown"`. For
pages that contradict each other, quote both with the day you read them.

## 5. Technical, schema and internal links (technical-seo-audit, schema-markup, internal-link-audit)

Run against the local preview served as treg.to (see `references/audit-checklist.md`):
- one `h1`, a logical `h2`/`h3` outline, title ≤ 60 and description ≤ 135 characters, a
  self-canonical, `robots` index, `og:*` and `twitter:*` with the page's own image;
- structured data parses (`WebPage` + `BreadcrumbList` + `FAQPage`), matches the visible text,
  invents nothing (no ratings, no prices that are not on the page);
- the page is in `/sitemap.xml` and `/llms.txt`, and `robots.txt` allows it;
- internal links: from the hub, from other comparisons (about as many for each page), from the
  product's `/tools/<provider>` page when the catalog has it, and from the site footers;
- every outbound link to another product is `nofollow`.
