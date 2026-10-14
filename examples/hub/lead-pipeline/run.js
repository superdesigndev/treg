// lead-pipeline: the people at a company, each with a work email that has been checked.
//
// One routed search, then per person: find the email (skipped when the search row already has
// one), verify it, and optionally find a phone. Every call is a routed catalog endpoint, so treg
// picks the provider and bills the caller at cost. This tool adds one thing on top: the caller
// pays the tool's own fee ONLY for people whose email came back `valid` or `risky` (`units` below).
//
// Verifiers use different words for their verdict. `verdict()` turns every one of them into four
// fixed values: valid, risky, invalid, unknown.
//
// Search providers return different row shapes. `person()` maps all of them onto one fixed set
// of columns, so the table looks the same whichever provider answered.

const SOFT_DEADLINE_MS = 90000;   // stop starting new people here; the run's hard limit is 120 s
const CALL_CAP = 20;              // the hub's per-run call cap
// A run that passes its spending ceiling is stopped by treg and returns NOTHING, even though every
// call before it was paid. So the script keeps its own count and stops early instead, returning the
// people already done. This tool declares a $2.00 ceiling (recipe.json `limits.cost_usd`), its fee
// holds up to $0.09 of it, and the calls get `max_spend_usd` (default $0.80, at most $1.80).
// First guess of each call's cost, replaced by the most expensive one actually seen in this run.
const FIRST_GUESS_MICRO = { "treg.people.email.find": 20000, "treg.people.email.verify": 10000, "treg.people.phone.find": 300000 };

export default async function run(ctx) {
  const started = Date.now();
  const { company_domain, title, country, include_phone } = ctx.inputs;
  const budget = Math.round(Math.max(0, Math.min(Number(ctx.inputs.max_spend_usd) || 0, 1.8)) * 1e6);
  let spent = 0;
  const priciest = { ...FIRST_GUESS_MICRO };
  const observed = new Set();
  const call = async (id, body) => {
    const r = await ctx.call(id, { method: "POST", body });
    const cost = Math.round((r.cost_usd || 0) * 1e6);
    spent += cost;
    // the first real price replaces the guess; after that, keep the most expensive one seen
    if (!observed.has(id) || cost > priciest[id]) priciest[id] = cost;
    observed.add(id);
    return r;
  };
  const affordable = id => spent + (priciest[id] || 0) <= budget;
  const domain = String(company_domain).trim().toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];
  const perPerson = include_phone ? 3 : 2;
  const maxPeople = Math.floor((CALL_CAP - 1) / perPerson);          // 9 without phone, 6 with
  const limit = Math.max(1, Math.min(ctx.inputs.limit, maxPeople));
  if (limit < ctx.inputs.limit) ctx.log(`limit lowered to ${limit}: ${perPerson} calls per person, ${CALL_CAP} per run`);

  // 1. search
  const searchBody = { company_domain: domain };
  if (title) searchBody.title = title;
  if (country) searchBody.country = country;
  const s = await call("treg.people.search", searchBody);
  if (s.status === 400 || s.status === 404) {
    // no provider found anyone at this company: an empty table, not an error
    ctx.log(`search found nobody at ${domain} (${s.status})`);
    return { people: [], count: 0, valid: 0, risky: 0, searched_by: null, stopped_early: false,
             spent_usd: spent / 1e6, units: 0 };
  }
  if (s.status !== 200) throw new Error(`people search answered ${s.status}`);
  const searchedBy = served(s);
  const rows = ((s.json && s.json.output && s.json.output.people) || []).map(r => person(r, domain));

  // keep rows we can look up, drop duplicates
  const seen = new Set();
  let people = [];
  for (const p of rows) {
    const key = (p.linkedin || p.name || "").toLowerCase();
    if (!key || seen.has(key) || (!p.name && !p.linkedin)) continue;
    seen.add(key);
    people.push(p);
    if (people.length >= limit) break;
  }
  ctx.log(`search: ${rows.length} rows from ${searchedBy}, ${people.length} kept`);

  // 2-4. per person: find, verify, phone
  let stoppedEarly = false;
  const emails = new Set();
  const stop = (p, why) => { stoppedEarly = true; p.note = p.note || `not looked up: ${why}`; };
  for (const p of people) {
    if (Date.now() - started > SOFT_DEADLINE_MS) { stop(p, "time limit"); continue; }
    const who = identity(p, domain);

    if (!p.email && who) {
      if (!affordable("treg.people.email.find")) { stop(p, "spending limit"); continue; }
      const f = await call("treg.people.email.find", who);
      const out = f.status === 200 && f.json && f.json.output;
      if (out && out.email && workEmail(out.email, domain)) { p.email = out.email; p.email_source = served(f); }
      else if (out && out.email) ctx.log(`dropped ${out.email}: not a work email at ${domain}`);
    }
    if (p.email && emails.has(p.email)) { p.duplicate = true; continue; }   // same person, another row
    if (p.email) emails.add(p.email);
    if (p.email) {
      if (!affordable("treg.people.email.verify")) { stop(p, "spending limit"); continue; }
      const v = await call("treg.people.email.verify", { email: p.email });
      const out = v.status === 200 && v.json && v.json.output;
      p.email_status = verdict(out);
    }
    if (include_phone && who) {
      if (!affordable("treg.people.phone.find")) { p.note = "phone not looked up: spending limit"; stoppedEarly = true; continue; }
      const ph = await call("treg.people.phone.find", who);
      const out = ph.status === 200 && ph.json && ph.json.output;
      if (out && out.phone) p.phone = out.phone;
    }
  }

  people = people.filter(p => !p.duplicate);
  const valid = people.filter(p => p.email_status === "valid").length;
  const risky = people.filter(p => p.email_status === "risky").length;
  ctx.log(`${valid} valid, ${risky} risky, of ${people.length} people; calls cost $${(spent / 1e6).toFixed(4)}`);
  // The price: $0.01 per valid or risky email, never more than the $0.09 cap in recipe.json.
  if (valid + risky) ctx.charge(Math.min(valid + risky, 9) * 0.01, "per usable email");
  return {
    people: people.map(columns),
    count: people.length,
    valid,
    risky,
    searched_by: searchedBy,
    stopped_early: stoppedEarly,
    spent_usd: spent / 1e6,
    units: valid + risky,                  // the fee is charged per valid or risky email only
  };
}

// ---------------------------------------------------------------- one row shape for every provider

function columns(p) {
  return {
    name: p.name || null, first_name: p.first_name || null, last_name: p.last_name || null,
    title: p.title || null, company: p.company || null, linkedin: p.linkedin || null,
    location: p.location || null, email: p.email || null, email_status: p.email_status || (p.email ? "unknown" : "not_found"),
    phone: p.phone || null, note: p.note || null,
  };
}

function person(r, domain) {
  const first = pick(r, "first_name", "firstName", "firstname");
  const last = pick(r, "last_name", "lastName", "lastname");
  const full = pick(r, "full_name", "fullName", "name") || [first, last].filter(Boolean).join(" ") || null;
  const bp = r.basic_profile || {};
  const cj = r.current_job || {};
  const jt = r.jobTitle;
  return {
    first_name: first || null,
    last_name: last || null,
    name: full || bp.name || null,
    title: text(pick(r, "title", "lastJobTitle", "position", "job_title") || (jt && (jt.title || jt)) || bp.current_title || cj.title || pick(r, "headline")),
    company: text(pick(r, "company_name", "companyName", "lastCompanyName") || (r.company && (r.company.name || r.company)) || (r.organization && r.organization.name) || cj.company_name) || domain,
    linkedin: linkedinUrl(r),
    location: text(pick(r, "location", "country_code", "address")),
    email: workEmail(emailOf(r), domain) ? emailOf(r) : null,
    phone: phoneOf(r),
  };
}

function linkedinUrl(r) {
  const direct = pick(r, "employee_linkedin", "linkedinUrl", "linkedin_url", "linkedin", "profileUrl", "profile_url");
  const nested = (r.socialLinks && r.socialLinks.linkedin) || (r.URLs && r.URLs.linkedin) ||
    (r.social_handles && r.social_handles.professional_network_identifier && r.social_handles.professional_network_identifier.profile_url);
  const u = text(direct || nested);
  return u && /linkedin\.com\//i.test(u) ? u.replace(/^http:\/\//i, "https://") : null;
}

// A WORK email only. Found live 2026-09-25 (hub simulation run 1): a finder returned a Gmail address
// for a company's CMO, the checker called the mailbox valid, and the tool charged its fee. An address
// at a free mail provider is never a work email; with a company domain given, the address must be at
// that domain (or one of its subdomains). Anything else counts as not found, and no fee is charged.
const FREE_MAIL = new Set(["gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com",
  "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me", "protonmail.com", "gmx.com",
  "gmx.net", "mail.com", "yandex.com", "yandex.ru", "qq.com", "163.com", "126.com", "zoho.com", "fastmail.com"]);

function workEmail(email, domain) {
  const at = String(email || "").toLowerCase().split("@")[1] || "";
  if (!at || FREE_MAIL.has(at)) return false;
  return !domain || at === domain || at.endsWith("." + domain);
}

function emailOf(r) {
  // a directory row's email counts only when the provider actually returned it (not locked)
  const e = pick(r, "email", "value", "work_email");
  if (typeof e === "string" && e.includes("@") && r.emailUnlocked !== false) return e.toLowerCase();
  return null;
}

function phoneOf(r) {
  const p = pick(r, "phone_number", "phone", "mobile_phone");
  if (typeof p === "string" && p) return p;
  if (Array.isArray(r.phoneNumbers) && r.phoneNumbers.length && r.phoneUnlocked !== false) return text(r.phoneNumbers[0]);
  return null;
}

function identity(p, domain) {
  if (p.linkedin) return { linkedin_url: p.linkedin, ...(p.name ? { full_name: p.name, domain } : {}) };
  if (p.first_name && p.last_name) return { first_name: p.first_name, last_name: p.last_name, domain };
  if (p.name && p.name.includes(" ")) return { full_name: p.name, domain };
  return null;
}

function verdict(out) {
  if (!out) return "unknown";
  const s = String(out.status || "").toLowerCase();
  if (/invalid|undeliverable|bounce|disposable|rejected|not_found|does_not_exist/.test(s)) return "invalid";
  if (/risky|accept_all|accept-all|catch_all|catch-all|catchall/.test(s)) return "risky";
  if (/^(valid|deliverable|ok|safe|verified)$/.test(s)) return "valid";
  if (!s && out.valid === true) return "valid";
  if (!s && out.valid === false) return "invalid";
  return "unknown";
}

function served(r) {
  return (r.headers && (r.headers["x-treg-served-by"] || r.headers["X-Treg-Served-By"])) ||
    (r.json && r.json._treg && r.json._treg.served_by) || null;
}

function pick(o, ...keys) {
  for (const k of keys) if (o[k] !== undefined && o[k] !== null && o[k] !== "") return o[k];
  return null;
}

function text(v) {
  if (v === null || v === undefined) return null;
  if (typeof v === "string") return v.trim() || null;
  if (typeof v === "object") return text(v.name || v.title || v.value || v.url || v.country || null);
  return String(v);
}
