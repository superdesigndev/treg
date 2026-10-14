"""Local, bounded diagnostics for money transactions. Never issue SQL or perform I/O.

SQLAlchemy supplies connections only after checkout. Their driver PID is already known to asyncpg;
reading it does not query PostgreSQL. Times describe client-observed transaction/statement spans,
not server lock duration. Only the runner may turn drained events into logs or network traffic.
"""

from __future__ import annotations

import asyncio
import functools
import itertools
import re
import sys
import threading
import time
import weakref
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import event
from sqlalchemy.orm import Session

from .money_timing import PROCESS_INSTANCE

SLOW_TXN_SECONDS = 1.0
SAMPLE_REPEAT_SECONDS = 5.0
MAX_ACTIVE = 512
MAX_QUEUED = 1024
MAX_IDENTITIES = 16
MAX_STAGES = 24
_KEY = "treg_money_trace"
_CONNECTION_KEY = "treg_money_trace_transaction"
_PACKAGE_PREFIX = __file__.replace("\\", "/").rsplit("/", 2)[0] + "/"
_clock = time.monotonic
_utcnow = lambda: datetime.now(timezone.utc)
_lock = threading.RLock()
_enabled = True
_serial = itertools.count(1)
_active: dict[str, _Txn] = {}
_queue: deque[dict] = deque()
_engines: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_connections: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_session_hooks_installed = False
_dropped_active_total = 0
_dropped_events_total = 0
_diagnostic_errors_total = 0
_tracked_transactions_total = 0
_ended_transactions_total = 0
_reported_drops = (0, 0, 0)


def _safe(fn):
    @functools.wraps(fn)
    def guarded(*args, **kwargs):
        global _diagnostic_errors_total
        try:
            return fn(*args, **kwargs)
        except Exception:  # noqa: BLE001 - diagnostics must never replace business behavior
            _diagnostic_errors_total += 1
            return None
    return guarded


def _label(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,47}", value) else None


def _call_id(value) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", value) else None


def _stamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


def _bounded_add(values: list, value, limit: int = MAX_IDENTITIES) -> bool:
    if value is None or value in values:
        return False
    if len(values) >= limit:
        return True
    values.append(value)
    return False


@dataclass
class _State:
    # This is session-local metadata, including a transaction that began before mark_money.
    transactions: dict[int, _Txn] = field(default_factory=dict)
    operation: str | None = None
    org_id: int | None = None
    call_id: str | None = None
    batch_size: int | None = None
    stages: list[tuple[int, str, float]] = field(default_factory=list)
    committing: bool = False
    flushing: bool = False
    mark_location: dict | None = None
    stage_location: dict | None = None
    operations: list[str] = field(default_factory=list)
    org_ids: list[int] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)
    identities_truncated: bool = False


@dataclass
class _Txn:
    app_txn_id: str
    root_id: int
    backend_pid: int | None
    pool: str
    started: float
    started_at: datetime
    connection_info: dict
    operation: str | None = None
    operations: list[str] = field(default_factory=list)
    org_ids: list[int] = field(default_factory=list)
    call_ids: list[str] = field(default_factory=list)
    identities_truncated: bool = False
    batch_size: int | None = None
    current_stage: str = "between_stages"
    stage_started: float = 0.0
    stage_started_at: datetime | None = None
    stage_totals: dict[str, float] = field(default_factory=dict)
    stage_history: deque = field(default_factory=lambda: deque(maxlen=MAX_STAGES))
    stage_history_dropped: int = 0
    sql_started: float | None = None
    sql_started_at: datetime | None = None
    last_sql_finished: float | None = None
    last_sql_finished_at: datetime | None = None
    sql_count: int = 0
    sql_total: float = 0.0
    sql_max: float = 0.0
    sqlstate: str | None = None
    outcome: str = "open"
    last_sample: float | None = None
    registered: bool = False
    task_ref: object | None = None
    mark_location: dict | None = None
    stage_location: dict | None = None
    await_chain: list[dict] = field(default_factory=list)
    await_chain_captured_at: str | None = None
    control_inflight: str | None = None
    control_started: float | None = None
    control_elapsed: float = 0.0
    connection_ref: object | None = None
    finished: float | None = None
    finished_at: datetime | None = None
    end_emitted: bool = False


def _location(frame) -> dict | None:
    path = frame.f_code.co_filename.replace("\\", "/")
    if not path.startswith(_PACKAGE_PREFIX):
        return None
    suffix = path[len(_PACKAGE_PREFIX):]
    if suffix == "infra/money_trace.py":
        return None
    return {"file": f"src/treg/{suffix}", "function": frame.f_code.co_name,
            "line": frame.f_lineno}


def _caller_location() -> dict | None:
    frame = sys._getframe(1)
    try:
        for _ in range(12):
            if frame is None:
                return None
            if (location := _location(frame)) is not None:
                return location
            frame = frame.f_back
    finally:
        del frame
    return None


def _task_ref():
    try:
        task = asyncio.current_task()
        return weakref.ref(task) if task is not None else None
    except RuntimeError:
        return None


def _capture_await_chain(txn: _Txn) -> None:
    """Inspect only live coroutine frame locations, never locals/source/arguments or task results."""
    task = txn.task_ref() if txn.task_ref is not None else None
    if task is None or task.done():
        return
    current = task.get_coro()
    rows = []
    # Bound both traversed objects and emitted project frames. SQLAlchemy greenlets can break
    # this chain; an empty/short chain is partial evidence, not a complete Python stack trace.
    for _ in range(32):
        if current is None or len(rows) >= 12:
            break
        frame = getattr(current, "cr_frame", None) or getattr(current, "gi_frame", None) or getattr(current, "ag_frame", None)
        if frame is not None and (location := _location(frame)) is not None:
            rows.append(location)
        current = getattr(current, "cr_await", None) or getattr(current, "gi_yieldfrom", None) or getattr(current, "ag_await", None)
    txn.await_chain = rows
    txn.await_chain_captured_at = _stamp(_utcnow())


def _state(db) -> _State:
    session = getattr(db, "sync_session", db)
    state = session.info.get(_KEY)
    if state is None:
        state = _State()
        session.info[_KEY] = state
    return state


def _effective_stage(state: _State) -> str:
    if state.flushing:
        return "flush"
    if state.committing:
        return "commit"
    return state.stages[-1][1] if state.stages else "between_stages"


def _set_stage(txn: _Txn, stage: str, now: float) -> None:
    if txn.finished is not None or stage == txn.current_stage:
        return
    elapsed = max(0.0, now - txn.stage_started)
    key = txn.current_stage if txn.current_stage in txn.stage_totals or len(txn.stage_totals) < MAX_STAGES else "other"
    txn.stage_totals[key] = txn.stage_totals.get(key, 0.0) + elapsed
    if len(txn.stage_history) == MAX_STAGES:
        txn.stage_history_dropped += 1
    txn.stage_history.append({"stage": txn.current_stage,
                              "started_at": _stamp(txn.stage_started_at),
                              "elapsed_ms": round(elapsed * 1000, 3)})
    txn.current_stage = stage
    txn.stage_started = now
    txn.stage_started_at = _utcnow()


def _sync_stages(state: _State) -> None:
    now = _clock()
    for txn in state.transactions.values():
        _set_stage(txn, _effective_stage(state), now)
        txn.stage_location = state.stage_location


def _register(txn: _Txn) -> None:
    global _dropped_active_total, _tracked_transactions_total
    if txn.registered or txn.operation is None or txn.finished is not None:
        return
    _tracked_transactions_total += 1
    # Never evict a possible blocker to retain a newer waiter.
    if len(_active) >= MAX_ACTIVE:
        _dropped_active_total += 1
        txn.registered = True
        return
    txn.registered = True
    _active[txn.app_txn_id] = txn


def _enrich(txn: _Txn, state: _State) -> None:
    if txn.finished is not None:
        return
    if txn.operation is None:
        txn.operation = state.operations[0] if state.operations else state.operation
    txn.identities_truncated |= state.identities_truncated
    for attribute in ("operations", "org_ids", "call_ids"):
        for value in getattr(state, attribute):
            txn.identities_truncated |= _bounded_add(getattr(txn, attribute), value)
    txn.batch_size = state.batch_size
    txn.mark_location = state.mark_location
    txn.task_ref = _task_ref()
    _register(txn)


@_safe
def mark_money(db, operation: str | None = None, *, org_id: int | None = None,
               call_id: str | None = None, batch_size: int | None = None) -> None:
    """Mark a session without acquiring a connection. None only enriches known identity.

    Call at each money entry, and again after an independently committing nested money operation
    when the caller resumes its own work. Each transaction keeps all observed identities, bounded.
    """
    if not _enabled:
        return
    with _lock:
        state = _state(db)
        state.mark_location = _caller_location()
        if (label := _label(operation)) is not None:
            state.operation = label
            state.identities_truncated |= _bounded_add(state.operations, label)
        if isinstance(org_id, int) and not isinstance(org_id, bool):
            state.org_id = org_id
            state.identities_truncated |= _bounded_add(state.org_ids, org_id)
        if (opaque := _call_id(call_id)) is not None:
            state.call_id = opaque
            state.identities_truncated |= _bounded_add(state.call_ids, opaque)
        if isinstance(batch_size, int) and batch_size >= 0:
            state.batch_size = batch_size
        for txn in state.transactions.values():
            _enrich(txn, state)


class _MoneyStage:
    def __init__(self, db, stage):
        self.db, self.stage = db, stage
        self.token = None

    @_safe
    def _enter(self):
        if not _enabled or (stage := _label(self.stage)) is None:
            return
        with _lock:
            state = _state(self.db)
            if len(state.stages) >= MAX_STAGES:
                return
            self.token = next(_serial)
            state.stage_location = _caller_location()
            state.stages.append((self.token, stage, _clock()))
            _sync_stages(state)

    def __enter__(self):
        self._enter()
        return self

    @_safe
    def _exit(self, exc):
        if self.token is None:
            return
        with _lock:
            state = _state(self.db)
            if isinstance(exc, asyncio.CancelledError):
                for txn in state.transactions.values():
                    txn.outcome = "cancelled"
            state.stages[:] = [row for row in state.stages if row[0] != self.token]
            state.stage_location = _caller_location()
            _sync_stages(state)

    def __exit__(self, _exc_type, exc, _tb):
        self._exit(exc)
        return False


def money_stage(db, stage: str) -> _MoneyStage:
    """A local stage scope. On exit, restore its parent or the between-stages state."""
    return _MoneyStage(db, stage)


@_safe
def _after_begin(session, transaction, connection):
    if not _enabled or transaction.parent is not None or connection.engine not in _engines:
        return
    with _lock:
        state = _state(session)
        info = connection.info  # existing checked-out connection; never session.connection()
        driver = connection.connection.driver_connection
        get_pid = getattr(driver, "get_server_pid", None)
        pid = get_pid() if get_pid is not None else None
        now = _clock()
        txn = _Txn(f"{PROCESS_INSTANCE}:{next(_serial)}", id(transaction), pid,
                   _engines[connection.engine], now, _utcnow(), info,
                   stage_started=now, stage_started_at=_utcnow())
        txn.task_ref = _task_ref()
        txn.connection_ref = weakref.ref(connection)
        txn.mark_location = state.mark_location
        txn.stage_location = state.stage_location
        state.transactions[id(connection)] = txn
        info[_CONNECTION_KEY] = txn
        _connections[connection] = txn
        _set_stage(txn, _effective_stage(state), now)
        if state.operation:
            _enrich(txn, state)


def _connection_txn(connection) -> _Txn | None:
    # Do not access Connection.info here: after invalidation it can revalidate/reconnect. Capture
    # that dictionary once in after_begin and use an in-memory weak mapping in all later hooks.
    return _connections.get(connection)


@_safe
def _before_sql(connection, _cursor, _statement, _parameters, _context, _executemany):
    if not _enabled:
        return
    with _lock:
        txn = _connection_txn(connection)
        if txn is not None:
            txn.sql_started = _clock()
            txn.sql_started_at = _utcnow()
            txn.sql_count += 1


def _finish_sql(txn: _Txn, now: float) -> None:
    if txn.sql_started is not None:
        elapsed = max(0.0, now - txn.sql_started)
        txn.sql_total += elapsed
        txn.sql_max = max(txn.sql_max, elapsed)
        txn.sql_started = None
        txn.sql_started_at = None
        txn.last_sql_finished = now
        txn.last_sql_finished_at = _utcnow()


@_safe
def _after_sql(connection, _cursor, _statement, _parameters, _context, _executemany):
    with _lock:
        txn = _connection_txn(connection)
        if txn is not None:
            _finish_sql(txn, _clock())


@_safe
def _handle_error(context):
    if context.connection is None:
        return
    with _lock:
        txn = _connection_txn(context.connection)
        if txn is not None:
            _finish_sql(txn, _clock())
            original = context.original_exception
            code = getattr(original, "sqlstate", None)
            if isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{5}", code):
                txn.sqlstate = code
            if isinstance(original, asyncio.CancelledError):
                txn.outcome = "cancelled"
            elif context.is_disconnect:
                txn.outcome = "invalidated"


@_safe
def _before_commit(session):
    if not _enabled or session.get_nested_transaction() is not None:
        return
    with _lock:
        state = _state(session)
        state.committing = True
        _sync_stages(state)


@_safe
def _before_flush(session, _context, _instances):
    if not _enabled:
        return
    with _lock:
        state = _state(session)
        state.flushing = True
        _sync_stages(state)


@_safe
def _after_flush(session, _context):
    if not _enabled:
        return
    with _lock:
        state = _state(session)
        state.flushing = False
        _sync_stages(state)


@_safe
def _after_commit(session):
    if not _enabled or session.get_nested_transaction() is not None:
        return
    with _lock:
        for txn in _state(session).transactions.values():
            txn.outcome = "committed"
            _finish_control(txn)
            _confirmed_end(txn)


@_safe
def _after_rollback(session):
    if not _enabled:
        return
    with _lock:
        state = _state(session)
        # An unsuccessful flush has no after_flush_postexec. A SAVEPOINT rollback ends that flush
        # without ending its parent transaction; future lookups must not remain labelled flush.
        state.flushing = False
        if session.get_nested_transaction() is not None:
            _sync_stages(state)
            return
        state.committing = False
        for txn in state.transactions.values():
            if txn.outcome in {"open", "rollback_requested"}:
                txn.outcome = "rolled_back"
            _finish_control(txn)
            _confirmed_end(txn)
        _sync_stages(state)


def _finish_control(txn: _Txn) -> None:
    if txn.control_started is not None:
        txn.control_elapsed += max(0.0, _clock() - txn.control_started)
    txn.control_started = None
    txn.control_inflight = None


def _confirmed_end(txn: _Txn) -> None:
    # A failed ORM flush can roll back the database while its SessionTransaction remains inactive
    # until the caller explicitly rolls back/closes it. It must no longer look like a lock holder.
    _active.pop(txn.app_txn_id, None)
    if txn.finished is None:
        txn.finished = _clock()
        txn.finished_at = _utcnow()
    # Publish the confirmed database end even if a failed Session waits indefinitely for cleanup.
    # The later SessionTransaction end may retry a failed diagnostic, but cannot emit it twice.
    if _enabled and txn.operation is not None:
        _queue_end(txn)


@_safe
def _commit_requested(connection):
    with _lock:
        txn = _connection_txn(connection)
        if txn is not None:
            txn.control_inflight = "commit"
            txn.control_started = _clock()
            _set_stage(txn, "commit", txn.control_started)


@_safe
def _rollback_requested(connection):
    with _lock:
        txn = _connection_txn(connection)
        if txn is not None:
            if txn.outcome == "open":
                txn.outcome = "rollback_requested"
            txn.control_inflight = "rollback"
            txn.control_started = _clock()
            _set_stage(txn, "rollback", txn.control_started)


@_safe
def _invalidated(_connection, record, _exception):
    with _lock:
        txn = record.info.get(_CONNECTION_KEY)
        if txn is not None:
            if txn.outcome != "cancelled":
                txn.outcome = "invalidated"
            _active.pop(txn.app_txn_id, None)


def _payload(txn: _Txn, kind: str, now: float, *, ended_at=None) -> dict:
    totals = dict(txn.stage_totals)
    totals[txn.current_stage] = totals.get(txn.current_stage, 0.0) + max(0.0, now - txn.stage_started)
    return {
        "event": kind, "process_instance": PROCESS_INSTANCE,
        "backend_pid": txn.backend_pid, "app_txn_id": txn.app_txn_id, "pool": txn.pool,
        "operation": txn.operation, "operations": list(txn.operations),
        "org_ids": list(txn.org_ids), "call_ids": list(txn.call_ids),
        "identities_truncated": txn.identities_truncated, "batch_size": txn.batch_size,
        "txn_started_at": _stamp(txn.started_at),
        "txn_ended_at": _stamp(ended_at) if ended_at is not None else None,
        "elapsed_ms": round(max(0.0, now - txn.started) * 1000, 3),
        "current_stage": txn.current_stage, "stage_started_at": _stamp(txn.stage_started_at),
        "stage_elapsed_ms": round(max(0.0, now - txn.stage_started) * 1000, 3),
        "stage_totals_ms": {key: round(value * 1000, 3) for key, value in totals.items()},
        "stage_history": list(txn.stage_history), "stage_history_dropped": txn.stage_history_dropped,
        "sql_inflight": txn.sql_started is not None,
        "transaction_control_inflight": txn.control_inflight,
        "transaction_control_ms": round((txn.control_elapsed + (
            max(0.0, now - txn.control_started) if txn.control_started is not None else 0)) * 1000, 3),
        "sql_started_at": _stamp(txn.sql_started_at) if txn.sql_started_at is not None else None,
        "sql_elapsed_ms": (round(max(0.0, now - txn.sql_started) * 1000, 3)
                           if txn.sql_started is not None else 0),
        "sql_count": txn.sql_count, "sql_total_ms": round(txn.sql_total * 1000, 3),
        "sql_max_ms": round(txn.sql_max * 1000, 3),
        "last_sql_finished_at": (_stamp(txn.last_sql_finished_at)
                                 if txn.last_sql_finished_at is not None else None),
        "gap_since_sql_ms": (round(max(0.0, now - txn.last_sql_finished) * 1000, 3)
                             if txn.sql_started is None and txn.control_inflight is None
                             and txn.last_sql_finished is not None else None),
        "outcome": txn.outcome, "sqlstate": txn.sqlstate,
        "last_mark_location": txn.mark_location, "stage_location": txn.stage_location,
        "await_chain": list(txn.await_chain),
        "await_chain_captured_at": txn.await_chain_captured_at,
        "await_chain_partial": True,
    }


def _enqueue(payload: dict) -> None:
    global _dropped_events_total
    if len(_queue) >= MAX_QUEUED:
        _dropped_events_total += 1
    else:
        _queue.append(payload)


@_safe
def _after_end(session, transaction):
    global _ended_transactions_total
    if transaction.parent is not None:
        return
    with _lock:
        state = session.info.get(_KEY)
        if state is None:
            return
        for key, txn in list(state.transactions.items()):
            if txn.root_id != id(transaction):
                continue
            # Keep no connection/session/exception object in the output queue.
            _active.pop(txn.app_txn_id, None)
            state.transactions.pop(key, None)
            connection = txn.connection_ref() if txn.connection_ref is not None else None
            if connection is not None and _connections.get(connection) is txn:
                _connections.pop(connection, None)
            if txn.connection_info.get(_CONNECTION_KEY) is txn:
                txn.connection_info.pop(_CONNECTION_KEY, None)
            if txn.operation is not None:
                _ended_transactions_total += 1
            if txn.outcome in {"open", "rollback_requested"}:
                txn.outcome = "rolled_back"
            # Cleanup precedes diagnostics: a broken clock/serializer must not retain a completed
            # transaction in the active registry or attach it to the next borrower of this PID.
            if _enabled and txn.operation is not None:
                _queue_end(txn)
        state.committing = state.flushing = False
        state.operation = state.org_id = state.call_id = state.batch_size = None
        state.mark_location = None
        state.operations.clear()
        state.org_ids.clear()
        state.call_ids.clear()
        state.identities_truncated = False


@_safe
def _queue_end(txn: _Txn) -> None:
    if txn.end_emitted:
        return
    now = txn.finished if txn.finished is not None else _clock()
    _finish_control(txn)
    if now - txn.started >= SLOW_TXN_SECONDS or txn.last_sample is not None:
        _enqueue(_payload(txn, "money_txn_end", now, ended_at=txn.finished_at or _utcnow()))
    txn.end_emitted = True


@_safe
def sample() -> None:
    """Called by the runner, normally once a second; only append bounded in-memory snapshots."""
    if not _enabled:
        return
    with _lock:
        now = _clock()
        for txn in _active.values():
            if now - txn.started < SLOW_TXN_SECONDS:
                continue
            if txn.last_sample is not None and now - txn.last_sample < SAMPLE_REPEAT_SECONDS:
                continue
            _capture_await_chain(txn)
            _enqueue(_payload(txn, "money_txn_slow", now))
            txn.last_sample = now


def drain_events() -> list[dict]:
    """Take the local queue. Caller owns logging/network I/O after returning from this function."""
    global _reported_drops
    try:
        with _lock:
            rows = list(_queue)
            _queue.clear()
            totals = (_dropped_active_total, _dropped_events_total, _diagnostic_errors_total)
            if totals != _reported_drops:
                rows.append({"event": "money_trace_dropped", "process_instance": PROCESS_INSTANCE,
                             "dropped_active": totals[0] - _reported_drops[0],
                             "dropped_events": totals[1] - _reported_drops[1],
                             "diagnostic_errors": totals[2] - _reported_drops[2]})
                _reported_drops = totals
            return rows
    except Exception:  # noqa: BLE001
        return []


def shutdown_events() -> list[dict]:
    sample()
    return drain_events()


def stats() -> dict:
    with _lock:
        return {"enabled": _enabled, "active": len(_active), "queued": len(_queue),
                "dropped_active_total": _dropped_active_total,
                "dropped_events_total": _dropped_events_total,
                "diagnostic_errors_total": _diagnostic_errors_total,
                "tracked_transactions_total": _tracked_transactions_total,
                "ended_transactions_total": _ended_transactions_total}


@_safe
def configure(*, enabled: bool = True) -> None:
    global _enabled
    with _lock:
        _enabled = enabled
        if not enabled:
            _active.clear()
            _queue.clear()


@_safe
def install_engine(engine, *, pool_name: str = "api") -> None:
    """Install only synchronous metadata callbacks; this does not open a database connection."""
    global _session_hooks_installed
    engine = getattr(engine, "sync_engine", engine)
    if engine in _engines:
        return
    _engines[engine] = _label(pool_name) or "other"
    event.listen(engine, "before_cursor_execute", _before_sql)
    event.listen(engine, "after_cursor_execute", _after_sql)
    event.listen(engine, "handle_error", _handle_error)
    event.listen(engine, "commit", _commit_requested)
    event.listen(engine, "rollback", _rollback_requested)
    event.listen(engine.pool, "invalidate", _invalidated)
    if not _session_hooks_installed:
        for name, callback in (
            ("after_begin", _after_begin), ("before_commit", _before_commit),
            ("after_commit", _after_commit), ("after_rollback", _after_rollback),
            ("after_transaction_end", _after_end), ("before_flush", _before_flush),
            ("after_flush_postexec", _after_flush),
        ):
            event.listen(Session, name, callback)
        _session_hooks_installed = True


def _reset_for_tests() -> None:
    """Reset process-local diagnostics only. Tests must first close their sessions."""
    global _enabled, _dropped_active_total, _dropped_events_total, _diagnostic_errors_total, _reported_drops
    global _tracked_transactions_total, _ended_transactions_total
    with _lock:
        _active.clear()
        _queue.clear()
        _connections.clear()
        _enabled = True
        _dropped_active_total = _dropped_events_total = _diagnostic_errors_total = 0
        _tracked_transactions_total = _ended_transactions_total = 0
        _reported_drops = (0, 0, 0)
