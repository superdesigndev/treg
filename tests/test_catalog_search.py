"""Catalog search for agents (application.catalog_search) in `v2` mode: the job-first answer laid
out for an agent's page, the verdicts an agent sees (and the one it does not: "not a task" serves
the lexical page), the holdouts, the per-caller judge cap, and what each search records.
"""
from __future__ import annotations

from sqlmodel import select

from tests.test_catalog_find import _abstain, _fake_v2, _on
from treg import audit
from treg import mcp as _mcp
from treg.application import catalog_find as find
from treg.application import catalog_search as cs
from treg.application import search_experiment as se
from treg.config import get_settings
from treg.domain.catalog import find_recall as fr
from treg.domain.catalog import store as catalog_store
from treg.infra import judge as judge_infra
from treg.infra.db import session_maker
from treg.models import SearchLog, SearchMiss

JOB = "people.email.find"            # 24 vendors, a routed row
JOB2 = "people.email.verify"         # 13 vendors, a routed row
PLAIN = "web.crawl"                  # vendors, no routed row


def _v2(monkeypatch, **over):
    _on(monkeypatch, search_experiment="v2", **over)


async def _search(q, limit=8, surface=None):
    return await _mcp._catalog_search_impl(q, limit, ctx=None, surface=surface or _mcp._TEAM_SURFACE)


async def _rows():
    await audit.drain()
    async with session_maker() as s:
        logs = (await s.execute(select(SearchLog).order_by(SearchLog.id))).scalars().all()
        misses = (await s.execute(select(SearchMiss).order_by(SearchMiss.id))).scalars().all()
    return logs, misses


# ---------------------------------------------------------------- the page, as a pure function

def _found(verdict, groups, reason=""):
    """An answer laid out by `expand_groups` (or `name_groups`): its groups and their flattening."""
    f = find.Found(verdict, judge_infra.Judgement(probs=[], ms=1, extra={}), [], [], reason)
    f.groups, f.rows = groups, find.flatten(groups)
    return f


def _job(cat, cap, p, own=None):
    """What `expand_groups` lays out for a job at `p`: every member in catalog order, a member rated
    on its own carrying its own fit."""
    unit = fr.index(cat).units[fr.index(cat).job_pos[cap]]
    rows = [{"ep": cat.by_id[m], "p": (own or {}).get(m, p),
             "fit_from": find.FIT_FROM_ENDPOINT if m in (own or {}) else find.FIT_FROM_JOB} for m in unit.members]
    return find.Group(cap, p, rows, len(unit.providers)), unit


def test_a_page_is_dealt_round_robin_across_jobs_and_laid_out_job_by_job():
    cat = catalog_store.load()
    (g1, u1), (g2, u2) = _job(cat, JOB, 0.9), _job(cat, JOB2, 0.8)
    found = _found(find.STRONG, [g1, g2])
    rows, total, jobs, hidden = cs.agent_page(found, cat, 8, hub=[], steer=True)
    # 4 + 4: a routed parent leads each job, then its members in `expand`'s order
    caps = [r["ep"].get("capability") for r in rows]
    assert caps == [JOB] * 4 + [JOB2] * 4
    assert rows[0]["ep"]["id"] == f"treg.{JOB}" and rows[4]["ep"]["id"] == f"treg.{JOB2}"
    assert [r["ep"]["id"] for r in rows[1:4]] == [r["ep"]["id"] for r in g1.rows[:3]]
    assert total == len(g1.rows) + len(g2.rows) + 2
    assert [(j.capability, j.providers, j.shown) for j in jobs] == [(JOB, len(u1.providers), 3), (JOB2, len(u2.providers), 3)]
    # the vendors the page left out, on each job's first row; a routed parent is not a vendor
    assert hidden[f"treg.{JOB}"] == len(set(u1.providers) - {r["ep"]["provider"] for r in rows[1:4]})
    assert hidden[f"treg.{JOB2}"] == len(set(u2.providers) - {r["ep"]["provider"] for r in rows[5:8]})
    # with routed discovery off, no parent and one more member per job
    rows_off, _, _, hidden_off = cs.agent_page(found, cat, 8, hub=[], steer=False)
    assert all(r["ep"].get("kind") != "routed" for r in rows_off) and len(rows_off) == 8
    assert hidden_off[rows_off[0]["ep"]["id"]] == len(set(u1.providers) - {r["ep"]["provider"] for r in rows_off[:4]})


def test_a_page_holds_a_few_jobs_so_the_best_one_still_shows_vendors():
    """Six kept jobs on a page of eight would give each one row, the best job only its routed
    parent; the page keeps the best four and the rest wait."""
    cat = catalog_store.load()
    ix = fr.index(cat)
    caps = [c for c, i in ix.job_pos.items() if len(ix.units[i].providers) >= 3][:6]
    groups = [_job(cat, cap, 0.9 - n * 0.02)[0] for n, cap in enumerate(caps)]
    page, total, jobs, _ = cs.agent_page(_found(find.STRONG, groups), cat, 8, hub=[], steer=False)
    assert [j.capability for j in jobs] == caps[:4] and [j.shown for j in jobs] == [2, 2, 2, 2]
    assert len(page) == 8 and total == sum(len(g.rows) for g in groups)
    assert cs.page_groups(8) == 4 and cs.page_groups(25) == 12 and cs.page_groups(1) == 1 and cs.page_groups(3) == 1


def test_a_member_the_judge_rated_on_its_own_leads_its_job():
    """The vendor whose own words matched the query's qualifier sits in the evidence order where
    the page cut would lose it; at or over high it leads the job instead."""
    cat = catalog_store.load()
    unit = fr.index(cat).units[fr.index(cat).job_pos[JOB]]
    last = unit.members[-1]
    g, _ = _job(cat, JOB, 0.75, own={last: 0.9})
    page, *_ = cs.agent_page(_found(find.CLOSEST, [g]), cat, 3, hub=[], steer=False)
    assert [r["ep"]["id"] for r in page][0] == last and page[0]["p"] == 0.9
    # under high it keeps its evidence-order place
    g, _ = _job(cat, JOB, 0.75, own={last: 0.5})
    page, *_ = cs.agent_page(_found(find.CLOSEST, [g]), cat, 3, hub=[], steer=False)
    assert [r["ep"]["id"] for r in page] == [r["ep"]["id"] for r in g.rows[:3]]


def test_a_listed_hub_tool_joins_its_job_without_a_lexical_gate_and_never_a_none_page():
    cat = catalog_store.load()
    g, unit = _job(cat, PLAIN, 0.9)
    hub_ep = {"id": "hub-abc", "kind": "hub", "provider": "maker-team", "capability": PLAIN, "name": "My crawler"}
    other = {"id": "hub-xyz", "kind": "hub", "provider": "maker-team", "capability": "people.search", "name": "Other"}
    page, total, jobs, hidden = cs.agent_page(_found(find.STRONG, [g]), cat, 25,
                                              hub=[(hub_ep, 3.0), (other, 3.0)], steer=True)
    ids = [r["ep"]["id"] for r in page]
    assert ids[-1] == "hub-abc" and "hub-xyz" not in ids and total == len(g.rows) + 1
    assert all(r["ep"].get("kind") != "routed" for r in page)        # no routed row for this job
    assert jobs[0].shown == len(g.rows) + 1 and not hidden
    assert cs.serve(_found(find.NONE, [], reason=find.GAP), cat, 8, hub=[(hub_ep, 3.0)], steer=True) == ([], 0, [], {})
    assert cs.serve(find.Found(find.KEYWORD, judge_infra.Judgement(probs=None, ms=1), []), cat, 8, hub=[], steer=True) is None


# ---------------------------------------------------------------- the use case, through the MCP tool

async def test_a_strong_answer_lists_the_job_by_vendor_with_a_verdict(clients, monkeypatch):
    _v2(monkeypatch)
    seen = []
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({JOB: 0.92}, seen))
    out = await _search("find the work email of a hotel manager in Cape Town", limit=8)
    assert out["verdict"] == "strong" and "reason" not in out
    assert out["jobs"] == [{"capability": JOB, "providers": 24, "shown": 7}]
    rows = out["results"]
    assert len(rows) == 8 and rows[0]["endpoint_id"] == f"treg.{JOB}" and "below" not in rows[0]["routed"]
    assert all(r["job"] == JOB and r["score"] is None for r in rows)
    assert rows[0]["more_providers"] == 24 - len({r["provider"] for r in rows[1:]})
    assert "more_providers" not in rows[1]
    assert out["total_matches"] == 33 and out["hint"].startswith(f"{JOB} does this; catalog_get('treg.{JOB}')")
    assert out["next"].startswith("catalog_get")
    (_, _, kw), = seen
    assert kw["timeout_s"] == get_settings().typesafe_timeout_s        # the agent's budget, not find's

    logs, misses = await _rows()
    (row,) = logs
    assert (row.mode, row.arm, row.engine, row.verdict, row.source) == ("v2", "v2", "v2", "strong", "mcp")
    assert row.judged == [[JOB, 0.92]] and all(owner == "judged" for _, owner, _ in row.shown)
    assert all(job == JOB for _, _, job in row.shown)                     # the job, for the report's credit
    assert [eid for eid, _, _ in row.shown] == [r["endpoint_id"] for r in rows]
    assert row.baseline_total == 0 and row.baseline_ids == []            # the lexical gate admitted nothing
    assert misses == []                                                   # a found answer is not a miss


async def test_a_gap_is_an_empty_page_that_says_so(clients, monkeypatch):
    _v2(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("none", 0.9)))
    out = await _search("book a table for two tonight")
    assert out["count"] == 0 and out["verdict"] == "none" and out["reason"] == "gap" and out["jobs"] == []
    assert "catalog_request" in out["hint"] and "near" not in out and "next" not in out
    logs, misses = await _rows()
    assert logs[0].verdict == "none:gap" and logs[0].shown == []
    assert [(m.reason, m.engine, m.source) for m in misses] == [("gap", "v2", "mcp")]


async def test_a_gap_on_a_platform_the_catalog_has_names_the_platform(clients, monkeypatch):
    """The judge names the platform with confidence and keeps no job: the catalog has Threads, not
    posting to it. An empty page that says so, recorded as a gap, not the lexical page of Threads
    search rows the words would match."""
    _v2(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("threads", 0.92)))
    out = await _search("publish post to Threads", limit=5)
    assert out["count"] == 0 and out["verdict"] == "none" and out["reason"] == "gap"
    assert out["hint"].startswith("the catalog has Threads but no tool for 'publish post to Threads'")
    logs, misses = await _rows()
    assert logs[0].verdict == "none:gap" and logs[0].platform_choice == "threads"
    assert [(m.reason, m.engine) for m in misses] == [("gap", "v2")]


async def test_not_a_task_serves_the_lexical_page_under_keyword(clients, monkeypatch):
    """An agent's input always means something; rule 8 was settled on people's queries."""
    _v2(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("seo", 0.3)))
    out = await _search("backlinks", limit=5)
    assert out["verdict"] == "keyword" and out["count"] == 5 and out["jobs"] == []
    assert all(r["score"] is not None and "job" not in r for r in out["results"])
    assert out["hint"] == "ranked by keywords only"
    logs, misses = await _rows()
    assert logs[0].verdict == "keyword:not_task" and all(o == "baseline" for _, o, _ in logs[0].shown)
    assert misses == []
    # ...and an empty lexical page under it is still a miss, with the reason
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("seo", 0.3)))
    out = await _search("zzzz-no-such-capability")
    assert out["count"] == 0 and out["verdict"] == "keyword" and "catalog_request" in out["hint"]
    _, misses = await _rows()
    assert [(m.reason, m.engine) for m in misses] == [("not_task", "v2")]


async def test_an_abstaining_judge_serves_the_lexical_page(clients, monkeypatch):
    _v2(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _abstain)
    out = await _search("backlinks", limit=3)
    assert out["verdict"] == "keyword" and out["count"] == 3 and "judge did not answer" in out["hint"]
    logs, _ = await _rows()
    assert logs[0].judge_error == "timeout" and logs[0].verdict == "keyword:timeout" and logs[0].arm == "v2"


async def test_a_name_answers_with_what_it_offers(clients, monkeypatch):
    _v2(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, name=0.95, plat=("none", 0.2)))
    out = await _search("hunter", limit=6)
    assert out["verdict"] == "name" and out["count"] == 6
    assert all(r["provider"] == "hunter" and r["score"] is None for r in out["results"])
    assert len(out["jobs"]) >= 2 and all(j["providers"] == 1 for j in out["jobs"]) and "search by the job" in out["hint"]
    logs, _ = await _rows()
    assert logs[0].verdict == "name" and all(o == "name" for _, o, _ in logs[0].shown)


async def test_the_holdouts_keep_their_pages_and_the_lexical_one_still_records_v2(clients, monkeypatch):
    _v2(monkeypatch)
    calls = []

    async def judge(query, cands, **kw):
        calls.append(sorted(kw.get("extra") or {}))
        return await _fake_v2({JOB: 0.92}, plat=("people", 0.9))(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", judge)

    monkeypatch.setattr(se, "arm_for", lambda key: se.ARM_BASELINE)
    out = await _search("work email", limit=5)
    assert "verdict" not in out and all(r["score"] is not None for r in out["results"])
    assert calls == [["name", "plat"]]                                  # v2 ran, for the record only
    logs, _ = await _rows()
    assert (logs[0].mode, logs[0].arm, logs[0].engine, logs[0].verdict) == ("v2", "baseline", "v2", "strong")
    assert all(o == "baseline" for _, o, _ in logs[0].shown) and logs[0].judged == [[JOB, 0.92]]

    monkeypatch.setattr(se, "arm_for", lambda key: se.ARM_JUDGED)
    out = await _search("work email", limit=5)
    assert "verdict" not in out and calls[-1] == []                     # the v1 judge: no extra questions
    logs, _ = await _rows()
    assert (logs[-1].mode, logs[-1].arm, logs[-1].engine) == ("v2", "judged", None)


async def test_arms_are_dealt_by_identity_where_it_resolves():
    k = se.identity_key(7, "Agent@Example.com")
    assert k == se.identity_key(7, "agent@example.com ") and k is not None
    assert se.identity_key(None, "a@b") is None and se.identity_key(7, None) is None
    assert se.caller_key("tok") and se.caller_key("") is None


async def test_a_caller_past_the_judge_cap_gets_the_lexical_page(clients, monkeypatch):
    _v2(monkeypatch, search_judge_max_per_caller_hour=1)
    judged = []

    async def judge(query, cands, **kw):
        judged.append(query)
        return await _fake_v2({JOB: 0.92})(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", judge)

    async def identity(ctx):
        return "caller-1", 7, "agent@example.com"
    monkeypatch.setattr(_mcp, "_search_identity", identity)
    first = await _search("work email", limit=3)
    second = await _search("work email again", limit=3)
    assert first["verdict"] == "strong" and second["verdict"] == "keyword" and judged == ["work email"]
    assert second["hint"] == "ranked by keywords only (the judge did not answer)"
    logs, _ = await _rows()
    assert (logs[1].verdict, logs[1].judge_error, logs[1].org_id, logs[1].user_email) == \
        ("keyword:rate_limited", "rate_limited", 7, "agent@example.com")


async def test_the_cap_guards_every_arm_before_any_judge(clients, monkeypatch):
    """A holdout caller runs the judge too (the counterfactual, the v1 page), so the cap must
    bound it as well: past it, no judge at all and the lexical page, whichever the arm."""
    _v2(monkeypatch, search_judge_max_per_caller_hour=1)
    judged = []

    async def judge(query, cands, **kw):
        judged.append(query)
        return await _fake_v2({JOB: 0.92})(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", judge)

    async def identity(ctx):
        return "caller-2", 8, "holdout@example.com"
    monkeypatch.setattr(_mcp, "_search_identity", identity)
    monkeypatch.setattr(se, "arm_for", lambda key: se.ARM_BASELINE)
    await _search("work email", limit=3)
    out = await _search("work email again", limit=3)
    assert judged == ["work email"] and "verdict" not in out      # the holdout's page, unjudged either way
    logs, _ = await _rows()
    assert [(r.arm, r.verdict, r.judge_error) for r in logs] == [("baseline", "strong", None),
                                                                  ("baseline", "keyword:rate_limited", "rate_limited")]


async def test_the_directory_surface_answers_the_same_way(clients, monkeypatch):
    _v2(monkeypatch)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({JOB: 0.92}))
    out = await _search("work email", limit=4, surface=_mcp._DIRECTORY_SURFACE)
    assert out["verdict"] == "strong" and out["jobs"][0]["capability"] == JOB
    assert "catalog_call_read" in out["next"]
    logs, _ = await _rows()
    assert logs[0].source == "claude-connector" and logs[0].verdict == "strong"


async def test_off_and_v1_modes_are_untouched(clients, monkeypatch):
    _on(monkeypatch, search_experiment="off")
    out = await _search("backlinks", limit=3)
    assert "verdict" not in out and "jobs" not in out and all(r["score"] is not None for r in out["results"])
    assert (await _rows()) == ([], [])

    # interleave (the rollback): every arm, the baseline holdout included, is the v1 experiment's;
    # the v2 engine never runs and nothing is recorded as mode v2
    _on(monkeypatch, search_experiment="interleave")
    calls = []

    async def judge(query, cands, **kw):
        calls.append(sorted(kw.get("extra") or {}))
        return await _fake_v2({JOB: 0.92})(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", judge)
    for arm in (se.ARM_BASELINE, se.ARM_JUDGED, se.ARM_INTERLEAVE):
        monkeypatch.setattr(se, "arm_for", lambda key, arm=arm: arm)
        out = await _search("work email", limit=3)
        assert "verdict" not in out
    assert calls == [[], [], []]                                     # the v1 judge, no v2 extras
    logs, _ = await _rows()
    assert [(r.mode, r.arm, r.engine, r.verdict) for r in logs] == [
        ("interleave", "baseline", None, None), ("interleave", "judged", None, None), ("interleave", "interleave", None, None)]


async def test_unresolved_callers_share_one_cap_bucket(clients, monkeypatch):
    """The tool reads the catalog without validating a per-team token, so a made-up token reaches
    the judge; rotating one per search must not buy a fresh cap each time."""
    _v2(monkeypatch, search_judge_max_per_caller_hour=1)
    judged = []

    async def judge(query, cands, **kw):
        judged.append(query)
        return await _fake_v2({JOB: 0.92})(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", judge)
    tokens = iter(["made-up-1", "made-up-2"])

    async def identity(ctx):
        return se.caller_key(next(tokens)), None, None                 # a key per token, no identity
    monkeypatch.setattr(_mcp, "_search_identity", identity)
    monkeypatch.setattr(se, "arm_for", lambda key: se.ARM_V2)
    first = await _search("work email", limit=3)
    second = await _search("work email again", limit=3)
    assert first["verdict"] == "strong" and second["verdict"] == "keyword" and judged == ["work email"]


async def test_a_failed_v2_leaves_a_holdout_page_as_it_was(clients, monkeypatch):
    _v2(monkeypatch)

    async def boom(*a, **kw):
        raise RuntimeError("embedding exploded")
    monkeypatch.setattr(find, "recall_with_meaning", boom)
    monkeypatch.setattr(se, "arm_for", lambda key: se.ARM_BASELINE)
    out = await _search("backlinks", limit=3)
    assert "verdict" not in out and out["count"] == 3
    monkeypatch.setattr(se, "arm_for", lambda key: se.ARM_V2)
    out = await _search("backlinks", limit=3)
    assert out["verdict"] == "keyword" and out["count"] == 3
    logs, _ = await _rows()
    assert [(r.arm, r.judge_error, r.verdict) for r in logs] == [("baseline", "error", "keyword:error"), ("v2", "error", "keyword:error")]


def test_the_hint_names_a_row_of_the_job_it_speaks_of():
    """With no routed row, the hint sends the agent to catalog_get on a row of the job; the page
    can lead with an endpoint the judge kept on its own, which is not one."""
    cat = catalog_store.load()
    g, unit = _job(cat, PLAIN, 0.9)
    other = next(e for e in cat.endpoints if catalog_store.browsable(e) and e.get("capability") not in (PLAIN, None))
    page = cs.Page(rows=[(other, 0.0), (g.rows[0]["ep"], 0.0)], total=2, tie_truncated=False, stats={}, hidden={},
                   steering=True, verdict=find.STRONG, jobs=[cs.Job(PLAIN, g.providers, 1)])
    hint = _mcp._verdict_hint(page, cat)
    assert f"catalog_get('{g.rows[0]['ep']['id']}')" in hint and other["id"] not in hint
