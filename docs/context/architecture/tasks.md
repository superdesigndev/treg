---
title: Async tasks - `/tasks/{call_id}` follows the task an async call started
status: building
sources:
  - src/treg/application/tasks.py
  - src/treg/routers/tasks.py
  - src/treg/application/asynctasks.py
  - src/treg/alembic/versions/0098_call_task.py
  - tests/test_tasks.py
related:
  - architecture/catalog.md
  - architecture/money.md
  - architecture/proxy-model.md
  - architecture/table.md
  - interface/api.md
  - interface/cli.md
---

# Async tasks

An async catalog call answers with a provider task, not a result ([catalog](catalog.md), "Async
descriptors"). `/tasks/{call_id}` lets a client follow that task by the call id it already holds
(`X-Treg-Call-Id`) while the registry does the reading: the descriptor is interpreted only by
`domain.asynctasks`, server-side, so a client in another language never ports it. The frozen
Python CLI still reads `X-Treg-Async` and polls `/call/`.

## The record

`CallTask` is written by the call runtime when it relays an async answer: a root call (a call ref
with no `:`; a routed child's task is named by its parent) to a catalog endpoint with an async
descriptor, status 2xx. One writer, `application.asynctasks.remember_call_task`, on its own short
session after the answer is read whole, so no connection is held while the provider is talking.

- **treg's key.** The body settlement already read (overflow-served and spooled answers excepted).
- **A team's own key.** The answer is read whole up to `_TASK_ANSWER_MAX` (gzip or deflate decoded
  for reading only) and relayed byte for byte; a larger one streams untouched and gets no record.
- **A routed pending answer** (202). The parent's call id names the child's task: its endpoint,
  descriptor and task id. There is no digest, because that body is treg's own envelope.

A row keeps the team, the caller's tags (the pin snapshot), the endpoint, the descriptor frozen as
it was, the task id and poll URL as `extract_submission` reads them (none when the answer names no
task), the SHA-256 of the bytes relayed, and the time. A failed write is logged; the answer goes
out either way and `/tasks/` will not find it. Team deletion deletes the rows; nothing else does.

## Reading it

Both methods find the row by call id within the caller's team and pin (`pinned_tag_predicates`);
anyone else gets 404 `task_not_found` with `X-Treg-Error: 1`, the answer an unknown id gets.

- **`POST /tasks/{call_id}`**, body the call's answer as received: no provider call. The body must
  hash to the recorded digest (409 `submission_mismatch`), so what is read is what treg relayed. It
  says what the submission is: not JSON, naming no task, already finished
  (`terminal_on_submission`, read as the terminal answer) or a pending task.
- **`GET /tasks/{call_id}`** makes one poll: 409 `task_not_pollable` when the answer named no task,
  and `state: error` `expired` with no poll once the descriptor's `max_age` has passed since the
  call. Otherwise `routers.call.run_call_input` runs the `CallInput` `treg call` would send
  (`GET /call/<poll endpoint>?<param>=<task id>`, or the provider's poll URL) with the caller's
  headers, minus the look's own body and transport headers and its `Idempotency-Key`, asking for
  identity encoding. Gates, credential, hold, settle, audit row and owned-poll settlement are
  exactly a `/call/` poll's; `application.tasks` cannot hold, charge or refund. A poll treg itself
  refuses is audited through `_stamp_call_exit` and reported as the poll's answer. The poll's
  answer is read up to 8 MiB.

## The answer

Every key is always present (`null` when it does not apply), so the shape is stable.

| Key | Meaning |
|---|---|
| `state` | `pending`, `succeeded`, `failed` or `error` |
| `task_id`, `task_shown` | the provider's id, and the same made safe to print (`asynctasks.shown`) |
| `status`, `status_shown` | the descriptor's status word as text (`"None"` when absent), raw and safe to print |
| `warning` | a status the descriptor does not list as progress |
| `billed` | on `failed`: a `billed_failure` word, settled rather than released |
| `response` | the answer read: `source` (`poll` or `submission`), `http_status`, `content_type`, `body` as text, the poll's `call_id`, `cost_micro` |
| `result`, `result_text`, `result_url` | a path-mode result, how it prints, its first URL |
| `fetch_command`, `ttl_note` | a fetch-mode retrieval line, and how long the result lives |
| `resume` | the `treg call` line that polls the task by hand |
| `retry_after`, `max_wait` | seconds before the next look is worth making (the descriptor's `interval`), and the most a waiter waits (`max_age`) |
| `error` | `kind`, `message`, `http_status`, `retryable`, `by` (`provider` or `treg`) |

Error kinds: `submission_not_json`, `submission_rejected` (no task id, or a poll URL off the
allow-list), `result_not_retrievable` (a fetch-mode success without the value to fetch by),
`poll_failed`, `poll_not_json`, `poll_too_large`, `expired`. `Retry-After` (whole seconds) is set
on `pending` and on a `retryable` poll failure (a 5xx); answers are `Cache-Control: no-store`.

`/call/` returns exactly what it returned before (AGENTS.md, non-negotiable 4): the record is read
from the answer, never written into it.
