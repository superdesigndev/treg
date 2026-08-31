"""The archive — every platform answer, kept and versioned. (PR 1: skeleton, no behavior.)

Two concepts, one word each (charter discipline):

- **cache**: the newest stored answer for a key, served instead of a vendor call while it is
  fresh. Serving arrives in a later PR and only when `TREG_ARCHIVE_MODE=serve`.
- **archive**: every version of every answer, forever, each with its timestamp. The cache is the
  archive's newest layer. History is deliberately kept — it is the future data product, not waste.

What this module owns: the mode gate, the per-endpoint eligibility policy, and the cache key.
What it will NEVER own: money. A cached hit tags records "cached" and nothing else — billing of a
cached hit is an explicitly deferred product decision, and no code here may touch ledger/billing.

The mode is a three-position switch, staged like a rollout and reversible without a deploy:
  off    → the archive does not exist at runtime (default).
  shadow → record + learn from responses we already relay; serve nothing (phase 0).
  serve  → shadow, plus eligible fresh hits are answered from the store (phase 1+).

Eligibility is three gates, in order, all of which must pass:
  1. Kind — actions (submit/send/write) are never stored; only data reads pass.
  2. License — per catalog entry: `cache: forbidden | transient | archive`, carried with the
     license quote and source URL exactly like `cost` provenance. Absent field ⇒ forbidden:
     a provider nobody has judged must not be stored by default.
  3. Tier — only METERED PLATFORM calls (treg's own vendor key) are recorded; those responses are
     already fully buffered for the settle, so recording adds no latency and no new data path.
     Own-key and own-tool calls stream and are never touched.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl

from .config import get_settings

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
from datetime import datetime, timezone

_log = logging.getLogger("treg.archive")
_pending: set[asyncio.Task] = set()
_MAX_PENDING = 512
# A recording is best-effort by contract (audit's discipline: shed, never wedge). A database that
# does not answer in this window — a lock, a stuck pool slot, a dying connection — costs ONE
# dropped sample, which the next identical call re-supplies; it must never hold drain(), a test,
# or a shutdown hostage. CI's serial Postgres job hung exactly that way three times before this
# bound existed, at whichever drain() happened to gather the stuck task.
_STORE_TIMEOUT_S = 30


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
) -> None:
    """Schedule one observation of a metered platform answer. Returns immediately; the write runs
    off-request on its own session. Call sites gate on `recording()` and 2xx — this function
    trusts them and never raises."""
    if len(_pending) >= _MAX_PENDING:  # shed load; the stream self-heals on the next call
        return
    task = asyncio.create_task(asyncio.wait_for(_store(
        method=method, endpoint_id=endpoint_id, provider=provider, url=url,
        caller_body=caller_body, headers=headers, status_code=status_code,
        media_type=media_type, body=body, origin=origin), timeout=_STORE_TIMEOUT_S))
    _pending.add(task)
    # NOT redundant with drain()'s own removal: on a running server drain() never fires, and this
    # callback is the only exit from `_pending` — without it the set fills to _MAX_PENDING and
    # record() sheds every recording from then on.
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Flush in-flight recordings — shutdown and tests. Bounded: every task carries its own
    _STORE_TIMEOUT_S, so this cannot wait longer than the slowest permitted recording."""
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
                _log.warning("archive recording dropped: database did not answer in %ss",
                             _STORE_TIMEOUT_S)


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
) -> None:
    """One recording: upsert the key, append a version, keep the change statistics honest.

    The license decides what is KEPT, not what is COUNTED: statistics and the content hash are
    recorded for every metered 2xx (a hash is an identity, not the content), while the body bytes
    are stored only when the catalog entry's cache policy allows it AND the body fits the size
    cap. Oversized bodies are skipped whole, never truncated. Consecutive identical answers
    deduplicate: the new version row points at the row carrying the bytes (`body_of`) — and when
    an identical answer arrives at a key whose bytes were never kept (policy or cap changed), the
    bytes are stored now, so a policy upgrade heals the store forward without a backfill."""
    try:
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        from .domain.catalog import store as catalog_store
        from .infra.db import session_maker
        from .models import ArchiveKey, ArchiveSnapshot

        entry = catalog_store.load().by_id.get(endpoint_id)
        pol = policy(entry)
        kh = cache_key(method, endpoint_id, url, caller_body, headers)
        ch = content_hash(body)
        cap = get_settings().archive_max_body_bytes
        keep_bytes = pol in _STORABLE and len(body) <= cap
        now = _utcnow()

        async with session_maker() as s:
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

            newest = (await s.execute(
                select(ArchiveSnapshot).where(ArchiveSnapshot.key_id == key.id)
                .order_by(ArchiveSnapshot.version.desc()).limit(1))).scalars().first()

            snap = ArchiveSnapshot(
                key_id=key.id, version=1 if newest is None else newest.version + 1,
                status_code=status_code, media_type=media_type, content_hash=ch,
                body=body if keep_bytes else None, size_bytes=len(body),
                fetched_at=now, origin=origin)
            if newest is not None:
                if newest.content_hash == ch:
                    key.stable_seen += 1
                    learn(key, stable=True, entry=entry)
                    carrier = newest.body_of or (newest.id if newest.body is not None else None)
                    if carrier is not None:      # bytes already on file — reference, don't repeat
                        snap.body, snap.body_of = None, carrier
                else:
                    old_body = newest.body
                    if old_body is None and newest.body_of is not None:
                        old = await s.get(ArchiveSnapshot, newest.body_of)
                        old_body = old.body if old is not None else None
                    if _noise_only(old_body, body, key):
                        # Learned request ids / server timestamps moved; the data did not.
                        key.stable_seen += 1
                        learn(key, stable=True, entry=entry)
                    else:
                        key.change_seen += 1
                        key.last_changed_at = now
                        learn(key, stable=False, entry=entry)
            key.fetched_at, key.policy = now, pol
            if origin == "caller":     # a refresh is treg asking itself — never demand
                key.last_requested_at = now
            s.add(key)
            s.add(snap)
            try:
                await s.commit()
            except IntegrityError:  # version race with a concurrent recording — drop this sample
                await s.rollback()
    except Exception:  # noqa: BLE001 — recording must never surface anywhere
        _log.warning("archive recording dropped for %s", endpoint_id, exc_info=True)


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
) -> dict[str, Any] | None:
    """A fresh stored answer for this exact question, or None (= make the live call).

    None on every uncertain branch: serving off, caller veto, unjudged/forbidden policy, no
    snapshot, stale snapshot, bytes not on file. The age check runs against the newest snapshot's
    own fetch time, and the window is min(endpoint TTL, caller X-Treg-Max-Age). Returns the
    verbatim stored bytes plus what the hook needs for headers: fetched_at and age_s."""
    try:
        if not serving() or caller_forces_live(request_headers):
            return None
        from sqlalchemy import select

        from .domain.catalog import store as catalog_store
        from .infra.db import session_maker
        from .models import ArchiveKey, ArchiveSnapshot

        entry = catalog_store.load().by_id.get(endpoint_id)
        if not storable(entry):
            return None
        wanted = caller_max_age_s(request_headers)

        kh = cache_key(method, endpoint_id, url, caller_body, {
            k: request_headers.get(k, "") for k in ("accept", "accept-language")})
        async with session_maker() as s:
            key = (await s.execute(
                select(ArchiveKey).where(ArchiveKey.key_hash == kh))).scalars().one_or_none()
            if key is None:
                return None
            if key.ttl_s == TTL_NEVER:   # the key marked itself: changes on every fetch
                return None
            # The learned per-key timer when one exists, else the fixed phase-1 guess; the
            # caller's own bar tightens, never widens.
            window = key.ttl_s if key.ttl_s > 0 else ttl_for(entry)
            if wanted is not None:
                window = min(window, wanted)
            if window <= 0:
                return None
            newest = (await s.execute(
                select(ArchiveSnapshot).where(ArchiveSnapshot.key_id == key.id)
                .order_by(ArchiveSnapshot.version.desc()).limit(1))).scalars().first()
            if newest is None or not (200 <= newest.status_code < 300):
                return None
            age_s = int((_utcnow() - newest.fetched_at).total_seconds())
            if age_s < 0 or age_s > window:
                return None
            body = newest.body
            if body is None and newest.body_of is not None:  # deduplicated — follow the carrier
                carrier = await s.get(ArchiveSnapshot, newest.body_of)
                body = carrier.body if carrier is not None else None
            if body is None:  # hash-only history (policy or size cap at record time)
                return None
        _touch(kh)
        return {"body": body, "media_type": newest.media_type,
                "status_code": newest.status_code, "fetched_at": newest.fetched_at,
                "age_s": age_s}
    except Exception:  # noqa: BLE001 — a lookup fault must degrade to a live call, never a 500
        _log.warning("archive lookup failed for %s — serving live", endpoint_id, exc_info=True)
        return None


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

        from .infra.db import session_maker
        from .models import ArchiveKey

        async with session_maker() as s:
            await s.execute(update(ArchiveKey).where(ArchiveKey.key_hash == key_hash)
                            .values(last_requested_at=_utcnow()))
            await s.commit()
    except Exception:  # noqa: BLE001
        _log.warning("archive touch dropped", exc_info=True)


# ---------------------------------------------------------------------------------------------
# The learner (PR 5) — AIMD timers and noise detection, applied inside the recorder.

TTL_FLOOR_S = 60
TTL_CEILING_S = 30 * 86400
TTL_NEVER = -1          # the key marked itself never-cache: changes on every fetch
_NEVER_AFTER = 4        # consecutive changed refetches (no stables) before self-marking
_NOISE_MAX_LEAF_SHARE = 0.4   # a repeated identical diff-set is noise only if it is a MINOR
_NOISE_MIN_LEAVES = 5         # corner of a body with at least this many leaves — a tiny body
#                               whose one value moves (a price) must stay "changed", never noise.


def _leaf_paths(node: Any, prefix: str = "$", *, depth: int = 0, out: list | None = None) -> list[str]:
    """Dotted leaf paths of a JSON tree; list items collapse to `[]` so per-row ids do not
    explode one logical path into hundreds. Bounded by depth and count — comparison machinery
    must never be the expensive part of a recording."""
    if out is None:
        out = []
    if len(out) >= 400 or depth > 6:
        return out
    if isinstance(node, dict):
        for k, v in node.items():
            _leaf_paths(v, f"{prefix}.{k}", depth=depth + 1, out=out)
    elif isinstance(node, list):
        for v in node[:50]:
            _leaf_paths(v, f"{prefix}[]", depth=depth + 1, out=out)
    else:
        out.append(prefix)
    return out


def _changed_paths(old: Any, new: Any, prefix: str = "$", *, depth: int = 0,
                   out: set | None = None) -> set[str]:
    """Leaf paths whose values differ between two JSON trees (same collapse rules as above)."""
    if out is None:
        out = set()
    if len(out) >= 400 or depth > 6:
        return out
    if isinstance(old, dict) and isinstance(new, dict):
        for k in set(old) | set(new):
            _changed_paths(old.get(k), new.get(k), f"{prefix}.{k}", depth=depth + 1, out=out)
    elif isinstance(old, list) and isinstance(new, list):
        for a, b in zip(old[:50], new[:50]):
            _changed_paths(a, b, f"{prefix}[]", depth=depth + 1, out=out)
        if len(old) != len(new):
            out.add(f"{prefix}[]")
    elif old != new:
        out.add(prefix)
    return out


def _noise_only(old_body: bytes | None, new_body: bytes, key) -> bool:
    """True when this refetch's difference is the SAME small diff-set as last time — learned
    request ids and server timestamps, not data. Two guards keep a real signal out of the noise
    bin: the identical set must repeat (first occurrence always counts as changed, and gets
    remembered as the candidate), and it must be a minor share (< 40%) of a body with at least
    5 leaves — a tiny body whose one value moves every fetch is a PRICE, not noise."""
    if not old_body:
        return False
    try:
        old_json, new_json = json.loads(old_body), json.loads(new_body)
    except (ValueError, UnicodeDecodeError):
        return False
    changed = _changed_paths(old_json, new_json)
    if not changed:
        return False
    known = set(key.volatile_paths or [])
    leaves = len(_leaf_paths(new_json))
    is_noise = (changed <= known
                and leaves >= _NOISE_MIN_LEAVES
                and len(changed) / leaves < _NOISE_MAX_LEAF_SHARE)
    # Remember this diff-set as the next candidate either way (bounded), so the SAME noise
    # repeating is recognized from its second occurrence on.
    key.volatile_paths = sorted(changed)[:50]
    return is_noise


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
    return serving() and get_settings().archive_refresh_daily_cap > 0


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
    from .infra.db import session_maker
    from .models import ArchiveKey, ArchiveSnapshot

    now = _utcnow()
    cat = catalog_store.load()
    async with session_maker() as s:
        candidates = (await s.execute(
            select(ArchiveKey)
            .where(ArchiveKey.ttl_s > 0, ArchiveKey.req_url != "",
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

    refreshed = 0
    for key in candidates:
        if refreshed >= _REFRESH_PER_PASS:
            break
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
