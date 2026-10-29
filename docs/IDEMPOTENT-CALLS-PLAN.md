# Idempotent calls — a retry must not bill twice

**Status:** shipped; kept as the design record. Caller `Idempotency-Key` replay is described in
`docs/context/interface/api.md` (`Idempotency-Key` on `/call/`) and `docs/context/architecture/money.md`
(Idempotency and retries); the per-org re-scoping on treg's shared key lives in
`src/treg/infra/upstream/relay.py` (`docs/context/architecture/proxy-model.md`).

## The question that prompted it

> "how does result pricing handle retries, agents need idempotent billing before this works"

Asked publicly, and correct. Agents retry far more than humans do, so this is not an edge case for
treg. It is the main traffic pattern.

## What already works, so we do not rebuild it

Most retries are already free, and this matters because it tells us how narrow the real gap is.

| Outcome | Billed? |
|---|---|
| 5xx, 3xx, network error, timeout upstream | never |
| 4xx | only under `per_call`, where the provider bills for accepting the request |
| 2xx | yes |

So the common case, where an agent retries *because something failed*, costs nothing extra today.
`_platform_billable` already draws that line.

Result pricing is also settled on the provider's own reported charge rather than our estimate
(`_observed_cost_micro`), so a Hunter lookup that finds nothing is free, and the ledger records the
real number. 1,362 endpoints are `per_success` and 120 are `per_result`, so this is most of the
catalog.

## The actual gap

One case, and only one:

1. treg calls the provider
2. the provider succeeds, and bills us
3. the response is lost on the way back to the agent, or the agent's own client times out
4. the agent retries
5. treg calls the provider again and bills again

From treg's side those are two successful calls with nothing linking them. The money is real: we pay
the provider twice and charge the team twice.

`ledger.topup` already has an idempotency key for exactly this reason (Stripe redelivers webhooks).
`/call/` has none.

## Not-charging-twice is not enough

The cheap version is to remember that a key was already billed and skip the second charge. It is
wrong, and it is worth being explicit about why: treg would still make the upstream call, so **we
still pay the provider**. We would be moving the double cost from the customer onto ourselves rather
than removing it.

Real idempotency means the second request never reaches the provider. That requires storing the first
response and replaying it, which is the Stripe model and the only version that actually solves the
problem.

## Design

### The key comes from the client, or nothing happens

`Idempotency-Key` on `/call/`. No key means today's behaviour exactly: no storage, no lookup, no
change. This is deliberate. A server-invented key (hashing the URL and body, say) would silently
collapse two calls a caller genuinely meant to make twice, and "post this twice" is a legitimate
thing to ask of an API.

### Scope: metered calls only

A team calling on its **own** key is their bill and their relationship with the provider. treg has no
business caching that, and storing those responses would mean holding other people's data for a
reason that benefits nobody.

### Only successful, billed responses are stored

Failures are already free, so there is nothing to protect. This is what keeps storage bounded: we
keep bodies only for calls that actually cost money.

### A 24 hour window

Retries happen within seconds. A day is generous, conventional, and easy to reason about. A row past
its window is simply absent and the call proceeds normally.

### The table

`IdempotentCall`: `(org_id, key)` unique, plus `endpoint_id`, the request fingerprint, the stored
status and body, `charged_micro`, and `expires_at`.

`(org_id, key)` and not key alone: keys are client-chosen, two teams will collide eventually, and a
collision across a tenant boundary would serve one team's data to another. That is the one failure
here that would be a security incident rather than a billing bug.

### The request fingerprint guards against key reuse

A client that reuses a key for a *different* request has made a mistake, and silently returning the
old response would hide it. Store a hash of (endpoint, method, body/query) and compare. On mismatch,
refuse with `422` and say the key was already used for a different request. Stripe does the same, and
the loud failure is the useful one.

### Concurrency: the database decides

Two retries can arrive at once, and both can miss the lookup. The `(org_id, key)` unique constraint
is what makes that safe: insert a **placeholder row first**, and the loser of that race waits for the
winner rather than making a second upstream call.

This is the same reasoning as the conditional UPDATE in `ledger.reserve` and the deleted-before-
validation authorization code: where two paths can read before either writes, the database has to be
the arbiter. A check-then-act in Python would leave exactly the window this feature exists to close.

### What the caller sees

A replayed response carries a header saying so, along with the original charge. A retry that silently
returned a cached body would be indistinguishable from a fresh call, and an agent reporting cost to a
human should be able to say "this was replayed, it cost nothing extra".

## Order

1. The table, and `(org_id, key)` uniqueness. No behaviour yet.
2. Lookup and replay on the read path, with the fingerprint check. Storage still off.
3. Store on the write path, for metered successes only.
4. The placeholder row and the concurrent-retry race.
5. Expiry, and a reaper for rows past their window.
6. **Stop and prove it**: fire the same key twice at a real endpoint and confirm the provider is
   called once, the ledger moves once, and both callers get the same body.

## What could go wrong, and where I would look first

- **Cross-tenant key collision.** Covered by the compound key, and it is the assertion I would write
  first, because it is the only failure here that leaks data rather than money.
- **Storing bodies we should not.** Metered-only and 24 hours are the limits. Anything that widens
  either should be argued for explicitly.
- **A replay after the price changed.** The stored `charged_micro` is what was actually taken; a
  replay charges nothing, so this is consistent by construction. Worth a test anyway.
- **Size.** SERP and scrape responses are not small. If storage becomes a problem the answer is a
  size ceiling above which we do not store rather than a shorter window, because a call too large to
  cache is exactly the one whose retry is most likely to have timed out.
- **Scope creep into a general cache.** This is a retry guard, not a response cache. It must never
  serve a request that did not carry the same key, or it stops being a correctness feature and starts
  being stale data.

## Honest sizing

Smaller than the OAuth work: one table, one lookup, one store, one race. The care goes into the
tenant boundary and the concurrency, not the volume of code. The existing behaviour stays untouched
for anyone who does not send a key, which keeps the blast area small.
