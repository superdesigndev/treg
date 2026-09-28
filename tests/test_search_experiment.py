"""The discovery experiment: a judge over a wider recall, compared with the shipped ranker on behaviour.

Three layers, each pinned where it can be wrong on its own: the pure merge/credit rules
(domain.catalog.interleave), the judge client's failure modes (infra.judge — it must abstain, never
raise), and the use case's promise that every arm gets a page (application.search_experiment). The
MCP tests at the end prove the wiring: `off` is byte-identical to before, `shadow` changes nothing
the caller sees and writes the row, `interleave` serves the merge and owns each row.
"""
from __future__ import annotations

import json

import httpx

from treg.application import search_experiment as se
from treg.config import get_settings
from treg.domain.catalog import interleave
from treg.domain.catalog import store as catalog_store
from treg.infra import judge as judge_infra


# ---- domain: recall, merge, credit ----------------------------------------------------------------
def test_candidates_admit_on_one_hit_and_hold_no_routed_rows():
    """The gate zeroes "apple stock closing prices for last year" (six rare words, four must hit);
    the judge's recall admits the daily-price rows on `stock`/`prices` alone and leaves routed
    parents to `with_routed_parents`."""
    cat = catalog_store.load()
    q = "apple stock closing prices for last year"
    assert catalog_store.search(q, cat)[1] == 0
    cands = catalog_store.candidates(q, cat, 40)
    ids = [ep["id"] for ep, _ in cands]
    assert "tiingo.daily.prices" in ids
    assert all(ep.get("kind") != "routed" for ep, _ in cands)
    assert len(cands) <= 40 and [s for _, s in cands] == sorted((s for _, s in cands), reverse=True)


def test_team_draft_alternates_and_marks_shared_rows():
    page = interleave.team_draft(["a", "b", "c"], ["c", "d", "e"], k=4, seed=1)
    ids = [i for i, _ in page]
    assert len(page) == 4 and len(set(ids)) == 4
    assert ids[0] in ("a", "c") and set(ids[:2]) == {"a", "c"}     # round one: each side's top pick
    owners = dict(page)
    assert owners["c"] == interleave.BOTH                            # wanted by both, whoever took it
    assert owners["a"] == interleave.BASELINE
    assert owners.get("d", interleave.JUDGED) == interleave.JUDGED


def test_team_draft_is_reproducible_and_survives_an_empty_side():
    a, b = ["x", "y"], ["p", "q", "r"]
    assert interleave.team_draft(a, b, 5, seed=7) == interleave.team_draft(a, b, 5, seed=7)
    assert [i for i, _ in interleave.team_draft([], b, 8, seed=3)] == b   # baseline empty: judged fills the page
    assert interleave.team_draft(a, a, 8, seed=3) == [("x", "both"), ("y", "both")]


def test_credit_only_on_disagreement():
    base, judged = ["a", "b", "c"], ["b", "d", "a"]
    assert interleave.credit("c", base, judged) == interleave.BASELINE     # only baseline had it
    assert interleave.credit("d", base, judged) == interleave.JUDGED       # only judged had it
    assert interleave.credit("b", base, judged) == interleave.JUDGED       # both; judged ranked it higher
    assert interleave.credit("a", base, judged) == interleave.BASELINE     # both; baseline ranked it higher
    assert interleave.credit("a", ["a"], ["a"]) is None                     # same rank: no evidence
    assert interleave.credit("zz", base, judged) is None                    # found some other way


def test_bucketed_drops_below_keep_and_keeps_lexical_order_inside_a_bucket():
    rows = [({"id": f"e{i}"}, 10.0 - i) for i in range(5)]      # lexical order e0 > e1 > ... > e4
    probs = [0.5, 0.9, 0.2, 0.95, 0.45]
    out = interleave.bucketed(rows, probs, keep=0.4, high=0.7)
    assert [ep["id"] for ep, _, _ in out] == ["e1", "e3", "e0", "e4"]   # high bucket first, e2 dropped


# ---- infra: the judge abstains, never raises --------------------------------------------------------
def _transport(handler):
    return httpx.MockTransport(handler)


async def _judge(handler, query="scrape a page", n=2, timeout_s=1.0):
    cands = [{"id": f"c{i}", "name": "", "summary": "", "capability": "", "platform": ""} for i in range(n)]
    return await judge_infra.judge(query, cands, api_key="k", model="jev-latest", url="https://judge.test/v1",
                                   timeout_s=timeout_s, transport=_transport(handler))


async def test_judge_parses_probabilities_in_candidate_order_and_caches():
    judge_infra.clear_cache()
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        assert body["model"] == "jev-latest" and set(body["questions"]) == {"c0", "c1"}
        assert "criteria" not in body["questions"]["c0"]     # the experiment's question, unchanged
        assert body["state"]["candidates"][1]["i"] == 1
        return httpx.Response(200, json={"answers": {"c0": {"type": "noul", "noul": 0.12},
                                                     "c1": {"type": "noul", "noul": 0.93}},
                                         "usage": {"input_tokens": 321, "output_tokens": 7}})

    v = await _judge(handler)
    assert v.probs == [0.12, 0.93] and v.error is None and v.tokens_in == 321 and not v.cached
    again = await _judge(handler)
    assert again.probs == [0.12, 0.93] and again.cached
    assert len(seen) == 1                                    # the second answer came from the cache


async def test_judge_sends_criteria_and_extra_questions_in_the_same_request():
    judge_infra.clear_cache()
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append(body)
        return httpx.Response(200, json={"answers": {"c0": {"noul": 0.2}, "x_name": {"noul": 0.97}}})

    cands = [{"id": "c0", "name": "", "summary": "", "capability": "", "platform": ""}]
    kw = dict(api_key="k", model="jev-latest", url="https://judge.test/v1", timeout_s=1.0,
              transport=_transport(handler))
    crit = {"true": "fits", "false": "does not"}
    v = await judge_infra.judge("google", cands, criteria=crit, extra={"name": {"type": "noul", "instructions": "a name"}}, **kw)
    assert v.probs == [0.2] and v.extra == {"name": 0.97}
    assert seen[0]["questions"]["c0"]["criteria"] == crit and "x_name" in seen[0]["questions"]
    again = await judge_infra.judge("google", cands, criteria=crit, extra={"name": {"type": "noul", "instructions": "a name"}}, **kw)
    assert again.cached and again.extra == {"name": 0.97}
    # other questions are another answer: not served from that cache entry
    await judge_infra.judge("google", cands, **kw)
    assert len(seen) == 2


async def test_judge_abstains_on_timeout_http_error_and_bad_body():
    judge_infra.clear_cache()

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)
    v = await _judge(slow)
    assert v.probs is None and v.error == "timeout"

    def denied(request):
        return httpx.Response(403, json={"detail": "no"})
    v = await _judge(denied, query="other")
    assert v.probs is None and v.error == "http_403"

    def garbage(request):
        return httpx.Response(200, json={"answers": {}})
    v = await _judge(garbage, query="third")
    assert v.probs is None and v.error == "KeyError"


# ---- application: every arm gets a page -----------------------------------------------------------
def _on(monkeypatch, mode="interleave", **over):
    s = get_settings()
    monkeypatch.setattr(s, "search_experiment", mode, raising=False)
    monkeypatch.setattr(s, "typesafe_api_key", "test-key", raising=False)
    for k, v in over.items():
        monkeypatch.setattr(s, k, v, raising=False)


def test_mode_is_off_without_a_judge_key_or_with_an_unknown_value(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "search_experiment", "interleave", raising=False)
    monkeypatch.setattr(s, "typesafe_api_key", "", raising=False)
    assert se.mode() == "off"
    _on(monkeypatch, mode="banana")
    assert se.mode() == "off"
    _on(monkeypatch, mode="shadow")
    assert se.mode() == "shadow"


def test_arms_are_sticky_per_caller_and_dealt_in_the_configured_shares(monkeypatch):
    _on(monkeypatch, search_experiment_holdout_percent=10, search_experiment_salt="s1")
    keys = [se.caller_key(f"token-{i}") for i in range(2000)]
    arms = [se.arm_for(k) for k in keys]
    assert arms == [se.arm_for(k) for k in keys]                  # sticky
    share = {a: arms.count(a) / len(arms) for a in set(arms)}
    assert 0.07 < share[se.ARM_BASELINE] < 0.13 and 0.07 < share[se.ARM_JUDGED] < 0.13
    assert share[se.ARM_INTERLEAVE] > 0.75
    assert se.arm_for(None) == se.ARM_INTERLEAVE
    monkeypatch.setattr(get_settings(), "search_experiment_salt", "s2", raising=False)
    assert [se.arm_for(k) for k in keys] != arms                  # a new salt re-deals


def _fake_judge(probs_by_id):
    async def fake(query, cands, **kw):
        return judge_infra.Judgement(probs=[probs_by_id.get(c["id"], 0.0) for c in cands], ms=12,
                                     tokens_in=100, tokens_out=5)
    return fake


async def _finish(rows):
    rows = sorted(rows, key=lambda r: -r[1])
    return rows[:8], {}


async def test_run_serves_baseline_in_shadow_and_logs_both_pages(monkeypatch):
    _on(monkeypatch, mode="shadow")
    cat = catalog_store.load()
    q = "apple stock closing prices for last year"
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({"tiingo.daily.prices": 0.9, "marketstack.eod.latest": 0.8}))
    out = await se.run(q, cat, baseline=[], baseline_total=0, limit=8, caller="abc", finish=_finish)
    assert out.arm == se.ARM_SHADOW and out.shown == []           # the caller sees the (empty) baseline
    assert out.log["baseline_ids"] == [] and out.log["differs"] is True
    judged = dict(out.log["judged"])                                  # the whole judged page, with probabilities
    assert judged["tiingo.daily.prices"] == 0.9 and judged["marketstack.eod.latest"] == 0.8
    assert judged["treg.stocks.eod.history"] is None                  # the routed parent rode in: listed, no probability
    assert set(judged) == {"tiingo.daily.prices", "marketstack.eod.latest", "treg.stocks.eod.history"}   # 0.0 rows dropped
    assert out.log["judge_ms"] == 12 and out.log["judge_error"] is None
    assert all(owner == "shadow" for _, owner in out.log["shown"])


async def test_run_falls_back_to_the_baseline_when_the_judge_abstains(monkeypatch):
    _on(monkeypatch, mode="interleave", search_experiment_holdout_percent=0)
    cat = catalog_store.load()
    ranked, total, _ = catalog_store.rank_band("backlinks", cat, 8)
    baseline = ranked[:8]

    async def abstain(query, cands, **kw):
        return judge_infra.Judgement(probs=None, ms=1500, error="timeout")
    monkeypatch.setattr(judge_infra, "judge", abstain)
    out = await se.run("backlinks", cat, baseline=baseline, baseline_total=total, limit=8, caller="k", finish=_finish)
    assert out.arm == se.ARM_INTERLEAVE and out.shown == baseline
    assert out.log["judged"] is None and out.log["differs"] is False and out.log["judge_error"] == "timeout"


async def test_run_interleaves_and_owns_rows_and_pure_arms_get_pure_pages(monkeypatch):
    _on(monkeypatch, mode="interleave", search_experiment_holdout_percent=50)
    cat = catalog_store.load()
    q = "apple stock closing prices for last year"
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({"tiingo.daily.prices": 0.9}))
    # a fabricated baseline so both pages are non-empty and disagree
    other = cat.by_id["dataforseo.web.backlinks.summary"]
    baseline = [(other, 5.0)]
    keys = {se.arm_for(se.caller_key(f"t{i}")): se.caller_key(f"t{i}") for i in range(200)}
    assert {se.ARM_BASELINE, se.ARM_JUDGED} <= set(keys)

    out = await se.run(q, cat, baseline=baseline, baseline_total=1, limit=8, caller=keys[se.ARM_BASELINE], finish=_finish)
    assert out.arm == se.ARM_BASELINE and [ep["id"] for ep, _ in out.shown] == [other["id"]]

    out = await se.run(q, cat, baseline=baseline, baseline_total=1, limit=8, caller=keys[se.ARM_JUDGED], finish=_finish)
    # the judged page steers like the baseline: the routed parent rides in over its matched child
    assert out.arm == se.ARM_JUDGED
    # (the test's `_finish` skips routed grouping, so only membership is asserted here — the MCP
    # wiring's finish groups the parent above its children exactly as the baseline page does)
    assert {ep["id"] for ep, _ in out.shown} == {"treg.stocks.eod.history", "tiingo.daily.prices"}
    assert all(score < se._BUCKET_LIFT for _, score in out.shown)   # the bucket lift never reaches the caller

    monkeypatch.setattr(get_settings(), "search_experiment_holdout_percent", 0, raising=False)
    out = await se.run(q, cat, baseline=baseline, baseline_total=1, limit=8, caller="anyone", finish=_finish)
    assert out.arm == se.ARM_INTERLEAVE
    assert {ep["id"] for ep, _ in out.shown} == {other["id"], "treg.stocks.eod.history", "tiingo.daily.prices"}
    assert out.owners == {other["id"]: "baseline", "treg.stocks.eod.history": "judged", "tiingo.daily.prices": "judged"}
    assert sorted(o for _, o in out.log["shown"]) == ["baseline", "judged", "judged"]


# ---- the MCP wiring -------------------------------------------------------------------------------
async def _search_rows_and_log(clients, query, token):
    from sqlmodel import select

    from treg import audit
    from treg.infra.db import session_maker
    from treg.models import SearchLog
    from test_mcp import _call_tool, mcp_session

    async with mcp_session(clients) as c:
        out = await _call_tool(c, "catalog_search", {"query": query, "limit": 8}, token=token)
    await audit.drain()
    async with session_maker() as s:
        rows = (await s.execute(select(SearchLog))).scalars().all()
    return out, rows


async def test_mcp_off_writes_no_searchlog(clients):
    token = (await clients.post("/users", json={"email": "off@superdesign.dev"})).json()["token"]
    out, rows = await _search_rows_and_log(clients, "backlinks", token)
    assert out["results"] and rows == []


async def test_mcp_shadow_serves_the_baseline_and_records_both_pages(clients, monkeypatch):
    _on(monkeypatch, mode="shadow")
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({"tiingo.daily.prices": 0.9}))
    token = (await clients.post("/users", json={"email": "shadow@superdesign.dev"})).json()["token"]
    q = "apple stock closing prices for last year"
    out, rows = await _search_rows_and_log(clients, q, token)
    assert out["count"] == 0 and "hint" in out                     # the caller still sees the lexical miss
    (row,) = rows
    assert row.query == q and row.source == "mcp" and row.mode == "shadow" and row.arm == "shadow"
    assert row.user_email == "shadow@superdesign.dev" and row.org_id is not None
    assert row.baseline_total == 0 and row.baseline_ids == [] and row.differs is True
    assert dict(row.judged) == {"treg.stocks.eod.history": None, "tiingo.daily.prices": 0.9}
    assert row.shown == [] and row.judge_ms == 12 and row.judge_error is None
    # and the lexical miss is still the miss log's business
    from sqlmodel import select
    from treg.infra.db import session_maker
    from treg.models import SearchMiss
    async with session_maker() as s:
        assert [m.query for m in (await s.execute(select(SearchMiss))).scalars()] == [q]


async def test_mcp_interleave_serves_the_merge_and_owns_each_row(clients, monkeypatch):
    _on(monkeypatch, mode="interleave", search_experiment_holdout_percent=0)
    monkeypatch.setattr(judge_infra, "judge", _fake_judge({"tiingo.daily.prices": 0.9}))
    token = (await clients.post("/users", json={"email": "merge@superdesign.dev"})).json()["token"]
    out, rows = await _search_rows_and_log(clients, "apple stock closing prices for last year", token)
    ids = [r["endpoint_id"] for r in out["results"]]
    assert ids and ids[0] == "treg.stocks.eod.history" and "tiingo.daily.prices" in ids   # grouped: parent first
    assert all("judged" not in r for r in out["results"])          # the probability stays in the log
    (row,) = rows
    assert row.arm == "interleave" and row.shown == [[i, "judged"] for i in ids]
    assert row.baseline_ids == []                                   # nothing to interleave WITH — the judged page fills it


async def test_mcp_an_abstaining_judge_leaves_search_as_it_was(clients, monkeypatch):
    _on(monkeypatch, mode="interleave", search_experiment_holdout_percent=0)

    async def down(query, cands, **kw):
        return judge_infra.Judgement(probs=None, ms=1500, error="timeout")
    monkeypatch.setattr(judge_infra, "judge", down)
    token = (await clients.post("/users", json={"email": "down@superdesign.dev"})).json()["token"]
    out, rows = await _search_rows_and_log(clients, "backlinks", token)
    assert out["results"]                                           # a page, not an error
    (row,) = rows
    assert row.judge_error == "timeout" and row.judged is None and row.differs is False
    assert [r["endpoint_id"] for r in out["results"]] == [i for i, _ in row.shown]
