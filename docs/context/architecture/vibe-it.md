---
title: Vibe-it — build a hub tool in conversation
status: building; behind `vibe_enabled` (TREG_VIBE_ENABLED) and the hub's own flag
sources:
  - src/treg/application/vibe/__init__.py
  - src/treg/application/vibe/agent.py
  - src/treg/application/vibe/status.py
  - src/treg/routers/vibe.py
  - src/treg/infra/llm.py
  - src/treg/alembic/versions/0066_vibe.py
  - frontend/src/vibe/VibePage.vue
  - frontend/src/vibe/FilesPanel.vue
  - frontend/src/vibe/AskCard.vue
  - frontend/src/vibe/EventCard.vue
  - frontend/src/vibe/StepCard.vue
  - frontend/src/vibe/markdown.ts
  - frontend/vibe.html
  - tests/test_vibe.py
related:
  - architecture/hub.md
  - architecture/hub-apps.md
  - architecture/find.md
---

# Vibe-it

`/vibe-it` is a chat page where a signed-in maker describes a tool in plain words and treg's agent
builds it with them as a hub tool: it finds the catalog tools for each step, proposes them with
their prices, writes the files, and asks the maker to test, publish and turn on an app
([hub apps](hub-apps.md)). It is a second way to make a hub tool, never a second kind: everything it
does goes through the hub's own routes. A conversation can also start from a tool the team already
published, from any surface, to change it, or continue a copy of an unpublished draft from another
conversation. Two conversations may publish the same tool; the last publish is the next version.

Behind `vibe_enabled` (`TREG_VIBE_ENABLED`, default off) AND the hub's gate (`hub_app.enabled_for`:
the flag and `TREG_HUB_TEAMS` / `TREG_HUB_USERS`). `/meta.vibe` says whether it exists here.
Browser only: a request carrying `X-Treg-Token`, or no session cookie, is 403 `vibe_browser_only`,
because the model budget is a person's and an agent already has the hub's routes.

## The agent (`application/vibe/agent.py`)

One maker message runs the loop: the model answers through the AI gateway, streamed
(`infra/llm.chat_stream`, OpenAI-style function tools; `vibe_model`, else `vibe_fallback_model`
when the first answers nothing) until it answers in text or asks the maker for something, at most
`MAX_TURNS` model turns and `MAX_TOOL_CALLS` tool calls per message. Every turn and tool step is
stored as it happens. The system prompt carries the hub section of `src/treg/web/skill.md`
verbatim, so this agent and every installed agent read the same rules.

Its tools are existing roads, each an in-process request to the running application with the
maker's own session cookie and team header (the `run_check` pattern), so it can do nothing the
maker could not:

| tool | does |
|---|---|
| `catalog_search` | the [find engine](find.md) in-process (job recall + one judge request): the verdict (`strong`, `closest`, `none`) and the rows; `/catalog/search` when find is not configured, over its limits, or the judge abstains (`keyword`) |
| `catalog_get` | the catalog contract |
| `my_tools` | `GET /tools` (names and base URLs only) and the team's published hub tools |
| `read_files` | one draft file in full (the files sent with each turn are cut at `DRAFT_NOTE_MAX`) |
| `load_draft` | copies one of the maker's unpublished drafts from another conversation (`my_tools` lists them) |
| `load_my_tool` | the team's own hub tool, newest version, as the draft (`domain/hub.as_files`, data.csv included) |
| `write_files` | stores the draft (a new version), then the hub's validator (`hub_app.transient`), which returns the field and rule |
| `test_run`, `publish`, `app_on`, `app_password` | ask the maker (below) |

Every search row and `catalog_get` carry the team's access (`/catalog/endpoints/{id}/access`):
callable now, and if not, why and the fix. A step the team holds no key for, and treg serves no
shared key for, fails every test run with a 404 before any provider; the prompt has the agent stop
and say so.

**The agent asks; the maker decides.** What costs money or changes the team is never done by the
agent: `test_run`, `publish`, `app_on` and `app_password` store one `pending` ask on the
conversation and end the message. The page shows it as a card with buttons; a press is `POST
.../actions`, which runs the hub's own route as the maker (`act`) and stores an **event** message.
The agent reads events as notes on the maker's side of the conversation. One exception, per
conversation: the maker may let the agent test-run without asking (`auto_test`), and then
`test_run` runs and returns the result to the agent. An app password is typed into the card and
goes to `PUT /hub/tools/{id}/app/password` only: it is never stored in a message or shown to the
model. A password press with no password is refused (422 `vibe_password_missing`) unless it says
`clear`: a button meant to set one must never silently remove a lock. A new maker message clears an open ask.

**Stop and regenerate.** `running_since` marks the agent (or a pressed button) at work on a
conversation, one at a time on any instance, under a `run_token`: the run refreshes the mark every
turn and step (`touch_run`), only the run holding the token may clear it, and a mark not refreshed
for `RUN_STALE` belonged to a dead process. A run that finds its mark taken over stops. While the
mark is held the maker's file saves are refused (409): the agent writes over the files as it last
read them, so an edit saved meanwhile would be lost; the page keeps unsaved edits per file and
shows the agent's changes to the others at once. `stop_requested` asks it to stop, read before each turn and step and every `STOP_POLL_S` while the model streams. A
regenerate deletes the agent's turns and steps after the maker's last message, puts the files back
as they were when it was sent, keeps the maker's events, and runs the loop again.

**What one message may cost.** A message stops at `MESSAGE_CAP_MICRO` of model spend and says
so; the maker tells it to go on. The history the model rereads every turn is kept lean: a past
`write_files` call carries only the size of what it wrote (the current files come with every turn),
and a tool result or an attachment from an earlier message is cut to `OLD_RESULT_MAX`.

No database connection is held while the model or a tool call is in flight: the session commits
before each. The loop is its own task with its own session, so a closed tab does not stop it; the
page follows a running conversation by reading it.

## Money

The model is treg's spend, not the team's: each assistant turn's gateway cost (`turn_cost`: or, when
the gateway reports none, its tokens at a deliberately high estimate, the input counted from the
history when no usage came back; a stopped turn and a fallback's failed first attempt are charged;
a request that never reached a model is not) is added to `VibeBudget.spent_micro` in one atomic
update, one row per person, never per team and never restored, and so is a fixed `FIND_COST_MICRO` per find
(the judge reports no cost). Before every model turn the loop checks `vibe_budget_usd` minus
spent; at zero it stops and says the draft is still there. A test run and the publish check are
ordinary hub runs on the team's balance, as from the CLI. None of this is a ledger entry.

## The data (migration 0066; `application/vibe/__init__.py` is the only writer)

`VibeSession` (person, team, title, `draft` = the files, `tool_id` once published or loaded,
`summary`, `pinned`, `auto_test`, `pending`, `running_since`, `run_token`, `stop_requested`), `VibeMessage`
(role user | assistant | tool | event; content; the turn's cost), `VibeDraft` (every version of
the files, numbered per conversation: who made it, the last message then, and the hub version it
became when published; data.csv is kept once, a version with the same data as the one before
keeping its digest and the number of the version that holds it, `files_of`), `VibeBudget`. A team's deletion takes its conversations, their messages
and versions (`cascade_delete_org`); an admin's deletion of a person takes theirs and the budget
row (`forget_user`).

**History (option C).** `treg-worker vibe trim` (cron it daily) finds conversations idle for
`vibe_trim_after_days` (default 30): each keeps its files and their versions, its published tool and
a short summary (the maker's first ask and the agent's last answer, after any earlier summary, no
model call); its messages go. A conversation resumed after a trim is trimmed again when it goes
idle again. A hundred conversations per transaction. The agent reads the summary when the
conversation resumes.

## Routes (`routers/vibe.py`)

| route | does |
|---|---|
| `GET /vibe-it` | the page (`noindex`) |
| `GET /vibe/state` | the budget left and the person's conversations in this team |
| `POST /vibe/sessions` | start one, empty, `{from_tool}` (a 404 when the team has no such tool), or `{from_draft}`: a copy of the files of one of this person's conversations in this team, titled "(copy)"; the two never affect each other |
| `GET` · `PATCH` · `DELETE /vibe/sessions/{id}` | read (messages, files, pending ask); rename, pin, `auto_test`; delete |
| `POST .../messages` | `{text, attachments?}`: streams newline-delimited JSON events (`message`, `delta`, `step`, `draft`, `pending`, `budget`, `error`, `done`); 409 while one runs, 429 past `MESSAGES_PER_MINUTE` |
| `POST .../regenerate` · `.../stop` | answer the last message again; ask the agent to stop |
| `POST .../actions` | `{kind: test \| publish \| app \| password \| skip, ...}`: a button; returns the event |
| `PUT .../draft` · `POST .../validate` | the maker's own edits (`data: ""` removes data.csv); returns `problem` (field, rule) or null |
| `GET .../versions[/{n}]` · `POST .../versions/{n}/restore` | the files' history, one version with the one before it, restore as a new version |
| `GET .../status` | the published tool (version, state, app, runs by others, share page, call line) and the warnings before a test run: a `uses` step the team cannot call, a balance under one run of those steps (`status.py`) |

Changes need member+ and a same-origin request. A conversation is its person's, in its team: anyone
else gets 404. Attachments (a pasted sample, a dropped file) are at most `ATTACH_MAX` characters per
message and reach the model as fenced blocks.

## The page (`frontend/src/vibe/`)

A standalone Vite entry beside the Dashboard (`routers/web.page_entry("vibe")`). Three panels: the
side two close from the header or the keyboard (Cmd/Ctrl+B, Cmd/Ctrl+\\) and resize by dragging
within limits (`layout.ts`), remembered per browser; below a narrow width they become tabs. The
chat follows the newest message while the reader is at the bottom and lets go when they scroll up,
with a "Jump to latest" button (`stickToBottom.ts`). Left: the
conversations, searchable, pinned first, grouped by the tool they built. Middle: a status header
for the tool, then the chat: the agent's replies as Markdown (`markdown.ts`: raw HTML off, links only
to http(s), mail and paths on this site, in a new tab, a Copy button on code), streamed as they come, with each answer's model cost,
Copy and Regenerate; runs of routine steps folded into one line, each tool step a card that opens to its arguments and result, a file change to
its diff with Revert, a test result to the table (`ResultView`); each ask a card with its buttons
and, for a test run, its estimated cost and what to fix first; each event a card with what to do
next ("Looks good, publish", "Fix it", which hands the failure to the agent in plain words,
`errors.ts`; a network failure on the machine, such as DNS answering with a private address, is
said as one and offers no "Fix it"). The message box grows, sends on Enter, attaches a long paste or a dropped file, and
offers a CSV as data.csv. Right: the files in a CodeMirror editor (JSON errors in place, the
validator's refusal on the line it names), their history (restore any, published versions marked),
and Test and Publish buttons that land in the chat like the agent's asks.
