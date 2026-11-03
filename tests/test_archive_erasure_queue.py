"""The queued archive erasure (archive.md, "Erasure"): public-keys mode, resume, races with other
teams, timeouts, the lock, cancel, the admin API and the owner delete.

Rows are seeded directly so a test can shape exactly which team marked which key. Runs in the
sqlite suite and against Postgres under TREG_TEST_DB_URL (the lock is then a real advisory lock).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import archive, bootstrap
from treg.application import archive_erasure as erasure
from treg.config import get_settings
from treg.domain.governance import archive_erasure as rows
from treg.infra.db import session_maker
from treg.models import (
    ArchiveEndpointStat, ArchiveErasureRequest, ArchiveKey, ArchiveKeyOrg, ArchiveSnapshot, Org, User,
)
from treg.timeutil import utcnow_naive
from tests.fake_object_store import MemoryObjectStore
from tests.test_archive_r2 import URL as R2_URL, r2  # noqa: F401 - fixture

ADMIN = "ENV-ADMIN-SECRET"
EPX = "test.erasure.endpoint"


@pytest.fixture
def store(monkeypatch):
    s = MemoryObjectStore()
    bootstrap.configure_archive_object_store(s)
    monkeypatch.setattr(erasure, "_PAUSE_S", 0)
    yield s
    bootstrap.configure_archive_object_store(None)


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    yield {"X-Treg-Token": ADMIN}
    get_settings.cache_clear()


async def _org(name: str, *, opted_out: bool = True) -> int:
    async with session_maker() as s:
        org = Org(name=name, slug=name, archive_opt_out_at=utcnow_naive() if opted_out else None)
        s.add(org)
        await s.commit()
        return org.id


async def _key(store, name: str, *, scope: str | None = None, origin: int | None = None,
               bodies: tuple[bytes, ...] | None = None) -> int:
    """One archive key with a snapshot per body; each body also lands in the object store."""
    bodies = bodies if bodies is not None else (name.encode(),)
    async with session_maker() as s:
        key = ArchiveKey(key_hash=_h(name), endpoint_id=EPX, scope=scope)
        s.add(key)
        await s.flush()
        for version, body in enumerate(bodies, 1):
            content_hash = hashlib.sha256(body).hexdigest()
            s.add(ArchiveSnapshot(key_id=key.id, version=version, content_hash=content_hash,
                                  size_bytes=len(body), body_storage="r2", origin_org_id=origin))
            store.objects[content_hash] = body
        stat = await s.get(ArchiveEndpointStat, EPX)
        if stat is None:
            stat = ArchiveEndpointStat(endpoint_id=EPX)
            s.add(stat)
        stat.keys += 1
        stat.snapshots += len(bodies)
        stat.bodies_kept += len(bodies)
        stat.kept_bytes += sum(len(b) for b in bodies)
        await s.commit()
        return key.id


def _h(name: str) -> str:
    return hashlib.sha256(name.encode()).hexdigest()


async def _mark(org_id: int, name: str) -> None:
    async with session_maker() as s:
        s.add(ArchiveKeyOrg(org_id=org_id, key_hash=_h(name)))
        await s.commit()


async def _key_names() -> set[int]:
    async with session_maker() as s:
        return set((await s.execute(select(ArchiveKey.id))).scalars().all())


async def _marks_of(org_id: int) -> set[str]:
    async with session_maker() as s:
        return set((await s.execute(select(ArchiveKeyOrg.key_hash).where(
            ArchiveKeyOrg.org_id == org_id))).scalars().all())


async def _enqueue(org_id: int, *, include_public: bool = True) -> int:
    async with session_maker() as s:
        return (await erasure.enqueue(s, org_id, include_public=include_public,
                                      reference="test", requested_by="tests"))["request_id"]


async def _request(request_id: int) -> ArchiveErasureRequest:
    async with session_maker() as s:
        return await s.get(ArchiveErasureRequest, request_id)


async def _world(store):
    """Team A (being erased) and team B. Keys:
    only_a    public, marked by A only                -> erased with include_public
    shared    public, marked by A and B               -> kept
    b_only    public, marked by B only                -> kept
    a_private A's own key                             -> always erased
    A's and B's public keys share one body, which must stay."""
    a, b = await _org("team-a"), await _org("team-b")
    ids = {
        "only_a": await _key(store, "only_a", bodies=(b"only-a-1", b"common")),
        "shared": await _key(store, "shared", bodies=(b"common",)),
        "b_only": await _key(store, "b_only"),
        "a_private": await _key(store, "a_private", scope="org", origin=a),
    }
    for name in ("only_a", "shared"):
        await _mark(a, name)
    for name in ("shared", "b_only"):
        await _mark(b, name)
    return a, b, ids


# ---------------------------------------------------------------------------------------------
# Selection

async def test_public_mode_erases_only_keys_no_other_team_marked(clients: AsyncClient, store):
    a, b, ids = await _world(store)
    request_id = await _enqueue(a)
    result = await erasure.run_pending()
    assert result["requests"][0]["outcome"] == "done"
    assert await _key_names() == {ids["shared"], ids["b_only"]}
    assert await _marks_of(a) == set() and await _marks_of(b) == {_h("shared"), _h("b_only")}
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    assert sha(b"only-a-1") not in store.objects and sha(b"a_private") not in store.objects
    assert sha(b"common") in store.objects            # the shared key still points at it
    req = await _request(request_id)
    assert (req.status, req.keys, req.snapshots, req.objects, req.marks) == ("done", 2, 3, 2, 2)
    assert req.finished_at is not None and req.phase == rows.PHASE_DONE
    async with session_maker() as s:
        stat = await s.get(ArchiveEndpointStat, EPX)
    assert (stat.keys, stat.snapshots) == (2, 2)


async def test_default_mode_keeps_public_keys(clients: AsyncClient, store):
    a, _, ids = await _world(store)
    assert (await erasure.erase_org(a))["keys"] == 1
    assert await _key_names() == {ids["only_a"], ids["shared"], ids["b_only"]}
    assert await _marks_of(a) == set()


async def test_erase_org_refuses_a_team_still_in_the_archive(clients: AsyncClient, store):
    a = await _org("still-in", opted_out=False)
    with pytest.raises(erasure.NotOptedOut):
        await erasure.erase_org(a)


# ---------------------------------------------------------------------------------------------
# Resume

class _Killed(BaseException):
    """The process dies: not an Exception, so nothing marks the request failed."""


@pytest.mark.parametrize("stop_after", [1, 2, 3, 4])
@pytest.mark.parametrize("where", ["after_objects", "after_commit"])
async def test_a_run_stopped_at_any_batch_resumes_and_finds_every_key(
        clients: AsyncClient, store, monkeypatch, stop_after, where):
    a, b = await _org("team-a"), await _org("team-b")
    erasable = [await _key(store, f"k{i}") for i in range(7)]
    for i in range(7):
        await _mark(a, f"k{i}")
    kept = await _key(store, "kept")
    await _mark(a, "kept")
    await _mark(b, "kept")
    await _key(store, "p0", scope="org", origin=a)
    await _key(store, "p1", scope="org", origin=a)
    monkeypatch.setattr(erasure, "START_BATCH", 2)
    request_id = await _enqueue(a)

    batches = {"n": 0}
    real_objects, real_commit = erasure._objects, erasure._commit_progress

    async def objects(hashes):
        await real_objects(hashes)
        if where == "after_objects":
            batches["n"] += 1
            if batches["n"] == stop_after:
                raise _Killed()

    async def commit(db, run, **kw):
        out = await real_commit(db, run, **kw)
        if where == "after_commit" and kw.get("cursor") and run.phase != rows.PHASE_MARKS:
            batches["n"] += 1
            if batches["n"] == stop_after:
                raise _Killed()
        return out

    monkeypatch.setattr(erasure, "_objects", objects)
    monkeypatch.setattr(erasure, "_commit_progress", commit)
    with pytest.raises(_Killed):
        await erasure.run_pending()
    req = await _request(request_id)
    assert req.status == rows.RUNNING
    # The marks of every key not yet erased are still there, so the resume can find them.
    async with session_maker() as s:
        left = set((await s.execute(select(ArchiveKey.key_hash).where(
            ArchiveKey.id.in_(erasable)))).scalars().all())
    assert left <= await _marks_of(a)

    monkeypatch.setattr(erasure, "_objects", real_objects)
    monkeypatch.setattr(erasure, "_commit_progress", real_commit)
    result = await erasure.run_pending()
    assert result["requests"][0]["outcome"] == "done"
    assert await _key_names() == {kept}
    assert await _marks_of(a) == set() and await _marks_of(b) == {_h("kept")}
    assert (await _request(request_id)).status == rows.DONE


# ---------------------------------------------------------------------------------------------
# Races and missing objects

async def test_a_key_another_team_marks_mid_run_is_kept(clients: AsyncClient, store, monkeypatch, caplog):
    a, b = await _org("team-a"), await _org("team-b")
    late = await _key(store, "late")
    gone = await _key(store, "gone")
    await _mark(a, "late")
    await _mark(a, "gone")
    request_id = await _enqueue(a)
    real_objects = erasure._objects

    async def objects(hashes):
        await real_objects(hashes)
        if hashes:
            await _mark(b, "late")          # B asks the same question after the selection
    monkeypatch.setattr(erasure, "_objects", objects)
    with caplog.at_level(logging.WARNING, logger="treg.archive.erasure"):
        assert (await erasure.run_pending())["requests"][0]["outcome"] == "done"
    assert late in await _key_names() and gone not in await _key_names()
    assert await _marks_of(a) == set() and await _marks_of(b) == {_h("late")}
    req = await _request(request_id)
    assert (req.kept_shared, req.keys) == (1, 1)
    logged = [r.getMessage() for r in caplog.records if "became shared" in r.getMessage()]
    assert logged == [f"archive erasure request {request_id}: public key {late} became shared "
                      f"after its bodies were deleted; rows kept, 1 body object(s) gone"]


async def test_missing_objects_count_as_deleted(clients: AsyncClient, store):
    a = await _org("team-a")
    await _key(store, "k", scope="org", origin=a)
    store.objects.clear()
    assert (await erasure.erase_org(a))["keys"] == 1
    assert await _key_names() == set()


async def test_no_rows_are_deleted_when_their_bodies_live_in_a_store_this_process_lacks(
        clients: AsyncClient, store):
    """A worker started without the object-store settings would otherwise delete the rows and
    leave the bodies behind with nothing pointing at them."""
    a = await _org("team-a")
    await _key(store, "k", scope="org", origin=a)
    request_id = await _enqueue(a)
    bootstrap.configure_archive_object_store(None)
    assert (await erasure.run_pending())["requests"][0]["outcome"] == "failed"
    req = await _request(request_id)
    assert req.status == rows.FAILED and "no archive object store" in req.last_error
    assert len(await _key_names()) == 1 and store.objects


async def test_a_snapshot_whose_body_is_missing_is_served_as_a_miss(clients: AsyncClient, r2, monkeypatch):
    """The accepted race leaves a kept public key whose bytes are gone. A caller then gets a
    normal miss: the vendor is asked again and its answer relayed, never an error."""
    from tests.test_marketplace_call import _fake_relay
    from treg.application.call import service
    monkeypatch.setattr(get_settings(), "archive_body_write", "r2")
    monkeypatch.setattr(get_settings(), "archive_body_read_lookup", "r2-first")
    assert (await clients.get(R2_URL)).status_code == 200
    await archive.drain()
    assert (await clients.get(R2_URL)).headers.get("x-treg-cache") == "hit"
    r2.objects.clear()                                  # the bodies are gone, the rows stay
    monkeypatch.setattr(service, "relay", _fake_relay(200, b'{"fresh": true}'))
    r = await clients.get(R2_URL)
    assert r.status_code == 200 and "x-treg-cache" not in r.headers
    assert r.content == b'{"fresh": true}'


# ---------------------------------------------------------------------------------------------
# The lock, timeouts, cancel

async def test_a_second_run_is_refused_while_one_holds_the_lock(clients: AsyncClient, store):
    a = await _org("team-a")
    await _key(store, "k", scope="org", origin=a)
    request_id = await _enqueue(a)
    async with erasure._exclusive():
        assert "another archive erasure" in (await erasure.run_pending())["skipped"]
        with pytest.raises(erasure.Busy):
            await erasure.erase_org(a)
    assert (await _request(request_id)).status == rows.PENDING
    assert (await erasure.run_pending())["requests"][0]["outcome"] == "done"


class _Timeout(Exception):
    sqlstate = "57014"


async def test_a_timeout_halves_the_batch_and_retries(clients: AsyncClient, store, monkeypatch):
    a = await _org("team-a")
    for i in range(30):
        await _key(store, f"k{i}")
        await _mark(a, f"k{i}")
    request_id = await _enqueue(a)
    limits, fails = [], {"n": 2}
    real_select, real_delete = rows.marked_public_keys, rows.delete_unshared_public_keys

    async def select_(db, org_id, *, after_hash="", limit):
        limits.append(limit)
        return await real_select(db, org_id, after_hash=after_hash, limit=limit)

    async def delete_(db, org_id, key_ids):
        if fails["n"]:
            fails["n"] -= 1
            raise _Timeout("canceling statement due to statement timeout")
        return await real_delete(db, org_id, key_ids)
    monkeypatch.setattr(rows, "marked_public_keys", select_)
    monkeypatch.setattr(rows, "delete_unshared_public_keys", delete_)
    assert (await erasure.run_pending())["requests"][0]["outcome"] == "done"
    assert limits[:3] == [25, 12, 6]
    assert await _key_names() == set()
    req = await _request(request_id)
    assert req.status == rows.DONE and "timeout (retried)" in req.last_error


async def test_repeated_timeouts_fail_the_request(clients: AsyncClient, store, monkeypatch):
    a = await _org("team-a")
    await _key(store, "k", scope="org", origin=a)
    request_id = await _enqueue(a, include_public=False)

    async def always(db, key_ids):
        raise _Timeout("canceling statement due to statement timeout")
    monkeypatch.setattr(rows, "delete_private_keys", always)
    result = await erasure.run_pending()
    assert result["requests"][0]["outcome"] == "failed"
    req = await _request(request_id)
    assert req.status == rows.FAILED and "TooManyTimeouts" in req.last_error
    assert req.finished_at is not None
    assert len(await _key_names()) == 1                       # rows stay for a new request


_postgres_only = pytest.mark.skipif(not get_settings().database_url.startswith("postgresql"),
                                    reason="real lock_timeout and advisory lock need Postgres")


@_postgres_only
async def test_a_real_lock_timeout_is_retried_then_fails_the_request(clients: AsyncClient, store, monkeypatch):
    """A row another transaction holds makes the DELETE hit `lock_timeout`; the engine sees the
    real driver error as a timeout, retries, and fails the request after repeated ones."""
    from sqlalchemy import text

    a = await _org("team-a")
    key_id = await _key(store, "k", scope="org", origin=a)
    request_id = await _enqueue(a, include_public=False)
    monkeypatch.setattr(erasure, "_LOCK_TIMEOUT", "100ms")
    monkeypatch.setattr(erasure, "START_BATCH", 2)
    async with session_maker() as holder:
        await holder.execute(text("SELECT id FROM archivekey WHERE id = :id FOR UPDATE"), {"id": key_id})
        result = await erasure.run_pending()
        await holder.rollback()
    assert result["requests"][0]["outcome"] == "failed"
    req = await _request(request_id)
    assert req.status == rows.FAILED and "TooManyTimeouts" in req.last_error
    assert len(await _key_names()) == 1


@_postgres_only
async def test_the_advisory_lock_is_held_on_one_connection(clients: AsyncClient, store):
    from sqlalchemy import text

    async with erasure._exclusive():
        async with session_maker() as s:
            held = (await s.execute(text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) AND classid = :a AND objid = :b"),
                {"a": erasure._LOCK[0], "b": erasure._LOCK[1]})).scalar_one()
        assert held == 1
        assert "another archive erasure" in (await erasure.run_pending())["skipped"]
    async with session_maker() as s:
        assert (await s.execute(text(
            "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND database = (SELECT oid FROM pg_database WHERE datname = current_database()) AND classid = :a"),
            {"a": erasure._LOCK[0]})).scalar_one() == 0


async def test_cancel_stops_after_the_current_batch(clients: AsyncClient, store, monkeypatch, admin_env):
    a = await _org("team-a")
    for i in range(6):
        await _key(store, f"k{i}")
        await _mark(a, f"k{i}")
    monkeypatch.setattr(erasure, "START_BATCH", 2)
    request_id = await _enqueue(a)
    real_objects = erasure._objects

    async def objects(hashes):
        await real_objects(hashes)
        async with session_maker() as s:
            await erasure.cancel(s, a)
    monkeypatch.setattr(erasure, "_objects", objects)
    assert (await erasure.run_pending())["requests"][0]["outcome"] == "cancelled"
    req = await _request(request_id)
    assert req.status == rows.CANCELLED and req.finished_at is not None
    assert len(await _key_names()) == 4                        # one batch of two went
    assert (await erasure.run_pending())["requests"] == []     # nothing left to claim


# ---------------------------------------------------------------------------------------------
# The org may be gone; it may not be back in the archive

async def test_a_request_outlives_its_org(clients: AsyncClient, store):
    a = await _org("team-a")
    await _key(store, "k", scope="org", origin=a)
    request_id = await _enqueue(a)
    async with session_maker() as s:
        await s.delete(await s.get(Org, a))
        await s.commit()
    assert (await erasure.run_pending())["requests"][0]["outcome"] == "done"
    assert (await _request(request_id)).status == rows.DONE and await _key_names() == set()


async def test_a_team_back_in_the_archive_fails_the_request(clients: AsyncClient, store):
    a = await _org("team-a")
    await _key(store, "k", scope="org", origin=a)
    request_id = await _enqueue(a)
    async with session_maker() as s:
        (await s.get(Org, a)).archive_opt_out_at = None
        await s.commit()
    assert (await erasure.run_pending())["requests"][0]["outcome"] == "opted_in"
    req = await _request(request_id)
    assert req.status == rows.FAILED and "back in the archive" in req.last_error
    assert len(await _key_names()) == 1


async def test_an_empty_queue_exits_at_once(clients: AsyncClient, store):
    assert await asyncio.wait_for(erasure.run_pending(), timeout=5) == {"requests": []}


# ---------------------------------------------------------------------------------------------
# The admin API

async def test_the_admin_api_enqueues_reports_and_cancels(clients: AsyncClient, admin_env):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    path = f"/admin/orgs/{org_id}/archive-erasure"
    body = {"include_public": True, "reference": "customer email 2026-11-02"}
    # Refused before the opt-out, so nothing is erased while the team is still being recorded.
    r = await clients.post(path, json=body, headers=admin_env)
    assert r.status_code == 409 and "opt it out" in r.json()["detail"]
    assert (await clients.patch(f"/orgs/{org_id}/settings", json={"archive": False})).status_code == 200
    assert (await clients.post(path, json={"reference": "  "}, headers=admin_env)).status_code == 422
    assert (await clients.post("/admin/orgs/999999/archive-erasure", json=body, headers=admin_env)).status_code == 404
    r = await clients.post(path, json=body, headers=admin_env)
    assert r.status_code == 200, r.text
    queued = r.json()
    assert (queued["status"], queued["include_public"], queued["reference"]) == (
        "pending", True, "customer email 2026-11-02")
    assert queued["requested_by"] == "env-admin" and queued["requested_by_user_id"] is None
    # A second request while one is open is refused.
    r = await clients.post(path, json=body, headers=admin_env)
    assert r.status_code == 409 and "already" in r.json()["detail"]
    status = (await clients.get(path, headers=admin_env)).json()
    assert status["request_id"] == queued["request_id"] and status["counts"]["keys"] == 0
    cancelled = (await clients.post(f"{path}/cancel", headers=admin_env)).json()
    assert cancelled["status"] == "cancelled" and cancelled["finished_at"]
    assert (await clients.post(f"{path}/cancel", headers=admin_env)).status_code == 409
    # After the cancel a new request is allowed again.
    assert (await clients.post(path, json=body, headers=admin_env)).status_code == 200


async def test_the_admin_api_is_superadmin_only(clients: AsyncClient, admin_env):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    await clients.patch(f"/orgs/{org_id}/settings", json={"archive": False})
    path = f"/admin/orgs/{org_id}/archive-erasure"
    body = {"reference": "x"}
    for method, url in (("post", path), ("get", path), ("post", f"{path}/cancel")):
        r = await getattr(clients, method)(url, **({"json": body} if url == path and method == "post" else {}))
        assert r.status_code == 403, (method, url, r.text)          # the team owner is not staff
        anon = await getattr(clients, method)(url, headers={"X-Treg-Token": ""},
                                              **({"json": body} if url == path and method == "post" else {}))
        assert anon.status_code == 401
    async with session_maker() as s:
        user = (await s.execute(select(User).where(User.email == "tim@superdesign.dev"))).scalar_one()
        user.is_superadmin = True
        await s.commit()
        user_id = user.id
    r = await clients.post(path, json=body)
    assert r.status_code == 200, r.text
    assert (r.json()["requested_by"], r.json()["requested_by_user_id"]) == ("tim@superdesign.dev", user_id)


# ---------------------------------------------------------------------------------------------
# The owner delete

async def test_owner_delete_of_a_big_team_queues_the_erasure(clients: AsyncClient, store, monkeypatch):
    org = (await clients.get("/orgs")).json()[0]
    for i in range(3):
        await _key(store, f"k{i}", scope="org", origin=org["org_id"])
    monkeypatch.setattr(rows, "INLINE_KEY_LIMIT", 1)
    r = await clients.delete(f"/orgs/{org['org_id']}", params={"confirm": org["slug"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted_org": org["org_id"], "archive_erasure": "queued"}
    assert len(await _key_names()) == 3                         # nothing erased in the request
    async with session_maker() as s:
        req = await rows.open_request(s, org["org_id"])
        assert await s.get(Org, org["org_id"]) is None
    assert (req.status, req.include_public, req.reference) == ("pending", False, "team deletion")
    assert (await erasure.run_pending())["requests"][0]["outcome"] == "done"
    assert await _key_names() == set() and store.objects == {}


async def test_owner_delete_of_a_small_team_erases_inline(clients: AsyncClient, store):
    org = (await clients.get("/orgs")).json()[0]
    await _key(store, "k", scope="org", origin=org["org_id"])
    await _mark(org["org_id"], "k")
    r = await clients.delete(f"/orgs/{org['org_id']}", params={"confirm": org["slug"]})
    assert r.json() == {"deleted_org": org["org_id"], "archive_erasure": "done"}
    assert await _key_names() == set() and store.objects == {}
    assert await _marks_of(org["org_id"]) == set()
    async with session_maker() as s:
        assert await rows.count_open(s) == 0


async def test_the_worker_command_runs_the_queue(clients: AsyncClient, store, monkeypatch, capsys):
    import argparse
    import json

    from treg import worker
    import treg.infra.db as infra_db

    async def verified():
        return None
    monkeypatch.setattr(infra_db, "verify_db", verified)
    real_store_context = bootstrap.archive_object_store
    monkeypatch.setattr(bootstrap, "archive_object_store", lambda: real_store_context(store))
    a = await _org("team-a")
    await _key(store, "k", scope="org", origin=a)
    await _enqueue(a)
    assert await worker._admin_erase_pending(argparse.Namespace(max_seconds=60)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["requests"][0]["outcome"] == "done" and out["requests"][0]["keys"] == 1
    assert store.objects == {}
