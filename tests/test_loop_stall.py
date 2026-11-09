"""A loop that stops running for seconds is reported, named by its stack, and never held by the watch."""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import threading
import time

import pytest

from treg.infra import loop_stall
from treg.infra.loop_stall import LoopStallWatch


def _hold_the_loop(seconds: float) -> None:
    time.sleep(seconds)


def _hold_the_gil(seconds: float) -> None:
    ctypes.PyDLL(None).usleep(int(seconds * 1_000_000))


@pytest.fixture
def out():
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    buffer = bytearray()

    def events() -> list[dict]:
        while True:
            try:
                chunk = os.read(read_fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            buffer.extend(chunk)
        return [json.loads(line.split(" ", 1)[1]) for line in buffer.decode().splitlines()]

    yield write_fd, events
    os.close(read_fd)
    os.close(write_fd)


async def _until(events, done) -> list[dict]:
    for _ in range(150):
        if done(seen := events()):
            return seen
        await asyncio.sleep(0.02)
    return events()


def _ended(seen: list[dict]) -> bool:
    return any(e["event"] == "loop_stall_end" and e["stalled_s"] >= 0.9 for e in seen)


async def test_a_stall_logs_the_holding_stack_and_then_the_whole_gap(out):
    out_fd, events = out
    watch = LoopStallWatch(stall_s=0.5, heartbeat_s=0.05, out_fd=out_fd).start()
    await asyncio.sleep(0.1)
    _hold_the_loop(0.9)
    seen = await _until(events, _ended)
    await watch.stop()
    assert any(e["event"] == "loop_stall" and e["stalled_s"] >= 0.5 and e["stack_reliable"]
               and any("_hold_the_loop" in line for line in e["stack"]) for e in seen)
    assert _ended(seen)


async def test_a_stall_the_watcher_could_not_see_is_still_reported_without_vouching_for_a_stack(out):
    out_fd, events = out
    watch = LoopStallWatch(stall_s=0.5, heartbeat_s=0.05, out_fd=out_fd).start()
    await asyncio.sleep(0.1)
    _hold_the_gil(0.9)
    seen = await _until(events, _ended)
    await watch.stop()
    assert _ended(seen)
    assert all(not e["stack_reliable"] for e in seen if e["event"] == "loop_stall")


async def test_a_full_output_never_holds_the_loop_or_its_stop(out):
    out_fd, _ = out
    os.set_blocking(out_fd, False)
    try:
        while True:
            os.write(out_fd, b"x" * 4096)
    except BlockingIOError:
        os.set_blocking(out_fd, True)
    watch = LoopStallWatch(stall_s=0.2, heartbeat_s=0.02, out_fd=out_fd).start()
    await asyncio.sleep(0.05)
    _hold_the_loop(0.3)
    await asyncio.sleep(0.1)
    started = time.monotonic()
    await asyncio.sleep(0.05)
    await watch.stop()
    assert time.monotonic() - started < 0.2


async def test_a_stopped_watch_writes_nothing(out):
    """A watcher blocked writing to a full pipe wakes after the pipe is closed; by then its fd
    number can name the next owner's pipe, which then read a stray `loop_stall_end` (CI, Linux)."""
    out_fd, events = out
    watch = LoopStallWatch(stall_s=0.2, heartbeat_s=0.02, out_fd=out_fd)
    watch._stop.set()
    watch._emit("loop_stall_end", stalled_s=0.31)
    assert not events()


async def test_a_running_loop_logs_nothing(out):
    out_fd, events = out
    watch = LoopStallWatch(stall_s=1.0, heartbeat_s=0.05, out_fd=out_fd).start()
    await asyncio.sleep(0.5)
    await watch.stop()
    assert not events()


async def test_a_watch_that_cannot_start_leaves_no_heartbeat_behind(monkeypatch):
    def refuse(self):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading.Thread, "start", refuse)
    assert loop_stall.start_loop_stall_watch() is None
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "treg-loop-stall-heartbeat"]
