"""The archive skeleton (PR 1): mode gate, eligibility policy, cache key, and the two tables.

No behavior exists yet — the recorder and the serve path arrive in later PRs — so these tests pin
the contracts everything later builds on: the mode degrades safely, the policy refuses every
uncertain input, the key is canonical, and the tables round-trip on both engines (this file runs
in the sqlite suite and in CI's serial Postgres job).
"""

from __future__ import annotations

import json

import pytest

from treg import archive, audit
from treg.application.call import service as call_service
from treg.archive import cache_key, content_hash, policy, storable
from treg.models import ArchiveKey, ArchiveSnapshot


# ---------------------------------------------------------------------------------------------
# Mode: a typo must disable, never enable

def test_mode_defaults_off():
    assert archive.mode() == "off"
    assert not archive.recording()
    assert not archive.serving()


@pytest.mark.parametrize("raw,expected", [
    ("shadow", "shadow"), ("serve", "serve"), ("off", "off"),
    ("SERVE", "serve"), ("  shadow ", "shadow"),          # env-var hygiene
    ("on", "off"), ("true", "off"), ("", "off"), ("srve", "off"),  # typos degrade to off
])
def test_mode_parses_and_degrades(monkeypatch, raw, expected):
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "archive_mode", raw)
    assert archive.mode() == expected


def test_serve_implies_recording(monkeypatch):
    from treg.config import get_settings
    monkeypatch.setattr(get_settings(), "archive_mode", "serve")
    assert archive.recording() and archive.serving()
    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    assert archive.recording() and not archive.serving()


# ---------------------------------------------------------------------------------------------
# Policy: forbidden on every uncertain branch

def test_policy_defaults():
    # The founder's 2026-08-29 keep-all decision: an UNJUDGED entry defaults to transient.
    assert policy(None) == "forbidden"                              # no entry: never
    assert policy({}) == "forbidden"                                # empty ≈ no entry: never
    assert policy({"kind": "read"}) == "transient"                  # unjudged license
    assert policy({"cache": "everything"}) == "transient"           # unknown value
    assert policy({"cache": {"mode": "keep"}}) == "transient"       # unknown value, dict form
    # A judged forbidden is always respected, whatever the default says.
    assert policy({"cache": "forbidden"}) == "forbidden"
    assert policy({"cache": {"mode": "forbidden", "license_quote": "q"}}) == "forbidden"


def test_keep_all_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_default_policy", "forbidden")
    assert policy({"kind": "read"}) == "forbidden"
    assert policy({"cache": "transient"}) == "transient"            # judged stays judged


def test_policy_action_beats_license():
    # Gate order: an action is never stored even when a license field says archive.
    assert policy({"kind": "action", "cache": "archive"}) == "forbidden"


def test_policy_accepts_judged_entries():
    assert policy({"cache": "transient"}) == "transient"
    assert policy({"cache": "archive"}) == "archive"
    # Provenance form — the catalog carries the license quote alongside, policy reads only mode.
    entry = {"cache": {"mode": "archive", "license_quote": "CC0", "source_url": "https://x"}}
    assert policy(entry) == "archive"
    assert storable(entry)
    assert not storable({"kind": "action", "cache": "archive"})


# ---------------------------------------------------------------------------------------------
# Cache key: canonical, deterministic, and blind to transport noise

def test_key_is_deterministic_and_canonical():
    a = cache_key("POST", "prov.x", "https://api.x/v1/q?b=2&a=1", b'{"z": 1, "a": 2}')
    b = cache_key("post", "prov.x", "https://api.x/v1/q?a=1&b=2", b'{"a": 2, "z": 1}')
    assert a == b and len(a) == 64


def test_key_separates_real_differences():
    base = cache_key("GET", "prov.x", "https://api.x/v1/q?a=1")
    assert base != cache_key("POST", "prov.x", "https://api.x/v1/q?a=1")      # method
    assert base != cache_key("GET", "prov.y", "https://api.x/v1/q?a=1")       # endpoint id
    assert base != cache_key("GET", "prov.x", "https://api.x/v1/q?a=2")       # param value
    assert base != cache_key("GET", "prov.x", "https://api.x/v1/q?a=1&b=1")   # extra param


def test_key_ignores_caller_noise_headers():
    quiet = cache_key("GET", "p.e", "https://api.x/q?a=1")
    noisy = cache_key("GET", "p.e", "https://api.x/q?a=1", headers={
        "Authorization": "Bearer zzz", "Cookie": "s=1", "User-Agent": "curl",
        "X-Treg-Token": "t", "Accept-Encoding": "gzip", "traceparent": "00-x",
    })
    assert quiet == noisy
    # …but Accept genuinely changes some vendors' answers, so it keys.
    assert quiet != cache_key("GET", "p.e", "https://api.x/q?a=1",
                              headers={"Accept": "text/csv"})


def test_key_non_json_body_hashes_raw():
    a = cache_key("POST", "p.e", "https://api.x/q", b"plain text body")
    b = cache_key("POST", "p.e", "https://api.x/q", b"plain text body")
    c = cache_key("POST", "p.e", "https://api.x/q", b"other text body")
    assert a == b != c


def test_content_hash_is_raw_identity():
    assert content_hash(b"same") == content_hash(b"same")
    assert content_hash(b"same") != content_hash(b"Same")


# ---------------------------------------------------------------------------------------------
# Tables: round-trip on the running engine (sqlite locally, Postgres in CI's serial job)

async def test_tables_round_trip(clients):  # clients fixture resets the schema on this engine
    from sqlmodel import select
    from treg.infra.db import session_maker

    async with session_maker() as s:
        key = ArchiveKey(key_hash="k" * 64, endpoint_id="prov.search", provider="prov",
                         policy="transient", ttl_s=3600, volatile_paths=["$.request_id"])
        s.add(key)
        await s.commit()
        await s.refresh(key)

        first = ArchiveSnapshot(key_id=key.id, version=1, status_code=200,
                                media_type="application/json", content_hash=content_hash(b"{}"),
                                body=b"{}", size_bytes=2, origin="caller")
        s.add(first)
        await s.commit()
        await s.refresh(first)
        # Deduplicated second version: same bytes, body carried by reference, not stored again.
        s.add(ArchiveSnapshot(key_id=key.id, version=2, status_code=200,
                              media_type="application/json", content_hash=first.content_hash,
                              body=None, body_of=first.id, size_bytes=2, origin="refresh"))
        await s.commit()

        rows = (await s.execute(select(ArchiveSnapshot).where(ArchiveSnapshot.key_id == key.id)
                                .order_by(ArchiveSnapshot.version))).scalars().all()
        assert [r.version for r in rows] == [1, 2]
        assert rows[0].body == b"{}" and rows[1].body is None
        assert rows[1].body_of == rows[0].id
        stored = (await s.execute(select(ArchiveKey)
                                  .where(ArchiveKey.key_hash == "k" * 64))).scalars().one()
        assert stored.volatile_paths == ["$.request_id"]
        assert stored.change_seen == 0 and stored.heat == 0.0


async def test_key_hash_is_unique(clients):
    from sqlalchemy.exc import IntegrityError
    from treg.infra.db import session_maker

    async with session_maker() as s:
        s.add(ArchiveKey(key_hash="dup", endpoint_id="a"))
        await s.commit()
        s.add(ArchiveKey(key_hash="dup", endpoint_id="b"))
        with pytest.raises(IntegrityError):
            await s.commit()


# ---------------------------------------------------------------------------------------------
# The recorder (PR 2): observe metered platform answers, never touch the call

from httpx import AsyncClient
from sqlalchemy import select

from treg.domain.catalog import store as catalog_store
from treg.config import get_settings
from treg.infra.db import session_maker

EP = "tikhub.tiktok.video.comments"   # tier-4 eligible in the test allow-list, GET, $0.001/call


@pytest.fixture
def platform_on(monkeypatch):
    """Tier 4 the way a deploy turns it on (mirrors test_marketplace_call)."""
    monkeypatch.setenv("TREG_PLATFORM_KEY_TIKHUB", "PLATFORM-TIKHUB-KEY")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tikhub")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def shadow(platform_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")


async def _rows():
    await archive.drain()
    async with session_maker() as s:
        keys = (await s.execute(select(ArchiveKey))).scalars().all()
        snaps = (await s.execute(
            select(ArchiveSnapshot).order_by(ArchiveSnapshot.version))).scalars().all()
        return keys, snaps


async def test_recorder_observes_a_metered_call(clients: AsyncClient, shadow):
    r = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r.status_code == 200, r.text
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 1
    assert keys[0].endpoint_id == EP and keys[0].provider == "tikhub"
    # No cache field on this entry → the keep-all default applies: bytes are stored.
    assert keys[0].policy == "transient"
    assert snaps[0].body is not None and snaps[0].body_of is None
    assert snaps[0].size_bytes > 0 and len(snaps[0].content_hash) == 64
    assert snaps[0].origin == "caller"


async def test_recorder_off_by_default(clients: AsyncClient, platform_on):
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200
    keys, snaps = await _rows()
    assert keys == [] and snaps == []


async def test_storable_policy_keeps_bytes_and_dedups(clients: AsyncClient, shadow, monkeypatch):
    entry = catalog_store.load().by_id[EP]
    monkeypatch.setitem(entry, "cache", "transient")
    r1 = await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()                              # land the recording before the repeat
    r2 = await clients.get(f"/call/{EP}?aweme_id=7")   # same key, identical echo answer
    assert r1.status_code == r2.status_code == 200
    keys, snaps = await _rows()
    assert len(keys) == 1 and [s.version for s in snaps] == [1, 2]
    assert keys[0].policy == "transient"
    assert snaps[0].body is not None                    # bytes kept once…
    assert snaps[1].body is None and snaps[1].body_of == snaps[0].id  # …then referenced
    assert keys[0].stable_seen == 1 and keys[0].change_seen == 0


async def test_different_answer_counts_as_change(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"n": 1}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()                              # land v1 before the differing answer
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"n": 2}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 2
    assert keys[0].change_seen == 1 and keys[0].stable_seen == 0
    assert keys[0].last_changed_at is not None
    assert snaps[0].body == b'{"n": 1}' and snaps[1].body == b'{"n": 2}'


async def test_different_params_are_different_keys(clients: AsyncClient, shadow):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await clients.get(f"/call/{EP}?aweme_id=8")
    keys, _ = await _rows()
    assert len(keys) == 2


async def test_oversized_body_is_counted_not_kept(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    monkeypatch.setattr(get_settings(), "archive_max_body_bytes", 4)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200
    _, snaps = await _rows()
    assert len(snaps) == 1 and snaps[0].body is None and snaps[0].size_bytes > 4


async def test_error_responses_are_not_recorded(clients: AsyncClient, shadow, monkeypatch):
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setattr(call_service, "relay", _fake_relay(500, b"boom"))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 500
    keys, snaps = await _rows()
    assert keys == [] and snaps == []


async def test_a_recorder_crash_never_fails_the_call(clients: AsyncClient, shadow, monkeypatch):
    async def _boom(**kwargs):
        raise RuntimeError("recorder exploded")
    monkeypatch.setattr(archive, "_store", _boom)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200, r.text
    await archive.drain()


# ---------------------------------------------------------------------------------------------
# The catalog cache field (PR 3): one judgment at the file header covers the provider

def test_header_cache_is_inherited_by_endpoints():
    c = catalog_store.load()
    entry = c.by_id["coingecko.simple.price"]
    assert entry["cache"]["mode"] == "transient"          # inherited from the file header
    assert archive.policy(entry) == "transient"
    assert entry["cache"]["max_age_s"] == 86400           # CoinGecko's own 24h refresh ceiling
    assert archive.policy(c.by_id["finnhub.quote"]) == "forbidden"   # judged forbidden
    assert c.by_id["finnhub.quote"]["cache"]["license_quote"]        # …with its evidence attached
    assert c.by_id["tikhub.tiktok.video.comments"]["cache"] is None  # unjudged stays absent


def test_every_declared_cache_field_in_the_catalog_is_valid():
    """A judged entry must be complete: a known mode, and provenance when declared as a dict.
    Absent is always legal (⇒ forbidden). This is the validator for the whole shipped catalog."""
    for ep in catalog_store.load().by_id.values():
        declared = ep.get("cache")
        if declared is None:
            continue
        if isinstance(declared, dict):
            assert declared.get("mode") in ("forbidden", "transient", "archive"), ep["id"]
            assert declared.get("license_quote"), f"{ep['id']}: judged cache needs its quote"
            assert declared.get("source_url"), f"{ep['id']}: judged cache needs its source"
            assert declared.get("checked"), f"{ep['id']}: judged cache needs its check date"
        else:
            assert declared in ("forbidden", "transient", "archive"), ep["id"]


async def test_recorder_respects_a_judged_forbidden(clients: AsyncClient, shadow, monkeypatch):
    """A provider judged forbidden is counted, never kept — even though the policy is declared."""
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache",
                        {"mode": "forbidden", "license_quote": "q", "source_url": "u", "checked": "d"})
    await clients.get(f"/call/{EP}?aweme_id=7")
    keys, snaps = await _rows()
    assert keys[0].policy == "forbidden"
    assert snaps[0].body is None and snaps[0].size_bytes > 0


# ---------------------------------------------------------------------------------------------
# The phase-0 report (PR 3): GET /admin/archive

async def test_admin_archive_report(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "ADM-TOKEN")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
        monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
        await clients.get(f"/call/{EP}?aweme_id=7")
        await clients.get(f"/call/{EP}?aweme_id=7")   # refetch, identical ⇒ stable
        await clients.get(f"/call/{EP}?aweme_id=9")   # second key
        await archive.drain()

        assert (await clients.get("/admin/archive")).status_code == 403  # org token is not admin
        r = await clients.get("/admin/archive", headers={"X-Treg-Token": "ADM-TOKEN"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["mode"] == "shadow" and d["keys"] == 2 and d["snapshots"] == 3
        assert d["bodies_kept"] == 2 and d["kept_bytes"] > 0   # v2 deduplicated, never re-stored
        row = next(x for x in d["endpoints"] if x["endpoint_id"] == EP)
        assert row == {"endpoint_id": EP, "provider": "tikhub", "policy": "transient",
                       "keys": 2, "refetches": 1, "stable": 1, "changed": 0,
                       "change_ratio": 0.0, "newest_fetch": row["newest_fetch"],
                       "hits": row["hits"], "kept_bytes": row["kept_bytes"]}
        assert row["newest_fetch"] is not None and row["kept_bytes"] > 0
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------------------------
# Serving (PR 4): the cache answers instead of the vendor; money is IDENTICAL to a live call

def test_ttl_fixed_guesses_and_vendor_ceiling():
    assert archive.ttl_for({"capability": "crypto.price.current"}) == 300
    assert archive.ttl_for({"capability": "ads.library.search"}) == 3600      # table default
    assert archive.ttl_for({"capability": "people.email.verify"}) == 7 * 86400
    # The vendor's declared ceiling CAPS, never widens.
    assert archive.ttl_for({"capability": "people.email.verify",
                            "cache": {"mode": "transient", "max_age_s": 60}}) == 60
    assert archive.ttl_for({"capability": "crypto.price.current",
                            "cache": {"mode": "transient", "max_age_s": 86400}}) == 300


@pytest.fixture
def serve(platform_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_mode", "serve")
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")


async def _spend_entries(clients):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    return (await clients.get(f"/orgs/{org_id}/balance")).json()["entries"]["items"]


async def test_a_hit_serves_stored_bytes_and_bills_like_live(clients: AsyncClient, serve):
    r1 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r1.status_code == 200 and "x-treg-cache" not in r1.headers
    await archive.drain()
    r2 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r2.status_code == 200, r2.text
    assert r2.headers["X-Treg-Cache"] == "hit"
    assert int(r2.headers["X-Treg-Age"]) >= 0 and r2.headers["X-Treg-Fetched-At"]
    assert r2.content == r1.content                      # verbatim stored bytes
    # Money identical to live, ON PURPOSE: both calls reserved and settled at the same price.
    assert r2.headers.get("X-Treg-Cost-Micro") == r1.headers.get("X-Treg-Cost-Micro")
    kinds = [e["kind"] for e in await _spend_entries(clients)]
    assert kinds[:4] == ["settle", "reserve", "settle", "reserve"]
    # The audit rows disagree only on the tag.
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert [row.get("cached") for row in rows[:2]] == [True, False]


async def test_a_hit_is_not_a_new_observation(clients: AsyncClient, serve):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    await clients.get(f"/call/{EP}?aweme_id=7")          # hit
    keys, snaps = await _rows()
    assert len(snaps) == 1                                # no snapshot added by the hit
    assert keys[0].stable_seen == 0 and keys[0].change_seen == 0
    assert keys[0].last_requested_at is not None          # …but demand was noted


async def test_no_cache_forces_live(clients: AsyncClient, serve):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Cache-Control": "no-cache"})
    assert r.status_code == 200 and "x-treg-cache" not in r.headers
    _, snaps = await _rows()
    assert len(snaps) == 2                                # the forced live call was recorded


async def test_max_age_zero_forces_live(clients: AsyncClient, serve):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"X-Treg-Max-Age": "0"})
    assert r.status_code == 200 and "x-treg-cache" not in r.headers


async def test_a_stale_snapshot_is_not_served(clients: AsyncClient, serve, monkeypatch):
    from datetime import timedelta
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    async with session_maker() as s:                      # age the snapshot past every window
        snap = (await s.execute(select(ArchiveSnapshot))).scalars().one()
        snap.fetched_at = snap.fetched_at - timedelta(days=30)
        s.add(snap)
        await s.commit()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and "x-treg-cache" not in r.headers


async def test_default_forbidden_never_serves(clients: AsyncClient, platform_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_mode", "serve")
    monkeypatch.setattr(get_settings(), "archive_default_policy", "forbidden")
    # With keep-all switched off, an unjudged entry is recorded hash-only and never served.
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and "x-treg-cache" not in r.headers


async def test_shadow_mode_never_serves(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and "x-treg-cache" not in r.headers


async def test_a_lookup_crash_degrades_to_live(clients: AsyncClient, serve, monkeypatch):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    async def _boom(**kwargs):
        raise RuntimeError("lookup exploded")
    monkeypatch.setattr(archive, "_touch", lambda kh: (_ for _ in ()).throw(RuntimeError))
    monkeypatch.setattr(archive, "lookup", _boom)
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and "x-treg-cache" not in r.headers


# ---------------------------------------------------------------------------------------------
# The learner (PR 5): timers that adjust, noise that stops counting, keys that opt out

async def test_stable_refetch_grows_the_timer(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()                           # land the first recording
    await clients.get(f"/call/{EP}?aweme_id=7")     # identical echo answer ⇒ stable
    keys, _ = await _rows()
    # other.* capability default is 3600; one stable step ⇒ ×1.5
    assert keys[0].ttl_s == int(keys[0].ttl_s)  # int stays int
    assert keys[0].stable_seen == 1 and keys[0].ttl_s > 3600 * 1.4


async def test_changed_refetch_shrinks_the_timer(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"n": 1}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"n": 2}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    keys, _ = await _rows()
    assert keys[0].change_seen == 1 and keys[0].ttl_s == 1800   # 3600 × 0.5


async def test_repeated_noise_counts_as_stable(clients: AsyncClient, shadow, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    from tests.test_marketplace_call import _fake_relay
    bodies = [json.dumps({"req_id": i, "ts": i * 10,
                          "data": {"a": 1, "b": 2, "c": 3, "d": 4}}).encode() for i in range(3)]
    for b in bodies:
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, b))
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()                          # recordings must land in call order
    keys, _ = await _rows()
    # fetch 2 differs (first diff: counts changed, remembers the set); fetch 3 repeats the SAME
    # small diff-set ⇒ noise ⇒ stable.
    assert keys[0].change_seen == 1 and keys[0].stable_seen == 1
    assert keys[0].volatile_paths == ["$.req_id", "$.ts"]


async def test_always_changing_key_marks_itself_never_cache(clients: AsyncClient, serve, monkeypatch):
    from tests.test_marketplace_call import _fake_relay
    for i in range(5):   # tiny 1-leaf body: the noise guard must NEVER rescue a moving price
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, json.dumps({"price": i}).encode()))
        r = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Cache-Control": "no-cache"})
        assert r.status_code == 200
        await archive.drain()
    keys, _ = await _rows()
    assert keys[0].ttl_s == archive.TTL_NEVER and keys[0].stable_seen == 0
    # …and a fresh call is NOT served from the store, however young the newest snapshot is.
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"price": 99}'))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert "x-treg-cache" not in r.headers


# ---------------------------------------------------------------------------------------------
# The refresh worker (PR 5): due + demanded + capped, through the real injection shape

class _FakeUpstream:
    """Stands in for app.state.http: answers like the vendor and remembers each request."""
    def __init__(self, body: bytes = b'{"fresh": true}'):
        self.body, self.calls = body, []

    async def request(self, method, url, params=None, headers=None, content=None):
        self.calls.append({"method": method, "url": url, "params": params or [],
                           "headers": headers or {}})
        import httpx
        return httpx.Response(200, content=self.body,
                              headers={"content-type": "application/json"})


async def _age_key(days: float, *, demanded: bool = True):
    from datetime import timedelta
    async with session_maker() as s:
        key = (await s.execute(select(ArchiveKey))).scalars().one()
        key.fetched_at = key.fetched_at - timedelta(days=days)
        key.last_requested_at = (key.fetched_at + timedelta(seconds=60)) if demanded else None
        s.add(key)
        await s.commit()
        return key.key_hash


async def test_refresh_worker_refreshes_a_due_demanded_key(clients: AsyncClient, serve):
    r = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r.status_code == 200
    await archive.drain()
    await _age_key(days=1)               # window 3600s ⇒ a day old is far past 80%
    fake = _FakeUpstream()
    assert await archive.refresh_once(fake) == 1
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["method"] == "GET" and "fetch_post_comment" in call["url"]
    assert ("aweme_id", "7") in [tuple(x) for x in _qs(call["url"])]
    # treg's platform key rode the provider's own injection shape (tikhub: Bearer header).
    assert call["headers"].get("Authorization") == "Bearer PLATFORM-TIKHUB-KEY"
    keys, snaps = await _rows()
    assert [s_.origin for s_ in snaps] == ["caller", "refresh"]
    assert keys[0].last_requested_at < keys[0].fetched_at   # a refresh is never demand


def _qs(url: str):
    from urllib.parse import parse_qsl, urlsplit
    return parse_qsl(urlsplit(url).query)


async def test_refresh_skips_undemanded_and_fresh_keys(clients: AsyncClient, serve):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    fake = _FakeUpstream()
    assert await archive.refresh_once(fake) == 0            # fresh: not due
    await _age_key(days=1, demanded=False)
    assert await archive.refresh_once(fake) == 0            # due but nobody asked
    assert fake.calls == []


async def test_refresh_daily_cap_holds(clients: AsyncClient, serve, monkeypatch):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await clients.get(f"/call/{EP}?aweme_id=8")
    await archive.drain()
    from datetime import timedelta
    async with session_maker() as s:                        # both keys due and demanded
        for key in (await s.execute(select(ArchiveKey))).scalars().all():
            key.fetched_at = key.fetched_at - timedelta(days=1)
            key.last_requested_at = key.fetched_at + timedelta(seconds=60)
            s.add(key)
        await s.commit()
    monkeypatch.setattr(get_settings(), "archive_refresh_daily_cap", 1)
    fake = _FakeUpstream()
    assert await archive.refresh_once(fake) == 1            # the cap, not the queue, decided
    assert await archive.refresh_once(fake) == 0            # today's budget is spent
    assert len(fake.calls) == 1


async def test_refresh_disabled_off_serve_or_at_cap_zero(clients: AsyncClient, serve, monkeypatch):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    await _age_key(days=1)
    fake = _FakeUpstream()
    monkeypatch.setattr(get_settings(), "archive_refresh_daily_cap", 0)
    assert await archive.refresh_once(fake) == 0
    monkeypatch.setattr(get_settings(), "archive_refresh_daily_cap", 50)
    monkeypatch.setattr(get_settings(), "archive_mode", "shadow")
    assert await archive.refresh_once(fake) == 0
    assert fake.calls == []


# ---------------------------------------------------------------------------------------------
# The panel (PR 6): the keys endpoint, the extended report, and the page shell

async def test_admin_archive_keys_endpoint(clients: AsyncClient, serve, monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "ADM-TOKEN")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(get_settings(), "archive_mode", "serve")
        monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
        await clients.get(f"/call/{EP}?aweme_id=7")   # live, recorded
        await clients.get(f"/call/{EP}?aweme_id=7")   # hit
        await archive.drain()
        await audit.drain()

        assert (await clients.get(f"/admin/archive/keys?endpoint_id={EP}")).status_code == 403
        r = await clients.get(f"/admin/archive/keys?endpoint_id={EP}",
                              headers={"X-Treg-Token": "ADM-TOKEN"})
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["endpoint_id"] == EP and len(d["keys"]) == 1
        k = d["keys"][0]
        assert k["ttl_s"] > 0 and k["question"].startswith("GET http")
        assert [v["stored"] for v in k["versions"]] == ["body"]
        # events carry the hit/live distinction the panel's feed shows
        assert [e["cached"] for e in d["events"]] == [True, False]

        rep = (await clients.get("/admin/archive", headers={"X-Treg-Token": "ADM-TOKEN"})).json()
        assert rep["hits_today"] == 1 and "worker_on" in rep and "refresh_daily_cap" in rep
        row = next(x for x in rep["endpoints"] if x["endpoint_id"] == EP)
        assert row["hits"] == 1 and row["kept_bytes"] > 0
    finally:
        get_settings.cache_clear()


async def test_archive_panel_page_serves(clients: AsyncClient):
    r = await clients.get("/admin/archive/panel")
    assert r.status_code == 200
    assert "Archive" in r.text and "TREG_ADMIN_TOKEN" in r.text  # the shell + its token gate
    assert "data-tip" in r.text                                  # the explanations shipped


async def test_admin_archive_body_viewer(clients: AsyncClient, serve, monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", "ADM-TOKEN")
    get_settings.cache_clear()
    try:
        monkeypatch.setattr(get_settings(), "archive_mode", "serve")
        monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
        await clients.get(f"/call/{EP}?aweme_id=7", headers={"Cache-Control": "no-cache"})
        await clients.get(f"/call/{EP}?aweme_id=7", headers={"Cache-Control": "no-cache"})
        await archive.drain()
        keys, snaps = await _rows()
        kh = keys[0].key_hash
        assert (await clients.get(f"/admin/archive/body?key_hash={kh}&version=1")).status_code == 403
        adm = {"X-Treg-Token": "ADM-TOKEN"}
        v1 = (await clients.get(f"/admin/archive/body?key_hash={kh}&version=1", headers=adm)).json()
        assert v1["stored"] is True and v1["body_text"] and v1["carried_by_version"] is None
        # v2 was identical ⇒ stored by reference; the viewer follows to the carrier
        v2 = (await clients.get(f"/admin/archive/body?key_hash={kh}&version=2", headers=adm)).json()
        assert v2["stored"] is True and v2["body_text"] == v1["body_text"]
        assert v2["carried_by_version"] == 1
        r = await clients.get(f"/admin/archive/body?key_hash={kh}&version=99", headers=adm)
        assert r.status_code == 404
    finally:
        get_settings.cache_clear()
