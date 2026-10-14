---
page_id: r5
slug: /rebuild/join-problem/
seo_title: "The Join Problem: Signal + Person in Three Calls | treg.to"       # 55
meta_description: "Cold outreach fails on timing, not wording. The trigger and the person live in different records. Here is the join — funding feed, people search, profile — as three priced calls."  # 176 → trim
h1: "The join problem: find the trigger and the person in one pass"
seo_terms:
  primary: "intent signals"              # 170 — editorial page; distribution is X Article + newsletter, not search
  secondary:
    - "buying signals"                   # 260
    - "hiring signals"
    - "company funding data"
capabilities: [companies.funding, jobs.search, people.search, linkedin.user.profile, people.email.find]
links_out: [/use-cases/company-research-for-ai-agents/, /rebuild/clay/, /rebuild/ai-sdr/]
links_in: [p5, /rebuild/gtm-engineering-stack/]
repurposed_from: "Akshay Pachaar / Daily Dose of DS, 'Build a Multi-Agent GTM Intelligence System' (Seltz-sponsored, 2026-08-24) — framing credited; pipeline rebuilt on the catalog with prices"
status: draft 2026-08-27 · this is also the X Article and the DDoDS sponsorship brief
---

# The join problem: find the trigger and the person in one pass

Most teams wait nine months after a lost deal to reach out again. The right moment isn't on the calendar. It's when something changed — a new VP joined, a round closed, a leadership hire landed in the right function.

The problem, as a recent walkthrough on Daily Dose of DS put it, is that the signal and the person don't live in the same place. "Who just joined" is a people record. "What the company announced" is a news record. A search API returns snippets of each, so an agent has to search, fetch, parse, and repeat for every person and every company before it can answer "who should I email today, and why."

That is the join problem. This page does the join with three calls, and prints what each one cost.

## Three agents, three calls

| Agent | Job | Call | Price | Measured |
|---|---|---|---|---|
| Signal Hunter | Trigger events at target companies in a window | `predictleads.financing.discover` (who just raised) · `apify.linkedin.search.jobs` (who's hiring) · `apollo.companies.news` | $0.04 · $0.001 · $0.026 | 228 · 92 · 1 |
| People Enricher | The full record of the person the trigger names, or the person the role implies | `icypeas.people.search` → `scrapecreators.linkedin.user.profile` | $0.00038 → $0.00188 | 347 · 1,997 (99.9%) |
| Outreach Strategist | Merge, rank, one-signal first line | your model over two complete records | — | — |

Neither of the first two fetches a second page. The join in agent three is a merge across two full records, not a reconstruction from fragments. **Per ranked contact, about $0.045.**

## The script

```bash
# join.sh — last 7 days of raises → the revenue leader at each → ranked
treg call predictleads.financing.discover --query 'days=7' --query 'min_amount=5000000' \
  | jq -c '.data[] | {domain:.attributes.domain, amount:.attributes.amount, date:.attributes.date}' > raises.jsonl

while read -r R; do
  D=$(echo "$R" | jq -r .domain)
  P=$(treg call icypeas.people.search --method POST --data "{\"query\":{\"currentCompanyWebsite\":{\"include\":[\"$D\"]},\"currentJobTitle\":{\"include\":[\"VP Sales\",\"CRO\",\"Head of Revenue\"]}},\"pagination\":{\"size\":1}}" | jq -c '.items[0]')
  PROF=$(treg call scrapecreators.linkedin.user.profile --query "url=$(echo "$P" | jq -r .linkedinUrl)")
  jq -n --argjson r "$R" --argjson p "$P" --argjson prof "$PROF" '{trigger:$r, person:$p, record:$prof}'
done < raises.jsonl > joined.jsonl
```

Then the strategist, which needs no retrieval:

```
For each record in joined.jsonl: rank by (amount, days since raise, seniority, tenure < 6 months).
Write one first line, under 25 words, that infers what the raise means for this person's next quarter.
Never say "congrats on the raise." Output JSON: {rank, domain, name, first_line, why}.
```

## When to chain with open web search

The walkthrough this page credits was honest about its retrieval tool's limits, and the same honesty applies here. For the single most senior executive at a company, a plain web search is often the stronger first call; the catalog's people search is strongest at the director and VP layer below. For discovery — "which companies should I even target" — use search or the funding feed, then the catalog for depth on each. And for reading a company's own site, the catalog has no page-to-markdown tool yet; Firecrawl or Perplexity on your own key covers that stage.

## The run receipt

> `[7 days of raises ≥ $5M → N companies → N revenue leaders found → N profiles → top 10 ranked with first lines, total $X.XX, N minutes]`

Related: [Research any company list without buying a database](/use-cases/company-research-for-ai-agents/) · [Rebuild Clay](/rebuild/clay/) · [Build your own AI SDR](/rebuild/ai-sdr/)
