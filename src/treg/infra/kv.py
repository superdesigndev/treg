"""The shared key-value store: expiring counters and optional admission leases.

Render Key Value (Redis protocol) in production, an in-process dictionary when `TREG_KV_URL` is
unset (local development, tests, self-hosters without one). Counters and leases expire; the optional
flow-control reader also reads externally published
snapshots without changing their contents or TTL. Each caller owns its unavailable policy. Socket I/O
uses `_TIMEOUT_S`; optional counters and health probes have their own shorter outer deadline.

Review-invitation budgets fail closed; money admission leases report unavailability explicitly
so the caller can retain PostgreSQL's original correctness path. New tenants add narrow methods,
not a generic get/set surface. Local counters do not pretend to provide distributed leases.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
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
_FLOW_CONTROL_MAX_BYTES = 32768
_READ_ORG_FLOW_CONTROL = """
local kind = redis.call('TYPE', KEYS[1]).ok
if kind == 'none' then return false end
if kind ~= 'string' then return -1 end
return redis.call('GETRANGE', KEYS[1], 0, 32768)
"""
log = logging.getLogger("treg.kv")
_clock = time.monotonic


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


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


@dataclass(frozen=True)
class RateWindowResult:
    outcome: Literal["admit", "over", "unavailable"]
    retry_after_ms: int = 0


@dataclass(frozen=True)
class OrgAdmissionResult:
    outcome: Literal["acquired", "busy", "rate_limited", "unavailable"]
    retry_after_s: int = 0


class FlowControlReadFailure(Enum):
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"


FlowControlReadResult = str | None | FlowControlReadFailure

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

# One sorted set per org; members are unique request owners, scores are Redis-time deadlines.
# Expiry recovers abandoned slots, not proof that old work stopped. Never renew an expired owner.
_SLOT_CLOCK = """
local t = redis.call('TIME')
local now = t[1] * 1000 + math.floor(t[2] / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
"""
_SLOT_EXTEND = """
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
local last = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
redis.call('PEXPIREAT', KEYS[1], math.ceil(tonumber(last[2])))
return 1
"""
_ACQUIRE_SLOT = _SLOT_CLOCK + """
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
""" + _SLOT_EXTEND
_RENEW_SLOT = _SLOT_CLOCK + """
if not redis.call('ZSCORE', KEYS[1], ARGV[1]) then return 0 end
""" + _SLOT_EXTEND
_RELEASE_SLOT = "return redis.call('ZREM', KEYS[1], ARGV[1])"
# A sliding window: members expire `ttl_ms` after admission; a refusal says when the oldest expires.
_RATE_WINDOW = _SLOT_CLOCK + """
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
    local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    return {0, math.max(0, math.ceil(tonumber(first[2]) - now))}
end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
local last = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
redis.call('PEXPIREAT', KEYS[1], math.ceil(tonumber(last[2])))
return {1, 0}
"""

# One persistent bucket identity across published versions. Rate and optional slot admission
# happen together: refusals may refill/update bucket metadata, but spend neither resource.
_ADMIT_ORG = """
local MAX_SAFE = 9007199254740991
local MAX_INT = 2147483647
local function finite(n)
    return n and n == n and n > -math.huge and n < math.huge
end
local function integer(n, low, high)
    return finite(n) and n >= low and n <= high and n == math.floor(n)
end
local function decimal(n)
    -- Redis Lua's implicit conversion rounds a value just below one up to one.
    return string.format('%.17g', n)
end
local ttl_ms = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local rps = tonumber(ARGV[4])
local burst = tonumber(ARGV[5])
if not integer(ttl_ms, 1, MAX_SAFE) or not integer(limit, 0, MAX_INT)
    or not finite(rps) or rps <= 0 or rps > MAX_SAFE
    or not integer(burst, 1, MAX_INT) then
    return {3, 0}
end
local slot_kind = 'none'
if limit > 0 then
    slot_kind = redis.call('TYPE', KEYS[1]).ok
    if slot_kind ~= 'none' and slot_kind ~= 'zset' then return {3, 0} end
end
local rate_kind = redis.call('TYPE', KEYS[2]).ok
if rate_kind ~= 'none' and rate_kind ~= 'hash' then return {3, 0} end
local t = redis.call('TIME')
local now_us = tonumber(t[1]) * 1000000 + tonumber(t[2])
local now_ms = math.floor(now_us / 1000)
if not integer(now_us, 0, MAX_SAFE) or (limit > 0 and now_ms + ttl_ms > MAX_SAFE) then
    return {3, 0}
end
local tokens = burst
local last_us = now_us
if rate_kind == 'hash' then
    if redis.call('HLEN', KEYS[2]) ~= 4 then return {3, 0} end
    local state = redis.call('HMGET', KEYS[2], 'tokens', 'last_us', 'rps', 'burst')
    tokens = tonumber(state[1])
    last_us = tonumber(state[2])
    local prior_rps = tonumber(state[3])
    local prior_burst = tonumber(state[4])
    if not finite(tokens) or not integer(last_us, 0, MAX_SAFE)
        or not finite(prior_rps) or prior_rps <= 0 or prior_rps > MAX_SAFE
        or not integer(prior_burst, 1, MAX_INT) or tokens < 0 or tokens > prior_burst then
        return {3, 0}
    end
    -- Fill under the previous policy before recording the incoming policy. Retaining a
    -- future watermark prevents double refill if the Redis clock has moved backwards.
    local elapsed_s = math.max(0, now_us - last_us) / 1000000
    tokens = math.min(prior_burst, tokens + elapsed_s * prior_rps)
    tokens = math.min(burst, tokens)
    last_us = math.max(last_us, now_us)
end
-- Validate even the slot scores before any mutation. Redis excludes NaN scores, so the
-- two extrema establish that every existing deadline is finite and in the safe range.
if slot_kind == 'zset' then
    local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
    local last = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
    if (#first > 0 and not integer(tonumber(first[2]), 0, MAX_SAFE))
        or (#last > 0 and not integer(tonumber(last[2]), 0, MAX_SAFE)) then
        return {3, 0}
    end
end
local outcome = 0
local retry_after = 0
if limit > 0 and redis.call('ZCOUNT', KEYS[1], '(' .. decimal(now_ms), '+inf') >= limit then
    outcome = 1
    retry_after = 1
elseif tokens < 1 then
    outcome = 2
    local wait_s = (last_us - now_us) / 1000000 + (1 - tokens) / rps
    retry_after = MAX_INT
    if finite(wait_s) and wait_s < MAX_INT then
        retry_after = math.max(1, math.ceil(wait_s))
    end
else
    tokens = tokens - 1
end
-- A bucket may disappear only when it would be full. Include the retained future
-- watermark; a clock rollback must not shorten expiry and hand out a fresh burst.
local fill_ms = (last_us - now_us) / 1000 + ((burst - tokens) / rps) * 1000
local expiry_ms = nil
if finite(fill_ms) and fill_ms <= MAX_SAFE then
    expiry_ms = math.max(1, math.ceil(fill_ms))
end
redis.call('HSET', KEYS[2], 'tokens', decimal(tokens), 'last_us', decimal(last_us),
    'rps', decimal(rps), 'burst', decimal(burst))
if expiry_ms then
    redis.call('PEXPIRE', KEYS[2], string.format('%.0f', expiry_ms))
else
    -- Extremely small positive rates can exceed safe integer TTLs. Persistent debt
    -- is conservative; capping the TTL would grant another burst before full refill.
    redis.call('PERSIST', KEYS[2])
end
if outcome == 0 and limit > 0 then
    redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)
    redis.call('ZADD', KEYS[1], now_ms + ttl_ms, ARGV[1])
    local last = redis.call('ZRANGE', KEYS[1], -1, -1, 'WITHSCORES')
    redis.call('PEXPIREAT', KEYS[1], math.ceil(tonumber(last[2])))
end
return {outcome, retry_after}
"""


class Store(Protocol):
    async def take(self, key: str, limit: int, ttl_s: int) -> bool:
        """Count one use of `key` inside a window that starts at its first use and lasts `ttl_s`.
        True while the window holds `limit` or fewer uses, False past it OR when the store cannot
        answer: a budget that cannot be checked is spent, never free."""

    async def ping(self) -> bool: ...

    async def read_org_flow_control(self, org_id: int) -> FlowControlReadResult:
        """Bounded JSON or missing (None), distinct from invalid data and transport failure."""

    async def acquire_lease(self, key: str, token: str, ttl_ms: int) -> AcquireResult: ...

    async def renew_lease(self, key: str, token: str, ttl_ms: int, *,
                          deadline: float | None = None) -> RenewResult: ...

    async def release_lease(self, key: str, token: str) -> ReleaseResult: ...

    async def acquire_slot(self, key: str, token: str, ttl_ms: int, limit: int) -> AcquireResult: ...

    async def admit_org(self, org_id: int, token: str, ttl_ms: int, limit: int | None, *,
                        rps: float, burst: int) -> OrgAdmissionResult:
        """Spend one rate token and optional slot together; neither is spent on refusal."""

    async def renew_slot(self, key: str, token: str, ttl_ms: int) -> RenewResult: ...

    async def release_slot(self, key: str, token: str) -> ReleaseResult: ...

    async def rate_window(self, key: str, token: str, ttl_ms: int, limit: int) -> RateWindowResult:
        """Admit one member into a sliding window of `limit`, or say how long until one expires."""

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

    async def read_org_flow_control(self, org_id: int) -> FlowControlReadResult:
        # Local state cannot stand in for published Redis configuration.
        return FlowControlReadFailure.UNAVAILABLE

    async def acquire_lease(self, key: str, token: str, ttl_ms: int) -> AcquireResult:
        return "unavailable"  # Local counters cannot promise cross-process exclusion.

    async def renew_lease(self, key: str, token: str, ttl_ms: int, *,
                          deadline: float | None = None) -> RenewResult:
        return "unavailable"

    async def release_lease(self, key: str, token: str) -> ReleaseResult:
        return "unavailable"

    async def aclose(self) -> None:
        self._windows.clear()

    async def acquire_slot(self, key: str, token: str, ttl_ms: int, limit: int) -> AcquireResult:
        return "unavailable"

    async def admit_org(self, org_id: int, token: str, ttl_ms: int, limit: int | None, *,
                        rps: float, burst: int) -> OrgAdmissionResult:
        return OrgAdmissionResult("unavailable")

    async def renew_slot(self, key: str, token: str, ttl_ms: int) -> RenewResult:
        return "unavailable"

    async def release_slot(self, key: str, token: str) -> ReleaseResult:
        return "unavailable"

    async def rate_window(self, key: str, token: str, ttl_ms: int, limit: int) -> RateWindowResult:
        return RateWindowResult("unavailable")


class RedisStore:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # server extra; imported here so the light CLI never loads it

        self._client = redis.Redis.from_url(
            url, socket_timeout=_TIMEOUT_S, socket_connect_timeout=_TIMEOUT_S,
            decode_responses=True,
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

    async def read_org_flow_control(self, org_id: int) -> FlowControlReadResult:
        if type(org_id) is not int or not 0 < org_id <= 2147483647:
            return FlowControlReadFailure.INVALID
        try:
            async with asyncio.timeout(_OPTIONAL_TIMEOUT_S):
                value = await self._client.eval(
                    _READ_ORG_FLOW_CONTROL, 1, f"treg:traffic-control:org:{org_id}")
            if value is None:
                return None
            if not isinstance(value, str) or len(value.encode("utf-8")) > _FLOW_CONTROL_MAX_BYTES:
                return FlowControlReadFailure.INVALID
            return value
        except UnicodeError:
            return FlowControlReadFailure.INVALID
        except Exception:  # noqa: BLE001
            # No URL, raw JSON, or exception text in the admission path.
            return FlowControlReadFailure.UNAVAILABLE

    async def _slot_eval(self, script: str, key: str, *args) -> int | None:
        try:
            # No contention wait or retry queue. Ambiguous writes are cleaned by owner token.
            async with asyncio.timeout(_TIMEOUT_S):
                return int(await self._client.eval(script, 1, key, *args))
        except Exception:  # noqa: BLE001 - any Redis fault reads as unavailable admission
            # No URL, token, request data or exception text in this admission signal.
            return None

    async def acquire_slot(self, key: str, token: str, ttl_ms: int, limit: int) -> AcquireResult:
        result = await self._slot_eval(_ACQUIRE_SLOT, key, token, ttl_ms, limit)
        return "unavailable" if result is None else "acquired" if result else "busy"

    async def admit_org(self, org_id: int, token: str, ttl_ms: int, limit: int | None, *,
                        rps: float, burst: int) -> OrgAdmissionResult:
        if (type(org_id) is not int or not 0 < org_id <= 2147483647
                or not isinstance(token, str) or not token
                or type(ttl_ms) is not int or not 0 < ttl_ms <= 9007199254740991
                or (limit is not None and (type(limit) is not int or not 0 < limit <= 2147483647))
                or type(rps) not in (int, float) or not 0 < rps <= 9007199254740991
                or not math.isfinite(rps)
                or type(burst) is not int or not 0 < burst <= 2147483647):
            return OrgAdmissionResult("unavailable")
        try:
            # No retry: a lost reply may already have spent the rate token. Owner-token
            # slot cleanup belongs to the caller and never refunds rate consumption.
            async with asyncio.timeout(_OPTIONAL_TIMEOUT_S):
                result = await self._client.eval(
                    _ADMIT_ORG, 2, f"treg:org-concurrency:v1:{org_id}",
                    f"treg:org-rate:v1:{org_id}", token, ttl_ms,
                    limit if limit is not None else 0, format(rps, ".17g"), burst)
            if (not isinstance(result, (list, tuple)) or len(result) != 2
                    or type(result[0]) is not int or result[0] not in (0, 1, 2, 3)
                    or type(result[1]) is not int or not 0 <= result[1] <= 2147483647):
                return OrgAdmissionResult("unavailable")
            outcomes = ("acquired", "busy", "rate_limited", "unavailable")
            return OrgAdmissionResult(outcomes[result[0]], result[1])
        except Exception:  # noqa: BLE001 - any Redis fault reads as unavailable admission
            # Cancellation propagates; logs never receive Redis URLs or exception data.
            return OrgAdmissionResult("unavailable")

    async def renew_slot(self, key: str, token: str, ttl_ms: int) -> RenewResult:
        result = await self._slot_eval(_RENEW_SLOT, key, token, ttl_ms)
        return "unavailable" if result is None else "renewed" if result else "lost"

    async def release_slot(self, key: str, token: str) -> ReleaseResult:
        result = await self._slot_eval(_RELEASE_SLOT, key, token)
        return "unavailable" if result is None else "released" if result else "not_owned"

    async def rate_window(self, key: str, token: str, ttl_ms: int, limit: int) -> RateWindowResult:
        try:
            async with asyncio.timeout(_OPTIONAL_TIMEOUT_S):
                result = await self._client.eval(_RATE_WINDOW, 1, key, token, ttl_ms, limit)
            admitted, wait_ms = int(result[0]), int(result[1])
        except Exception:  # noqa: BLE001 - any Redis fault reads as an unknown window
            return RateWindowResult("unavailable")
        return RateWindowResult("admit") if admitted else RateWindowResult("over", wait_ms)

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

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
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
