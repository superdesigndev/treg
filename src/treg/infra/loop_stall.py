"""Name the code that holds a process's event loop when it stops running for seconds.

The loop only stamps a heartbeat; it never logs, writes or waits for this diagnostic. A daemon
thread reads the stamp: once it is `STALL_SECONDS` old, the thread logs `loop_stall` with the
loop thread's stack, and when the loop beats again it logs `loop_stall_end` with the whole gap,
both measured from the same last beat. A Python thread cannot run while C code holds the GIL, so
when the watcher itself woke that late its stack would show where the loop went next, not what
held it: the line then says `stack_reliable: false`.

Never raises into the service: a diagnostic that cannot start leaves the service as it was.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import traceback

STALL_SECONDS = 5.0
HEARTBEAT_SECONDS = 1.0
_STACK_FRAMES = 40
log = logging.getLogger("treg.loop_stall")


class LoopStallWatch:
    def __init__(self, *, stall_s: float = STALL_SECONDS, heartbeat_s: float = HEARTBEAT_SECONDS) -> None:
        self.stall_s = stall_s
        self.heartbeat_s = heartbeat_s
        self._beat = 0.0
        self._loop_thread = 0
        self._task: asyncio.Task | None = None
        self._stop = threading.Event()

    def start(self) -> LoopStallWatch:
        if self._task is None:
            self._loop_thread = threading.get_ident()
            self._beat = time.monotonic()
            self._task = asyncio.create_task(self._heartbeat(), name="treg-loop-stall-heartbeat")
            threading.Thread(target=self._watch, name="treg-loop-stall-watch", daemon=True).start()
        return self

    async def _heartbeat(self) -> None:
        while True:
            self._beat = time.monotonic()
            await asyncio.sleep(self.heartbeat_s)

    def _watch(self) -> None:
        reported_beat = None
        woke = time.monotonic()
        while not self._stop.wait(self.heartbeat_s):
            now = time.monotonic()
            watcher_late = now - woke - self.heartbeat_s
            woke = now
            beat = self._beat
            if reported_beat is not None:
                if beat != reported_beat:
                    self._emit("loop_stall_end", stalled_s=beat - reported_beat)
                    reported_beat = None
                continue
            gap = now - beat
            if gap >= self.stall_s:
                reported_beat = beat
                frame = sys._current_frames().get(self._loop_thread)
                self._emit("loop_stall", stalled_s=gap, stack_reliable=watcher_late < self.stall_s,
                           stack=traceback.format_stack(frame)[-_STACK_FRAMES:] if frame else [])
            elif watcher_late >= self.stall_s:
                # The whole process was held and the loop is already back: a gap, no stack.
                self._emit("loop_stall", stalled_s=watcher_late, stack_reliable=False, stack=[])

    def _emit(self, event: str, **fields) -> None:
        try:
            log.warning("%s %s", event, json.dumps(
                {"event": event, "worker_pid": os.getpid(), **fields,
                 "stalled_s": round(fields["stalled_s"], 3)}, separators=(",", ":")))
        except Exception:  # noqa: BLE001 - see the module docstring
            pass

    async def stop(self) -> None:
        self._stop.set()
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
