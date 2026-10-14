// search-console-health: one Search Console property's health, in one call, on the CALLER's own
// Google connection. Every step is a Google Search Console endpoint (scope own_account, free): a
// hub catalog step runs as the caller, so the run reads the caller's data with the caller's
// consent and costs nothing in provider money. This is the tool that proves the hub works on a
// customer's own accounts.
//
// Four reports at once: search performance for the last 28 days against the 28 before, the
// sitemaps Google knows about and their errors, an index inspection of the home page (or the
// URL given), and the property list to confirm the caller actually owns the property. Folded
// into one card with plain-rule findings (in code, stated in the README).
//
// UNTESTED against a live connection as of 2026-09-23: the local server cannot complete Google's
// OAuth round trip (redirect_uri_mismatch). Shapes follow Google's documented responses.

const STEP_TIMEOUT_S = 40;
const DAYS = 28;
const LAG_DAYS = 3;                 // Search Console data lags about two days; three is safe

export default async function run(ctx) {
  const site = String(ctx.inputs.site).trim();
  const inspect = String(ctx.inputs.inspect_url || "").trim() || homeOf(site);
  const { current, previous } = windows(DAYS, LAG_DAYS);

  const [sites, perfNow, perfThen, byQuery, byPage, sitemaps, inspection] = await Promise.all([
    safe(ctx, "google-search-console.sites", { method: "GET" }),
    safe(ctx, "google-search-console.performance", { method: "POST", query: { siteUrl: site }, body: { startDate: current[0], endDate: current[1] } }),
    safe(ctx, "google-search-console.performance", { method: "POST", query: { siteUrl: site }, body: { startDate: previous[0], endDate: previous[1] } }),
    safe(ctx, "google-search-console.performance", { method: "POST", query: { siteUrl: site }, body: { startDate: current[0], endDate: current[1], dimensions: ["query"], rowLimit: 10 } }),
    safe(ctx, "google-search-console.performance", { method: "POST", query: { siteUrl: site }, body: { startDate: current[0], endDate: current[1], dimensions: ["page"], rowLimit: 10 } }),
    safe(ctx, "google-search-console.sitemaps", { method: "GET", query: { siteUrl: site } }),
    inspect ? safe(ctx, "google-search-console.url_inspection", { method: "POST", body: { inspectionUrl: inspect, siteUrl: site } }) : Promise.resolve({ ok: false, note: "no url to inspect", json: null }),
  ]);

  // ownership: is this property on the caller's account, and with what permission?
  const entries = sites.ok && Array.isArray(sites.json.siteEntry) ? sites.json.siteEntry : null;
  const mine = entries ? entries.find(e => e.siteUrl === site) : null;
  const permission = mine ? mine.permissionLevel : (entries ? "not_on_this_account" : null);

  // performance: totals now vs before
  const now = totals(perfNow), before = totals(perfThen);
  const performance = {
    window_days: DAYS, current: { from: current[0], to: current[1], ...now }, previous: { from: previous[0], to: previous[1], ...before },
    clicks_change_pct: pct(before.clicks, now.clicks), impressions_change_pct: pct(before.impressions, now.impressions),
    top_queries: rows(byQuery).map(r => ({ query: r.keys && r.keys[0], clicks: r.clicks, impressions: r.impressions, ctr: r.ctr, position: r.position })),
    top_pages: rows(byPage).map(r => ({ page: r.keys && r.keys[0], clicks: r.clicks, impressions: r.impressions, ctr: r.ctr, position: r.position })),
  };

  // sitemaps
  const smaps = sitemaps.ok && Array.isArray(sitemaps.json.sitemap) ? sitemaps.json.sitemap : [];
  const sitemapRows = smaps.map(s => ({ path: s.path, last_submitted: s.lastSubmitted || null, last_downloaded: s.lastDownloaded || null,
                                        errors: num(s.errors), warnings: num(s.warnings), is_pending: !!s.isPending,
                                        urls_submitted: (s.contents || []).reduce((a, c) => a + (num(c.submitted) || 0), 0),
                                        urls_indexed: (s.contents || []).reduce((a, c) => a + (num(c.indexed) || 0), 0) }));

  // index inspection of one page
  const ir = inspection.ok && inspection.json.inspectionResult;
  const idx = ir && ir.indexStatusResult;
  const page = ir ? { url: inspect, verdict: idx && idx.verdict || null, coverage: idx && idx.coverageState || null,
                      indexing_allowed: idx ? idx.indexingState : null, robots: idx && idx.robotsTxtState || null,
                      last_crawl: idx && idx.lastCrawlTime || null, canonical_google: idx && idx.googleCanonical || null,
                      canonical_user: idx && idx.userCanonical || null,
                      mobile: ir.mobileUsabilityResult && ir.mobileUsabilityResult.verdict || null,
                      rich_results: ir.richResultsResult && ir.richResultsResult.verdict || null,
                      inspection_link: ir.inspectionResultLink || null }
                  : { url: inspect, verdict: null, note: inspection.note };

  // findings: plain rules, each one a sentence a person can act on
  const findings = [];
  if (permission === "not_on_this_account") findings.push({ level: "error", text: `${site} is not a property on the connected Google account` });
  if (!perfNow.ok) findings.push({ level: "error", text: `performance report failed: ${perfNow.note}` });
  if (now.clicks !== null && before.clicks !== null && before.clicks >= 50 && now.clicks < before.clicks * 0.7)
    findings.push({ level: "warning", text: `clicks fell ${Math.round(100 - 100 * now.clicks / before.clicks)}% against the previous ${DAYS} days (${before.clicks} → ${now.clicks})` });
  if (now.impressions !== null && before.impressions !== null && before.impressions >= 500 && now.impressions < before.impressions * 0.7)
    findings.push({ level: "warning", text: `impressions fell ${Math.round(100 - 100 * now.impressions / before.impressions)}% (${before.impressions} → ${now.impressions})` });
  if (sitemaps.ok && smaps.length === 0) findings.push({ level: "warning", text: "no sitemap is submitted for this property" });
  for (const s of sitemapRows) {
    if (s.errors) findings.push({ level: "error", text: `sitemap ${s.path} has ${s.errors} error(s)` });
    if (s.urls_submitted && s.urls_indexed !== null && s.urls_indexed < s.urls_submitted * 0.5)
      findings.push({ level: "warning", text: `sitemap ${s.path}: only ${s.urls_indexed} of ${s.urls_submitted} submitted URLs are indexed` });
  }
  if (page.verdict && page.verdict !== "PASS") findings.push({ level: "error", text: `${inspect} is not indexed: ${page.coverage || page.verdict}` });
  if (page.canonical_google && page.canonical_user && page.canonical_google !== page.canonical_user)
    findings.push({ level: "warning", text: `Google chose a different canonical for ${inspect}: ${page.canonical_google}` });
  if (page.mobile && page.mobile !== "PASS") findings.push({ level: "warning", text: `${inspect} fails mobile usability` });

  const errors = findings.filter(f => f.level === "error").length, warnings = findings.filter(f => f.level === "warning").length;
  const health = errors ? "unhealthy" : warnings ? "needs_attention" : (perfNow.ok ? "healthy" : "unknown");
  const failed = [["sites", sites], ["performance", perfNow], ["performance_previous", perfThen], ["sitemaps", sitemaps], ["url_inspection", inspection]].filter(([, r]) => !r.ok).map(([n, r]) => `${n}: ${r.note}`);
  ctx.log(`${site}: ${health}, ${errors} error(s), ${warnings} warning(s); clicks ${before.clicks} → ${now.clicks}`);
  ctx.charge(0.01, "fee");                               // the price: $0.01 a run
  return {
    site, permission, health, findings, performance, sitemaps: sitemapRows, page, failed,
    summary: `${site}: ${health}. ${now.clicks ?? "?"} clicks and ${now.impressions ?? "?"} impressions in the last ${DAYS} days` +
      (before.clicks !== null && now.clicks !== null ? ` (${signed(pct(before.clicks, now.clicks))}% clicks)` : "") +
      `; ${errors} error(s), ${warnings} warning(s).`,
  };
}

async function safe(ctx, id, opts) {
  try {
    const r = await ctx.call(id, { ...opts, timeout_s: STEP_TIMEOUT_S });
    if (r.timed_out) return { ok: false, note: `no answer in ${STEP_TIMEOUT_S} s`, json: null };
    if (r.status === 401 || r.status === 403) return { ok: false, note: `Google refused (${r.status}): connect Search Console, or this account cannot see the property`, json: null };
    if (r.status !== 200 || !r.json) return { ok: false, note: `answered ${r.status}`, json: null };
    return { ok: true, note: null, json: r.json };
  } catch (err) { return { ok: false, note: String(err && err.message || err).slice(0, 120), json: null }; }
}

function totals(r) {
  const row = rows(r)[0];
  return row ? { ok: true, clicks: num(row.clicks), impressions: num(row.impressions), ctr: num(row.ctr), position: num(row.position) }
             : { ok: r.ok, clicks: r.ok ? 0 : null, impressions: r.ok ? 0 : null, ctr: null, position: null, note: r.note };
}
function rows(r) { return r.ok && Array.isArray(r.json.rows) ? r.json.rows : []; }
function windows(days, lag) {
  const end = new Date(Date.now() - lag * 86400000); const start = new Date(end.getTime() - (days - 1) * 86400000);
  const pEnd = new Date(start.getTime() - 86400000); const pStart = new Date(pEnd.getTime() - (days - 1) * 86400000);
  const d = x => x.toISOString().slice(0, 10);
  return { current: [d(start), d(end)], previous: [d(pStart), d(pEnd)] };
}
function homeOf(site) {
  if (site.startsWith("sc-domain:")) return "https://" + site.slice(10).replace(/\/+$/, "") + "/";
  return /^https?:\/\//.test(site) ? site.replace(/\/*$/, "/") : null;
}
function pct(before, now) { return before === null || now === null || !before ? null : Math.round(1000 * (now - before) / before) / 10; }
function signed(v) { return v === null ? "?" : (v > 0 ? "+" : "") + v; }
function num(v) { if (v === null || v === undefined || v === "") return null; const n = Number(v); return Number.isFinite(n) ? n : null; }
