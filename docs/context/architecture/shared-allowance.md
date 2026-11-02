---
title: Shared allowances on treg's app - a connection's calls that spend one pool
status: shipped
sources:
  - src/treg/application/call/allowance.py
  - src/treg/application/call/service.py
  - src/treg/oauth_providers.py
  - src/treg/domain/capacity/signatures.py
  - src/treg/domain/capacity/marks.py
  - src/treg/domain/capacity/view.py
  - src/treg/config.py
  - tests/test_shared_allowance.py
  - tests/test_call_architecture.py
related:
  - ops/capacity.md
  - architecture/proxy-model.md
  - architecture/auth-secrets.md
---

# Shared allowances on treg's app

Some providers meter a daily allowance on the developer app, not on the account a call acts for.
A connection made through treg's own OAuth client, or carrying treg's own second credential, then
spends a pool every team's connection shares: YouTube's Data API units belong to the Cloud project
behind treg's client, and Google Ads operations to treg's developer token. Without a guard, one
team's scheduled job spends the day for everyone, and every later call is relayed into the same
quota error until the provider resets.

## Which calls draw

`OAuthProvider.shared_allowance` declares the pool. `drawn_by` says which calls spend it:

- `app`: a connection made through treg's client (`Secret.provider` set by the registry connect).
  A connection on the team's own client carries an empty `provider` and is never counted.
- `platform_extra`: a tool carrying treg's own second credential as a `platform_setting` binding
  equal to `extra_credential_setting`. A team that supplies its own value binds a secret instead
  and is never counted. The binding's `account` gives each of treg's credentials its own pool.

`scopes` names a smaller limit inside the pool (YouTube's `search.list` per-day limit): the calls
it covers by upstream path, and the exhaustion answer that names it by body.

`application/call/allowance.draw_for` makes the decision from the tool and the loaded secrets, with
no I/O. It runs at the same point as the paused-provider check, so a catalog call on a connection,
a URL passthrough, MCP and hub steps all pass through it.

## Refuse until the reset

The pool rides the call path's breaker (`domain/capacity/marks.py`, `ops/capacity.md`) under its
own lock keys, `allowance:<service>[#<account>]` and `<that>/<scope>`. The signature table
recognises each provider's spent-allowance answer and its reset: a wait stated in the body
(`wait_regex`; a stated wait no longer than a burst is a burst) or a named reset such as the next
midnight Pacific time. A per-account limit or a short token-wide wait never strikes.

Two signatures lock, as on treg's own keys. A lock lasts to the stated reset, bounded by the
lock row's lifetime (`MAX_LOCK`), instead of the six hours that bound a platform key's lock. While
it holds, a drawing call is refused before it leaves, with a 503 of kind `provider_capacity` and
body error `shared_allowance_exhausted` (`provider`, `resets_at`, a `message` naming the team's own
client or credential as the way around it), not charged, audited `refused_by=capacity`. One call
per process per minute is let through as the probe, never answered from the archive; its 2xx
clears the lock it probed. An archive hit is never recovery evidence.

The admission reads the cached capacity view and may open its own session when the cache is
stale, so the request's session is committed first. Marks are written after the answer, on their
own short session, and never raise.

## Mode

`TREG_SHARED_ALLOWANCE_MODE`:

- `off` (default): nothing is classified, locked or refused.
- `observe`: locks are learned and probed, and a call the lock would refuse is relayed with a
  `capacity` line `allowance_would_refuse`.
- `enforce`: the lock refuses.

The lock covers the time after the provider has already said no. Splitting the pool between
teams before it runs out is separate work.
