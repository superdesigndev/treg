---
page_id: r1
slug: /rebuild/clay/
seo_title: "Clay Alternative: Rebuild the Waterfall in 40 Lines | treg.to"           # 58
meta_description: "Rebuild Clay's enrichment waterfall on six email providers with one token. Per-call prices, a real run receipt, and the honest list of what Clay still does better."  # 158
h1: "Rebuild Clay: the enrichment waterfall, priced per call"
seo_terms:
  primary: "clay alternative"            # 480/mo, CPC $36.72, KD 0
  secondary:
    - "clay pricing"                     # 1,900
    - "clay api"                         # 390
    - "clay vs apollo"                   # 260
    - "waterfall enrichment"             # 140
    - "open source clay alternative"     # 10, but the SERP is the builders
capabilities: [people.email.find, people.email.verify, people.search, companies.enrich, companies.tech_stack]
links_out: [/use-cases/lead-enrichment-for-ai-agents/, /use-cases/company-research-for-ai-agents/, catalog:email-finder-api, catalog:email-verification-api, catalog:people-search-api]
links_in: [p2, p5, catalog:email-finder-api, /for/claude-code]
status: draft 2026-08-27 · needs a real run receipt before publish
---

# Rebuild Clay: the enrichment waterfall, priced per call

Clay's moat, in the words of the agency that runs 250 Clay tables at once: "150-plus pre-negotiated data providers — one datapoint from HG Insights without an HG contract." That is the part people pay for. The visual table is the wrapper.

This page rebuilds that part. One token, six email-finding providers, a verifier, a people search and a tech-stack check, called in order until one answers. Forty lines. Every call has a price on it before you run it.

It does not rebuild the table, the Claygent research column, or the 150 integrations. The last section says exactly where Clay is still the right buy.

## What a Clay row actually costs

Clay bills in credits, and the credit cost of a row depends on which providers the waterfall touches. Users report the gap between stated and actual: "Per-row credit cost can vary 100% from stated amounts (e.g., stated 11 credits/row, actual 25)" — a verified G2 review. r/gtmengineering's most-commented threads this year are "cost-effective alternatives to Clay" (50+ comments) and a founder whose last ten customers "migrated from Clay" because the alternative was "50 to 70% cheaper."

The per-call version has no credit. A found email is one price; a verification is another; you see both before the call.

| Step | Providers, one token | Price per call | Measured |
|---|---|---|---|
| Find a work email (name + domain) | tomba → icypeas → findymail → hunter → leadsforge → leadmagic | $0.0089 → $0.019 → $0.0198 → $0.0245 → $0.0245 → $0.025 | hunter 5,206 calls · leadmagic 3,130 · tomba 445 (98%) |
| Verify it | icypeas → leadmagic → hunter | $0.0019 → $0.00625 → $0.01225 | leadmagic 2,997 calls, 100% |
| Find the person by title | icypeas.people.search → findymail.search.employees | $0.00038 → $0.0198 | icypeas 347 calls |
| Tech stack by domain | tomba.companies.tech_stack | $0.0089 | 8 calls |
| Company profile | scrapecreators LinkedIn company page | $0.00188 | 754 calls, 100% |

Typical case, first or second provider hits: **$0.01–0.03 per verified email**. Every finder in the loop documents a miss as free, and for tomba, hunter, leadmagic and leadsforge we have watched the credit counter confirm it. One caveat from our own ledger: treg currently meters findymail (and Fiber) at the full rate whether or not the call hits, until the cost parser for those providers is fixed — budget findymail rungs as if every call were a hit.

## The script

```bash
# waterfall.sh — find + verify one work email across five sync providers, cheapest first, stop at first hit
NAME="$1"; DOMAIN="$2"; FIRST="${NAME%% *}"; LAST="${NAME#* }"
try() { EMAIL=$(eval "$2" | jq -r "$3 // empty"); [ -n "$EMAIL" ] && VIA="$1"; }
try tomba     "treg call tomba.people.email.find --query domain=$DOMAIN --query 'full_name=$NAME'"                                   '.data.email'
[ -z "$EMAIL" ] && try findymail "treg call findymail.search.name --method POST --data '{\"name\":\"$NAME\",\"domain\":\"$DOMAIN\"}'" '.contact.email'
[ -z "$EMAIL" ] && try hunter    "treg call hunter.people.email.find --query domain=$DOMAIN --query 'full_name=$NAME'"                '.data.email'
[ -z "$EMAIL" ] && try leadsforge "treg call leadsforge.people.email.find --method POST --data '{\"firstName\":\"$FIRST\",\"lastName\":\"$LAST\",\"companyDomain\":\"$DOMAIN\"}'" '.email'
[ -z "$EMAIL" ] && try leadmagic "treg call leadmagic.people.email.find --method POST --data '{\"full_name\":\"$NAME\",\"domain\":\"$DOMAIN\"}'" '.email'
[ -z "$EMAIL" ] && { echo "miss"; exit 1; }
STATUS=$(treg call leadmagic.people.email.verify --method POST --data "{\"email\":\"$EMAIL\"}" | jq -r '.email_status')
echo "$EMAIL  $STATUS  via $VIA"
```

Each provider has its own parameter shape — two take a query string, three take a JSON body — which is exactly the tedium Clay's waterfall column hides. `treg catalog get <endpoint>` prints the exact input, the response path and the price. Icypeas is left out of the sync loop because its finder and verifier are async (submit, then read the result by id); add it as the sixth rung with `icypeas.bulk.search` when you run lists. The loop is the whole product: treg shows the providers side by side with price and success rate — **the script picks the order; treg does not route or fail over on its own.**

Run it over a CSV and you have Clay's "Find work email" waterfall column. Add the people search in front and you have the "Find people at company" column. Add the tech-stack call and you have the "Uses Salesforce?" filter.

## The run receipt

> `[to be pasted from the real run before publish: 50 contacts from a Series-B SaaS list — N found on provider 1, N on provider 2…, N verified deliverable, total $X.XX, wall time]`

## Clay vs Apollo vs this

| | Clay | Apollo | Per-call catalog |
|---|---|---|---|
| Data | 150+ providers behind credits | One provider's database ("under 10% valid phone numbers in our industry" — r/n8n) | 6 email finders, 3 verifiers, 11 company-search providers, each priced |
| Pricing unit | Credits, monthly plan | Seats + credits; export is "too expensive," so people scrape it via Apify | Per call, prepaid balance, $1.00 free |
| Orchestration | The table, Claygent, integrations | The app | Your script, n8n, or Claude Code — the catalog is only the data layer |
| Where it wins | Non-technical teams, edge cases, 50 tables running unattended | Search graph for building TAM | Anyone who already runs their outbound from code or an agent |

## Where Clay is still the right buy

- You want a spreadsheet, not a script. The table is genuinely good.
- You need Claygent-style "read the website and answer a question per row." The catalog has no raw website-to-markdown tool; it does have named-field extraction by domain (`branddev.brand.ai.query`, $0.025), which covers the structured half. For free-text reading use Firecrawl or Perplexity on your own key.
- You run 50 tables unattended and want Clay's queueing and rate-limit handling. Here, that's your job.
- Open-source route: YALC (MIT, CLI-first, runs in Claude Code) does the orchestration half; it still needs a key per provider, which is the half this page replaces.

## Install

```bash
curl -fsSL https://treg.to/install.sh | sh     # adds skill.md to Claude Code / Cursor
# the waterfall script above is the recipe; a packaged skill is not published yet
```

Related: [Find and verify decision-makers from one agent prompt](/use-cases/lead-enrichment-for-ai-agents/) · [Email finder API: 6 providers compared](/catalog/email-finder-api) · [Email verification API: 3 verifiers compared](/catalog/email-verification-api) · [Research any company list without buying a database](/use-cases/company-research-for-ai-agents/)
