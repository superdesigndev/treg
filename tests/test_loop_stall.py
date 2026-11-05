"""A loop that stops running for seconds is named by its stack, and the watch never holds the loop."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time

from treg.infra.loop_stall import LoopStallWatch


def _hold_the_loop(seconds: float) -> None:
    time.sleep(seconds)


def _events(caplog) -> list[dict]:
    return [json.loads(record.getMessage().split(" ", 1)[1]) for record in caplog.records
            if record.name == "treg.loop_stall"]


async def test_a_stall_logs_the_holding_stack_and_then_the_whole_gap(caplog):
    caplog.set_level(logging.WARNING, logger="treg.loop_stall")
    watch = LoopStallWatch(stall_s=0.3, heartbeat_s=0.05).start()
    await asyncio.sleep(0.1)
    _hold_the_loop(0.8)
    for _ in range(100):
        if len(_events(caplog)) == 2:
            break
        await asyncio.sleep(0.02)
    await watch.stop()
    stall, end = _events(caplog)
    assert stall["event"] == "loop_stall" and stall["stalled_s"] >= 0.3
    assert any("_hold_the_loop" in line for line in stall["stack"])
    assert end["event"] == "loop_stall_end" and end["stalled_s"] >= 0.8


async def test_a_blocked_log_handler_never_holds_the_loop(caplog):
    caplog.set_level(logging.WARNING, logger="treg.loop_stall")
    release = threading.Event()

    class Stuck(logging.Handler):
        def emit(self, record):
            release.wait(5)

    stuck = Stuck()
    logging.getLogger("treg.loop_stall").addHandler(stuck)
    try:
        watch = LoopStallWatch(stall_s=0.2, heartbeat_s=0.02).start()
        await asyncio.sleep(0.05)
        _hold_the_loop(0.3)
        started = time.monotonic()
        await asyncio.sleep(0.05)
        await watch.stop()
        assert time.monotonic() - started < 0.2
    finally:
        release.set()
        logging.getLogger("treg.loop_stall").removeHandler(stuck)


async def test_a_running_loop_logs_nothing(caplog):
    caplog.set_level(logging.WARNING, logger="treg.loop_stall")
    watch = LoopStallWatch(stall_s=0.3, heartbeat_s=0.05).start()
    await asyncio.sleep(0.5)
    await watch.stop()
    assert not _events(caplog)
