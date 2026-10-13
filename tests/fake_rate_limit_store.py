"""An in-memory shared store for rate-limit tests that do not need a real Redis.

It holds the `rl:` documents and counts every call, so a test can prove which code path reads
what. The ceiling raises `Unavailable` unless a test sets `admit_ok`, which sends decisions to
`LocalCeiling`; the Lua scripts run against a real Redis in `test_rate_limit_redis.py`.
"""

from __future__ import annotations

import json
from collections import Counter

from treg.infra import kv


class FakeRateLimitStore:
    def __init__(self) -> None:
        self.docs: dict[str, str] = {}
        self.claims: set[str] = set()
        self.live: dict[str, str] = {}
        self.calls: Counter = Counter()
        self.down = False
        self.windows: Counter = Counter()

    def _check(self, op: str) -> None:
        self.calls[op] += 1
        if self.down:
            raise kv.Unavailable("ConnectionError")

    async def rl_admit_key(self, key, member, cap, ttl_ms, enforce):
        self._check("rl_admit_key")
        raise kv.Unavailable("no admission in the fake store")

    async def rl_release_key(self, key, member):
        self._check("rl_release_key")

    async def rl_renew(self, key, members, ttl_ms):
        self._check("rl_renew")
        return 0

    async def rl_get(self, key):
        self._check(f"rl_get:{key}")
        return self.docs.get(key)

    async def rl_publish(self, key, value, version):
        """Compare-and-set like the Lua script: only a strictly newer version is written."""
        self._check(f"rl_publish:{key}")
        held = self.docs.get(key)
        if held:
            current = json.loads(held).get("version", 0)
            if current >= version:
                return current
        self.docs[key] = value
        return version

    async def rl_heartbeat(self, key, value, ttl_s):
        self._check("rl_heartbeat")
        self.live[key] = value

    async def rl_live(self):
        self._check("rl_live")
        return list(self.live.values())

    async def rl_memory(self, max_keys):
        self._check("rl_memory")
        return {"rl_keys_sampled": 0, "rl_bytes_sampled": 0, "rl_sample_truncated": False,
                "kv_used_memory": 0}

    async def take(self, key, limit, ttl_s):
        if self.down:
            return False
        self.windows[key] += 1
        return self.windows[key] <= limit

    async def ping(self):
        return not self.down

    async def aclose(self):
        pass


def install(monkeypatch) -> FakeRateLimitStore:
    store = FakeRateLimitStore()
    monkeypatch.setattr(kv, "configured", lambda: True)
    monkeypatch.setattr(kv, "_store", store)
    return store


def install_blocklist(*entries, version: int = 5, mode: str = "log_only"):
    """Put a blocklist in the process cache the way `blocklist_sync` does after a Postgres load.
    `entries` are (type, value, expires_in_seconds)."""
    import time

    from treg.application.ratelimit import blocklist_state
    from treg.domain.ratelimit import BlockEntry, Blocklist

    now = time.time()
    bl = Blocklist.build(version, "2026-01-01T00:00:00Z", tuple(
        BlockEntry(kind, value, "flood from one IP", now + expires_in, "ops")
        for kind, value, expires_in in entries), mode=mode)
    blocklist_state.install(bl)
    return bl
