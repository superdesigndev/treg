"""Find tools for a job (GET /catalog/find): the discovery experiment's recall + judge, served to
people as a two-event NDJSON stream. Pinned here: the event order and shapes, the verdict at each
cut, the abstaining judge's keyword fallback, the rate limit, and the /search page it powers being
served to signed-out visitors (it has no legacy view to fall back to).
"""
from __future__ import annotations

import dataclasses
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


async def test_provider_display_name_and_old_slug_find_the_same_tools(clients, monkeypatch):
    _on(monkeypatch, find_engine="v1")
    for judge in (_fake_judge({}), _abstain):
        monkeypatch.setattr(judge_infra, "judge", judge)
        for query in ("context", "context.de", "context.dev", "brand.de", "brand.dev"):
            _, events = await _find(clients, query)
            result = events[1]
            assert result["verdict"] == "name" and result["named"] == "provider"
            assert result["rows"] and {row["provider"] for row in result["rows"]} == {"branddev"}


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


async def test_search_page_is_served_to_signed_out_visitors(clients):
    clients.headers.pop("X-Treg-Token", None)
    clients.cookies.clear()
    r = await clients.get("/search")
    assert r.status_code == 200 and "/app/ui/assets/" in r.text
    # `find` is reserved: it is the JSON route, never a platform shelf
    assert (await clients.get("/catalog/find")).status_code == 400


async def _find_on(clients, q, platform):
    clients.headers.pop("X-Treg-Token", None)
    r = await clients.get("/catalog/find", params={"q": q, "platform": platform})
    return r, [json.loads(line) for line in r.text.splitlines() if line.strip()]


async def test_a_shelf_search_reads_and_answers_only_that_shelf(clients, monkeypatch):
    """The platform page's box asks the same judge, over that platform's endpoints only."""
    seen = []
    _on(monkeypatch, find_candidates=30)
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({}, seen))
    r, (first, second) = await _find_on(clients, "enrich a company from its domain", "companies")
    assert r.status_code == 200
    assert first["candidates"] and {c["platform"] for c in first["candidates"]} == {"companies"}
    (_, judged_ids, _), = seen
    assert judged_ids == [c["id"] for c in first["candidates"]] and len(judged_ids) <= 30
    # an abstaining judge falls back to the keyword page, cut to the same shelf
    monkeypatch.setattr(judge_infra, "judge", _abstain)
    _, (_, fallback) = await _find_on(clients, "company enrich domain", "companies")
    assert fallback["verdict"] == "keyword" and fallback["rows"]
    assert {row["platform"] for row in fallback["rows"]} == {"companies"}


async def test_a_bare_name_on_a_shelf_means_that_provider_there(clients, monkeypatch):
    _on(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({}, name=0.95))
    _, (_, judged) = await _find_on(clients, "hunter", "companies")
    assert judged["verdict"] == "name" and judged["named"] == "provider" and judged["rows"]
    assert {(row["provider"], row["platform"]) for row in judged["rows"]} == {("hunter", "companies")}


async def test_an_unknown_shelf_is_a_404(clients, monkeypatch):
    _on(monkeypatch)
    r, _ = await _find_on(clients, "anything at all", "no-such-shelf")
    assert r.status_code == 404


# ==== v2: recall by job (find_engine v2 | shadow) =================================================
from treg.application import catalog_find as F  # noqa: E402
from treg.domain.catalog import find_recall as fr  # noqa: E402
from treg.domain.catalog import store as catalog_store  # noqa: E402
from tests.test_find_recall import _cat  # noqa: E402


def _fake_v2(probs_by_id, seen=None, name=0.0, plat=("people", 0.9)):
    """A judge for units: a job is scored by its capability id, an endpoint by its own id."""
    async def fake(query, cands, **kw):
        if seen is not None:
            seen.append((query, cands, kw))
        extra = {"name": name}
        if "plat" in (kw.get("extra") or {}):
            extra["plat"] = {"choice": plat[0], "confidence": plat[1], "probabilities": {}}
        return judge_infra.Judgement(probs=[probs_by_id.get(c["id"], 0.0) for c in cands], ms=9,
                                     tokens_in=90, tokens_out=4, extra=extra)
    return fake


def _judged(cands, probs, name=0.0, plat=None):
    extra = {"name": name}
    if plat:
        extra["plat"] = {"choice": plat[0], "confidence": plat[1], "probabilities": {}}
    return judge_infra.Judgement(probs=[probs.get(c.unit.id, 0.0) for c in cands], ms=1, extra=extra)


def _decide(q, probs, **kw):
    cat = _cat()
    ix = fr.build(cat)
    cands = fr.recall(q, ix, cat.aliases)
    return F.decide(q, cands, _judged(cands, probs, **kw), ix), cat


def test_v2_rules_in_order():
    abstain = F.decide("x", [], judge_infra.Judgement(probs=None, ms=1), fr.build(_cat()))
    assert abstain.verdict == F.KEYWORD
    # exactly a name wins over a strong fit ("zerobounce" is a name page, not email verify)
    found, _ = _decide("pdl", {"people.search": 0.95})
    assert found.verdict == F.NAME and found.name.keys == ("pdl",)
    # a prefix is a name when the judge reads one, or when the query is short and nothing is strong
    assert _decide("scrapecr", {}, name=0.9)[0].verdict == F.NAME
    assert _decide("imag", {"image-gen.flux.generate": 0.5})[0].verdict == F.NAME
    assert _decide("imag", {"image-gen.flux.generate": 0.8})[0].verdict == F.STRONG
    # a word several platforms share names none: the judged answer stands
    assert _decide("tikt", {"tiktok.video.comments": 0.5})[0].verdict != F.NAME
    # a name the catalog does not carry, and nothing kept: a gap
    gap = _decide("zzqx widgets", {}, name=0.95)[0]
    assert (gap.verdict, gap.reason, gap.kept) == (F.NONE, F.GAP, [])
    # no platform, confidently: a gap under 0.6, else capped at closest - never strong
    q = "find a work email"
    assert _decide(q, {"people.email.find": 0.5}, plat=("none", 0.8))[0].reason == F.GAP
    assert _decide(q, {"people.email.find": 0.9}, plat=("none", 0.8))[0].verdict == F.CLOSEST
    assert _decide(q, {"people.email.find": 0.9}, plat=("none", 0.3))[0].verdict == F.STRONG
    assert _decide(q, {"people.email.find": 0.5})[0].verdict == F.CLOSEST
    # nothing kept: a gap when the judge named a platform with confidence (the catalog has the
    # platform, not this job on it), else not a task - which an agent's search asks to be keyword
    platform_gap = _decide(q, {"people.email.find": 0.2}, plat=("people", 0.9))[0]
    assert (platform_gap.verdict, platform_gap.reason) == (F.NONE, F.GAP)
    not_task = _decide(q, {"people.email.find": 0.2}, plat=("people", 0.3))[0]
    assert (not_task.verdict, not_task.reason) == (F.NONE, F.NOT_TASK)
    cat = _cat()
    ix = fr.build(cat)
    cands = fr.recall(q, ix, cat.aliases)
    agent = F.decide(q, cands, _judged(cands, {"people.email.find": 0.2}, plat=("people", 0.3)), ix, not_task=F.KEYWORD)
    assert (agent.verdict, agent.reason) == (F.KEYWORD, F.NOT_TASK)
    agent_gap = F.decide(q, cands, _judged(cands, {"people.email.find": 0.2}, plat=("people", 0.9)), ix, not_task=F.KEYWORD)
    assert (agent_gap.verdict, agent_gap.reason) == (F.NONE, F.GAP)


def test_v2_a_strong_job_lists_every_vendor_and_a_judged_member_keeps_its_own_fit():
    found, cat = _decide("find someone's personal gmail",
                         {"people.email.find": 0.9, "perso.people.email.find": 0.2})
    assert found.verdict == F.STRONG and "perso.people.email.find" in [c.unit.id for c in found.cands]
    rows = F.expand(found, cat, {})
    # every vendor of the job at its fit, except the member whose own words were judged a miss
    assert {(r["ep"]["id"], r["p"], r["fit_from"]) for r in rows} == {
        ("hunter.people.email.find", 0.9, F.FIT_FROM_JOB), ("leadco.people.email.find", 0.9, F.FIT_FROM_JOB)}
    found, cat = _decide("find someone's personal gmail",
                         {"people.email.find": 0.9, "perso.people.email.find": 0.8})
    rows = F.expand(found, cat, {})
    assert ("perso.people.email.find", 0.8, F.FIT_FROM_ENDPOINT) in {(r["ep"]["id"], r["p"], r["fit_from"]) for r in rows}
    assert len(rows) == 3 and not any(r.get("children_hidden") for r in rows)


def test_v2_a_job_under_high_folds_to_its_first_vendors(monkeypatch):
    monkeypatch.setattr(F, "FOLDED", 2)
    found, cat = _decide("work email", {"people.email.find": 0.5})
    rows = F.expand(found, cat, {"leadco.people.email.find": {"ok_rate": 1.0}})
    assert [r["ep"]["id"] for r in rows] == ["leadco.people.email.find", "hunter.people.email.find"]  # measured first
    assert rows[0]["children_hidden"] == 1
    found, cat = _decide("work email", {"people.email.find": 0.9})
    assert len(F.expand(found, cat, {})) == 3                    # at high: every vendor


def test_v2_a_folded_job_counts_providers_not_rows(monkeypatch):
    """A job under high shows one row per provider, and its hidden count is providers - so "N of M
    providers" on the page adds up to the vendor count the judge was shown."""
    monkeypatch.setattr(F, "FOLDED", 1)
    base = _cat()
    extra = {**base.by_id["hunter.people.email.find"], "id": "hunter.people.email.find.bulk", "name": "Bulk finder"}
    cat = dataclasses.replace(base, endpoints=[*base.endpoints, extra], by_id={**base.by_id, extra["id"]: extra})
    ix = fr.build(cat)
    cands = fr.recall("work email", ix, cat.aliases)
    found = F.decide("work email", cands, _judged(cands, {"people.email.find": 0.5}), ix)
    rows = F.expand(found, cat, {})
    job = ix.units[ix.job_pos["people.email.find"]]
    assert len(job.providers) == 3 and len(job.members) == 4
    assert len(rows) == 1 and rows[0]["children_hidden"] == 2      # 1 of 3 providers, not "1 of 4 rows"
    monkeypatch.setattr(F, "FOLDED", 5)
    rows = F.expand(found, cat, {})
    assert len({r["ep"]["provider"] for r in rows}) == len(rows) == 3 and not rows[0].get("children_hidden")


def test_v2_name_pages():
    cat = _cat()
    ix = fr.build(cat)
    assert {e["platform"] for e in F.name_page(fr.name_of("tiktok", ix), cat)} == {"tiktok", "tiktok-ads"}
    assert F.name_page(fr.name_of("tiktok", ix), cat)[0]["platform"] == "tiktok"
    assert {e["provider"] for e in F.name_page(fr.name_of("hunter", ix), cat)} == {"hunter"}
    assert {e["id"] for e in F.name_page(fr.name_of("flux", ix), cat)} == {"replicate.flux.schnell", "falco.flux.pro"}


def test_v2_provider_name_survives_judge_abstention():
    cat = catalog_store.load()
    ix = fr.index(cat)
    display = lambda service: "Context.dev" if service == "branddev" else service
    for query in ("context.dev", "brand.dev"):
        found = F.decide(query, [], judge_infra.Judgement(probs=None, ms=1), ix,
                         provider_display=display)
        assert found.verdict == F.NAME and found.name.keys == ("branddev",)
        assert {e["provider"] for e in F.name_page(found.name, cat)} == {"branddev"}


async def test_v2_streams_units_and_the_answer_and_logs_its_readings(clients, monkeypatch):
    seen = []
    _on(monkeypatch, find_engine="v2")
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({"people.email.find": 0.92}, seen))
    q = "find the work email of a hotel manager"
    r, (first, second) = await _find(clients, q)
    assert r.status_code == 200
    units = {(u["kind"], u["id"]) for u in first["units"]}
    assert ("job", "people.email.find") in units and len(first["units"]) <= 45
    assert {"id", "platform", "provider"} == set(first["candidates"][0])
    (_, views, kw), = seen
    assert kw["job_criteria"] == F.JOB_CRITERIA and set(kw["extra"]) == {"name", "plat"}
    assert any(v.get("job") for v in views) and [v["id"] for v in views] == [u["id"] for u in first["units"]]

    assert second["verdict"] == "strong" and second["engine"] == "v2" and second["reason"] == ""
    assert second["platform"] == {"choice": "people", "confidence": 0.9}
    ix = fr.index(catalog_store.load())
    members = set(ix.units[ix.job_pos["people.email.find"]].members)
    assert {row["id"] for row in second["rows"]} == members
    assert all(row["p"] == 0.92 and row["fit_from"] == "job" for row in second["rows"])

    await audit.drain()
    async with session_maker() as s:
        (row,) = (await s.execute(select(SearchLog))).scalars().all()
    assert row.engine == "v2" and row.platform_choice == "people" and row.platform_conf == 0.9
    assert row.name_p == 0.0 and row.recall_ms is not None and row.verdict == "strong"
    assert ["job", "people.email.find", 0.92] in row.units and dict(row.judged) == {"people.email.find": 0.92}


async def test_v2_empty_answers_say_why(clients, monkeypatch):
    _on(monkeypatch, find_engine="v2")
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("none", 0.9)))
    _, (_, judged) = await _find(clients, "book a table for two tonight")
    assert (judged["verdict"], judged["reason"], judged["rows"]) == ("none", "gap", [])
    # the judge names a platform with confidence and keeps nothing: the catalog has the platform,
    # not this job on it, which is a gap worth recording too
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("threads", 0.9)))
    _, (_, judged) = await _find(clients, "publish a post to threads")
    assert (judged["verdict"], judged["reason"]) == ("none", "gap")
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("people", 0.3)))
    _, (_, judged) = await _find(clients, "book a table for two tonight")
    assert (judged["verdict"], judged["reason"]) == ("none", "not_task")
    await audit.drain()
    async with session_maker() as s:
        assert [m.reason for m in (await s.execute(select(SearchMiss))).scalars()] == ["gap", "gap", "not_task"]
    monkeypatch.setattr(judge_infra, "judge", _abstain)
    _, (_, judged) = await _find(clients, "backlinks for a domain")
    assert judged["verdict"] == "keyword" and judged["rows"] and all(r["p"] is None for r in judged["rows"])


def test_v2_a_shelf_none_is_scope_never_a_gap():
    """A shelf's find read one shelf: even a name the judge is sure of is not a catalog gap there."""
    cat = _cat()
    ix = fr.build(cat)
    cands = fr.recall("zzqx widgets", ix, cat.aliases, platform="people")
    found = F.decide("zzqx widgets", cands, _judged(cands, {}, name=0.95), ix, platform="people")
    assert (found.verdict, found.reason) == (F.NONE, F.SCOPE)
    found = F.decide("zzqx widgets", cands, _judged(cands, {}), ix, platform="people")
    assert (found.verdict, found.reason) == (F.NONE, F.SCOPE)


async def test_v2_with_no_units_still_tells_a_gap_from_not_a_task(clients, monkeypatch):
    seen = []
    _on(monkeypatch, find_engine="v2")
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, seen, plat=("none", 0.9)))
    q = "zzqx qqxz"                                              # no word on any card, no vectors
    _, (first, judged) = await _find(clients, q)
    assert first["units"] == [] and (judged["verdict"], judged["reason"]) == ("none", "gap")
    (_, views, kw), = seen
    assert views == [] and set(kw["extra"]) == {"name", "plat"}
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("people", 0.3)))
    _, (_, judged) = await _find(clients, q)
    assert (judged["verdict"], judged["reason"]) == ("none", "not_task")


async def test_v2_on_a_shelf_reads_that_shelf_and_asks_no_platform(clients, monkeypatch):
    seen = []
    _on(monkeypatch, find_engine="v2")
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, seen))
    _, (first, _) = await _find_on(clients, "enrich a company from its domain", "companies")
    assert first["units"] and {c["platform"] for c in first["candidates"]} == {"companies"}
    (_, _, kw), = seen
    assert set(kw["extra"]) == {"name"}
    await audit.drain()
    async with session_maker() as s:
        assert [m.reason for m in (await s.execute(select(SearchMiss))).scalars()] == ["scope"]


async def test_shadow_serves_v1_and_logs_both_engines(clients, monkeypatch):
    _on(monkeypatch, find_engine="shadow")
    judged_v1 = _fake_judge({"tiingo.daily.prices": 0.91})
    judged_v2 = _fake_v2({"stocks.eod": 0.9})

    async def either(query, cands, **kw):
        return await (judged_v2 if kw.get("job_criteria") else judged_v1)(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", either)
    _, (first, second) = await _find(clients, JOB)
    assert "units" not in first and "engine" not in second            # the v1 answer, as served today
    assert [r["id"] for r in second["rows"]] == ["tiingo.daily.prices"]
    await audit.drain()
    async with session_maker() as s:
        rows = (await s.execute(select(SearchLog))).scalars().all()
    assert sorted(r.engine for r in rows) == ["v1", "v2"]


async def test_v2_with_the_semantic_channel_reports_it_on_the_event_and_the_log(clients, monkeypatch):
    from treg.application import find_index
    from treg.infra import embed as embed_infra

    _on(monkeypatch, find_engine="v2", find_embed_api_key="embed-key", find_embed_model="test/fake")

    async def fake_embed(texts, **kw):
        return embed_infra.Embedding(vectors=[[1.0, float(len(t) % 7), 0.5] for t in texts], ms=3)
    monkeypatch.setattr(embed_infra, "embed", fake_embed)
    embed_infra.clear_cache()
    find_index.reset()
    find_index.configure(None)
    try:
        # the first find starts the build and answers from the lexical channel
        monkeypatch.setattr(judge_infra, "judge", _fake_v2({}))
        _, (_, judged) = await _find(clients, "find a work email")
        assert judged["embed"] == {"ms": None, "error": "not_ready"}
        await find_index._build.task
        _, events = await _find(clients, "find a work email")
        first, *rest, judged = events
        # the lexical candidates come first, before the query's vector; the fused ones follow
        assert first["event"] == "candidates" and [e["event"] for e in rest] in ([], ["candidates"])
        assert judged["event"] == "judged" and judged["embed"] == {"ms": 3, "error": None}
        assert judged["read"] == len((rest or [first])[-1]["units"])
        await audit.drain()
        async with session_maker() as s:
            rows = (await s.execute(select(SearchLog).order_by(SearchLog.id))).scalars().all()
        assert [(r.embed_ms, r.embed_error) for r in rows] == [(None, "not_ready"), (3, None)]
    finally:
        find_index.reset()


async def test_shadow_files_one_miss_from_the_engine_it_serves(clients, monkeypatch):
    _on(monkeypatch, find_engine="shadow")
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("none", 0.9)))   # both engines find nothing
    await _find(clients, JOB)
    await audit.drain()
    async with session_maker() as s:
        misses = (await s.execute(select(SearchMiss))).scalars().all()
        logs = (await s.execute(select(SearchLog))).scalars().all()
    assert [(m.engine, m.source) for m in misses] == [("v1", "web-find")]
    assert sorted((r.engine, r.verdict) for r in logs) == [("v1", None), ("v2", "none:gap")]


async def test_v2_first_event_does_not_wait_for_the_query_vector(clients, monkeypatch):
    from treg.application import find_index
    from treg.infra import embed as embed_infra

    _on(monkeypatch, find_engine="v2", find_embed_api_key="embed-key", find_embed_model="test/fake")
    asked = []

    async def fake_embed(texts, **kw):
        asked.extend(texts)
        return embed_infra.Embedding(vectors=[[1.0, float(len(t) % 7), 0.5] for t in texts], ms=3)
    monkeypatch.setattr(embed_infra, "embed", fake_embed)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}))
    embed_infra.clear_cache()
    find_index.reset()
    find_index.configure(None)
    try:
        cat = catalog_store.load()
        await find_index.prepare(cat, fr.index(cat))
        asked.clear()
        stream = F._stream_v2("find a work email", lambda s: s, None, None)
        first = await stream.__anext__()
        assert first["event"] == "candidates" and first["units"] and asked == []   # lexical, before the vector
        rest = [e async for e in stream]
        assert asked == ["find a work email"] and rest[-1]["event"] == "judged"
    finally:
        find_index.reset()
