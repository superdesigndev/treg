"""The search query set (tests/fixtures/search_queries.yaml), offline: the word search's page as the
CLI and the keyword fallback show it, v2 recall's job seats, and, for a query that names a
provider, the page a judge keeping the expected job lays out. Plus the CLI's `engine=agent` search,
and the provider-name rules the set leans on."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.test_catalog_find import _fake_v2, _on
from treg.application import catalog_find as find
from treg.application import catalog_search as cs
from treg.domain.catalog import find_recall as fr
from treg.domain.catalog import store as catalog_store
from treg.domain.identity import api_keys as managed_keys
from treg.infra import judge as judge_infra

CASES = yaml.safe_load((Path(__file__).parent / "fixtures" / "search_queries.yaml").read_text())["cases"]


def _id(case):
    return case["q"]


def _matches(pattern: str, ep: dict) -> bool:
    return bool(re.search(pattern, ep["id"]) or re.search(pattern, ep.get("capability") or ""))


def _word_page(q: str, cat) -> list[dict]:
    """The word search's first five rows as `/catalog/search` serves them."""
    ranked, _, _ = catalog_store.rank_band(q, cat, 20)
    rows = [dict(ep) for ep, _ in catalog_store.rerank(ranked, {}, cat)]
    return catalog_store.group_routed(rows, max_children=catalog_store.MAX_ROUTED_CHILDREN)[:5]


@pytest.fixture(autouse=True)
def _routed_on(monkeypatch):
    monkeypatch.setenv("TREG_ROUTED_DISCOVERY", "on")


@pytest.mark.parametrize("case", [c for c in CASES if "word_first" in c or "word_top5" in c], ids=_id)
def test_word_search(case):
    cat = catalog_store.load()
    page = _word_page(case["q"], cat)
    ids = [r["id"] for r in page]
    if "word_first" in case:
        assert page and _matches(case["word_first"], page[0]), ids
    if "word_top5" in case:
        assert any(_matches(case["word_top5"], r) for r in page), ids


@pytest.mark.parametrize("case", [c for c in CASES if "recall" in c], ids=_id)
def test_recall_seats_the_job(case):
    cat = catalog_store.load()
    focus = find.focus_of(case["q"], cat)
    jobs = [c.unit.id for c in find.recall_v2(case["q"], cat, None, focus=focus) if c.unit.kind == fr.JOB]
    assert any(re.search(case["recall"], j) for j in jobs), jobs


@pytest.mark.parametrize("case", [c for c in CASES if "provider" in c], ids=_id)
async def test_a_named_provider_leads_the_kept_job(case, monkeypatch):
    _on(monkeypatch, search_experiment="v2")
    seen = []
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({case["job"]: 0.92}, seen))
    cat = catalog_store.load()
    r = await find.recall_with_meaning(case["q"], cat, lambda s: s)
    found = await find.answer_v2(case["q"], r.cands, cat, lambda s: s, focus=r.focus, keyword_rows=False)
    assert found.verdict == find.STRONG and found.providers == {case["provider"]}
    rows, _, _, _ = cs.agent_page(found, cat, 8, hub=[], steer=True)
    assert rows[0]["ep"]["provider"] == case["provider"], [x["ep"]["id"] for x in rows]
    assert not any(x["ep"].get("kind") == "routed" for x in rows)
    (judged_query, _, _), = seen
    assert case["provider"] not in judged_query.lower().split()      # the judge reads the task, not the name


# ---- the rules behind the set --------------------------------------------------------------------

def test_a_provider_named_with_a_task_is_a_focus_and_a_bare_name_is_not():
    cat = catalog_store.load()
    assert catalog_store.provider_focus("people data labs company enrichment", cat) == ({"pdl"}, "company enrichment")
    assert catalog_store.provider_focus("exa search", cat) == ({"exa"}, "search")
    assert catalog_store.provider_focus("exa", cat) is None                       # a name page's query
    assert catalog_store.provider_focus("tiktok comments", cat) is None           # a platform, not a provider
    assert catalog_store.provider_focus("find the work email of a person", cat) is None
    # a domain is part of a name: "you.com" is You.com, while "you" and "context" alone are words
    assert catalog_store.provider_focus("you.com web search", cat) == ({"you"}, "web search")
    assert catalog_store.provider_focus("context of a web page", cat) is None


def test_a_short_word_matches_whole_not_as_the_start_of_a_longer_one():
    """"exa" finds Exa's rows, never a row that only says "exact"."""
    cat = catalog_store.load()
    rows, _ = catalog_store.search("exa", cat, 200)
    concrete = [ep for ep, _ in rows if ep.get("kind") != "routed"]   # a routed parent rides in with its child
    words = [catalog_store._padded(" ".join(t for _, t in catalog_store._haystacks(ep, cat))).split() for ep in concrete]
    assert concrete and all({"exa", "exas"} & set(w) for w in words)


def test_any_other_short_word_still_starts_a_longer_word():
    """Whole-word matching is for a short provider name only: "web" finds "website"."""
    cat = catalog_store.load()
    rows, _ = catalog_store.search("web", cat, 1000)
    texts = [catalog_store._padded(" ".join(t for _, t in catalog_store._haystacks(ep, cat)))
             for ep, _ in rows if ep.get("kind") != "routed"]
    assert any(" website" in t and " web " not in t for t in texts)


def test_only_the_named_providers_rows_are_admitted_loosely():
    """The rest of the query admits every other row exactly as an ordinary search for it would."""
    cat = catalog_store.load()
    led, _ = catalog_store.search("hunter email", cat, 1000)
    plain, _ = catalog_store.search("email", cat, 1000)
    others = {ep["id"] for ep, _ in led if ep["provider"] != "hunter"}
    assert others == {ep["id"] for ep, _ in plain if ep["provider"] != "hunter" and ep.get("kind") != "routed"}


def test_word_search_and_the_judged_answer_read_a_provider_the_same_way():
    cat = catalog_store.load()
    for case in CASES:
        assert find.focus_of(case["q"], cat).providers == catalog_store.led_by(case["q"], cat), case["q"]


async def test_a_page_led_by_a_named_provider_shows_no_score(clients):
    clients.headers.pop("X-Treg-Token", None)
    out = (await clients.get("/catalog/search", params={"q": "exa search", "limit": 5})).json()
    assert out["led_by"] == ["exa"] and out["results"][0]["provider"] == "exa"
    assert all(r["score"] is None for r in out["results"])
    plain = (await clients.get("/catalog/search", params={"q": "web search", "limit": 5})).json()
    assert "led_by" not in plain and all(r["score"] is not None for r in plain["results"])


def test_a_provider_with_nothing_for_the_rest_reads_as_typed():
    """"perplexity" in a brand-visibility question is the AI engine, not a request for Perplexity's
    API: when the named provider has nothing the rest of the query reaches, nothing changes."""
    cat = catalog_store.load()
    q = "does perplexity mention my brand when people ask about project management tools"
    assert find.focus_of(q, cat).providers == frozenset()


async def test_the_cli_search_is_the_agent_search(clients, monkeypatch):
    _on(monkeypatch, search_experiment="v2")
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({"people.email.find": 0.92}))
    clients.headers.pop("X-Treg-Token", None)
    q = "find the work email of a hotel manager in Cape Town"
    out = (await clients.get("/catalog/search", params={"q": q, "engine": "agent", "limit": 8})).json()
    assert out["engine"] == "agent" and out["verdict"] == "strong"
    assert out["jobs"][0]["capability"] == "people.email.find"
    assert out["results"][0]["id"] == "treg.people.email.find"
    plain = (await clients.get("/catalog/search", params={"q": q, "limit": 8})).json()
    assert "verdict" not in plain and "engine" not in plain                       # unchanged without it


async def test_the_cli_search_falls_back_to_keywords_when_the_judge_is_down(clients, monkeypatch):
    _on(monkeypatch, search_experiment="v2")

    async def refused(query, cands, **kw):
        return judge_infra.Judgement(probs=None, ms=5, error="http_402")
    monkeypatch.setattr(judge_infra, "judge", refused)
    clients.headers.pop("X-Treg-Token", None)
    out = (await clients.get("/catalog/search", params={"q": "verify an email address", "engine": "agent"})).json()
    assert out["verdict"] == "keyword" and out["reason"] == "http_402"
    assert out["results"] and "people.email.verify" in out["results"][0]["capability"]


async def test_the_cli_search_holds_no_connection_while_the_judge_thinks(clients, monkeypatch):
    """Non-negotiable 3: a signed-in search resolves its caller (and the hub's reader) in sessions
    of its own, closed before the embedding and the judge are awaited."""
    from treg.infra import db as infra_db
    _on(monkeypatch, search_experiment="v2", search_experiment_holdout_percent=0,
        hub_enabled=True, hub_teams="some-team")   # a list: the reader is looked up
    held = []
    judged = _fake_v2({"people.email.find": 0.92})

    async def watching(query, cands, **kw):
        held.append(infra_db._engine.sync_engine.pool.checkedout())
        return await judged(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", watching)
    # This search's auth would schedule a last-used write on SQLite's shared pool.
    monkeypatch.setattr(managed_keys, "touch", lambda *args, **kwargs: None)
    out = (await clients.get("/catalog/search", params={"q": "find the work email of a hotel manager in Cape Town",
                                                         "engine": "agent"})).json()
    assert out["verdict"] == "strong" and held == [0]


async def test_a_signed_out_cli_search_is_capped_per_address_not_in_one_shared_bucket(clients, monkeypatch):
    _on(monkeypatch, search_experiment="v2", search_judge_max_per_caller_hour=1)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({"people.email.find": 0.92}))
    clients.headers.pop("X-Treg-Token", None)
    q = {"q": "find the work email of a hotel manager in Cape Town", "engine": "agent"}

    async def search(ip):
        return (await clients.get("/catalog/search", params=q, headers={"X-Forwarded-For": ip})).json()
    assert (await search("198.51.100.7"))["verdict"] == "strong"
    capped = await search("198.51.100.7")
    assert capped["verdict"] == "keyword" and capped["reason"] == cs.RATE_LIMITED
    assert (await search("203.0.113.9"))["verdict"] == "strong"      # another address keeps its own


async def test_a_failed_caller_lookup_searches_as_signed_out(clients, monkeypatch):
    """Who is asking is attribution, never a reason to fail the search."""
    from treg.domain.identity import access
    _on(monkeypatch, search_experiment="v2", search_experiment_holdout_percent=0)   # the token deals no holdout
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({"people.email.find": 0.92}))

    async def broken(*a, **kw):
        raise RuntimeError("identity store down")
    monkeypatch.setattr(access, "require_member", broken)
    r = await clients.get("/catalog/search", params={"q": "find the work email of a hotel manager in Cape Town",
                                                      "engine": "agent"})
    assert r.status_code == 200 and r.json()["verdict"] == "strong"
