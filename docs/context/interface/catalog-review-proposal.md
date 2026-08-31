---
title: Catalog browse review — categories, platform placement, and domain sections
status: reference
sources:
  - src/treg/domain/catalog/store.py
  - src/treg/catalog/capabilities.yaml
related:
  - architecture/catalog.md
  - interface/dashboard.md
---

# Catalog browse review — categories, platform placement, domain sections

Reviewer pass over the browse taxonomy that an agent (or human) navigates to find data:
the top-level **categories**, each platform's **category** placement, and the per-platform
**domain sections** that `catalog_store._domain` derives.

Scope split, as requested:
- **(a) Category structure** — recommendation only. Renames/merges/splits of the top-level
  category set are the founder's call and are **not applied** here.
- **(b) Platform → category placement** — safe corrections, applied.
- **(c) Domain section names** (`DOMAIN_KEYWORDS` / `DOMAIN_NOISE`) — safe fixes, applied.

Gate at time of writing: `catalog_validate.py` → **0 errors**, 61 files, 2617 endpoints;
`test_catalog_api.py` + `test_dashboard_markup.py` + `test_detail_pages.py` → **112 passed**.

---

## (a) Category structure — PROPOSAL ONLY (founder's call)

Current set: **SEO** (AEO/GEO folded in), **Social**, **China Social**, **Advertising**,
**Enrichment**, **E-commerce**, **Reviews & Apps**, **Community**, **Developer**, **Other**.

The nine-plus-Other cut is a good browse map: it mirrors how a growth/agent user thinks
("I need social data", "I need company data", "I need my ad accounts"). Keep the spine.
Four things to decide:

1. **"Developer" is a rogue 10th category with a single platform (`github`).** It is not in
   the canonical nine. Either (i) bless it as a real category and start filling it
   (GitLab, npm, PyPI, Stack Overflow, Hugging Face…), or (ii) **fold `github` into
   Social** — its capability surface is social-graph shaped (`user.profile`,
   `user.followers`, `user.following`, `trending.*`). Recommendation: **fold into Social
   now**, split out a Developer category later when there are ≥3 platforms to justify a tab.
   *(Not applied — creating/removing a category is a structure change.)*

2. **China Social is a geography, not a job.** Everything else is bucketed by what the data
   *is* (Social, E-commerce, Advertising); China platforms are bucketed by *where they are*.
   This is a defensible pragmatic choice (a China-focused user wants one shelf), and the
   catalog already lets China platforms escape it by function (`douyin-shop` → E-commerce,
   `douyin-xingtu` → Advertising). Recommendation: **keep**, but name the rule explicitly in
   `capabilities.yaml` so future China platforms are placed consistently (content → China
   Social; storefront → E-commerce; ad/creator marketplace → Advertising).

3. **No "Music / Media" category.** `spotify`, `apple-music`, `soundcloud` sit in Social and
   `netease-music` in China Social. Fine at today's volume; revisit only if music platforms
   proliferate. Recommendation: **keep in Social**; do not split a Music tab for four
   platforms.

4. **"Reviews & Apps" is two ideas stapled together** (app-store review sites *and* app
   stores *and* general review sites like Trustpilot/Yelp/Tripadvisor/IMDb/Douban). It reads
   fine as "reputation & listings", but if it grows, consider splitting **App Stores** from
   **Reviews**. Recommendation: **keep for now**, flag for a future split.

Net: **keep all nine core categories**; the only near-term structural decision is
Developer-vs-Social for `github`.

---

## (b) Platform → category placement — AUDITED, applied fixes: 0

All 78 platforms were checked against their category. The map is **internally consistent**:
no typos/whitespace variants, no China platform stranded in global Social (and vice-versa),
no ad platform mis-shelved, nothing accidentally defaulted to `Other` except the meta
`account` shelf (correct). **No unambiguous, non-controversial miscategorization was found,
so nothing was force-moved.**

Three placements are *debatable* but are judgment calls, not safe fixes — escalated to the
founder rather than applied:

- **`google-business` (Google Business Profile) — currently SEO.** Its surface is
  listings + reviews management (`reviews`, `review.reply`, `locations`), which parallels
  `trustpilot`/`yelp` in **Reviews & Apps**. But "local SEO" is a legitimate SEO home.
  *Proposal, not applied.*
- **`wechat-search` (搜一搜) — currently China Social.** It is a search engine, so **SEO**
  is arguable; kept in China Social under the geography rule above. *Proposal, not applied.*
- **`github` — currently Developer.** See (a.1). *Structure decision, not applied.*

### On the founder's "mis-platformed ad-search endpoint"

The reported symptom — an **ADS** section inside the **People** platform — was **not** an
ad-search endpoint on `people`. It was 10 Hunter lead-list CRUD endpoints
(`hunter.x.leads-*`, `/leads`, `/leads_lists`) that the *domain heuristic* mislabeled "ads"
because `"ads"` is a substring of `"le​ads"`. That is fixed in **(c)** — People no longer has
an `ads` section at all.

There is a separate, real ad-search trio on the **companies** platform
(`leadmagic.x.{google,meta,b2b}-ads-search`, `/v1/ads/…`), correctly labeled `ads` after the
(c) fix. These are debatable: their *data* is ad-library results (→ would live on
`google`/`meta-ads`/`linkedin`), but their *job* is B2B prospecting ("find companies
advertising on X"), which fits `companies`. Because it is genuinely two-sided, it is left in
place and flagged, not moved. **Proposal:** if the founder prefers data-shape over job-shape,
move them to `google` / `meta-ads` / `linkedin` respectively (each has native ad-library
capabilities); all three carry no `capability`, so the move is validator-safe.

---

## (c) Domain section names — APPLIED (`src/treg/domain/catalog/store.py`)

Two mechanism fixes, **29 endpoints re-sectioned**, no validator or test regressions.

### Fix 1 — keyword matching moved from raw substring to **word boundaries**

`_domain` matched `DOMAIN_KEYWORDS` with `key in " ".join(segments)` — a raw substring test.
That fired keywords *inside* unrelated words and produced false headings:

- `ads` matched inside **le​ads** → an **"ads"** section of 10 Hunter lead endpoints on the
  **People** page (the founder's complaint).
- `ads` matched inside comment**thre​ads** → a YouTube `commentThreads` route filed under
  **ads**.
- `user` matched inside **abuser**eports → a YouTube abuse-report route filed under **user**.

Now the path is split into whole words (on non-letters **and** camelCase humps, via new
`_WORDS = re.compile(r"[A-Z]?[a-z]+")`), and a keyword must match a word:
- stem keys still match by prefix (`keyword` → `keywords`, `backlink` → `backlinks`,
  `shop` → `shopping`);
- short keys (`ads`, `llm`, and `ad_`→`ad`) must match a **whole** word, so `ads` ≠ `leads`
  / `adset`, `user` ≠ `abuser`;
- camelCase is preserved: `searchAnalytics` → `search` + `analytics` (GSC performance keeps
  its `analytics` heading), `fetch_product_detail` → still `product`.

This also *correctly* pulls real ad-library routes **into** `ads` that substring-noise had
scattered: `scrapecreators` Facebook/Google ad-library endpoints (were `facebook`/`search`/
`other`), `tiktok-ads.ads` (was singular `ad`), `microsoft-ads.adgroups`.

### Fix 2 — `web_vN` added to `DOMAIN_NOISE`

TikHub ships routes like `/xiaohongshu/web_v3/…` and `/youtube/web_v2/…`. The `web_v2` /
`web_v3` segment is the "web" delivery marker with a version glued on — how the vendor
organizes its API, not a subject. Left in, it grew a **"web_v2"/"web_v3"** section per
platform (LinkedIn, Weibo, YouTube, Xiaohongshu). Added `web_v2`, `web_v3`, `web_v4` to
`DOMAIN_NOISE` (alongside the existing `dataforseo_labs` / `appendix` brand-family filters);
those 7 routes now fall through to their function keyword or to `other`.

### Net effect of the 29 changes

| Change | Count | Verdict |
|---|---|---|
| Hunter `leads*` : `ads` → `other` (People page loses its bogus ads section) | 10 | fix |
| `web_v2`/`web_v3` brand headings → `other` | 7 | fix |
| Real ad-library routes correctly pulled into `ads` (scrapecreators, tiktok-ads, microsoft-ads) | 6 | fix |
| `commentThreads` `ads`→`video`, `abuseReports` `user`→`other` | 2 | fix |
| meta-ads `adset*` `ads`→`other` (management routes; ideally `kind:account`) | 3 | neutral |
| `xingtu_v2` (was wrongly `ads`) / lone xhs `homefeed` (was `feed`) | 2 | minor |

### Not fixed here — needs a taxonomy edit, not a heuristic tweak

Some brand-family / version headings come from **capability ids** (the middle segment wins in
`_domain`), so `DOMAIN_KEYWORDS`/`DOMAIN_NOISE` cannot touch them:
`google-analytics` shows `measurement_protocol_secret`, `google_ads_link`, `firebase_link`,
`v1beta`, `key_event`; `x` shows `account_activity`, `community_notes`; `douyin` shows
`xingtu`, `xingtu_v2`, `douplus`, `index`. These read as vendor internals, not subjects.
**Proposal:** rename the offending capability middle-segments (e.g. GA `*_link` → `integrations`,
`measurement_protocol_secret` → `settings`; Douyin `xingtu*` → `creator`) on the next taxonomy
pass — a `capabilities.yaml` change, so left for the founder.
