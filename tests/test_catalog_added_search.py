"""Recently added tools on `/catalog/search`: `added_within_days` and `sort=newest`
(docs/context/architecture/catalog.md "`added`")."""
from __future__ import annotations

from datetime import date, timedelta

import pytest
from httpx import AsyncClient

from treg.domain.catalog import store as cs

QUERIES = ["tiktok comments", "phone", "linkedin company employees", "web search", "keyword volume",
           "generate video", "instagram"]


def _today_after_newest(monkeypatch) -> date:
    """Pin today to the day after the newest catalog date, so the windows below mean the same thing
    whenever the suite runs."""
    newest = max(date.fromisoformat(ep["added"]) for ep in cs.load().endpoints if ep.get("added"))
    today = newest + timedelta(days=1)
    monkeypatch.setattr(cs, "utc_today", lambda: today)
    return today


async def test_search_without_the_options_ignores_added_entirely(clients: AsyncClient, monkeypatch):
    """No option, no change: the same rows in the same order whatever the `added` dates are, and
    the only new key on a row is `added`."""
    before = {q: (await clients.get("/catalog/search", params={"q": q, "limit": 50})).json() for q in QUERIES}
    for i, ep in enumerate(cs.load().endpoints):
        if ep.get("added"):
            monkeypatch.setitem(ep, "added", (date(2026, 1, 1) + timedelta(days=i % 300)).isoformat())
    for q in QUERIES:
        after = (await clients.get("/catalog/search", params={"q": q, "limit": 50})).json()
        assert [(r["id"], r["score"]) for r in after["results"]] == \
               [(r["id"], r["score"]) for r in before[q]["results"]], q
        assert after["total"] == before[q]["total"] and after["hints"] == before[q]["hints"]
        assert "sort" not in after and "added_within_days" not in after
        for row in after["results"]:
            assert "added" in row
            assert (row["added"] is None) == (row.get("kind") == "routed"), row["id"]


async def test_new_tools_with_no_words_are_newest_first_inside_the_window(clients: AsyncClient, monkeypatch):
    today = _today_after_newest(monkeypatch)
    body = (await clients.get("/catalog/search", params={"added_within_days": 30, "limit": 100})).json()
    rows = body["results"]
    assert rows and body["added_within_days"] == 30 and body["sort"] == "newest"
    since = (today - timedelta(days=30)).isoformat()
    assert all(r["added"] >= since for r in rows)
    assert [r["added"] for r in rows] == sorted((r["added"] for r in rows), reverse=True)
    assert all(r["provider"] and r.get("kind") != "routed" for r in rows)
    in_window = sum(1 for ep in cs.load().endpoints if ep.get("added") and ep["added"] >= since)
    assert body["total"] == in_window


async def test_words_and_a_window_keep_best_match_order(clients: AsyncClient, monkeypatch):
    today = _today_after_newest(monkeypatch)
    since = (today - timedelta(days=60)).isoformat()
    new = (await clients.get("/catalog/search", params={"q": "phone", "added_within_days": 60, "limit": 100})).json()
    assert new["sort"] == "best" and new["results"]
    # the same matches the plain search admits, cut to the window, in score order; a flat list
    # (no routed parent: it is a choice among tools, not a tool added on a day)
    matches, _ = cs.search("phone", cs.load(), 10_000)
    expected = {ep["id"] for ep, _ in matches if ep.get("added") and ep["added"] >= since}
    assert {r["id"] for r in new["results"]} == expected and new["total"] == len(expected)
    scores = [r["score"] for r in new["results"]]
    assert scores == sorted(scores, reverse=True)


async def test_sort_newest_with_words_has_no_window(clients: AsyncClient, monkeypatch):
    _today_after_newest(monkeypatch)
    newest = (await clients.get("/catalog/search", params={"q": "instagram", "sort": "newest", "limit": 100})).json()
    dates = [r["added"] for r in newest["results"]]
    assert dates == sorted(dates, reverse=True)
    matches, _ = cs.search("instagram", cs.load(), 10_000)
    every = [ep for ep, _ in matches if ep.get("kind") != "routed"]
    assert newest["total"] == len(every)
    assert dates[0] == max(ep["added"] for ep in every)
    assert "added_within_days" not in newest


async def test_window_bounds(clients: AsyncClient):
    for bad in (0, -3):
        r = await clients.get("/catalog/search", params={"added_within_days": bad})
        assert r.status_code == 400 and "at least 1" in r.text
    r = await clients.get("/catalog/search", params={"sort": "oldest"})
    assert r.status_code == 400 and "newest" in r.text
    body = (await clients.get("/catalog/search", params={"added_within_days": 1000})).json()
    assert body["capped_at_days"] == 365 and body["added_within_days"] == 365
    assert any("capped at 365" in h for h in body["hints"])


async def test_endpoint_detail_carries_added(clients: AsyncClient):
    ep = next(e for e in cs.load().endpoints if e.get("added"))
    body = (await clients.get(f"/catalog/endpoints/{ep['id']}")).json()
    assert body["endpoint"]["added"] == ep["added"]


@pytest.mark.parametrize("days,sort,ok", [(None, None, True), (1, "newest", True), (365, "best", True),
                                          (0, None, False), (None, "date", False)])
def test_added_options(days, sort, ok):
    if ok:
        assert cs.added_options(days, sort).active == (days is not None or sort == "newest")
    else:
        with pytest.raises(cs.AddedOptionError):
            cs.added_options(days, sort)


# ---- MCP: catalog_search on /mcp/ and /mcp/v2/ ------------------------------------------------
async def test_mcp_new_tools_bypass_the_discovery_experiment(clients: AsyncClient, monkeypatch):
    """With an option the page is a date list on both MCP surfaces: the experiment (and its judge)
    never runs, every row carries `added`, and a bad option is an error, not a search."""
    from treg import mcp
    from treg.application import catalog_search as search_app
    from tests.test_catalog_find import _on

    _on(monkeypatch, search_experiment="v2")
    _today_after_newest(monkeypatch)

    async def no_experiment(*a, **k):
        raise AssertionError("the discovery experiment ran for a recently-added search")
    monkeypatch.setattr(search_app, "search", no_experiment)
    for surface in (mcp._TEAM_SURFACE, mcp._DIRECTORY_SURFACE):
        out = await mcp._catalog_search_impl("", 10, surface=surface, added_within_days=30)
        rows = out["results"]
        assert rows and out["sort"] == "newest" and out["added_within_days"] == 30
        assert [r["added"] for r in rows] == sorted((r["added"] for r in rows), reverse=True)
        assert all(r["provider"] and "routed" not in r for r in rows)
        out = await mcp._catalog_search_impl("phone", 10, surface=surface, sort="newest")
        assert out["results"] and out["sort"] == "newest" and "added_within_days" not in out
        out = await mcp._catalog_search_impl("phone", 10, surface=surface, added_within_days=999)
        assert out["capped_at_days"] == 365 and "capped" in out["hint"]
        assert (await mcp._catalog_search_impl("", 10, surface=surface, added_within_days=0))["error"] == "invalid_option"


async def test_mcp_search_without_options_is_unchanged_but_dated(clients: AsyncClient):
    from treg import mcp
    out = await mcp._catalog_search_impl("tiktok comments", 8, surface=mcp._TEAM_SURFACE)
    assert out["results"] and "sort" not in out
    for row in out["results"]:
        assert ("added" in row) and (row["added"] is None) == bool(row.get("routed")), row
    tools = {t.name: t for t in await mcp.mcp.list_tools()}
    assert {"added_within_days", "sort"} <= set(tools["catalog_search"].input_schema["properties"])
    assert tools["catalog_search"].input_schema["required"] == ["query"]
    directory = {t.name: t for t in await mcp.directory_mcp.list_tools()}
    assert {"added_within_days", "sort"} <= set(directory["catalog_search"].input_schema["properties"])
