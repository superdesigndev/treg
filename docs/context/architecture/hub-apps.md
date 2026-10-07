---
title: Hub apps — a hub tool's web page
status: building; behind `hub_apps_enabled` (TREG_HUB_APPS_ENABLED) and the hub's own flag
sources:
  - src/treg/domain/hub/apps.py
  - src/treg/application/hub/apps.py
  - src/treg/routers/hub_apps.py
  - src/treg/alembic/versions/0065_hub_app.py
  - src/treg/application/call/service.py
  - src/treg/routers/call.py
  - frontend/src/apps/AppPage.vue
  - frontend/src/standalone/form.ts
  - frontend/src/standalone/render.ts
  - frontend/apps.html
  - tests/test_hub_apps.py
related:
  - architecture/hub.md
  - architecture/vibe-it.md
  - architecture/money.md
---

# Hub apps

A **hub app** is an optional web page for one hub tool, at `/apps/<team slug>/<name>`: a form built
from the tool's inputs, the result, and the visitor's own runs. It is a surface on a hub tool, never
a second kind of tool: a run from the page is the same `/call/<tool_id>` run as from the CLI or
MCP, paid by the visitor's own team at the same prices (`architecture/hub.md`).

Everything sits behind `hub_apps_enabled` (`TREG_HUB_APPS_ENABLED`, default off) AND the hub's own
gate (`hub_app.enabled_for`: `TREG_HUB_ENABLED`, `TREG_HUB_TEAMS`, `TREG_HUB_USERS`). Off, or off
for the reader, every app route answers 404. `/meta.hub_apps` says whether apps exist here.

## The record (`HubApp`, migration 0065)

One row per hub tool that has an app, keyed by `tool_id`; `application/hub/apps.py` is its only
writer. `name` is the last part of the URL, unique within the team (`uq_hubapp_org_name`); the team
slug in front already carries the identity, so a name is only a clean URL part
(`domain/hub/apps.validate_app_name`: lower-case letters, digits and hyphens, at most 48), defaulting
to the tool's own name. Any live tool of the team may have one, listed or not. `enabled` false takes
the page down and keeps the row, its name and its password. The row goes with the team.

**Links survive a rename.** A renamed app keeps up to `MAX_OLD_NAMES` earlier names (`old_names`,
newest first), and a team that changed its slug keeps the old one (`Org.previous_slug`): the page at
an old path answers 308 to where the app is now, query string kept (`apps.moved`). A current name
always wins: another app of the team may take a freed name, and then the path is that app's. A
deleted team's slug is not reserved ([hub](hub.md#the-makers-road-routershubpy-applicationhub__init__py)).

## The password and the lock

Optional, 8 to 128 characters, stored only as `scrypt$n$r$p$salt$hash` (stdlib `hashlib.scrypt`, a
fresh salt each time, constant-time compare, run off the event loop). Never returned by any route.
`lock_version` moves on every set or clear, which signs everyone out of the app.

**The lock applies only while the app is on** (`apps.locked`). With the app on and a password set:
- the page shows only the name and a password prompt until unlocked; `POST .../unlock` sets a
  signed cookie (`treg_app_<hash>`, httponly, scoped to the app's own path, a few hours) carrying the
  tool and the password version;
- **the tool itself**: another team's `/call/<tool_id>` must carry `X-Treg-Tool-Password`, else
  403 `hub_tool_locked` (`apps.call_lock`, read on the call road in `application/call/service.py`
  right after the hub id resolves; a read, not a write). The app page's run route has already checked
  its cookie and says so with `CallInput.hub_unlocked`, set only by that route, never from a header.
  The header never reaches a step (`runner._DROP_FROM_CHILD`) and, being `x-treg-*`, never upstream;
- the tool leaves catalog search and the capability siblings (`hub_app.locked_ids`); `catalog_get`
  says `password_protected`; the share page says so.

The maker's own team never needs the password. A password this process verified in the last
`VERIFIED_TTL_S` passes at once (remembered as a keyed digest, never the password), so a caller who
knows it is neither slowed by the hash nor counted. Any other try counts against
`TRIES_PER_CLIENT` per app and client and `TRIES_PER_APP` per app (`infra/kv`) before the hash
runs, then 429: guessing is bounded, and so is the hash work a stranger can cause. The call road
reads the app row and releases its database connection before the tries and the hash
(`apps.app_of`, then `apps.call_lock`). Turning the app off lifts the lock: the tool is callable by
id again, as every live hub tool is.

## Routes (`routers/hub_apps.py`)

The maker's (member+ for changes, the caller's own team's tools only):

| route | does |
|---|---|
| `GET /hub/tools/{id}/app` | `{enabled, name, url, password, locked}`; never the hash |
| `PUT /hub/tools/{id}/app` | `{name?}`: turn on (made on first use) or rename; needs a live version |
| `DELETE /hub/tools/{id}/app` | turn off; name and password kept |
| `PUT /hub/tools/{id}/app/password` | `{password}` sets, `{password: null}` clears |

The visitor's:

| route | needs | does |
|---|---|---|
| `GET /apps/{team}/{name}` | - | the page (`noindex`); 404 when off, unknown, or rejected by treg's review; 308 from a name or slug from before a rename |
| `GET /apps/{team}/{name}/contract` | unlock if locked | the public contract: inputs, output fields, price line, health, readme; never the script, `uses` or a key |
| `POST /apps/{team}/{name}/unlock` | same origin | `{password}` |
| `POST /apps/{team}/{name}/run` | signed-in member, unlock, same origin | `run_call_surface` on `<tool_id>`: the one call road; cookies are not forwarded |
| `GET /apps/{team}/{name}/runs[/{run_id}]` | signed-in member, unlock | the visitor's own runs (their team AND their sign-in) from `HubRun`, 30 days |

The routes live in the control role beside `/hub/run`. A signed-out visitor is sent to
`/app?next=/apps/<team>/<name>`; the Dashboard's boot follows `next` back for these pages only
(`/apps/<team>/<name>` and `/vibe-it`), so the parameter cannot become an open redirect.

## The page (`frontend/src/apps/`, `frontend/src/standalone/`)

A standalone Vite entry beside the Dashboard (`apps.html`, served by `routers/web.page_entry`), on
the redesign's light system without the Dashboard's shell. `form.ts` maps each input type to a
control (`int`/`float` bounded, `bool` a toggle, `list` one per line or JSON, `object` JSON, `secret`
masked and never pre-filled) and builds the run body. `render.ts` turns the answer into blocks by
shape: short values side by side as tiles, URLs as links (http(s) only), image URLs as images, lists
of objects as sortable tables with CSV export (a wide table scrolls sideways in its card rather than
squeezing its columns; long cells clamp to a few lines until clicked; links show as host and path), nested objects as sections, deeper ones as JSON;
copy and download JSON for the whole answer, and Expand to read it over the whole window. Everything renders as text; nothing is maker HTML.
The visitor picks the paying team; a person with no team is told to make one.

## The maker's surfaces

The Dashboard's Hub view has an **App** tab (on/off, name, URL, password set/change/remove). The
CLI: `treg hub app on|off|password|status <id>` (the password at a hidden prompt or from
`TREG_TOOL_PASSWORD`) and `treg call <id> --tool-password`. MCP (`/mcp/` only): `hub_app` turns the
page on or off and renames it, listed only while apps are on, and never sets a password. MCP has no
way to send `X-Treg-Tool-Password`, so a locked tool is not callable over MCP by another team: they
call it from the CLI with `--tool-password`, over HTTP with the header, or on its app page. The agent
files carry the apps text in `<!--hubapps-->` blocks inside the hub blocks, stripped unless apps are on.
