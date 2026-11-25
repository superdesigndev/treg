---
name: treg
description: Search and call treg's catalog of external data APIs - SEO and search results, backlinks, social profiles, company and business-contact enrichment, ad libraries and web data - through the treg connector. Use when a task needs live data from an outside API; search by the task, check the price, then call.
---

# treg

This plugin connects Claude to treg's MCP server. treg holds the provider credentials on its server,
so the human never pastes an API key here. There is nothing to install.

If the treg tools are not visible, or a tool answers "not authenticated", the human has not signed
in yet. Tell them to connect treg in the app's connector or MCP settings (in Claude Code: `/mcp`,
pick treg, Authenticate), then stop.

## Tools

| tool | use it for |
|---|---|
| `catalog_search` | find endpoints by the task in plain words, such as "backlinks for a domain" |
| `catalog_get` | one endpoint's parameters, provider documentation, price and measured reliability, plus other providers for the same job |
| `catalog_call_read` | call an endpoint whose documented method is GET, HEAD or OPTIONS |
| `catalog_call_write` | call an endpoint whose documented method is POST, PUT, PATCH or DELETE |
| `balance` | the team's prepaid balance and money held for calls in progress |
| `catalog_request` | record a request when the catalog has nothing for the task (free) |

## Flow

1. `catalog_search` with the task in plain words, not a vendor name. Read `verdict`: `strong` means
   these endpoints do the task, `closest` means check them with `catalog_get`, `none` means the
   catalog has no tool for it.
2. `catalog_get` the endpoint you picked. When several providers do the same job, compare price,
   success rate and speed, and pick one that takes the inputs you actually have.
3. Call it with `catalog_call_read` or `catalog_call_write`, matching the method `catalog_get` shows.
4. Nothing fits: `catalog_request` one sentence about what is missing, then tell the human.

## Money: the human decides

Calls are paid from the team's prepaid treg balance at the price `catalog_get` shows. Most cost a
fraction of a cent.

- Before the first paid call in a task, tell the human the price per call and the expected total,
  and get their agreement. Agree on a budget once for the task, not for each call.
- Keep a running total of each answer's `cost_usd`, and stop and ask before you go over the budget.
- Before a loop over many inputs, run one or two calls first and check the result.
- Do not try to add funds. If a call is refused for funds, check `balance` and tell the human; a
  team admin adds funds in the treg dashboard.

## Write calls

`catalog_call_write` can create, change or delete data at the provider. Before each write call,
tell the human what it will change and wait for their approval. Many search endpoints use POST only
to send a query; `catalog_get` says what an endpoint does.

## When something goes wrong

- **A provider error:** the answer is the provider's own, relayed unchanged. Read it and fix the
  request. Do not repeat a 4xx with other providers: it is almost always the request.
- **A 429, 5xx or timeout:** try another provider from `catalog_get`, and tell the human which.
- **An answer that never arrived** (timeout, dropped connection): the provider may have answered and
  been paid. Repeat the call with the same `idempotency_key` to get the stored answer without paying
  again. Use a new key, or none, for new work.
- An error from treg itself says what to do in its `message`. Tell the human.

## Personal data

Some endpoints return data about people, such as business contact details. Use them only for the
human's stated task, keep the results in this conversation, and do not send them anywhere else.
