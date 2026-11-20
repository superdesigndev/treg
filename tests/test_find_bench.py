"""The find bench (scripts/find_bench.py): CI runs its recall tier on the synthetic fixture, so a
catalog change that strands a label, or a recall change that loses most gold jobs, fails here. The
scorer, the disk cache and the paired diff are pinned on hand-made answers; no judge is called."""
from __future__ import annotations

from pathlib import Path

import pytest

from scripts import find_bench as fb
from treg.domain.catalog import store
from treg.infra import judge as judge_infra

FIXTURE = Path(__file__).parent / "fixtures" / "find_bench.yaml"


def test_recall_tier_on_the_fixture():
    cases = fb.load_cases(FIXTURE)
    assert len(cases) >= 25 and len({c.id for c in cases}) == len(cases)
    run = fb.bench(cases, engine="v1", tier="recall")
    hit, total = run["summary"]["recall"]
    assert total >= 15
    assert hit / total >= 0.8, [r["q"] for r in run["cases"].values() if r.get("hit") is False]
    assert run["summary"]["coverage"]["n"] > 0


def test_a_stranded_label_refuses_to_run():
    stale = fb.Case(id="x", q="anything", stratum="strong", gold=r"^no\.such\.job$", expect=("strong",))
    with pytest.raises(SystemExit, match="no longer match"):
        fb.bench([stale], engine="v1", tier="recall")


def _case(**kw):
    base = dict(id="c", q="q", stratum="strong", gold=r"^people\.email\.find$", expect=("strong",))
    return fb.Case(**{**base, **kw})


def test_score_needs_the_verdict_and_a_gold_top_row():
    view = fb.View(store.load())
    gold_ep = next(e["id"] for e in view.endpoints if e.get("capability") == "people.email.find")
    other = next(e["id"] for e in view.endpoints if e.get("capability") == "people.search")
    assert fb.score(_case(), fb.Answer([], verdict="strong", kept=[(gold_ep, 0.9)]), view) == (True, "")
    ok, why = fb.score(_case(), fb.Answer([], verdict="strong", kept=[(other, 0.9), (gold_ep, 0.8)]), view)
    assert not ok and "not gold" in why
    assert not fb.score(_case(), fb.Answer([], verdict="closest", kept=[(gold_ep, 0.5)]), view)[0]
    named = _case(gold=None, expect=("name",), target="platform:reddit")
    assert fb.score(named, fb.Answer([], verdict="name", named="platform:reddit"), view)[0]
    assert not fb.score(named, fb.Answer([], verdict="name", named="platform:tiktok"), view)[0]
    assert fb.false_none(_case(expect=("strong", "closest")), fb.Answer([], verdict="none"))
    assert fb.false_strong(_case(expect=("closest", "none")), fb.Answer([], verdict="strong"))


def test_coverage_counts_the_gold_jobs_vendors_on_the_page():
    view = fb.View(store.load())
    cap = "people.email.find"
    eps = [e for e in view.endpoints if e.get("capability") == cap]
    one_per_vendor = list({e["provider"]: e["id"] for e in eps}.values())
    case = _case()
    job = fb.coverage_job(case, view, one_per_vendor[:2])
    assert job == cap
    cov = fb.coverage([(case, one_per_vendor[:2])], view, {case.id: job})
    assert cov["micro"] == [2, len(view.vendors[cap])] and cov["full"] == 0
    assert fb.coverage([(case, one_per_vendor)], view, {case.id: job})["full"] == 1


async def test_disk_judge_serves_a_stored_answer_and_never_stores_an_abstention(tmp_path):
    calls = []

    async def live(query, cands, **kw):
        calls.append(query)
        if query == "down":
            return judge_infra.Judgement(probs=None, ms=2500, error="timeout")
        return judge_infra.Judgement(probs=[0.8], ms=640, tokens_in=900, extra={"name": 0.1})

    disk = fb.DiskJudge(tmp_path, live)
    cands = [{"id": "a.b"}]
    first = await disk("q", cands, model="m", criteria={"true": "x"})
    again = await disk("q", cands, model="m", criteria={"true": "x"})
    assert calls == ["q"] and again.probs == first.probs and again.ms == 640 and again.cached
    await disk("q", cands, model="m", criteria={"true": "changed"})   # a new question is a new key
    await disk("q", cands, model="m", criteria={"true": "changed"}, job_criteria={"true": "a job"})
    await disk("down", cands, model="m")
    await disk("down", cands, model="m")
    assert calls == ["q", "q", "q", "down", "down"] and (disk.hits, disk.misses) == (1, 5)


def test_paired_diff_lists_each_flip(capsys):
    def run(ok):
        return {"meta": {"engine": "v1", "tier": "judge", "when": "t"},
                "summary": {"correct": [int(ok), 1]},
                "cases": {"c": {"q": "q", "stratum": "strong", "ok": ok, "verdict": "strong" if ok else "none",
                                "why": ""}}}
    fb._diff(run(True), run(False))
    assert "FIXED [strong]" in capsys.readouterr().out
    fb._diff(run(True), run(True))
    assert "no case flipped" in capsys.readouterr().out
