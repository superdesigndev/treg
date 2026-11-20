"""Call events: one structured event per call, sent to an HTTP stream — bounded, droppable.

Same discipline as audit.py and analytics.py (rule #2: never block the proxied response), but
the sink is an HTTP stream (a Cloudflare Pipelines stream that writes an Apache Iceberg table),
never the database. The analytics this feeds (endpoint benchmarks, find-then-verify joins) are
batch reads over months of calls; keeping them out of Postgres is the point. Losing an event
costs one sample, never money or a response, so every failure mode is a drop:

- not configured (no URL or no HMAC secret)  -> the module is inert, nothing is queued;
- queue full (`_MAX_PENDING`)                 -> the new event is dropped and counted;
- no running loop                             -> dropped and counted;
- a builder raises                            -> the event goes out with its generic columns only;
- the POST fails or answers non-2xx           -> a transient failure (timeout, transport error,
                                                429, 5xx) is retried with backoff; then the batch
                                                is dropped and counted.

A retried POST whose first attempt timed out may already have been accepted, so the table can
hold a batch twice: readers deduplicate on `event_id`. Each process logs its cumulative counters
every `_STATS_INTERVAL_S` (`call events stats: ...`), so the loss rate is readable from logs.

`emit()` is synchronous, takes no lock, opens no DB session and never raises. The expensive part
of an event (parsing request and answer bodies, hashing identifiers) is a `build` callable that
runs in the flusher, not on the call path. A builder keeps the body bytes it was handed alive
until it runs, so their total is bounded too (`_MAX_RETAINED_BYTES`): past it the event is still
queued, without its builder (counted as `unbuilt`).

One flusher task per process micro-batches the queue (at most `_BATCH_MAX` events or about
`_BATCH_BYTES` per POST, at most every `_FLUSH_INTERVAL_S`). The HTTP client is opened per
flush, as in analytics.py: a loop-bound client cached at module level breaks across test loops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

import httpx

from .config import get_settings

log = logging.getLogger("treg.call_events")

_MAX_PENDING = 5000            # events waiting to be sent; past this a new event is dropped
_MAX_RETAINED_BYTES = 8 << 20  # body bytes builders may keep alive while they wait
_BATCH_MAX = 200               # events per POST
_BATCH_BYTES = 900_000         # a POST stays under the stream's request size limit
_FLUSH_INTERVAL_S = 2.0        # max staleness before a flush
_POST_TIMEOUT_S = 10.0
_RETRY_DELAYS_S = (1.0, 4.0)   # waits before the 2nd and 3rd attempt of one batch
_STATS_INTERVAL_S = 300.0      # cumulative counters to the log, per process

Build = Callable[[], dict]


@dataclass
class _Pending:
    event: dict
    build: Build | None
    retained: int


_queue: deque[_Pending] = deque()
_retained = 0
_flusher: asyncio.Task | None = None
# False after a batch exhausted its retries: later batches get one attempt each until one lands,
# so a stream outage costs one try per batch instead of stalling the flusher on backoffs.
_healthy = True
_last_stats: float | None = None   # monotonic time of the last stats line; None = never
counters = {"queued": 0, "sent": 0, "retried": 0, "dropped_overflow": 0, "dropped_failed": 0,
            "dropped_unserializable": 0, "unbuilt": 0, "build_failed": 0}


def enabled() -> bool:
    s = get_settings()
    return bool(s.call_events_url and s.call_events_hmac_secret)


def emit(event: dict, build: Build | None = None, *, retained_bytes: int = 0) -> None:
    """Queue one event. Synchronous, non-blocking, never raises.

    `build`, when given, returns more columns for the event; it runs later in the flusher and
    may parse bodies it closed over (`retained_bytes` says how many it keeps alive)."""
    global _retained
    try:
        if not enabled():
            return
        if len(_queue) >= _MAX_PENDING:
            _drop("dropped_overflow")
            return
        if build is not None and _retained + retained_bytes > _MAX_RETAINED_BYTES:
            build, retained_bytes = None, 0
            counters["unbuilt"] += 1
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _drop("dropped_overflow")
            return
        _queue.append(_Pending(event, build, retained_bytes if build is not None else 0))
        _retained += retained_bytes if build is not None else 0
        counters["queued"] += 1
        _ensure_flusher(loop)
    except Exception:  # noqa: BLE001 — an event must never surface into a caller's path
        pass


def _drop(kind: str, n: int = 1) -> None:
    counters[kind] += n
    total = counters[kind]
    if total == n or total // 1000 != (total - n) // 1000:
        log.error("call events %s: %d event(s) this process", kind, total)


def _ensure_flusher(loop: asyncio.AbstractEventLoop) -> None:
    global _flusher
    if _flusher is None or _flusher.done() or _flusher.get_loop() is not loop:
        _flusher = loop.create_task(_flush_loop())


def _take() -> _Pending:
    global _retained
    item = _queue.popleft()
    _retained -= item.retained
    return item


def _materialize(item: _Pending) -> dict:
    event = item.event
    if item.build is not None:
        try:
            event = {**event, **(item.build() or {})}
        except Exception:
            counters["build_failed"] += 1
            log.warning("call event builder failed for %s", event.get("call_ref"), exc_info=True)
    return event


def _next_batch() -> list[bytes]:
    batch: list[bytes] = []
    size = 2
    while _queue and len(batch) < _BATCH_MAX:
        try:
            # An omitted field lands as SQL NULL; an explicit null in a `json` column would
            # land as the string 'null'.
            event = {k: v for k, v in _materialize(_take()).items() if v is not None}
            line = json.dumps(event, separators=(",", ":"),
                              ensure_ascii=False, allow_nan=False, default=str).encode()
        except (TypeError, ValueError):
            counters["dropped_unserializable"] += 1
            continue
        if batch and size + len(line) + 1 > _BATCH_BYTES:
            # Over the size budget: this one goes first in the next batch.
            _queue.appendleft(_Pending(json.loads(line), None, 0))
            break
        batch.append(line)
        size += len(line) + 1
    return batch


async def _flush_loop() -> None:
    while _queue:
        await asyncio.sleep(_FLUSH_INTERVAL_S)
        await _flush_all()


async def _flush_all(*, retry: bool = True) -> None:
    while _queue:
        batch = _next_batch()
        if batch:
            await _post(batch, retry=retry)
    _log_stats()


def _log_stats(*, force: bool = False) -> None:
    global _last_stats
    now = time.monotonic()
    # Not "0.0 = long ago": monotonic time counts from boot, so a freshly booted host would
    # wait out the interval before its first line.
    if not force and _last_stats is not None and now - _last_stats < _STATS_INTERVAL_S:
        return
    _last_stats = now
    log.info("call events stats: pid=%d pending=%d %s", os.getpid(), len(_queue),
             " ".join(f"{k}={v}" for k, v in counters.items()))


def _transient(status: int) -> bool:
    return status == 429 or status >= 500


async def _post(batch: list[bytes], *, retry: bool = True) -> None:
    global _healthy
    s = get_settings()
    headers = {"Content-Type": "application/json"}
    if s.call_events_token:
        headers["Authorization"] = f"Bearer {s.call_events_token}"
    content = b"[" + b",".join(batch) + b"]"
    delays = _RETRY_DELAYS_S if retry and _healthy else ()
    for attempt in range(len(delays) + 1):
        if attempt:
            counters["retried"] += 1
            await asyncio.sleep(delays[attempt - 1])
        try:
            async with httpx.AsyncClient(timeout=_POST_TIMEOUT_S) as client:
                response = await client.post(s.call_events_url, headers=headers, content=content)
        except Exception as exc:  # noqa: BLE001 — a stream hiccup costs this batch, nothing else
            log.warning("call events POST failed: %s (attempt %d)", type(exc).__name__, attempt + 1)
            continue
        if 200 <= response.status_code < 300:
            counters["sent"] += len(batch)
            _healthy = True
            return
        log.warning("call events stream answered HTTP %s (attempt %d)",
                    response.status_code, attempt + 1)
        if not _transient(response.status_code):
            break   # a refusal (bad schema, bad token) will not change on retry
    _healthy = False
    _drop("dropped_failed", len(batch))


async def drain() -> None:
    """Best-effort flush of whatever is queued (shutdown / tests). One attempt per batch, so a
    stream outage cannot hold up shutdown; the final counters go to the log."""
    global _flusher
    if _flusher is not None and not _flusher.done():
        _flusher.cancel()
        try:
            await _flusher
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
    _flusher = None
    try:
        await _flush_all(retry=False)
        if counters["queued"]:
            _log_stats(force=True)
    except Exception:  # noqa: BLE001 — drain runs in lifespan shutdown; never mask teardown
        pass


def reset() -> None:
    """Tests: forget queued events and counters."""
    global _retained, _flusher, _healthy, _last_stats
    _healthy, _last_stats = True, None
    _queue.clear()
    _retained = 0
    if _flusher is not None and not _flusher.done():
        _flusher.cancel()
    _flusher = None
    for k in counters:
        counters[k] = 0
