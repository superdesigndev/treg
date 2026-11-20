# search-console-health

One Search Console property's health, in one call, on **your own** Google account.

## Before you call it

Connect Google Search Console to your treg team once (the dashboard, or `treg connect
google-search-console`). Every step runs on that connection, with your consent, and costs nothing
in provider money. The tool never sees your Google token; treg injects it.

## What it does, all at the same time

1. **Ownership**: confirms the property is on the connected account, and with what permission.
2. **Performance**: clicks, impressions, CTR and position for the last 28 days (ending 3 days ago,
   because Search Console data lags), against the 28 days before; the top 10 queries and pages.
3. **Sitemaps**: every sitemap Google knows for the property, with errors, warnings, and submitted
   versus indexed URL counts.
4. **Index status of one page**: the home page by default, or `inspect_url`: Google's verdict,
   coverage state, robots state, last crawl, canonical chosen, mobile usability, rich results.

## The findings

Plain rules, in code:

- **error**: the property is not on this account; the performance report failed; a sitemap has
  errors; the inspected page is not indexed.
- **warning**: clicks or impressions fell more than 30% against the previous window (only when
  the previous window had 50 clicks or 500 impressions); no sitemap submitted; fewer than half of a
  sitemap's URLs indexed; Google chose a different canonical; mobile usability fails.

`health` is `unhealthy` with any error, `needs_attention` with any warning, else `healthy`.

## What it costs

Nothing in provider calls. **$0.01** for the tool (`ctx.charge(0.01, "fee")`).

## Status

Built 2026-09-23 from Google's documented response shapes. **Not yet run against a live
connection**: the local server cannot complete Google's OAuth round trip. First real test on the
hosted service.
