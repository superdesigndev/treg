"""The agent search bench (scripts/search_agent_bench.py): scores the job-first answer against what
agents called next, beside the pages the log served. Pinned on synthetic cases and a fake judge."""
from __future__ import annotations

import json
from pathlib import Path

from scripts import search_agent_bench as sb
from tests.test_catalog_find import _fake_v2
from treg.config import get_settings
from treg.domain.catalog import store
from treg.infra import judge as judge_infra

JOB = "people.email.find"


def _cases(tmp_path: Path, cat) -> Path:
    members = [e["id"] for e in cat.endpoints if e.get("capability") == JOB and store.browsable(e)]
    other = next(e["id"] for e in cat.endpoints if e.get("capability") == "people.search" and store.browsable(e))
    rows = [
        # the caller called a vendor of the job v2 finds: a job-hit for v2 whichever row it shows first
        {"id": "a", "q": "work email of a person at a company", "arm": "interleave", "baseline_empty": True,
         "baseline_ids": [], "judged": [[members[-1], 0.8]], "called": members[-1], "called_on_shown": True},
        # the caller called something else: a miss for every page
        {"id": "b", "q": "work email finder", "arm": "baseline", "baseline_empty": False,
         "baseline_ids": [other], "judged": [], "called": other, "called_on_shown": True},
        # no call: counts for the verdict distribution only
        {"id": "c", "q": "book a table for two", "arm": "interleave", "baseline_empty": True,
         "baseline_ids": [], "judged": [], "called": None, "called_on_shown": False},
    ]
    p = tmp_path / "agent.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return p


def test_the_bench_scores_job_hits_against_the_logged_pages(tmp_path, monkeypatch):
    cat = store.load()
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "test-key", raising=False)

    async def judge(query, cands, **kw):
        if query.startswith("book"):
            return await _fake_v2({}, plat=("none", 0.9))(query, cands, **kw)
        return await _fake_v2({JOB: 0.9})(query, cands, **kw)
    monkeypatch.setattr(judge_infra, "judge", judge)

    cases = sb.load_cases(_cases(tmp_path, cat))
    run = sb.bench(cases, limit=4, cache=None)
    s = run["summary"]
    assert s["n"] == 3 and s["labeled"] == 2
    assert s["verdicts"] == {"none:gap": 1, "strong": 2}
    assert s["job_hit_all"] == {"v2": [1, 2], "baseline": [1, 2], "judged": [1, 2]}
    assert s["hit_all"]["baseline"] == [1, 2] and s["hit_all"]["judged"] == [1, 2]
    assert s["job_hit_empty"]["v2"] == [1, 1] and s["job_hit_nonempty"]["v2"] == [0, 1]
    assert s["false_none"] == [0, 2] and s["false_none_by_arm"] == {"baseline": [0, 1], "interleave": [0, 1]}
    a = run["cases"]["a"]
    assert a["job_hit"] is True and a["jobs"] == [JOB] and len(a["rows"]) == 4
    assert run["cases"]["c"]["verdict"] == "none" and "job_hit" not in run["cases"]["c"]


def test_a_gap_where_the_caller_called_is_a_false_none(tmp_path, monkeypatch):
    cat = store.load()
    monkeypatch.setattr(get_settings(), "typesafe_api_key", "test-key", raising=False)
    monkeypatch.setattr(judge_infra, "judge", _fake_v2({}, plat=("none", 0.9)))
    run = sb.bench(sb.load_cases(_cases(tmp_path, cat)), limit=8, cache=None)
    assert run["summary"]["false_none"] == [2, 2] and run["summary"]["false_none_by_arm"]["baseline"] == [1, 1]
    assert run["summary"]["job_hit_all"]["v2"] == [0, 2]
