---
page_id: r3
slug: /rebuild/ai-sdr/
seo_title: "Build Your Own AI SDR: Open Source, Priced Per Lead | treg.to"     # 58
meta_description: "An AI SDR is a loop: signal, list, enrich, research, draft, send, triage. Here is the open-source version with treg as the data layer — every call priced, sending left to your sequencer."  # 176 → trim
h1: "Build your own AI SDR: the open-source loop, with prices"
seo_terms:
  primary: "ai sdr"                      # 1,900/mo, −46% YoY, CPC $92
  secondary:
    - "ai sdr tools"                     # 210
    - "ai sdr open source"               # SERP is a bare GitHub repo at #1
    - "build ai sdr"
    - "outbound agent"                   # 70
capabilities: [jobs.search, companies.funding, people.search, people.email.find, people.email.verify, linkedin.user.posts]
links_out: [/rebuild/clay/, /rebuild/linkedin-intent-signals/, /use-cases/company-buying-signals/, /use-cases/lead-enrichment-for-ai-agents/]
links_in: [p2, p5, /for/claude-code, /for/cursor]
status: draft 2026-08-27 · needs a run receipt
---

# Build your own AI SDR: the open-source loop, with prices

"AI SDR is a bullshit category, $45M later, still can't sell" is a top post on r/salesdevelopment this month. The complaint is specific: the funded products send from burned domains, personalize from scraped bios, and "over half of buyers churn inside 90 days." One founder who dogfoods his own: 1,842 contacts → 11.6% reply → 52 booked → 31 held. "The tool is maybe 30% of this. The list filter and the first line are the other 70%."

So this page is the 70%. It is the AI SDR as a loop you own — the shape every serious build guide converges on — with treg as the data layer and your existing sequencer as the hands. It is not a product that sends.

## The loop

| Stage | What runs | Data layer call | Price |
|---|---|---|---|
| Signal | Companies that posted SDR/AE roles this week, or raised in 90 days | `apify.linkedin.search.jobs` · `predictleads.financing.discover` | $0.001 · $0.04 |
| List | The VP Sales / Head of RevOps at each | `icypeas.people.search` | $0.00038 |
| Enrich + verify | Work email, waterfall, then verify | [the Clay waterfall](/rebuild/clay/) | $0.01–0.03 |
| Research | Their last three posts; the company page | `tikhub…get-user-posts` · `scrapecreators.x.v1-linkedin-company` | $0.001 · $0.00188 |
| Draft | Claude fills a fixed template (never writes the whole email) | your model | — |
| Send + sequence | Instantly / Smartlead / Lemlist, your key | own tool via treg, never metered | — |
| Triage | Six labels: interested / not now / OOO / referral / unsubscribe / angry | your model | — |
| Feedback | Reply rate by segment, weekly | Sheets / SQLite | — |

**About $0.05 per fully researched lead.** The $18K/month SDR vs $200/month agent framing that vendors use is theirs; ours is that the data for a lead costs less than a nickel and you can see it before you spend it.

## The script (the first four stages)

```bash
# sdr-signal.sh — hiring-signal list for one week
treg call apify.linkedin.search.jobs --query 'title=Sales Development Representative' --query 'posted=week' \
  | jq -r '.[] | .company_domain' | sort -u > companies.txt

while read -r D; do
  P=$(treg call icypeas.people.search --method POST --data "{\"query\":{\"currentCompanyWebsite\":{\"include\":[\"$D\"]},\"currentJobTitle\":{\"include\":[\"VP Sales\",\"Head of Sales\",\"CRO\"]}},\"pagination\":{\"size\":1}}" | jq -c '.items[0]')
  NAME=$(echo "$P" | jq -r '.firstname + " " + .lastname'); LI=$(echo "$P" | jq -r '.linkedinUrl')
  EMAIL=$(./waterfall.sh "$NAME" "$D")                 # from /rebuild/clay
  POSTS=$(treg call tikhub.x.linkedin-web-v2-get-user-posts --query "url=$LI" | jq -r '[.posts[:3][].text] | join(" | ")')
  echo -e "$D\t$NAME\t$EMAIL\t$POSTS"
done < companies.txt > leads.tsv
```

## The first line (the part that decides the 70%)

The rules that operators agree on, from 24M+ sends of teardown material:

- One signal per email. "Saw you've got two SDR roles open" beats a paragraph about their mission.
- The LLM fills a template; it does not compose. Otherwise you can't A/B anything.
- Feed it 25+ of your own hand-written emails and say "copy style, not content."
- Under 85 words. No link in email one. No "hope this finds you well."
- Openers that are an *inference* ("two SDR roles plus an enablement hire in one month reads like a ramp problem, not headcount") outperform scraped facts, because scraped facts read as scraped.

```
System: You write the first line of a cold email. Input: a hiring signal and the prospect's last three posts.
Output one sentence, under 25 words, that infers what the signal means for them. Never quote a post. Never say "I noticed."
Examples: [your 25 real first lines]
```

## What this does not do

- **Send.** Deliverability is domains, warmup, 30–40 per inbox per day, and cancelling a domain under 0.7% reply. That is Instantly/Smartlead's job; connect yours as an own-key tool and the calls are never metered.
- **De-anonymize site visitors.** RB2B / Warmly, own key.
- **Read a prospect's website as free text.** The catalog extracts named fields from a site by domain (`branddev.brand.ai.query`, $0.025) but has no raw page-to-markdown tool; Firecrawl on your own key for prose.
- **Route between providers.** The script picks the order; treg shows the options.

## The run receipt

> `[one week of SDR postings → N companies → N VP-level contacts → N verified emails → N first lines, total $X.XX]`

Related: [Rebuild Clay](/rebuild/clay/) · [Rebuild Gojiberry](/rebuild/linkedin-intent-signals/) · [Company data and buying signals](/use-cases/company-buying-signals/) · [Find and verify decision-makers](/use-cases/lead-enrichment-for-ai-agents/)
