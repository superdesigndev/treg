"""Archive body I/O and upload scheduling, separate from archive indexing and TTL learning."""
import asyncio
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import logging
import re
import random
import time

from .config import get_settings
from .infra.object_store import ObjectInfo, ObjectStore, ObjectStoreError, exception_name, failure_reason

_log = logging.getLogger("treg.archive_bodies")
_store: ObjectStore | None = None
_pending: set[asyncio.Task] = set()
_pending_bytes = 0
_sem = None
_sem_loop = None
_RECENT_UPLOADS = 20_000
_RETRYABLE_FAILURES = frozenset({"timeout", "rate_limited", "upstream_error", "store_error"})
_WRITE_ATTEMPT_MAX_S = 4.0
_READ_ATTEMPTS = 2
_READ_ATTEMPT_MAX_S = 1.0
_uploaded: OrderedDict[str, None] = OrderedDict()
_inflight: dict[str, asyncio.Future] = {}
_queued_hashes: set[str] = set()
# Operational counters have bounded labels and no call, team, key or body dimensions.
outcomes: Counter = Counter()


def configure(store: ObjectStore | None) -> None:
    global _store, _uploaded, _inflight, _queued_hashes
    _store = store
    # A new store/bucket must not inherit success from the previous one. Leaders retain their
    # old maps so their completion cannot populate the newly configured cache.
    _uploaded, _inflight, _queued_hashes = OrderedDict(), {}, set()


def uses_r2() -> bool:
    s = get_settings()
    return s.archive_body_write != "db" or any(
        getattr(s, f"archive_body_read_{path}") != "db" for path in ("lookup", "result", "terminal"))


def validate_configuration() -> bool:
    s = get_settings()
    from .archive import mode
    from .infra.object_store import R2_ENDPOINT_RE
    if mode() == "off" or not uses_r2():
        return False
    if s.archive_body_write == "r2" and any(
            getattr(s, "archive_body_read_" + path) != "r2-first"
            for path in ("lookup", "result", "terminal")):
        raise RuntimeError("Archive R2-only writing requires all read paths to use r2-first")
    if (not R2_ENDPOINT_RE.fullmatch(s.archive_object_store_endpoint)
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", s.archive_object_store_bucket)
            or not s.archive_object_store_access_key_id or not s.archive_object_store_secret_access_key):
        raise RuntimeError("Archive R2 is enabled but its endpoint, bucket or credentials are missing/invalid")
    return True


class StorageReport:
    """One small completion event, independent of the caller's tool_called event."""
    def __init__(self, *, call_ref=None, emit=None):
        self.call_ref, self.emit = call_ref, emit
        self.finished = False
        self.props = {"archive_body_upload_status": "not_requested", "archive_body_upload_ms": 0.0,
                      "archive_body_queue_wait_ms": 0.0, "archive_body_upload_attempts": 0,
                      "archive_body_upload_retry_reason": "none",
                      "archive_body_upload_retry_recovered": False}
        self.timings = dict.fromkeys(("compare_sem_wait", "compare", "record_key_wait",
                                     "record_sem_wait", "record_db", "observe_sem_wait", "observe"))
        self.failure_phase = None

    @contextmanager
    def measure(self, phase):
        """Include interrupted work; an unentered phase stays unknown rather than zero."""
        started = time.monotonic()
        try:
            yield
        except BaseException:
            self.failure_phase = phase
            raise
        finally:
            self.timings[phase] = (time.monotonic() - started) * 1000

    @asynccontextmanager
    async def wait(self, gate, phase):
        # Measure acquisition only, then retain the original lock/semaphore lifetime.
        with self.measure(phase):
            await gate.acquire()
        try:
            yield
        finally:
            gate.release()

    def finish(self, *, storage=None, reason=None):
        if self.finished:
            return
        self.finished = True
        outcomes[reason or storage or "none"] += 1
        data = {"call_ref": self.call_ref, "storage": storage or "none",
                "upload_status": self.props["archive_body_upload_status"],
                "upload_ms": round(self.props["archive_body_upload_ms"], 3),
                "queue_wait_ms": round(self.props["archive_body_queue_wait_ms"], 3),
                "upload_attempts": self.props["archive_body_upload_attempts"],
                "upload_retry_reason": self.props["archive_body_upload_retry_reason"],
                "upload_retry_recovered": self.props["archive_body_upload_retry_recovered"],
                "dropped": storage is None, "drop_reason": reason or "none"}
        data.update({phase + "_ms": round(ms, 3) if ms is not None else None
                     for phase, ms in self.timings.items()})
        data["failure_phase"] = self.failure_phase
        if self.emit is not None:
            self.emit(data)
        elif self.call_ref:
            from . import analytics
            analytics.capture("archive", "archive_body_stored", data)


def _upload_sem():
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(get_settings().archive_r2_upload_concurrency)
        _sem_loop = loop
    return _sem


@dataclass(frozen=True)
class WritePlan:
    storage: str | None
    reason: str | None = None

    @property
    def keep_db(self) -> bool:
        return self.storage in ("db", "both")


async def prepare(body: bytes, content_hash: str, *, mode: str, observation: StorageReport,
                  terminal: bool = False) -> WritePlan:
    if mode == "db":
        return WritePlan("db")
    uploaded, inflight = _uploaded, _inflight
    if content_hash in uploaded:
        uploaded.move_to_end(content_hash)
        observation.props["archive_body_upload_status"] = "skipped_duplicate"
        outcomes["skipped_duplicate"] += 1
        return WritePlan(mode)

    flight = inflight.get(content_hash)
    if flight is not None:
        queued = time.monotonic()
        # Cancelling a waiter (including a terminal deadline) must not cancel the leader.
        reason = await asyncio.shield(flight)
        observation.props["archive_body_queue_wait_ms"] += (time.monotonic() - queued) * 1000
        observation.props["archive_body_upload_status"] = "coalesced" if reason is None else "failed"
        outcomes["coalesced"] += 1
    else:
        flight = asyncio.get_running_loop().create_future()
        inflight[content_hash] = flight
        reason = "cancelled"
        try:
            error_type, attempt = "none", 0
            attempts = get_settings().archive_r2_terminal_attempts if terminal else 2
            transfer_budget = get_settings().archive_r2_timeout_s
            sem = _upload_sem()
            queued = time.monotonic()
            await sem.acquire()
            observation.props["archive_body_queue_wait_ms"] += (time.monotonic() - queued) * 1000
            deadline = asyncio.get_running_loop().time() + transfer_budget
            slot_acquired = True
            retry_reason = "none"
            while attempt < attempts:
                if attempt:
                    queued = time.monotonic()
                    try:
                        async with asyncio.timeout_at(deadline):
                            await sem.acquire()
                    except TimeoutError:
                        observation.props["archive_body_queue_wait_ms"] += (
                            time.monotonic() - queued) * 1000
                        reason, error_type = "timeout", "TimeoutError"
                        break
                    observation.props["archive_body_queue_wait_ms"] += (
                        time.monotonic() - queued) * 1000
                    slot_acquired = True
                try:
                    attempt += 1
                    observation.props["archive_body_upload_attempts"] = attempt
                    transfer = time.monotonic()
                    try:
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError
                        attempt_timeout = min(_WRITE_ATTEMPT_MAX_S, transfer_budget * 0.4, remaining)
                        async with asyncio.timeout(attempt_timeout):
                            if _store is None:
                                raise ObjectStoreError("store_unavailable")
                            info = await _store.put(body, content_hash=content_hash)
                            if info != ObjectInfo(content_hash, len(body)):
                                raise ObjectStoreError("hash_mismatch")
                        reason = None
                    except Exception as exc:
                        reason, error_type = failure_reason(exc), exception_name(exc)
                    finally:
                        observation.props["archive_body_upload_ms"] += (
                            time.monotonic() - transfer) * 1000
                finally:
                    if slot_acquired:
                        sem.release()
                        slot_acquired = False
                if reason is None or reason not in _RETRYABLE_FAILURES or attempt == attempts:
                    break
                if retry_reason == "none":
                    retry_reason = reason
                    observation.props["archive_body_upload_retry_reason"] = reason
                outcomes["upload_retry_" + reason] += 1
                delay = (random.uniform(1.0, 1.5) if reason in {"rate_limited", "upstream_error"}
                         else random.uniform(0.05, 0.15))
                delay_started = time.monotonic()
                try:
                    async with asyncio.timeout_at(deadline):
                        await asyncio.sleep(delay)
                except TimeoutError:
                    reason, error_type = "timeout", "TimeoutError"
                    break
                finally:
                    observation.props["archive_body_upload_ms"] += (
                        time.monotonic() - delay_started) * 1000
            if reason is None and retry_reason != "none":
                observation.props["archive_body_upload_retry_recovered"] = True
                outcomes["upload_retry_recovered"] += 1
            if reason is not None:
                _log.error("archive body upload failed after %s attempt(s): %s exception_type=%s",
                           attempt, reason, error_type)
            if reason is None:
                uploaded[content_hash] = None
                if len(uploaded) > _RECENT_UPLOADS:
                    uploaded.popitem(last=False)
            observation.props["archive_body_upload_status"] = "uploaded" if reason is None else "failed"
        finally:
            # Publish failures too, and never cache them. A cancelled leader releases its waiters
            # to their normal DB fallback; a later call may attempt the immutable object again.
            inflight.pop(content_hash, None)
            flight.set_result(reason)
            observation.props["archive_body_upload_ms"] = round(observation.props["archive_body_upload_ms"], 3)
    # The write mode selects the normal destination, not the failure policy. Once an eligible
    # body cannot be published to R2, retain it in DB so the snapshot remains readable. This is
    # deliberately rare and keeps R2-only operation from turning a transient store fault into
    # permanent body loss.
    return WritePlan(mode) if reason is None else WritePlan("db", reason=reason)


def submit(factory, body_len: int, observation: StorageReport, *, content_hash: str) -> str | None:
    """Separate count/byte budgets from archive's DB queue and DB semaphore."""
    global _pending_bytes
    s = get_settings()
    queued_hashes = _queued_hashes
    if content_hash in _uploaded or content_hash in queued_hashes or content_hash in _inflight:
        outcomes["duplicate_queue_bypass"] += 1
        return "duplicate"
    reason = ("upload_queue_full" if len(_pending) >= s.archive_r2_max_pending else
              "upload_bytes_full" if _pending_bytes + body_len > s.archive_r2_max_pending_bytes else None)
    if reason:
        observation.props["archive_body_upload_status"] = "dropped"
        observation.props["archive_body_upload_drop_reason"] = reason
        return reason
    _pending_bytes += body_len
    queued_hashes.add(content_hash)

    task = asyncio.create_task(factory())
    _pending.add(task)

    def done(task):
        global _pending_bytes
        _pending.discard(task)
        _pending_bytes -= body_len
        queued_hashes.discard(content_hash)
        if task.cancelled():
            observation.finish(reason="cancelled")
    task.add_done_callback(done)


async def drain() -> None:
    while _pending:
        tasks = list(_pending)
        await asyncio.gather(*tasks, return_exceptions=True)
        _pending.difference_update(tasks)


@dataclass(frozen=True)
class BodyPointer:
    content_hash: str
    storage: str | None
    body: bytes | None
    enc: str | None
    snapshot_id: int | None = None


def _r2_first(path: str) -> bool:
    # Auxiliary readers share existing rollout switches; none enables R2 independently.
    path = {"observation": "lookup", "initialization": "lookup",
            "admin": "result", "arena": "result"}.get(path, path)
    return getattr(get_settings(), "archive_body_read_" + path) == "r2-first"


def read_options(path):
    from sqlalchemy.orm import defer
    from .models import ArchiveSnapshot
    return (defer(ArchiveSnapshot.body),) if _r2_first(path) else ()


async def pointer(session, snapshot, path):
    """Capture metadata only for R2-first; DB fallback is loaded in a later short session."""
    if _r2_first(path):
        return BodyPointer(snapshot.content_hash, snapshot.body_storage, None, None, snapshot.id)
    from .archive import _snapshot_body
    body = await _snapshot_body(session, snapshot)
    return BodyPointer(snapshot.content_hash, snapshot.body_storage, body, None)


async def _db_fallback(pointer, path, *, session_factory=None):
    from .infra.db import session_maker, background_session_maker, admin_session_maker
    from .models import ArchiveSnapshot
    from .archive import _snapshot_body, _unpack
    if pointer.snapshot_id is None:
        return _unpack(pointer.body, pointer.enc)
    if session_factory is not None:
        maker = session_factory
    elif path in {"observation", "initialization"}:
        maker = background_session_maker
    elif path == "admin":
        maker = admin_session_maker
    else:
        maker = session_maker
    async with maker() as session:
        row = await session.get(ArchiveSnapshot, pointer.snapshot_id)
        return await _snapshot_body(session, row) if row is not None else None


async def read(pointer: BodyPointer, path: str, *, diagnostics: dict | None = None,
               session_factory=None) -> bytes | None:
    """Read an already-authorized snapshot, with no DB connection held during object I/O.

    Storage labels describe the original write, not later hash-addressed backfills. R2-first
    probes every selected hash, including legacy rows whose DB copy was subsequently pruned.
    Missing objects still use the original snapshot/carrier; never substitute another answer.
    """
    from . import analytics

    started = time.monotonic()
    storage = pointer.storage if pointer.storage in ("db", "both", "r2") else "legacy"
    report = dict(path=path, storage=storage, source="none",
                  read_mode="r2-first" if _r2_first(path) else "db", outcome="unavailable",
                  fallback_reason="none", r2_attempts=0, r2_retry_reason="none",
                  r2_retry_recovered=False, r2_read_ms=0.0, db_read_ms=0.0, bytes=0)
    try:
        if _r2_first(path):
            body = await _read_object(pointer, path, report)
            if body is not None:
                report.update(source="r2", outcome="r2", bytes=len(body))
                return body
        db_started = time.monotonic()
        try:
            body = (await _db_fallback(pointer, path) if session_factory is None else
                    await _db_fallback(pointer, path, session_factory=session_factory))
        except Exception:
            report["outcome"] = "db_error"
            raise
        finally:
            report["db_read_ms"] = round((time.monotonic() - db_started) * 1000, 3)
        if body is not None:
            report.update(source="db", outcome="db_fallback" if report["r2_attempts"] else "db",
                          bytes=len(body))
        return body
    except asyncio.CancelledError:
        report["outcome"] = "cancelled"
        raise
    finally:
        report["total_ms"] = round((time.monotonic() - started) * 1000, 3)
        if report["outcome"] in {"unavailable", "db_error"} and pointer.snapshot_id is not None:
            report["snapshot_id"] = pointer.snapshot_id
        outcomes["read_" + path + "_" + report["outcome"]] += 1
        if diagnostics is not None:
            diagnostics.update(cache_body_source=report["source"],
                               cache_body_fallback_reason=report["fallback_reason"],
                               cache_r2_read_ms=report["r2_read_ms"],
                               cache_r2_attempts=report["r2_attempts"],
                               cache_r2_retry_reason=report["r2_retry_reason"],
                               cache_r2_retry_recovered=report["r2_retry_recovered"])
        # Best-effort completion event; snapshot identity is limited to unresolved reads.
        # A completed fallback records whether DB rescued the read, unlike a fallback log.
        analytics.capture("archive", "archive_body_read", report)


async def _read_object(pointer, path, report):
    started = time.monotonic()
    reason, error_type = "none", "none"
    total_timeout = get_settings().archive_r2_read_timeout_s
    deadline = asyncio.get_running_loop().time() + total_timeout
    attempt_timeout = min(_READ_ATTEMPT_MAX_S, total_timeout / _READ_ATTEMPTS)
    try:
        for attempt in range(1, _READ_ATTEMPTS + 1):
            report["r2_attempts"] = attempt
            try:
                async with asyncio.timeout_at(min(deadline, asyncio.get_running_loop().time() + attempt_timeout)):
                    if _store is None:
                        raise ObjectStoreError("store_unavailable")
                    body = await _store.get(pointer.content_hash)
                if body is None:
                    reason = "not_found"
                    break
                report["r2_retry_recovered"] = report["r2_retry_reason"] != "none"
                if report["r2_retry_recovered"]:
                    outcomes["read_retry_recovered_" + path] += 1
                return body
            except Exception as exc:
                reason, error_type = failure_reason(exc), exception_name(exc)
            if reason not in _RETRYABLE_FAILURES or attempt == _READ_ATTEMPTS:
                break
            report["r2_retry_reason"] = reason
            outcomes["read_retry_" + path] += 1
            outcomes["read_retry_" + path + "_" + reason] += 1
            delay = min(random.uniform(0.05, 0.1), total_timeout * 0.05)
            await asyncio.sleep(min(delay, max(0, deadline - asyncio.get_running_loop().time())))
        report["fallback_reason"] = reason
        if reason in {"not_found", "hash_mismatch"}:
            _uploaded.pop(pointer.content_hash, None)
        outcomes["read_fallback_" + path] += 1
        outcomes["read_fallback_" + path + "_" + reason] += 1
        level = logging.ERROR if reason in {"permission_denied", "hash_mismatch", "too_large"} else logging.WARNING
        # NULL markers include intentionally hash-only history; a miss is not proof of loss.
        if reason == "not_found" and pointer.storage is None:
            level = logging.INFO
        _log.log(level, "archive R2 read fallback path=%s reason=%s elapsed_ms=%s exception_type=%s",
                 path, reason, round((time.monotonic() - started) * 1000, 3), error_type)
        return None
    finally:
        report["r2_read_ms"] = round((time.monotonic() - started) * 1000, 3)
