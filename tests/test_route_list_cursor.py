"""Routed list tools issue a treg cursor when the serving adapter declares paging.

The people.search path already had this (treg#1700). These tools used to copy the provider's
own token into output.next_cursor and then refuse that same value as route_cursor_invalid.
"""

import pytest

from treg.application.call import service
from treg.config import get_settings

from test_marketplace_call import _balance, platform_on  # noqa: F401
from test_routing import _relay_by_provider


@pytest.fixture
def lists_on(monkeypatch, platform_on):
    for p in ("ANYAPI", "FETCHINIO"):
        monkeypatch.setenv(f"TREG_PLATFORM_KEY_{p}", f"K-{p}")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "anyapi,fetchinio")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _tweets(cursor):
    return {"output": {"data": {"tweets": [{"id": "1", "text": "hello"}], "nextCursor": cursor}}}


def _posts(cursor):
    return {"posts": [{"id": "urn:li:activity:1"}], "paginationToken": cursor, "hasMore": True}


async def test_x_user_posts_cursor_pages_on_the_serving_provider(clients, lists_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"anyapi": [(200, _tweets("cur_page2")), (200, _tweets("cur_page3"))]}, seen))
    body = {"username": "levelsio"}
    first = await clients.post("/call/treg.x.user.posts", json=body,
                               headers={"X-Treg-Route-Prefer": "anyapi.x.user.posts"})
    assert first.status_code == 200, first.text
    answer = first.json()
    cursor = answer["output"]["next_cursor"]
    assert cursor.startswith("trg1.")
    assert answer["raw"]["output"]["data"]["nextCursor"] == "cur_page2"
    assert answer["_treg"]["served_by"] == "anyapi.x.user.posts"

    second = await clients.post("/call/treg.x.user.posts", json={**body, "cursor": cursor},
                                headers={"X-Treg-Route-Prefer": "anyapi.x.user.posts"})
    assert second.status_code == 200, second.text
    nxt = second.json()
    assert nxt["_treg"]["served_by"] == "anyapi.x.user.posts"
    assert nxt["output"]["next_cursor"].startswith("trg1.")
    assert seen[0][3] is not None and "cursor" not in seen[0][3]
    assert seen[1][3]["cursor"] == "cur_page2"
    assert seen[1][3]["handle"] == "levelsio"


async def test_provider_token_is_still_refused_on_the_routed_tool(clients, lists_on, monkeypatch):
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"anyapi": [(200, _tweets("cur_page2"))]}, seen))
    first = await clients.post("/call/treg.x.user.posts", json={"username": "levelsio"},
                               headers={"X-Treg-Route-Prefer": "anyapi.x.user.posts"})
    assert first.status_code == 200, first.text
    raw = first.json()["raw"]["output"]["data"]["nextCursor"]
    assert raw == "cur_page2"
    before = await _balance(clients)
    r = await clients.post("/call/treg.x.user.posts",
                           json={"username": "levelsio", "cursor": raw})
    assert r.status_code == 422, r.text
    assert r.json()["detail"]["error"] == "route_cursor_invalid"
    assert seen[1:] == [] and await _balance(clients) == before


async def test_linkedin_user_posts_sends_the_provider_token_on_its_query_path(
        clients, lists_on, monkeypatch):
    seen = []
    # The Fetchin host is api.fetchin.io; _relay_by_provider matches a substring of the URL.
    monkeypatch.setattr(service, "relay", _relay_by_provider(
        {"fetchin": [(200, _posts("tok-2")), (200, _posts("tok-3"))]}, seen))
    body = {"linkedin_handle": "williamhgates", "limit": 5}
    first = await clients.post("/call/treg.linkedin.user.posts", json=body,
                               headers={"X-Treg-Route-Prefer": "fetchinio"})
    assert first.status_code == 200, first.text
    cursor = first.json()["output"]["next_cursor"]
    assert cursor.startswith("trg1.")
    second = await clients.post("/call/treg.linkedin.user.posts", json={**body, "cursor": cursor},
                                headers={"X-Treg-Route-Prefer": "fetchinio"})
    assert second.status_code == 200, second.text
    assert seen[0][2].get("paginationToken") in (None, "")
    assert seen[1][2]["paginationToken"] == "tok-2"
