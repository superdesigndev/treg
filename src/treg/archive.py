"""Versioned platform responses, result admission, adaptive TTL and cache serving.

History and cache share exact provider bytes but have different eligibility rules. Endpoints
with verified hit/miss rules can serve only confirmed useful results; explicit empty results invalidate it. Errors and unknown
results preserve the previous decisive observation without renewing its freshness. Endpoints
without verified hit/miss rules retain the original cache and learning behavior. Response
history and byte deduplication remain independent of result usefulness.

Recording observes already-buffered metered platform calls under the catalog retention policy.
Own-key and own-tool streams are untouched. Async terminal JSON is mandatory settlement evidence,
not cache evidence. This module never changes balances or the upstream response.

Modes: off disables recording and serving; shadow records and learns; serve additionally permits
fresh eligible answers behind the endpoint allowlist and team cohort gate. Background recording
is bounded and best-effort. Cache hits do not create new observations or learning evidence.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from collections import Counter
from urllib.parse import parse_qsl

from .config import get_settings
from . import archive_bodies

# ---------------------------------------------------------------------------------------------
# Mode

MODES = ("off", "shadow", "serve")


def mode() -> str:
    """The archive's runtime mode. Any unrecognized value degrades to "off": a typo in an env var
    must disable the feature, never accidentally enable serving."""
    m = (get_settings().archive_mode or "off").strip().lower()
    return m if m in MODES else "off"


def recording() -> bool:
    return mode() in ("shadow", "serve")


def serving() -> bool:
    return mode() == "serve"



def serve_endpoints() -> set[str]:
    return {value.strip() for value in get_settings().archive_serve_endpoints.split(",")
            if value.strip()}


def rollout_reason(endpoint_id: str, cohort: str) -> str:
    if endpoint_id not in serve_endpoints():
        return "endpoint_disabled"
    percent = get_settings().archive_serve_percent
    if not 0 < percent <= 100:
        return "rollout_disabled"
    if not cohort:
        return "missing_cohort"
    bucket = int(hashlib.sha256(f"{endpoint_id}:{cohort}".encode()).hexdigest()[:8], 16) % 100
    return "selected" if bucket < percent else "control"


# ---------------------------------------------------------------------------------------------
# Eligibility (gates 1 + 2; gate 3 — the tier — is the caller's context and is checked at the
# hook site, where "metered platform call" is already an established fact)

# The catalog's per-entry cache policy values. Absent/unknown ⇒ "forbidden" (see policy()).
CACHE_FORBIDDEN = "forbidden"   # license forbids storing, or nobody has judged it yet
CACHE_TRANSIENT = "transient"   # short-lived cache only; old versions are prunable
CACHE_ARCHIVE = "archive"       # keep versions long-term (public-domain and license-cleared)

_STORABLE = (CACHE_TRANSIENT, CACHE_ARCHIVE)


def policy(entry: dict[str, Any] | None) -> str:
    """Gates 1+2 for one catalog entry, returning the effective cache policy.

    `entry` is the endpoint's catalog mapping (the same dict the resolver already holds). Two
    branches are non-negotiable whatever the default says: a missing entry or an ACTION is never
    stored, and a JUDGED forbidden (a licence that was read and says no) is always respected.
    An UNJUDGED entry takes `archive_default_policy` — "transient" since the founder's 2026-08-29
    keep-all decision, flippable back to "forbidden" by env without a deploy."""
    if not entry:
        return CACHE_FORBIDDEN
    if entry.get("kind") == "action":  # gate 1 — never store an action's answer
        return CACHE_FORBIDDEN
    declared = entry.get("cache")
    if isinstance(declared, dict):  # provenance form: {mode, license_quote, source_url, checked}
        declared = declared.get("mode")
    if declared in _STORABLE:  # gate 2 — an explicit, judged license decision
        return str(declared)
    if declared == CACHE_FORBIDDEN:  # judged and refused — always respected
        return CACHE_FORBIDDEN
    default = (get_settings().archive_default_policy or "").strip().lower()
    return CACHE_TRANSIENT if default == CACHE_TRANSIENT else CACHE_FORBIDDEN


def storable(entry: dict[str, Any] | None) -> bool:
    return policy(entry) in _STORABLE


# ---------------------------------------------------------------------------------------------
# The cache key



def cache_key(
    method: str,
    endpoint_id: str,
    upstream_url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> str:
    """One deterministic key per logical request: sha256 over the canonical request shape.

    Canonical means: uppercased method, the catalog endpoint id (so a provider's URL reshuffle
    starts a fresh history instead of poisoning the old one), the URL with its query pairs
    SORTED (param order is transport noise, not meaning), a hash of the body bytes, and the few
    caller headers that can change a vendor's answer (Accept, Accept-Language) — everything else
    (auth, cookies, tracing, encodings) is excluded by the allow-list below: headers vary per
    caller/hop without changing what the vendor computes, and keying on them would shatter one
    logical answer into many dead entries. Credentials never appear at all — injection happens
    after the key is taken.

    POST bodies hash canonically when they parse as JSON (sorted keys, separators pinned), raw
    otherwise: two JSON bodies that differ only in key order are the same question.
    """
    base, _, query = upstream_url.partition("?")
    pairs = sorted(parse_qsl(query, keep_blank_values=True))
    kept_headers = sorted(
        (k.lower(), v.strip())
        for k, v in (headers or {}).items()
        if k.lower() in ("accept", "accept-language") and v.strip()
    )
    material = json.dumps(
        {
            "m": method.upper(),
            "e": endpoint_id,
            "u": base,
            "q": pairs,
            "b": _body_digest(body),
            "h": kept_headers,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _body_digest(body: bytes | None) -> str:
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return hashlib.sha256(body).hexdigest()
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


# ---------------------------------------------------------------------------------------------
# Body compression (zlib, stdlib). Measured on 40 real prod bodies (2026-09-03): 5.2x at
# 68 MB/s compress / 1 GB/s decompress — the write path averages under 1 MB/s, so the cost is
# invisible and ~6.5 GB/day of JSON becomes ~1.25 GB/day. The exact ORIGINAL bytes come back on
# every read: hashes, dedup, and the verbatim-relay promise all operate on raw bytes only.

_COMPRESS_MIN_BYTES = 256  # below this, zlib overhead can exceed the win — store raw


def _pack(body: bytes) -> tuple[bytes, str | None]:
    """(stored_bytes, encoding): zlib-compressed when it actually shrinks, else raw with None."""
    if len(body) < _COMPRESS_MIN_BYTES:
        return body, None
    import zlib
    packed = zlib.compress(body, 6)
    return (packed, "zlib") if len(packed) < len(body) else (body, None)


def _unpack(body: bytes | None, enc: str | None) -> bytes | None:
    if body is None or not enc:
        return body
    import zlib
    return zlib.decompress(body)


def content_hash(body: bytes) -> str:
    """The stored body's identity, for deduplication across versions: same bytes, one blob.
    (Change DETECTION will strip noisy fields before comparing — that arrives with the learner in
    a later PR and never alters what is stored: bytes are kept verbatim, always.)"""
    return hashlib.sha256(body).hexdigest()


# ---------------------------------------------------------------------------------------------
# The recorder (PR 2) — fire-and-forget, audit's discipline: bounded, swallowed, drainable.
# A recording hiccup must NEVER surface into a call's result, and a burst must never OOM the
# server. Unlike audit there is no shed-counter subtlety: a dropped snapshot is one lost sample
# in a statistics stream that the next identical call re-supplies.

import asyncio
import logging
import weakref
from datetime import datetime, timedelta, timezone

# Losing a recording is ERROR, not WARNING: the fault handler starts at ERROR, so anything below it
# reaches stdout and nowhere else. Degradations that cost nothing (a lookup miss serving live, a
# retried pass) stay at WARNING — the line is whether data was actually lost.
_log = logging.getLogger("treg.archive")
_pending: set[asyncio.Task] = set()
_MAX_PENDING = 512
# Memory bound: each pending task holds its body bytes in a closure. 512 tasks × 8 MB = 4 GB in the
# worst case — the 2026-09-07 OOM. This cap sheds recordings when total pending body bytes exceeds
# the threshold, BEFORE the task count would shed them. 256 MB is generous for a 4 GB container and
# still allows ~128 concurrent recordings of typical 2 MB bodies.
_MAX_PENDING_BYTES = 256 * 1024 * 1024
_pending_bytes = 0
# At most this many recordings TOUCH THE DATABASE at once (audit's discipline, and its exact
# loop-bound pattern). Without it a traffic burst put up to 512 concurrent short sessions in
# front of the API's 15-slot pool — SToneX's pool-pressure report, 2026-09-03. Queued recordings
# wait INSIDE their task; the caller's response left long ago either way. Two, not four: every
# slot here is paid twice (two uvicorn workers) and again at every deploy against the database's
# 103-connection ceiling, and a recording is one INSERT of a body that is already in memory.
_MAX_CONCURRENT_WRITES = 2

_sem: asyncio.Semaphore | None = None
_sem_loop = None
_key_locks: weakref.WeakValueDictionary[str, asyncio.Lock] | None = None
_key_locks_loop = None


def _get_sem() -> asyncio.Semaphore:
    """A semaphore bound to the CURRENT running loop (recreated if the loop changed — tests)."""
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem = asyncio.Semaphore(_MAX_CONCURRENT_WRITES)
        _sem_loop = loop
    return _sem


def _get_key_lock(key_hash: str) -> asyncio.Lock:
    """Exact per-key serialization without retaining inactive keys forever."""
    global _key_locks, _key_locks_loop
    loop = asyncio.get_running_loop()
    if _key_locks is None or _key_locks_loop is not loop:
        _key_locks = weakref.WeakValueDictionary()
        _key_locks_loop = loop
    lock = _key_locks.get(key_hash)
    if lock is None:
        lock = asyncio.Lock()
        _key_locks[key_hash] = lock
    return lock


# A recording is best-effort by contract (audit's discipline: shed, never wedge). A database that
# does not answer in this window — a lock, a stuck pool slot, a dying connection — costs ONE
# dropped sample, which the next identical call re-supplies; it must never hold drain(), a test,
# or a shutdown hostage. CI's serial Postgres job hung exactly that way three times before this
# bound existed, at whichever drain() happened to gather the stuck task.
_STORE_TIMEOUT_S = 30
_CHANGE_TIMEOUT_S = 3
change_outcomes: Counter[str] = Counter()


def _utcnow() -> datetime:
    # Naive UTC, matching every models.py datetime column (see models._now).
    return datetime.now(timezone.utc).replace(tzinfo=None)


def key_url(upstream_url: str, query_items: list[tuple[str, str]], exclude: set[str]) -> str:
    """The URL as the vendor effectively sees it, before credential injection: the resolved
    upstream (fixed query included) plus the caller's forwarded params — minus `exclude`, the
    resolution-consumed names the relay drops. Order does not matter; cache_key sorts."""
    from urllib.parse import urlencode
    q = urlencode([(k, v) for k, v in query_items if k not in exclude])
    if not q:
        return upstream_url
    return f"{upstream_url}&{q}" if "?" in upstream_url else f"{upstream_url}?{q}"


def _write_plan(endpoint_id: str, body: bytes, origin: str) -> archive_bodies.WritePlan:
    from .domain.catalog import store as catalog_store
    keep = ((origin == "async_terminal" or storable(catalog_store.load().by_id.get(endpoint_id)))
            and len(body) <= get_settings().archive_max_body_bytes)
    return archive_bodies.WritePlan(get_settings().archive_body_write if keep else None,
                                    reason=None if keep else "policy_or_size")


def record(
    *,
    method: str,
    endpoint_id: str,
    provider: str,
    url: str,
    caller_body: bytes,
    headers: dict[str, str],
    status_code: int,
    media_type: str,
    body: bytes,
    origin: str = "caller",
    observation: archive_bodies.StorageReport | None = None,
) -> tuple[str, str]:
    """Schedule one observation of a metered platform answer. Returns immediately; the write runs
    off-request on its own session. Call sites gate on `recording()` and 2xx — this function
    trusts them and never raises.

    Returns `(key_hash, content_hash)` — the identities of the question and of this exact
    answer. They are what the audit row keeps so a call can later be joined back to its stored
    bytes (`/calls/{id}/result`). Computed here rather than in `_store` so they are computed
    ONCE (the store reuses them) and are true whether or not the write lands: a shed recording
    still names the answer the caller received."""
    global _pending_bytes
    kh = cache_key(method, endpoint_id, url, caller_body, headers)
    ch = content_hash(body)
    observation = observation or archive_bodies.StorageReport()
    plan = _write_plan(endpoint_id, body, origin)
    if plan.storage in ("both", "r2"):
        rejection = archive_bodies.submit(lambda: _store(
            method=method, endpoint_id=endpoint_id, provider=provider, url=url,
            caller_body=caller_body, headers=headers, status_code=status_code,
            media_type=media_type, body=body, origin=origin, key_hash=kh, body_hash=ch,
            observation=observation, plan=plan), len(body), observation, content_hash=ch)
        if rejection is None:
            return kh, ch
        if rejection != "duplicate":
            plan = archive_bodies.WritePlan("db" if plan.keep_db else None, reason=rejection)
        # Duplicates use the existing bounded DB queue and still join prepare's shared upload.
        # Both mode preserves the DB copy when admission of a distinct upload is rejected.

    body_len = len(body)
    # Shed on EITHER count OR bytes — whichever bound bites first. The bytes bound prevents OOM
    # when a few large bodies queue while the semaphore is full; the count bound is the legacy
    # backstop for many small bodies (archive_max_body_bytes is 2 MB, so 512 × 2 MB = 1 GB).
    if len(_pending) >= _MAX_PENDING or _pending_bytes + body_len > _MAX_PENDING_BYTES:
        observation.finish(reason="db_queue_full" if len(_pending) >= _MAX_PENDING else "db_bytes_full")
        return kh, ch
    _pending_bytes += body_len
    task = asyncio.create_task(_store(
        method=method, endpoint_id=endpoint_id, provider=provider, url=url,
        caller_body=caller_body, headers=headers, status_code=status_code,
        media_type=media_type, body=body, origin=origin, key_hash=kh, body_hash=ch,
        observation=observation, plan=plan))
    _pending.add(task)
    # Release bytes AND task when done. NOT redundant with drain()'s own removal: on a running
    # server drain() never fires, and this callback is the only exit from `_pending` — without it
    # the set fills to _MAX_PENDING and record() sheds every recording from then on.
    def done(task):
        _task_done(task, body_len)
        if task.cancelled():
            observation.finish(reason="cancelled")
    task.add_done_callback(done)
    return kh, ch


def _task_done(task: asyncio.Task, body_len: int) -> None:
    """Release the task and its body bytes from the pending budget."""
    global _pending_bytes
    _pending.discard(task)
    _pending_bytes -= body_len


_TERMINAL_TOTAL_S = 28.0
_TERMINAL_UPLOAD_S = 8.0
_TERMINAL_DB_S = 20.0


async def store_terminal_response(
    call_id: str, provider: str, endpoint_id: str, status_code: int, body: bytes,
) -> None:
    """Archive terminal task JSON under the originating call id without fetching linked media."""
    async def persist():
        try:
            async with asyncio.timeout(_TERMINAL_TOTAL_S):
                await _store(
                    method="GET", endpoint_id=endpoint_id, provider=provider,
                    url=f"treg://asynctasks/{call_id}", caller_body=b"", headers={},
                    status_code=status_code, media_type="application/json", body=body,
                    origin="async_terminal", observation=archive_bodies.StorageReport(call_ref=call_id))
        except TimeoutError:
            _log.error("terminal archive total deadline exceeded for %s", call_id)
    task = asyncio.create_task(persist())
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        # Settlement is already committed. Finish this bounded evidence operation even when
        # the polling request/worker deadline cancels its waiter, then preserve cancellation.
        _log.warning("terminal archive waiter cancelled; finishing bounded evidence for %s", call_id)
        await asyncio.shield(task)
        raise


async def load_terminal_responses(tasks: list[tuple[str, str]]) -> dict[str, bytes]:
    """The archived terminal JSON for each `(call_id, endpoint_id)` that has one. Bytes only,
    verbatim: the caller decides how to read them. Misses are absent from the mapping, never None.
    Looked up by the same key hash `store_terminal_response` wrote under (indexed), not by URL."""
    if not tasks:
        return {}
    from sqlalchemy import select

    from .infra.db import session_maker
    from .models import ArchiveKey, ArchiveSnapshot

    hashes = {cache_key("GET", endpoint_id, f"treg://asynctasks/{call_id}", b"", {}): call_id
              for call_id, endpoint_id in tasks}
    out: dict[str, bytes] = {}
    async with session_maker() as s:
        keys = (await s.execute(
            select(ArchiveKey).where(ArchiveKey.key_hash.in_(list(hashes))))).scalars().all()
        if not keys:
            return out
        by_key = {k.id: hashes[k.key_hash] for k in keys}
        snaps = (await s.execute(
            select(ArchiveSnapshot).options(*archive_bodies.read_options("terminal")).where(ArchiveSnapshot.key_id.in_(list(by_key)))
            .order_by(ArchiveSnapshot.key_id, ArchiveSnapshot.version.desc()))).scalars().all()
        newest: dict[int, ArchiveSnapshot] = {}
        for snap in snaps:
            newest.setdefault(snap.key_id, snap)
        pointers = {by_key[key_id]: await archive_bodies.pointer(s, snap, "terminal")
                    for key_id, snap in newest.items()}
    semaphore = asyncio.Semaphore(8)
    async def load(call_id, pointer):
        async with semaphore:
            return call_id, await archive_bodies.read(pointer, "terminal")
    return {call_id: body for call_id, body in await asyncio.gather(
        *(load(call_id, pointer) for call_id, pointer in pointers.items())) if body is not None}


async def drain() -> None:
    """Flush in-flight recordings for shutdown and tests. Each task carries bounded upload,
    write and optional change-analysis stages."""
    await archive_bodies.drain()
    while _pending:
        tasks = list(_pending)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        # Remove what was just gathered HERE: a gather of already-finished tasks returns without
        # yielding, so the queued `_pending.discard` callbacks may not have run yet, and a loop
        # keyed on them busy-spins forever, starving the loop and every timer on it (the 2026-08
        # serial-Postgres CI hang). Same discipline as audit.drain(); test_audit.py pins both.
        _pending.difference_update(tasks)
        for r in results:
            if isinstance(r, asyncio.TimeoutError | TimeoutError):
                _log.error("archive recording dropped: database did not answer in %ss",
                           _STORE_TIMEOUT_S)


async def _bump_stats(s, *, endpoint_id: str, provider: str, pol: str, new_key: bool,
                      stable_d: int, changed_d: int, kept: bool, size: int, now) -> None:
    """Move the endpoint's running totals (ArchiveEndpointStat) inside the caller's transaction.
    Atomic column arithmetic only (col = col + n) — never read-modify-write: the credit-block
    drift showed what parallel writers do to in-memory subtraction. A missing row is inserted
    and the racing loser retries as an update."""
    from sqlalchemy import update as sa_update
    from sqlalchemy.exc import IntegrityError

    from .models import ArchiveEndpointStat

    values = {
        "provider": provider, "policy": pol, "newest_fetch": now,
        "keys": ArchiveEndpointStat.keys + (1 if new_key else 0),
        "stable": ArchiveEndpointStat.stable + stable_d,
        "changed": ArchiveEndpointStat.changed + changed_d,
        "snapshots": ArchiveEndpointStat.snapshots + 1,
        "bodies_kept": ArchiveEndpointStat.bodies_kept + (1 if kept else 0),
        "kept_bytes": ArchiveEndpointStat.kept_bytes + (size if kept else 0),
    }
    res = await s.execute(sa_update(ArchiveEndpointStat)
                          .where(ArchiveEndpointStat.endpoint_id == endpoint_id).values(**values))
    if res.rowcount == 0:
        try:
            async with s.begin_nested():
                s.add(ArchiveEndpointStat(
                    endpoint_id=endpoint_id, provider=provider, policy=pol,
                    keys=1 if new_key else 0, stable=stable_d, changed=changed_d,
                    snapshots=1, bodies_kept=1 if kept else 0,
                    kept_bytes=size if kept else 0, newest_fetch=now))
        except IntegrityError:  # a concurrent recording inserted the row first — count on it
            await s.execute(sa_update(ArchiveEndpointStat)
                            .where(ArchiveEndpointStat.endpoint_id == endpoint_id).values(**values))


async def _store(
    *,
    method: str,
    endpoint_id: str,
    provider: str,
    url: str,
    caller_body: bytes,
    headers: dict[str, str],
    status_code: int,
    media_type: str,
    body: bytes,
    origin: str = "caller",
    key_hash: str | None = None,
    body_hash: str | None = None,
    observation: archive_bodies.StorageReport | None = None,
    plan: archive_bodies.WritePlan | None = None,
) -> None:
    """One recording: upsert the key, append a version, keep the change statistics honest.

    The license decides what is KEPT, not what is COUNTED: statistics and the content hash are
    recorded for every metered 2xx (a hash is an identity, not the content), while the body bytes
    are stored only when the catalog entry's cache policy allows it AND the body fits the size
    cap. Oversized bodies are skipped whole, never truncated. Consecutive identical answers
    deduplicate: the new version row points at the row carrying the bytes (`body_of`) — and when
    an identical answer arrives at a key whose bytes were never kept (policy or cap changed), the
    bytes are stored now, so a policy upgrade heals the store forward without a backfill."""
    from sqlalchemy.exc import IntegrityError

    observation = observation or archive_bodies.StorageReport()
    stored, reason = None, "record_failed"
    try:
        kh = key_hash or cache_key(method, endpoint_id, url, caller_body, headers)
        ch = body_hash or content_hash(body)
        plan = plan or _write_plan(endpoint_id, body, origin)
        if plan.storage in ("both", "r2"):
            if origin == "async_terminal":
                try:
                    async with asyncio.timeout(_TERMINAL_UPLOAD_S):
                        plan = await archive_bodies.prepare(body, ch, mode=plan.storage,
                                                           observation=observation, terminal=True)
                except TimeoutError:
                    observation.props["archive_body_upload_status"] = "failed"
                    plan = archive_bodies.WritePlan("db", reason="timeout")
                    _log.error("terminal archive upload deadline exceeded; saving DB evidence")
                if plan.storage is None:
                    plan = archive_bodies.WritePlan("db", reason=plan.reason)
            else:
                plan = await archive_bodies.prepare(body, ch, mode=plan.storage, observation=observation)

        from .domain.catalog import store as catalog_store
        cache = (catalog_store.load().by_id.get(endpoint_id) or {}).get("cache")
        ignore_paths = cache.get("ignore_paths", []) if isinstance(cache, dict) else []
        ignored_matches = set()
        if ignore_paths and plan.storage is not None and origin in ("caller", "refresh"):
            async with _get_sem():
                ignored_matches = await _ignored_matches(kh, body, ignore_paths)

        # Same-key waiters must queue before taking a scarce database-write slot. Otherwise four
        # duplicate recordings can occupy the whole semaphore while only one touches the database.
        async with asyncio.timeout(_TERMINAL_DB_S if origin == "async_terminal" else _STORE_TIMEOUT_S), _get_key_lock(kh):
            async with _get_sem():
                # Postgres row locking handles other processes. A retry also covers the narrow
                # first-key race and multi-process SQLite, where SELECT FOR UPDATE is ignored.
                for attempt in range(4):
                    try:
                        change = await _store_locked(
                            method=method, endpoint_id=endpoint_id, provider=provider, url=url,
                            caller_body=caller_body, headers=headers, status_code=status_code,
                            media_type=media_type, body=body, origin=origin,
                            key_hash=kh, body_hash=ch, plan=plan, ignored_matches=ignored_matches)
                        stored, reason = plan.storage, plan.reason
                        break
                    except IntegrityError:
                        if attempt == 3:
                            raise
                        await asyncio.sleep(0.01 * (attempt + 1))
        if change is not None and get_settings().archive_change_observation_enabled:
            previous_id, masked_by_ignore = change
            async with _get_sem():
                await _observe_change(previous_id, body, endpoint_id, provider, masked_by_ignore)
    except asyncio.CancelledError:
        reason = "cancelled"
        raise
    except TimeoutError:
        reason = "record_timeout"
        _log.error("archive record_timeout for %s", endpoint_id)
    except Exception:
        reason = "record_failed"
        _log.error("archive recording dropped for %s", endpoint_id, exc_info=True)
    finally:
        observation.finish(storage=stored, reason=reason)


def _change_json(body: bytes):
    def invalid_constant(_value):
        raise ValueError("non-JSON numeric constant")
    return json.loads(body, parse_constant=invalid_constant)


def _normalized_hash(body: bytes, paths: list[str]) -> str | None:
    """Delete only declared paths in a parsed copy; raw bytes and their identity never change."""
    try:
        value = _change_json(body)

        def remove(node, parts):
            part, *rest = parts
            if part == "[*]":
                if isinstance(node, list):
                    if rest:
                        for child in node:
                            remove(child, rest)
                    else:
                        node.clear()
            elif isinstance(node, dict) and part in node:
                if rest:
                    remove(node[part], rest)
                else:
                    del node[part]

        for path in paths:
            # The catalog validates the grammar; keeping [*] as a token also supports root arrays.
            parts = path.replace("[*]", ".[*]").lstrip(".").split(".")
            remove(value, parts)
        canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                               allow_nan=False).encode()
        return content_hash(canonical)
    except (ValueError, UnicodeError, RecursionError):
        return None


async def _change_compute(fn, *args):
    """Keep the caller's semaphore slot until its CPU job really finishes on cancellation."""
    task = asyncio.create_task(asyncio.to_thread(fn, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


def _has_change_body(snapshot) -> bool:
    # Legacy rows may have a deferred body and no location marker: preserve their fallback.
    # A loaded NULL with no carrier/location is known hash-only and needs no read.
    return (snapshot.body_storage in ("both", "r2")
            or snapshot.body_of is not None
            or snapshot.__dict__.get("body", True) is not None)


async def _ignored_matches(key_hash: str, body: bytes, paths: list[str]) -> set[int]:
    """Pre-read at most latest/decisive bodies; the writer accepts only its actual baseline ID.

    A concurrent writer can invalidate this sample. That observation falls back to raw hashes,
    without keeping a DB connection/row lock across object I/O or adding a second write.
    """
    from sqlalchemy import select
    from .infra.db import background_session_maker
    from .models import ArchiveKey, ArchiveSnapshot

    matches = set()
    try:
        async with asyncio.timeout(_CHANGE_TIMEOUT_S):
            new_hash = await _change_compute(_normalized_hash, body, paths)
            if new_hash is None:
                return matches
            async with background_session_maker() as s:
                key = (await s.execute(select(ArchiveKey).where(
                    ArchiveKey.key_hash == key_hash))).scalar_one_or_none()
                if key is None:
                    return matches
                latest = (await s.execute(select(ArchiveSnapshot.id).where(
                    ArchiveSnapshot.key_id == key.id).order_by(ArchiveSnapshot.version.desc())
                    .limit(1))).scalar_one_or_none()
                ids = {i for i in (latest, key.result_snapshot_id) if i is not None}
                rows = (await s.execute(select(ArchiveSnapshot).where(
                    ArchiveSnapshot.key_id == key.id, ArchiveSnapshot.id.in_(ids))
                    .options(*archive_bodies.read_options("observation")))).scalars().all()
                pointers = [(row.id, await archive_bodies.pointer(s, row, "observation"))
                            for row in rows if _has_change_body(row)]
            for snapshot_id, pointer in pointers:
                previous = await archive_bodies.read(pointer, "observation")
                if previous is None:
                    change_outcomes["ignore_body_unavailable"] += 1
                elif await _change_compute(_normalized_hash, previous, paths) == new_hash:
                    matches.add(snapshot_id)
    except Exception:
        change_outcomes["ignore_comparison_failed"] += 1
    return matches


def _change_summary(old_body: bytes, new_body: bytes) -> dict:
    """Report structure only; array indices collapse, containers stop at depth six."""
    try:
        old, new = _change_json(old_body), _change_json(new_body)
    except (ValueError, UnicodeError, RecursionError):
        return dict(changed_paths=["non_json"], path_count=1, truncated=False,
                    leaf_count=0, sole_path="non_json", masked_by_ignore=False)

    paths: set[str] = set()
    missing = object()

    def equal(left, right):
        if type(left) is not type(right):
            return False
        if isinstance(left, dict):
            return left.keys() == right.keys() and all(equal(v, right[k]) for k, v in left.items())
        if isinstance(left, list):
            return len(left) == len(right) and all(equal(a, b) for a, b in zip(left, right))
        return left == right

    def walk(left, right, path, depth):
        if depth >= 6:
            if not equal(left, right):
                paths.add(path or "$")
        elif isinstance(left, dict) and isinstance(right, dict):
            for key in left.keys() | right.keys():
                walk(left.get(key, missing), right.get(key, missing),
                     f"{path}.{key}" if path else key, depth + 1)
        elif isinstance(left, list) and isinstance(right, list):
            for i in range(max(len(left), len(right))):
                walk(left[i] if i < len(left) else missing,
                     right[i] if i < len(right) else missing, path + "[*]", depth + 1)
        elif not equal(left, right):
            paths.add(path or "$")

    def leaves(value, depth=0):
        if depth >= 6 or not isinstance(value, (dict, list)) or not value:
            return 1
        return sum(leaves(v, depth + 1) for v in
                   (value.values() if isinstance(value, dict) else value))

    walk(old, new, "", 0)
    ordered = sorted(paths)
    return dict(changed_paths=ordered[:20], path_count=len(paths), truncated=len(paths) > 20,
                leaf_count=leaves(new), sole_path=ordered[0] if len(paths) == 1 else None,
                masked_by_ignore=False)


async def _read_change_body(snapshot_id: int) -> bytes | None:
    from sqlalchemy import select
    from .infra.db import background_session_maker
    from .models import ArchiveSnapshot

    async with background_session_maker() as s:
        row = (await s.execute(select(ArchiveSnapshot).where(ArchiveSnapshot.id == snapshot_id)
                              .options(*archive_bodies.read_options("observation")))).scalar_one_or_none()
        pointer = (await archive_bodies.pointer(s, row, "observation")
                   if row is not None and _has_change_body(row) else None)
    return await archive_bodies.read(pointer, "observation") if pointer is not None else None


async def _observe_change(previous_id: int, body: bytes, endpoint_id: str, provider: str,
                          masked_by_ignore: bool = False) -> None:
    from . import analytics

    try:
        async with asyncio.timeout(_CHANGE_TIMEOUT_S):
            previous = await _read_change_body(previous_id)
            if previous is None:
                change_outcomes["body_unavailable"] += 1
                return
            props = await _change_compute(_change_summary, previous, body)
            props["masked_by_ignore"] = masked_by_ignore
            analytics.capture("archive", "archive_change_observed",
                              dict(endpoint_id=endpoint_id, provider=provider, **props))
            change_outcomes["observed"] += 1
    except Exception:
        # Observation is optional and happens after commit. Never log provider bytes/errors.
        change_outcomes["observation_failed"] += 1


async def _lock_archive_key(s, key_id: int):
    """Lock and refresh a key whose earlier unlocked lookup may be stale in the identity map."""
    from sqlalchemy import select

    from .models import ArchiveKey

    return (await s.execute(
        select(ArchiveKey).where(ArchiveKey.id == key_id).with_for_update()
        .execution_options(populate_existing=True)
    )).scalars().one()


async def _snapshot_body(session, snapshot):
    from .models import ArchiveSnapshot

    body = _unpack(snapshot.body, snapshot.enc)
    if body is None and snapshot.body_of is not None:
        carrier = await session.get(ArchiveSnapshot, snapshot.body_of)
        if carrier is not None and carrier.key_id == snapshot.key_id:
            body = _unpack(carrier.body, carrier.enc)
    return body


async def _store_locked(
    *,
    method: str,
    endpoint_id: str,
    provider: str,
    url: str,
    caller_body: bytes,
    headers: dict[str, str],
    status_code: int,
    media_type: str,
    body: bytes,
    origin: str = "caller",
    key_hash: str | None = None,
    body_hash: str | None = None,
    plan: archive_bodies.WritePlan,
    ignored_matches: set[int] | None = None,
) -> tuple[int, bool] | None:
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError

    from .domain.catalog import store as catalog_store
    from .domain.catalog.results import classify, has_result_rules
    from .infra.db import background_session_maker
    from .models import ArchiveKey, ArchiveSnapshot

    result_aware = has_result_rules(endpoint_id)
    result = classify(endpoint_id, status_code, body) if result_aware else None
    entry = catalog_store.load().by_id.get(endpoint_id)
    pol = policy(entry)
    # `record()` hands both hashes in, computed once on the call path; the fallback keeps
    # `_store` callable on its own (tests).
    kh = key_hash or cache_key(method, endpoint_id, url, caller_body, headers)
    ch = body_hash or content_hash(body)
    now = _utcnow()

    async with background_session_maker() as s:
        key = (await s.execute(
            select(ArchiveKey).where(ArchiveKey.key_hash == kh))).scalars().one_or_none()
        if key is None:
            # The request shape is stored WITH the key so the refresh worker can re-ask the
            # exact question later. It is the pre-injection request: credentials cannot be in
            # it (they are added inside the relay, after this shape is fixed).
            kept = {k.lower(): v.strip() for k, v in (headers or {}).items()
                    if k.lower() in ("accept", "accept-language") and v.strip()}
            key = ArchiveKey(key_hash=kh, endpoint_id=endpoint_id, provider=provider,
                             policy=pol, fetched_at=now,
                             last_requested_at=now if origin == "caller" else None,
                             ttl_s=ttl_for(entry),
                             req_method=method.upper(), req_url=url,
                             req_body=caller_body or None, req_headers=kept)
            s.add(key)
            try:
                await s.commit()
            except IntegrityError:  # two first-calls raced; the winner's row is the key
                await s.rollback()
                key = (await s.execute(
                    select(ArchiveKey).where(ArchiveKey.key_hash == kh))).scalars().one()

        # Snapshot versions are allocated from the newest row. Serialize that read and insert per
        # key across processes; the in-process semaphore only bounds pool pressure and cannot stop
        # two Render instances from both choosing version N+1. Re-lock after the key-creation commit
        # as well, because that commit necessarily released the insert transaction's locks.
        key = await _lock_archive_key(s, key.id)

        newest = (await s.execute(
            select(ArchiveSnapshot).where(ArchiveSnapshot.key_id == key.id)
            .order_by(ArchiveSnapshot.version.desc()).limit(1))).scalars().first()
        new_key = newest is None            # first version ⇒ this recording created the key
        seen_before = (key.stable_seen, key.change_seen)

        stored, enc = _pack(body) if plan.keep_db else (None, None)
        snap = ArchiveSnapshot(
            key_id=key.id, version=1 if newest is None else newest.version + 1,
            status_code=status_code, media_type=media_type, content_hash=ch,
            body=stored, enc=enc, size_bytes=len(body),
            fetched_at=now, origin=origin, body_storage=plan.storage)
        # Byte deduplication is independent of usefulness, including empty history.
        if newest is not None and newest.content_hash == ch:
            carrier = newest.body_of or (newest.id if newest.body is not None else None)
            if plan.keep_db and carrier is not None:
                snap.body, snap.body_of = None, carrier

        baseline = None
        if not result_aware:
            baseline = newest
            key.result_state = key.result_snapshot_id = None
        elif (key.result_state is None or
                (newest is not None and key.result_observed_version != newest.version)):
            # Upgrade lazily from the latest version only. Never search past an empty result.
            key.result_state = "unknown"
            key.result_snapshot_id = None
            if newest is not None:
                previous_body = await _snapshot_body(s, newest)
                if previous_body is not None:
                    previous = classify(endpoint_id, newest.status_code, previous_body)
                    if previous.state in ("found", "empty"):
                        key.result_state = previous.state
                        key.result_snapshot_id = newest.id
                        baseline = newest
        elif key.result_snapshot_id is not None:
            baseline = await s.get(ArchiveSnapshot, key.result_snapshot_id)
            if baseline is not None and baseline.key_id != key.id:
                baseline = None

        previous_state = key.result_state if result_aware else "found"
        next_state = result.state if result_aware else "found"
        masked_by_ignore = False
        decisive = next_state in ("found", "empty") and origin != "async_terminal"
        if decisive and baseline is not None and (previous_state, next_state) != ("empty", "empty"):
            stable = previous_state == next_state == "found" and (
                baseline.content_hash == ch or baseline.id in (ignored_matches or ()))
            masked_by_ignore = stable and baseline.content_hash != ch
            if stable:
                key.stable_seen += 1
            else:
                key.change_seen += 1
                key.last_changed_at = now
            learn(key, stable=stable, entry=entry)
        key.fetched_at, key.policy = now, pol
        if origin == "caller":     # a refresh is treg asking itself — never demand
            key.last_requested_at = now
        s.add(key)
        s.add(snap)
        # Make version conflicts explicit here, before any stats query or commit handling. The
        # IntegrityError leaves this function and the outer loop retries the whole transaction.
        await s.flush()
        key.result_observed_version = snap.version if result_aware else None
        if decisive and result_aware:
            key.result_state = result.state
            key.result_snapshot_id = snap.id
            s.add(key)
        await _bump_stats(
            s, endpoint_id=endpoint_id, provider=provider, pol=pol, new_key=new_key,
            stable_d=key.stable_seen - seen_before[0], changed_d=key.change_seen - seen_before[1],
            kept=plan.storage is not None, size=len(body), now=now)
        await s.commit()
        return ((newest.id, masked_by_ignore) if newest is not None and newest.content_hash != ch
                and plan.storage is not None and _has_change_body(newest)
                and get_settings().archive_change_observation_enabled
                and origin in ("caller", "refresh") else None)



# ---------------------------------------------------------------------------------------------
# The pruner — profit-shaped shelf clearing. A served hit is revenue with no vendor cost, so a
# body's right to disk is its earning potential. It strips BYTES only: every version row stays
# (hash, size, timestamp — the history and the statistics are untouched), the newest version of
# every key stays whole (serving and change-detection need it), and a dedup carrier is never
# stripped while a kept version still points at it.
#
# Rank of removal, first to go:
#   1. Old versions of keys the learner marked never-servable (TTL_NEVER) — they can never sell.
#   2. Old versions beyond the newest `archive_prune_keep_versions` on keys nobody has demanded
#      recently, oldest first, once they pass `archive_prune_min_age_days`.
# Archive-policy endpoints are exempt: their history is the future data product.

_PRUNE_DEMAND_GRACE_DAYS = 14  # a key demanded in this window keeps its full version budget


def prune_enabled() -> bool:
    return recording() and get_settings().archive_prune_batch > 0


async def prune_worker() -> None:
    """Run forever from lifespan; same discipline as refresh_worker."""
    while True:
        try:
            done = await prune_once()
            if done:
                _log.info("archive prune: %d body(ies) stripped", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.warning("archive prune pass failed", exc_info=True)
        await asyncio.sleep(get_settings().archive_prune_interval_s)


async def prune_once() -> int:
    """One bounded pass. Returns how many bodies were stripped."""
    if not prune_enabled():
        return 0
    from sqlalchemy import select, update as sa_update

    from .infra.db import background_session_maker
    from .models import ArchiveEndpointStat, ArchiveKey, ArchiveSnapshot

    s_cfg = get_settings()
    batch = s_cfg.archive_prune_batch
    keep_n = max(1, s_cfg.archive_prune_keep_versions)
    min_age = _utcnow() - timedelta(days=s_cfg.archive_prune_min_age_days)
    demand_floor = _utcnow() - timedelta(days=_PRUNE_DEMAND_GRACE_DAYS)
    stripped = 0

    async with background_session_maker() as s:
        # Candidate keys, worst earners first: never-servable, then long-undemanded.
        keys = (await s.execute(
            select(ArchiveKey)
            .where(ArchiveKey.policy != CACHE_ARCHIVE)
            .where((ArchiveKey.ttl_s == TTL_NEVER)
                   | (ArchiveKey.last_requested_at.is_(None))
                   | (ArchiveKey.last_requested_at < demand_floor))
            .order_by((ArchiveKey.ttl_s == TTL_NEVER).desc(), ArchiveKey.last_requested_at)
            .limit(2000))).scalars().all()

        for key in keys:
            if stripped >= batch:
                break
            versions = (await s.execute(
                select(ArchiveSnapshot).where(ArchiveSnapshot.key_id == key.id)
                .order_by(ArchiveSnapshot.version.desc()))).scalars().all()
            if not versions:
                continue
            budget = 1 if key.ttl_s == TTL_NEVER else keep_n
            # Decide the strip set FIRST, then protect the carriers of everything that survives —
            # a surviving version may reference a body on an older row (dedup), and stripping the
            # carrier would silently orphan it.
            candidates = [v for v in versions[budget:]
                          if v.id != key.result_snapshot_id
                          and v.body is not None
                          and (key.ttl_s == TTL_NEVER or v.fetched_at <= min_age)]
            surviving = [v for v in versions if v not in candidates]
            protected = ({v.id for v in surviving}
                         | {v.body_of for v in surviving if v.body_of is not None})
            freed_bytes = 0
            freed_n = 0
            for v in candidates:
                if v.id in protected:
                    continue
                v.body, v.enc = None, None
                if v.body_storage == "both":
                    v.body_storage = "r2"
                else:
                    v.body_storage = None
                    freed_n += 1
                    freed_bytes += v.size_bytes
                s.add(v)
                stripped += 1
                if stripped >= batch:
                    break
            if freed_n:
                await s.execute(sa_update(ArchiveEndpointStat)
                                .where(ArchiveEndpointStat.endpoint_id == key.endpoint_id)
                                .values(bodies_kept=ArchiveEndpointStat.bodies_kept - freed_n,
                                        kept_bytes=ArchiveEndpointStat.kept_bytes - freed_bytes))
        await s.commit()
    return stripped


# ---------------------------------------------------------------------------------------------
# Reading one call's stored answer back (the team-facing `/calls/{id}/result`)

def _decode(body: bytes | None) -> str | None:
    if body is None:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def resolve_result(key_hash: str, content_hash: str) -> dict[str, Any] | None:
    """The request shape and the stored answer a call row points at, or None when the key or
    that exact answer is no longer on file.

    `content_hash` names the exact bytes the caller received (dedup means several versions can
    share them — the newest matching version is the one reported). Follows a `body_of` reference
    to the row carrying the bytes; a hash-only version returns `stored=False` with the bytes
    honestly absent. Never raises on decode: a non-UTF-8 body reports `body_text=None`."""
    from sqlalchemy import select

    from .models import ArchiveKey, ArchiveSnapshot

    from .infra.db import session_maker

    async with session_maker() as session:
        key = (await session.execute(
            select(ArchiveKey).where(ArchiveKey.key_hash == key_hash))).scalars().one_or_none()
        if key is None:
            return None
        snap = (await session.execute(
            select(ArchiveSnapshot).options(*archive_bodies.read_options("result"))
            .where(ArchiveSnapshot.key_id == key.id, ArchiveSnapshot.content_hash == content_hash)
            .order_by(ArchiveSnapshot.version.desc()).limit(1))).scalars().first()
        if snap is None:
            return None
        pointer = await archive_bodies.pointer(session, snap, "result")
    body = await archive_bodies.read(pointer, "result")
    return {
        "stored": body is not None,
        "request": {"method": key.req_method or "", "url": key.req_url or "",
                    "body_text": _decode(key.req_body)},
        "response": {"status_code": snap.status_code, "media_type": snap.media_type,
                     "size_bytes": snap.size_bytes, "fetched_at": snap.fetched_at.isoformat(),
                     "origin": snap.origin, "version": snap.version,
                     "body_text": _decode(body)},
    }


# ---------------------------------------------------------------------------------------------
# Serving (PR 4) — the cache answers instead of the vendor, and NOTHING about money changes.
# The lookup replaces only the network trip: reserve, settle, audit and the cost header all run
# exactly as on a live call, at today's price, and the response is tagged cached. Whatever
# billing rule the founder later chooses attaches to that tag without touching this code.

# Phase-1 freshness: FIXED guesses per capability prefix, longest prefix wins, seconds. These are
# deliberately conservative starting values, not knowledge — the learner (PR 5) replaces them per
# key. A vendor-declared `cache.max_age_s` (CoinGecko's 24h refresh duty) always CAPS the result.
_TTL_DEFAULTS: tuple[tuple[str, int], ...] = (
    ("crypto.price", 300),        # live-ish market numbers: minutes, not hours
    ("crypto.", 3600),
    ("web.search", 3600),         # SERPs move within hours
    ("web.papers", 86400),        # scholarly metadata barely moves
    ("people.", 7 * 86400),       # person/company enrichment: weeks in practice, start at one
    ("company.", 7 * 86400),
    ("seo.", 86400),              # backlink/rank profiles: days
)
DEFAULT_TTL_S = 3600


def ttl_for(entry: dict[str, Any] | None) -> int:
    """The phase-1 freshness window for one endpoint, in seconds. Longest matching capability
    prefix from the fixed table (else the 1-hour default), always capped by the vendor's own
    declared ceiling when the judged `cache` block carries `max_age_s`."""
    capability = str((entry or {}).get("capability") or "")
    ttl = DEFAULT_TTL_S
    best = -1
    for prefix, seconds in _TTL_DEFAULTS:
        if capability.startswith(prefix) and len(prefix) > best:
            best, ttl = len(prefix), seconds
    declared = (entry or {}).get("cache")
    if isinstance(declared, dict):
        try:
            cap = int(declared.get("max_age_s") or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            ttl = min(ttl, cap)
    return ttl


def caller_forces_live(headers) -> bool:
    """`Cache-Control: no-cache` (or no-store) is the caller's veto — always honored, billed as
    the live call it causes. This is also the read-after-write escape: the archive never guesses
    cross-endpoint effects (that would be modeling the upstream)."""
    cc = (headers.get("cache-control") or "").lower()
    return "no-cache" in cc or "no-store" in cc


def caller_max_age_s(headers) -> int | None:
    """`X-Treg-Max-Age`: the caller's own freshness bar in seconds, tightening (never widening)
    the endpoint's window. Malformed values are ignored — a typo must not change behavior."""
    raw = headers.get("x-treg-max-age")
    if raw is None:
        return None
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


async def lookup(
    *,
    method: str,
    endpoint_id: str,
    url: str,
    caller_body: bytes,
    request_headers,
    cohort: str = "",
    diagnostics: dict | None = None,
) -> dict[str, Any] | None:
    """A fresh stored answer for this exact question, or None (= make the live call).

    None on every uncertain branch: serving off, caller veto, unjudged/forbidden policy, no
    snapshot, stale snapshot, bytes not on file. The age check runs against the newest snapshot's
    own fetch time, and the window is min(endpoint TTL, caller X-Treg-Max-Age). Returns the
    verbatim stored bytes plus what the hook needs for headers: fetched_at and age_s."""
    def miss(reason: str):
        if diagnostics is not None:
            diagnostics["cache_outcome"] = reason
        return None

    try:
        if not serving():
            return miss("mode_disabled")
        if caller_forces_live(request_headers):
            return miss("caller_bypass")
        selection = rollout_reason(endpoint_id, cohort)
        if selection != "selected":
            return miss(selection)
        from sqlalchemy import select

        from .domain.catalog import store as catalog_store
        from .domain.catalog.results import classify, has_result_rules
        # The API pool, deliberately: a lookup runs INSIDE a caller's /call/. Every other session in
        # this module is a write nobody awaits and goes to the background pool; this one is on the
        # hot path and must not queue behind them.
        from .infra.db import session_maker
        from .models import ArchiveKey, ArchiveSnapshot

        result_aware = has_result_rules(endpoint_id)
        entry = catalog_store.load().by_id.get(endpoint_id)
        if not storable(entry):
            return miss("policy_excluded")
        wanted = caller_max_age_s(request_headers)

        kh = cache_key(method, endpoint_id, url, caller_body, {
            k: request_headers.get(k, "") for k in ("accept", "accept-language")})
        async with session_maker() as s:
            key = (await s.execute(
                select(ArchiveKey).where(ArchiveKey.key_hash == kh))).scalars().one_or_none()
            if key is None:
                return miss("key_missing")
            if key.ttl_s == TTL_NEVER:
                return miss("ttl_disabled")
            window = key.ttl_s if key.ttl_s > 0 else ttl_for(entry)
            if wanted is not None:
                window = min(window, wanted)
            if window <= 0:
                return miss("ttl_disabled")
            newest = (await s.execute(
                select(ArchiveSnapshot).options(*archive_bodies.read_options("lookup")).where(ArchiveSnapshot.key_id == key.id)
                .order_by(ArchiveSnapshot.version.desc()).limit(1))).scalars().first()
            # Older writers can still append during a rolling deploy. A version they wrote
            # invalidates our saved decision; classify that newest body as legacy evidence.
            assessed = (result_aware and newest is not None and key.result_state is not None
                        and key.result_observed_version == newest.version)
            if assessed:
                if key.result_state != "found":
                    return miss("result_" + key.result_state)
                newest = await s.get(ArchiveSnapshot, key.result_snapshot_id, options=archive_bodies.read_options("lookup"))
                if newest is not None and newest.key_id != key.id:
                    return miss("snapshot_unavailable")
            if newest is None or not (200 <= newest.status_code < 300):
                return miss("snapshot_unavailable")
            age_s = int((_utcnow() - newest.fetched_at).total_seconds())
            if diagnostics is not None:
                diagnostics.update(cache_age_s=age_s, cache_window_s=window)
            if age_s < 0 or age_s > window:
                return miss("stale")
            pointer = await archive_bodies.pointer(s, newest, "lookup")
        body = await archive_bodies.read(pointer, "lookup", diagnostics=diagnostics)
        if body is None:
            return miss("body_missing")
        if result_aware:
            result = classify(endpoint_id, newest.status_code, body)
            if result.state != "found":
                return miss("result_" + result.state)
        if diagnostics is not None:
            diagnostics["cache_outcome"] = "hit"
        _touch(kh)
        return {"body": body, "media_type": newest.media_type,
                "status_code": newest.status_code, "fetched_at": newest.fetched_at,
                "age_s": age_s,
                # Identities of the served answer, for the audit row's call→archive link.
                "key_hash": kh, "content_hash": newest.content_hash, "version": newest.version}
    except Exception:  # noqa: BLE001 — a lookup fault must degrade to a live call, never a 500
        _log.warning("archive lookup failed for %s - serving live", endpoint_id, exc_info=True)
        return miss("lookup_error")


def _touch(key_hash: str) -> None:
    """Note that a stored answer was actually wanted (last_requested_at) — fire-and-forget, the
    demand signal the refresh worker (PR 5) will read. A served hit is NOT a recording: it adds
    no snapshot and no change statistics, because nothing new was observed."""
    if len(_pending) >= _MAX_PENDING:
        return
    task = asyncio.create_task(asyncio.wait_for(_touch_write(key_hash), timeout=_STORE_TIMEOUT_S))
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _touch_write(key_hash: str) -> None:
    try:
        from sqlalchemy import update

        from .infra.db import background_session_maker
        from .models import ArchiveKey

        # The SAME semaphore `_store` takes. A touch is a smaller write, not a freer one: `_touch`
        # bounds only the pending SET (512), so without this a burst of served hits would put
        # hundreds of sessions against the background pool at once. What gets dropped then is
        # `last_requested_at` — the demand signal `prune_once` reads — so a burst of hits would make
        # exactly those keys look undemanded and eligible for stripping.
        async with _get_sem(), background_session_maker() as s:
            await s.execute(update(ArchiveKey).where(ArchiveKey.key_hash == key_hash)
                            .values(last_requested_at=_utcnow()))
            await s.commit()
    except Exception:  # noqa: BLE001
        _log.error("archive touch dropped", exc_info=True)


# ---------------------------------------------------------------------------------------------
# The learner (PR 5): AIMD timers on admitted results, applied inside the recorder.

TTL_FLOOR_S = 60
TTL_CEILING_S = 30 * 86400
TTL_NEVER = -1          # the key marked itself never-cache: changes on every fetch
_NEVER_AFTER = 4        # consecutive changed refetches (no stables) before self-marking


def learn(key, *, stable: bool, entry: dict[str, Any] | None) -> None:
    """One AIMD step on the key's timer. Grow slowly on stability (×1.5, capped), shrink fast on
    change (×0.5, floored). The vendor's declared ceiling always caps; a key that only ever
    changes marks itself TTL_NEVER and is never served again until a stable refetch resets it."""
    ceiling = TTL_CEILING_S
    declared = (entry or {}).get("cache")
    if isinstance(declared, dict):
        try:
            cap = int(declared.get("max_age_s") or 0)
        except (TypeError, ValueError):
            cap = 0
        if cap > 0:
            ceiling = min(ceiling, cap)
    current = key.ttl_s if key.ttl_s > 0 else ttl_for(entry)
    if stable:
        key.ttl_s = min(int(current * 1.5), ceiling)
    elif key.change_seen >= _NEVER_AFTER and key.stable_seen == 0:
        key.ttl_s = TTL_NEVER
    else:
        key.ttl_s = max(int(current * 0.5), TTL_FLOOR_S)


# ---------------------------------------------------------------------------------------------
# The refresh worker (PR 5) — serve mode only. A key EARNS refreshing; it is never entitled:
# refreshed only when its window is ≥80% consumed AND a caller asked for it since its last fetch.
# A refresh is treg's own vendor spend with no caller attached, so two brakes bound it — the
# per-provider daily call cap (archive_refresh_daily_cap; 0 disables) and the per-pass limit.
# Every refresh is an observation: it lands through the same _store, teaching the timer.

_REFRESH_PER_PASS = 10
_DUE_SHARE = 0.8


def worker_enabled() -> bool:
    return (serving() and get_settings().archive_refresh_daily_cap > 0
            and bool(serve_endpoints()) and 0 < get_settings().archive_serve_percent <= 100)


async def refresh_worker(client) -> None:
    """Run forever from lifespan, adsconv.worker's discipline: a bad pass must never kill the
    loop, and only cancellation ends it."""
    while True:
        try:
            done = await refresh_once(client)
            if done:
                _log.info("archive refresh: %d key(s) refreshed", done)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _log.warning("archive refresh pass failed", exc_info=True)
        await asyncio.sleep(get_settings().archive_refresh_interval_s)


async def refresh_once(client) -> int:
    """One pass: find due-and-demanded keys, re-ask each question on treg's platform key, and
    record the answers (origin="refresh"). Returns how many were refreshed."""
    if not worker_enabled():
        return 0
    from sqlalchemy import func, select

    from .domain.catalog import store as catalog_store
    from .infra.db import background_session_maker
    from .models import ArchiveKey, ArchiveSnapshot

    now = _utcnow()
    cat = catalog_store.load()
    async with background_session_maker() as s:
        candidates = (await s.execute(
            select(ArchiveKey)
            .where(ArchiveKey.endpoint_id.in_(serve_endpoints()), ArchiveKey.ttl_s > 0,
                   ArchiveKey.req_url != "",
                   ArchiveKey.last_requested_at.is_not(None))
            .order_by(ArchiveKey.fetched_at))).scalars().all()
        # Today's refresh spend per provider — counted from the snapshots themselves, no extra
        # bookkeeping table to drift.
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        spent_rows = (await s.execute(
            select(ArchiveKey.provider, func.count(ArchiveSnapshot.id))
            .join(ArchiveSnapshot, ArchiveSnapshot.key_id == ArchiveKey.id)
            .where(ArchiveSnapshot.origin == "refresh", ArchiveSnapshot.fetched_at >= day_start)
            .group_by(ArchiveKey.provider))).all()
    spent = {provider: int(n) for provider, n in spent_rows}
    cap = get_settings().archive_refresh_daily_cap

    from .domain.catalog.results import has_result_rules

    refreshed = 0
    for key in candidates:
        if refreshed >= _REFRESH_PER_PASS:
            break
        if has_result_rules(key.endpoint_id) and key.result_state != "found":
            continue
        entry = cat.by_id.get(key.endpoint_id)
        if not storable(entry):
            continue  # judgment changed since recording — never refresh what may not be kept
        window = key.ttl_s if key.ttl_s > 0 else ttl_for(entry)
        age = (now - key.fetched_at).total_seconds()
        demanded = key.last_requested_at is not None and key.last_requested_at > key.fetched_at
        if age < window * _DUE_SHARE or not demanded:
            continue
        if spent.get(key.provider, 0) >= cap:
            continue
        if await _refresh_call(client, key):
            spent[key.provider] = spent.get(key.provider, 0) + 1
            refreshed += 1
    if refreshed:
        await drain()  # the recordings ARE this pass's output — land them before returning
    return refreshed


async def _refresh_call(client, key) -> bool:
    """Re-make one stored question on treg's own key. Injection reuses the ONE authoritative
    binding builder (oauth_providers.platform_bindings — moved there so this worker never
    imports api; the routers→api boundary forbids that chain); the key value resolves from
    settings exactly as the relay does it."""
    try:
        from . import oauth_providers

        provider = oauth_providers.get(key.provider)
        secret_value = get_settings().platform_key_for(key.provider)
        if provider is None or not secret_value:
            return False  # key withdrawn or provider de-listed — quietly not refreshable
        headers: dict[str, str] = dict(key.req_headers or {})  # replay what keyed the question
        params: list = []
        for binding in oauth_providers.platform_bindings(provider):
            value = getattr(get_settings(), binding.get("platform_setting", ""), "") or ""
            fmt = binding.get("format") or "{secret}"
            rendered = fmt.replace("{secret}", value) if value else fmt
            if binding.get("location") == "query":
                params.append((binding.get("name"), rendered))
            else:
                headers[binding.get("name", "")] = rendered
        resp = await client.request(key.req_method or "GET", key.req_url, params=params or None,
                                    headers=headers, content=key.req_body or None)
        body = resp.content
        if not (200 <= resp.status_code < 300):
            _log.warning("archive refresh got %s for %s", resp.status_code, key.endpoint_id)
            return False
        record(method=key.req_method or "GET", endpoint_id=key.endpoint_id,
               provider=key.provider, url=key.req_url, caller_body=key.req_body or b"",
               headers=dict(key.req_headers or {}), status_code=resp.status_code,
               media_type=resp.headers.get("content-type", ""), body=body, origin="refresh")
        return True
    except Exception:  # noqa: BLE001 — one dead key must not stop the pass
        _log.warning("archive refresh call failed for %s", key.endpoint_id, exc_info=True)
        return False
