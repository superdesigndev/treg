"""A team's archive opt-out (archive.md, "Opting out"): the gate on the call path, the setting that
records it, and the separate erasure of what the team stored (team deletion, the worker command).

Runs in the sqlite suite and in CI's serial Postgres job like test_archive.py.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from conftest import verified_signup
from treg import archive, archive_bodies, audit, bootstrap
from treg.application import archive_erasure
from treg.application.call import service as call_service
from treg.infra.db import session_maker
from treg.models import ArchiveEndpointStat, ArchiveKey, ArchiveKeyOrg, ArchiveSnapshot, Org
from tests.fake_object_store import MemoryObjectStore
from tests.test_archive import (  # noqa: F401 - fixtures
    EP, OWN, PLAT, _own_key, _rows, _spend_entries, _vendor_says, own_key_serve, platform_on, serve,
)


async def _org_id(clients, headers=None) -> int:
    return (await clients.get("/orgs", headers=headers or {})).json()[0]["org_id"]


async def _settings(clients, org_id, headers=None) -> dict:
    r = await clients.get(f"/orgs/{org_id}/settings", headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


async def _opt_out(clients, org_id, headers=None) -> dict:
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": False}, headers=headers or {})
    assert r.status_code == 200, r.text
    return r.json()


async def _marks():
    async with session_maker() as s:
        return (await s.execute(select(ArchiveKeyOrg))).scalars().all()


async def _record_own_answer(org_id: int, body: bytes, question: str = "aweme_id=7") -> None:
    """An own-credential answer for `org_id`, keyed to the org, as the recorder stores it."""
    await archive._store(
        method="GET", endpoint_id=EP, provider="tikhub",
        url=f"https://api.tikhub.io/x?{question}", caller_body=b"", headers={},
        status_code=200, media_type="application/json", body=body,
        origin_org_id=org_id, scope=f"org:{org_id}")


# ---------------------------------------------------------------------------------------------
# The gate: an opted-out team is never answered from the archive and never recorded into it

async def test_an_opted_out_team_is_neither_served_nor_recorded_on_treg_key(
        clients: AsyncClient, serve, monkeypatch):
    org_id = await _org_id(clients)
    r1 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r1.status_code == 200
    await archive.drain()
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 1 and len(await _marks()) == 1
    live = int(r1.headers["X-Treg-Cost-Micro"])

    events = []
    monkeypatch.setattr(call_service.analytics, "capture",
                        lambda who, event, props, **kw: events.append((event, props)))
    cfg = await _opt_out(clients, org_id)
    assert cfg["archive"] is False
    # The public answer is on file and the question is a repeat for this team, yet it reaches
    # the vendor at full price: no hit, no repeat discount, no mark.
    r2 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r2.status_code == 200 and "x-treg-cache" not in r2.headers
    assert int(r2.headers["X-Treg-Cost-Micro"]) == live
    props = [p for e, p in events if e == "tool_called"][-1]
    assert props["cache_outcome"] == "org_opt_out"
    await archive.drain()
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 1                     # nothing new recorded
    # A fresh question: not recorded either, and no mark for the team.
    await clients.get(f"/call/{EP}?aweme_id=8")
    await archive.drain()
    keys, snaps = await _rows()
    assert len(keys) == 1 and len(snaps) == 1
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert [row.get("cached") for row in rows[:2]] == [False, False]
    # Back in: the next call is a hit again, at full price (the erasure took the team's mark).
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": True})
    assert r.json()["archive"] is True
    r3 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r3.headers["X-Treg-Cache"] == "hit"


async def test_an_opted_out_teams_own_key_calls_skip_the_archive(
        clients: AsyncClient, own_key_serve, monkeypatch):
    _vendor_says(monkeypatch, OWN)
    await _own_key(clients)
    org_id = await _org_id(clients)
    await _opt_out(clients, org_id)
    r1 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r1.status_code == 200 and r1.content == OWN
    keys, snaps = await _rows()
    assert keys == [] and snaps == []                              # nothing recorded
    _vendor_says(monkeypatch, b'{"changed": true}')
    r2 = await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    assert r2.status_code == 200 and r2.content == b'{"changed": true}'   # always live
    assert "x-treg-cache" not in r2.headers
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert not any(row.get("cached") for row in rows[:2])
    assert not any(row["has_result"] for row in rows[:2])


# ---------------------------------------------------------------------------------------------
# The erasure: what the team stored goes, what other teams stored stays

async def test_erasure_removes_the_teams_private_keys_and_marks_and_nothing_else(
        clients: AsyncClient, own_key_serve, monkeypatch):
    # Team A: an own-key answer (org-scoped key) and a platform answer it paid for (public key).
    _vendor_says(monkeypatch, OWN)
    await _own_key(clients)
    await clients.get(f"/call/{EP}?aweme_id=7&count=5")
    await archive.drain()
    org_a = await _org_id(clients)
    # Team B: its own own-key answer, and a platform answer to A's question.
    other = await verified_signup(clients, json={"email": "other-team@example.com"})
    hb = {"X-Treg-Token": other.json()["token"]}
    _vendor_says(monkeypatch, PLAT)
    await clients.get(f"/call/{EP}?aweme_id=7&count=5", headers=hb)       # public key, B's mark
    await archive.drain()
    await _own_key(clients, hb)
    _vendor_says(monkeypatch, b'{"b": "own"}')
    await clients.get(f"/call/{EP}?aweme_id=9", headers=hb)             # B's org key
    await archive.drain()
    # A keeps its own key, so no metered call ever marks it; give A a mark by hand to prove the
    # sweep takes the team's marks along with its keys.
    async with session_maker() as s:
        s.add(ArchiveKeyOrg(org_id=org_a, key_hash="a" * 64))
        await s.commit()
    keys, snaps = await _rows()
    assert sorted(k.scope or "public" for k in keys) == ["org", "org", "public"]
    assert sorted(r.org_id == org_a for r in await _marks()) == [False, True]

    # Still in the archive: erasure refuses, so nothing is removed while recording goes on.
    with pytest.raises(archive_erasure.NotOptedOut):
        await archive_erasure.erase_org(org_a)
    await _opt_out(clients, org_a)
    result = await archive_erasure.erase_org(org_a)
    assert result["keys"] == 1
    keys, snaps = await _rows()
    assert sorted(k.scope or "public" for k in keys) == ["org", "public"]
    assert all(s.origin_org_id != org_a for s in snaps)
    assert all(r.org_id != org_a for r in await _marks())
    # A second run finds nothing and changes nothing.
    assert (await archive_erasure.erase_org(org_a))["keys"] == 0
    # B is untouched: its own answer is still a hit.
    _vendor_says(monkeypatch, b'{"must": "not be asked"}')
    r = await clients.get(f"/call/{EP}?aweme_id=9", headers=hb)
    assert r.headers["X-Treg-Cache"] == "hit" and r.content == b'{"b": "own"}'
    # The endpoint's running totals went down with the rows, never below zero.
    async with session_maker() as s:
        stat = (await s.execute(select(ArchiveEndpointStat).where(
            ArchiveEndpointStat.endpoint_id == EP))).scalar_one()
    assert stat.keys == 2 and stat.snapshots == 2 and stat.bodies_kept == 2


async def test_erasure_deletes_orphaned_objects_but_keeps_shared_ones(
        clients: AsyncClient, own_key_serve, monkeypatch):
    """Bodies are content-addressed: an object another team's snapshot still points at stays."""
    store = MemoryObjectStore()
    bootstrap.configure_archive_object_store(store)
    monkeypatch.setattr(archive.get_settings(), "archive_body_write", "both")
    try:
        _vendor_says(monkeypatch, OWN)
        await _own_key(clients)
        await clients.get(f"/call/{EP}?aweme_id=7")                 # A: OWN bytes
        await clients.get(f"/call/{EP}?aweme_id=8")                 # A: OWN bytes again (same hash)
        await archive.drain()
        other = await verified_signup(clients, json={"email": "sharer@example.com"})
        hb = {"X-Treg-Token": other.json()["token"]}
        await _own_key(clients, hb)
        await clients.get(f"/call/{EP}?aweme_id=7", headers=hb)      # B: the SAME bytes, its own key
        _vendor_says(monkeypatch, b'{"only": "a"}')
        await clients.get(f"/call/{EP}?aweme_id=10")                # A: bytes nobody else has
        await archive.drain()
        import hashlib
        shared, only_a = hashlib.sha256(OWN).hexdigest(), hashlib.sha256(b'{"only": "a"}').hexdigest()
        assert shared in store.objects and only_a in store.objects
        org_a = await _org_id(clients)
        await _opt_out(clients, org_a)
        assert (await archive_erasure.erase_org(org_a))["objects"] == 1
        assert shared in store.objects and only_a not in store.objects
        assert store.delete_calls == 1
        # The upload cache forgot the deleted hash: an identical answer recorded later uploads
        # again instead of pointing at bytes that are gone (R2-only would otherwise lose it).
        await clients.patch(f"/orgs/{org_a}/settings", json={"archive": True})
        _vendor_says(monkeypatch, b'{"only": "a"}')
        await clients.get(f"/call/{EP}?aweme_id=11")
        await archive.drain()
        assert only_a in store.objects
        # Another process's cache cannot be told; its dedup path asks the store first. Simulate
        # the stale entry: the object is deleted behind the cache's back, and the next identical
        # answer is uploaded again rather than pointed at nothing.
        store.objects.pop(only_a)
        assert only_a in archive_bodies._uploaded
        _vendor_says(monkeypatch, b'{"only": "a"}')
        await clients.get(f"/call/{EP}?aweme_id=12")
        await archive.drain()
        assert only_a in store.objects
        async with session_maker() as s:
            left = (await s.execute(select(ArchiveSnapshot))).scalars().all()
        assert len(left) == 3 and all(row.content_hash in store.objects for row in left)
    finally:
        await archive.drain()
        bootstrap.configure_archive_object_store(None)


async def test_a_failed_object_delete_keeps_the_rows_for_a_retry(
        clients: AsyncClient, own_key_serve, monkeypatch):
    store = MemoryObjectStore()
    bootstrap.configure_archive_object_store(store)
    monkeypatch.setattr(archive.get_settings(), "archive_body_write", "both")
    try:
        _vendor_says(monkeypatch, OWN)
        await _own_key(clients)
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()
        org_a = await _org_id(clients)
        await _opt_out(clients, org_a)
        store.fail_deletes = True
        with pytest.raises(RuntimeError):
            await archive_erasure.erase_org(org_a)
        keys, _ = await _rows()
        assert len(keys) == 1                                       # the rows wait for the retry
        store.fail_deletes = False
        assert (await archive_erasure.erase_org(org_a))["keys"] == 1
        assert store.objects == {}
        keys, _ = await _rows()
        assert keys == []
    finally:
        await archive.drain()
        bootstrap.configure_archive_object_store(None)


async def test_deleting_a_team_erases_its_archive_too(clients: AsyncClient, own_key_serve, monkeypatch):
    store = MemoryObjectStore()
    bootstrap.configure_archive_object_store(store)
    monkeypatch.setattr(archive.get_settings(), "archive_body_write", "both")
    try:
        _vendor_says(monkeypatch, OWN)
        await _own_key(clients)
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()
        org = (await clients.get("/orgs")).json()[0]
        assert store.objects
        # A second team to prove the owner's delete only takes the owner's rows.
        other = await verified_signup(clients, json={"email": "stays@example.com"})
        hb = {"X-Treg-Token": other.json()["token"]}
        await _own_key(clients, hb)
        _vendor_says(monkeypatch, PLAT)
        await clients.get(f"/call/{EP}?aweme_id=7", headers=hb)
        await archive.drain()
        r = await clients.delete(f"/orgs/{org['org_id']}", params={"confirm": org["slug"]})
        assert r.status_code == 200, r.text
        keys, snaps = await _rows()
        assert len(keys) == 1 and len(snaps) == 1 and snaps[0].origin_org_id != org["org_id"]
        assert list(store.objects) == [snaps[0].content_hash]
    finally:
        await archive.drain()
        bootstrap.configure_archive_object_store(None)


# ---------------------------------------------------------------------------------------------
# The setting itself

async def test_opt_out_is_admin_only_and_records_the_moment(clients: AsyncClient):
    org_id = await _org_id(clients)
    cfg = await _settings(clients, org_id)
    assert cfg["archive"] is True and cfg["archive_opt_out_at"] is None
    cfg = await _opt_out(clients, org_id)
    assert cfg["archive"] is False and cfg["archive_opt_out_at"]
    first = cfg["archive_opt_out_at"]
    # Opting out again does not move the record: the objection stands from its first moment.
    assert (await _opt_out(clients, org_id))["archive_opt_out_at"] == first
    async with session_maker() as s:
        row = await s.get(Org, org_id)
    assert row.archive_opt_out_at is not None
    # A member who is not an admin may read it but not change it.
    inv = await clients.post(f"/orgs/{org_id}/invites", json={"email": "plain@example.com", "role": "member"})
    accepted = await clients.post("/invites/accept", json={"code": inv.json()["code"], "email": "plain@example.com"})
    assert accepted.status_code == 200, accepted.text
    headers = {"X-Treg-Token": accepted.json()["token"]}
    assert (await _settings(clients, org_id, headers))["archive"] is False
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": True}, headers=headers)
    assert r.status_code == 403


# ---------------------------------------------------------------------------------------------
# Erasure beside the archive's other writers

async def test_two_erasers_on_the_same_rows_subtract_the_totals_once(
        clients: AsyncClient, own_key_serve, monkeypatch):
    """The running totals are taken from what each statement deleted, so a second eraser that
    finds the rows gone (the sweep racing an owner delete, two instances) subtracts nothing."""
    _vendor_says(monkeypatch, OWN)
    await _own_key(clients)
    await clients.get(f"/call/{EP}?aweme_id=7")
    await clients.get(f"/call/{EP}?aweme_id=8")
    await archive.drain()
    org_id = await _org_id(clients)
    from treg.domain.governance.teams import erase_archive_rows, private_archive_key_ids
    async with session_maker() as s:
        key_ids = await private_archive_key_ids(s, org_id)
    assert len(key_ids) == 2
    async with session_maker() as a, session_maker() as b:
        assert await erase_archive_rows(a, org_id, key_ids) == 2
        await a.commit()
        assert await erase_archive_rows(b, org_id, key_ids) == 0    # the loser: nothing counted
        await b.commit()
    async with session_maker() as s:
        stat = (await s.execute(select(ArchiveEndpointStat).where(
            ArchiveEndpointStat.endpoint_id == EP))).scalar_one()
    assert (stat.keys, stat.snapshots, stat.bodies_kept, stat.kept_bytes) == (0, 0, 0, 0)


async def test_deleting_a_team_fences_its_writes_first(clients: AsyncClient, own_key_serve, monkeypatch):
    """The owner delete opts the team out before erasing, so nothing new of the team's is
    recorded from that moment, and a team still in the archive is never erased by accident."""
    org = (await clients.get("/orgs")).json()[0]
    seen = []
    real = archive_erasure.erase_org

    async def spy(org_id, **kw):
        async with session_maker() as s:
            row = await s.get(Org, org_id)
        seen.append(row.archive_opt_out_at is not None)
        return await real(org_id, **kw)
    monkeypatch.setattr(archive_erasure, "erase_org", spy)
    r = await clients.delete(f"/orgs/{org['org_id']}", params={"confirm": org["slug"]})
    assert r.status_code == 200, r.text
    assert seen == [True]


async def test_the_worker_command_erases_one_opted_out_team(clients: AsyncClient, own_key_serve, monkeypatch, capsys):
    from treg import worker
    _vendor_says(monkeypatch, OWN)
    await _own_key(clients)
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    org_id = await _org_id(clients)
    monkeypatch.setattr(worker, "_need_server", lambda: None)
    # The command's startup check refuses a Postgres database without TREG_SECRET_KEY, which the
    # CI Postgres job does not set; the check is the server's, not this command's, to test.
    import treg.infra.db as infra_db

    async def verified():
        return None
    monkeypatch.setattr(infra_db, "verify_db", verified)
    import argparse
    assert await worker._admin_erase_archive(argparse.Namespace(org=org_id)) == 1   # still in
    assert "still in the archive" in capsys.readouterr().out
    await _opt_out(clients, org_id)
    assert await worker._admin_erase_archive(argparse.Namespace(org=org_id)) == 0
    keys, _ = await _rows()
    assert keys == []
