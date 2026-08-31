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
