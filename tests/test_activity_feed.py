"""The dashboard's Activity feed: calls, server runs and local runs merged newest first, paged by
one cursor with no gap and no repeat, even where rows of different sources share a timestamp."""
from __future__ import annotations

from datetime import datetime, timedelta

from treg.infra.db import session_maker
from treg.models import CallRecord, Org, RunRecord
from test_tag_billing import _mint_agent, _org_id

T = datetime(2026, 9, 30, 12)


def _call(org: int, at: int, kind: str = "http", **extra) -> CallRecord:
    return CallRecord(**{"org_id": org, "user_email": "a@example.com", "tool_name": "serp",
                         "method": "GET", "path": "/search", "status_code": 200, "kind": kind,
                         "created_at": T + timedelta(seconds=at), **extra})


async def _seed(org: int, other: int) -> None:
    async with session_maker() as db:
        db.add_all([
            _call(org, 4),
            # One timestamp shared by every source, and by two calls.
            _call(org, 3), _call(org, 3),
            _call(org, 3, kind="local_run", method="GRANT", path="gh pr list"),
            RunRecord(org_id=org, user_email="a@example.com", bundle_name="sh", argv=["x"],
                      exit_code=0, duration_ms=5, created_at=T + timedelta(seconds=3)),
            _call(org, 2, tags={"customer": "a"}),
            # Not a row of this feed: owned polling, another team's call.
            _call(org, 1, kind="async_poll"),
            _call(other, 5),
        ])
        await db.commit()


def _keys(rows: list[dict]) -> list[tuple]:
    return [(r["source"], r.get("where"), r["id"], r["created_at"]) for r in rows]


async def test_the_feed_pages_every_source_with_one_cursor(clients):
    org = await _org_id(clients)
    async with session_maker() as db:
        other = Org(name="Elsewhere", slug="elsewhere")
        db.add(other)
        await db.commit()
        other_id = other.id
    await _seed(org, other_id)
    whole = (await clients.get("/activity")).json()
    assert whole["next"] is None
    assert [(s, w) for s, w, _, _ in _keys(whole["rows"])] == [
        ("call", None), ("call", None), ("call", None), ("run", "local"), ("run", "server"), ("call", None)]
    assert [r["created_at"] for r in whole["rows"]] == sorted((r["created_at"] for r in whole["rows"]), reverse=True)

    paged, before = [], None
    while True:
        page = (await clients.get("/activity", params={"limit": 1, **({"before": before} if before else {})})).json()
        paged += page["rows"]
        before = page["next"]
        if before is None:
            break
    assert _keys(paged) == _keys(whole["rows"])


async def test_a_pinned_agent_pages_only_its_own_rows(clients):
    org = await _org_id(clients)
    await _seed(org, org)
    token = await _mint_agent(clients, org, "a", role="viewer", pinned_tags={"customer": "a"})
    rows = (await clients.get("/activity", headers={"X-Treg-Token": token})).json()["rows"]
    assert [r["tags"] for r in rows] == [{"customer": "a"}]


async def test_a_cursor_the_feed_did_not_return_is_refused(clients):
    for before in ["nonsense", "2026-09-30T12:00:00~x~1", "2026-09-30T12:00:00+00:00~c~1", "2026-09-30T12:00:00~c~z"]:
        r = await clients.get("/activity", params={"before": before})
        assert r.status_code == 400, before
