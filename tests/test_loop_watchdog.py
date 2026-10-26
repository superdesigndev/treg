"""The loop watchdog writes every thread's stack when the event loop stalls, only then, and never
blocks the loop itself, whatever stderr or the logging locks are doing."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import threading
import time

from treg.infra import loop_watchdog


def _blocking_call_in_the_loop(seconds: float) -> None:
    time.sleep(seconds)


async def _loop_runs_freely() -> bool:
    """Ten short sleeps finish close to on time only when nothing blocks the loop."""
    started = time.monotonic()
    for _ in range(10):
        await asyncio.sleep(0.02)
    return time.monotonic() - started < 0.5


async def test_a_stalled_loop_writes_the_stack_of_the_blocking_call(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(loop_watchdog, "_BEAT_S", 0.05)
    with open(tmp_path / "stderr", "w+") as out:
        watchdog = loop_watchdog.LoopWatchdog(0.2, file=out).start()
        try:
            await asyncio.sleep(0.15)
            with caplog.at_level(logging.WARNING, logger="treg.loop_watchdog"):
                _blocking_call_in_the_loop(0.8)
                await asyncio.sleep(0.2)
        finally:
            watchdog.stop()
        out.seek(0)
        dump = out.read()
    assert "most recent call first" in dump
    assert "_blocking_call_in_the_loop" in dump, "the dump names the call that blocked the loop"
    assert any("loop_stall_end" in r.getMessage() for r in caplog.records)


async def test_a_running_loop_writes_nothing(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(loop_watchdog, "_BEAT_S", 0.05)
    with open(tmp_path / "stderr", "w+") as out:
        watchdog = loop_watchdog.LoopWatchdog(0.3, file=out).start()
        with caplog.at_level(logging.WARNING, logger="treg.loop_watchdog"):
            await asyncio.sleep(0.8)
        watchdog.stop()
        out.seek(0)
        assert out.read() == ""
    assert not caplog.records


async def test_a_full_stderr_pipe_never_blocks_the_recovered_loop(monkeypatch):
    monkeypatch.setattr(loop_watchdog, "_BEAT_S", 0.05)
    read_fd, write_fd = os.pipe()
    fcntl.fcntl(write_fd, 1031, 4096)  # F_SETPIPE_SZ: a small pipe fills with one dump
    os.set_blocking(write_fd, False)
    try:
        while True:
            os.write(write_fd, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(write_fd, True)
    out = os.fdopen(write_fd, "w")
    watchdog = loop_watchdog.LoopWatchdog(0.2, file=out).start()
    try:
        await asyncio.sleep(0.15)
        _blocking_call_in_the_loop(0.6)    # the dump now blocks on the full pipe
        assert await _loop_runs_freely(), "a dump stuck on stderr must not hold the loop"
        watchdog.stop()                    # and stopping must not wait for it either
        assert await _loop_runs_freely()
    finally:
        drain = threading.Thread(target=lambda: _drain(read_fd), daemon=True)
        drain.start()
        await asyncio.to_thread(watchdog._thread.join, 2)
        out.close()
        drain.join(2)


def _drain(fd: int) -> None:
    try:
        while os.read(fd, 65536):
            pass
    except OSError:
        pass
    finally:
        os.close(fd)


async def test_a_held_logging_lock_never_blocks_the_recovered_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(loop_watchdog, "_BEAT_S", 0.05)
    release = threading.Event()

    class _StuckLogger:
        def warning(self, *args, **kwargs):
            release.wait(5)               # what a logging lock held elsewhere looks like

    monkeypatch.setattr(loop_watchdog, "log", _StuckLogger())
    with open(tmp_path / "stderr", "w+") as out:
        watchdog = loop_watchdog.LoopWatchdog(0.2, file=out).start()
        try:
            await asyncio.sleep(0.15)
            _blocking_call_in_the_loop(0.6)
            await asyncio.sleep(0.2)       # the watchdog thread is now stuck logging the stall end
            assert await _loop_runs_freely(), "a stuck stall log must not hold the loop"
        finally:
            watchdog.stop()
            release.set()
            await asyncio.to_thread(watchdog._thread.join, 2)


def test_zero_turns_the_watchdog_off():
    assert loop_watchdog.start_loop_watchdog(0) is None
