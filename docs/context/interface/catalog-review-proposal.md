---
title: Catalog browse taxonomy — open placement and naming decisions
status: backlog
sources: []
related:
  - architecture/catalog.md
  - interface/dashboard.md
---

# Catalog browse taxonomy — open decisions

Open questions from a 2026-09-21 reviewer pass over the browse taxonomy an agent (or human) navigates
to find data: the top-level **categories**, each platform's **category** placement, and the
per-platform **domain sections**. What that pass already applied — the `_domain` word-boundary
matching fix and the `DOMAIN_NOISE` additions — now lives in
[architecture/catalog.md](../architecture/catalog.md#domain-sections--grouping-endpoints-for-browse) as
shipped behavior. This document keeps only what is still the founder's call.

## (a) Category structure

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

## (b) Platform → category placement

All 78 platforms were audited against their category (2026-09-21) and the map came back internally
consistent — no unambiguous miscategorization was force-moved. Three placements are debatable
judgment calls, escalated rather than applied:

- **`google-business` (Google Business Profile) — currently SEO.** Its surface is
  listings + reviews management (`reviews`, `review.reply`, `locations`), which parallels
  `trustpilot`/`yelp` in **Reviews & Apps**. But "local SEO" is a legitimate SEO home.
- **`wechat-search` (搜一搜) — currently China Social.** It is a search engine, so **SEO**
  is arguable; kept in China Social under the geography rule above.
- **`github` — currently Developer.** See (a.1).

There is also a real ad-search trio on the **companies** platform
(`leadmagic.x.{google,meta,b2b}-ads-search`, `/v1/ads/…`). Their *data* is ad-library results (→ would
live on `google`/`meta-ads`/`linkedin`), but their *job* is B2B prospecting ("find companies
advertising on X"), which fits `companies`. Left in place and flagged, not moved. **Proposal:** if the
founder prefers data-shape over job-shape, move them to `google` / `meta-ads` / `linkedin`
respectively (each has native ad-library capabilities); all three carry no `capability`, so the move
is validator-safe.

## (c) Domain section renames — needs a taxonomy edit, not a heuristic tweak

Some brand-family / version headings come from **capability ids**, so the shipped `_domain` heuristic
(`architecture/catalog.md`) cannot touch them: `google-analytics` shows `measurement_protocol_secret`,
`google_ads_link`, `firebase_link`, `v1beta`, `key_event`; `x` shows `account_activity`,
`community_notes`; `douyin` shows `xingtu`, `xingtu_v2`, `douplus`, `index`. These read as vendor
internals, not subjects. **Proposal:** rename the offending capability middle-segments (e.g. GA
`*_link` → `integrations`, `measurement_protocol_secret` → `settings`; Douyin `xingtu*` → `creator`)
on the next taxonomy pass — a `capabilities.yaml` change, so left for the founder.
