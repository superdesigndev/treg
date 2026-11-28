"""Tool requests — the "the catalog doesn't have X" report (`POST /tool-requests`).

The contract under test: anyone may file (no auth — the filer is usually an agent that just got
zero search results and holds no token), per-IP rate limiting is the abuse valve, fields are
capped, and identity — when the caller happens to have one — is attribution on the stored row,
never a requirement. Plus the discovery half: the zero-result search surfaces file-a-request as
its hint, in the API and over MCP.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from tests.test_catalog_find import _abstain, _fake_v2, _on
from treg import audit
from treg.api import TOOLREQ_RATE_MAX, app
from treg.application import catalog_search as search_app
from treg.config import get_settings
from treg.domain.catalog import store as catalog_store
from treg.infra import judge as judge_infra
from treg.infra.db import session_maker
from treg.models import SearchMiss, ToolRequest


async def _fetch_rows() -> list[ToolRequest]:
    async with session_maker() as s:
        return list((await s.execute(select(ToolRequest))).scalars())


async def test_anyone_can_file_without_a_token(clients: AsyncClient):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as anon:
        r = await anon.post("/tool-requests", json={
            "capability": "Ahrefs backlinks", "query": "ahrefs backlinks", "source": "cli"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "received" and r.json()["id"]

    (row,) = await _fetch_rows()
    assert row.capability == "Ahrefs backlinks"
    assert row.query == "ahrefs backlinks"
    assert row.source == "cli"
    assert row.status == "open"
    assert row.org_id is None and row.user_email == ""  # anonymous stays anonymous


async def test_a_token_becomes_attribution_on_the_row(clients: AsyncClient):
    """The default `clients` fixture carries a per-org token — the row should say who asked."""
    r = await clients.post("/tool-requests", json={"capability": "flight prices"})
    assert r.status_code == 200, r.text
    (row,) = await _fetch_rows()
    assert row.user_email == "tim@superdesign.dev"
    assert row.org_id is not None


@pytest.mark.parametrize("capability", ["   ", "x" * 201], ids=["blank", "too-long"])
async def test_a_blank_or_too_long_capability_is_refused(clients: AsyncClient, capability):
    r = await clients.post("/tool-requests", json={"capability": capability})
    assert r.status_code == 422
    assert await _fetch_rows() == []


async def test_free_text_fields_are_capped_not_refused(clients: AsyncClient):
    """The capability is the headline and must be short; the rest is best-effort context — clip it
    rather than bounce a filing over an over-long note."""
    r = await clients.post("/tool-requests", json={
        "capability": "HN comments", "note": "n" * 5000, "contact": "c" * 500,
        "query": "q" * 500, "source": "definitely-not-a-source"})
    assert r.status_code == 200, r.text
    (row,) = await _fetch_rows()
    assert len(row.note) == 2000 and len(row.contact) == 200 and len(row.query) == 300
    assert row.source == "api"  # an unknown source label collapses to "api", not garbage


async def test_filings_are_rate_limited_per_ip(clients: AsyncClient):
    for i in range(TOOLREQ_RATE_MAX):
        r = await clients.post("/tool-requests", json={"capability": f"thing {i}"})
        assert r.status_code == 200, r.text
    r = await clients.post("/tool-requests", json={"capability": "one too many"})
    assert r.status_code == 429
    assert len(await _fetch_rows()) == TOOLREQ_RATE_MAX


async def test_the_empty_search_names_the_way_to_file_one(clients: AsyncClient):
    """Discovery: an agent that searched and found nothing is the exact caller we want a filing
    from — the zero-result response has to say so, or the feature might as well not exist."""
    r = await clients.get("/catalog/search", params={"q": "zzzz-no-such-capability"})
    assert r.status_code == 200
    body = r.json()
    assert body["results"] == []
    assert any("tool-requests" in h for h in body["hints"]), body["hints"]


async def _fetch_misses() -> list[SearchMiss]:
    async with session_maker() as s:
        return list((await s.execute(select(SearchMiss))).scalars())


async def test_the_empty_search_is_logged_as_a_miss(clients: AsyncClient):
    """The other half of discovery: most agents that miss never file, so the miss itself is the
    record (models.SearchMiss). Written fire-and-forget — drain() flushes it for the assert."""
    r = await clients.get("/catalog/search", params={"q": "zzzz-no-such-capability"})
    assert r.status_code == 200 and r.json()["results"] == []
    await audit.drain()
    (row,) = await _fetch_misses()
    assert row.query == "zzzz-no-such-capability"
    assert row.source == "api"


async def test_a_search_that_matches_logs_no_miss(clients: AsyncClient):
    r = await clients.get("/catalog/search", params={"q": "backlinks"})
    assert r.status_code == 200 and r.json()["results"]
    await audit.drain()
    assert await _fetch_misses() == []


async def test_a_blank_search_logs_no_miss(clients: AsyncClient):
    """No q is a browse, not a failed ask — logging it would drown the signal in noise."""
    r = await clients.get("/catalog/search")
    assert r.status_code == 200
    await audit.drain()
    assert await _fetch_misses() == []


async def test_an_exempt_address_is_not_rate_limited(clients: AsyncClient, monkeypatch):
    monkeypatch.setattr(get_settings(), "rate_limit_exempt_ips", "127.0.0.1")
    for i in range(TOOLREQ_RATE_MAX + 1):
        r = await clients.post("/tool-requests", json={"capability": f"thing {i}"})
        assert r.status_code == 200, r.text
    assert len(await _fetch_rows()) == TOOLREQ_RATE_MAX + 1


# ---- the reply names existing tools that may already do the job ----------------------------

async def test_the_reply_names_existing_tools_the_text_matches(clients: AsyncClient):
    """The ids are the shipped ranker's for the same text; the reply's old fields are unchanged."""
    r = await clients.post("/tool-requests", json={"capability": "backlinks"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "received" and body["id"] and body["note"].startswith("logged")
    expected = [ep["id"] for ep, _ in search_app.request_matches("backlinks", catalog_store.load())]
    assert expected and [m["endpoint_id"] for m in body["matches"]] == expected
    assert len(expected) <= search_app.REQUEST_MATCHES
    assert all(set(m) == {"endpoint_id", "name", "provider", "summary"} for m in body["matches"])
    assert "recorded either way" in body["matches_note"]
    assert len(await _fetch_rows()) == 1


@pytest.mark.parametrize("capability", ["!!! ???", "zzzz-no-such-capability", "a " * 99],
                         ids=["punctuation", "no-match", "function-words"])
async def test_text_that_finds_nothing_files_with_an_empty_list(clients: AsyncClient, capability):
    r = await clients.post("/tool-requests", json={"capability": capability})
    assert r.status_code == 200, r.text
    assert r.json()["matches"] == [] and "matches_note" not in r.json()
    assert len(await _fetch_rows()) == 1


def test_a_long_text_searches_a_bounded_number_of_words(monkeypatch):
    """Each searched word is one pass over the catalog, so the words searched are capped."""
    searched: list[str] = []
    real = catalog_store.search
    monkeypatch.setattr(catalog_store, "search", lambda q, *a, **k: (searched.append(q), real(q, *a, **k))[1])
    text = " ".join(f"word{i}" for i in range(60))
    assert search_app.request_matches(text, catalog_store.load()) == []
    (query,) = searched
    assert catalog_store.content_words(query) == [f"word{i}" for i in range(search_app.REQUEST_TERMS)]


@pytest.mark.parametrize("text, right", [
    ("Zillow property listings", lambda i: "zillow" in i),
    ("text to speech in Persian", lambda i: "instagram" not in i and "transcript" not in i),
    ("generate image from text prompt", lambda i: "video" not in i),
    ("domain WHOIS lookup", lambda i: "whois" in i),
], ids=["zillow", "tts", "image", "whois"])
def test_a_row_that_misses_a_word_does_not_point_the_wrong_way(text, right):
    """Search admits a row missing one word in three; the reply keeps only rows holding every
    word, or else the rows tied at the top."""
    ids = [ep["id"] for ep, _ in search_app.request_matches(text, catalog_store.load())]
    assert ids and all(right(i) for i in ids), ids


@pytest.mark.parametrize("text, word", [("TikTok comments", "comments"), ("YouTube transcript", "youtube")])
def test_a_plain_request_still_finds_its_tools(text, word):
    ids = [ep["id"] for ep, _ in search_app.request_matches(text, catalog_store.load())]
    assert len(ids) >= 2 and all(word in i for i in ids), ids


async def test_the_search_that_came_up_empty_is_searched_too(clients: AsyncClient):
    r = await clients.post("/tool-requests", json={"capability": "zzzz-no-such-capability", "query": "backlinks"})
    assert r.status_code == 200 and r.json()["matches"], r.text


async def test_the_directory_surface_skips_the_matcher(clients: AsyncClient, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("the matcher ran for a reply that drops it")
    monkeypatch.setattr(search_app, "request_matches", boom)
    r = await clients.post("/tool-requests", json={"capability": "backlinks", "source": "claude-connector"})
    assert r.status_code == 200 and "matches" not in r.json()
    assert len(await _fetch_rows()) == 1


async def test_the_matcher_runs_on_its_own_pool(monkeypatch):
    import threading
    seen = []
    monkeypatch.setattr(search_app, "request_reply", lambda *_a: seen.append(threading.current_thread().name) or {})
    await search_app.request_reply_off_loop("backlinks")
    assert seen and seen[0].startswith("treg-request-match")


async def test_a_failing_matcher_still_files_the_request(clients: AsyncClient, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("matcher down")
    monkeypatch.setattr(search_app, "request_matches", boom)
    r = await clients.post("/tool-requests", json={"capability": "backlinks"})
    assert r.status_code == 200 and r.json()["status"] == "received" and r.json()["matches"] == []
    assert len(await _fetch_rows()) == 1


# ---- a long request's matches are the search judge's -----------------------------------------

LONG = "find the verified work email address of a person from their name and company"
JOB = "people.email.find"


def _word_rows(monkeypatch) -> list[str]:
    """The word search answers one known row, so a reply built from it is told apart."""
    ep = next(e for e in catalog_store.load().by_id.values() if e.get("capability") == "web.crawl")
    monkeypatch.setattr(search_app, "request_matches", lambda *_a, **_k: [(ep, 1.0)])
    return [ep["id"]]


def test_the_long_request_is_long_and_a_short_one_is_not():
    assert len(catalog_store.content_words(LONG)) >= search_app.REQUEST_JUDGE_WORDS
    assert len(catalog_store.content_words("TikTok comments")) < search_app.REQUEST_JUDGE_WORDS


async def test_a_long_request_is_matched_by_one_judge_request(clients: AsyncClient, monkeypatch):
    _on(monkeypatch, search_experiment="v2")
    seen = []
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({JOB: 0.92}, seen))
    words = _word_rows(monkeypatch)
    r = await clients.post("/tool-requests", json={"capability": LONG})
    assert r.status_code == 200 and r.json()["status"] == "received", r.text
    ids = [m["endpoint_id"] for m in r.json()["matches"]]
    cat = catalog_store.load()
    assert ids and ids != words and all(cat.by_id[i].get("capability") == JOB for i in ids), ids
    assert len(ids) <= search_app.REQUEST_MATCHES
    (query, _, kw), = seen
    assert query == LONG and kw["timeout_s"] == get_settings().typesafe_timeout_s
    assert len(await _fetch_rows()) == 1


async def test_a_short_request_never_asks_the_judge(clients: AsyncClient, monkeypatch):
    _on(monkeypatch, search_experiment="v2")
    seen = []
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({JOB: 0.92}, seen))
    r = await clients.post("/tool-requests", json={"capability": "backlinks"})
    expected = [ep["id"] for ep, _ in search_app.request_matches("backlinks", catalog_store.load())]
    assert r.status_code == 200 and [m["endpoint_id"] for m in r.json()["matches"]] == expected
    assert seen == []


async def _raises(*_a, **_k):
    raise RuntimeError("judge down")


async def _bad_body(*_a, **_k):
    return judge_infra.Reply(answers={"c0": {"noul": "not a number"}}, ms=5)


async def _keeps_nothing(query, cands, **kw):
    return await _fake_v2({}, plat=("people", 0.9))(query, cands, **kw)


@pytest.mark.parametrize("case", ["abstains", "raises", "bad_body", "keeps_nothing", "judge_off", "no_key"])
async def test_a_judge_that_gives_no_rows_falls_back_to_the_word_search(clients: AsyncClient, monkeypatch, case):
    _on(monkeypatch, search_experiment="v2")
    asked = []
    if case == "bad_body":
        judge_infra.clear_cache()
        monkeypatch.setattr(judge_infra, "ask", _bad_body)
    elif case in ("judge_off", "no_key"):
        monkeypatch.setattr(judge_infra, "judge", _fake_v2({JOB: 0.92}, asked))
        monkeypatch.setattr(get_settings(), *(("search_experiment", "shadow") if case == "judge_off"
                                              else ("typesafe_api_key", "")))
    else:
        monkeypatch.setattr(judge_infra, "judge", {"abstains": _abstain, "raises": _raises,
                                                    "keeps_nothing": _keeps_nothing}[case])
    words = _word_rows(monkeypatch)
    r = await clients.post("/tool-requests", json={"capability": LONG})
    assert r.status_code == 200 and r.json()["status"] == "received", r.text
    assert [m["endpoint_id"] for m in r.json()["matches"]] == words
    assert asked == [] and len(await _fetch_rows()) == 1


async def test_a_slow_judge_is_cut_off_and_the_word_search_answers(clients: AsyncClient, monkeypatch):
    """The judge's own timeout is per network phase; the whole judged road is bounded as well."""
    _on(monkeypatch, search_experiment="v2", typesafe_timeout_s=0.05, find_embed_timeout_s=0.05)

    async def hangs(*_a, **_k):
        await asyncio.sleep(30)
    monkeypatch.setattr(judge_infra, "judge", hangs)
    words = _word_rows(monkeypatch)
    t0 = time.perf_counter()
    r = await clients.post("/tool-requests", json={"capability": LONG})
    assert time.perf_counter() - t0 < 5
    assert r.status_code == 200 and [m["endpoint_id"] for m in r.json()["matches"]] == words
    assert len(await _fetch_rows()) == 1


async def test_an_exempt_address_stays_within_the_judge_cap(clients: AsyncClient, monkeypatch, connections_held_here):
    """An exempt address has no filing limit, so the search's per-caller judge cap bounds its
    judging; past it the word search answers, and every filing is still filed."""
    _on(monkeypatch, search_experiment="v2", search_judge_max_per_caller_hour=5, rate_limit_exempt_ips="127.0.0.1")
    seen, held = [], []
    fake = _fake_v2({JOB: 0.92}, seen)

    async def judge(query, cands, **kw):
        held.append(connections_held_here())
        return await fake(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", judge)
    words = _word_rows(monkeypatch)
    rs = await asyncio.gather(*(clients.post("/tool-requests", json={"capability": f"{LONG} number {i}"})
                                for i in range(40)))
    assert all(r.status_code == 200 and r.json()["status"] == "received" for r in rs)
    replies = [[m["endpoint_id"] for m in r.json()["matches"]] for r in rs]
    assert 0 < len(seen) <= 5 and sum(ids != words for ids in replies) == len(seen)
    assert sum(ids == words for ids in replies) == 40 - len(seen)
    assert held == [0] * len(seen)
    assert len(await _fetch_rows()) == 40


async def test_the_directory_surface_never_asks_the_judge(clients: AsyncClient, monkeypatch):
    _on(monkeypatch, search_experiment="v2")
    seen = []
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({JOB: 0.92}, seen))
    r = await clients.post("/tool-requests", json={"capability": LONG, "source": "claude-connector"})
    assert r.status_code == 200 and "matches" not in r.json()
    assert seen == [] and len(await _fetch_rows()) == 1
