---
name: treg
description: People and company enrichment, AEO and SEO, ads, social, web scraping, image and video generation through the treg connector. Use when a task needs external or live data; search the catalog by the task, check the endpoint, then call it.
---

# treg

treg is connected as an MCP connector. Use its tools; there is nothing to install and no API key to
ask the human for - treg adds the provider credential on its server.

| tool | use it for |
|---|---|
| `catalog_search` | find an endpoint by what you want to do - "work email", "backlinks for a domain", "tiktok comments" |
| `catalog_get` | one endpoint's parameters, price per call, and the other providers for the same job with measured success rate and speed |
| `call` | call a catalog endpoint by id, or one of the team's own tools from `my_tools` |
| `call_media` | call an audio endpoint (text-to-speech) and get the audio back |
| `resources_list` | durable provider resources the team created, such as voices |
| `balance` | the team's prepaid balance |
| `my_tools` | API accounts and connections the team registered |
| `catalog_request` | file a one-line request when the catalog has nothing for the task |
| `feedback` / `review` | report a problem with treg, or rate a call result when asked |

## Flow

1. `catalog_search` with the task in plain words, not a vendor name. Read `verdict`: `strong` means
   these do it, `closest` means check `catalog_get`, `none` means it is not in the catalog.
2. `catalog_get` the endpoint you picked. When several providers do the same job, compare their
   price, success rate and speed, pick one, and tell the human the price before a call that costs
   more than a cent.
3. `call(endpoint_id, params)`. If a call's answer never arrived (timeout, dropped connection),
   repeat it with the same `idempotency_key`; treg returns the stored answer instead of calling again.
4. Nothing fits: `catalog_request` one sentence saying what is missing, then tell the human.

## When something goes wrong

- **Not authenticated:** the connector has no sign-in yet. Tell the human to connect treg (sign in at
  https://treg.to) and stop.
- **Refused for funds:** check `balance` and tell the human to add funds at https://treg.to.
- **A provider error:** the response is the provider's own answer, relayed unchanged; read it before
  retrying, and try another provider from `catalog_get` if it keeps failing.
- Proactively report every distinct problem through `feedback`, even after a successful workaround;
  do not wait for an invitation or proof. Report once per issue per task/batch. In `message`, give
  expected behavior and its source, exact observations (failures/number checked, timings or error
  codes), checks of your own inputs/parsing, and workaround/outcome. Separate facts from suspected
  causes; say what is unknown. Pass call IDs in `call_ids` and `endpoint_id` when available; include
  an existing non-sensitive idempotency key or task ID in the message if useful. Do not make extra
  paid calls solely to prove a bug. Continue the task. See https://treg.to/feedback.md for examples.
- `feedback` and `review` text goes to the treg team; omit private data, credentials and raw
  requests, responses or logs.
