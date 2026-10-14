// domain-authority: how strong a site is in Google, in one call.
//
// Three Serpstat reports at the same time (backlinks summary, ranked keywords, linking domains),
// folded into one report card: the numbers a person compares by hand today, a top-20 table of
// the sites that link here, and a one-word verdict computed in code. 14 teams buy the three
// reports separately for one domain. Serpstat is a database, so the whole run is a few seconds.
// No judgment model: these are numbers.

const SE_TIMEOUT_S = 40;
const TOP_LINKERS = 20;
const TOP_KEYWORDS = 20;

export default async function run(ctx) {
  const domain = String(ctx.inputs.domain).trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];
  const se = String(ctx.inputs.search_engine || "g_us").trim();
  const rpc = (method, params) => ({ method: "POST", body: { id: "1", method, params }, timeout_s: SE_TIMEOUT_S });

  const [sum, kw, refs] = await Promise.all([
    safe(ctx, "serpstat.web.backlinks.summary", rpc("SerpstatBacklinksProcedure.getSummaryV2", { query: domain, searchType: "domain" })),
    safe(ctx, "serpstat.google.domain.ranked_keywords", rpc("SerpstatDomainProcedure.getDomainKeywords", { domain, se, size: TOP_KEYWORDS, sort: { traff: "desc" } })),
    safe(ctx, "serpstat.web.linking_domains.list", rpc("SerpstatBacklinksProcedure.getRefDomains", { query: domain, searchType: "domain", size: TOP_LINKERS, sort: "domain_links", order: "desc" })),
  ]);

  const d = sum.ok && sum.json.result && sum.json.result.data || null;
  const kws = kw.ok && kw.json.result && Array.isArray(kw.json.result.data) ? kw.json.result.data : null;
  const kwTotal = kw.ok && kw.json.result && kw.json.result.summary_info ? num(kw.json.result.summary_info.total) : null;
  const linkers = refs.ok && refs.json.result && Array.isArray(refs.json.result.data) ? refs.json.result.data : null;
  const linkersTotal = refs.ok && refs.json.result && refs.json.result.summary_info ? num(refs.json.result.summary_info.total) : null;

  const report = {
    domain_rank: d ? num(d.sersptat_domain_rank) : null,             // Serpstat's own 0-100 authority score
    backlinks: d ? num(d.backlinks) : null,
    backlinks_change: d ? num(d.backlinks_change) : null,
    dofollow_backlinks: d ? num(d.dofollow_backlinks) : null,
    referring_domains: d ? num(d.referring_domains) : null,
    referring_domains_change: d ? num(d.referring_domains_change) : null,
    referring_ips: d ? num(d.referring_ip_addresses) : null,
    malicious_referring_domains: d ? num(d.referring_malicious_domains) : null,
    keywords_ranked: kwTotal,
    keywords_top10: kws ? kws.filter(k => num(k.position) !== null && num(k.position) <= 10).length : null,   // within the top rows fetched
    estimated_monthly_traffic: kws ? kws.reduce((a, k) => a + (num(k.traff) || 0), 0) : null,                // sum over the top rows fetched
  };
  const topKeywords = (kws || []).map(k => ({ keyword: k.keyword, position: num(k.position), monthly_searches: num(k.region_queries_count),
                                              traffic: num(k.traff), difficulty: num(k.difficulty), url: k.url || null }));
  const topLinkers = (linkers || []).map(l => ({ domain: l.domain_from, domain_rank: num(l.domainRank), linking_pages: num(l.ref_pages) }));

  const verdict = grade(report);
  const failed = [["backlinks_summary", sum], ["ranked_keywords", kw], ["linking_domains", refs]].filter(([, r]) => !r.ok).map(([n, r]) => `${n}: ${r.note}`);
  ctx.log(`${domain}: rank ${report.domain_rank}, ${report.referring_domains} referring domains, ${report.keywords_ranked} keywords → ${verdict}` + (failed.length ? `; failed ${failed.join(", ")}` : ""));
  ctx.charge(0.01, "fee");                               // the price: $0.01 a run
  return {
    domain, search_engine: se, verdict, report,
    top_keywords: topKeywords, top_linking_domains: topLinkers,
    linking_domains_total: linkersTotal,
    reports_answered: 3 - failed.length, failed,
    summary: `${domain}: ${verdict}. Rank ${report.domain_rank ?? "?"}, ${fmt(report.referring_domains)} referring domains, ${fmt(report.keywords_ranked)} ranked keywords.`,
  };
}

async function safe(ctx, id, opts) {
  try {
    const r = await ctx.call(id, opts);
    if (r.timed_out) return { ok: false, note: `no answer in ${SE_TIMEOUT_S} s`, json: null };
    if (r.status !== 200 || !r.json) return { ok: false, note: `answered ${r.status}`, json: null };
    if (r.json.error) return { ok: false, note: String(r.json.error.message || "provider error").slice(0, 100), json: null };
    return { ok: true, note: null, json: r.json };
  } catch (err) { return { ok: false, note: String(err && err.message || err).slice(0, 100), json: null }; }
}

// The verdict is a plain rule, stated in the README so it can be argued with.
function grade(r) {
  const rd = r.referring_domains, rank = r.domain_rank, kw = r.keywords_ranked;
  if (rd === null && rank === null && kw === null) return "unknown";
  if ((rd ?? 0) >= 10000 || (rank ?? 0) >= 60 || (kw ?? 0) >= 100000) return "strong";
  if ((rd ?? 0) >= 500 || (rank ?? 0) >= 30 || (kw ?? 0) >= 5000) return "medium";
  return "weak";
}
function num(v) { if (v === null || v === undefined || v === "") return null; const n = Number(v); return Number.isFinite(n) ? n : null; }
function fmt(n) { return n === null || n === undefined ? "?" : n.toLocaleString("en-US"); }
