"""What a recording keeps so the exchange can be replayed (archive.md, "What a recording keeps"):
the caller's own query on the key, the allowlisted response headers on the snapshot, and a 2xx the
endpoint's `expect` rule rejects as history that is never served, learned from or refreshed.

Runs in the sqlite suite and in CI's serial Postgres job like test_archive.py.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from treg import archive, audit
from treg.application.call import service as call_service
from treg.domain.catalog import results as catalog_results
from treg.domain.catalog import store as catalog_store
from treg.infra.db import session_maker
from treg.application.call.types import UpstreamResponse
from treg.models import ArchiveKey, ArchiveKeyOrg, ArchiveSnapshot
from tests.test_archive import (  # noqa: F401 - fixtures
    EP, _FakeUpstream, _own_key, _rows, own_key_serve, platform_on, serve, shadow,
)

FOUND = b'{"code": 0, "comments": [{"text": "hi"}]}'
FAILED = b'{"code": 500, "message": "upstream task failed"}'


def _vendor(monkeypatch, *answers: tuple[bytes, tuple[tuple[bytes, bytes], ...]]) -> list:
    """The vendor answers 200 with each scripted (body, headers) in turn; returns the asks seen."""
    seen = []

    async def _relay(request, upstream_url, tool, secrets, client, drop_params=None,
                     force_identity=False):
        body, headers = answers[len(seen)]
        seen.append(upstream_url)

        async def _stream():
            yield body

        async def _close():
            return None

        return UpstreamResponse(200, ((b"content-type", b"application/json"), *headers),
                                _stream(), _close)

    monkeypatch.setattr(call_service, "relay", _relay)
    return seen


@pytest.fixture
def legacy_expect(monkeypatch):
    """EP as an endpoint with an `expect` rule and no hit/miss adapter: it serves its newest answer."""
    monkeypatch.setitem(catalog_store.load().by_id[EP], "expect", {"json_path": "code", "equals": 0})
    real = catalog_results.has_result_rules
    monkeypatch.setattr(catalog_results, "has_result_rules",
                        lambda endpoint_id: endpoint_id != EP and real(endpoint_id))


async def _marks() -> list[ArchiveKeyOrg]:
    async with session_maker() as s:
        return (await s.execute(select(ArchiveKeyOrg))).scalars().all()


# ---------------------------------------------------------------------------------------------
# The caller's query and the allowlisted response headers

async def test_a_recording_keeps_the_callers_query_and_only_allowlisted_headers(
        clients: AsyncClient, serve, monkeypatch):
    _vendor(monkeypatch, (FOUND, (
        (b"X-Credits-Used", b"2"), (b"X-C4-Cost", b"0.5"), (b"Retry-After", b"3"),
        (b"X-Request-Id", b"req-1"), (b"x-request-id", b"req-2"),
        (b"Authorization", b"Bearer PLATFORM-TIKHUB-KEY"), (b"Set-Cookie", b"s=1"),
        (b"X-Trace-Id", b"t-PLATFORM-TIKHUB-KEY"), (b"X-Unlisted", b"1"))))
    r = await clients.get(f"/call/{EP}?count=5&aweme_id=7&count=6")
    assert r.status_code == 200, r.text
    keys, snaps = await _rows()
    # The query exactly as the caller sent it, order and repeats kept.
    assert keys[0].req_query == [["count", "5"], ["aweme_id", "7"], ["count", "6"]]
    # Charge and diagnostic headers only; never an auth header, a cookie, or a value quoting
    # the injected credential.
    assert snaps[0].response_headers == {
        "x-credits-used": "2", "x-c4-cost": "0.5", "retry-after": "3",
        "x-request-id": "req-1, req-2"}


async def test_no_headers_are_kept_where_the_bytes_are_not(clients: AsyncClient, serve, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "forbidden")
    _vendor(monkeypatch, (FOUND, ((b"X-Credits-Used", b"2"),)))
    await clients.get(f"/call/{EP}?aweme_id=7")
    _, snaps = await _rows()
    assert len(snaps) == 1 and snaps[0].body is None and snaps[0].response_headers is None


async def test_a_key_from_before_the_column_learns_its_callers_query(
        clients: AsyncClient, serve, monkeypatch):
    _vendor(monkeypatch, (FOUND, ()), (FOUND, ()))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    async with session_maker() as s:
        key = (await s.execute(select(ArchiveKey))).scalars().one()
        key.req_query = None
        s.add(key)
        await s.commit()
    await clients.get(f"/call/{EP}?aweme_id=7", headers={"Cache-Control": "no-cache"})
    keys, snaps = await _rows()
    assert len(snaps) == 2 and keys[0].req_query == [["aweme_id", "7"]]


# ---------------------------------------------------------------------------------------------
# A 2xx the `expect` rule rejects: stored as history, never an answer

async def test_an_expect_failure_is_stored_but_never_served_or_learned_from(
        clients: AsyncClient, serve, legacy_expect, monkeypatch):
    seen = _vendor(monkeypatch, (FOUND, ()), (FAILED, ((b"X-Credits-Used", b"1"),)))
    r1 = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r1.status_code == 200 and r1.content == FOUND
    await archive.drain()
    r2 = await clients.get(f"/call/{EP}?aweme_id=7", headers={"Cache-Control": "no-cache"})
    assert r2.content == FAILED and r2.headers["X-Treg-Upstream-Failed"] == "code=500"
    keys, snaps = await _rows()
    assert [s.history_only for s in snaps] == [None, True]
    failed = snaps[1]
    assert failed.body == FAILED and failed.response_headers == {"x-credits-used": "1"}
    # Not an observation of the answer: the timer, the change counts and the key's fetch time
    # (what refresh ages) all still describe the found answer.
    assert (keys[0].stable_seen, keys[0].change_seen) == (0, 0)
    assert keys[0].fetched_at == snaps[0].fetched_at
    # The team paid for a failure, not for the question: no repeat-price mark from it.
    assert [m.calls for m in await _marks()] == [1]
    # The next caller is served the found answer, not the newer failure.
    r3 = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r3.headers.get("x-treg-cache") == "hit" and r3.content == FOUND
    assert len(seen) == 2
    # The failing call still links to the bytes it received.
    await audit.drain()
    rows = (await clients.get("/calls")).json()
    assert [row["has_result"] for row in rows[:3]] == [True, True, True]


async def test_a_question_only_ever_failed_is_never_served_or_refreshed(
        clients: AsyncClient, serve, legacy_expect, monkeypatch):
    seen = _vendor(monkeypatch, (FAILED, ()), (FAILED, ()))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    r2 = await clients.get(f"/call/{EP}?aweme_id=7")
    assert "x-treg-cache" not in r2.headers and len(seen) == 2
    keys, snaps = await _rows()
    assert [s.history_only for s in snaps] == [True, True]
    async with session_maker() as s:
        key = (await s.execute(select(ArchiveKey))).scalars().one()
        key.fetched_at -= timedelta(days=1)
        key.last_requested_at = key.fetched_at + timedelta(seconds=60)
        s.add(key)
        await s.commit()
    fake = _FakeUpstream()
    assert await archive.refresh_once(fake) == 0 and fake.calls == []


async def test_a_refresh_answered_with_an_expect_failure_waits_a_full_window(
        clients: AsyncClient, serve, legacy_expect, monkeypatch):
    _vendor(monkeypatch, (FOUND, ()))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    async with session_maker() as s:
        key = (await s.execute(select(ArchiveKey))).scalars().one()
        key.fetched_at -= timedelta(days=1)
        key.last_requested_at = key.fetched_at + timedelta(seconds=60)
        s.add(key)
        await s.commit()
    fake = _FakeUpstream(FAILED)
    assert await archive.refresh_once(fake) == 1
    assert await archive.refresh_once(fake) == 0 and len(fake.calls) == 1
    _, snaps = await _rows()
    assert [(s.origin, s.history_only) for s in snaps] == [("caller", None), ("refresh", True)]
    r = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r.headers.get("x-treg-cache") == "hit" and r.content == FOUND


async def test_an_expect_failure_on_a_result_aware_endpoint_keeps_the_decisive_answer(
        clients: AsyncClient, serve, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "expect", {"json_path": "code", "equals": 0})
    assert catalog_results.has_result_rules(EP)
    _vendor(monkeypatch, (FOUND, ()), (FAILED, ()))
    await clients.get(f"/call/{EP}?aweme_id=7")
    await archive.drain()
    await clients.get(f"/call/{EP}?aweme_id=7", headers={"Cache-Control": "no-cache"})
    keys, snaps = await _rows()
    assert [s.history_only for s in snaps] == [None, True]
    assert keys[0].result_state == "found" and keys[0].result_snapshot_id == snaps[0].id
    r3 = await clients.get(f"/call/{EP}?aweme_id=7")
    assert r3.headers.get("x-treg-cache") == "hit" and r3.content == FOUND


async def test_an_expect_failure_is_not_recorded_where_bytes_may_not_be_kept(
        clients: AsyncClient, serve, legacy_expect, monkeypatch):
    monkeypatch.setitem(catalog_store.load().by_id[EP], "cache", "forbidden")
    _vendor(monkeypatch, (FAILED, ()))
    await clients.get(f"/call/{EP}?aweme_id=7")
    assert await _rows() == ([], [])


async def test_an_opted_out_team_has_no_expect_failure_recorded(
        clients: AsyncClient, serve, legacy_expect, monkeypatch):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    r = await clients.patch(f"/orgs/{org_id}/settings", json={"archive": False})
    assert r.status_code == 200, r.text
    _vendor(monkeypatch, (FAILED, ((b"X-Credits-Used", b"1"),)))
    await clients.get(f"/call/{EP}?aweme_id=7")
    assert await _rows() == ([], [])


async def test_an_own_key_expect_failure_is_history_and_an_echoing_one_is_never_recorded(
        clients: AsyncClient, own_key_serve, legacy_expect, monkeypatch):
    await _own_key(clients)
    echo = call_service.relay
    _vendor(monkeypatch, (FAILED, ()))
    await clients.get(f"/call/{EP}?aweme_id=7")
    _, snaps = await _rows()
    assert len(snaps) == 1 and snaps[0].history_only is True and snaps[0].body == FAILED
    # The echo upstream quotes the team's key back in a body that has no `code`: it fails the
    # rule AND echoes the credential, and the echo guard still keeps it out entirely.
    monkeypatch.setattr(call_service, "relay", echo)
    r = await clients.get(f"/call/{EP}?aweme_id=8")
    assert r.json()["auth"] == "Bearer MKKEY"
    _, snaps = await _rows()
    assert len(snaps) == 1


async def test_the_pruner_keeps_the_served_answer_under_newer_failures(
        clients: AsyncClient, shadow, legacy_expect, monkeypatch):
    _vendor(monkeypatch, (FOUND, ()), *[(FAILED + b" " * n, ()) for n in range(3)])
    for _ in range(4):
        await clients.get(f"/call/{EP}?aweme_id=7")
        await archive.drain()
    async with session_maker() as s:
        for snap in (await s.execute(select(ArchiveSnapshot))).scalars().all():
            snap.fetched_at -= timedelta(days=30)
            s.add(snap)
        for key in (await s.execute(select(ArchiveKey))).scalars().all():
            key.last_requested_at -= timedelta(days=30)
            s.add(key)
        await s.commit()
    # The two newest versions stay whole, and so does the answer the key serves, older though it is.
    assert await archive.prune_once() == 1
    _, snaps = await _rows()
    assert [s.body is not None for s in snaps] == [True, False, True, True]
    assert snaps[0].history_only is None and snaps[0].body == FOUND
