"""The fire-and-forget drain discipline — audit and archive carry twin copies of it."""

import asyncio
import subprocess
import sys

import pytest

from treg import archive, audit

# Two hand-copied implementations of the same pending-set/drain pattern (archive documents itself
# as "audit's discipline"), so both are pinned here: archive copied audit's drain BEFORE the
# busy-spin fix landed in audit, and the stale copy went on wedging CI for a day after audit was
# already safe. A shared pin is what keeps the twins from diverging again.
_MODULES = [audit, archive]


@pytest.mark.parametrize("mod", _MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
async def test_drain_exits_when_a_finished_task_lingers_in_the_pending_set(mod):
    """The CI livelock shape: a completed task still in `_pending` with no removal callback coming.

    Awaiting a gather of already-complete tasks never suspends (Python ≥3.10 gather returns a done
    future eagerly), so a drain that relies on the call_soon'd discard callback spins synchronously
    forever — asyncio timers included, which is why the in-process `wait_for` below could never fire
    against the broken shape. Drain must remove what it gathered itself.
    """
    async def _noop() -> None:
        return None

    task = asyncio.create_task(_noop())
    await task
    mod._pending.add(task)
    await asyncio.wait_for(mod.drain(), timeout=5)
    assert task not in mod._pending


@pytest.mark.parametrize("name", ["audit", "archive"])
def test_drain_livelock_regression_fails_instead_of_wedging(name):
    """Run the same shape in a subprocess with a parent-side deadline.

    A regression here livelocks the event loop at ~100% CPU, starving every in-process watchdog —
    the only reliable referee lives outside the process. A wedged suite was exactly how the original
    bug presented on CI; a reintroduction must fail in seconds instead.
    """
    program = (
        f"import asyncio\n"
        f"from treg import {name} as mod\n"
        "async def main():\n"
        "    async def _noop():\n"
        "        return None\n"
        "    task = asyncio.create_task(_noop())\n"
        "    await task\n"
        "    mod._pending.add(task)\n"
        "    await mod.drain()\n"
        "    assert not mod._pending\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], timeout=30, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-500:]


# ---- the batching writer (2026-09-07) ----------------------------------------------------------
# One writer per process, `_BATCH` rows per INSERT. What the pool budget bought must not cost rows:
# every row enqueued lands, a row the database refuses costs only itself, and shedding is the one
# loss - on the queue, where rows wait now.

from treg.infra.db import reset_db, session_maker  # noqa: E402
from treg.models import SearchMiss  # noqa: E402


async def _misses() -> list[str]:
    from sqlalchemy import select
    async with session_maker() as db:
        return sorted((await db.execute(select(SearchMiss.query))).scalars().all())


@pytest.fixture
async def clean_audit():
    await reset_db()
    await audit.drain()
    audit._queue.clear()
    yield
    await audit.drain()


async def test_a_burst_lands_in_batches_on_one_writer(clean_audit, monkeypatch):
    """More rows than one batch, enqueued at once: every one lands, and never more than one writer
    task existed to do it (the whole point - one `background` slot per process, not four)."""
    monkeypatch.setattr(audit, "_BATCH", 7)
    peak = 0
    real = audit._schedule

    def counting(coro):
        nonlocal peak
        real(coro)
        peak = max(peak, len(audit._pending))
    monkeypatch.setattr(audit, "_schedule", counting)

    for i in range(50):
        audit.record_search_miss(query=f"q{i:02d}", source="test")
    await audit.drain()
    assert await _misses() == [f"q{i:02d}" for i in range(50)]
    assert peak == 1


async def test_a_refused_row_costs_only_itself(clean_audit, monkeypatch):
    """A batch with one row the database rejects (NULL into a NOT NULL column) is retried row by
    row, so the 199 rows around it still land - the failure evidence of a burst must survive the
    burst."""
    monkeypatch.setattr(audit, "_BATCH", 10)
    for i in range(5):
        audit.record_search_miss(query=f"ok{i}", source="test")
    audit._enqueue(SearchMiss, dict(query=None, source="test"))   # the bad row, mid-batch
    for i in range(5, 9):
        audit.record_search_miss(query=f"ok{i}", source="test")
    await audit.drain()
    assert await _misses() == [f"ok{i}" for i in range(9)]


async def test_rows_past_the_queue_bound_are_shed_not_queued(clean_audit, monkeypatch):
    monkeypatch.setattr(audit, "_MAX_PENDING", 3)
    monkeypatch.setattr(audit, "_schedule", lambda coro: coro.close())   # nothing drains meanwhile
    before = audit._shed
    for i in range(5):
        audit.record_search_miss(query=f"s{i}", source="test")
    assert len(audit._queue) == 3
    assert audit._shed - before == 2
    audit._queue.clear()


async def test_a_row_enqueued_as_the_writer_exits_still_lands(clean_audit, monkeypatch):
    """The gap between the writer's last empty-queue check and its done-callback: a row enqueued
    there sees `_pending` occupied and starts nothing. The callback must start a writer for it, or
    the row waits for the next call - forever, on a quiet server.

    The writer is made to finish inside its first step (a batch writer that never suspends), so a
    `call_soon` queued right behind that step runs after the task completed and BEFORE its done
    callbacks - exactly the gap.
    """
    landed: list[str] = []

    async def instant(rows):
        landed.extend(fields["query"] for _, fields in rows)
        return True
    monkeypatch.setattr(audit, "_write_batch", instant)

    audit.record_search_miss(query="first", source="test")
    (writer,) = audit._pending
    seen: dict = {}

    def late():
        audit.record_search_miss(query="late", source="test")
        seen["pending"] = set(audit._pending)
        seen["queued"] = len(audit._queue)
    asyncio.get_running_loop().call_soon(late)

    await writer   # resumes after the writer's done callbacks, `late` having run before them
    assert seen == {"pending": {writer}, "queued": 1}, "the late row did not hit the gap"
    # No drain: the done callback alone must have started a writer for the late row.
    assert audit._pending and writer not in audit._pending
    await asyncio.gather(*audit._pending)
    assert landed == ["first", "late"]
