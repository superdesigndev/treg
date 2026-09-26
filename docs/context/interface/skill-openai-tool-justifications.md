---
title: Tool justifications for the ChatGPT plugin submission
status: reference
sources: []
related:
  - interface/skill.md
  - interface/skill-openai-submission.md
---

# Tool justifications for the ChatGPT plugin submission

One block per form field. Copy the paragraph under each heading exactly as it is.

Taken from what the server actually declares (`tools/list` on production), not from what the code
intends. Those agreed when this was written, and the claim under review is what the server sends.

Four of the five tools are read-only and closed-world. `call` is none of those, and its
justifications are the ones a reviewer should read closely, because they explain a deliberately
cautious labelling rather than a convenient one.


## catalog_search

### Read Only is True

It searches treg's own catalog of API endpoints and returns names, descriptions and prices. It creates nothing, changes nothing, and spends nothing. Running it a hundred times leaves the account exactly as it was.

### Open World is False

It reads only treg's internal catalog, which is a fixed dataset we curate and ship. It does not reach any third-party API, follow any URL, or accept a host from the caller. Nothing outside our own service is contacted.

### Destructive is False

It performs no write of any kind. There is nothing for it to delete, overwrite or cancel, because the catalog is read-only data and the tool has no write path to it.


## catalog_get

### Read Only is True

It returns the full detail of one catalog entry: parameters, exact price per call, documentation links, and the reliability we have observed. It is a lookup with no side effect, and no money moves.

### Open World is False

It reads the same fixed internal catalog as catalog_search. The caller supplies an endpoint ID we already publish, not a URL, so there is no way to point it at an arbitrary host.

### Destructive is False

It is a read-only lookup. It has no write path and cannot remove or modify anything.


## call

These four are the deliberately cautious ones. treg relays to a third-party API on the user's behalf.
We do not model what that API does, and we would rather over-warn than let a client assume safety we
cannot promise.

### Read Only is False

It performs a real API call to a third-party provider and can spend money from the team's prepaid balance. Two things change: the upstream provider does whatever that endpoint does, and the balance is debited. Neither is a read.

### Open World is True

This is the tool's whole purpose. It calls external APIs across roughly 2,600 catalogued endpoints from many independent providers, and it can also call an endpoint the user's own team registered at any URL. The set of systems it can reach is open by design and is not bounded by us.

### Destructive is True

We label it destructive because treg cannot see inside the endpoint being called. The catalog includes endpoints that create, update, cancel and delete on third-party systems, and treg relays whatever the caller asks for. Claiming otherwise would be a guess presented as a fact, so we take the cautious label and let the client ask the user first. treg itself never deletes user data. The risk being flagged is the upstream API's, which is exactly the risk the caller cannot otherwise see.

### Idempotent is False

Repeating a call can charge again and can repeat a side effect upstream. Some catalog endpoints are plain lookups, but many are not, and treg has no reliable way to tell them apart, so it does not claim safety it cannot verify.


## balance

### Read Only is True

It reports the team's prepaid balance and any spend currently in flight. It reads the ledger and writes nothing. It cannot add, move or refund funds.

### Open World is False

It reads treg's own database only. No third-party system is contacted.

### Destructive is False

Reporting a number changes nothing. There is no write path from this tool to the ledger.


## my_tools

### Read Only is True

It lists the API tools the user's team has already registered with treg, so the model knows what it may call. It is a directory listing. Nothing is created, changed or removed, and no credential is revealed, only names and base URLs.

### Open World is False

It reads treg's own database only, scoped to the team the user chose when authorizing. No external system is contacted.

### Destructive is False

It is a read-only listing with no write path. It cannot unregister a tool or alter a credential.
