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
        assert d["bodies_kept"] == 3 and d["kept_bytes"] > 0   # counts readable versions, including dedup
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
    monkeypatch.setattr(get_settings(), "archive_serve_endpoints", EP)
    monkeypatch.setattr(get_settings(), "archive_serve_percent", 100)
    monkeypatch.setattr(get_settings(), "archive_refresh_daily_cap", 50)
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


@pytest.mark.parametrize("cache_header", [True, False])
async def test_archive_hit_never_invites_review(clients: AsyncClient, serve, monkeypatch, cache_header):
    monkeypatch.setattr(get_settings(), "review_sample_rate", 1)
    original = call_service._served_response

    def stored_response(served, body):
        response = original(served, body)
        if not cache_header:
            response.raw_headers = tuple((name, value) for name, value in response.raw_headers
                                         if name.lower() != b"x-treg-cache")
        return response

    monkeypatch.setattr(call_service, "_served_response", stored_response)
    live = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert live.headers["X-Treg-Hint"] == "review"
    assert live.headers["X-Treg-Review"] == "requested"
    await archive.drain()
    hit = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert hit.status_code == 200 and hit.content == live.content
    assert "X-Treg-Hint" not in hit.headers and "X-Treg-Review" not in hit.headers
    await audit.drain()
    assert (await clients.get("/calls")).json()[0]["cached"] is True


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
    monkeypatch.setattr(get_settings(), "archive_serve_endpoints", EP)
    monkeypatch.setattr(get_settings(), "archive_serve_percent", 100)
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


@pytest.mark.parametrize("comparison", ["strict", "typo"])
async def test_repeated_business_change_is_strict_by_default(clients: AsyncClient, shadow, monkeypatch,
                                                            comparison):
    assert not hasattr(get_settings(), "archive_comparison_mode")
    from tests.test_marketplace_call import _fake_relay
    for revenue in (100, 200, 300):
        body = json.dumps({"company": "A", "country": "US", "currency": "USD",
                           "year": 2026, "revenue": revenue}).encode()
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, body))
        response = await clients.get(f"/call/{EP}?aweme_id=7")
        assert response.status_code == 200 and response.content == body
        await archive.drain()
    keys, snaps = await _rows()
    assert keys[0].change_seen == 2 and keys[0].stable_seen == 0
    assert keys[0].ttl_s == 900
    assert len(snaps) == 3 and all(s.body is not None for s in snaps)


async def test_removed_noise_mode_cannot_weaken_strict_comparison(clients: AsyncClient, shadow, monkeypatch):
    assert not hasattr(get_settings(), "archive_comparison_mode")
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
    from tests.test_marketplace_call import _fake_relay
    bodies = [json.dumps({"req_id": i, "ts": i * 10,
                          "data": {"a": 1, "b": 2, "c": 3, "d": 4}}).encode() for i in range(3)]
    for b in bodies:
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, b))
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()                          # recordings must land in call order
    keys, _ = await _rows()
    # A legacy configuration value cannot restore heuristic comparisons. Both changes count.
    assert keys[0].change_seen == 2 and keys[0].stable_seen == 0
    assert keys[0].volatile_paths == []


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
        monkeypatch.setattr(get_settings(), "archive_serve_endpoints", EP)
        monkeypatch.setattr(get_settings(), "archive_serve_percent", 100)
        monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "transient")
        await clients.get(f"/call/{EP}?aweme_id=7")   # live, recorded
        await archive.drain()
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
        assert rep["change_outcomes"] == dict(archive.change_outcomes)
        assert rep["body_outcomes"] == dict(archive.archive_bodies.outcomes)
        monkeypatch.setitem(archive.change_outcomes, "observation_failed", 123)
        cached = (await clients.get("/admin/archive", headers={"X-Treg-Token": "ADM-TOKEN"})).json()
        assert cached["change_outcomes"]["observation_failed"] == 123
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
        monkeypatch.setattr(get_settings(), "archive_serve_endpoints", EP)
        monkeypatch.setattr(get_settings(), "archive_serve_percent", 100)
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


# ---- the call→archive link: `has_result` on /calls and GET /calls/{id}/result -------------------
# The team-facing read of a stored answer. Only metered platform 2xx calls have one (the archive's
# gate 3); every other row answers `stored: false` with a note saying which case it is.

async def _make_org_and_token(name: str, email: str) -> str:
    from treg import crypto
    from treg.models import Membership, Org, User
    async with session_maker() as db:
        org = Org(name=name, slug=name)
        db.add(org)
        await db.flush()
        user = User(email=email)
        db.add(user)
        await db.flush()
        token = crypto.new_token()
        db.add(Membership(user_id=user.id, org_id=org.id, role="admin",
                          token_hash=crypto.hash_token(token)))
        await db.commit()
    return token


async def test_a_recorded_call_links_to_its_stored_answer(clients: AsyncClient, shadow):
    r1 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r1.status_code == 200, r1.text
    await archive.drain()
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert row["has_result"] is True
    got = await clients.get(f"/calls/{row['id']}/result")
    assert got.status_code == 200, got.text
    d = got.json()
    assert d["stored"] is True and d["note"] is None and d["cached"] is False
    assert d["endpoint_id"] == EP
    assert d["response"]["body_text"] == r1.text            # the exact bytes the caller got
    assert d["response"]["status_code"] == 200 and d["response"]["origin"] == "caller"
    assert d["request"]["method"] == "GET"
    assert "aweme_id=7" in d["request"]["url"] and "count=5" in d["request"]["url"]
    assert "PLATFORM-TIKHUB-KEY" not in d["request"]["url"]  # pre-injection shape, no secret


async def test_a_served_hit_links_to_the_same_answer(clients: AsyncClient, serve):
    r1 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    await archive.drain()
    r2 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r2.headers["X-Treg-Cache"] == "hit"
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    hit, live = rows[0], rows[1]
    assert hit["cached"] is True and hit["has_result"] is True
    a = (await clients.get(f"/calls/{hit['id']}/result")).json()
    b = (await clients.get(f"/calls/{live['id']}/result")).json()
    assert a["stored"] and b["stored"] and a["cached"] is True
    assert a["response"]["body_text"] == b["response"]["body_text"] == r1.text
    assert a["response"]["version"] == b["response"]["version"]   # a hit is not a new version


async def test_an_own_tool_call_has_no_stored_result(clients: AsyncClient, shadow):
    await clients.post("/tools", json={"name": "echo", "base_url": "http://upstream",
                                       "auth": {"kind": "bearer", "secret_name": "echo"}})
    await clients.post("/secrets", json={"name": "echo", "value": "S"})
    r = await clients.get("/call/echo/anything")
    assert r.status_code == 200, r.text
    await archive.drain()
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert row["has_result"] is False
    d = (await clients.get(f"/calls/{row['id']}/result")).json()
    assert d["stored"] is False and d["request"] is None and d["response"] is None
    assert d["note"].startswith("not stored: calls on your own key")


async def test_a_platform_call_made_while_recording_was_off(clients: AsyncClient, platform_on,
                                                            monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_mode", "off")
    assert (await clients.get(f"/call/{EP}?aweme_id=7&count=5")).status_code == 200
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert row["has_result"] is False
    d = (await clients.get(f"/calls/{row['id']}/result")).json()
    assert d["stored"] is False and d["note"] == "not stored: recording was off when this call was made"


async def test_a_hash_only_answer_says_so(clients: AsyncClient, shadow, monkeypatch):
    # A judged `forbidden` licence: counted, hashed, bytes never kept.
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "forbidden")
    assert (await clients.get(f"/call/{EP}?aweme_id=7&count=5")).status_code == 200
    await archive.drain()
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert row["has_result"] is True                  # the link exists; the bytes do not
    d = (await clients.get(f"/calls/{row['id']}/result")).json()
    assert d["stored"] is False and d["note"].startswith("hash-only")
    assert d["response"]["body_text"] is None and d["response"]["size_bytes"] > 0
    assert d["request"]["method"] == "GET"            # the question is still on file


async def test_a_result_is_scoped_to_the_team(clients: AsyncClient, shadow):
    assert (await clients.get(f"/call/{EP}?aweme_id=7&count=5")).status_code == 200
    await archive.drain()
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    assert (await clients.get(f"/calls/{row['id']}/result")).status_code == 200
    outsider = await _make_org_and_token("other-team", "outsider@example.com")
    denied = await clients.get(f"/calls/{row['id']}/result", headers={"X-Treg-Token": outsider})
    assert denied.status_code == 404


async def test_the_result_never_carries_failure_evidence(clients: AsyncClient, shadow):
    # The redacted error columns stay admin-only: a failed call answers with a note, nothing more.
    from treg.models import CallRecord
    assert (await clients.get(f"/call/{EP}?aweme_id=7&count=5")).status_code == 200
    await archive.drain()
    await audit.drain()
    row = (await clients.get("/calls")).json()[0]
    async with session_maker() as s:
        rec = await s.get(CallRecord, row["id"])
        rec.status_code, rec.archive_key_hash, rec.archive_content_hash = 502, None, None
        rec.error_response = "SECRET-EVIDENCE"
        s.add(rec)
        await s.commit()
    got = await clients.get(f"/calls/{row['id']}/result")
    assert got.status_code == 200
    assert "SECRET-EVIDENCE" not in got.text
    assert got.json()["note"] == "not stored: the call failed, so there is no answer on file"


async def test_endpoint_stats_match_direct_aggregation(clients: AsyncClient, shadow, monkeypatch):
    """The rollup IS the report now, so it must equal what walking the tables would say — after
    new keys, dedup references, a changed answer, and a policy-forbidden recording."""
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"n": 1}'))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    await clients.get(f"/call/{EP}?aweme_id=7")            # identical → dedup ref, stable+1
    await archive.drain()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"n": 2}'))
    await clients.get(f"/call/{EP}?aweme_id=7")            # changed+1
    await clients.get(f"/call/{EP}?aweme_id=8")            # second key
    await archive.drain()

    from treg.models import ArchiveEndpointStat
    async with session_maker() as s:
        st = (await s.execute(select(ArchiveEndpointStat)
                              .where(ArchiveEndpointStat.endpoint_id == EP))).scalars().one()
        keys, snaps = await _rows()
        assert st.keys == len(keys) == 2
        assert st.snapshots == len(snaps) == 4
        assert st.stable == sum(k.stable_seen for k in keys) == 1
        assert st.changed == sum(k.change_seen for k in keys) == 1
        assert st.bodies_kept == sum(1 for x in snaps if x.body_storage is not None)
        assert st.kept_bytes == sum(x.size_bytes for x in snaps if x.body_storage is not None)
        assert st.newest_fetch is not None


async def test_big_bodies_compress_and_serve_back_identical(clients: AsyncClient, serve, monkeypatch):
    """A compressible body is stored smaller (enc='zlib') and the serve path returns the exact
    original bytes; a tiny body stays raw (enc NULL). size_bytes always reports the RAW size."""
    from tests.test_marketplace_call import _fake_relay
    big = ('{"rows": [' + ",".join('{"name": "creator-%d", "followers": 1000}' % i
                                   for i in range(200)) + "]}").encode()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, big))
    r1 = await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    _, snaps = await _rows()
    assert snaps[0].enc == "zlib" and len(snaps[0].body) < len(big)
    assert snaps[0].size_bytes == len(big)
    r2 = await clients.get(f"/call/{EP}?aweme_id=7")     # a hit, decompressed on the way out
    assert r2.headers.get("x-treg-cache") == "hit" and r2.content == big == r1.content

    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"n": 1}'))
    await clients.get(f"/call/{EP}?aweme_id=8")
    await archive.drain()
    _, snaps = await _rows()
    tiny = [s for s in snaps if s.size_bytes == len(b'{"n": 1}')]
    assert tiny and tiny[0].enc is None                  # below the 256-byte floor: raw


async def test_compressed_change_detection_still_compares_raw(clients: AsyncClient, shadow, monkeypatch):
    """The noise/change compare must unpack the previous body first — a compressed v1 against a
    raw-diffed v2 still counts stable/changed correctly."""
    from tests.test_marketplace_call import _fake_relay
    a = ('{"data": "' + "x" * 400 + '", "n": 1}').encode()
    b = ('{"data": "' + "x" * 400 + '", "n": 2}').encode()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, a))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    keys, _ = await _rows()
    assert keys[0].change_seen == 1 and keys[0].stable_seen == 0


# ---------------------------------------------------------------------------------------------
# The pruner (profit-shaped): strip bytes that cannot earn, never rows, never the newest body

async def _age_versions(days: int):
    from datetime import timedelta
    async with session_maker() as s:
        for v in (await s.execute(select(ArchiveSnapshot))).scalars().all():
            v.fetched_at = v.fetched_at - timedelta(days=days)
            s.add(v)
        for k in (await s.execute(select(ArchiveKey))).scalars().all():
            if k.last_requested_at is not None:
                k.last_requested_at = k.last_requested_at - timedelta(days=days)
            s.add(k)
        await s.commit()


async def test_pruner_strips_old_undemanded_bodies_keeps_rows(clients: AsyncClient, shadow, monkeypatch):
    from tests.test_marketplace_call import _fake_relay
    for n in range(4):                                   # 4 distinct answers → 4 stored bodies
        monkeypatch.setattr(call_service, "relay",
                            _fake_relay(200, b'{"v": %d, "pad": "%s"}' % (n, b"x" * 300)))
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()
    await _age_versions(days=30)                         # old AND undemanded
    assert await archive.prune_once() == 2               # newest 2 bodies kept, 2 stripped
    keys, snaps = await _rows()
    assert len(snaps) == 4                               # rows never deleted
    bodies = [s for s in snaps if s.body is not None]
    assert len(bodies) == 2
    assert max(s.version for s in bodies) == 4           # the newest survives whole
    from treg.models import ArchiveEndpointStat
    async with session_maker() as s:
        st = (await s.execute(select(ArchiveEndpointStat)
                              .where(ArchiveEndpointStat.endpoint_id == EP))).scalars().one()
        assert st.bodies_kept == 2                       # totals moved with the strip
    assert await archive.prune_once() == 0               # idempotent — nothing left to strip


async def test_pruner_never_cache_keeps_only_newest(clients: AsyncClient, shadow, monkeypatch):
    from tests.test_marketplace_call import _fake_relay
    for n in range(3):
        monkeypatch.setattr(call_service, "relay",
                            _fake_relay(200, b'{"v": %d, "pad": "%s"}' % (n, b"y" * 300)))
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()
    async with session_maker() as s:                     # the learner's verdict, set directly
        k = (await s.execute(select(ArchiveKey))).scalars().one()
        k.ttl_s = archive.TTL_NEVER
        s.add(k); await s.commit()
    assert await archive.prune_once() == 2               # young age is no defense for never-cache
    _, snaps = await _rows()
    assert sum(1 for x in snaps if x.body_storage is not None) == 1
    assert next(x.version for x in snaps if x.body is not None) == 3


async def test_pruner_spares_demanded_and_carriers(clients: AsyncClient, shadow, monkeypatch):
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"stable": "%s"}' % (b"z" * 300)))
    for _ in range(4):                                   # identical → v1 carries, v2-4 reference
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()
    await _age_versions(days=30)
    async with session_maker() as s:                     # but the key was demanded YESTERDAY
        from datetime import timedelta
        k = (await s.execute(select(ArchiveKey))).scalars().one()
        k.last_requested_at = archive._utcnow() - timedelta(days=1)
        s.add(k); await s.commit()
    assert await archive.prune_once() == 0               # demanded recently: full budget kept
    _, snaps = await _rows()
    assert snaps[0].body is not None                     # v1 the carrier untouched


async def test_recorder_concurrency_is_throttled(clients: AsyncClient, shadow, monkeypatch):
    """A burst of recordings may QUEUE, but at most _MAX_CONCURRENT_WRITES touch the database at
    once — the pool-pressure guarantee. Measured by instrumenting the locked store."""
    import asyncio as aio
    peak = 0
    active = 0
    real = archive._store_locked

    async def counting(**kw):
        nonlocal peak, active
        active += 1
        peak = max(peak, active)
        try:
            await aio.sleep(0.01)          # hold the slot long enough for overlap to show
            return await real(**kw)
        finally:
            active -= 1

    monkeypatch.setattr(archive, "_store_locked", counting)
    for i in range(12):                    # 12 concurrent recordings, one per key
        archive.record(method="GET", endpoint_id=EP, provider="tikhub",
                       url=f"https://api.example/x?aweme_id={i}", caller_body=b"",
                       headers={}, status_code=200, media_type="application/json",
                       body=b'{"n": %d}' % i)
    await archive.drain()
    assert peak <= archive._MAX_CONCURRENT_WRITES
    keys, _ = await _rows()
    assert len(keys) == 12                 # throttled, not shed: every recording landed


async def test_same_key_waiters_do_not_consume_slots_needed_by_other_keys(monkeypatch):
    """Duplicate work queues at its key lock before entering the global database-write bound."""
    import asyncio as aio

    first_same_entered = aio.Event()
    unrelated_entered = aio.Event()
    release_same = aio.Event()
    same_entries = 0

    async def blocked_store(**kw):
        nonlocal same_entries
        if kw["url"].endswith("same"):
            same_entries += 1
            first_same_entered.set()
            await release_same.wait()
        else:
            unrelated_entered.set()

    monkeypatch.setattr(archive, "_store_locked", blocked_store)
    monkeypatch.setattr(archive, "_sem", None)
    monkeypatch.setattr(archive, "_key_locks", None)
    common = dict(method="GET", endpoint_id=EP, provider="tikhub", caller_body=b"",
                  headers={}, status_code=200, media_type="application/json", body=b"{}")
    duplicates = [aio.create_task(archive._store(url="https://api.example/same", **common))
                  for _ in range(archive._MAX_CONCURRENT_WRITES)]
    await aio.wait_for(first_same_entered.wait(), timeout=1)
    await aio.sleep(0.05)  # let every duplicate reach the key lock

    unrelated = aio.create_task(archive._store(url="https://api.example/other", **common))
    try:
        await aio.wait_for(unrelated_entered.wait(), timeout=1)
        assert same_entries == 1
    finally:
        release_same.set()
        await aio.gather(*duplicates, unrelated, return_exceptions=True)


async def test_store_retries_integrity_conflicts(monkeypatch):
    """A version collision retries the complete transaction instead of dropping the snapshot."""
    from sqlalchemy.exc import IntegrityError

    attempts = 0

    async def colliding_store(**kw):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise IntegrityError("snapshot version collision", {}, Exception("duplicate"))

    monkeypatch.setattr(archive, "_store_locked", colliding_store)
    await archive._store(
        method="GET", endpoint_id=EP, provider="tikhub",
        url="https://api.example/retry", caller_body=b"", headers={},
        status_code=200, media_type="application/json", body=b"{}",
    )
    assert attempts == 3


async def test_locked_key_refreshes_identity_map_state(clients: AsyncClient, shadow):
    """The locking read observes changes committed after the session's initial unlocked lookup."""
    from sqlalchemy import update as sa_update

    from treg.infra.db import background_session_maker

    await archive._store(
        method="GET", endpoint_id=EP, provider="tikhub",
        url="https://api.example/stale", caller_body=b"", headers={},
        status_code=200, media_type="application/json", body=b'{"v": 1}',
    )
    async with background_session_maker() as first:
        cached = (await first.execute(select(ArchiveKey))).scalars().one()
        assert cached.stable_seen == 0
        async with background_session_maker() as second:
            await second.execute(sa_update(ArchiveKey).where(ArchiveKey.id == cached.id)
                                 .values(stable_seen=6))
            await second.commit()

        locked = await archive._lock_archive_key(first, cached.id)
        assert locked is cached
        assert locked.stable_seen == 6


async def test_same_key_recordings_allocate_distinct_versions(clients: AsyncClient, shadow):
    """Concurrent writers serialize on the key before choosing the next snapshot version."""
    import asyncio as aio

    await aio.gather(*(archive._store(
        method="GET", endpoint_id=EP, provider="tikhub",
        url="https://api.example/same?aweme_id=7", caller_body=b"", headers={},
        status_code=200, media_type="application/json",
        body=b'{"writer": %d}' % i,
    ) for i in range(12)))

    keys, snaps = await _rows()
    assert len(keys) == 1
    assert [snap.version for snap in snaps] == list(range(1, 13))


# ---- the 2026-09-07 OOM regression test: memory-bounded pending work ---------------------------
# _MAX_PENDING_BYTES caps total body bytes held by pending tasks. Without it, 512 pending tasks ×
# 8 MB bodies = 4 GB worst case — the exact OOM that killed production at 2026-09-07T00:43:06Z.


async def test_pending_body_bytes_are_bounded_and_excess_is_shed(monkeypatch):
    """The bytes bound sheds recordings before the count bound would — the 2026-09-07 OOM fix.

    With _MAX_CONCURRENT_WRITES=2 and heavy traffic, pending tasks holding large bodies can
    accumulate faster than they drain. The bytes cap ensures total memory held by pending work
    never exceeds a threshold, regardless of how many tasks fit under _MAX_PENDING.
    """
    import asyncio as aio

    release = aio.Event()

    async def blocked_store(**kw):
        await release.wait()

    monkeypatch.setattr(archive, "_store_locked", blocked_store)
    monkeypatch.setattr(archive, "_sem", None)
    monkeypatch.setattr(archive, "_key_locks", None)
    monkeypatch.setattr(archive, "_pending_bytes", 0)
    archive._pending.clear()
    original_max_bytes = archive._MAX_PENDING_BYTES
    monkeypatch.setattr(archive, "_MAX_PENDING_BYTES", 1000)

    common = dict(method="GET", endpoint_id=EP, provider="tikhub", caller_body=b"",
                  headers={}, status_code=200, media_type="application/json")
    try:
        archive.record(url="https://api.example/1", body=b"x" * 400, **common)
        assert len(archive._pending) == 1
        assert archive._pending_bytes == 400

        archive.record(url="https://api.example/2", body=b"y" * 400, **common)
        assert len(archive._pending) == 2
        assert archive._pending_bytes == 800

        archive.record(url="https://api.example/3", body=b"z" * 300, **common)
        assert len(archive._pending) == 2, "third recording should be shed (800 + 300 > 1000)"
        assert archive._pending_bytes == 800

        archive.record(url="https://api.example/4", body=b"w" * 150, **common)
        assert len(archive._pending) == 3, "fourth recording should fit (800 + 150 <= 1000)"
        assert archive._pending_bytes == 950
    finally:
        release.set()
        monkeypatch.setattr(archive, "_MAX_PENDING_BYTES", original_max_bytes)
        await aio.gather(*archive._pending, return_exceptions=True)
        archive._pending.clear()
        monkeypatch.setattr(archive, "_pending_bytes", 0)


async def test_pending_bytes_released_when_task_completes(monkeypatch):
    """The done callback releases body bytes so they can be reused by new recordings."""
    import asyncio as aio

    release = aio.Event()
    entered = aio.Event()

    async def blocking_store(**kw):
        entered.set()
        await release.wait()

    monkeypatch.setattr(archive, "_store_locked", blocking_store)
    monkeypatch.setattr(archive, "_sem", None)
    monkeypatch.setattr(archive, "_key_locks", None)
    monkeypatch.setattr(archive, "_pending_bytes", 0)
    archive._pending.clear()

    common = dict(method="GET", endpoint_id=EP, provider="tikhub", caller_body=b"",
                  headers={}, status_code=200, media_type="application/json")
    try:
        archive.record(url="https://api.example/1", body=b"x" * 500, **common)
        await aio.wait_for(entered.wait(), timeout=1)
        assert archive._pending_bytes == 500

        release.set()
        await aio.gather(*archive._pending, return_exceptions=True)
        await aio.sleep(0)

        assert archive._pending_bytes == 0, "bytes should be released when task completes"
        assert len(archive._pending) == 0
    finally:
        release.set()
        await aio.gather(*archive._pending, return_exceptions=True)
        archive._pending.clear()
        monkeypatch.setattr(archive, "_pending_bytes", 0)


@pytest.mark.parametrize("endpoints,percent,reason", [
    ("", 100, "endpoint_disabled"), (EP, 0, "rollout_disabled"),
    (EP, 101, "rollout_disabled"), (EP, -1, "rollout_disabled"),
])
async def test_rollout_bypass_never_queries_cache(clients, serve, monkeypatch,
                                                 endpoints, percent, reason):
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    monkeypatch.setattr(get_settings(), "archive_serve_endpoints", endpoints)
    monkeypatch.setattr(get_settings(), "archive_serve_percent", percent)
    events = []
    monkeypatch.setattr(call_service.analytics, "capture",
                        lambda who, event, props, **kw: events.append((event, props)))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200 and "x-treg-cache" not in r.headers
    props = [p for e, p in events if e == "tool_called"][-1]
    assert props["cache_outcome"] == reason
    assert not archive.worker_enabled()


def test_rollout_cohorts_are_stable_and_nested(monkeypatch):
    monkeypatch.setattr(get_settings(), "archive_serve_endpoints", EP)
    monkeypatch.setattr(get_settings(), "archive_serve_percent", 10)
    first = {str(i) for i in range(1000) if archive.rollout_reason(EP, str(i)) == "selected"}
    assert 50 < len(first) < 150
    assert first == {str(i) for i in range(1000)
                     if archive.rollout_reason(EP, str(i)) == "selected"}
    monkeypatch.setattr(get_settings(), "archive_serve_percent", 50)
    second = {str(i) for i in range(1000) if archive.rollout_reason(EP, str(i)) == "selected"}
    assert first < second
    assert archive.rollout_reason(EP, "") == "missing_cohort"


async def test_cache_reports_miss_hit_bypass_and_lookup_failure(clients, serve, monkeypatch):
    events = []
    monkeypatch.setattr(call_service.analytics, "capture",
                        lambda who, event, props, **kw: events.append((event, props)))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    await clients.get(f"/call/{EP}?aweme_id=7")
    await clients.get(f"/call/{EP}?aweme_id=7", headers={"Cache-Control": "no-cache"})
    async def broken(**kwargs):
        raise RuntimeError("lookup failed")
    monkeypatch.setattr(archive, "lookup", broken)
    response = await clients.get(f"/call/{EP}?aweme_id=7")
    assert response.status_code == 200
    props = [p for e, p in events if e == "tool_called"]
    assert [p["cache_outcome"] for p in props] == [
        "key_missing", "hit", "caller_bypass", "lookup_error"]
    assert props[1]["cache_age_s"] >= 0
    assert props[1]["cache_window_s"] == 3600
    for p in props:
        assert p["cache_lookup_ms"] >= 0
        assert p["cache_comparison_mode"] == "strict"
        assert p["cache_ttl_policy"] == "adaptive"
        assert not any(k in p for k in ("key_hash", "volatile_paths", "body", "request_headers"))


@pytest.mark.parametrize("timer,outcome", [
    (30 * 86400, "hit"), (3600, "stale"), (archive.TTL_NEVER, "ttl_disabled"),
])
async def test_strict_comparison_preserves_existing_ttl(clients, serve, monkeypatch, timer, outcome):
    from datetime import timedelta
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    async with session_maker() as session:
        key = (await session.execute(select(ArchiveKey))).scalars().one()
        snap = (await session.execute(select(ArchiveSnapshot))).scalars().one()
        key.ttl_s = timer
        snap.fetched_at -= timedelta(hours=2)
        session.add(key)
        session.add(snap)
        await session.commit()
    events = []
    monkeypatch.setattr(call_service.analytics, "capture",
                        lambda who, event, props, **kw: events.append((event, props)))
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.status_code == 200
    assert (r.headers.get("x-treg-cache") == "hit") == (outcome == "hit")
    props = [p for e, p in events if e == "tool_called"][-1]
    assert props["cache_outcome"] == outcome
    if timer > 0:
        assert props["cache_window_s"] == timer


@pytest.mark.parametrize("learned,cap,wanted,age,window,outcome", [
    (86400, 3600, None, 1800, 3600, "hit"),
    (86400, 3600, None, 7200, 3600, "stale"),
    (1800, 3600, None, 900, 1800, "hit"),
    (30 * 86400, None, None, 15 * 86400, 30 * 86400, "hit"),
    (86400, 3600, 600, 900, 600, "stale"),
    (86400, 3600, 7200, 1800, 3600, "hit"),
    (600, 3600, 1800, 900, 600, "stale"),
])
async def test_serve_caps_learned_ttl_only_by_declared_and_caller_limits(
    clients, serve, monkeypatch, learned, cap, wanted, age, window, outcome,
):
    from datetime import timedelta
    entry = catalog_store.load().by_id[EP]
    monkeypatch.setitem(entry, "cache", {"mode": "transient", "max_age_s": cap})
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    async with session_maker() as session:
        key = (await session.execute(select(ArchiveKey))).scalars().one()
        snap = (await session.execute(select(ArchiveSnapshot))).scalars().one()
        key.ttl_s = learned
        snap.fetched_at -= timedelta(seconds=age)
        session.add(key)
        session.add(snap)
        await session.commit()
    events = []
    monkeypatch.setattr(call_service.analytics, "capture",
                        lambda who, event, props, **kw: events.append((event, props)))
    headers = {} if wanted is None else {"X-Treg-Max-Age": str(wanted)}
    response = await clients.get(f"/call/{EP}?aweme_id=7", headers=headers)
    assert response.status_code == 200
    assert (response.headers.get("x-treg-cache") == "hit") == (outcome == "hit")
    props = [p for e, p in events if e == "tool_called"][-1]
    assert props["cache_outcome"] == outcome
    assert props["cache_window_s"] == window
    if cap is None:
        assert learned > archive.ttl_for(entry)


@pytest.mark.parametrize("old,new,paths", [
    ({"items": [{"id": 1}, {"id": 2}]}, {"items": [{"id": 3}, {"id": 4}]}, ["items[*].id"]),
    ({"a": 1}, {"b": 2}, ["a", "b"]),
    ({"a": True}, {"a": 1}, ["a"]),
    ({"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}},
     {"a": {"b": {"c": {"d": {"e": {"f": {"g": 2}}}}}}}, ["a.b.c.d.e.f"]),
    ([1], [1, 2], ["[*]"]),
    ({}, {}, []),
])
def test_change_summary_paths(old, new, paths):
    props = archive._change_summary(json.dumps(old).encode(), json.dumps(new).encode())
    assert props["changed_paths"] == paths
    assert props["path_count"] == len(paths)
    assert props["sole_path"] == (paths[0] if len(paths) == 1 else None)


def test_change_summary_bounds_and_non_json():
    props = archive._change_summary(b'{}', json.dumps({f"p{i}": i for i in range(25)}).encode())
    assert len(props["changed_paths"]) == 20
    assert props["path_count"] == props["leaf_count"] == 25
    assert props["truncated"] and props["sole_path"] is None
    assert archive._change_summary(b'not json', b'{}')["changed_paths"] == ["non_json"]


async def test_change_observation_is_read_only(clients, shadow, monkeypatch):
    from treg import analytics
    from tests.test_marketplace_call import _fake_relay
    events = []
    monkeypatch.setattr(analytics, "capture", lambda who, name, props, **kw: events.append((name, props)))
    bodies = [b'{"request_id":"secret-one","value":42}',
              b'{"request_id":"secret-two","value":42}']
    for body in bodies + [bodies[-1]]:
        monkeypatch.setattr(call_service, "relay", _fake_relay(200, body))
        response = await clients.get(f"/call/{EP}?aweme_id=7")
        assert response.content == body
        await archive.drain()
    keys, snaps = await _rows()
    assert (keys[0].change_seen, keys[0].stable_seen, keys[0].ttl_s) == (1, 1, 2700)
    assert snaps[2].body_of == snaps[1].id
    props = [p for name, p in events if name == "archive_change_observed"]
    assert props == [dict(endpoint_id=EP, provider="tikhub", changed_paths=["request_id"],
                          path_count=1, leaf_count=2, sole_path="request_id", truncated=False,
                          masked_by_ignore=False)]
    assert "secret" not in json.dumps(props) and "call_ref" not in props[0]


@pytest.mark.parametrize("failure", ["missing", "exception", "timeout"])
async def test_change_observation_failure_preserves_record(clients, shadow, monkeypatch, failure):
    import asyncio
    from tests.test_marketplace_call import _fake_relay
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    async def unavailable(*args):
        if failure == "exception":
            raise RuntimeError("must not leak")
        if failure == "timeout":
            await asyncio.Event().wait()
        return None
    monkeypatch.setattr(archive, "_read_change_body", unavailable)
    monkeypatch.setattr(archive, "_CHANGE_TIMEOUT_S", 0.05)
    before = archive.change_outcomes.copy()
    monkeypatch.setattr(call_service, "relay", _fake_relay(200, b'{"new":42}'))
    assert (await clients.get(f"/call/{EP}?aweme_id=7")).status_code == 200
    keys, snaps = await _rows()
    assert len(snaps) == 2 and keys[0].change_seen == 1
    counter = "body_unavailable" if failure == "missing" else "observation_failed"
    assert archive.change_outcomes[counter] == before[counter] + 1


@pytest.mark.parametrize('paths', [None, 'request_id', [1], [''], ['a..b'], ['a[0]'],
                                  ['a.*.b'], ['$.a'], ['a.[*]'], ['a '], ['a\\.b']])
@pytest.mark.parametrize('at_header', [False, True])
def test_catalog_rejects_invalid_ignore_paths(tmp_path, paths, at_header):
    import yaml
    doc = {'provider': 'test', 'endpoints': [{'id': 'test.read', 'kind': 'read'}]}
    target = doc if at_header else doc['endpoints'][0]
    target['cache'] = {'mode': 'transient', 'ignore_paths': paths}
    (tmp_path / 'test.yaml').write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError, match='cache.ignore_paths'):
        catalog_store.load(directory=tmp_path)


def test_catalog_preserves_ignore_paths_and_defaults(tmp_path):
    import yaml
    paths = ['2fa_enabled', 'data.123status', 'request_id', 'data[*].updated_at', '[*].id', 'matrix[*][*].meta.request-id']
    doc = {'provider': 'test', 'cache': {'mode': 'transient', 'ignore_paths': paths},
           'endpoints': [{'id': 'test.inherit'}, {'id': 'test.override', 'cache': {'mode': 'transient'}}]}
    (tmp_path / 'test.yaml').write_text(yaml.safe_dump(doc))
    cat = catalog_store.load(directory=tmp_path)
    assert cat.by_id['test.inherit']['cache']['ignore_paths'] == paths
    assert cat.by_id['test.override']['cache'].get('ignore_paths', []) == []
    assert all(not (ep.get('cache') or {}).get('ignore_paths')
               for ep in catalog_store.load().endpoints if isinstance(ep.get('cache'), dict))


@pytest.mark.parametrize('old,new,paths,equal', [
    ({'id': 1, 'a': 2}, {'a': 2, 'id': 3}, ['id'], True),
    ({'a': 2}, {'a': 2, 'id': 3}, ['id'], True),
    ({'a': 2}, {'a': 3}, ['missing'], False),
    ({'rows': [{'id': 1, 'value': 2}]}, {'rows': [{'id': 3, 'value': 2}]}, ['rows[*].id'], True),
    ({'rows': [{'id': 1}]}, {'rows': [{'id': 3}, {'id': 4}]}, ['rows[*].id'], False),
    ({'rows': [1]}, {'rows': [2, 3]}, ['rows[*]'], True),
    ([{'id': 1}], [{'id': 2}], ['[*].id'], True),
    ([[{'id': 1}]], [[{'id': 2}]], ['[*][*].id'], True),
    ({'a': True}, {'a': 1}, ['missing'], False),
    ({'x': {'id': 1}}, {'x': {'id': 2}}, ['x', 'x.id'], True),
])
def test_ignore_normalization(old, new, paths, equal):
    before, after = json.dumps(old).encode(), json.dumps(new).encode()
    assert (archive._normalized_hash(before, paths) == archive._normalized_hash(after, paths)) is equal
    assert json.loads(before) == old and json.loads(after) == new


@pytest.mark.parametrize('raw', [b'not JSON', b'\xff', b'{"x":NaN}'])
def test_ignore_non_json_uses_raw_comparison(raw):
    assert archive._normalized_hash(raw, ['id']) is None
    assert archive._normalized_hash(raw, ['x']) is None
    assert archive._change_summary(raw, b'{}')['changed_paths'] == ['non_json']


@pytest.mark.parametrize('paths,expected', [([], (0, 1, 1800)), (['request_id'], (1, 0, 5400))])
async def test_ignore_only_changes_learning(clients, serve, monkeypatch, paths, expected):
    from treg import analytics
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setitem(catalog_store.load().by_id[EP], 'cache',
                        {'mode': 'transient', 'ignore_paths': paths})
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda who, name, props, **kw: events.append((name, props)))
    bodies = [b'{"request_id":1,"value":42}', b'{ "value":42, "request_id":2 }']
    for raw in bodies:
        monkeypatch.setattr(call_service, 'relay', _fake_relay(200, raw))
        r = await clients.get(f'/call/{EP}?aweme_id=7', headers={'Cache-Control': 'no-cache'})
        assert r.content == raw and 'x-treg-cache' not in r.headers
        await archive.drain()
    keys, snaps = await _rows()
    assert (keys[0].stable_seen, keys[0].change_seen, keys[0].ttl_s) == expected
    assert [snap.content_hash for snap in snaps] == [archive.content_hash(b) for b in bodies]
    assert all(snap.body_of is None for snap in snaps)
    for raw in bodies:
        result = await archive.resolve_result(keys[0].key_hash, archive.content_hash(raw))
        assert result['response']['body_text'] == raw.decode()
    hit = await clients.get(f'/call/{EP}?aweme_id=7')
    assert hit.headers['x-treg-cache'] == 'hit' and hit.content == bodies[-1]
    await archive.drain()
    observed = [p for name, p in events if name == 'archive_change_observed']
    assert len(observed) == 1 and observed[0]['masked_by_ignore'] is bool(paths)


async def test_missing_ignore_baseline_falls_back_to_bytes(clients, shadow, monkeypatch):
    from treg import archive_bodies
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setitem(catalog_store.load().by_id[EP], 'cache',
                        {'mode': 'transient', 'ignore_paths': ['request_id']})
    monkeypatch.setattr(call_service, 'relay', _fake_relay(200, b'{"request_id":1}'))
    await clients.get(f'/call/{EP}?aweme_id=7')
    await archive.drain()
    async def missing(*args, **kwargs):
        return None
    monkeypatch.setattr(archive_bodies, 'read', missing)
    monkeypatch.setattr(call_service, 'relay', _fake_relay(200, b'{"request_id":2}'))
    await clients.get(f'/call/{EP}?aweme_id=7')
    keys, snaps = await _rows()
    assert len(snaps) == 2 and (keys[0].stable_seen, keys[0].change_seen) == (0, 1)


async def test_ignore_rechecks_baseline_after_concurrent_recording(clients, shadow, monkeypatch):
    import asyncio
    monkeypatch.setitem(catalog_store.load().by_id[EP], 'cache',
                        {'mode': 'transient', 'ignore_paths': ['id']})
    common = dict(method='GET', endpoint_id=EP, provider='tikhub', url='https://example.com/race',
                  caller_body=b'', headers={}, status_code=200, media_type='application/json')
    await archive._store(**common, body=b'{"id":1,"value":1}')
    second = b'{"id":2,"value":1}'
    entered, release = asyncio.Event(), asyncio.Event()
    real = archive._ignored_matches
    async def paused(kh, body, paths):
        matches = await real(kh, body, paths)
        if body == second:
            assert matches
            entered.set()
            await release.wait()
        return matches
    monkeypatch.setattr(archive, '_ignored_matches', paused)
    task = asyncio.create_task(archive._store(**common, body=second))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await archive._store(**common, body=b'{"id":3,"value":9}')
    finally:
        release.set()
        await task
    keys, snaps = await _rows()
    assert len(snaps) == 3 and (keys[0].stable_seen, keys[0].change_seen) == (0, 2)


async def test_refresh_observation_and_terminal_exclusion(clients, shadow, monkeypatch):
    from treg import analytics
    events = []
    monkeypatch.setattr(analytics, 'capture', lambda who, name, props, **kw: events.append((name, props)))
    monkeypatch.setitem(catalog_store.load().by_id[EP], 'cache',
                        {'mode': 'transient', 'ignore_paths': ['id']})
    common = dict(method='GET', endpoint_id=EP, provider='tikhub', url='https://example.com/origins',
                  caller_body=b'', headers={}, status_code=200, media_type='application/json')
    for i, origin in enumerate(('caller', 'refresh', 'async_terminal')):
        await archive._store(**common, body=json.dumps({'id': i}).encode(), origin=origin)
    keys, snaps = await _rows()
    assert len(snaps) == 3 and (keys[0].stable_seen, keys[0].change_seen) == (1, 0)
    observed = [p for name, p in events if name == 'archive_change_observed']
    assert len(observed) == 1 and observed[0]['masked_by_ignore'] is True


async def test_observation_and_ignore_reads_share_archive_budget(clients, shadow, monkeypatch):
    from tests.test_marketplace_call import _fake_relay
    monkeypatch.setitem(catalog_store.load().by_id[EP], 'cache',
                        {'mode': 'transient', 'ignore_paths': ['request_id']})
    seen = []
    for name in ('_ignored_matches', '_read_change_body'):
        original = getattr(archive, name)
        async def checked(*args, _original=original, _name=name):
            assert archive._get_sem()._value < archive._MAX_CONCURRENT_WRITES
            seen.append(_name)
            return await _original(*args)
        monkeypatch.setattr(archive, name, checked)
    for value in (1, 2):
        monkeypatch.setattr(call_service, 'relay', _fake_relay(200, json.dumps({'value': value}).encode()))
        assert (await clients.get(f'/call/{EP}?aweme_id=7')).status_code == 200
        await archive.drain()
    assert '_ignored_matches' in seen and '_read_change_body' in seen


@pytest.mark.parametrize('skip', ['disabled', 'old_hash_only', 'oversize', 'action'])
async def test_change_skips_unavailable_or_disabled_without_io(clients, shadow, monkeypatch, skip):
    from tests.test_marketplace_call import _fake_relay
    settings = get_settings()
    if skip == 'old_hash_only':
        monkeypatch.setattr(settings, 'archive_max_body_bytes', 0)
    if skip == 'action':
        monkeypatch.setitem(catalog_store.load().by_id[EP], 'kind', 'action')
    monkeypatch.setattr(call_service, 'relay', _fake_relay(200, b'{"value":1}'))
    await clients.get(f'/call/{EP}?aweme_id=7')
    await archive.drain()
    if skip == 'old_hash_only':
        monkeypatch.setattr(settings, 'archive_max_body_bytes', 2_000_000)
    if skip == 'oversize':
        monkeypatch.setattr(settings, 'archive_max_body_bytes', 0)
    if skip == 'disabled':
        monkeypatch.setattr(settings, 'archive_change_observation_enabled', False)
    async def forbidden(*args):
        pytest.fail('skipped observation must not start any read/compute')
    monkeypatch.setattr(archive, '_read_change_body', forbidden)
    monkeypatch.setattr(archive, '_change_compute', forbidden)
    before = archive.change_outcomes.copy()
    monkeypatch.setattr(call_service, 'relay', _fake_relay(200, b'{"value":2}'))
    await clients.get(f'/call/{EP}?aweme_id=7')
    await archive.drain()
    assert archive.change_outcomes == before
    assert len((await _rows())[1]) == 2


async def test_cancelled_change_compute_keeps_slot_until_thread_finishes():
    import asyncio
    import threading
    started, release = threading.Event(), threading.Event()
    def compute():
        started.set()
        release.wait(2)
    async def run():
        async with archive._get_sem():
            await archive._change_compute(compute)
    task = asyncio.create_task(run())
    try:
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done()
        assert archive._get_sem()._value == archive._MAX_CONCURRENT_WRITES - 1
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert archive._get_sem()._value == archive._MAX_CONCURRENT_WRITES


def test_change_summary_never_reserializes_subtrees(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('structure comparison must not reserialize JSON')
    monkeypatch.setattr(archive.json, 'dumps', forbidden)
    assert archive._change_summary(b'{"a":[{"b":1}]}', b'{"a":[{"b":2}]}')['changed_paths'] == ['a[*].b']
