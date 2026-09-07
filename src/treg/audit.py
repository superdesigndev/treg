"""Audit writes — deferred and fire-and-forget (rule #2: never block the proxied response).

`record_call` queues a row and returns immediately; the response streams without waiting. One
writer task per process drains the queue in batches on one connection (a strong reference to it
is held until it finishes, otherwise the event loop may GC a bare create_task). Failures are
swallowed: an audit hiccup must never break a real call. `drain()` flushes pending writes on
shutdown / in tests.

Back-pressure (why this matters): the writer's connection comes from the BACKGROUND pool (db.py),
so a burst here can starve other background work but never real calls. Rows queue in-process, not
as pooled connections: one writer per process takes them off the queue `_BATCH` at a time and lands
each batch in one INSERT round trip, which is what keeps `drain()` deterministic on sqlite, where
all three makers share one engine. Under an extreme burst we DROP audit rows past `_MAX_PENDING`
rather than grow without bound — audit is best-effort; never OOM or wedge the server for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from .infra.db import background_session_maker
from .models import CallRecord, RunRecord, SearchMiss

_pending: set[asyncio.Task] = set()
# ONE writer per process, and it writes in batches. Audit rows are single-row inserts that cost
# milliseconds each, so four concurrent writers bought nothing but four `background` slots - and
# every slot is paid twice (two uvicorn workers) and again at every deploy against the database's
# 103-connection ceiling (ops/deploy.md). One writer draining a queue in batches of `_BATCH` rows
# lands the same rows in fewer round trips and holds one connection.
_MAX_CONCURRENT_WRITES = 1
_MAX_PENDING = 5000          # shed load past this: drop the audit row rather than grow unbounded
_BATCH = 200                 # rows per INSERT round trip; a failed batch retries row by row
_queue: deque[tuple[type, dict]] = deque()

_sem: asyncio.Semaphore | None = None
_sem_loop = None


def _get_sem() -> asyncio.Semaphore:
    """A semaphore bound to the CURRENT running loop (recreated if the loop changed — test isolation)."""
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(_MAX_CONCURRENT_WRITES)
        _sem_loop = loop
    return _sem


def record_call(
    *, org_id: int | None = None, user_email: str, tool_name: str, method: str, path: str,
    status_code: int, client: str = "", refused_by: str | None = None, telemetry: dict | None = None
) -> None:
    """`telemetry` carries the marketplace/spend columns (endpoint_id, provider, credential_tier,
    cost_*_micro, duration_ms, response_bytes, params_hash) — absent for a plain tool call, where they
    stay NULL. It is still fire-and-forget: the money landed in the ledger synchronously, so losing a
    row here costs analytics, not accounting. `refused_by` marks a call TREG refused before anything
    went upstream (see models.CallRecord) — NULL whenever the provider actually answered."""
    _enqueue(CallRecord, dict(
        org_id=org_id, user_email=user_email, tool_name=tool_name,
        method=method, path=path, status_code=status_code, client=client, refused_by=refused_by,
        **_known_fields(CallRecord, telemetry),
    ))


def _known_fields(model, telemetry: dict | None) -> dict:
    """Drop telemetry keys the model has no column for, loudly.

    `telemetry` is splatted straight into the model constructor, so ONE unknown key used to raise
    inside `_write` — where the except swallows it — and the whole row vanished with no trace. That
    is the worst possible failure for an audit table: a telemetry field added a commit before its
    migration would silently delete every row it touched. An unknown key must cost one column, never
    the row.
    """
    if not telemetry:
        return {}
    known = {k: v for k, v in telemetry.items() if k in model.model_fields}
    if len(known) != len(telemetry):
        logging.getLogger("treg.audit").warning(
            "dropping unknown %s telemetry keys %s — is a migration missing?",
            model.__name__, sorted(set(telemetry) - set(known)))
    return known


def record_search_miss(*, query: str, source: str) -> None:
    """A catalog search that matched nothing — logged so the misses can steer ingest (see
    models.SearchMiss). Same contract as every write here: fire-and-forget, and a dropped row
    under load costs a data point, never a search response."""
    _enqueue(SearchMiss, dict(query=query[:300], source=source))


def record_run(
    *, org_id: int | None = None, user_email: str, bundle_name: str, argv: list, exit_code: int,
    duration_ms: int, client: str = ""
) -> None:
    _enqueue(RunRecord, dict(
        org_id=org_id, user_email=user_email, bundle_name=bundle_name,
        argv=argv, exit_code=exit_code, duration_ms=duration_ms, client=client,
    ))


_shed = 0  # audit rows dropped by back-pressure this process; only ever grows


def _schedule(coro) -> None:
    """Run the writer as a tracked task. Shedding happens in `_enqueue`, on the queue: a shed row is
    invisible - the audit table simply has less in it - so for a table whose job is to record what
    happened, "quiet" and "quietly broken" must not look identical. The failure-evidence columns
    ride this same path, so a burst would otherwise silently lose exactly the errors someone would
    go looking for. Logged on the first drop and then every 1,000th."""
    task = asyncio.create_task(coro)
    _pending.add(task)
    task.add_done_callback(_writer_done)


def _writer_done(task: asyncio.Task) -> None:
    """A row enqueued between the writer's last empty-queue check and this callback saw `_pending`
    still occupied and did not start a writer; start one for it here or it waits for the next call."""
    _pending.discard(task)
    if _queue and not _pending:
        _schedule(_flush())


def _enqueue(model, fields: dict) -> None:
    """Queue one row and make sure a writer is running. The shed check is on the QUEUE, which is
    where rows wait now; `_pending` holds at most the one writer task."""
    if len(_queue) >= _MAX_PENDING:
        _shed_one()
        return
    _queue.append((model, fields))
    if not _pending:
        _schedule(_flush())


def _shed_one() -> None:
    global _shed
    _shed += 1
    if _shed == 1 or _shed % 1000 == 0:
        logging.getLogger("treg.audit").error(
            "audit back-pressure: %d row(s) dropped this process (pending at %d)",
            _shed, _MAX_PENDING)


async def _flush() -> None:
    """Drain the queue in batches until it is empty, then exit. One connection for the whole run.

    A batch that fails is retried row by row, so one row the database refuses (a value out of
    range, a constraint) costs that row and not the 199 around it - the failure evidence of a
    burst is exactly what such a burst must not take down with it.
    """
    async with _get_sem():
        while _queue:
            batch = [_queue.popleft() for _ in range(min(_BATCH, len(_queue)))]
            if not await _write_batch(batch):
                for row in batch:
                    await _write_batch([row])


async def _write_batch(rows: list[tuple[type, dict]]) -> bool:
    try:
        async with background_session_maker() as session:
            session.add_all([model(**fields) for model, fields in rows])
            await session.commit()
        return True
    except Exception:  # noqa: BLE001 — audit must never surface into a call's result
        # Swallowed on purpose, but neither silent nor local: the row is lost — that is the
        # contract — and ERROR is what puts that loss in front of someone. At WARNING it stayed
        # in the container's stdout, below the fault handler's threshold, so the only way to
        # learn that audit was dropping rows was to already suspect it and go grep.
        if len(rows) == 1:
            logging.getLogger("treg.audit").error(
                "audit write dropped for %s", rows[0][0].__name__, exc_info=True)
        return False


async def drain() -> None:
    # Loop until quiescent, not a one-shot snapshot: a call finishing DURING shutdown enqueues a new
    # record_call after we'd have gathered, and that audit write would otherwise be dropped.
    #
    # Drain must remove what it gathered ITSELF. A finished task leaves `_pending` through a
    # call_soon'd done-callback — and awaiting a gather whose tasks are all already complete never
    # suspends, so a loop keyed only on the callback spins synchronously forever while that callback
    # (and every timer on the loop) starves. Latent since the first import; a CI Postgres runner hit
    # the window deterministically and wedged whole 15-minute jobs on it.
    #
    # Whatever is still queued once no writer is running is flushed HERE, inline, not through
    # `_schedule`: a drain that depends on scheduling a task to make progress spins forever the
    # moment scheduling is stubbed out (a test kills the pipeline exactly that way).
    while True:
        tasks = list(_pending)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
            _pending.difference_update(tasks)
            continue
        if not _queue:
            return
        await _flush()
