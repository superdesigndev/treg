"""Web Arena safety and score rules that do not need paid provider calls."""
import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
import pytest
from cryptography.fernet import InvalidToken
from sqlmodel import select

from treg.domain import web_arena, web_arena_scores
from treg.application import web_arena as app, web_arena_quality, web_arena_publications
from treg.application.call import service
from treg.application.call.types import UpstreamResponse
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra import db as infra_db
from treg.infra.db import session_maker
from treg.models import Hold, LedgerEntry
from test_marketplace_call import _balance
from test_routing import _relay_by_provider


def test_web_input_enforces_task_limits_and_public_urls():
    assert web_arena.input_for("search", "open data") == {"q": "open data", "limit": 10}
    assert web_arena.input_for("news", "  AI policy  ") == {"q": "AI policy", "limit": 10}
    assert web_arena.input_for("papers", "graph search") == {"q": "graph search", "limit": 10}
    assert web_arena.input_for("youtube", "graph search") == {"q": "graph search"}
    assert web_arena.input_for("maps", "coffee shops in Austin TX") == {"q": "coffee shops in Austin TX"}
    assert web_arena.input_for("sitemap", "https://example.com") == {"url": "https://example.com", "limit": 10}
    assert web_arena.input_for("sitemap", "https://example.com", "  pricing  ") == {
        "url": "https://example.com", "limit": 10, "q": "pricing"}
    for value in ("file:///etc/passwd", "http://localhost", "http://127.0.0.1", "https://user:pass@example.com"):
        with pytest.raises(web_arena.WebArenaError):
            web_arena.input_for("fetch", value)
    with pytest.raises(web_arena.WebArenaError):
        web_arena.input_for("brand", "example.com")


def test_web_input_reads_a_bare_address_as_https():
    assert web_arena.input_for("fetch", "apple.com/iphone") == {"url": "https://apple.com/iphone"}
    assert web_arena.input_for("sitemap", " www.apple.com ")["url"] == "https://www.apple.com"
    assert web_arena.input_for("fetch", "http://example.com")["url"] == "http://example.com"
    with pytest.raises(web_arena.WebArenaError):
        web_arena.input_for("fetch", "ftp://example.com")
    with pytest.raises(web_arena.WebArenaError):
        web_arena.input_for("fetch", "localhost/admin")


def test_sitemap_counts_only_unique_same_host_urls_without_claiming_coverage():
    result = web_arena.url_rows(["https://example.com/a#one", "https://example.com/a#two",
        "https://other.com/a", "bad", {"url": "https://example.com/b"}], "https://example.com")
    assert result["unique_valid_urls"] == 2
    assert result["invalid_urls"] == 2
    assert result["coverage_percent"] is None


def test_search_dates_stay_unknown_when_sources_have_no_dates():
    assert web_arena_quality._recent_share([{"source_date": None}])["freshness_percent"] is None


def test_diffbot_page_url_is_available_to_search_quality_check():
    output = {"results": [{"pageUrl": "https://example.com/article", "title": "An article",
                           "content": "Article summary"}]}
    assert web_arena_quality._search_links(output) == [{
        "url": "https://example.com/article", "title": "An article",
        "snippet": "Article summary", "source_date": None}]


def test_fetch_reads_nested_markdown_and_markdown_content_from_catalog_adapters():
    adapters = catalog_store.load().adapters
    examples = (
        ("branddev.web.scrape", {"url": "https://example.com", "markdown": {
            "requested": True, "success": True, "data": "# Example Domain"}}),
        ("olostep.web.scrape", {"result": {"markdown_content": "# Example Domain"}}),
        ("parallel.web.extract", {"results": [{"url": "https://example.com", "excerpts": ["Example"],
                                               "full_content": "# Example Domain"}]}),
    )
    for endpoint, response in examples:
        output = adapters[endpoint].from_upstream(response)
        assert web_arena.fetch_text(output) == "# Example Domain"
        assert web_arena.valid_result("fetch", output, "https://example.com")


async def test_local_web_arena_quality_skips_daily_caps(monkeypatch):
    monkeypatch.setattr(web_arena_quality, "get_settings", lambda: SimpleNamespace(
        ai_gateway_api_key="test", local_dev=True))
    assert await web_arena_quality._take_budget(user_id=1)


async def test_arena_opens_with_flag_without_benchmark_publication(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    get_settings.cache_clear()
    try:
        assert (await clients.get("/web-arena")).status_code == 200
        old_page = await clients.get("/web-arena/leaderboard", follow_redirects=False)
        assert old_page.status_code == 308
        assert old_page.headers["location"] == "/web-arena"
        assert (await clients.get("/web-arena/api/tasks")).status_code == 200
        assert (await clients.get("/web-arena/benchmark")).status_code == 404
        assert (await clients.get("/web-arena/api/benchmark")).status_code == 404
    finally:
        get_settings.cache_clear()


def test_sitemap_win_requires_known_coverage():
    assert web_arena_scores.winner_values("sitemap", [{"provider": "a", "state": "hit",
        "quality": {"unique_valid_urls": 5}}, {"provider": "b", "state": "hit",
        "quality": {"unique_valid_urls": 6}}]) == {}


def test_live_provider_stats_need_distinct_checked_inputs(monkeypatch):
    monkeypatch.setattr(web_arena_publications.arena, "_unpack", lambda payload: payload)
    rows = [SimpleNamespace(task="search", payload={"input": f"query {i}", "attempts": [{
        "provider": "exa", "state": "miss" if i == 0 else "hit", "duration_ms": 100 + i,
        "quality": {} if i == 0 else {"state": "checked", "estimated_match": 80},
    }]}) for i in range(21)]
    rows.append(SimpleNamespace(task="search", payload={"input": "query 1", "attempts": [{
        "provider": "exa", "state": "miss", "duration_ms": 900, "quality": {},
    }]}))  # An older repeat must not outweigh the latest result.
    doc = web_arena_publications.summarize_live(rows, catalog_store.load())
    row = doc["task_results"]["search"][0]
    assert row["runs"] == 21
    assert row["success_rate"] == 95.2
    assert row["median_provider_ms"] == 110
    assert row["metric_sample_count"] == 20
    assert row["metric_percent"] == 80
    assert "query 1" not in str(doc)


def test_fetch_live_totals_keep_fact_coverage_and_token_efficiency_separate(monkeypatch):
    monkeypatch.setattr(web_arena_publications.arena, "_unpack", lambda payload: payload)
    rows = [SimpleNamespace(task="fetch", payload={"input": f"https://example.com/{i}", "attempts": [{
        "provider": "exa", "state": "hit", "duration_ms": 100,
        "quality": {"state": "checked", "relative_coverage": 60, "token_efficiency": 80},
    }]}) for i in range(20)]
    row = web_arena_publications.summarize_live(rows, catalog_store.load())["task_results"]["fetch"][0]
    assert row["metric_percent"] == 60
    assert row["token_efficiency_percent"] == 80
    assert row["token_efficiency_sample_count"] == 20


def test_waterfall_quality_joins_direct_call_totals_without_double_counting(monkeypatch):
    monkeypatch.setattr(web_arena_publications.arena, "_unpack", lambda payload: payload)
    rows = [SimpleNamespace(task="search", mode="waterfall", payload={
        "input": f"query {i}", "attempts": [{"provider": "exa", "state": "hit",
            "duration_ms": 999, "quality": {"state": "checked", "estimated_match": 70}}]})
        for i in range(20)]
    traffic = {"search": {"exa": {"runs": 25, "hit_samples": 24, "time_samples": 22,
        "success_rate": 87.5, "average_provider_ms": 240, "median_provider_ms": 200}}}
    row = web_arena_publications.summarize_live(rows, catalog_store.load(), traffic)["task_results"]["search"][0]
    assert row["runs"] == 25
    assert row["success_rate"] == 87.5
    assert row["average_provider_ms"] == 240
    assert row["metric_sample_count"] == 20
    assert row["metric_percent"] == 70


def test_repeat_fetch_checks_do_not_hide_later_efficiency(monkeypatch):
    monkeypatch.setattr(web_arena_publications.arena, "_unpack", lambda payload: payload)
    rows = [SimpleNamespace(task="fetch", mode="battle", payload={
        "input": f"https://example.com/{i}", "attempts": [{"provider": "exa", "state": "hit",
            "quality": {"state": "checked", "relative_coverage": 60}},
            {"provider": "firecrawl", "state": "hit", "quality": {}}]})
        for i in range(20)]
    rows += [SimpleNamespace(task="fetch", mode="battle", payload={
        "input": f"https://example.com/{i}", "attempts": [{"provider": "exa", "state": "hit",
            "quality": {"state": "checked", "relative_coverage": 55, "token_efficiency": 80}},
            {"provider": "firecrawl", "state": "hit", "quality": {}}]})
        for i in range(20)]
    row = web_arena_publications.summarize_live(rows, catalog_store.load())["task_results"]["fetch"][0]
    assert row["metric_sample_count"] == 20
    assert row["token_efficiency_sample_count"] == 20
    assert row["token_efficiency_percent"] == 80


async def test_local_leaderboard_uses_saved_totals_if_old_runs_cannot_decrypt(monkeypatch):
    async def rows(observed_since=None):
        return [SimpleNamespace(payload="old ciphertext")]

    async def saved(kind):
        assert kind == "live"
        return {"status": "live", "task_results": {"search": [{"provider": "exa", "runs": 3}]}}

    def cannot_decrypt(payload):
        raise InvalidToken

    monkeypatch.setattr(web_arena_publications, "_live_rows", rows)
    monkeypatch.setattr(web_arena_publications.web_arena_calls, "coverage_start", lambda: asyncio.sleep(0, result=None))
    monkeypatch.setattr(web_arena_publications.web_arena_calls, "local_snapshot", lambda: asyncio.sleep(0, result={}))
    monkeypatch.setattr(web_arena_publications, "published", saved)
    monkeypatch.setattr(web_arena_publications.arena, "_unpack", cannot_decrypt)
    result = await web_arena_publications.live_now()
    assert result["task_results"]["search"][0]["runs"] == 3
    assert result["stale"] is True
    assert "Last saved" in result["source"]


async def test_local_leaderboard_keeps_readable_runs_when_old_runs_cannot_decrypt(monkeypatch):
    current = {"input": "https://example.com", "attempts": [{
        "provider": "search1api", "state": "hit", "duration_ms": 100,
        "quality": {"unique_valid_urls": 10}}]}
    async def rows(observed_since=None):
        return [SimpleNamespace(task="sitemap", payload=current),
                SimpleNamespace(task="sitemap", payload="old ciphertext")]

    async def saved(kind):
        assert kind == "live"
        return {"status": "live", "task_results": {"fetch": [{"provider": "exa", "runs": 2}]}}

    def unpack(payload):
        if isinstance(payload, str):
            raise InvalidToken
        return payload

    monkeypatch.setattr(web_arena_publications, "_live_rows", rows)
    monkeypatch.setattr(web_arena_publications.web_arena_calls, "coverage_start", lambda: asyncio.sleep(0, result=None))
    monkeypatch.setattr(web_arena_publications.web_arena_calls, "local_snapshot", lambda: asyncio.sleep(0, result={}))
    monkeypatch.setattr(web_arena_publications, "published", saved)
    monkeypatch.setattr(web_arena_publications.arena, "_unpack", unpack)
    result = await web_arena_publications.live_now()
    assert result["task_results"]["sitemap"][0]["provider"] == "search1api"
    assert result["task_results"]["fetch"][0]["provider"] == "exa"
    assert result["partial"] is True
    assert result["stale_tasks"] == ["fetch"]


async def test_scheduled_live_totals_refresh_only_when_due(monkeypatch):
    current = web_arena_publications.now()
    refreshed = []

    async def refresh():
        refreshed.append(True)
        return {"battle_runs": 3}

    async def saved(kind):
        return {"status": "live", "updated_at": (current - timedelta(minutes=10)).isoformat() + "Z"}

    monkeypatch.setattr(web_arena_publications, "now", lambda: current)
    monkeypatch.setattr(web_arena_publications, "published", saved)
    monkeypatch.setattr(web_arena_publications, "refresh_live", refresh)
    assert (await web_arena_publications.refresh_live_if_due())["skipped"] is True
    assert not refreshed

    async def stale(kind):
        return {"status": "live", "updated_at": (current - timedelta(minutes=30)).isoformat() + "Z"}

    monkeypatch.setattr(web_arena_publications, "published", stale)
    assert await web_arena_publications.refresh_live_if_due() == {"battle_runs": 3}
    assert len(refreshed) == 1


def test_public_task_previews_show_verified_search_providers(monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    get_settings.cache_clear()
    try:
        tasks = {row["id"]: row for row in app.tasks()}
        search = {row["provider"] for row in tasks["search"]["provider_previews"]}
        assert {"crawl4ai", "exa", "firecrawl", "tavily", "tinyfish", "serper", "spidercloud", "octen",
                "parallel"} <= search
        fetch = {row["endpoint_id"] for row in tasks["fetch"]["provider_previews"]}
        assert "parallel.web.extract" in fetch
        news = {row["provider"] for row in tasks["news"]["provider_previews"]}
        assert news == {"anyapi", "cloro", "dataforseo", "exa", "litescrape", "search1api",
                        "serpapi", "serper", "tavily", "tinyfish"}
        assert {p["endpoint_id"] for p in tasks["papers"]["provider_previews"]} == app.PAPER_ENDPOINTS
        assert {p["endpoint_id"] for p in tasks["youtube"]["provider_previews"]} == app.YOUTUBE_ENDPOINTS
        assert {p["endpoint_id"] for p in tasks["maps"]["provider_previews"]} == app.MAPS_ENDPOINTS
        assert "valyu" not in search
        assert not tasks["brand"]["enabled"]
        assert tasks["brand"]["provider_previews"] == []
    finally:
        get_settings.cache_clear()


async def test_sitemap_quotes_use_optional_query_and_first_ten_urls(clients, monkeypatch):
    providers = ("anyapi", "branddev", "firecrawl", "olostep", "search1api", "tavily")
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", ",".join(providers))
    for provider in providers:
        monkeypatch.setenv("TREG_PLATFORM_KEY_" + provider.upper(), "TEST-" + provider)
    get_settings.cache_clear()
    try:
        without = await clients.post("/web-arena/api/quotes", json={
            "task": "sitemap", "value": "https://example.com", "jev": True})
        assert without.status_code == 200, without.text
        assert without.json()["jev"] is False
        plain = {p["provider"] for p in without.json()["providers"]}
        assert plain == set(providers) - {"olostep"}

        with_query = await clients.post("/web-arena/api/quotes", json={
            "task": "sitemap", "value": "https://example.com", "query": "pricing", "jev": False})
        assert with_query.status_code == 200, with_query.text
        assert {p["provider"] for p in with_query.json()["providers"]} == set(providers)
        from treg.application import arena
        from treg.infra.db import session_maker
        from treg.models import WebArenaRun
        async with session_maker() as db:
            row = await db.get(WebArenaRun, with_query.json()["id"])
            attempts = {a["provider"]: a for a in arena._unpack(row.payload)["attempts"]}
        assert attempts["olostep"]["body"]["search_query"] == "pricing"
        assert attempts["tavily"]["body"]["limit"] == 10
        assert attempts["branddev"]["query"]["maxLinks"] == "10"
        assert "pricing" not in str(attempts["branddev"]["query"])
        assert "pricing" not in str(attempts["search1api"]["body"])
    finally:
        get_settings.cache_clear()


async def test_search1api_sitemap_compares_only_first_ten_urls(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "search1api")
    monkeypatch.setenv("TREG_PLATFORM_KEY_SEARCH1API", "TEST-SEARCH1API")
    get_settings.cache_clear()
    seen = []
    links = [f"https://example.com/{index}" for index in range(12)]
    monkeypatch.setattr(service, "relay", _relay_by_provider({"search1api": [(200, {"links": links})]}, seen))
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": "sitemap", "value": "https://example.com", "query": "pricing",
            "providers": ["search1api"], "jev": False})
        assert response.status_code == 200, response.text
        run_id = response.json()["id"]
        started = await clients.post(f"/web-arena/api/runs/{run_id}/start")
        assert started.status_code == 200, started.text
        worker = app._owners.get(run_id)
        if worker:
            await asyncio.wait_for(asyncio.shield(worker), 15)
        finished = await clients.get(f"/web-arena/api/runs/{run_id}")
        assert finished.status_code == 200, finished.text
        result = finished.json()["attempts"][0]
        assert result["state"] == "hit"
        assert result["output"]["results"] == links[:10]
        assert result["output"]["count"] == 10
        assert len(seen) == 1 and "limit" not in seen[0][3]
    finally:
        get_settings.cache_clear()


async def test_battle_quotes_and_settles_direct_search_with_jev_off(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FIRECRAWL", "TEST-FIRECRAWL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "firecrawl")
    get_settings.cache_clear()
    async def no_jev(*args, **kwargs):
        raise AssertionError("Jev must stay off")
    monkeypatch.setattr(web_arena_quality, "search", no_jev)
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"firecrawl": [(200, {
        "data": {"web": [{"url": "https://example.com/a", "title": "A"}]}, "creditsUsed": 1})]}, seen))
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": "example query", "mode": "battle", "providers": ["firecrawl"], "jev": False})
        assert response.status_code == 200, response.text
        quote = response.json()
        assert quote["providers"][0]["endpoint_id"] == "firecrawl.web.search"
        assert seen == []
        started = await clients.post(f"/web-arena/api/runs/{quote['id']}/start")
        assert started.status_code == 200, started.text
        worker = app._owners.get(quote["id"])
        if worker:
            await asyncio.wait_for(asyncio.shield(worker), 15)
        finished = await clients.get(f"/web-arena/api/runs/{quote['id']}")
        assert finished.status_code == 200, finished.text
        run = finished.json()
        assert run["state"] == "completed"
        assert run["attempts"][0]["state"] == "hit"
        assert run["attempts"][0]["charged_micro"] is not None
        assert len(seen) == 1 and seen[0][3]["limit"] == 10
    finally:
        get_settings.cache_clear()


async def test_web_arena_run_is_private_to_its_creator(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FIRECRAWL", "TEST-FIRECRAWL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "firecrawl")
    get_settings.cache_clear()
    try:
        quote = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": "private query", "providers": ["firecrawl"], "jev": False})
        assert quote.status_code == 200, quote.text
        run_id = quote.json()["id"]
        path = f"/web-arena/api/runs/{run_id}"
        attempt_id = (await clients.get(path)).json()["attempts"][0]["id"]
        other = (await clients.post("/users", json={"email": "other-web-arena@example.com"})).json()["token"]
        headers = {"X-Treg-Token": other}
        assert (await clients.get(path, headers=headers)).status_code == 404
        assert (await clients.post(path + "/start", headers=headers)).status_code == 404
        assert (await clients.post(path + "/cancel", headers=headers)).status_code == 404
        assert (await clients.post(path + f"/attempts/{attempt_id}/rating", json={"value": "up"},
                                   headers=headers)).status_code == 404
        assert (await clients.get("/web-arena/api/runs", headers=headers)).json() == []
        assert (await clients.get(path)).status_code == 200
    finally:
        get_settings.cache_clear()


async def test_web_arena_releases_api_connection_while_provider_waits(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FIRECRAWL", "TEST-FIRECRAWL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "firecrawl")
    get_settings.cache_clear()
    entered, resume = asyncio.Event(), asyncio.Event()

    async def relay(*args, **kwargs):
        entered.set()
        await resume.wait()
        async def body():
            yield json.dumps({"data": {"web": [{"url": "https://example.com/a", "title": "A"}]},
                              "creditsUsed": 1}).encode()
        async def close():
            pass
        return UpstreamResponse(200, ((b"content-type", b"application/json"),), body(), close)

    monkeypatch.setattr(service, "relay", relay)
    try:
        quote = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": "pool check", "providers": ["firecrawl"], "jev": False})
        assert quote.status_code == 200, quote.text
        run_id = quote.json()["id"]
        assert (await clients.post(f"/web-arena/api/runs/{run_id}/start")).status_code == 200
        await asyncio.wait_for(entered.wait(), 15)
        assert infra_db._engine.sync_engine.pool.checkedout() == 0
        async with session_maker() as db:
            assert (await db.execute(select(Hold))).scalars().all()
        assert infra_db._engine.sync_engine.pool.checkedout() == 0
    finally:
        resume.set()
        if "run_id" in locals() and (worker := app._owners.get(run_id)):
            await asyncio.wait_for(asyncio.shield(worker), 15)
        get_settings.cache_clear()


@pytest.mark.parametrize("stop", ["cancel", "timeout"])
async def test_web_arena_cancel_or_timeout_closes_one_paid_hold(clients, monkeypatch, stop):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FIRECRAWL", "TEST-FIRECRAWL")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "firecrawl")
    get_settings.cache_clear()
    entered = asyncio.Event()
    wait_forever = asyncio.Event()

    async def relay(*args, **kwargs):
        entered.set()
        await wait_forever.wait()
        raise AssertionError("The provider should have been stopped")

    monkeypatch.setattr(service, "relay", relay)
    if stop == "timeout":
        monkeypatch.setattr(app, "RUN_SECONDS", 0.25)
    try:
        before = await _balance(clients)
        quote = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": f"{stop} paid call", "providers": ["firecrawl"], "jev": False})
        assert quote.status_code == 200, quote.text
        run_id = quote.json()["id"]
        assert (await clients.post(f"/web-arena/api/runs/{run_id}/start")).status_code == 200
        await asyncio.wait_for(entered.wait(), 15)
        if stop == "cancel":
            response = await clients.post(f"/web-arena/api/runs/{run_id}/cancel")
            assert response.status_code == 200, response.text
        worker = app._owners.get(run_id)
        if worker:
            if stop == "cancel":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(asyncio.shield(worker), 15)
            else:
                await asyncio.wait_for(asyncio.shield(worker), 15)
        result = (await clients.get(f"/web-arena/api/runs/{run_id}")).json()
        assert result["state"] == ("cancelled" if stop == "cancel" else "completed")
        attempt = result["attempts"][0]
        assert attempt["state"] == ("cancelled" if stop == "cancel" else "timeout")
        assert attempt["call_ref"]
        async with session_maker() as db:
            assert not (await db.execute(select(Hold).where(Hold.id == attempt["call_ref"]))).scalars().all()
            entries = (await db.execute(select(LedgerEntry).where(
                LedgerEntry.call_id == attempt["call_ref"]))).scalars().all()
        assert [entry.kind for entry in entries].count("reserve") == 1
        assert sum(entry.kind in {"settle", "release"} for entry in entries) == 1
        assert await _balance(clients) == before
    finally:
        wait_forever.set()
        get_settings.cache_clear()


async def test_fixed_ten_result_search_can_join_quote(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_KEY_BRANDDEV", "TEST-BRANDDEV")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "branddev")
    get_settings.cache_clear()
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": "example query", "mode": "battle",
            "providers": ["branddev"], "jev": False})
        assert response.status_code == 200, response.text
        assert response.json()["providers"][0]["endpoint_id"] == "branddev.web.search"
    finally:
        get_settings.cache_clear()


async def test_tinyfish_first_page_is_capped_for_comparison(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TINYFISH", "TEST-TINYFISH")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tinyfish")
    get_settings.cache_clear()
    seen = []
    rows = [{"url": f"https://example.com/{index}", "title": str(index)} for index in range(11)]
    monkeypatch.setattr(service, "relay", _relay_by_provider({"tinyfish": [(200, {
        "results": rows, "total_results": 11, "page": 0})]}, seen))
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": "example query", "mode": "battle",
            "providers": ["tinyfish"], "jev": False})
        assert response.status_code == 200, response.text
        quote = response.json()
        assert quote["providers"][0]["estimate_micro"] == 0
        started = await clients.post(f"/web-arena/api/runs/{quote['id']}/start")
        assert started.status_code == 200, started.text
        worker = app._owners.get(quote["id"])
        if worker:
            await asyncio.wait_for(asyncio.shield(worker), 15)
        finished = await clients.get(f"/web-arena/api/runs/{quote['id']}")
        assert finished.status_code == 200, finished.text
        output = finished.json()["attempts"][0]["output"]
        assert len(output["results"]) == output["count"] == 10
        assert len(seen) == 1
        assert "limit" not in seen[0][2]
    finally:
        get_settings.cache_clear()


async def test_crawl4ai_first_page_is_capped_for_comparison(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_KEY_CRAWL4AI", "TEST-CRAWL4AI")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "crawl4ai")
    get_settings.cache_clear()
    seen = []
    rows = [{"url": f"https://example.com/{index}", "title": str(index)} for index in range(11)]
    monkeypatch.setattr(service, "relay", _relay_by_provider({"crawl4ai": [(200, {
        "results": rows, "n": 11})]}, seen))
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": "example query", "mode": "battle",
            "providers": ["crawl4ai"], "jev": False})
        assert response.status_code == 200, response.text
        quote = response.json()
        assert quote["providers"][0]["endpoint_id"] == "crawl4ai.web.search"
        started = await clients.post(f"/web-arena/api/runs/{quote['id']}/start")
        assert started.status_code == 200, started.text
        worker = app._owners.get(quote["id"])
        if worker:
            await asyncio.wait_for(asyncio.shield(worker), 15)
        finished = await clients.get(f"/web-arena/api/runs/{quote['id']}")
        assert finished.status_code == 200, finished.text
        output = finished.json()["attempts"][0]["output"]
        assert output["results"] == rows[:10]
        assert output["count"] == 10
        assert len(seen) == 1
        assert seen[0][2] == {"q": "example query"}
    finally:
        get_settings.cache_clear()


async def test_new_search_providers_join_one_ten_link_quote(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tinyfish,serper,spidercloud,octen")
    for provider in ("TINYFISH", "SERPER", "SPIDERCLOUD", "OCTEN"):
        monkeypatch.setenv("TREG_PLATFORM_KEY_" + provider, "TEST-" + provider)
    get_settings.cache_clear()
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": "IANA example domains", "mode": "battle",
            "providers": ["tinyfish", "serper", "spidercloud", "octen"], "jev": False})
        assert response.status_code == 200, response.text
        quote = response.json()
        assert {p["provider"] for p in quote["providers"]} == {
            "tinyfish", "serper", "spidercloud", "octen"}
        assert next(p for p in quote["providers"] if p["provider"] == "tinyfish")["estimate_micro"] == 0
        assert quote["required_micro"] == quote["estimate_micro"]
    finally:
        get_settings.cache_clear()


async def test_news_search_quotes_platform_providers_with_news_parameters(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tinyfish,search1api,exa,anyapi,cloro,serpapi,serper,dataforseo,litescrape,tavily")
    for provider in ("TINYFISH", "SEARCH1API", "EXA", "ANYAPI", "CLORO", "SERPAPI", "SERPER",
                     "DATAFORSEO", "LITESCRAPE", "TAVILY"):
        monkeypatch.setenv("TREG_PLATFORM_KEY_" + provider, "TEST-" + provider)
    get_settings.cache_clear()
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": "news", "value": "AI policy", "mode": "battle", "jev": False})
        assert response.status_code == 200, response.text
        quote = response.json()
        assert {p["endpoint_id"] for p in quote["providers"]} == app.NEWS_ENDPOINTS
        async with session_maker() as db:
            from treg.models import WebArenaRun
            saved = await db.get(WebArenaRun, quote["id"])
        attempts = {a["provider"]: a for a in app.arena._unpack(saved.payload)["attempts"]}
        assert attempts["tinyfish"]["query"]["domain_type"] == "news"
        assert attempts["search1api"]["body"]["max_results"] == 10
        assert attempts["exa"]["body"]["category"] == "news"
        assert attempts["exa"]["body"]["numResults"] == 10
        assert attempts["anyapi"]["body"]["limit"] == 10
        assert attempts["serper"]["body"]["num"] == 10
        assert attempts["cloro"]["body"]["pages"] == 1
        assert attempts["serpapi"]["query"]["engine"] == "google_news"
        assert attempts["dataforseo"]["body"] == [{"keyword": "AI policy", "location_code": 2840,
                                                    "language_code": "en", "depth": 10}]
        assert attempts["litescrape"]["query"]["tbm"] == "nws"
        assert attempts["litescrape"]["query"]["num"] == "10"
        assert attempts["tavily"]["body"]["topic"] == "news"
        assert attempts["tavily"]["body"]["max_results"] == 10
        assert attempts["tavily"]["body"]["include_usage"] is True
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(("task", "value", "expected"), [
    ("papers", "graph search", app.PAPER_ENDPOINTS),
    ("youtube", "graph search", app.YOUTUBE_ENDPOINTS),
    ("maps", "coffee shops in Austin TX", app.MAPS_ENDPOINTS),
])
async def test_new_search_tasks_quote_platform_lineups(clients, monkeypatch, task, value, expected):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "exa,tinyfish,serper,justoneapi,serpapi,tikhub,apify,dataforseo")
    for provider in ("EXA", "TINYFISH", "SERPER", "JUSTONEAPI", "SERPAPI", "TIKHUB", "APIFY", "DATAFORSEO"):
        monkeypatch.setenv("TREG_PLATFORM_KEY_" + provider, "TEST-" + provider)
    get_settings.cache_clear()
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": task, "value": value, "mode": "battle", "jev": True})
        assert response.status_code == 200, response.text
        quote = response.json()
        assert {p["endpoint_id"] for p in quote["providers"]} == expected
        assert quote["jev"] is (task != "maps")
        async with session_maker() as db:
            from treg.models import WebArenaRun
            saved = await db.get(WebArenaRun, quote["id"])
        attempts = {a["provider"]: a for a in app.arena._unpack(saved.payload)["attempts"]}
        if task == "papers":
            assert attempts["exa"]["body"] == {"query": value, "numResults": 10, "category": "publication"}
            assert attempts["tinyfish"]["query"]["domain_type"] == "research_paper"
        elif task == "youtube":
            assert attempts["serpapi"]["query"]["engine"] == "youtube"
            assert attempts["tikhub"]["query"]["type"] == "video"
        else:
            assert attempts["serpapi"]["query"]["type"] == "search"
            assert attempts["apify"]["body"]["maxCrawledPlaces"] == 10
            assert attempts["apify"]["body"]["maxCrawledPlacesPerSearch"] == 10
            assert attempts["apify"]["query"]["maxTotalChargeUsd"] == "0.12"
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(("provider", "task", "answer", "expected_count"), [
    ("apify", "maps", [{"place_id": str(i), "name": f"Cafe {i}", "address": "Austin",
                       "rating": 4.5, "google_maps_url": f"https://maps.google.com/{i}",
                       "extra": "x" * 60_000} for i in range(10)], 10),
    ("justoneapi", "youtube", {"code": 0, "data": {"items": [
        {"type": "channel", "id": "channel", "title": "A channel"},
        {"type": "video", "id": "abc12345678", "title": "A video", "description": "About it"},
    ], "nextToken": "next"}}, 1),
])
async def test_new_search_result_shapes_are_usable(clients, monkeypatch, provider, task, answer, expected_count):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", provider)
    monkeypatch.setenv("TREG_PLATFORM_KEY_" + provider.upper(), "TEST-" + provider)
    get_settings.cache_clear()
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({provider: [(201 if provider == "apify" else 200,
                                                                        answer)]}, seen))
    try:
        quote = await clients.post("/web-arena/api/quotes", json={
            "task": task, "value": "coffee shops in Austin TX" if task == "maps" else "claude mods",
            "mode": "battle", "providers": [provider], "jev": False})
        assert quote.status_code == 200, quote.text
        run_id = quote.json()["id"]
        assert (await clients.post(f"/web-arena/api/runs/{run_id}/start")).status_code == 200
        worker = app._owners.get(run_id)
        if worker:
            await asyncio.wait_for(asyncio.shield(worker), 15)
        attempt = (await clients.get(f"/web-arena/api/runs/{run_id}")).json()["attempts"][0]
        assert attempt["state"] == "hit", attempt
        rows = attempt["output"]["places" if task == "maps" else "videos"]
        assert len(rows) == expected_count
        if task == "maps":
            assert "extra" not in rows[0]
            assert attempt["output"]["count"] == 10
        else:
            assert rows[0]["url"] == "https://www.youtube.com/watch?v=abc12345678"
            assert attempt["output"]["next_cursor"] == "next"
            assert web_arena_quality._search_links(attempt["output"], "youtube")
        assert len(seen) == 1
    finally:
        get_settings.cache_clear()


async def test_tinyfish_news_rate_limit_explains_retry(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "tinyfish")
    monkeypatch.setenv("TREG_PLATFORM_KEY_TINYFISH", "TEST-TINYFISH")
    get_settings.cache_clear()

    async def relay(*args, **kwargs):
        async def body():
            yield b'{"error":{"code":"RATE_LIMIT_EXCEEDED"}}'
        async def close():
            return None
        return UpstreamResponse(429, ((b"content-type", b"application/json"),
                                      (b"retry-after", b"20")), body(), close)

    monkeypatch.setattr(service, "relay", relay)
    try:
        quote = await clients.post("/web-arena/api/quotes", json={
            "task": "news", "value": "AI policy", "mode": "battle", "providers": ["tinyfish"], "jev": False})
        assert quote.status_code == 200, quote.text
        run_id = quote.json()["id"]
        assert (await clients.post(f"/web-arena/api/runs/{run_id}/start")).status_code == 200
        worker = app._owners.get(run_id)
        if worker:
            await asyncio.wait_for(asyncio.shield(worker), 15)
        attempt = (await clients.get(f"/web-arena/api/runs/{run_id}")).json()["attempts"][0]
        assert attempt["state"] == "error"
        assert attempt["status"] == 429
        assert attempt["detail"] == "Try again in about 20 seconds."
    finally:
        get_settings.cache_clear()


async def test_waterfall_stops_at_first_result_even_with_low_quality_score(clients, monkeypatch):
    monkeypatch.setenv("TREG_WEB_ARENA_ENABLED", "true")
    monkeypatch.setenv("TREG_PLATFORM_KEY_FIRECRAWL", "TEST-FIRECRAWL")
    monkeypatch.setenv("TREG_PLATFORM_KEY_LINKUP", "TEST-LINKUP")
    monkeypatch.setenv("TREG_PLATFORM_PROVIDERS", "firecrawl,linkup")
    get_settings.cache_clear()
    async def weak_match(*args, **kwargs):
        return {"state": "checked", "estimated_match": 10}
    monkeypatch.setattr(web_arena_quality, "search", weak_match)
    seen = []
    monkeypatch.setattr(service, "relay", _relay_by_provider({"firecrawl": [(200, {
        "data": {"web": [{"url": "https://example.com/a", "title": "A"}]}, "creditsUsed": 1})],
        "*": []}, seen))
    try:
        response = await clients.post("/web-arena/api/quotes", json={
            "task": "search", "value": "example query", "mode": "waterfall",
            "providers": ["firecrawl", "linkup"], "jev": True})
        assert response.status_code == 200, response.text
        quote = response.json()
        assert [p["provider"] for p in quote["providers"]] == ["firecrawl", "linkup"]
        started = await clients.post(f"/web-arena/api/runs/{quote['id']}/start")
        assert started.status_code == 200, started.text
        worker = app._owners.get(quote["id"])
        if worker:
            await asyncio.wait_for(asyncio.shield(worker), 15)
        run = (await clients.get(f"/web-arena/api/runs/{quote['id']}")).json()
        first, second = run["attempts"]
        assert first["provider"] == "firecrawl" and first["state"] == "hit"
        assert first["quality"]["estimated_match"] == 10
        assert second["state"] == "not_attempted" and second["charged_micro"] == 0
        assert run["stop_reason"] == "Stopped at the first useful result."
        assert [s[0] for s in seen] == ["firecrawl"]
    finally:
        get_settings.cache_clear()
