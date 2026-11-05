"""Name the code that holds a process's event loop when it stops running for seconds.

A heartbeat task re-arms `faulthandler.dump_traceback_later` on every beat. If the loop does not
come back within `STALL_SECONDS`, faulthandler's own C thread writes every thread's stack to
stderr: it needs no GIL, so it fires even when the stall is C code that holds it. When the loop
resumes, the heartbeat logs one `loop_stall` line with how long it was gone, which is what to
search for; the stack dump just before it (`Timeout (0:00:05)!`) names the code.

Never raises into the service: a diagnostic that cannot start leaves the service as it was.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import logging
import os
import sys
import time

STALL_SECONDS = 5.0
HEARTBEAT_SECONDS = 1.0
log = logging.getLogger("treg.loop_stall")


class LoopStallWatch:
    def __init__(self, *, stall_s: float = STALL_SECONDS, heartbeat_s: float = HEARTBEAT_SECONDS,
                 stream=None) -> None:
        self.stall_s = stall_s
        self.heartbeat_s = heartbeat_s
        self._stream = stream
        self._task: asyncio.Task | None = None

    def start(self) -> LoopStallWatch:
        if self._task is None:
            self._task = asyncio.create_task(self._beat(), name="treg-loop-stall-watch")
        return self

    async def _beat(self) -> None:
        stream = self._stream or sys.stderr
        try:
            faulthandler.dump_traceback_later(self.stall_s, repeat=False, file=stream)
        except (AttributeError, OSError, ValueError):   # a stream without a file descriptor
            return
        try:
            while True:
                faulthandler.dump_traceback_later(self.stall_s, repeat=False, file=stream)
                planned = time.monotonic() + self.heartbeat_s
                await asyncio.sleep(self.heartbeat_s)
                late = time.monotonic() - planned
                if late >= self.stall_s:
                    log.warning("loop_stall %s", json.dumps(
                        {"event": "loop_stall", "worker_pid": os.getpid(), "stalled_s": round(late, 3)},
                        separators=(",", ":")))
        finally:
            faulthandler.cancel_dump_traceback_later()

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except BaseException:  # noqa: BLE001 - stopping a diagnostic never replaces a shutdown's outcome
            pass
        self._task = None


def start_loop_stall_watch() -> LoopStallWatch | None:
    try:
        return LoopStallWatch().start()
    except Exception:  # noqa: BLE001 - see the module docstring
        return None


async def stop_loop_stall_watch(watch: LoopStallWatch | None) -> None:
    if watch is not None:
        await watch.stop()
