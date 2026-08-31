---
page_id: r2
slug: /rebuild/linkedin-intent-signals/
seo_title: "Gojiberry & Trigify, Rebuilt: LinkedIn Intent From $0.002 a Post | treg.to"   # 66 → trim to "Rebuild Gojiberry: LinkedIn Intent Signals From $0.002 | treg.to"
meta_description: "Gojiberry, Trigify and Teamfluence sell LinkedIn intent as a subscription with no API. Here is the same loop — competitor-post engagers, profile pull, funding check — as a script with prices."  # 178 → trim
h1: "Rebuild Gojiberry: LinkedIn intent signals, per post, per profile"
seo_terms:
  primary: "gojiberry ai"                # 1,600/mo
  secondary:
    - "trigify"                          # 720
    - "teamfluence"                      # 90
    - "linkedin post scraper"            # 110
    - "linkedin profile scraper"         # 210
    - "buying signals"                   # 260
    - "intent signals"                   # 170
capabilities: [linkedin.post, linkedin.user.profile, linkedin.user.posts, people.search, people.email.find, companies.funding]
links_out: [/use-cases/company-buying-signals/, /use-cases/social-creator-trends/, /rebuild/clay/, /rebuild/ai-sdr/]
links_in: [p3, p5, /for/claude-code]
status: draft 2026-08-27 · one honest gap stated (keyword-level monitoring) · needs a run receipt
---

# Rebuild Gojiberry: LinkedIn intent signals, per post, per profile

A GDPR/SOC 2 vendor went from $0 to $2M ARR on one LinkedIn play, per its founder's post this week (549 bookmarks): watch who in your ICP engages with compliance content and with your competitors' posts, enrich them, contact them while the signal is fresh, and stack the signals — fit, plus topic engagement, plus follows a competitor, plus just raised. "Fit tells you who CAN buy. Intent tells you who might buy NOW."

Gojiberry, Trigify and Teamfluence sell that loop as a subscription. None of them sells an API. This page is the loop as a script, with a price on each step, and one part it can't do yet, stated plainly.

## The loop, step by step

| Step (Pierre's playbook) | Call | Price | Measured |
|---|---|---|---|
| 6. Who commented on a competitor's post | `scrapecreators.x.v1-linkedin-post` — commenters with profile URLs, plus the like count | $0.00188 | 21 calls, 100% |
| 2/4. Who they are — title, company, tenure | `scrapecreators.linkedin.user.profile` | $0.00188 | 1,997 calls, 99.9% |
| 2. What they've been saying | `tikhub.x.linkedin-web-v2-get-user-posts` — a person's recent posts | $0.001 | 78 calls, 100% |
| 3. Filter to ICP (role, size, geo) | `scrapecreators.x.v1-linkedin-company` — headcount, industry | $0.00188 | 754 calls, 100% |
| 7. Did the company just raise | `aviato.companies.funding_rounds` · `predictleads.companies.financing_events` | $0.01 · $0.04 | 7 · 228 calls |
| 4. Email + verify | the [Clay waterfall](/rebuild/clay/) | $0.01–0.03 | — |

Per engager, fully enriched with a funding check: **about $0.05**. A hundred engagers on three competitor posts: about $5, and you own the list.

## The script

```bash
# commenters.sh — everyone who commented on a post, scored against the ICP
POST_URL="$1"
treg call scrapecreators.x.v1-linkedin-post --query "url=$POST_URL" \
  | jq -r '.comments[].linkedinUrl' | sort -u > commenters.txt

while read -r P; do
  PROFILE=$(treg call scrapecreators.linkedin.user.profile --query "url=$P")
  TITLE=$(echo "$PROFILE" | jq -r '.headline'); CO=$(echo "$PROFILE" | jq -r '.current_company.url')
  COMPANY=$(treg call scrapecreators.x.v1-linkedin-company --query "url=$CO")
  SIZE=$(echo "$COMPANY" | jq -r '.employee_count'); DOMAIN=$(echo "$COMPANY" | jq -r '.website')
  RAISED=$(treg call aviato.companies.funding_rounds --query "domain=$DOMAIN" | jq -r '.rounds[0].date // ""')
  echo -e "$P\t$TITLE\t$SIZE\t$RAISED"
done < commenters.txt > scored.tsv
```

Commenters, not likers: the post endpoint returns each commenter's name and profile URL but only a *count* of reactions. Commenters are the stronger signal anyway — a comment is a sentence of intent, a like is a thumb — but if you need the likers list, that is PhantomBuster/Vayne territory on your own key.

Then the stack, as a score, not a vibe:

```python
score  = 10 if fits_icp(title, size, geo) else 0          # fit
score += 15 if engaged_with_topic_post else 0             # step 2
score += 20 if engaged_with_competitor_post else 0        # step 6
score += 30 if raised_within_days(RAISED, 180) else 0     # step 7
# >= 50 → "call them now" (Pierre's step 8)
```

## The honest scope (corrected after review, 2026-08-28)

Pierre's step 2, monitoring everyone in your ICP who engages with content around keywords, is a keyword-level feed across all of LinkedIn; that feed is what Gojiberry and Trigify actually are. The catalog gets most of the way there with two calls the first draft of this page wrongly called gaps: `scrapecreators.x.v1-linkedin-search-posts` finds public posts by keyword through Google's index (621 calls measured, 99.7% ok, $0.00188, `date_posted=last-week`), and `aviato.linkedin.post.reactions` lists a post's reactors by URN ($0.02 per success, 100 per page; JustOneAPI is the sibling at $0.03). So the honest pipeline is: scheduled keyword search over public posts → reactions and comments per post → profile → ICP filter → funding check. What it is not: LinkedIn's own feed, private posts, or a real-time stream. Say that on the page.

Also not here: sending. LinkedIn caps sit at ~100 invites per rolling week, 20–25 a day, and browser-click automation gets whole user bases banned. Send by hand, or through HeyReach/Unipile on your own key. The catalog reads; it does not press the button.

## The run receipt

> `[three competitor posts → N unique engagers → N ICP fits → N with a raise in 180 days → N verified emails, total $X.XX]`

## Gojiberry vs Trigify vs this

| | Gojiberry | Trigify | Per-call catalog |
|---|---|---|---|
| Keyword monitoring across LinkedIn | Their own feed | Their own feed | Public posts via Google's index, $0.00188 a search, scheduled by you |
| Engagers on named posts | Likers + commenters | Likers + commenters | Commenters $0.00188 per post; reactors $0.02 per page of 100 |
| Profile + company + funding enrichment | Bundled | Bundled | $0.002 + $0.002 + $0.01, itemized |
| API | No | No | Everything is an API |
| Pricing | Subscription | Subscription | Per call, prepaid |
| Sending | Built in | Built in | Not here, by design |

Related: [Company data and buying signals](/use-cases/company-buying-signals/) · [Rebuild Clay](/rebuild/clay/) · [Rebuild an AI SDR](/rebuild/ai-sdr/) · [Social & creator tools](/use-cases/social-creator-trends/)
