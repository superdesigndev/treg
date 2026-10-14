#!/usr/bin/env python3
"""Agent search bench - scores the job-first answer (the `v2` mode of MCP `catalog_search`) against
searches agents made and what they called next, beside the pages the log served them.

    uv run --frozen python scripts/search_agent_bench.py --cases <agent.jsonl> --cache <dir> \\
        [--limit 8] [--out <run.json>] [--baseline <run.json>]

A case is one JSON object per line, extracted from the search log outside this repository:

    id, q                      the search
    arm                        baseline | judged | interleave: the page the caller was served
    baseline_empty             the lexical gate admitted nothing
    baseline_ids               the lexical page, endpoint ids in order
    judged                     the v1 judged page, [[endpoint id, p], ...] in order
    called                     the endpoint the same caller went on to call, or null
    called_on_shown            that endpoint was on the page served

The label is behaviour: `called`. It can only name what the caller was shown (the lexical and the
v1 judged page), so it favours those two; the numbers here are a lower bound for v2, and the online
holdout decides. The score for a labeled case:

  - job-hit: the called endpoint does a job the page shows (its capability is among the page's rows'),
    or is on the page itself. The main number: a v2 page lists a job's vendors by measured success
    and sends the agent to the rest through catalog_get, so the vendor it shows first is not the point.
  - hit@limit: the called endpoint is among the page's first `limit` rows.
  - false-none: v2 answered a gap (an empty page) where the caller did call something. Read by arm:
    on the `baseline` arm the caller never saw a judged page, so that is the counterfactual the
    online report reads as well.

Every case counts toward the verdict distribution, the keyword fallbacks (not a task, abstained),
tokens and latency. The judge's answers and the queries' vectors are cached on disk and the card
vectors under `<cache>/find-vectors/`, as the find bench does (`find_bench.cached_apis`, `warm_semantic`).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.find_bench import _frac, _provider_display, cached_apis, judge_cost, warm_semantic  # noqa: E402
from treg.application import catalog_find, catalog_search  # noqa: E402
from treg.config import get_settings  # noqa: E402
from treg.domain.catalog import store  # noqa: E402

PAGES = ("v2", "baseline", "judged")


@dataclass(frozen=True)
class Case:
    id: str
    q: str
    arm: str
    baseline_empty: bool
    baseline_ids: tuple[str, ...]
    judged: tuple[str, ...]
    called: str | None
    called_on_shown: bool


@dataclass
class Answer:
    verdict: str
    reason: str
    rows: list[str]                      # the page served, endpoint ids in order
    jobs: list[str]                      # the jobs on it
    tokens_in: int | None = None
    judge_ms: int | None = None
    error: str | None = None
    embed_error: str | None = None
    kept: list[tuple[str, float]] = field(default_factory=list)


def load_cases(path: Path) -> list[Case]:
    out = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        out.append(Case(id=str(r["id"]), q=r["q"], arm=r.get("arm") or "?", baseline_empty=bool(r.get("baseline_empty")),
                        baseline_ids=tuple(r.get("baseline_ids") or ()),
                        judged=tuple(i for i, *_ in (r.get("judged") or ())),
                        called=r.get("called") or None, called_on_shown=bool(r.get("called_on_shown"))))
    return out


async def answer(case: Case, cat: store.Catalog, limit: int) -> Answer:
    """One search as `v2` mode serves it: recall by job and meaning, the judge under the agent's
    budget, the page `catalog_search.serve` lays out (no hub rows, routed discovery on, no
    evidence), the lexical page under a keyword verdict."""
    r = await catalog_find.recall_with_meaning(case.q, cat, _provider_display)
    found = await catalog_find.answer_v2(case.q, r.cands, cat, _provider_display,
                                         timeout_s=float(get_settings().typesafe_timeout_s),
                                         not_task=catalog_find.KEYWORD, keyword_rows=False)
    served = catalog_search.serve(found, cat, limit, hub=[], steer=True)
    if served is None:
        rows, jobs = list(case.baseline_ids[:limit]), []
    else:
        rows, jobs = [row["ep"]["id"] for row in served[0]], [j.capability for j in served[2]]
    j = found.judgement
    return Answer(found.verdict, found.reason, rows, jobs, tokens_in=j.tokens_in, judge_ms=j.ms, error=j.error,
                  embed_error=r.embed.error, kept=[(c.unit.id, round(p, 3)) for c, p in found.kept])


def jobs_of(ids: list[str] | tuple[str, ...], cat: store.Catalog) -> set[str]:
    return {cap for i in ids if (cap := (cat.by_id.get(i) or {}).get("capability"))}


def job_hit(called: str, rows: list[str] | tuple[str, ...], cat: store.Catalog) -> bool:
    cap = (cat.by_id.get(called) or {}).get("capability")
    return called in rows or bool(cap and cap in jobs_of(rows, cat))


def summarize(cases: list[Case], answers: dict[str, Answer], cat: store.Catalog, limit: int) -> dict:
    out: dict = {"n": len(cases), "limit": limit}
    out["verdicts"] = dict(sorted(Counter(catalog_find.verdict_label(a.verdict, a.reason) for a in answers.values()).items()))
    labeled = [c for c in cases if c.called and c.called in cat.by_id]
    out["labeled"] = len(labeled)
    pages = {"v2": lambda c: answers[c.id].rows, "baseline": lambda c: c.baseline_ids[:limit],
             "judged": lambda c: c.judged[:limit]}
    for stratum, subset in (("all", labeled), ("empty", [c for c in labeled if c.baseline_empty]),
                            ("nonempty", [c for c in labeled if not c.baseline_empty])):
        if not subset:
            continue
        out[f"job_hit_{stratum}"] = {p: [sum(job_hit(c.called, rows(c), cat) for c in subset), len(subset)]
                                     for p, rows in pages.items()}
        out[f"hit_{stratum}"] = {p: [sum(c.called in rows(c)[:limit] for c in subset), len(subset)]
                                 for p, rows in pages.items()}
    none = Counter(c.arm for c in labeled if answers[c.id].verdict == catalog_find.NONE)
    by_arm = Counter(c.arm for c in labeled)
    out["false_none"] = [sum(none.values()), len(labeled)]
    out["false_none_by_arm"] = {arm: [none[arm], n] for arm, n in sorted(by_arm.items())}
    out.update(judge_cost(answers.values()))
    out["embed_off"] = sum(1 for a in answers.values() if a.embed_error)
    return out


def report(run: dict, baseline: dict | None) -> None:
    s, meta = run["summary"], run["meta"]
    print(f"\nagent search bench  cases={s['n']}  labeled={s['labeled']}  limit={s['limit']}  catalog={meta['catalog']}")
    if meta.get("semantic"):
        print("  semantic channel   " + "  ".join(f"{k}={v}" for k, v in meta["semantic"].items()))
    print("  verdicts           " + "  ".join(f"{k} {v}" for k, v in s["verdicts"].items()))
    for stratum in ("all", "empty", "nonempty"):
        jh = s.get(f"job_hit_{stratum}")
        if not jh:
            continue
        h = s[f"hit_{stratum}"]
        print(f"  {stratum:<9}job-hit  " + "  ".join(f"{p} {_frac(jh[p])}" for p in PAGES))
        print(f"  {stratum:<9}hit@{s['limit']:<4} " + "  ".join(f"{p} {_frac(h[p])}" for p in PAGES))
    print(f"  false-none         {_frac(s['false_none'])}   by arm "
          + "  ".join(f"{k} {v[0]}/{v[1]}" for k, v in s["false_none_by_arm"].items()))
    print(f"  tokens in p50/p95  {s['tokens_in']['p50']}/{s['tokens_in']['p95']}   "
          f"judge ms p50/p95 {s['judge_ms']['p50']}/{s['judge_ms']['p95']}   "
          f"abstained {s['abstained']}   embed off {s['embed_off']}")
    if baseline:
        _diff(run, baseline)


def _diff(run: dict, baseline: dict) -> None:
    print(f"\npaired diff vs {baseline['meta']['when']}:")
    flips = []
    for cid, row in run["cases"].items():
        old = baseline["cases"].get(cid)
        if old is None:
            continue
        if (old["verdict"], old.get("job_hit")) != (row["verdict"], row.get("job_hit")):
            flips.append((cid, row, old))
    for cid, row, old in sorted(flips, key=lambda t: t[1]["q"]):
        what = ("FIXED" if row.get("job_hit") and not old.get("job_hit") else
                "BROKE" if old.get("job_hit") and not row.get("job_hit") else "MOVED")
        print(f"  {what} {row['q'][:60]!r:<64} {old['verdict']:<9} -> {row['verdict']}")
    if not flips:
        print("  (no case changed)")


async def run_all(cases: list[Case], cat: store.Catalog, limit: int, concurrency: int, cache: Path | None):
    embed = await warm_semantic(cat, cache)
    sem = asyncio.Semaphore(concurrency)

    async def one(c: Case) -> tuple[str, Answer]:
        async with sem:
            return c.id, await answer(c, cat, limit)
    return dict(await asyncio.gather(*(one(c) for c in cases))), embed


def bench(cases: list[Case], *, limit: int = 8, cache: Path | None = None, concurrency: int = 6) -> dict:
    cat = store.load()
    s = get_settings()
    with cached_apis(cache) as disk:
        answers, embed = asyncio.run(run_all(cases, cat, limit, concurrency, cache))
    rows = {}
    for c in cases:
        a = answers[c.id]
        row = {"q": c.q, "arm": c.arm, "baseline_empty": c.baseline_empty, "called": c.called, **asdict(a)}
        if c.called and c.called in cat.by_id:
            row["job_hit"] = job_hit(c.called, a.rows, cat)
            row["hit"] = c.called in a.rows[:limit]
        rows[c.id] = row
    meta = {"tier": "agent", "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "catalog": f"{len(cat.capabilities)} jobs, {len([e for e in cat.endpoints if store.browsable(e)])} endpoints",
            "keep": float(s.search_judge_keep), "high": float(s.search_judge_high), "model": s.typesafe_model,
            "judge_cache": {"hits": disk.hits, "misses": disk.misses}, "semantic": embed}
    return {"meta": meta, "summary": summarize(cases, answers, cat, limit), "cases": rows}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", type=Path, required=True, help="JSONL of agent searches with what they called")
    ap.add_argument("--limit", type=int, default=8, help="the page size scored (MCP's default is 8)")
    ap.add_argument("--baseline", type=Path, help="an earlier run file to diff against")
    ap.add_argument("--cache", type=Path, help="directory for cached judge answers and card vectors")
    ap.add_argument("--out", type=Path, help="where to write this run (default: <cache>/run-agent.json)")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args(argv)
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    run = bench(load_cases(args.cases), limit=args.limit, cache=args.cache, concurrency=args.concurrency)
    report(run, baseline)
    out = args.out or (args.cache / "run-agent.json" if args.cache else None)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(run, ensure_ascii=False, indent=1))
        print(f"\nrun written to {out}")


if __name__ == "__main__":
    main()
