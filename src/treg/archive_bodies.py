"""Archive body I/O and upload scheduling, separate from archive indexing and TTL learning."""
import asyncio
from collections import Counter
from dataclasses import dataclass
import logging
import re
import time

from .config import get_settings
from .infra.object_store import ObjectInfo, ObjectStore, ObjectStoreError

_log = logging.getLogger("treg.archive_bodies")
_store: ObjectStore | None = None
_pending: set[asyncio.Task] = set()
_pending_bytes = 0
_sem = None
_sem_loop = None
# Operational counters have bounded labels and no call, team, key or body dimensions.
outcomes: Counter = Counter()


def configure(store: ObjectStore | None) -> None:
    global _store
    _store = store


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
                      "archive_body_queue_wait_ms": 0.0}

    def finish(self, *, storage=None, reason=None):
        if self.finished:
            return
        self.finished = True
        outcomes[reason or storage or "none"] += 1
        data = {"call_ref": self.call_ref, "storage": storage or "none",
                "upload_status": self.props["archive_body_upload_status"],
                "upload_ms": round(self.props["archive_body_upload_ms"], 3),
                "queue_wait_ms": round(self.props["archive_body_queue_wait_ms"], 3),
                "dropped": storage is None, "drop_reason": reason or "none"}
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
    reason = "store_error"
    attempts = get_settings().archive_r2_terminal_attempts if terminal else 1
    try:
        for attempt in range(attempts):
            try:
                queued = time.monotonic()
                async with _upload_sem():
                    observation.props["archive_body_queue_wait_ms"] += (time.monotonic() - queued) * 1000
                    transfer = time.monotonic()
                    try:
                        async with asyncio.timeout(get_settings().archive_r2_timeout_s):
                            if _store is None:
                                raise RuntimeError("archive object store unavailable")
                            info = await _store.put(body, content_hash=content_hash)
                            if info != ObjectInfo(content_hash, len(body)):
                                raise ObjectStoreError("hash_mismatch")
                    finally:
                        observation.props["archive_body_upload_ms"] += (time.monotonic() - transfer) * 1000
                observation.props["archive_body_upload_status"] = "uploaded"
                return WritePlan(mode)
            except TimeoutError:
                reason = "timeout"
            except ObjectStoreError as exc:
                reason = exc.reason
            except Exception:
                reason = "store_error"
            if attempt + 1 < attempts:
                await asyncio.sleep(min(0.1 * 2 ** attempt, 1.0))
        observation.props["archive_body_upload_status"] = "failed"
        _log.error("archive body upload failed after %s attempt(s): %s", attempts, reason)
        # Double write preserves the DB copy when R2 fails, without publishing an R2 pointer.
        return WritePlan("db" if mode == "both" else None, reason=reason)
    finally:
        observation.props["archive_body_upload_ms"] = round(observation.props["archive_body_upload_ms"], 3)


def submit(factory, body_len: int, observation: StorageReport) -> str | None:
    """Separate count/byte budgets from archive's DB queue and DB semaphore."""
    global _pending_bytes
    s = get_settings()
    reason = ("upload_queue_full" if len(_pending) >= s.archive_r2_max_pending else
              "upload_bytes_full" if _pending_bytes + body_len > s.archive_r2_max_pending_bytes else None)
    if reason:
        observation.props["archive_body_upload_status"] = "dropped"
        observation.props["archive_body_upload_drop_reason"] = reason
        return reason
    _pending_bytes += body_len

    task = asyncio.create_task(factory())
    _pending.add(task)

    def done(task):
        global _pending_bytes
        _pending.discard(task)
        _pending_bytes -= body_len
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


def read_options(path):
    from sqlalchemy.orm import defer
    from .models import ArchiveSnapshot
    return (defer(ArchiveSnapshot.body),) if getattr(get_settings(), "archive_body_read_" + path) == "r2-first" else ()


async def pointer(session, snapshot, path):
    """Capture metadata only for R2-first; DB fallback is loaded in a later short session."""
    if getattr(get_settings(), "archive_body_read_" + path) == "r2-first":
        return BodyPointer(snapshot.content_hash, snapshot.body_storage, None, None, snapshot.id)
    from .archive import _snapshot_body
    body = await _snapshot_body(session, snapshot)
    return BodyPointer(snapshot.content_hash, snapshot.body_storage, body, None)


async def _db_fallback(pointer):
    from .infra.db import session_maker
    from .models import ArchiveSnapshot
    from .archive import _snapshot_body, _unpack
    if pointer.snapshot_id is None:
        return _unpack(pointer.body, pointer.enc)
    async with session_maker() as session:
        row = await session.get(ArchiveSnapshot, pointer.snapshot_id)
        return await _snapshot_body(session, row) if row is not None else None


async def read(pointer: BodyPointer, path: str, *, diagnostics: dict | None = None) -> bytes | None:
    """Call only after closing every DB session owned by the request."""
    reason, elapsed = "none", 0.0
    def observed(body, source):
        if diagnostics is not None:
            diagnostics.update(cache_body_source=source, cache_body_fallback_reason=reason,
                               cache_r2_read_ms=elapsed)
        return body

    if (getattr(get_settings(), f"archive_body_read_{path}") == "r2-first"
            and pointer.storage in ("both", "r2")):
        started = time.monotonic()
        try:
            async with asyncio.timeout(get_settings().archive_r2_read_timeout_s):
                if _store is None:
                    raise ObjectStoreError("store_unavailable")
                body = await _store.get(pointer.content_hash)
                if body is None:
                    reason = "not_found"
                else:
                    elapsed = round((time.monotonic() - started) * 1000, 3)
                    return observed(body, "r2")
        except TimeoutError:
            reason = "timeout"
        except PermissionError:
            reason = "permission_denied"
        except ObjectStoreError as exc:
            reason = exc.reason if exc.reason in {
                "not_found", "timeout", "permission_denied", "hash_mismatch", "too_large",
                "store_unavailable", "store_error"} else "store_error"
        except Exception:
            reason = "store_error"
        elapsed = round((time.monotonic() - started) * 1000, 3)
        outcomes["read_fallback_" + path] += 1
        outcomes["read_fallback_" + path + "_" + reason] += 1
        level = logging.ERROR if reason in {"permission_denied", "hash_mismatch", "too_large"} else logging.WARNING
        _log.log(level, "archive R2 read fallback path=%s reason=%s elapsed_ms=%s", path, reason, elapsed)
    body = await _db_fallback(pointer)
    return observed(body, "db" if body is not None else "none")
