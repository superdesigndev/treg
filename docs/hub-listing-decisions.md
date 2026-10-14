---
title: hub listing and public run log — the decisions
status: settled 2026-09-16, mockup https://claude.ai/artifact/MW5m46BcxTtcrc7ghcNEG7
---

# hub listing and public run log — the decisions

Two additions Jason asked for on 2026-09-15: a way to list a hub tool in the catalog (until now
every hub tool was unlisted by decision), and a public log of the calls a tool has served. Five
decisions, approved by the owner on 2026-09-16 from the mockup.

**1. Listing is a switch on the tool, not a field in recipe.json.**
Default off. The maker flips it with `treg hub list <id>` / `treg hub unlist <id>`,
`PATCH /hub/tools/{id} {"listed": true}`, or the dashboard switch. No version bump, like a price
change. It is a distribution choice, not part of the recipe contract.

**2. A listed tool appears in catalog search; an unlisted one stays callable by id only.**
In the same results, ranked by relevance, no boost, marked `hub` with the maker's team, the price
label and the 30-day ok rate. `catalog_search` over MCP returns it too. Only `live` versions are
ever listed; failed and retired never appear.

**3. The public log shows outcomes, never people or data.**
Per run: time, ok or failed, duration, steps, units (per_unit), the price paid. Never who called,
never the inputs, never the output. The last 20 runs, and runs per day for 30 days.

**4. The maker can switch the public log off.**
Default on. Failed runs are shown too: the page already shows health, and a log that hides
failures is not a log.

**5. The agent page gets the same log.**
`/hub/<id>.md` carries the run table, so an agent can judge a tool before calling it, the same
way it reads the price.

## Round 2 — treg approves the listing (2026-09-24, owner + Jason)

Jason's maker page puts search behind treg's approval. Decided:

**1. `treg hub list` is a request.** The tool enters catalog search only when a superadmin approves
it, in the dashboard's Admin page or `POST /admin/hub/listings/{id}`. A rejection carries a reason
the maker reads; listing again asks again; unlisting withdraws the request or the approval.

**2. An approval belongs to the tool, not to a version.** A new version stays listed, and the
admin can take an approval back (a rejection with a reason). So the state is its own row
(`HubListing`), not a column on each version, which reset on every publish.

**3. Why approval.** At the start the hub serves a few teams; a weak tool in search costs every
caller who picks it. Review keeps search worth trusting until run evidence can do that job.

## Round 3 — capability: the tool beside the providers of its job (2026-09-24, owner + Jason)

**1. The maker names the job; treg approves it.** `"capability"` in recipe.json is a catalog
capability id. It is a proposal: it takes effect with the listing approval, and the admin may
change or clear it.

**2. Beside, never instead.** An approved job puts the tool in `catalog_get`'s siblings for every
provider of that job, and those providers beside it on its own page. treg does not route to it; the
agent compares and picks.

**3. A seed, because a new tool has no runs.** Its success rate starts at 90%, counted as 5 runs,
and runs by other teams move it; after about 20 the seed barely counts. Until then the number is
marked `estimated`. The owner chose a seed over hiding new tools: hidden, a tool never gets the runs
that would prove it.

## Round 4 — every update to a listed tool goes through treg (2026-09-24, owner + Jason)

Jason: a maker could get a tool approved, then publish a version that charges more or returns
worse data. Decided:

**1. Any update to a listed tool waits for review:** a new version and a price change. The approved
version and price keep serving callers, search and the place beside providers until treg approves.
This replaces round 2's "an approval stays across new versions".

**2. The maker can still try it.** A waiting version answers `<id>@N` for the maker's team only.

**3. A rejection keeps the approved version,** marks the new one `rejected`, drops the new price,
and gives the maker the reason.

**4. Unlisted tools stay self-serve.** A tool nobody can find publishes and prices without review.
(Round 5 changes this for a tool treg has approved once: it stays under review after unlisting.)

## Round 5 — what a cheating seller found (2026-09-25, owner, after hub simulation run 2)

A simulated seller tried 14 ways to change what buyers get or pay. Decided:

**1. Once approved, always reviewed.** Approve, unlist, then change the price and the versions
was a way round the review: buyers who kept the id paid up to 50 times more for less. Now unlisting
an approved tool only takes it out of search; its changes still wait, callers keep the approved
version, and listing it again needs no new review. A rejection that takes an approval back keeps
the review too.

**2. Reserved team names.** A team slug is the first half of every hub tool id. Names that read as
treg, as official, or as a catalog provider or platform are refused, for creating, renaming and
publishing, unless a superadmin acts.

**3. An empty answer pays no seller price**, and the price line names the provider-fee limit and
says plainly that a failed run still pays the fees of the steps that ran.

**4. The public price rests on other teams' runs** once there are any; the maker's own free runs
are marked as tests. **5. The contract names the maker's own server hosts** a tool sends inputs to.

Not decided: re-reviewing a tool when the maker's own server changes; the maker is only disclosed.

## Round 6 — the same seller again (2026-09-25, owner, after hub simulation run 3)

Run 2's holes held. New ones, decided:

**1. A rejected tool is not callable by other teams**; its share page answers 410. A tool never
reviewed stays callable by id, so makers can build and share before asking for search.

**2. Names are judged as they read:** lookalike letters, digits and separators do not get round the
reserved names (`trеg-hub`, `tregg`, `hunter-io`, `Hunter.io data`, `apol1o`, `verified-partner`).

**3. No relays:** a team tool may not point at treg itself.

**4. The reviewer reads the code:** the script or steps, a diff for an update, and the own tools'
addresses. check.json may carry several cases. A maker cannot review its own tool.

**5. Routing says who bills a miss:** a routed price no longer claims "you pay exactly the child
that served"; it names the children that bill a miss.

