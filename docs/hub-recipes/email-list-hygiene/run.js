// A free first pass over a lead list. Paid verifiers bill for gmail.com, info@ and throwaway inboxes
// that a B2B list never wanted; this drops them before any money moves. No tool is called: the
// domain lists travel with the tool as data.csv (personal providers, plus the CC0
// disposable-email-domains blocklist).
const ROLE = new Set(["admin", "info", "sales", "support", "contact", "hello", "hi", "team", "office",
  "billing", "accounts", "accounting", "finance", "careers", "jobs", "hr", "recruiting", "marketing",
  "press", "media", "pr", "help", "service", "enquiries", "inquiries", "noreply", "no-reply",
  "donotreply", "webmaster", "postmaster", "hostmaster", "abuse", "security", "privacy", "legal",
  "compliance", "partners", "orders", "newsletter", "feedback", "mail", "email", "all", "staff"]);
const SYNTAX = /^[^\s@"(),:;<>[\]\\]+@([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$/;
const MAX = 2000;

export default async function run(ctx) {
  const { emails, allow_personal } = ctx.inputs;
  const kind = new Map((ctx.data || []).map(r => [r.domain, r.type]));
  const seen = new Set();
  const rows = [];
  const summary = { ok: 0, personal: 0, disposable: 0, role: 0, invalid: 0, duplicate: 0 };
  if (emails.length > MAX) ctx.log(`only the first ${MAX} of ${emails.length} were checked`);

  for (const raw of emails.slice(0, MAX)) {
    const email = String(raw ?? "").trim().toLowerCase();
    let verdict = "ok", reason = "";
    const [local, domain] = email.split("@");
    if (!SYNTAX.test(email)) { verdict = "invalid"; reason = "not an email address"; }
    else if (seen.has(email)) { verdict = "duplicate"; reason = "already in this list"; }
    else if (kind.get(domain) === "disposable") { verdict = "disposable"; reason = domain + " is a throwaway inbox"; }
    else if (ROLE.has(local.split("+")[0])) { verdict = "role"; reason = local + "@ is a shared inbox, not a person"; }
    else if (kind.get(domain) === "personal") { verdict = "personal"; reason = domain + " is a personal provider"; }
    seen.add(email);
    summary[verdict]++;
    rows.push({ email, verdict, reason });
  }
  const to_verify = rows.filter(r => r.verdict === "ok" || (allow_personal && r.verdict === "personal")).map(r => r.email);
  ctx.charge(0.002, "fee");                              // the price: $0.002 a run
  return { checked: rows.length, to_verify, summary, rows };
}
