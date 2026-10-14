"""Audit writes — deferred and fire-and-forget (rule #2: never block the proxied response).

`record_call` and terminal hit corrections queue their writes and return immediately; the
response streams without waiting. One
writer task per process drains the queue in batches on one connection (a strong reference to it
is held until it finishes, otherwise the event loop may GC a bare create_task). Failures are
swallowed: an audit hiccup must never break a real call. `drain()` flushes pending writes on
shutdown / in tests.

Back-pressure (why this matters): the writer's connection comes from the BACKGROUND pool (db.py),
so a burst here can starve other background work but never real calls. Rows queue in-process, not
as pooled connections: one writer per process takes them off the queue `_BATCH` at a time and lands
inserts in batches, which is what keeps `drain()` deterministic on sqlite, where
all three makers share one engine. Under an extreme burst we DROP audit rows past `_MAX_PENDING`
rather than grow without bound — audit is best-effort; never OOM or wedge the server for it.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque

from sqlalchemy import select, update

from .infra.db import background_session_maker
from .models import AsyncTaskRecord, CallRecord, RunRecord, SearchLog, SearchMiss

_pending: set[asyncio.Task] = set()
# ONE writer per process, and it writes in batches. Audit rows are single-row inserts that cost
# milliseconds each, so four concurrent writers bought nothing but four `background` slots - and
# every slot is paid twice (two uvicorn workers) and again at every deploy against the database's
# 103-connection ceiling (ops/deploy.md). One writer draining a queue in batches of `_BATCH` rows
# lands the same rows in fewer round trips and holds one connection.
_MAX_CONCURRENT_WRITES = 1
_MAX_PENDING = 5000          # shed load past this: drop the audit row rather than grow unbounded
_BATCH = 200                 # queued operations per batch; a failed batch retries one by one
_queue: deque[tuple[type, dict]] = deque()


class _AsyncHitUpdate:
    """Queued audit mutation, ordered with inserts on the same writer."""


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
    status_code: int, client: str = "", refused_by: str | None = None, telemetry: dict | None = None,
    api_key_id: int | None = None, api_key_name: str | None = None,
    api_key_prefix: str | None = None,
    async_submission: bool = False,
) -> None:
    """`telemetry` carries the marketplace/spend columns (endpoint_id, provider, credential_tier,
    cost_*_micro, duration_ms, response_bytes, params_hash) — absent for a plain tool call, where they
    stay NULL. It is still fire-and-forget: the money landed in the ledger synchronously, so losing a
    row here costs analytics, not accounting. `refused_by` marks a call TREG refused before anything
    went upstream (see models.CallRecord) — NULL whenever the provider actually answered."""
    _enqueue(CallRecord, dict(
        org_id=org_id, user_email=user_email, tool_name=tool_name,
        method=method, path=path, status_code=status_code, client=client, refused_by=refused_by,
        api_key_id=api_key_id, api_key_name=api_key_name, api_key_prefix=api_key_prefix,
        **_known_fields(CallRecord, telemetry),
        **({"_async_submission": True} if async_submission else {}),
    ))


def record_async_call_hit(call_id: str, endpoint_id: str, org_id: int, hit: bool, *,
                          verdict: str | None = None) -> None:
    """Queue the terminal audit correction without delaying the provider's response.

    If the insert has not landed, its task-row read supplies the durable verdict. If it
    has landed, this update corrects it. Both operations use the one audit writer.
    """
    _enqueue(_AsyncHitUpdate, dict(call_id=call_id, endpoint_id=endpoint_id,
                                   org_id=org_id, hit=hit, verdict=verdict))


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


def record_search_miss(*, query: str, source: str, reason: str | None = None,
                       engine: str | None = None) -> None:
    """A catalog search that matched nothing — logged so the misses can steer ingest (see
    models.SearchMiss). On a find, `reason` says why the answer was empty and `engine` which find
    answered. Same contract as every write here: fire-and-forget, and a dropped row under load
    costs a data point, never a search response."""
    _enqueue(SearchMiss, dict(query=query[:300], source=source, reason=reason, engine=engine))


def record_search(*, query: str, source: str, org_id: int | None, user_email: str | None,
                  **fields) -> None:
    """One search under the discovery experiment (models.SearchLog): both rankers' pages and the one
    served, with the caller's identity so a later call can be credited. `fields` are the
    experiment's own columns (application.search_experiment.Outcome.log). Fire-and-forget, like
    every write here — a dropped row costs one sample of the experiment, never a search."""
    _enqueue(SearchLog, dict(query=query[:300], source=source, org_id=org_id,
                             user_email=user_email, **fields))


def record_run(
    *, org_id: int | None = None, user_email: str, bundle_name: str, argv: list, exit_code: int,
    duration_ms: int, client: str = "", api_key_id: int | None = None,
    api_key_name: str | None = None, api_key_prefix: str | None = None,
    tags: dict | None = None,
) -> None:
    _enqueue(RunRecord, dict(
        org_id=org_id, user_email=user_email, bundle_name=bundle_name,
        argv=argv, exit_code=exit_code, duration_ms=duration_ms, client=client,
        tags=dict(tags) if tags else None,
        api_key_id=api_key_id, api_key_name=api_key_name, api_key_prefix=api_key_prefix,
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
            # Lock task rows before reading their verdicts. The terminal finalizer holds the
            # same row lock while committing its verdict, so whichever side wins the race,
            # the inserted CallRecord gets the terminal hit or the later update finds it.
            async_ids = [fields["call_ref"] for model, fields in rows
                         if model is CallRecord and fields.get("_async_submission")]
            tasks = {}
            if async_ids:
                tasks = {row.call_id: row for row in (await session.execute(
                    select(AsyncTaskRecord).where(AsyncTaskRecord.call_id.in_(async_ids))
                    .with_for_update())).scalars()}
            records = []
            for model, fields in rows:
                if model is _AsyncHitUpdate:
                    # Earlier inserts in this batch must be visible to the UPDATE.
                    if records:
                        session.add_all(records)
                        records = []
                        await session.flush()
                    await session.execute(update(CallRecord).where(
                        CallRecord.call_ref == fields["call_id"],
                        CallRecord.endpoint_id == fields["endpoint_id"],
                        CallRecord.org_id == fields["org_id"],
                    ).values(hit=fields["hit"], verdict=fields["verdict"]))
                    continue
                values = {k: v for k, v in fields.items() if k != "_async_submission"}
                if fields.get("_async_submission") and (task := tasks.get(fields["call_ref"])) is not None:
                    values["hit"] = task.hit
                    values["verdict"] = task.verdict
                records.append(model(**values))
            session.add_all(records)
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
