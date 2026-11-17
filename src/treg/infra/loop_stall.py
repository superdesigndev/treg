"""Name the code that holds a process's event loop when it stops running for seconds.

The loop only stamps a heartbeat and, when a beat comes `STALL_SECONDS` or more after the one
before, appends that gap to a bounded queue; it never logs, writes or waits for this diagnostic.
Every such gap becomes one `loop_stall_end` line, measured on the loop's own clock, so a stall is
reported even when nothing caught it in progress. A daemon thread reads the stamp: once it is
`STALL_SECONDS` old, the thread also writes `loop_stall` with the loop thread's stack.

A Python thread cannot run while C code holds the GIL. When the watcher woke later than its own
schedule allows, its stack may show where the loop went next rather than what held it, and the
line says `stack_reliable: false`.

Lines go straight to the process's stderr descriptor, never through `logging`: a full pipe blocks
only the watcher thread and takes no lock that request logging or interpreter shutdown needs.
Never raises into the service: a diagnostic that cannot start leaves the service as it was.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import sys
import threading
import time
import traceback

STALL_SECONDS = 5.0
HEARTBEAT_SECONDS = 0.25
_STACK_FRAMES = 40
_ENDS_KEPT = 64
# A pipe write of at most PIPE_BUF bytes is not interleaved with other writers (POSIX).
_LINE_BYTES = 4096


class LoopStallWatch:
    def __init__(self, *, stall_s: float = STALL_SECONDS, heartbeat_s: float = HEARTBEAT_SECONDS,
                 out_fd: int = 2) -> None:
        self.stall_s = stall_s
        self.heartbeat_s = heartbeat_s
        self.late_tolerance_s = stall_s / 5
        self.out_fd = out_fd
        self._beat = 0.0
        self._ends: collections.deque[float] = collections.deque(maxlen=_ENDS_KEPT)
        self._loop_thread = 0
        self._task: asyncio.Task | None = None
        self._stop = threading.Event()

    def start(self) -> LoopStallWatch:
        if self._task is None:
            self._loop_thread = threading.get_ident()
            self._beat = time.monotonic()
            threading.Thread(target=self._watch, name="treg-loop-stall-watch", daemon=True).start()
            try:
                self._task = asyncio.create_task(self._heartbeat(), name="treg-loop-stall-heartbeat")
            except BaseException:
                self._stop.set()
                raise
        return self

    async def _heartbeat(self) -> None:
        while True:
            now = time.monotonic()
            if now - self._beat >= self.stall_s:
                self._ends.append(now - self._beat)
            self._beat = now
            await asyncio.sleep(self.heartbeat_s)

    def _watch(self) -> None:
        reported_beat = None
        while True:
            slept_from = time.monotonic()
            if self._stop.wait(self.heartbeat_s):
                return
            now = time.monotonic()
            late = now - slept_from - self.heartbeat_s
            beat = self._beat
            if beat != reported_beat and now - beat >= self.stall_s:
                frame = sys._current_frames().get(self._loop_thread)
                if self._beat == beat:
                    reported_beat = beat
                    self._emit("loop_stall", stalled_s=now - beat,
                               stack_reliable=late < self.late_tolerance_s,
                               stack=traceback.format_stack(frame)[-_STACK_FRAMES:] if frame else [])
            while self._ends:
                self._emit("loop_stall_end", stalled_s=self._ends.popleft())

    def _emit(self, event: str, **fields) -> None:
        # A stopped watch writes nothing: a write it was blocked in can return after its owner
        # closed `out_fd`, and the number may by then name someone else's descriptor.
        if self._stop.is_set():
            return
        try:
            fields = {"event": event, "worker_pid": os.getpid(), **fields,
                      "stalled_s": round(fields["stalled_s"], 3)}
            while True:
                line = f"{event} {json.dumps(fields, separators=(',', ':'))}\n".encode()
                if len(line) <= _LINE_BYTES or not fields.get("stack"):
                    break
                fields["stack"] = fields["stack"][1:]
            os.write(self.out_fd, line[:_LINE_BYTES])
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
