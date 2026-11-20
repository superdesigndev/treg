# REA for growth: how Lovable gets its users

A real run of the `/rea-growth` skill against **lovable.dev**, made through treg on 2026-10-23.
Data cost: **$0.58**. Every finding is marked **SEEN** (a call result shows it) or **INFERRED**
(reasoned from what was seen). Counts are as of 2026-10-23 and will change. "Why it works" lines
are interpretation. treg is not affiliated with Lovable.

## The short version

- **The loop.** Every app a user publishes on `*.lovable.app` carries an "Edit with Lovable" badge.
  That anchor sits on 20,132 referring domains (255,420 backlinks), first seen 2025-02-09. SEEN.
- **The megaphone.** The founder posts revenue milestones and users' money stories on X; creators
  turned "I built a $35K/month app" into million-view YouTube videos. SEEN.
- **The front doors.** Partner bundles (LinkedIn Premium, Lenny's newsletter, students), an
  affiliate program and a certified-experts directory. SEEN.
- **Search came after.** 79% of estimated US search traffic is brand searches. SEEN. Search
  followed the growth; it did not drive it. INFERRED.
- **What is next.** Brazil (Portuguese ads, a Brazil growth lead), paid search and social, partner
  channels and enterprise sales. SEEN in ads and job openings.

## 1 · When it grew

US organic search traffic estimate (DataForSEO historical rank overview), monthly:

| Month | Est. organic visits | Ranking keywords | Est. paid visits |
|---|---:|---:|---:|
| 2024-09 | 122 | 22 | 0 |
| 2024-12 | 2,651 | 118 | 16 |
| 2025-03 | 12,046 | 5,095 | 469 |
| 2025-06 | 50,556 | 37,511 | 2,187 |
| 2025-09 | 75,251 | 31,360 | 10 |
| 2025-12 | 95,415 | 30,055 | 0 |
| 2026-03 | 170,320 | 60,612 | 32 |
| 2026-07 | 260,315 | 12,437 | 632 |
| 2026-10 | 194,408 | 7,183 | 168 |

- **Inflection: January to June 2025.** Organic traffic rose about 11x (4,411 to 50,556). SEEN.
- Lined up against it, all SEEN:
  - the Supabase integration (Dec 2024);
  - ad and attribution pixels switched on: Google Ads (Dec 2024), Bing Ads (Dec 2024),
    Rewardful affiliates (Dec 2024), X Ads (Jan 2025), Reddit Ads (Mar 2025), TikTok Pixel (Apr 2025);
  - the badge anchor (Feb 2025);
  - a $15M round (Feb 2025);
  - the founder's "0 to $17m ARR in 3 months" post.
- Reported revenue (news events, PredictLeads): $100M ARR within about eight months of the late-2024
  launch; $500M annualized by June 2026. Valuation $1.8B (Jul 2025), $6.6B (Dec 2025),
  $13.3B (Aug 2026). SEEN as reported, not verified.
- Ranking keywords fell from 63,474 (Feb 2026) to 7,183 (Oct 2026) while traffic held near 195K:
  fewer pages ranking, same visits. SEEN. Cause not seen.
- Traffic by source (Akta) was unavailable: it needs the team's own Akta key.

### Moves vs growth

Each dated move (the timeline's pins plus the dated social posts) lined up against how fast visits
grew that month. Growth is the month-over-month change in the monthly estimate above; a move's lift
is the average growth in its month and the two after, minus the two months before. Moves in the
same month are one cluster. Python did all the math, from the calls above; no new calls.

- **Dec 2025 and Jan 2026 sped up most:** growth went from about +4% and +1% a month to about +24%
  and +19%, lifts of +20 and +18 points. Those months had the $330M raise, the founder's post about it
  (842K views) and two more posts on X with about 800K views each. SEEN as numbers; that they caused
  it is INFERRED.
- **Founder posts on X** are the only channel whose monthly count lines up with growth speeding up
  (Spearman r = +0.42 in the same month, +0.35 a month later, over 24 and 23 months). Every other
  channel stays between -0.27 and +0.35:

| Moves on | n | same month | 1 month later | 2 months later |
|---|---:|---:|---:|---:|
| Founder on X | 18 | +0.42 | +0.35 | -0.27 |
| Others on X | 6 | +0.31 | -0.18 | +0.20 |
| Reddit | 7 | +0.17 | +0.17 | +0.07 |
| YouTube (approximate months) | 5 | -0.12 | +0.16 | +0.14 |
| Ads & tracking | 11 | -0.09 | +0.06 | +0.14 |
| Product & loop | 8 | -0.02 | +0.25 | -0.25 |
| Fundraising | 6 | +0.05 | +0.32 | -0.15 |

- **Fast spreaders that moved nothing:** a founder post in Aug 2026 (535K views, growth kept falling)
  and a "$400K/mo app" YouTube video around May 2026 (396K views). Spread speed is views or upvotes
  a day since posting, ranked within each channel, so recent posts rank higher.
- Left out: 15 of the 20 YouTube videos, dated only "1 year+ ago". Search and hiring had too few
  moves for a correlation. Brand-search volume by month was not pulled in this run.

## 2 · The path, from first touch to sharing

| Stage | What they do | |
|---|---|---|
| Get noticed | Founder posts on X with ARR milestones and user stories (@antonosika, 85K followers, top posts 1.5K-5.9K likes). Creator YouTube videos about earning money with AI apps (up to 1.1M views). Google Ads Transparency lists about 8,000 creatives, the longest sampled running 285 days. 692 active Meta ads, the top ones in Portuguese. 392 LinkedIn ads; 11 of the 16 sampled with text are employees' own posts. ChatGPT names Lovable first for all 3 buyer questions asked. | SEEN |
| Sign up | Free plan: 5 build credits a day (up to 30 a month). Partner offers: LinkedIn Premium (1 year of Pro Lite and 300 credits), a Lenny's newsletter page, 50% off for students, 10 free credits through ONEGO. Ad landing pages (`/meta-lp`, `/motion-tutorial-reddit`) ask only for an email. | SEEN |
| First win | Describe the idea and a working app appears; 250 templates; backend through Supabase. | SEEN |
| Pay | Credits per message (0.5 to 1.7 credits for example prompts). Pro is $25 a month (student page), Business $50 (the founder's own post), Business plan since Jul 2025, then Enterprise. | SEEN |
| Share | Published apps live on `*.lovable.app` with an "Edit with Lovable" badge (134,702 backlinks from `lovable.app`). A "Made with" badge since Sep 2026 (1,407 domains). Experts directory, affiliates. | SEEN |

## 3 · Ten plays you can copy

### 1. Put a badge on everything users publish · Share
- **Evidence (SEEN):** the "Edit with" anchor is on 20,132 referring domains and 255,420 backlinks,
  first seen 2025-02-09. 134,702 backlinks come from `lovable.app`.
- **Why it works:** every output becomes an ad that its maker distributes for free, and a link
  that builds domain authority.
- **Effort:** a week · **First result:** 4-6 weeks; watch sign-ups whose referrer is a user's output · **Cost:** engineering time only.
- **The ask:** "Add a small 'Made with [product]' badge to every page, file or share our users
  publish. Link it to a page that starts a new project from that output, tag the link with the
  source, let paid plans remove it, and report weekly sign-ups that came from badges."

### 2. Post your numbers in public · Get noticed
- **Evidence (SEEN):** the founder's top X posts are milestones ("0 to $17m ARR in 3 months",
  "growth up almost 50% last week, now adding 1,500 customers per day") and user money stories
  ("a Lovable user built a service that turns pet photos into portraits… $100k/month").
- **Why it works:** growth numbers are news, and users' revenue is proof that the product pays.
- **Effort:** an hour a week · **First result:** 2-4 weeks; follower growth and replies · **Cost:** none.
- **The ask:** "Every other Monday, draft an X post from our numbers: one metric that moved, why,
  and one customer story with a number in it. Send it to me to approve; never post on your own."

### 3. Turn customers' money stories into creator videos · Get noticed
- **Evidence (SEEN):** the most-viewed YouTube results for "lovable ai app builder" are earnings
  stories ("I cloned 3 apps and now make $35K/month", 1.1M views; "How I Built A $1.4 Million/Month
  AI App At Age 18", 958K). Lovable's partner page invites content creators. Whether those videos
  were paid: not seen.
- **Why it works:** creators want stories that end in money; the product is the means.
- **Effort:** a week · **First result:** 4-8 weeks; views and sign-ups from links · **Cost:** creator fees, if paid.
- **The ask:** "Find our 5 customers with the strongest revenue or time-saved result, write a
  one-page story for each, and list 20 YouTube creators in our niche with 50K-500K subscribers who
  made a similar story in the last 6 months, with a pitch for each."

### 4. Bundle into other companies' memberships · Sign up
- **Evidence (SEEN):** `/linkedin-premium-offer` (1 year of Pro Lite plus 300 credits for Premium
  members), `/lenny` (a page for Lenny's newsletter readers), `/students` (50% off Pro for 12
  months), and an ONEGO partnership with 10 free credits.
- **Why it works:** another brand's paying audience arrives pre-qualified, and the perk costs
  credits, not cash.
- **Effort:** a week per partner · **First result:** 2-6 weeks; redemptions per partner page · **Cost:** free usage.
- **The ask:** "List 10 newsletters, communities and memberships our buyers already pay for. For
  each, draft a partner offer (what members get, how they redeem it, the landing page) and the
  email to their partnerships contact."

### 5. Pay the people who sell you · Share
- **Evidence (SEEN):** affiliates earn up to $100 per first-time subscriber; Experts earn 10% of
  every Business plan they bring and get a certified profile in a public directory; Rewardful
  (Dec 2024) and Impact (Sep 2026) affiliate tracking were detected on the site.
- **Why it works:** freelancers and agencies already build for clients; a commission and a
  directory turn them into a sales team.
- **Effort:** a week · **First result:** 6-8 weeks; partner-sourced sign-ups · **Cost:** commission on revenue only.
- **The ask:** "Draft an affiliate and an expert program for us: commission rules, who qualifies,
  a directory page, and the tool to track it. Estimate the cost per paying customer at our prices."

### 6. Write the comparison pages AI reads · Get noticed
- **Evidence (SEEN):** ChatGPT named Lovable first for all 3 buyer questions asked and cited
  Lovable's own `/guides/lovable-vs-bolt-vs-v0` and `/guides/bolt-vs-replit-vs-lovable`. The
  `/guides/` section ranks for 517 keywords.
- **Why it works:** AI answers quote pages that compare options plainly; owning the comparison
  shapes the answer.
- **Effort:** a week · **First result:** 4-8 weeks; how often AI names you · **Cost:** writing time.
- **The ask:** "Write comparison guides for us against our top 3 competitors: who each is for,
  prices, a table, and when not to pick us. Then ask ChatGPT our 5 buyer questions and report
  whether we are named and which pages are cited."

### 7. Promote your employees' posts to their peers · Get noticed
- **Evidence (SEEN):** 11 of the 16 sampled LinkedIn ads with text are staff posts promoted as ads:
  the People lead ("the People team at Lovable builds its own software"), the first CSM, the revenue
  team, two marketers. The 5 company-page posts speak to product teams and designers.
- **Why it works:** a peer in the same job is more credible than a brand.
- **Effort:** a day per post · **First result:** 2-4 weeks; cost per click by job title · **Cost:** ad spend.
- **The ask:** "Interview three of our team about one tool or workflow they built with our
  product, draft a LinkedIn post in each person's voice for their job function, and set each up as
  a thought-leader ad aimed at that job title."

### 8. Open one country at a time · Get noticed
- **Evidence (SEEN):** the top active Meta ads are in Portuguese, point to `lovable.dev/pt-br/`
  and started in Sep 2026. A "Growth Lead, Brazil" job opened on 2026-09-29. The site has 7
  language versions (about 529 URLs each). Platzi launched an official free course in Spanish.
- **Why it works:** a localized site, local ads, a local school and a local hire make one market
  at a time.
- **Effort:** a quarter · **First result:** 6-12 weeks; sign-ups from that country · **Cost:** translation, ads, one hire.
- **The ask:** "Rank 5 countries by our current sign-ups, search demand and competitor presence.
  For the top one, plan a localized landing page, 5 ads in the local language and 3 local
  schools or communities to partner with."

### 9. Make the free plan a daily habit · Pay
- **Evidence (SEEN):** the free plan grants 5 build credits a day, up to 30 a month; the pricing
  page shows what each example prompt costs. **INFERRED:** a daily grant brings people back every
  day and shows them what a paid plan buys.
- **Effort:** a week · **First result:** 4 weeks; day-7 return rate and free-to-paid · **Cost:** free usage.
- **The ask:** "Model a daily free allowance for our product: how much a day, what it costs us
  per active user, and which upgrade prompt to show when it runs out."

### 10. Help your users grow theirs · Share
- **Evidence (SEEN):** `/grow` (launched 2026-10-14) plans SEO, posts and ads for the apps users
  build, "you approve before anything spends". **INFERRED:** apps that get traffic carry the
  badge to more people, so users' growth feeds play 1.
- **Effort:** a quarter · **First result:** 8-12 weeks · **Cost:** product work.
- **The ask:** "List the 3 things our users most need after their first result, and sketch a
  feature that does one of them inside our product, with the metric that shows it helped them."

## 4 · Things to skip

- **Leading with SEO.** 79% of their search traffic is brand searches, and paid search was never
  large (at most 2,797 estimated visits a month). Search followed the growth. Do the loop and the
  founder posts first.
- **Counting raw backlinks.** Many top anchors are link sellers ("Premium SEO Authority
  Backlinks…", Telegram link shops) aimed at the domain. Only the badge anchors measure the loop.
- **Credits without spending caps.** The top Reddit threads are surprise bills ("How my private
  Lovable App Generated a $4,000 Bill in ~2 Hours") and "why pay when Claude or Codex are cheaper".
  If you copy credit pricing, ship caps and alerts with it.

## 5 · What is next for them

45 distinct marketing, partner and sales roles among 209 openings seen since 2026-09-15 (PredictLeads), all SEEN:
- **Paid:** performance marketers for paid search and paid social.
- **Search:** an organic search growth marketer and growth engineers on the marketing team.
- **Brazil:** a Growth Lead, Brazil.
- **Community:** a community manager for virtual spaces.
- **Partners:** partner managers for hyperscalers, GSIs and regional SIs.
- **Sales:** enterprise, strategic and mid-market account executives in New York, London and
  Stockholm.

## Method notes

Endpoints called through treg, in order, with what each cost:

| Step | Endpoint | Result | Cost |
|---|---|---|---:|
| Site | `treg.web.map` | 5,000 URLs (the map's cap) | $0.001 |
| Site | `treg.web.extract` × 10 (TinyFish) | home, pricing, affiliates, students, lenny, linkedin-premium-offer, meta-lp, partners, experts, grow | $0 |
| Stack | `predictleads.companies.technology_detections` | 483 detections (noisy; only high-confidence ad and attribution tech used) | $0.04 |
| News | `predictleads.companies.news_events`, `financing_events`, `connections` | 164 events, 7 rounds | $0.12 |
| Pages | `predictleads.companies.website_evolution` | 404 from the provider, not billed | $0 |
| Growth | `dataforseo.x.dataforseo-labs-google-historical-rank-overview-live` | 26 months, US | $0.151 |
| Growth | `akta.x.website-traffic` | refused: needs the team's own Akta key | $0 |
| Search | `dataforseo.google.domain.ranked_keywords` (1,000 rows) | 7,005 keywords total | $0.132 |
| AI | `cloro.ai-search.chatgpt.scrape` × 3 | named first 3/3 | $0.008 |
| Ads | Meta search + company ads, Google transparency, LinkedIn, TikTok | 692 / ~8,000 / 392 / 0 | $0.031 |
| Social | `anyapi.x.search.posts` × 2, `anyapi.youtube.search`, `scrapecreators.reddit.search.posts` | | $0.004 |
| Links | `dataforseo.web.backlinks.summary`, `dataforseo.web.anchors.list` | 46,791 referring domains | $0.049 |
| Links | `dataforseo.x.backlinks-timeseries-new-lost-summary-live` | rejected `date_from`, not billed | $0 |
| Next | `predictleads.companies.job_openings` | 678 openings | $0.04 |
| | **Total** (balance before minus after) | | **$0.577** |

Checked again on 2026-10-29, for the page's evidence cards:

| Step | Endpoint | Result | Cost |
|---|---|---|---:|
| Ads | `scrapecreators.x.v1-facebook-adlibrary-company-ads` × 3 pages | 90 most-seen of 711 active ads: the top 4 in Portuguese, 45 linking to `/pt-br/`, 72 on 2 captions | $0.006 |
| Links | `dataforseo.web.backlinks.list`, filtered to `?via=` and to "Edit with" anchors from `*.lovable.app` | 918 domains with an affiliate code; 17,118 app domains with the badge | $0.052 |

- **Limits:**
  - US search data only.
  - The TikTok ad library found no ads under the name "Lovable", although a TikTok pixel is on
    the site.
  - Reported revenue and valuations come from news events, not filings.
  - Pricing plan cards did not render in the page read, so only the $25 Pro price (from the
    student page) is used.
- **Ground rules:** public evidence only; no accounts created, no forms filled, no one contacted.
