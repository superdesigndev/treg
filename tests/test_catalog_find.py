"""Find tools for a job (GET /catalog/find): the discovery experiment's recall + judge, served to
people as a two-event NDJSON stream. Pinned here: the event order and shapes, the verdict at each
cut, the abstaining judge's keyword fallback, the rate limit, and the /search page it powers being
served to signed-out visitors (it has no legacy view to fall back to).
"""
from __future__ import annotations

import json

from sqlmodel import select

from treg import audit
from treg.config import get_settings
from treg.infra import judge as judge_infra
from treg.infra.db import session_maker
from treg.models import SearchLog, SearchMiss

JOB = "apple stock closing prices for last year"


def _on(monkeypatch, **over):
    s = get_settings()
    monkeypatch.setattr(s, "typesafe_api_key", "test-key", raising=False)
    for k, v in over.items():
        monkeypatch.setattr(s, k, v, raising=False)


def _fake_judge(probs_by_id, seen=None, name=0.0):
    async def fake(query, cands, **kw):
        if seen is not None:
            seen.append((query, [c["id"] for c in cands], kw))
        return judge_infra.Judgement(probs=[probs_by_id.get(c["id"], 0.0) for c in cands], ms=12,
                                     tokens_in=100, tokens_out=5, extra={"name": name})
    return fake


async def _abstain(query, cands, **kw):
    return judge_infra.Judgement(probs=None, ms=2500, error="timeout")


async def _find(clients, q):
    clients.headers.pop("X-Treg-Token", None)              # open route: no identity needed
    r = await clients.get("/catalog/find", params={"q": q})
    return r, [json.loads(line) for line in r.text.splitlines() if line.strip()]


async def test_streams_candidates_then_the_judged_rows(clients, monkeypatch):
    seen = []
    _on(monkeypatch, find_candidates=60)
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({"tiingo.daily.prices": 0.91, "marketstack.eod": 0.55}, seen))
    r, events = await _find(clients, JOB)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-ndjson")
    first, second = events
    assert first["event"] == "candidates"
    ids = [c["id"] for c in first["candidates"]]
    assert "tiingo.daily.prices" in ids and 0 < len(ids) <= 60
    assert set(first["candidates"][0]) == {"id", "platform", "provider"}
    # the judge read exactly the recall, with the find route's own timeout
    (query, judged_ids, kw), = seen
    assert query == JOB and judged_ids == ids and kw["timeout_s"] == get_settings().find_timeout_s
    assert kw["criteria"] and set(kw["extra"]) == {"name"}   # what a fit means, and "is it a name?"

    assert second["event"] == "judged" and second["verdict"] == "strong" and second["read"] == len(ids)
    assert second["high"] == get_settings().search_judge_high
    assert [row["id"] for row in second["rows"]] == ["tiingo.daily.prices", "marketstack.eod"]  # best first, cut at keep
    top = second["rows"][0]
    assert top["p"] == 0.91 and top["platform"] and top["capability"] and top["provider_display"]
    assert top["cost"]["type"]

    await audit.drain()
    async with session_maker() as s:
        (row,) = (await s.execute(select(SearchLog))).scalars().all()
    assert row.mode == "find" and row.source == "web-find" and row.org_id is None
    assert dict(row.judged) == {"tiingo.daily.prices": 0.91, "marketstack.eod": 0.55}


async def test_verdicts_at_each_cut(clients, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({"tiingo.daily.prices": 0.6}))
    _, events = await _find(clients, JOB)
    assert events[1]["verdict"] == "closest" and [r["id"] for r in events[1]["rows"]] == ["tiingo.daily.prices"]

    monkeypatch.setattr(judge_infra, "judge", _fake_judge({}))
    _, events = await _find(clients, JOB)
    assert events[1]["verdict"] == "none" and events[1]["rows"] == []
    await audit.drain()
    async with session_maker() as s:
        assert [m.source for m in (await s.execute(select(SearchMiss))).scalars()] == ["web-find"]


async def test_a_bare_name_is_answered_with_what_it_offers(clients, monkeypatch):
    _on(monkeypatch)
    # the judge reads "tiktok" as a name: the answer is the TikTok platforms, the one named first
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({}, name=0.96))
    _, events = await _find(clients, "tiktok")
    judged = events[1]
    assert judged["verdict"] == "name" and judged["named"] == "platform" and judged["rows"][0]["platform"] == "tiktok"
    assert all(row["p"] is None for row in judged["rows"])
    assert {row["platform"] for row in judged["rows"]} >= {"tiktok", "tiktok-ads"}

    # a provider's name, with no platform of that name, is that provider's endpoints
    _, events = await _find(clients, "semrush")
    assert events[1]["verdict"] == "name" and events[1]["named"] == "provider"
    assert {r["provider"] for r in events[1]["rows"]} == {"semrush"}

    # exactly a platform's name counts even when the judge is unsure
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({}, name=0.6))
    _, events = await _find(clients, "google ads")
    assert events[1]["verdict"] == "name" and {r["platform"] for r in events[1]["rows"]} == {"google-ads"}

    # a name nothing in the catalog carries falls through to the judged verdict (and is a miss)
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({}, name=0.97))
    _, events = await _find(clients, "zzqx-nothing")
    assert events[1]["verdict"] == "none"
    await audit.drain()
    async with session_maker() as s:
        logs = (await s.execute(select(SearchLog))).scalars().all()
        misses = (await s.execute(select(SearchMiss))).scalars().all()
    assert [m.query for m in misses] == ["zzqx-nothing"]
    assert {tuple(x)[1] for x in logs[0].shown} == {"name"}


async def test_a_strong_fit_wins_over_a_name(clients, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({"tiingo.daily.prices": 0.9}, name=0.95))
    _, events = await _find(clients, JOB)
    assert events[1]["verdict"] == "strong"


async def test_an_abstaining_judge_serves_the_keyword_page(clients, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _abstain)
    _, events = await _find(clients, "backlinks for a domain")
    judged = events[1]
    assert judged["verdict"] == "keyword" and judged["rows"]
    assert all(row["p"] is None for row in judged["rows"])


async def test_refuses_empty_unconfigured_and_over_the_limit(clients, monkeypatch):
    r, _ = await _find(clients, "   ")
    assert r.status_code == 400
    _on(monkeypatch, typesafe_api_key="")
    r, _ = await _find(clients, JOB)
    assert r.status_code == 503
    _on(monkeypatch, find_max_per_ip_hour=1)
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({}))
    assert (await _find(clients, JOB))[0].status_code == 200
    r, _ = await _find(clients, JOB)
    assert r.status_code == 429


async def test_search_page_is_served_to_signed_out_visitors(clients, monkeypatch):
    s = get_settings()
    clients.headers.pop("X-Treg-Token", None)
    clients.cookies.clear()
    monkeypatch.setattr(s, "dashboard_rollout_enabled", True)
    monkeypatch.setattr(s, "dashboard_rollout_percent", 0)
    r = await clients.get("/search")
    assert r.status_code == 200 and "/app/legacy/assets/" not in r.text
    assert "<title>Find tools for your agent | treg</title>" in r.text
    monkeypatch.setattr(s, "dashboard_rollout_enabled", False)
    assert (await clients.get("/search")).status_code == 404
    # `find` is reserved: it is the JSON route, never a platform shelf
    assert (await clients.get("/catalog/find")).status_code == 400
