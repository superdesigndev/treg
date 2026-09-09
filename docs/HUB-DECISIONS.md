---
title: The tool hub — the decisions, from the owner interview
status: settled 2026-09-09, no code written yet
---

# The tool hub — the decisions

The tool hub lets a maker turn something they have into a tool other people's agents can call:
their own data (Supabase, Google Sheets, a pasted CSV), their own routing logic, or a combination
of catalog tools. Two kinds of recipe: a JSON steps file, or a script. A script runs in a sandbox
and reaches the maker's treg account only through `ctx.call`: the maker's registered tools, their
credentials by name, their credit. Nobody ever pastes a key into a script. The tool is callable
at once, by API and by MCP, with a URL to share, and the maker sets a price.

This file is the record of a question-and-answer session with the owner on 2026-09-09, before any
design work. It follows the owner's meeting with Jason on 2026-09-08 (the single source of truth for
the roadmap) and the earlier recipe decisions in `docs/RECIPES-DECISIONS.md`, which it extends and
in places overrides. **The questions are kept word for word.** Under each one is the answer the
owner approved. Where the owner added something the question did not ask, it is written under
"The owner added".

The purpose: no design decision below rests on a guess.

---

## The frame the owner set

- This is not a prototype. It is the MVP, the real system. We test it with Mitchel, then finalize
  it and bring it up.
- Two kinds of recipe from the start: a JSON steps file, or a script.
- The case study is data: a script connects to the maker's Supabase or Google Sheet, or to a pasted
  CSV, and serves the data with input parameters (filter, search, limit).
- A script gets the maker's treg context: all their tools, their own credit, their own credentials,
  referred to by name. They never write a credential down.
- The whole thing is an MCP from day one. Their agents create the tool and get the API call back
  immediately, and callers' agents call it immediately.
- From the 2026-09-08 meeting: "just serve" is permissionless and live at once; listing in
  discovery needs a pull request and verification. Makers set a price and earn credits.

**The owner added, at the end:** the agent-facing files must tell the agent: if you use a new
credential that is not yet in the system, do not hard-code it. First use another tool to create it
(register the secret or the tool), then use it.

---

## Round 1 — the boundary

**1. Is the data tool its own template system, or a script recipe we write?**
No template system in the MVP. treg writes one script recipe per source kind (Supabase, Sheets,
CSV). The maker fills its inputs.

The owner added: the template idea is for more data providers, later. This recipe has no
template; it is just a script.

**2. Does the sandbox exist from day one?**
Yes. A separate short-lived process that we control, with memory and time caps and no network.

**3. How does a script reach the maker's Supabase?**
The maker registers Supabase as an own tool (base URL + key) exactly as today. The script calls it
with `ctx.call("supabase/rest/v1/leads?...")`. The sandbox stays network-free; `ctx.call` is the
only road out.

**4. Does the script ever see a secret value?**
Never. Secrets ride only inside `ctx.call` through the team's registered tools. There is no
`ctx.secret(name)`.

**5. Where does the seller's money go?**
The full amount lands on the seller's team as a new credit-block kind, `earned`. Spendable as treg
credits at once. Withdrawal is backlog.

**6. Where does a pasted CSV live?**
treg stores it, cap 50 MB per tool, read-only after upload. The same data script serves it.
Replacing the file makes a new version.

**7. Is anything listed in the prototype?**
Everything in the MVP is live-but-unlisted: callable by id and by URL, absent from search. The
listing road (pull request, verification) is out of the MVP.

**8. How does a maker create the tool in the prototype?**
The agent road first: API plus MCP verbs to create, test, and price the tool, and a minimal
dashboard page that shows what exists. The rich interface comes from the mockup after.

The owner added: this is the MVP, not a prototype. API and MCP both, tested immediately, then
finalized and brought up.

**9. What does a hub tool's id look like on the call road?**
Dotted, like catalog ids: `mitchel.leads-db`. The handle is the first segment. No slash, so no
collision with own-tool resolution, and MCP-friendly.

**10. What inputs does the data case study declare?**
Four: `select` (columns), `filter` (`column=value` pairs), `search` (one text field), `limit`
(int, default 20, max 100). Nothing else in version one.

---

## Round 2 — the script contract and the run

**1. What is the script's file and language?**
`run.js`, one default async function, run by an embedded JavaScript engine in a child process
(QuickJS). No Node, no npm, no imports.

**2. Does `ctx.call` for an own tool take the same shape as a catalog call?**
One function, three target shapes, same as `/call/` today: a catalog id, `<tool>/<path>`, or a
full URL. `ctx.call(target, {method, query, body, headers})`. It returns
`{status, headers, json, text}`.

**3. Must the manifest name the team tools the script will use?**
Yes. `uses` accepts catalog ids and own-tool names. A `ctx.call` outside the list is refused.

**4. What may the script return?**
One JSON object, at most 2 MB. The manifest declares its top-level fields; the runner checks they
exist and refuses the run otherwise.

**5. Where does the run's own compute cost go?**
Not billed in the MVP. The caller pays the steps plus the seller's price. Compute is ours until we
know the numbers.

**6. What are the run caps for a script?**
The same as a JSON recipe: 120 seconds wall, 20 `ctx.call`s, 64 MB memory, 4 concurrent runs per
team.

**7. Can a script log, and who reads it?**
`ctx.log(text)`, 50 lines, 2 KB each, kept on the run record for 30 days. The maker sees their own
tool's logs; a caller sees only the trace, not the logs.

**8. What happens when the maker's Supabase key is revoked mid-life?**
The step fails, the run fails with the trace naming the tool, the caller is charged nothing for the
failed step and nothing for the seller's price. Health marks the tool failing after 3 failed runs
in a row.

**9. Does a script run against the maker's team or the caller's team?**
The maker's team for own tools and secrets, the caller's team for money and identity. A run carries
both: it spends the caller's balance and uses the maker's registered keys.

**10. Do we test the script before it goes live?**
Yes, on create: the maker supplies `check.json` (sample inputs plus required output fields), treg
runs it once for real on the maker's own balance, and the tool goes live only when it passes.

---

## Round 3 — price and money

**1. How does the seller state a price?**
One field in the manifest: `price_usd` per successful run, decimals allowed (`0.01`). treg shows
the per-1,000 line for them. Zero means free.

**2. What does the caller pay, in total, for one run?**
Seller price plus every metered step, one line each in the trace. The catalog page shows both:
"seller $0.01 + steps from $0.003".

**3. When is the seller paid: on success only?**
On a successful run only. A failed run pays the seller nothing; the caller pays only the steps that
completed.

**4. Does treg take a share of the seller's price?**
No share in the MVP. 100% of the seller's price lands as `earned` credit on the seller's team. A
platform fee is a later decision, announced before it starts.

**5. Can earned credit pay for the seller's own steps?**
Yes. `earned` is ordinary spendable balance. It is consumed after promotional and before purchased.

Plain words, as the owner asked: when someone runs Mitchel's tool and pays his price, that money
lands on Mitchel's treg balance as credit, in a block marked `earned`. It is the same kind of
balance as the $1 welcome credit or a $10 top-up. He can spend it on any treg call right away,
including the catalog steps inside his own recipes. The only thing he cannot do yet is take it out
as cash. The spend order is promotional first, earned second, purchased last.

**6. Who pays the check run on create?**
The maker's own balance, at the normal step prices, seller price not charged. Creating a tool you
cannot afford to run once is refused with the amount.

**7. Does the seller's price go through reserve-and-settle like a step?**
Yes. One extra hold `{run}:price` opened at run start, settled to the seller on success, released
on failure. Same primitives, no new money path.

**8. Can the maker change the price of a live tool?**
Yes, any time; it applies to runs after the change. The trace stamps the price paid. Version
numbers do not change for a price edit.

**9. Is there a ceiling for the caller on a hub tool?**
Yes, the existing header `X-Treg-Run-Max-Cost`, default $1.00, covering seller price plus steps.
The run stops before the step that would pass it.

**10. Does the seller see who called and what they paid?**
A per-tool earnings view: runs, success count, earned total, by day. Caller identity is not shown;
only counts and amounts.

---

## Round 4 — the maker's road: create, share, MCP

**1. How does a maker create a tool over the API?**
`POST /hub/tools` with the same four files as fields: `manifest`, `script`, `check`, `readme`. Same
shape as the folder, one request. The CLI and MCP send the same body.

**2. What MCP verbs does the maker's agent get?**
Three new tools on `/mcp/`: `hub_create` (the request above, runs the check, returns the live id or
the failure), `hub_update` (new version), `hub_mine` (list, health, earnings). Calling stays the
existing `call`.

**3. Does a caller's agent need anything new to use a hub tool?**
Nothing new. `call("mitchel.leads-db", {...})` works. Unlisted tools do not appear in
`catalog_search`; `catalog_get` on a known id returns them.

**4. What does "share the URL" mean exactly?**
One public page per tool: `treg.to/hub/mitchel.leads-db`. It shows the readme, inputs, price,
health, and the exact `call` line. Reading it needs no sign-in; calling it needs a token and
balance.

**5. Who may call an unlisted tool?**
Anyone with a treg token and balance, if they know the id. Unlisted means not in search. A
private-to-my-team switch is backlog.

**6. What is the handle in the id?**
The team slug. A tool belongs to a team, not a person; the slug is already unique and already in
URLs.

**7. May a hub tool name a hub tool in `uses`?**
Still no. `uses` accepts catalog ids and the maker's own tools only. Depth one.

**8. How does the maker update a live tool?**
`hub_update` runs the check and, on pass, publishes version N+1; the newest serves by default;
`id@N` pins. Old versions stay callable for 30 days after a newer one exists.

**9. Does the readme have a required shape?**
Free markdown, 4,000 characters max, plus the manifest's `summary` (200 characters) shown in every
list. The summary is what an agent reads first.

**10. What does the caller see in a 424 step failure of a hub tool?**
The step name, the tool's own name, the status, never the upstream URL or the error body. The
maker sees the full error in their run log.

---

## Round 5 — the interfaces

**1. Which interface is the primary one?**
The agent over MCP. The CLI mirrors it one-to-one. The dashboard shows and edits what the agent
made. Every screen has an agent equivalent.

**2. What is the CLI command family?**
`treg hub init <name>` (scaffold four files), `treg hub run . --input k=v` (run locally against the
real API), `treg hub publish .` (create or update), `treg hub ls`, `treg hub earnings <id>`,
`treg hub logs <id>`.

**3. Does the dashboard get a builder, or only a viewer, in the MVP?**
A viewer and light edits only. A Hub page lists my tools; each opens a page with readme, inputs,
price (editable), health, earnings, run log. No script editor.

**4. How does the maker's agent get taught the hub?**
A new section in `skill.md` and `llms.txt`, added in the same PR that ships the runner. It teaches:
init, the four files, `ctx.call`, the check, publish, price, the share URL.

The owner added: the section must also say: if you use a new credential that is not yet in the
system, do not hard-code it. First use another tool to create it, then use it.

**5. What does the agent-facing setup look like end to end?**
The agent registers the Supabase tool, writes the four files, runs `hub_create`, reads the check
result, and returns the share URL and the `call` line. Five agent actions, no person.

**6. Where does the caller find a hub tool?**
The share URL, a `catalog_get` by id, and `treg catalog get <id>`. Listed tools appear in search
with a `HUB` mark and the seller's price beside the step price.

**7. What does the public tool page show and hide?**
Shows summary, readme, inputs table, output fields, price line, health dot, version, the `call`
example, and the run trace of the check. Hides the script, the maker's tools, and every key.

**8. Does the run page exist in the MVP?**
Yes, for the caller's own runs: `/app/runs/<run_id>` with the trace and costs. Polling while
running is backlog; the MVP shows finished runs.

**9. What does the maker's earnings view show?**
Per tool and per day: runs, successes, failures, earned total. One table, 90 days, CSV download.
No caller identity.

**10. What does the maker see when a run fails?**
The run log page with `ctx.log` lines, the failing step's status and error body, and the inputs the
caller sent, with `secret` inputs masked.

---

## The backlog, in the order it was named

1. Templates for more data providers (the data tool as a template system).
2. Withdrawal of earned credit as cash.
3. The listing road: pull request, verification, the three catalog states in search.
4. A private-to-my-team switch on a hub tool.
5. A platform fee on the seller's price (announced before it starts).
6. A script editor in the dashboard.
7. Live polling of a run in progress on the run page.
8. Billing the script's own compute.
9. From the 2026-09-08 meeting: Crawl4AI as a built-in function inside scripts; the benchmark and
   auditor fed by Tim's agent-feedback tool; a job queue, recipe-calls-recipe, step retry, live
   progress on the API, and a team's own sandbox (carried from `docs/RECIPES-DECISIONS.md`).
