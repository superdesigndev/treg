"""A loop that stops running for seconds leaves the blocking code's stack and one searchable line."""

from __future__ import annotations

import asyncio
import logging
import time

from treg.infra.loop_stall import LoopStallWatch


def _hold_the_loop(seconds: float) -> None:
    time.sleep(seconds)


async def test_a_stall_dumps_the_holding_stack_and_logs_how_long(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="treg.loop_stall")
    with open(tmp_path / "stacks.txt", "w+") as stream:
        watch = LoopStallWatch(stall_s=0.3, heartbeat_s=0.05, stream=stream).start()
        await asyncio.sleep(0.1)
        _hold_the_loop(0.6)
        await asyncio.sleep(0.1)
        await watch.stop()
        stream.seek(0)
        dumped = stream.read()
    assert "_hold_the_loop" in dumped
    assert any("loop_stall" in record.getMessage() for record in caplog.records)


async def test_a_running_loop_dumps_nothing(tmp_path, caplog):
    caplog.set_level(logging.WARNING, logger="treg.loop_stall")
    with open(tmp_path / "stacks.txt", "w+") as stream:
        watch = LoopStallWatch(stall_s=0.3, heartbeat_s=0.05, stream=stream).start()
        await asyncio.sleep(0.5)
        await watch.stop()
        time.sleep(0.4)   # past the bound after stop: a cancelled watch must not fire
        stream.seek(0)
        assert stream.read() == ""
    assert not caplog.records
