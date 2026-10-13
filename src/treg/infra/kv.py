"""The shared key-value store: expiring counters and optional admission leases.

Render Key Value (Redis protocol) in production, an in-process dictionary when `TREG_KV_URL` is
unset (local development, tests, self-hosters without one). The store holds nothing that must
survive: every key expires, and every caller treats "unavailable" as a safe answer. Socket I/O
for money leases uses `_TIMEOUT_S`; optional counters and health probes have their own shorter
outer deadline. Rate-limit commands use a separate client at `rate_limit_store_timeout_ms`.

Review-invitation budgets fail closed; money admission leases report unavailability explicitly
so the caller can retain PostgreSQL's original correctness path. New tenants add narrow methods,
not a generic get/set surface. Local counters do not pretend to provide distributed leases.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Literal, Protocol

from ..config import get_settings

_TIMEOUT_S = 1.0
# Optional work must not make a response wait for money's longer socket timeout.
_OPTIONAL_TIMEOUT_S = 0.2
# Lease attempts outlast one socket timeout; retries share a fixed per-key budget.
# Renewal also remains bounded by the earliest confirmed lease expiry.
_LEASE_ATTEMPT_TIMEOUT_S = 1.5
_LEASE_RETRY_BUDGET_S = 2.5
_LOCAL_MAX_KEYS = 10_000
log = logging.getLogger("treg.kv")
_clock = time.monotonic
_utcnow = lambda: datetime.now(timezone.utc).isoformat(timespec="milliseconds")
_lease_error_lock = threading.Lock()
_lease_errors: dict[tuple[str, str], dict] = {}
_lease_timings: dict[tuple[str, str], dict] = {}
_LEASE_OUTCOMES = {
    "acquire": {"acquired", "busy", "unavailable", "cancelled"},
    "renew": {"renewed", "lost", "unavailable", "cancelled", "deadline_expired"},
    "release": {"released", "lost", "not_owned", "unavailable", "cancelled"},
}
_LEASE_BUCKETS = (10, 50, 100, 200)

AcquireResult = Literal["acquired", "busy", "unavailable"]
RenewResult = Literal["renewed", "lost", "unavailable", "deadline_expired"]
ReleaseResult = Literal["released", "lost", "not_owned", "unavailable"]
_RENEW_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('pexpire', KEYS[1], ARGV[2])
end
return 0
"""
_RELEASE_LEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""
# A concurrency lease set: the global ceiling or one client IP's bucket (domain/ratelimit/policy.py
# `LocalCeiling` is the in-process twin). A sorted set of request ids scored by their last renewal;
# entries older than the TTL age out, so a release lost to a crash overcounts for at most that
# long. ARGV: member, ttl_ms, cap (-1 = none), enforce.
_RL_ADMIT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local ttl = tonumber(ARGV[2])
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now - ttl)
local n = redis.call('ZCARD', KEYS[1]) + 1
local cap = tonumber(ARGV[3])
local ok = cap < 0 or n <= cap
local admitted = ok or ARGV[4] ~= '1'
if admitted then
  redis.call('ZADD', KEYS[1], now, ARGV[1])
  redis.call('PEXPIRE', KEYS[1], ttl)
end
return {admitted and 1 or 0, ok and 1 or 0, n}
"""
# Renew the leases of requests still running. XX never re-adds a lease that was released or aged
# out. The last ARGV is the key's TTL in ms.
_RL_RENEW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local n = 0
for i = 1, #ARGV - 1 do
  n = n + redis.call('ZADD', KEYS[1], 'XX', 'CH', now, ARGV[i])
end
redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[#ARGV]))
return n
"""
# A versioned cache document, written only when the incoming version is strictly newer than the
# stored one. Returns the version the key holds afterwards.
_RL_PUBLISH = """
local incoming = tonumber(ARGV[2])
local cur = redis.call('GET', KEYS[1])
if cur then
  local ok, doc = pcall(cjson.decode, cur)
  if ok and type(doc) == 'table' and tonumber(doc.version) and tonumber(doc.version) >= incoming then
    return tonumber(doc.version)
  end
end
redis.call('SET', KEYS[1], ARGV[1])
return incoming
"""
RL_CONFIG_KEY = "rl:config"
RL_GLOBAL_KEY = "rl:g:c"
RL_LIVE_PREFIX = "rl:live:"


class Unavailable(Exception):
    """The store cannot answer a rate-limit operation; the caller fails open."""


class Store(Protocol):
    async def take(self, key: str, limit: int, ttl_s: int) -> bool:
        """Count one use of `key` inside a window that starts at its first use and lasts `ttl_s`.
        True while the window holds `limit` or fewer uses, False past it OR when the store cannot
        answer: a budget that cannot be checked is spent, never free."""

    async def ping(self) -> bool: ...

    async def acquire_lease(self, key: str, token: str, ttl_ms: int) -> AcquireResult: ...

    async def renew_lease(self, key: str, token: str, ttl_ms: int, *,
                          deadline: float | None = None) -> RenewResult: ...

    async def release_lease(self, key: str, token: str) -> ReleaseResult: ...

    # Rate limiting raises `Unavailable` instead of answering for the store; see application/ratelimit.
    async def rl_admit_key(self, key: str, member: str, cap: int | None, ttl_ms: int,
                           enforce: bool) -> tuple[bool, bool, int]: ...

    async def rl_release_key(self, key: str, member: str) -> None: ...

    async def rl_renew(self, key: str, members: list[str], ttl_ms: int) -> int: ...

    async def rl_get(self, key: str) -> str | None: ...

    async def rl_publish(self, key: str, value: str, version: int) -> int: ...

    async def rl_heartbeat(self, key: str, value: str, ttl_s: int) -> None: ...

    async def rl_live(self) -> list[str]: ...

    async def rl_memory(self, max_keys: int) -> dict: ...

    async def aclose(self) -> None: ...


class LocalStore:
    """Per-process fallback with the same contract. Bounded so a flood of keys cannot grow it."""

    def __init__(self) -> None:
        self._windows: dict[str, tuple[float, int]] = {}

    async def take(self, key: str, limit: int, ttl_s: int) -> bool:
        now = time.monotonic()
        expires, count = self._windows.get(key, (0.0, 0))
        if expires <= now:
            if len(self._windows) >= _LOCAL_MAX_KEYS:
                self._evict(now)
            expires, count = now + ttl_s, 0
        count += 1
        self._windows[key] = (expires, count)
        return count <= limit

    def _evict(self, now: float) -> None:
        live = {k: v for k, v in self._windows.items() if v[0] > now}
        if len(live) >= _LOCAL_MAX_KEYS:
            live = dict(sorted(live.items(), key=lambda kv: kv[1][0])[_LOCAL_MAX_KEYS // 2:])
        self._windows = live

    async def ping(self) -> bool:
        return True

    async def acquire_lease(self, key: str, token: str, ttl_ms: int) -> AcquireResult:
        return "unavailable"  # Local counters cannot promise cross-process exclusion.

    async def renew_lease(self, key: str, token: str, ttl_ms: int, *,
                          deadline: float | None = None) -> RenewResult:
        return "unavailable"

    async def release_lease(self, key: str, token: str) -> ReleaseResult:
        return "unavailable"

    # No shared store: the limiter counts per process.
    async def rl_admit_key(self, key, member, cap, ttl_ms, enforce):
        raise Unavailable("no shared store")

    async def rl_release_key(self, key, member):
        raise Unavailable("no shared store")

    async def rl_renew(self, key, members, ttl_ms):
        raise Unavailable("no shared store")

    async def rl_get(self, key):
        raise Unavailable("no shared store")

    async def rl_publish(self, key, value, version):
        raise Unavailable("no shared store")

    async def rl_heartbeat(self, key, value, ttl_s):
        raise Unavailable("no shared store")

    async def rl_live(self):
        raise Unavailable("no shared store")

    async def rl_memory(self, max_keys):
        raise Unavailable("no shared store")

    async def aclose(self) -> None:
        self._windows.clear()


class RedisStore:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # server extra; imported here so the light CLI never loads it

        self._client = redis.Redis.from_url(
            url, socket_timeout=_TIMEOUT_S, socket_connect_timeout=_TIMEOUT_S,
            decode_responses=True,
        )
        # Not the money client's one-second socket. The request path's budget is
        # rate_limit_store_timeout_ms (default 30); a miss is fail-open.
        rl_s = get_settings().rate_limit_store_timeout_ms / 1000
        self._rl_client = redis.Redis.from_url(
            url, socket_timeout=rl_s, socket_connect_timeout=rl_s, decode_responses=True,
        )

    async def take(self, key: str, limit: int, ttl_s: int) -> bool:
        try:
            async with asyncio.timeout(_OPTIONAL_TIMEOUT_S):
                async with self._client.pipeline(transaction=True) as pipe:
                    pipe.incr(key)
                    pipe.expire(key, ttl_s, nx=True)  # the window starts at the first use
                    count, _ = await pipe.execute()
            return int(count) <= limit
        except Exception as exc:  # noqa: BLE001 — a store fault is a spent budget, never a call fault
            _note_fault(exc)
            return False

    async def ping(self) -> bool:
        try:
            async with asyncio.timeout(_OPTIONAL_TIMEOUT_S):
                return bool(await self._client.ping())
        except Exception as exc:  # noqa: BLE001
            _note_fault(exc)
            return False

    async def acquire_lease(self, key: str, token: str, ttl_ms: int) -> AcquireResult:
        """A short-lived admission lease, never the authority for a money write.

        Unavailable is deliberately distinct from contention. Lease callers aggregate faults;
        unlike optional invitation budgets, an outage must not discard a pending settlement.
        """
        started = _clock()
        outcome = "cancelled"
        try:
            async with asyncio.timeout(_LEASE_ATTEMPT_TIMEOUT_S):
                acquired = await self._client.set(key, token, nx=True, px=ttl_ms)
            outcome = "acquired" if acquired else "busy"
            return outcome
        except Exception as exc:  # noqa: BLE001 - cancellation still propagates to the owning scope
            outcome = "unavailable"
            _record_lease_error("acquire", exc, started)
            return "unavailable"
        finally:
            _record_lease_timing("acquire", outcome, started)

    async def renew_lease(self, key: str, token: str, ttl_ms: int, *,
                          deadline: float | None = None) -> RenewResult:
        """Retry one transient fault with the original token; never recreate an expired lease.

        The gate supplies the earliest held lease's absolute time.monotonic() deadline.
        Check it before each EVAL: a cooperative timeout may arrive late or be translated
        into a transient error by driver cleanup. These budgets cannot preempt a pause.
        A lost reply may have extended the TTL, but only a confirmed renewal retains ownership.
        """
        stop_at = time.monotonic() + _LEASE_RETRY_BUDGET_S
        if deadline is not None:
            stop_at = min(stop_at, deadline)
        for attempt in range(2):
            now = time.monotonic()
            if deadline is not None and now >= deadline:
                return "deadline_expired"
            remaining = stop_at - now
            if remaining <= 0:
                break
            started = _clock()
            outcome = "cancelled"
            validity_limited = stop_at == deadline and remaining <= _LEASE_ATTEMPT_TIMEOUT_S
            attempt_timeout = asyncio.timeout(min(remaining, _LEASE_ATTEMPT_TIMEOUT_S))
            try:
                async with attempt_timeout:
                    renewed = await self._client.eval(_RENEW_LEASE, 1, key, token, ttl_ms)
                outcome = "renewed" if renewed else "lost"
                return outcome
            except Exception as exc:  # noqa: BLE001 - cancellation and ownership loss never retry
                if isinstance(exc, TimeoutError) and attempt_timeout.expired() and validity_limited:
                    outcome = "deadline_expired"
                    return "deadline_expired"
                outcome = "unavailable"
                _record_lease_error("renew", exc, started)
                if deadline is not None and time.monotonic() >= deadline:
                    return "deadline_expired"  # Retain the actual failed attempt above.
                if attempt or not _transient_lease_error(exc):
                    return "unavailable"
            finally:
                _record_lease_timing("renew", outcome, started, retry=bool(attempt))
        return "unavailable"

    async def release_lease(self, key: str, token: str) -> ReleaseResult:
        """Retry one ambiguous transient failure with the same owner-checked delete.

        A retry that finds no matching owner cannot distinguish a lost reply after DEL from an
        expired/replaced lease. It is safe cleanup, but not evidence that this owner lost its
        lease while working. No retry may delete a later owner's token.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _LEASE_RETRY_BUDGET_S
        for attempt in range(2):
            if loop.time() >= deadline:
                break
            started = _clock()
            outcome = "cancelled"
            try:
                async with asyncio.timeout_at(min(deadline, loop.time() + _LEASE_ATTEMPT_TIMEOUT_S)):
                    released = await self._client.eval(_RELEASE_LEASE, 1, key, token)
                outcome = "released" if released else ("not_owned" if attempt else "lost")
                return outcome
            except Exception as exc:  # noqa: BLE001 - cancellation never retries
                outcome = "unavailable"
                _record_lease_error("release", exc, started)
                if attempt or not _transient_lease_error(exc):
                    return "unavailable"
            finally:
                _record_lease_timing("release", outcome, started, retry=bool(attempt))
        return "unavailable"

    def _limiter(self):
        """The short-timeout client. A test that only stubs `_client` still reaches the limiter."""
        return getattr(self, "_rl_client", None) or self._client

    async def _rl(self, coro_fn):
        try:
            async with asyncio.timeout(get_settings().rate_limit_store_timeout_ms / 1000):
                return await coro_fn()
        except Exception as exc:  # noqa: BLE001 - cancellation still propagates
            raise Unavailable(type(exc).__name__) from exc

    async def rl_admit_key(self, key: str, member: str, cap: int | None, ttl_ms: int,
                           enforce: bool) -> tuple[bool, bool, int]:
        admitted, ok, n = await self._rl(lambda: self._limiter().eval(
            _RL_ADMIT, 1, key, member, ttl_ms, -1 if cap is None else cap, "1" if enforce else "0"))
        return bool(admitted), bool(ok), int(n)

    async def rl_release_key(self, key: str, member: str) -> None:
        await self._rl(lambda: self._limiter().zrem(key, member))

    async def rl_renew(self, key: str, members: list[str], ttl_ms: int) -> int:
        if not members:
            return 0
        return int(await self._rl(lambda: self._limiter().eval(_RL_RENEW, 1, key, *members, ttl_ms)))

    async def rl_get(self, key: str) -> str | None:
        return await self._rl(lambda: self._limiter().get(key))

    async def rl_publish(self, key: str, value: str, version: int) -> int:
        """Write only if `version` is newer than the stored one; returns what the key now holds."""
        return int(await self._rl(lambda: self._limiter().eval(_RL_PUBLISH, 1, key, value, str(version))))

    async def rl_heartbeat(self, key: str, value: str, ttl_s: int) -> None:
        await self._rl(lambda: self._limiter().set(key, value, ex=ttl_s))

    async def rl_live(self) -> list[str]:
        async def read():
            client = self._limiter()
            keys = [k async for k in client.scan_iter(match=RL_LIVE_PREFIX + "*", count=100)]
            return [v for v in (await client.mget(keys) if keys else []) if v]
        return await self._rl(read)

    async def rl_memory(self, max_keys: int) -> dict:
        """A bounded sample of what `rl:` keys occupy, next to the store's total, so a reader can
        see the limiter crowding money-admission leases out of an LRU-evicting store.

        The sample can outlast one request-path budget, so it uses the money client, with its own
        two-second cap. It does not scale with `_TIMEOUT_S`.
        """
        try:
            async with asyncio.timeout(2.0):
                keys: list[str] = []
                async for key in self._client.scan_iter(match="rl:*", count=200):
                    keys.append(key)
                    if len(keys) >= max_keys:
                        break
                sizes: list = []
                if keys:
                    async with self._client.pipeline(transaction=False) as pipe:
                        for key in keys:
                            pipe.memory_usage(key)
                        sizes = await pipe.execute()
                info = await self._client.info("memory")
            return {"rl_keys_sampled": len(keys), "rl_bytes_sampled": sum(int(s or 0) for s in sizes),
                    "rl_sample_truncated": len(keys) >= max_keys,
                    "kv_used_memory": int(info.get("used_memory", 0))}
        except Exception as exc:  # noqa: BLE001
            raise Unavailable(type(exc).__name__) from exc

    async def aclose(self) -> None:
        for client in (self._client, getattr(self, "_rl_client", None)):
            if client is None:
                continue
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass


def _transient_lease_error(exc: Exception) -> bool:
    from redis import exceptions as errors

    # Authentication errors inherit ConnectionError, but retrying them cannot recover a lease.
    if isinstance(exc, (errors.AuthenticationError, errors.AuthorizationError, errors.NoPermissionError)):
        return False
    return isinstance(exc, (TimeoutError, errors.TimeoutError, errors.ConnectionError))


def _record_lease_timing(phase: str, outcome: str, started: float, *, retry: bool = False) -> None:
    """Fixed-cardinality command-attempt summaries; no IDs, I/O or per-command events."""
    try:
        if outcome not in _LEASE_OUTCOMES.get(phase, ()):
            return
        elapsed_ms = max(0.0, _clock() - started) * 1000
        bucket = next((f"bucket_le_{limit}_ms" for limit in _LEASE_BUCKETS if elapsed_ms <= limit),
                      "bucket_gt_200_ms")
        with _lease_error_lock:
            row = _lease_timings.setdefault((phase, outcome), {
                "event": "kv_lease_gauge", "phase": phase, "outcome": outcome,
                "count": 0, "retry_count": 0, "elapsed_total_ms": 0.0, "elapsed_max_ms": 0.0,
                **{f"bucket_le_{limit}_ms": 0 for limit in _LEASE_BUCKETS}, "bucket_gt_200_ms": 0,
            })
            row["count"] += 1
            row["retry_count"] += int(retry)
            row["elapsed_total_ms"] += elapsed_ms
            row["elapsed_max_ms"] = max(row["elapsed_max_ms"], elapsed_ms)
            row[bucket] += 1
    except Exception:  # noqa: BLE001 - observation must not change money admission behavior
        pass


def drain_lease_timings() -> list[dict]:
    """Disjoint duration buckets per attempt; only the existing background runner drains them."""
    with _lease_error_lock:
        rows = list(_lease_timings.values())
        _lease_timings.clear()
    return rows


def _record_lease_error(phase: str, exc: Exception, started: float) -> None:
    """Only bounded memory work on the caller; never retain an exception or its message."""
    try:
        from redis import exceptions as errors

        if phase not in {"acquire", "renew", "release"}:
            return
        if isinstance(exc, TimeoutError):
            error_type = "deadline_exceeded"
        elif isinstance(exc, errors.TimeoutError):
            error_type = "redis_timeout"
        elif isinstance(exc, errors.AuthenticationError):
            error_type = "authentication"
        elif isinstance(exc, (errors.NoPermissionError, errors.AuthorizationError)):
            error_type = "permission"
        elif isinstance(exc, errors.ConnectionError):
            error_type = "connection"
        elif isinstance(exc, errors.ResponseError):
            error_type = "response"
        else:
            error_type = "other"
        elapsed_ms = round(max(0.0, _clock() - started) * 1000, 3)
        failed_at = _utcnow()
        with _lease_error_lock:
            # Three phases times seven fixed types; unknown exception classes share "other".
            row = _lease_errors.setdefault((phase, error_type), {
                "event": "kv_lease_error", "phase": phase, "error_type": error_type,
                "count": 0, "first_failed_at": failed_at, "last_failed_at": failed_at,
                "elapsed_max_ms": 0.0, "socket_timeout_ms": _TIMEOUT_S * 1000,
                "operation_deadline_ms": _LEASE_ATTEMPT_TIMEOUT_S * 1000,
            })
            row["count"] += 1
            row["last_failed_at"] = failed_at
            row["elapsed_max_ms"] = max(row["elapsed_max_ms"], elapsed_ms)
    except Exception:  # noqa: BLE001 - diagnostics cannot alter lease/fallback behavior
        pass


def drain_lease_errors() -> list[dict]:
    """Minute/worker-exit summaries for the existing background diagnostic log sink."""
    with _lease_error_lock:
        rows = list(_lease_errors.values())
        _lease_errors.clear()
    return rows


def _note_fault(exc: BaseException) -> None:
    from .. import analytics  # lazy: analytics imports config, and this module is imported early

    log.warning("kv unavailable: %s: %s", type(exc).__name__, exc)
    analytics.capture_fault(exc, component="kv")


_store: Store | None = None


def store() -> Store:
    """The process-wide store, built on first use from `TREG_KV_URL`."""
    global _store
    if _store is None:
        url = get_settings().kv_url
        _store = RedisStore(url) if url else LocalStore()
    return _store


def configured() -> bool:
    return bool(get_settings().kv_url)


async def close() -> None:
    global _store
    if _store is not None:
        await _store.aclose()
        _store = None
