"""The provider-capacity tenant of KV. No credentials or per-request Redis keys.

An account has four expiring collections: active owner tokens, counts by team, the fair queue,
and queue deadlines. A rate bucket has one expiring timestamp. Redis TIME is the shared clock.
Scripts are atomic; lost replies are ambiguous, never permission to release another owner.
This is admission accounting, not fencing of work already running at an external provider.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

LEASE_MS = 30_000
MAX_WAITERS = 1024
POLL_S = 0.25
RENEW_S = 10.0

# All keys for one provider share a hash tag. Only catalog identifiers/account labels are
# inputs; hashing bounds the key length and keeps labels out of store diagnostics.
def pool_keys(pool: str) -> list[str]:
    provider = hashlib.sha256(pool.split("#", 1)[0].encode()).hexdigest()[:32]
    account = hashlib.sha256(pool.encode()).hexdigest()
    prefix = f"provider-admission:{{{provider}}}:{account}"
    return [f"{prefix}:{part}" for part in ("active", "teams", "queue", "deadlines")]


def rate_key(pool: str, bucket: str) -> str:
    return pool_keys(pool)[0].removesuffix("active") + "rate:" + hashlib.sha256(bucket.encode()).hexdigest()


_COMMON = """
local clock = redis.call('TIME')
local now = tonumber(clock[1]) * 1000 + tonumber(clock[2]) / 1000
local function team(token) return string.match(token, '^([^:]+):') end
local function drop_active(keys, token)
    if redis.call('ZREM', keys[1], token) == 1 then
        local t = team(token)
        if redis.call('HINCRBY', keys[2], t, -1) <= 0 then redis.call('HDEL', keys[2], t) end
    end
end
local function prune(keys)
    for _, token in ipairs(redis.call('ZRANGEBYSCORE', keys[1], '-inf', now)) do
        drop_active(keys, token)
    end
    for _, token in ipairs(redis.call('ZRANGEBYSCORE', keys[4], '-inf', now)) do
        redis.call('ZREM', keys[3], token)
        redis.call('ZREM', keys[4], token)
    end
end
local function active(keys, token)
    return tonumber(redis.call('HGET', keys[2], team(token)) or '0')
end
local function state(keys, token)
    if redis.call('ZSCORE', keys[1], token) then return 'acquired' end
    if redis.call('ZSCORE', keys[3], token) then return 'waiting' end
    return 'missing'
end
local function touch(keys, ttl)
    if redis.call('ZCARD', keys[1]) == 0 and redis.call('ZCARD', keys[3]) == 0 then
        redis.call('DEL', unpack(keys))
    else
        for _, key in ipairs(keys) do redis.call('PEXPIRE', key, ttl) end
    end
end
local function dispatch(keys, cap, lease)
    local room = cap - redis.call('ZCARD', keys[1])
    if room <= 0 then return end
    local line = redis.call('ZRANGE', keys[3], 0, -1, 'WITHSCORES')
    -- Group the FIFO line by team, then use a heap ordered by (active, oldest). Work is
    -- O(waiters + grants * log(teams)), even when a large account empties at once.
    local groups, heap = {}, {}
    for i = 1, #line, 2 do
        local t = team(line[i])
        local g = groups[t]
        if not g then
            g = {t=t, n=tonumber(redis.call('HGET', keys[2], t) or '0'), tokens={}, seq={}, head=1}
            groups[t] = g
        end
        table.insert(g.tokens, line[i]); table.insert(g.seq, tonumber(line[i+1]))
    end
    local function less(a,b)
        return a.n < b.n or (a.n == b.n and a.seq[a.head] < b.seq[b.head])
    end
    local function push(g)
        local i = #heap + 1
        while i > 1 do
            local parent = math.floor(i / 2)
            if not less(g, heap[parent]) then break end
            heap[i] = heap[parent]; i = parent
        end
        heap[i] = g
    end
    local function pop()
        local first, last = heap[1], table.remove(heap)
        if #heap > 0 then
            local i = 1
            while i * 2 <= #heap do
                local child = i * 2
                if child < #heap and less(heap[child+1], heap[child]) then child = child+1 end
                if not less(heap[child], last) then break end
                heap[i] = heap[child]; i = child
            end
            heap[i] = last
        end
        return first
    end
    for _, g in pairs(groups) do push(g) end
    while room > 0 and #heap > 0 do
        local g = pop()
        local token = g.tokens[g.head]
        redis.call('ZREM', keys[3], token); redis.call('ZREM', keys[4], token)
        redis.call('ZADD', keys[1], now + lease, token)
        redis.call('HINCRBY', keys[2], g.t, 1)
        g.n = g.n + 1; g.head = g.head + 1; room = room - 1
        if g.head <= #g.tokens then push(g) end
    end
end
"""

_RATE = _COMMON + """
local rows = cjson.decode(ARGV[1])
local cap, budget = tonumber(ARGV[2]), tonumber(ARGV[3])
local best, best_load, best_wait, next_at = 1, math.huge, math.huge, 0
for i, row in ipairs(rows) do
    local offset = (i-1)*5
    local keys = {KEYS[offset+1], KEYS[offset+2], KEYS[offset+3], KEYS[offset+4]}
    prune(keys)
    local load = 0
    if cap > 0 then load = math.max(0, redis.call('ZCARD', keys[1]) + redis.call('ZCARD', keys[3]) - cap + 1) end
    local rate = redis.call('HMGET', KEYS[offset+5], 'at', 'interval')
    local at = now
    if tonumber(rate[2]) == row then at = math.max(now, tonumber(rate[1]) or now) end
    local wait = row > 0 and at - now or 0
    if load < best_load or (load == best_load and wait < best_wait) then
        best, best_load, best_wait, next_at = i, load, wait, at
    end
end
local interval = rows[best]
if interval > 0 and best_wait <= budget then
    local key = KEYS[(best-1)*5+5]
    redis.call('HSET', key, 'at', string.format('%.3f', next_at + interval), 'interval', interval)
    redis.call('PEXPIRE', key, math.max(1, math.ceil(best_wait + interval)))
else
    best_wait = 0 -- Preserve soft overflow: do not reserve a token beyond the wait budget.
end
return {best, math.ceil(best_wait)}
"""

_CLAIM = _COMMON + """
local token, cap, wait, lease, bound = ARGV[1], tonumber(ARGV[2]), tonumber(ARGV[3]), tonumber(ARGV[4]), tonumber(ARGV[5])
local best, load, chosen = 1, math.huge, nil
for i = 1, #KEYS, 4 do
    local keys = {KEYS[i], KEYS[i+1], KEYS[i+2], KEYS[i+3]}
    prune(keys)
    -- A driver retry after a lost reply must find its first account, not take another one.
    if state(keys, token) ~= 'missing' then best, chosen = (i+3)/4, keys; break end
    local n = math.max(0, redis.call('ZCARD', keys[1]) + redis.call('ZCARD', keys[3]) - cap + 1)
    if n < load then best, load, chosen = (i+3)/4, n, keys end
end
local status = state(chosen, token)
if status == 'missing' then
    if redis.call('ZCARD', chosen[1]) < cap and redis.call('ZCARD', chosen[3]) == 0 then
        redis.call('ZADD', chosen[1], now + lease, token)
        redis.call('HINCRBY', chosen[2], team(token), 1)
        touch(chosen, lease + 1000)
        return {best, 'acquired', active(chosen, token)}
    end
    if redis.call('ZCARD', chosen[3]) >= bound then return {best, 'full', active(chosen, token)} end
    local sequence = redis.call('HINCRBY', chosen[2], '@sequence', 1)
    redis.call('ZADD', chosen[3], sequence, token)
    redis.call('ZADD', chosen[4], now + wait, token)
end
dispatch(chosen, cap, lease)
touch(chosen, math.max(lease, wait) + 1000)
return {best, state(chosen, token), active(chosen, token)}
"""

_UPDATE = _COMMON + """
local cap, lease = tonumber(ARGV[1]), tonumber(ARGV[2])
local renew, waiting, release = cjson.decode(ARGV[3]), cjson.decode(ARGV[4]), cjson.decode(ARGV[5])
prune(KEYS)
for _, token in ipairs(release) do
    drop_active(KEYS, token)
    redis.call('ZREM', KEYS[3], token); redis.call('ZREM', KEYS[4], token)
end
for _, token in ipairs(renew) do
    -- XX only, after expiry pruning: an old owner can never recreate a lease.
    redis.call('ZADD', KEYS[1], 'XX', now + lease, token)
end
dispatch(KEYS, cap, lease)
local result = {}
for _, tokens in ipairs({renew, waiting}) do
    for _, token in ipairs(tokens) do
        -- A waiting owner acknowledges a grant here. Renew only a still-live grant.
        redis.call('ZADD', KEYS[1], 'XX', now + lease, token)
        table.insert(result, {token, state(KEYS, token), active(KEYS, token)})
    end
end
-- Queue deadlines are at most the public gate's 30-second wait limit.
touch(KEYS, math.max(lease, 30000) + 1000)
return result
"""

_ABANDON = _COMMON + """
for i = 1, #KEYS, 4 do
    local keys = {KEYS[i], KEYS[i+1], KEYS[i+2], KEYS[i+3]}
    prune(keys)
    drop_active(keys, ARGV[1])
    redis.call('ZREM', keys[3], ARGV[1]); redis.call('ZREM', keys[4], ARGV[1])
    dispatch(keys, tonumber(ARGV[2]), tonumber(ARGV[3]))
    touch(keys, math.max(tonumber(ARGV[3]), 30000) + 1000)
end
return 1
"""


class RedisProviderStore:
    def __init__(self, client, timeout_s: float):
        self.client, self.timeout_s = client, timeout_s
        self.unavailable_until = 0.0

    async def _eval(self, script, keys, *args, cleanup=False):
        if not cleanup and time.monotonic() < self.unavailable_until:
            return None
        try:
            async with asyncio.timeout(self.timeout_s):
                return await self.client.eval(script, len(keys), *keys, *args)
        except Exception as exc:  # Cancellation belongs to the caller; replies may be ambiguous.
            now = time.monotonic()
            if now >= self.unavailable_until:
                logging.getLogger("treg.capacity").warning(
                    "provider admission unavailable; using local fallback (%s)", type(exc).__name__)
            self.unavailable_until = now + 1.0
            return None

    async def reserve_rate(self, candidates: list[tuple[str, str, float]], cap: int,
                           max_wait_ms: int) -> tuple[int, int] | None:
        keys = [key for pool, bucket, _ in candidates for key in (*pool_keys(pool), rate_key(pool, bucket))]
        result = await self._eval(_RATE, keys, json.dumps([c[2] for c in candidates]), cap, max_wait_ms)
        return (int(result[0]) - 1, int(result[1])) if result is not None else None

    async def claim(self, pools: list[str], token: str, cap: int, wait_ms: int,
                    lease_ms: int = LEASE_MS) -> tuple[int, str, int] | None:
        result = await self._eval(_CLAIM, [key for pool in pools for key in pool_keys(pool)],
                                  token, cap, wait_ms, lease_ms, MAX_WAITERS)
        return (int(result[0]) - 1, result[1], int(result[2])) if result is not None else None

    async def update(self, pool: str, cap: int, *, renew=(), waiting=(), release=(),
                     lease_ms: int = LEASE_MS) -> dict[str, tuple[str, int]] | None:
        result = await self._eval(_UPDATE, pool_keys(pool), cap, lease_ms,
                                  json.dumps(list(renew)), json.dumps(list(waiting)),
                                  json.dumps(list(release)), cleanup=bool(release))
        return {r[0]: (r[1], int(r[2])) for r in result} if result is not None else None

    async def abandon(self, pools: list[str], token: str, cap: int, lease_ms: int = LEASE_MS):
        """Unknown claim reply: clean this token from every candidate in one bounded operation."""
        return await self._eval(_ABANDON, [key for pool in pools for key in pool_keys(pool)],
                                token, cap, lease_ms, cleanup=True)
