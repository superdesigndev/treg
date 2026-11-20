---
name: "rea-marketing"
description: "Use when the user wants to reverse engineer a brand's holiday marketing (Black Friday, Christmas, Singles' Day…): what ran on each channel, when, at what estimated budget, and whether it worked in sales."
---

# REA marketing

Brand(s) + a holiday in; a two-part teardown out.

- **Part 1 · What the marketing team did.** Per channel: what ran, in which week, at what
  estimated budget; what the content says and how the viral posts were made; every other strategy
  and move.
- **Part 2 · Did it work.** Real sales numbers plus estimated sales by channel, set against the
  spend, ending in one verdict: worked, mixed, didn't work, or too early to call.

The report closes with a few plays to copy. Runs on treg: use the treg tools if connected
(mcp__treg__call, catalog_get, catalog_search); otherwise read https://treg.to/llms.txt, install,
sign in, and come back.

## Four principles

1. **Counts are pulled in full; only money is estimated.** Ads, posts and moves are counted as
   found: page until the source is empty, never scale or extrapolate a count. If a source stops
   early, show "pulled N of M". Dollars (spend, sales) are never public, so they are estimates at
   market rates, each with a low / mid / high range and the rate shown.
2. **SEEN or INFERRED.** SEEN = a call result or image shows it (link it). INFERRED = reasoned
   from what was seen (say from what). Nothing seen → "not seen", never guessed. That a move
   caused a result is always INFERRED.
3. **HOLIDAY and BRAND(S) come from the user.** Nothing here assumes a holiday or a brand.
4. **Public evidence only.** No accounts, forms, sign-ups, purchases or contacting anyone.

## Working rules

- Before an endpoint's first call: `catalog_get` it, quote the price, check the cap (default $3
  per brand). DataForSEO endpoints take a JSON array body: [{…}].
- Free first: `treg.web.search` and `treg.web.extract` (header `X-Treg-Route-Prefer: tinyfish`).
- A call fails or returns nothing → `catalog_search` the same job, try one alternative → else
  "unavailable". An empty answer is a finding only if a second provider agrees.
- Big responses: let them save to a file (or trim with the endpoint's `jq` / `fields`), parse with
  `python3 -I`. Never page raw JSON through the chat.
- Python does all math; every number on the page comes from code output and a CSV. Date every
  count.
- **Social media collab** = any post by someone other than the brand that the brand is behind or
  profits from: paid partnership, gifted/PR, affiliate or TikTok Shop, code or storefront link,
  ambassador, creator post run as an ad. Use this name everywhere in the report.

## 0 · Setup: interview first

No data call runs until the user has answered (AskUserQuestion when available):
1. Which holiday or season, and which country (US → location_code 2840, language_code en).
2. Which brand or brands. One or several; the user names them. Never pick for the user. Only if
   asked for ideas, search "best <holiday> campaigns <category> <last year>" and let them choose.
3. Optional: their own company and stage (for the plays); anything to focus on or leave out.

If the first message already names holiday and brands, restate them in one line and go.

Then set, in Python, from today's date (never hard-code a year):
- **Day 0** and the windows, for this year and last year: baseline = days -70 to -42, ramp = -28
  to -1, peak = day 0 to its end, after = +1 to +14. Chart window = weeks -12 to +2. Say "D-n"
  for today against day 0.

| holiday | day 0 | peak ends |
|---|---|---|
| Black Friday | day after the 4th Thursday of Nov | Cyber Monday |
| Christmas | Dec 1 | Dec 24 |
| Singles' Day | Nov 11 | Nov 11 (presales from ~Oct 20) |
| Valentine's | Feb 1 | Feb 14 |
| Mother's Day (US) | 2nd Sunday of May, -14 | that Sunday |
| Halloween | Oct 1 | Oct 31 |
| anything else | search its dates, propose day 0 and peak end, confirm with the user | |

- **HOLIDAY_TERMS**: the 3-5 words people search for it ("black friday", "cyber monday", "bfcm").
  Every search and filter uses these.
- **Mode**: *live* (holiday ahead or running: last year's season + this year so far) or *retro*
  (holiday over: that season + the one before). Season labels are "Last year (YYYY)" and "This
  year so far (YYYY)".
- **One brand** = the brand against itself (this year vs last year at the same D-day); no
  cross-brand lines, no earnings tab if private. **Several** = same steps per brand, then side by
  side. If a brand is added or dropped later, rebuild every section, not only the tables.

◆ After each step below: a 3-line finding, what the next step checks, cost so far.

## Part 1 data · what ran, when

Every step saves dated rows (date, brand, channel, what, link, views) so steps 9-12 can count them.

### 1 · Brand map — ~$0.01
- `treg.web.extract` on home, where-to-buy, rewards, holiday/gift page, FAQ (look for
  HOLIDAY_TERMS, "sale", "early access", "price match").
- Official handles linked from the site. Retail map: own site, Amazon (official store or
  resellers), TikTok Shop, Walmart, key retailers.
- Product line-up: model names from the nav and best-sellers. Shopify store →
  `crawl4ai.shopify.store.products` {"store","limit":250}; count tags that name a promo or an
  exclusion (sale, bfcm, early-access, exclude, gwp, bundle).

### 2 · Paid ads — ~$0.15
- **Meta, every ad, both seasons.** pageId from
  `scrapecreators.x.v1-facebook-adlibrary-search-companies` {"query":BRAND}. Then page
  `anyapi.facebook.company_ads` {pageId, country, status ALL, startDate, endDate, sortBy recent}
  with a `jq` query that returns only id, start day and end day per ad plus nextCursor, until
  nextCursor is empty. Check the row count against the library total. Take ad copy and format
  from one fuller first page. Cursors expire between sessions: restart from page one. No ads for
  the country → try companyName, then say "none found in the <country> library".
- **Google.** `spyfu.google.domain.overview` {"domain","pastNMonths":14} → monthly budget
  (estimate); `spyfu.google.domain.ad_history` {"domain","keywordFilter":HOLIDAY_TERMS} → holiday
  copy and keywords bought, including rivals' names; `adyntel.google.ads.transparency` for now.
- **TikTok.** `adyntel.tiktok-ads.library.search.company` {"company_domain","influencer_ads":true}.
- Keep per ad: start, end, network, format, offer in the copy, landing page, and whether it runs
  from a creator's page (a partnership ad, also a collab).

### 3 · Social media collabs — ~$0.15
- **Search wide.** `scrapecreators.tiktok.search.videos`, date_posted last-3-months, no trim (so
  results save to files), for: BRAND + each HOLIDAY_TERM, "tiktok shop", "deal", "code",
  "gifted", "partner", "haul", and one search per product name. 10+ searches a brand, page 2
  (cursor 30) on the main ones. Also `tikhub.x.instagram-v2-fetch-user-tagged-posts`,
  `scrapecreators.x.v1-instagram-search-hashtag`, `treg.youtube.search.videos`.
- **Clean.** Keep the report's country (`region`), drop the brand's own accounts, dedupe by
  video id, keep posts inside the window.
- **Label each post** from the raw item: paid (`is_paid_partnership`,
  `branded_content_accounts`, #ad, #partner) | gifted/PR | TikTok Shop affiliate (a shop anchor,
  or "Creator earns commission") | link/code | run as an ad. Tier by followers: under 10k,
  10-100k, 100-500k, 500k-1M, 1M+.
- **Flag dupes**: the cart product doesn't carry the brand or model name, or the caption says
  dupe / "$X vs $Y". Count them apart from real collabs.
- No platform lists every collab: the method note says how many searches ran and which platforms.

### 4 · Owned social — ~$0.05
- Pull each handle back to the start of the window, both seasons where dates allow (X:
  `anyapi.x.search.posts` with from:HANDLE since: until:; IG and TikTok: page back by cursor).
- Tag every post: holiday | offer (%, sale, code, gift) | launch | none. Record which product
  each post shows. Owned posts are read for the offer calendar and products, not ranked.

### 5 · Site, offers and email — ~$0.05
- What the home, holiday and sale pages say now: offer, popups, early access, rewards.
- Email/SMS/loyalty/affiliate tools: `predictleads.companies.technology_detections` (score ≥
  0.9). Launches and news dated in the window: `predictleads.companies.news_events`.

### 6 · Retail and prices — ~$0.05 + $0.03 per snapshot
- Per retailer and marketplace: official store or resellers, the retailer's own event and the
  brand's discount in it, who discounts first.
- 5-10 hero products: own-site price vs list, `serper.google.serp.shopping` {"q":PRODUCT} for
  every merchant (China: `justoneapi.x.taobao-get-item-detail-v5`,
  `justoneapi.x.jd-get-item-price-v1`). Discount depth = 1 - price / list. Note products never
  discounted. Past prices can't be rebuilt: in retro mode use dated ads and posts that quote
  prices, and say so.

### 7 · Search, AI answers and word of mouth — ~$0.17
- **Demand line.** `dataforseo.x.keywords-data-dataforseo-trends-explore-live` [{"keywords":[…],
  "location_code","date_from":day -70,"date_to":day +14 or today}], one call per season, all
  brands in one call (max 5; more → extra calls sharing one anchor term). Pick a term that means
  the brand ("rhode skin", not "rhode"). Express each week as a multiple of the baseline (1× =
  normal). Seasons are on separate scales: compare shape and timing, not height. A spike no move
  explains is "unexplained, check", not credited.
- 3-5 holiday buyer questions → `cloro.ai-search.chatgpt.shopping`: is the brand named.
- What buyers say: comments and searches like "is BRAND having a <holiday> sale"; after the peak,
  reviews about shipping and stock-outs (`crawl4ai.trustpilot.business.reviews`).

## Part 2 data · what it got

### 8 · Sales — ~$0.06 per brand
**Real numbers** (label each with its date and what it is):
- Amazon: `justoneapi.x.amazon-search-products-v1` {"keyword","country"} → "bought in past
  month" (a floor per listing), price, review count, stock hints.
- TikTok Shop: `anyapi.tiktok_shop.search` {"query","country"} → `soldCount` (lifetime) by shop:
  official, authorised resellers, dupes.
- eBay resale: `serpapi.x.ebay-search` → `quantity_sold`.
- Public parent: holiday-quarter sales ÷ full year (25% = an average quarter). Private: "not
  disclosed".
- Store traffic where there are stores: `litescrape.google.maps.popular_times`.

**Periods need two snapshots.** Lifetime and rolling counts become "sold during the holiday" only
as the difference between two dated pulls. In live mode, schedule daily snapshots (sold counts,
reviews, stock, prices) from today to day 0 + 14 and tell the user.

## Compute — no calls

### 9 · Volume per week
One unit = one ad live that week (counted in every week between its start and end), one collab
post, one owned holiday/offer/launch post, one site, retail or PR move. Save `volume_weekly.csv`
(brand, season, week, channel, units, views). Per brand: **prep start** (first unit, weeks before
day 0, which channel), **peak week** and the top channel's share.

### 10 · Estimated spend (always labelled "estimate", with its range)

| Spend | Mid (low - high) |
|---|---|
| Google search ads | SpyFu monthly budget, spread by day |
| Meta / TikTok ads | $150 per live ad per day ($50 - 400) |
| Paid collab post, by followers | <10k $150 (50-300) · 10-100k $750 (250-1,500) · 100-500k $3,000 (1,500-5,000) · 500k-1M $8,000 (5,000-12,000) · 1M+ $20,000 (10,000-50,000) |
| Gifted post | one product at retail |
| Affiliate / TikTok Shop post | views × 0.2% bought (0.06-0.36%) × price × 12% commission (5-20%) + one sample; capped at real sold units × price × commission where sold counts exist |
| Making an owned post | $1,000 ($150 - 3,000) |
| PR event or brand collab | $100,000 ($30,000 - 200,000) |
| Not estimated (say so) | retailer co-op fees, email/SMS sends, staff time |

Refresh the rates with WebSearch (current CPM, creator rate card, commission) and cite them.
Output: spend per week by type, channels ranked by spend, the peak spend week.

### 11 · Estimated sales by channel (low / mid / high, period, trust)

| Channel | How | Trust |
|---|---|---|
| Amazon | units from "bought in past month" × price, per product line; a floor | medium |
| TikTok Shop, real listings | sold counts × price | high |
| TikTok Shop, dupes | units only, own row: "not the brand's revenue" | high |
| Own site | visits (organic clicks + Google budget ÷ category CPC) × category conversion rate × order value | low |
| Social media collabs | views × share who buy × price; a ceiling, checked against real sold units | low |
| Other retail | review growth between snapshots ÷ 1-2% review rate, or "not estimated" | low |

Find the category's conversion rate and CPC with WebSearch and cite them. Totals are for comparing
channels with each other, not the truth; a public brand's total is checked against reported sales.

### 12 · Did it work: six tests, one verdict

| Test | Number | Compared with |
|---|---|---|
| Demand rose | peak-week search ÷ baseline | last year's multiple at the same D-day |
| Reach was cheap | views per estimated dollar, per channel | the brand's other channels |
| Sales rose | units or sales in the holiday window | a normal window (snapshot difference), or holiday-quarter share vs 25% |
| Spend paid back | estimated sales in window ÷ estimated spend in window, as a range (low sales ÷ high spend to high ÷ low) | 1× |
| Price held | discount depth on hero products; new models at full price | list price, last year |
| Demand was kept | real units ÷ (real + dupe units) | — |

Grade each pass / mixed / fail / can't tell yet, with its trust. Verdict:
- **Worked**: sales rose and at least three other tests pass.
- **Didn't work**: sales flat or down, or spend clearly not paid back.
- **Mixed**: anything between; say which tests split.
- **Too early to call**: before day 0 + 14, or no second snapshot. Give the reading "so far" from
  the tests that can already be read and the date the verdict will be final.

Never call success from marketing volume alone: a verdict needs a result number. Then two short
lists, each item tied to a test: what worked, what didn't.

Also from the dated moves (`timeline.csv`): search in the 7 days before a move vs the 8 days from
it; moves under 3 days apart share one lift. Last year vs this year: the same move at the same
D-day, side by side.

## The report: report.html, published as an artifact

Plain, casual US English whatever the chat language. About the brands, not the tooling: no data-
platform names, no coverage table, no bill on the page (those go in the chat reply). **Every title
says what its section contains** ("How early DJI started, where it pushed, and what it got"); no
slogans or abstract phrasing.

**Hero.** "<Holiday YYYY> marketing teardown: <brand(s)>", D-n badge, per brand one sentence on
what it did and a verdict pill, 4 KPI tiles.

**Part 1 · What <brand> did**
1. **Timeline: which channel ran when, and what it cost.** One card, one brand at a time, season
   toggle. Four readouts: prep started · biggest push (volume) · biggest estimated spend ·
   result. One chart on weeks -12 to +2: top, estimated spend per week stacked by type; bottom,
   one small square per live unit stacked by channel, with the search line (1× = normal) and the
   views line (dashed). Spend colours follow their channel's colour family; legend in two
   labelled rows (Squares, Spend bars). Ramp shaded, day 0 marked, future weeks hatched.
   **Every axis is labelled**: spend panel titled "Estimated spend per week (US$, bars)" with
   $0 / half / max ticks and the week's total above each bar; squares panel titled "Campaign
   units live per week (count, squares · left axis)" with count ticks on the left, gridlines
   and the week's total above each column; the search line names its right axis ("Search vs
   normal", 1× marked).
   **Hover shows a tooltip only**: no fading, highlighting, zooming or other animation. The
   tooltip appears at once and leads with a colour swatch and the channel or spend type in bold, then the
   week, the count or amount with its range and share of that week, then the single ad or post.
   Hovering a search point shows "N× normal" and what 1× means. Under it: "Where the money most likely went,
   in order", named moves as channel lanes on the same weeks (chips stay inside their lane), then
   a note listing every rate and anything not pulled.
2. **Each channel, week by week** (keep section titles this short). Tabs: Paid ads | Social media collabs |
   Owned social | Site & offers | Retail & prices | Search & PR. Every tab reads top to bottom
   in the same order:
   - **Summary block**, three boxes: *Estimated budget* (one big number, its range, how it was
     worked out; "Not estimated" with the reason where it can't be) · *What <brand> did* (2-3
     counted bullets) · *When* (first seen, peak week, still running or not). The boxes are
     tinted in the channel's colour (paid orange, collabs purple, owned teal, others grey).
   - **Week by week chart**: bars = this channel's units live per week (count on each bar, left
     axis), the estimated spend under each week in the channel's colour, and the search line
     (1× = usual) on the same weeks. Charts first, text short.
   - **Show, don't describe**: Paid ads → 1-2 real ad examples (image from the ad library, page
     name, "Sponsored", first line of copy, dates live, platforms, link to the ad); Social media
     collabs → the top 8 posts as cards (cover, views, kind pill, the creator's profile photo as
     a small circle, handle, followers, date, link); Owned social → the top 4 posts with covers.
     Download images at pull time and embed them (CDN links expire within hours).
   - The week-by-week table folded under "Show the week-by-week table": one row per week with
     what ran, the top post, est. spend and range, search, views. A number that isn't public says
     "not public"; a week not pulled says "not pulled" and is never shown as zero; a week whose
     data isn't out says "not out yet". Search is brand-wide, not caused by one channel.
   - Then the dated moves. No stat tiles or cards that repeat or contradict the summary.
3. **Content strategy and how the viral posts were made.** Tabs: *What the content says*
   (each model name counted in ads, collab posts, owned posts and the holiday page: products
   pushed on social vs sold at the holiday; hooks; whether the offer is in the copy; formats) |
   *Viral teardown* (top 5 real collab posts and top 2 dupes by views, covers opened and read:
   hook, format, length, call to action, views ÷ followers; the shared formula as counted
   points; which kind of collab makes the hits; dupes outlined in red).
4. **Other strategies and every move.** Show, don't list: every point is a picture, a chart or
   a marked-up example with a link, never a numbered paragraph.
   - *Offer and pricing*: offer cards from the brand's own store, one per discounted product plus
     the hero product: a real screenshot of the product page (Playwright, popups closed, cropped
     to image + price), price now, struck-through list price, a "-N% · save $X" pill (or "list
     price, no discount yet") and a link. Prices from the store's own data (Shopify:
     `/products/<handle>.js` gives `price` and `compare_at_price` per variant); say "N of M
     variants discounted". Then a price-by-seller bar chart for the hero product (own store, big
     retailers, marketplace sellers, refurbished; coloured, linked) and tiles for where the offer
     shows up (yes / partly / no per channel).
   - *Patterns*: one card per pattern, each with its own visual. Always include how the top
     collab clips are built: the 5 most-viewed real posts, each with cover, hook text, a bar as
     long as the clip split into hook → body → call to action, length, views, followers, kind,
     link. Others as fits: one real ad's copy marked up by part, with a link; posts-per-week mini
     bars; views by collab kind; real vs other-brand units. Never fill a bar with a guessed value.
   - *All moves*: the full dated table with lift, kept plain and scannable.

**Part 2 · Did it work**
5. **Sales: real numbers and estimates.** Charts over text. Tabs: *Real numbers* first (a bar
   per Amazon listing with thumbnail, price, list price and link; TikTok Shop units by product
   for the brand; a share bar and top listings for the hero product's searches, coloured own
   shop / retailer / pre-owned / other brands) | *Estimated sales by channel* (a range chart:
   band low to high, mark at mid, coloured by trust; the table under it) | *Indirect signals* |
   *Earnings* (public brands only). No text cards that repeat the charts.
   TikTok Shop search returns a different set of listings on each call: run 4-5 searches (brand,
   hero product full name, short name, the category phrase), include each listing's id in the
   `jq`, combine and dedupe by id, save the rows, and say it is a combined sample, not a census.
   Call real rival brands "other brands", not dupes. If a new pull contradicts an earlier
   figure, replace it everywhere and tell the user what changed and why.
6. **Verdict: did <brand>'s <holiday> strategy work.** The verdict in one line, the six-test
   table (number, compared with, grade, trust), estimated spend next to estimated sales, then
   "What worked" and "What didn't". In live mode: "so far", plus the date it becomes final.

**Close.** "N plays to copy" (3-6 cards drawn from what passed the tests, each with evidence,
when to start, effort and the number to watch). A play gets a folded, copy-ready prompt only
when its first step is pulling data with treg; that prompt starts "Using treg, …". Plays that
are pure writing or planning get no prompt. "N things to skip" (from what failed), a short method note (SEEN vs INFERRED, sources,
snapshot dates).

**Look.** Read the artifact design guide first. Model: https://treg.to/rea-growth. Inter Tight
600 headings, Inter text, JetBrains Mono labels; max width 1120px; light and dark via CSS
variables; warm-grey cards (#F4F2EE), hairlines, one green accent (#1F7A4D), one colour per
brand. Tabs are a centred segmented control with a one-line caption. SEEN = solid pill, INFERRED
= dashed. Tables stack and the nav scrolls on mobile.

**Checks before publishing.** Render headless in light and dark at 1280px and at 390px; look at
the hero, the timeline per brand and every tab. Squares add up to `volume_weekly.csv`; counts
match the pulled rows (nothing scaled); every number traces to a CSV; every estimate shows its
range; no chip outside its lane, no broken image, no sideways scroll; one brand → no other
brand named. Fix, then publish. End the chat reply with the verdict and the bill.