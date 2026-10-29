# Capability choice — the agent decides, treg makes the decision factual

**Status:** shipped; kept as the design record. Supersedes the "treg routes for you" reading of landing
pillar 2. Measured comparison is `docs/context/architecture/catalog.md` (Choosing between providers);
the two cases where treg does choose, routed endpoints and overflow, are in the same fragment
(Routing) and `docs/context/architecture/proxy-model.md` (Overflow).

## The promise, and what it actually needs

The landing says: *"Your agent asks for the task, not the tool… treg knows the ladder for each
scenario and routes every call to the best one that's up"* — with *failover built in*, *compared
live*, *pin a vendor any time*.

Read one way, that means **treg** picks the vendor and silently switches on failure. We measured what
that would cost:

| | |
|---|---|
| capabilities served by 2+ distinct providers treg holds keys for | **171** |
| ...where the request shape is already identical across providers | **5** |
| price spread within one capability | up to **261×** (`companies.enrich`: $0.0015 → $0.39) |

So 166 of 171 would need a **translation layer** — a canonical input schema per capability, plus a
per-endpoint mapping — before treg could re-send a call elsewhere. `capabilities.yaml` is a taxonomy
(`id: description`), so none of that data exists.

And translation is not the real problem. **These endpoints ask different questions:**

| Endpoint | Needs |
|---|---|
| `hunter.people.email.find` | `domain` + `first_name`/`last_name` (GET, query) |
| `leadmagic.people.email.find` | `first_name`, `company_name`, … (POST, body) |
| `leadmagic.x.b2b-profile-email` | a **`profile_url`** — different information entirely |

A router choosing on price would pick the third for a caller holding a name and a domain, and fail.

## The decision: the agent chooses; treg publishes the facts

The agent knows **which inputs it actually has** — treg cannot. It also needs no translation layer,
because it reads the chosen endpoint's own parameters from the catalog and builds that request. So
the hard part disappears by giving the job to the party that already has the context.

This also keeps the founding rule intact: treg **relays, never models**. A router would have been the
first feature to break it.

What treg owes in exchange is the part **only treg can do**: it sees every call, from every tenant,
so it can say which endpoints actually work, how fast, and what they really charge. That is what
turns "compared live" from copy into fact.

## Three pieces

### 1. Publish observed reality per endpoint

**The cold start is the hard part, and money is not what solves it.** On day one `CallRecord` is
empty for these endpoints, so every cell would read `—`. Three sources fill it, and they must stay
visually distinct — the value of a measurement is destroyed by averaging it with a claim:

| Source | Covers | Cost | What it honestly says |
|---|---|---|---|
| the catalog's own `verified:` stamp | **1,380 of 1,810** eligible endpoints (76%), all stamped within the last month | **free, today** | "we ran it by hand and it answered, N days ago" |
| a vendor status page | ~40 providers (3/3 of the ones we pay have one; ScrapeCreators publishes it **per endpoint**) | free, one-off research | "the vendor says it is up right now" |
| our own `CallRecord` | grows with real use | see below | **"here is what actually happened"** |

Only the third is measurement. So the `LAST OK` column prints a bare age when a real call produced
it and a **`✓` age** when it came from the stamp — one glance separates evidence from a dated claim.

**The seeding sweep is bounded by ACCOUNTS, not by dollars.** `scripts/catalog_verify.py` already
sends each endpoint's `test_request` and checks the status; it just discards the timing. But it can
only call providers we hold a key for, and production holds **three**:

| | endpoints callable | 5 calls each | capabilities we could actually COMPARE |
|---|---|---|---|
| keys we have today (tikhub, dataforseo, scrapecreators) | 351 | **$7.50** | **68 of 157** |
| if all 16 key slots were filled | 526 | $26.90 | 157 of 157 |

$7.50 is nothing; the constraint is that comparing nine email-lookup providers needs a paid account
with nine of them. And a comparison needs two sides: for the 89 capabilities where we hold exactly
one provider's key, a sweep produces one number beside two dashes — an advert for the only vendor we
pay, not a choice. **Which providers treg buys accounts with is therefore a business decision, and it
is the same list that decides what treg can serve keylessly at all.**

`CallRecord` **already records** everything needed and has since the marketplace shipped:
`endpoint_id`, `status_code`, `duration_ms`, `response_bytes`, `cost_observed_micro`,
`cost_estimated_micro`, `params_hash`, `created_at`. Nothing new to collect — only to aggregate.

Per endpoint, over a rolling window: **success rate**, **p50/p95 latency**, **observed-vs-estimated
cost drift**, **last time it answered**, and **sample size**. Sample size is not decoration: a 100%
success rate over two calls must not outrank 97% over four hundred, and a reader has to be able to
see that.

Surfaced where the choice is already made — `treg catalog get <id>` lists a capability's alternatives
today, so the numbers belong on those rows — and over the API for agents.

Follows `reconcile.py`'s precedent: query-time, read-only, admin-scale windows, aggregation in Python
where the provenance lives in a JSON column.

### 2. Teach the decision procedure

In `skill.md` and `/llms.txt`, as an ordered rule an agent can follow:

1. **Match the inputs you have.** An endpoint wanting a `profile_url` is not a substitute when you
   hold a name and a domain, whatever it costs.
2. Then prefer **verified** over unverified, then **success rate** (with sample size in view), then
   **price**.
3. On **429 / 5xx / timeout**, try the next provider — you know its parameters, so you can build its
   request.
4. **Never retry a 4xx you caused.** Fixing the parameter is the fix; retrying elsewhere burns money
   on N providers for one mistake.
5. **Tell the human the price before spending** the team's balance.

### 3. Bound it with team policy

An org can **pin** a provider for a capability, or **deny** one, so an agent's freedom stops where
the team's decision starts. Pinning is also what makes the choice deterministic for CI. Reuses the
existing deny-rule machinery rather than inventing a second policy engine.

## Deliberately later

**Automatic selection (`--auto`)** for callers that are not agents — a `curl`, a CI job, a human.
Once (3) exists, a pin already does this for the common case. Cross-provider *translation* stays out
of scope until a capability's canonical schema is worth writing by hand, and even then only for the
handful with the widest price spreads.

## Copy this changes

The landing's *"✓ your agent never picked a vendor — treg routed it"* becomes false under this
design. The truthful line is closer to *"your agent picked from live data — price, success rate,
latency — instead of guessing."* Until (1) and (2) ship, the wording already in `llms.txt`,
`skill.md` and the charter stands: **treg compares, choosing is the caller's, there is no automatic
failover.**
