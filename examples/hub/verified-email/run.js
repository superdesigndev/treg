// verified-email: one person's work email, found and then checked, in one call.
//
// 360 teams do this by hand every week: find the email, then verify it, one check per email.
// This tool does both, and charges its own fee ONLY when the email came back valid or risky
// (`units` below); an invalid or missing email costs the provider calls and nothing more.
//
// Both steps are routed (treg picks the provider). The find takes a LinkedIn URL, or a full
// name and a domain, or first + last name and a domain. When the find already vouches for
// deliverability (`verified: true`), the verify step is skipped: no second charge for the same
// fact. The email status is always one of four values, whatever verifier answered: valid,
// risky (the domain accepts everything, so the mailbox cannot be proven), invalid, unknown.

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

export default async function run(ctx) {
  const who = identity(ctx.inputs);
  if (!who) throw new Error("give linkedin_url, or full_name + domain, or first_name + last_name + domain");
  let spent = 0;
  const call = async (id, body) => { const r = await ctx.call(id, { method: "POST", body }); spent += r.cost_usd || 0; return r; };

  const f = await call("treg.people.email.find", who);
  const found = f.status === 200 && f.json && f.json.output;
  const foundBy = f.status === 200 && f.json && f.json._treg && f.json._treg.served_by || null;
  if (found && found.email && !workEmail(found.email, who.domain)) {
    ctx.log(`dropped ${found.email} from ${foundBy}: not a work email${who.domain ? " at " + who.domain : ""}`);
    found.email = null;
  }
  if (!found || !found.email) {
    ctx.log(`no email found (${f.status})`);
    return { email: null, email_status: "not_found", found_by: foundBy, verified_by: null,
             confidence: null, first_name: null, last_name: null, spent_usd: round(spent), units: 0 };
  }

  let status, verifiedBy = null;
  if (found.verified === true) {
    status = "valid"; verifiedBy = foundBy;                 // the finder checked the mailbox itself
    ctx.log(`found by ${foundBy}, which vouched for it: verify skipped`);
  } else {
    const v = await call("treg.people.email.verify", { email: found.email });
    const out = v.status === 200 && v.json && v.json.output;
    status = verdict(out);
    verifiedBy = v.status === 200 && v.json && v.json._treg && v.json._treg.served_by || null;
    ctx.log(`found by ${foundBy}, verified by ${verifiedBy}: ${status}`);
  }
  const usable = status === "valid" || status === "risky";
  if (usable) ctx.charge(0.01, "usable email");          // the price: $0.01 per valid or risky email
  return {
    email: String(found.email).toLowerCase(), email_status: status, found_by: foundBy, verified_by: verifiedBy,
    confidence: typeof found.confidence === "number" ? round(found.confidence) : null,
    first_name: found.first_name || null, last_name: found.last_name || null,
    spent_usd: round(spent), units: usable ? 1 : 0,
  };
}

function identity(i) {
  const s = v => (v === undefined || v === null) ? "" : String(v).trim();
  const domain = s(i.domain).toLowerCase().replace(/^https?:\/\//, "").replace(/^www\./, "").split("/")[0];
  if (s(i.linkedin_url)) return { linkedin_url: s(i.linkedin_url) };
  if (s(i.first_name) && s(i.last_name) && domain) return { first_name: s(i.first_name), last_name: s(i.last_name), domain };
  if (s(i.full_name) && domain) return { full_name: s(i.full_name), domain };
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

function round(v) { return Math.round(v * 10000) / 10000; }
