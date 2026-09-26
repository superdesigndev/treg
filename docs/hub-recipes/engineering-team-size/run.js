// Engineering team size, as a cost-ordered ladder. Every tier is one ordinary treg call; the
// first tier that returns a count above zero wins. A reported 0 is treated as "the provider
// could not classify this company", never as "no engineers".
//
//   0. pdl.x.company-clean        free   canonical LinkedIn URL + name (the join key for tier 1)
//   1. crustdata.companies.enrich ~4 cr  headcount.by_role_absolute.Engineering (+ YoY growth)
//   2. pdl.companies.enrich       1 cr   employee_count_by_class.research_and_development
//                                        (present only on PDL plans that carry class data)
const METHODS = ["cost_optimized", "crust_only", "pdl_only", "validate"];

function num(v) { return typeof v === "number" && Number.isFinite(v) ? v : null; }

function range(n) {
  if (n <= 5) return "1-5";
  if (n <= 10) return "6-10";
  if (n <= 25) return "11-25";
  if (n <= 50) return "26-50";
  if (n <= 100) return "51-100";
  if (n <= 250) return "101-250";
  if (n <= 500) return "251-500";
  if (n <= 1000) return "501-1000";
  return "1000+";
}

function attempt(provider, count, extra) {
  return { provider, count, status: count === null ? "no_answer" : count > 0 ? "answered" : "zero", ...(extra || {}) };
}

async function resolveIdentity(ctx, domain) {
  const r = await ctx.call("pdl.x.company-clean", { query: { website: domain } });
  const j = r.status === 200 && r.json ? r.json : null;
  return { linkedin_url: j && typeof j.linkedin_url === "string" ? j.linkedin_url : null,
           name: j && typeof j.name === "string" ? j.name : null, status: r.status };
}

async function crustByBody(ctx, body) {
  const r = await ctx.call("crustdata.companies.enrich", { method: "POST", body: { ...body, fields: ["headcount"] } });
  if (r.status !== 200 || !Array.isArray(r.json)) return { count: null, total: null, growth: null, status: r.status };
  const match = ((r.json[0] || {}).matches || [])[0];
  const h = match && match.company_data && match.company_data.headcount;
  if (!h) return { count: null, total: null, growth: null, status: r.status };
  // No Engineering bucket at all means a wrong-entity match; never sum the other buckets.
  return { count: num((h.by_role_absolute || {}).Engineering), total: num(h.total),
           growth: num((h.by_role_growth_yoy_pct || {}).Engineering), status: r.status };
}

async function crustTier(ctx, domain, linkedinUrl) {
  if (linkedinUrl) {
    const byUrl = await crustByBody(ctx, { professional_network_profile_urls: ["https://www." + linkedinUrl.replace(/^https?:\/\/(www\.)?/, "")] });
    if (byUrl.count !== null) return { ...byUrl, matched_on: "linkedin_url" };
  }
  return { ...(await crustByBody(ctx, { domains: [domain] })), matched_on: "domain" };
}

async function pdlTier(ctx, domain) {
  const r = await ctx.call("pdl.companies.enrich", { query: { website: domain } });
  const j = r.status === 200 && r.json ? r.json : null;
  if (!j) return { count: null, total: null, growth: null, status: r.status };
  return { count: num((j.employee_count_by_class || {}).research_and_development), total: num(j.employee_count),
           growth: num((j.employee_growth_rate_12_month_by_class || {}).research_and_development), status: r.status };
}

export default async function run(ctx) {
  const domain = String(ctx.inputs.domain).trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").replace(/\/.*$/, "");
  const method = METHODS.includes(ctx.inputs.method) ? ctx.inputs.method : "cost_optimized";
  const attempts = [];

  const identity = await resolveIdentity(ctx, domain);
  ctx.log("identity: " + (identity.linkedin_url || "no linkedin url") + " (" + identity.status + ")");

  let crust = { count: null, total: null, growth: null };
  if (method !== "pdl_only") {
    crust = await crustTier(ctx, domain, identity.linkedin_url);
    attempts.push(attempt("crustdata company Engineering headcount", crust.count, { matched_on: crust.matched_on, status_code: crust.status }));
    ctx.log("crustdata: " + crust.count + " engineers of " + crust.total + " via " + crust.matched_on);
  }
  const crustHas = crust.count !== null && crust.count > 0;

  let pdl = { count: null, total: null, growth: null };
  if (method === "pdl_only" || method === "validate" || (method !== "crust_only" && !crustHas)) {
    pdl = await pdlTier(ctx, domain);
    attempts.push(attempt("pdl company R&D class headcount", pdl.count, { status_code: pdl.status }));
    ctx.log("pdl: " + pdl.count + " r&d of " + pdl.total);
  }
  const pdlHas = pdl.count !== null && pdl.count > 0;

  const selected = crustHas ? { count: crust.count, source: "crust_company_engineering_headcount", total: crust.total, growth: crust.growth }
                 : pdlHas ? { count: pdl.count, source: "pdl_company_rnd_headcount", total: pdl.total, growth: pdl.growth }
                 : null;

  const counts = [crust.count, pdl.count].filter((c) => c !== null && c > 0);
  const disagreement = counts.length > 1 ? (Math.max(...counts) - Math.min(...counts)) / Math.max(1, Math.max(...counts)) : null;
  const headcount = selected && selected.total > 0 ? selected.total : (crust.total || pdl.total || null);
  const ratio = selected && headcount ? selected.count / headcount : null;

  const reasons = [];
  if (disagreement !== null && disagreement > 0.5) reasons.push("provider_counts_disagree");
  if (ratio !== null && ratio > 0.6) reasons.push("engineering_count_exceeds_60_percent_of_company_headcount");
  if (ratio !== null && ratio > 1) reasons.push("engineering_density_exceeds_100_percent");
  if (!selected && (crust.count === 0 || pdl.count === 0)) reasons.push("zero_engineering_count_reported");
  if (selected && selected.source.startsWith("pdl") && headcount !== null && headcount < 50) reasons.push("small_company_pdl_estimate_low_confidence");
  if (!identity.linkedin_url) reasons.push("no_canonical_linkedin_url");

  const confidence = !selected ? "none" : reasons.length ? "low" : disagreement !== null && disagreement <= 0.25 ? "strong" : "medium";

  ctx.charge(0.15, "fee");                               // the price: $0.15 a run
  return {
    domain,
    status: !selected ? "no_estimate" : reasons.length ? "needs_review" : "estimated",
    estimated_engineering_count: selected ? selected.count : null,
    estimated_range: selected ? range(selected.count) : null,
    confidence,
    selected_method: selected ? selected.source : null,
    company_headcount: headcount,
    engineering_to_company_ratio: ratio === null ? null : Math.round(ratio * 1000) / 1000,
    engineering_growth_yoy_pct: selected && selected.growth !== null ? Math.round(selected.growth * 10) / 10 : null,
    query_disagreement_ratio: disagreement,
    review_reasons: reasons,
    resolved_linkedin_url: identity.linkedin_url,
    resolved_company_name: identity.name,
    provider_attempts: attempts,
  };
}
