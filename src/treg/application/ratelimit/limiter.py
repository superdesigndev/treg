"""Admit or refuse one request before auth, the database or money admission see it.

Identity: the caller is the credential the route's auth honors (`request_scope.credential`), known
only once auth has accepted it and published the hash of what it used. A pinned credential (its
team fixed by the credential itself) is cached by that hash alone, so no other header can change
who the limiter thinks it is; an unpinned identity token is cached by hash and the team header auth
honored for it. A request whose credential is not resolved, or that carries none, is "unresolved".

Order: the blocklist by key hash (before auth, and by org when the key's org is cached), then the
counting layers: an unresolved request takes a lease in its client IP's small bucket, every request
one in the global ceiling (evaluated last, with a paid reserve). After auth, `access.require_member`
calls the slot's `check_org` so a blocked org is refused even on a key's first request.

Every lease is tracked from admission to release (`finish`, run shielded from cancellation by the
middleware), renewed while the request lasts (`renew_leases`, from the background), and ages out
of the store after `concurrency_ttl_s` if a process dies holding it.

The request path reads only memory (`config_state.current()`, `blocklist_state.current()`, the key
cache) plus the store round trips for its leases. A store fault fails open to per-process ceilings,
and a short cooldown keeps an outage from adding a timeout to every request.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

from ...config import get_settings
from ...domain.ratelimit import (
    ANONYMOUS_IP_REASON,
    BLOCKLIST_REASONS,
    GLOBAL_REASON,
    BlockEntry,
    Caller,
    GlobalVerdict,
    KeyCache,
    LocalCeiling,
    retry_seconds,
)
from ...infra import kv, request_scope
from . import blocklist_state, config_state, metrics, signals

log = logging.getLogger("treg.ratelimit")
_LOCAL_IP_BUCKETS = 10_000


@dataclass(frozen=True)
class RequestFacts:
    ip: str
    path: str = ""
    key_hash: str | None = None  # SHA-256 of the credential the route's auth honors, never the key
    org_header: str = ""  # X-Treg-Org, which auth honors only for unpinned identity tokens
    alias: str | None = None  # the outer request's key hash, on treg's own in-process requests
    exempt: bool = False  # the counting layers and signals skip it
    trusted: bool = False  # a server-side credential: the blocklist skips it too


@dataclass(frozen=True)
class Rejection:
    status: int
    headers: tuple[tuple[bytes, bytes], ...]
    detail: dict


@dataclass
class Lease:
    key: str
    member: str
    backend: str  # store | local


@dataclass
class Admission:
    state: config_state.ConfigState
    caller: Caller | None = None
    verdict: GlobalVerdict | None = None
    leases: list[Lease] = field(default_factory=list)
    rejection: Rejection | None = None
    visit: signals.Visit | None = None
    blocked: bool = False
    alias: str | None = None
    released: bool = False

    @property
    def backend(self) -> str:
        return self.leases[-1].backend if self.leases else ""


def _tier(caller: Caller | None) -> str:
    return "unknown" if caller is None else ("paid" if caller.paid else "unpaid")


def _ip_key(ip: str) -> str:
    return f"rl:ip:{ip}:c"


class RateLimiter:
    def __init__(self) -> None:
        s = get_settings()
        self.cache = KeyCache(ttl_s=s.rate_limit_key_cache_ttl_s, max_size=s.rate_limit_key_cache_size)
        self.local = LocalCeiling()  # the global ceiling, per process
        self.local_ip: OrderedDict[str, LocalCeiling] = OrderedDict()
        self._live: dict[str, Lease] = {}  # member -> lease, while the request runs
        self._store_skip_until = 0.0
        self._clock = time.time

    # ---- identity ---------------------------------------------------------------------------
    def resolve(self, facts: RequestFacts, now: float) -> Caller | None:
        if not facts.key_hash:
            return None
        return (self.cache.get(facts.key_hash, now=now)
                or self.cache.get(request_scope.team_key(facts.key_hash, facts.org_header), now=now))

    # ---- the request path -----------------------------------------------------------------
    async def admit(self, facts: RequestFacts, slot: dict) -> Admission:
        state = config_state.current()
        cfg = state.config
        now = self._clock()
        caller = self.resolve(facts, now)
        alias_known = not facts.alias or self.cache.get(facts.alias, now=now) is not None
        if (facts.key_hash and caller is None) or not alias_known:
            slot["want_caller"] = True
        a = Admission(state, caller=caller, alias=facts.alias)

        bl = blocklist_state.current()
        if bl is not None and bl.mode != "off" and not facts.trusted:
            slot["check_org"] = lambda org_id: self._check_org(a, facts, org_id)
            hit = self._match("key_hash", facts.key_hash)
            kind = "key_hash"
            if hit is None and caller is not None:
                hit, kind = self._match("org_id", str(caller.org_id)), "org_id"
            if hit is not None:
                a.rejection = self._blocked(a, facts, kind, hit)
                if a.rejection is not None:
                    return a

        if facts.exempt:
            metrics.count("exempt_requests")
            return a
        metrics.ip_seen(facts.ip)
        if facts.key_hash and caller is None:
            metrics.count("unknown_key_requests")  # includes the post-deploy cold start
        a.visit = signals.enter(facts.ip, caller.org_id if caller else None, facts.key_hash, facts.path,
                                unknown=bool(facts.key_hash) and caller is None)
        if cfg.mode == "off":
            return a
        enforce = cfg.mode == "enforce"
        ttl_ms, retry_ms = cfg.concurrency_ttl_s * 1000, cfg.concurrency_retry_after_s * 1000
        member = uuid.uuid4().hex
        if caller is None:
            # Unresolved: the client IP's small bucket refuses; the global ceiling only counts it,
            # because a key refused before auth could never be learned.
            v = await self._take(a, _ip_key(facts.ip), member, cfg.anonymous_ip_concurrency,
                                 ttl_ms, retry_ms, enforce, now, local=self._local_ip(facts.ip))
            self._observe(a, facts, v, "anonymous_ip_concurrency", ANONYMOUS_IP_REASON, enforce)
            if not v.admitted:
                a.rejection = self._too_many(state, "anonymous_ip_concurrency", ANONYMOUS_IP_REASON, v)
                return a
        global_enforce = enforce and caller is not None
        a.verdict = await self._take(a, kv.RL_GLOBAL_KEY, member, cfg.global_cap(_tier(caller)),
                                     ttl_ms, retry_ms, global_enforce, now, local=self.local)
        self._observe(a, facts, a.verdict, "global_concurrency", GLOBAL_REASON, global_enforce)
        if not a.verdict.admitted:
            a.rejection = self._too_many(state, "global_concurrency", GLOBAL_REASON, a.verdict)
        return a

    async def finish(self, a: Admission, slot: dict, *, status: int) -> None:
        """After the downstream app, whatever it raised or however it was cancelled (the middleware
        shields this call): give back every lease, record the signal, then let auth's answer teach
        the key cache."""
        try:
            await self._release(a)
        finally:
            caller = slot.get("caller")
            org_id = a.caller.org_id if a.caller else (caller.get("org_id") if isinstance(caller, dict) else None)
            signals.leave(a.visit, status=status, org_id=org_id, blocked=a.blocked or bool(slot.get("blocked")))
            if isinstance(caller, dict):
                self._learn(caller, a.alias)

    def _learn(self, caller: dict, alias: str | None) -> None:
        now = self._clock()
        key = caller["key_hash"] if caller.get("pinned") else request_scope.team_key(
            caller["key_hash"], caller.get("org_header", ""))
        for cache_key in (key, alias):
            if cache_key:
                self.cache.put(Caller(key_id=cache_key, org_id=caller["org_id"], org_slug=caller["org_slug"],
                                      paid=caller["paid"], org_created_at=caller["org_created_at"]), now=now)

    def _too_many(self, state, layer: str, reason: str, v: GlobalVerdict) -> Rejection:
        retry = retry_seconds(v.retry_ms)
        return Rejection(429, (
            (b"retry-after", str(retry).encode()),
            (b"x-treg-ratelimit-layer", layer.encode()),
            (b"x-treg-ratelimit-reason", reason.encode()),
        ), {"error": "rate_limited", "layer": layer, "reason": reason, "retry_after_s": retry,
            "config_version": state.version, "mode": state.config.mode})

    # ---- the blocklist ----------------------------------------------------------------------
    def _match(self, kind: str, value: str | None) -> BlockEntry | None:
        bl = blocklist_state.current()
        if bl is None or not value:
            return None
        return bl.match(kind, value, now=self._clock())

    def _check_org(self, a: Admission, facts: RequestFacts, org_id: int) -> dict | None:
        """Called by auth once it knows the org. Returns the 403 detail when it must refuse."""
        hit = self._match("org_id", str(org_id))
        if hit is None:
            return None
        rejection = self._blocked(a, facts, "org_id", hit, org_id=org_id)
        return rejection.detail if rejection is not None else None

    def _blocked(self, a: Admission, facts: RequestFacts, kind: str, entry: BlockEntry, *,
                 org_id: int | None = None) -> Rejection | None:
        bl = blocklist_state.current()
        mode = bl.mode if bl else "off"
        enforce = mode == "enforce_403"
        reason = BLOCKLIST_REASONS[kind]
        a.blocked = True
        paid = bool(a.caller and a.caller.paid)
        metrics.decision(layer="blocklist", reason=reason, result="reject" if enforce else "log_only",
                         tier=_tier(a.caller), paid=paid, version=a.state.version, mode=mode)
        metrics.event_log(logging.WARNING, "rate_limit_blocklist_match", {
            "match": kind, "reason": reason, "mode": mode, "enforced": enforce,
            "blocklist_version": bl.version if bl else None, "config_version": a.state.version,
            "key_hash": facts.key_hash[:12] if facts.key_hash else None,
            "org_id": org_id or (a.caller.org_id if a.caller else None), "ip": facts.ip, "paid": paid,
        }, budget=get_settings().rate_limit_log_max_per_window, always=paid)
        if not enforce:
            return None
        # Only the fixed code: the operator's reason text stays in the audit record, so a refused
        # caller learns nothing about what was detected.
        return Rejection(403, ((b"x-treg-block-reason", reason.encode()),), {
            "error": "blocked", "reason": reason, "blocklist_version": bl.version if bl else None,
        })

    # ---- counting layers --------------------------------------------------------------------
    def _local_ip(self, ip: str) -> LocalCeiling:
        ceiling = self.local_ip.get(ip)
        if ceiling is None:
            ceiling = self.local_ip[ip] = LocalCeiling()
            while len(self.local_ip) > _LOCAL_IP_BUCKETS:
                self.local_ip.popitem(last=False)
        self.local_ip.move_to_end(ip)
        return ceiling

    async def _take(self, a: Admission, key: str, member: str, cap: int | None, ttl_ms: int,
                    retry_ms: int, enforce: bool, now: float, *, local: LocalCeiling) -> GlobalVerdict:
        if kv.configured() and time.monotonic() >= self._store_skip_until:
            started = time.monotonic()
            try:
                admitted, ok, n = await kv.store().rl_admit_key(key, member, cap, ttl_ms, enforce)
                metrics.latency((time.monotonic() - started) * 1000)
                if admitted:
                    self._hold(a, Lease(key, member, "store"))
                return GlobalVerdict(admitted, ok, n, cap, 0 if ok else retry_ms)
            except kv.Unavailable as exc:
                self._store_fault("admit", exc)
        metrics.count("local_decisions")
        v = local.admit(member=member, cap=cap, ttl_ms=ttl_ms, retry_ms=retry_ms, enforce=enforce,
                        now_ms=int(now * 1000))
        if v.admitted:
            self._hold(a, Lease(key, member, "local"))
        return v

    def _hold(self, a: Admission, lease: Lease) -> None:
        a.leases.append(lease)
        self._live[f"{lease.key}|{lease.member}"] = lease

    def _store_fault(self, op: str, exc: Exception) -> None:
        metrics.count("store_errors")
        self._store_skip_until = time.monotonic() + get_settings().rate_limit_store_cooldown_s
        metrics.event_log(logging.WARNING, "rate_limit_store_unavailable", {"op": op, "error": str(exc)},
                          budget=get_settings().rate_limit_log_max_per_window)

    def _local_for(self, key: str) -> LocalCeiling | None:
        if key == kv.RL_GLOBAL_KEY:
            return self.local
        ip = key[len("rl:ip:"):-len(":c")] if key.startswith("rl:ip:") else None
        return self.local_ip.get(ip) if ip is not None else None

    async def _release(self, a: Admission) -> None:
        if a.released:
            return
        a.released = True
        for lease in a.leases:
            self._live.pop(f"{lease.key}|{lease.member}", None)
            if lease.backend == "local":
                ceiling = self._local_for(lease.key)
                if ceiling is not None:
                    ceiling.release(lease.member)
                continue
            try:
                await kv.store().rl_release_key(lease.key, lease.member)
            except kv.Unavailable as exc:
                # The entry ages out after concurrency_ttl_s; until then it overcounts by one.
                metrics.count("release_errors")
                self._store_fault("release", exc)
            except asyncio.CancelledError:
                metrics.count("release_cancelled")
                raise

    async def renew_leases(self) -> int:
        """Keep every in-flight request's leases fresh, so one that outlives the lease TTL (a
        stream) is still counted. Called from the background; one store call per key."""
        by_key: dict[tuple[str, str], list[str]] = {}
        for lease in list(self._live.values()):
            by_key.setdefault((lease.backend, lease.key), []).append(lease.member)
        now_ms = int(self._clock() * 1000)
        for (backend, key), members in by_key.items():
            if backend == "local":
                ceiling = self._local_for(key)
                if ceiling is not None:
                    for m in members:
                        ceiling.renew(m, now_ms=now_ms)
                continue
            try:
                await kv.store().rl_renew(key, members, config_state.current().config.concurrency_ttl_s * 1000)
            except kv.Unavailable as exc:
                self._store_fault("renew", exc)
        return sum(len(m) for m in by_key.values())

    def _observe(self, a: Admission, facts: RequestFacts, v: GlobalVerdict, layer: str, reason: str,
                 enforce: bool) -> None:
        cfg, caller = a.state.config, a.caller
        metrics.peak("global" if layer == "global_concurrency" else "ip", "concurrency",
                     "global" if layer == "global_concurrency" else facts.ip, v.in_flight)
        paid, tier = bool(caller and caller.paid), _tier(caller)
        if v.ok:
            if layer == "global_concurrency":
                metrics.decision(layer="all", reason="none", result="allow", tier=tier, paid=paid,
                                 version=a.state.version, mode=cfg.mode)
            return
        result = "reject" if enforce else "shadow_reject"
        metrics.decision(layer=layer, reason=reason, result=result, tier=tier,
                         paid=paid, version=a.state.version, mode=cfg.mode)
        metrics.event_log(
            logging.WARNING if paid else logging.INFO,
            "rate_limit_reject" if enforce else "rate_limit_would_block",
            {"layer": layer, "reason": reason, "tier": tier, "paid": paid,
             "mode": cfg.mode, "config_version": a.state.version, "backend": a.backend,
             "key_hash": facts.key_hash[:12] if facts.key_hash else None,
             "org": caller.org_slug if caller else None, "ip": facts.ip,
             "in_flight": v.in_flight, "cap": v.cap},
            budget=get_settings().rate_limit_log_max_per_window, always=paid,
        )


_limiter: RateLimiter | None = None


def limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter


def reset_for_tests() -> None:
    global _limiter
    _limiter = None
    config_state.reset_for_tests()
    blocklist_state.reset_for_tests()
    signals.reset_for_tests()
    metrics.drain()
