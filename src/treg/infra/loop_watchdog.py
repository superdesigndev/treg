"""Write every thread's stack when the event loop stops running callbacks.

The loop only stamps a heartbeat every `_BEAT_S`; nothing it does here can block. A watchdog
thread re-arms `faulthandler.dump_traceback_later` each time it sees a fresh beat. When beats
stop for about `threshold_s`, nothing re-arms the timer and faulthandler's own C thread writes all
Python stacks to stderr: it needs neither the GIL nor the logging locks, so it also reports a loop
stuck in C code (which starves the watchdog thread too) or behind a held logging lock. One dump
per stall; the watchdog thread logs how long the loop stood still once beats resume.

faulthandler writes to its own non-blocking descriptor for stderr, reopened through /proc: re-arming
waits for a running dump while holding the GIL, so a dump that could block on a full pipe would
freeze the whole process. A full pipe drops the dump instead. Where /proc is missing the watchdog
does not start. A held logging lock blocks only the watchdog thread.
"""
from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import time
from typing import TextIO

# CPython starts a new timer thread on every re-arm, so the beat stays coarse.
_BEAT_S = 1.0

log = logging.getLogger("treg.loop_watchdog")


class LoopWatchdog:
    def __init__(self, threshold_s: float, *, file: TextIO | None = None) -> None:
        self._threshold = threshold_s
        self._file = file if file is not None else sys.stderr
        self._beat = time.monotonic()
        self._fd = -1
        self._handle: asyncio.TimerHandle | None = None
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="treg-loop-watchdog", daemon=True)

    def start(self) -> "LoopWatchdog":
        # A new open file description, so O_NONBLOCK never reaches the process's own stderr.
        self._fd = os.open(f"/proc/self/fd/{self._file.fileno()}",
                           os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK)
        try:
            self._stamp()
            self._thread.start()
        except BaseException:
            self.stop()
            os.close(self._fd)
            raise
        return self

    def _stamp(self) -> None:
        self._beat = time.monotonic()
        self._handle = asyncio.get_running_loop().call_later(_BEAT_S, self._stamp)

    def stop(self) -> None:
        """Never waits: the watchdog thread cancels the timer itself on its way out."""
        self._stopped.set()
        if self._handle is not None:
            self._handle.cancel()
            self._handle = None

    def _watch(self) -> None:
        seen = self._beat
        try:
            while not self._stopped.wait(_BEAT_S):
                beat = self._beat
                if beat == seen:  # no beat since the last look: let the armed timer run out
                    continue
                stalled = beat - seen - _BEAT_S
                seen = beat
                if stalled >= self._threshold:
                    log.warning("loop_stall_end stalled_ms=%d threshold_s=%g; the stacks written "
                                "during the stall precede this line", int(stalled * 1000),
                                self._threshold)
                # Slack for this thread's own polling phase, so a running loop never fires it.
                faulthandler.dump_traceback_later(self._threshold + 2 * _BEAT_S, exit=False,
                                                  file=self._fd)
        finally:
            faulthandler.cancel_dump_traceback_later()
            os.close(self._fd)


def start_loop_watchdog(threshold_s: float) -> LoopWatchdog | None:
    """`None` when disabled (`threshold_s <= 0`) or when stderr cannot be reopened."""
    if threshold_s <= 0:
        return None
    try:
        return LoopWatchdog(threshold_s).start()
    except Exception:  # a missing diagnostic must never stop the service
        log.warning("loop watchdog unavailable", exc_info=True)
        return None


def stop_loop_watchdog(watchdog: LoopWatchdog | None) -> None:
    if watchdog is not None:
        try:
            watchdog.stop()
        except Exception:  # noqa: BLE001 - shutdown keeps its original exception
            pass
