"""/follower-analysis board: finished scans are kept for every visitor, one per handle."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from treg.api import app


async def _browser(clients: AsyncClient) -> AsyncClient:
    """The board saves from a signed-in browser: a session cookie, not an API key."""
    clients.headers.pop("X-Treg-Token", None)
    code = (await clients.post("/auth/email/start", json={"email": "tim@superdesign.dev"})).json()["dev_code"]
    assert (await clients.post("/auth/email/verify", json={"email": "tim@superdesign.dev", "code": code})).status_code == 200
    return clients


def _scan(handle="Some_Account", read=100, n=40, exact=False):
    return {
        "handle": handle, "kind": "company",
        "cats": [{"key": "c0", "name": "Founder", "desc": "founders"}, {"key": "c1", "name": "Engineer"}],
        "owner": {"name": "Some Account", "image": "https://pbs.twimg.com/profile_images/1/a_normal.jpg", "followers": n},
        "sample": [{"handle": "a1", "name": "A", "avatar": "https://evil.example/a.jpg", "followers": 3, "bio": "x" * 900, "cat": "c0"},
                   {"handle": "b2", "avatar": "https://pbs.twimg.com/profile_images/2/b_normal.jpg", "cat": "c1"}],
        "notable": [{"handle": "big", "followers": 1_000_000, "cat": "c0"}],
        "facts": {"n": n, "read": read, "exact": exact, "places": ["SF"], "secret": "dropped"},
    }


async def test_a_saved_scan_is_listed_and_served_to_everyone(clients: AsyncClient):
    clients = await _browser(clients)
    assert (await clients.get("/follower-analysis/board.json")).json() == {"rows": []}
    r = await clients.post("/follower-analysis/board", json=_scan())
    assert r.status_code == 200, r.text
    rows = (await clients.get("/follower-analysis/board.json")).json()["rows"]
    assert [(x["handle"], x["kind"], x["counts"]) for x in rows] == [("Some_Account", "company", {"c0": 1, "c1": 1})]
    full = (await clients.get("/follower-analysis/board/some_account.json")).json()
    assert "by" not in full and "secret" not in full["facts"]
    assert "avatar" not in full["sample"][0] and len(full["sample"][0]["bio"]) == 400
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as stranger:
        assert (await stranger.get("/follower-analysis/board/SOME_ACCOUNT.json")).status_code == 200
        assert (await stranger.post("/follower-analysis/board", json=_scan("other"))).status_code == 401


async def test_a_handle_keeps_the_result_that_read_more(clients: AsyncClient):
    clients = await _browser(clients)
    await clients.post("/follower-analysis/board", json=_scan(read=40, n=40, exact=True))
    r = await clients.post("/follower-analysis/board", json=_scan(read=20))
    assert r.json()["kept"] is True
    full = (await clients.get("/follower-analysis/board/some_account.json")).json()
    assert full["facts"]["read"] == 40 and full["facts"]["exact"] is True


async def test_a_malformed_scan_is_refused(clients: AsyncClient):
    clients = await _browser(clients)
    assert (await clients.post("/follower-analysis/board", json={**_scan(), "handle": "not a handle"})).status_code == 400
    assert (await clients.post("/follower-analysis/board", json={**_scan(), "sample": []})).status_code == 400


async def test_a_shared_link_previews_the_account(clients: AsyncClient):
    clients = await _browser(clients)
    await clients.post("/follower-analysis/board", json=_scan())
    page = (await clients.get("/follower-analysis", params={"u": "some_account"})).text
    assert '<meta property="og:title" content="Who follows @Some_Account"/>' in page
    assert "50% Founder, 50% Engineer. Followed by @big." in page
    generic = (await clients.get("/follower-analysis", params={"u": "<script>"})).text
    assert '<meta property="og:title" content="See who follows you on X | treg"/>' in generic and "<script>\"" not in generic
