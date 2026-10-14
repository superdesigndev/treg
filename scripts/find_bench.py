#!/usr/bin/env python3
"""Find benchmark - scores `/catalog/find` against labeled queries, and diffs two runs case by case.

    uv run --frozen python scripts/find_bench.py --cases tests/fixtures/find_bench.yaml --tier recall
    uv run --frozen python scripts/find_bench.py --cases <cases.jsonl> --tier judge --cache <dir> \\
        [--baseline <run.json>] [--out <run.json>]

Two tiers:

  - `recall` calls no API: is a gold unit among the candidates the judge would read, and how many of
    the gold job's vendors are among them.
  - `judge` runs the engine's whole answer, one judge request per query, and scores the verdict:
    accuracy by stratum, false-strong, false-none, top-1 and MRR of the judged rows, tokens, latency,
    and **job coverage**: for a case whose gold is a job sold by two or more vendors, the vendors the
    page shows over the vendors the catalog lists for it (micro = summed, macro = mean per case, full
    = cases showing every vendor). Coverage is the first number: it measures what the page gives a
    person; verdict accuracy measures the verdict.

Judge answers are cached on disk under `--cache` by (model, query, unit ids, questions), so a rerun
of the same recall costs nothing, and a changed question is a new key rather than a stale answer;
the query's vector is cached beside them, so a rerun reads the same recall.
The cache stores the original latency and tokens, so a cached run reports what the live one cost.
`--baseline` names an earlier run file; the report ends with every case that flipped between them.

Cases come from a YAML file (`cases:` list, labels inline; `tests/fixtures/find_bench.yaml`) or a
JSONL file with its labels in `labels.yaml` beside it. A label: `gold` is a regex matched against a
unit's id and its capability id; `expect` lists the acceptable verdicts, `|`-separated; `target`
(`platform:<slug>` or `provider:<name>`) is what a name verdict must name. Every gold must still
match something in the catalog, or the bench refuses to run.

Engines: `v1` is the endpoint-recall find (lexical `store.candidates`, one judge request, the
verdict of `catalog_find.judge`); `v2` the job-first one (`recall_v2`, `answer_v2`), whose recall
tier also counts the vendors its job units reach. `logged` scores the verdict and top rows a JSONL case carries
from the find log, with no calls: the scorer's check against the production record.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import statistics
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml  # noqa: E402

from treg.application import catalog_find, find_index  # noqa: E402
from treg.config import get_settings  # noqa: E402
from treg.domain.catalog import store  # noqa: E402
from treg.infra import embed as embed_infra  # noqa: E402
from treg.infra import judge as judge_infra  # noqa: E402

TASK_VERDICTS = ("strong", "closest")


# ---- cases ---------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Case:
    id: str
    q: str
    stratum: str
    gold: str | None
    expect: tuple[str, ...]
    target: str | None = None
    logged: dict | None = None   # a JSONL case's production record: verdict, top ids


def case_id(q: str) -> str:
    return hashlib.sha256(q.lower().encode()).hexdigest()[:12]


def load_cases(path: Path, labels_path: Path | None = None) -> list[Case]:
    if path.suffix in (".yaml", ".yml"):
        rows = (yaml.safe_load(path.read_text()) or {}).get("cases") or []
        return [Case(id=case_id(r["q"]), q=r["q"], stratum=r["stratum"], gold=r.get("gold"),
                     expect=tuple(r["expect"].split("|")), target=r.get("target")) for r in rows]
    labels = {str(k): v for k, v in yaml.safe_load(
        (labels_path or path.with_name("labels.yaml")).read_text()).items()}
    cases = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        lab = labels.get(r["id"])
        if lab is None:
            sys.exit(f"case {r['id']} has no label")
        cases.append(Case(id=r["id"], q=r["q"], stratum=r["stratum"], gold=lab.get("gold"),
                          expect=tuple(lab["expect"].split("|")), target=lab.get("target"),
                          logged={"verdict": r.get("verdict"), "top": r.get("top") or []}))
    return cases


# ---- the catalog as the bench reads it -----------------------------------------------------------
class View:
    """The catalog's shown endpoints (browsable, no routed parents) and each job's vendors."""

    def __init__(self, cat: store.Catalog):
        self.cat = cat
        self.endpoints = [e for e in cat.endpoints if store.browsable(e) and e.get("kind") != "routed"]
        self.vendors: dict[str, set[str]] = {}
        for e in self.endpoints:
            if e.get("capability"):
                self.vendors.setdefault(e["capability"], set()).add(e["provider"])

    def is_gold(self, unit_id: str, gold: str | None) -> bool:
        if not gold:
            return False
        ep = self.cat.by_id.get(unit_id)
        cap = (ep or {}).get("capability") or (unit_id if unit_id in self.cat.capabilities else "")
        return bool(re.search(gold, unit_id, re.I) or (cap and re.search(gold, cap, re.I)))

    def gold_jobs(self, gold: str | None) -> list[str]:
        return [c for c in self.vendors if gold and re.search(gold, c, re.I)]

    def shown_vendors(self, ids: list[str], cap: str) -> set[str]:
        return {self.cat.by_id[i]["provider"] for i in ids
                if i in self.cat.by_id and self.cat.by_id[i].get("capability") == cap}

    def check_golds(self, cases: list[Case]) -> list[str]:
        ids = list(self.cat.by_id) + list(self.cat.capabilities)
        bad = []
        for c in cases:
            if c.gold and not any(re.search(c.gold, i, re.I) for i in ids):
                bad.append(f"{c.id} gold {c.gold!r} matches nothing in the catalog")
            if c.target:
                kind, _, name = c.target.partition(":")
                known = self.cat.platforms if kind == "platform" else {e["provider"] for e in self.endpoints}
                if name not in known:
                    bad.append(f"{c.id} target {c.target!r} is not in the catalog")
        return bad


# ---- the judge, cached on disk -------------------------------------------------------------------
class DiskJudge:
    """Wraps `infra.judge.judge`: an answer already on disk is served from there with the latency and
    tokens it cost live; an abstention is never stored, so a rerun retries it."""

    def __init__(self, directory: Path | None, live: Callable[..., Awaitable[judge_infra.Judgement]]):
        self.dir = directory
        self.live = live
        self.hits = self.misses = 0
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(query: str, cands: list[dict], kw: dict) -> str:
        parts = [kw.get("model"), query, [c["id"] for c in cands], kw.get("criteria"), kw.get("extra")]
        if kw.get("job_criteria") is not None:   # v2: the job question's wording, and which ids are jobs
            parts += [kw["job_criteria"], ["job" if "job" in c else "endpoint" for c in cands]]
        return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()

    async def __call__(self, query: str, cands: list[dict], **kw) -> judge_infra.Judgement:
        path = self.dir / f"{self.key(query, cands, kw)}.json" if self.dir else None
        if path and path.exists():
            self.hits += 1
            return judge_infra.Judgement(**json.loads(path.read_text()))
        self.misses += 1
        j = await self.live(query, cands, **kw)
        if path and j.probs is not None:
            path.write_text(json.dumps({**asdict(j), "cached": True}))
        return j


# ---- engines -------------------------------------------------------------------------------------
@dataclass
class Answer:
    candidates: list[str]                 # unit ids the judge reads, in recall order
    recall_ms: float = 0.0
    reach: list[str] | None = None        # endpoint ids those units reach (v2: a job's members)
    embed_error: str | None = None        # v2: why the semantic channel was off for this query
    verdict: str = ""
    kept: list[tuple[str, float]] = field(default_factory=list)   # judged rows at or over keep, best first
    shown: list[str] | None = None        # endpoint ids on the page, in order; None = not known
    named: str = ""                       # on a name verdict: platform:<slug> or provider:<name>
    tokens_in: int | None = None
    judge_ms: int | None = None
    error: str | None = None


def _provider_display(service: str) -> str:
    from treg.routers.catalog import _provider_display as display
    return display(service)


class V1:
    """Today's find: `store.candidates` recall, `catalog_find.judge` verdict."""

    def __init__(self, cat: store.Catalog):
        self.cat = cat
        self.n = max(1, int(get_settings().find_candidates))

    async def recall(self, case: Case) -> Answer:
        t0 = time.perf_counter()
        cands = store.candidates(case.q, self.cat, self.n)
        return Answer(candidates=[ep["id"] for ep, _ in cands], recall_ms=(time.perf_counter() - t0) * 1000)

    async def answer(self, case: Case) -> Answer:
        a = await self.recall(case)
        cands = [(self.cat.by_id[i], 0.0) for i in a.candidates]
        judged = await catalog_find.judge(case.q, cands, self.cat, _provider_display)
        j = judged.judgement
        a.verdict = judged.verdict
        a.kept = [(ep["id"], round(p, 3)) for ep, p in (judged.kept or [])]
        a.shown = [ep["id"] for ep, _ in judged.rows]
        if judged.verdict == catalog_find.NAME and judged.rows:
            first = judged.rows[0][0]
            a.named = f"{judged.named}:{first['platform'] if judged.named == 'platform' else first['provider']}"
        a.tokens_in, a.judge_ms, a.error = j.tokens_in, j.ms, j.error
        return a


class V2:
    """Job-first find: `catalog_find.recall_with_meaning` units (the semantic channel when an
    embedding key is set and the card vectors are built), one judge request, `decide` and `expand`
    (no evidence: the rerank orders by core and price)."""

    def __init__(self, cat: store.Catalog):
        self.cat = cat

    async def _recall(self, case: Case):
        r = await catalog_find.recall_with_meaning(case.q, self.cat, _provider_display)
        reach = [c["id"] for c in catalog_find._candidate_endpoints(r.cands, self.cat)]
        return r.cands, Answer(candidates=[c.unit.id for c in r.cands], recall_ms=r.recall_ms, reach=reach,
                               embed_error=r.embed.error)

    async def recall(self, case: Case) -> Answer:
        return (await self._recall(case))[1]

    async def answer(self, case: Case) -> Answer:
        cands, a = await self._recall(case)
        found = await catalog_find.answer_v2(case.q, cands, self.cat, _provider_display)
        j = found.judgement
        a.verdict = found.verdict
        a.kept = [(c.unit.id, round(p, 3)) for c, p in found.kept]
        a.shown = [r["ep"]["id"] for r in found.rows]
        if found.name:
            key = found.name.label if found.name.kind == "product" else found.name.keys[0]
            a.named = f"{found.name.kind}:{key}"
        a.tokens_in, a.judge_ms, a.error = j.tokens_in, j.ms, j.error
        return a


class Logged:
    """The verdict and top rows the find log recorded for a JSONL case; no recall, no calls."""

    def __init__(self, cat: store.Catalog):
        self.cat = cat

    async def recall(self, case: Case) -> Answer:
        sys.exit("the logged engine has no recall; use --tier judge")

    async def answer(self, case: Case) -> Answer:
        if not case.logged:
            sys.exit("the logged engine needs JSONL cases with a logged verdict")
        v = case.logged["verdict"]
        top = case.logged["top"]
        return Answer(candidates=[], verdict="keyword" if v == "abstain" else v,
                      kept=[(i, 1.0) for i in top], shown=None)


ENGINES = {"v1": V1, "v2": V2, "logged": Logged}


# ---- scoring -------------------------------------------------------------------------------------
def score(case: Case, a: Answer, view: View) -> tuple[bool, str]:
    """A verdict is right when it is one `expect` allows AND, for a task verdict, the best judged row
    is gold; for a name verdict, it names the label's target when the label has one."""
    if a.verdict not in case.expect:
        return False, f"verdict {a.verdict} not in {'|'.join(case.expect)}"
    if a.verdict in TASK_VERDICTS and case.gold:
        top = a.kept[0][0] if a.kept else ""
        if not view.is_gold(top, case.gold):
            return False, f"top {top or '-'} not gold"
    if a.verdict == "name" and case.target and a.named and a.named != case.target:
        return False, f"named {a.named} != {case.target}"
    return True, ""


def false_strong(case: Case, a: Answer) -> bool:
    return a.verdict == "strong" and "strong" not in case.expect


def false_none(case: Case, a: Answer) -> bool:
    return a.verdict == "none" and "none" not in case.expect and "strong" in case.expect


def gold_rank(case: Case, a: Answer, view: View) -> int | None:
    return next((r + 1 for r, (i, _) in enumerate(a.kept) if view.is_gold(i, case.gold)), None)


def coverage_job(case: Case, view: View, *shown: list[str] | None) -> str | None:
    """The gold job a coverage figure is about: of the gold jobs any compared page touches, the one
    with the most vendors; else the largest gold job. Shared by both sides of a paired diff."""
    if not case.gold or "strong" not in case.expect:
        return None
    jobs = view.gold_jobs(case.gold)
    if not jobs:
        return None
    touched = [j for j in jobs if any(s and view.shown_vendors(s, j) for s in shown)]
    return max(touched or jobs, key=lambda j: (len(view.vendors[j]), j))


def coverage(rows: list[tuple[Case, list[str]]], view: View, jobs: dict[str, str]) -> dict:
    """Job coverage over cases whose chosen gold job has two or more vendors."""
    per = []
    for case, ids in rows:
        cap = jobs.get(case.id)
        if cap and len(view.vendors[cap]) >= 2:
            per.append((case.id, cap, len(view.shown_vendors(ids, cap)), len(view.vendors[cap])))
    if not per:
        return {"n": 0}
    shown, listed = sum(p[2] for p in per), sum(p[3] for p in per)
    return {"n": len(per), "micro": [shown, listed], "macro": round(statistics.mean(p[2] / p[3] for p in per), 4),
            "full": sum(p[2] == p[3] for p in per), "cases": {p[0]: [p[1], p[2], p[3]] for p in per}}


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def summarize(cases: list[Case], answers: dict[str, Answer], tier: str, view: View,
              baseline: dict | None) -> dict:
    by: dict[str, list[int]] = {}
    out: dict = {"n": len(cases)}
    recall_ms = [answers[c.id].recall_ms for c in cases if answers[c.id].candidates]
    out["recall_ms"] = {"p50": pct(recall_ms, 0.5), "p95": pct(recall_ms, 0.95)}
    jobs = {c.id: j for c in cases
            if (j := coverage_job(c, view, answers[c.id].shown if tier == "judge" else _reach(answers[c.id]),
                                  *(_baseline_ids(baseline, c.id, tier),)))}
    if tier == "recall":
        gold_cases = [c for c in cases if c.gold]
        for c in gold_cases:
            hit = any(view.is_gold(i, c.gold) for i in answers[c.id].candidates)
            by.setdefault(c.stratum, [0, 0])
            by[c.stratum][0] += hit
            by[c.stratum][1] += 1
        out["recall"] = [sum(v[0] for v in by.values()), sum(v[1] for v in by.values())]
        out["by_stratum"] = by
        out["coverage"] = coverage([(c, _reach(answers[c.id])) for c in cases], view, jobs)
        return out
    for c in cases:
        ok, _ = score(c, answers[c.id], view)
        by.setdefault(c.stratum, [0, 0])
        by[c.stratum][0] += ok
        by[c.stratum][1] += 1
    out["correct"] = [sum(v[0] for v in by.values()), len(cases)]
    out["by_stratum"] = by
    out["false_strong"] = sum(false_strong(c, answers[c.id]) for c in cases)
    out["false_none"] = sum(false_none(c, answers[c.id]) for c in cases)
    task = [c for c in cases if c.gold and set(c.expect) & set(TASK_VERDICTS)]
    ranks = [gold_rank(c, answers[c.id], view) for c in task]
    out["top1"] = [sum(r == 1 for r in ranks), len(task)]
    out["mrr"] = round(sum(1 / r for r in ranks if r) / len(task), 4) if task else None
    out.update(judge_cost(answers.values()))
    shown = [(c, answers[c.id].shown) for c in cases if answers[c.id].shown is not None]
    out["coverage"] = coverage(shown, view, jobs) if shown else {"n": 0}
    return out


def _reach(a: Answer) -> list[str]:
    return a.reach if a.reach is not None else a.candidates


def _baseline_ids(baseline: dict | None, cid: str, tier: str) -> list[str] | None:
    row = (baseline or {}).get("cases", {}).get(cid)
    if not row:
        return None
    return row.get("shown") if tier == "judge" else row.get("reach") or row.get("candidates")


# ---- report --------------------------------------------------------------------------------------
def _frac(pair) -> str:
    n, d = pair
    return f"{n}/{d} = {n / d:.0%}" if d else "-"


def report(run: dict, baseline: dict | None) -> None:
    s, meta = run["summary"], run["meta"]
    print(f"\nfind bench  engine={meta['engine']}  tier={meta['tier']}  cases={s['n']}  catalog={meta['catalog']}")
    if meta.get("semantic"):
        print("  semantic channel   " + "  ".join(f"{k}={v}" for k, v in meta["semantic"].items()))
    strata = " ".join(f"{k} {v[0]}/{v[1]}" for k, v in sorted(s["by_stratum"].items()))
    if meta["tier"] == "recall":
        print(f"  recall@candidates  {_frac(s['recall'])}   {strata}")
    else:
        print(f"  verdict accuracy   {_frac(s['correct'])}   {strata}")
        print(f"  false-strong {s['false_strong']}   false-none {s['false_none']}   "
              f"top-1 {_frac(s['top1'])}   MRR {s['mrr']}   abstained {s['abstained']}")
        print(f"  tokens in p50/p95  {s['tokens_in']['p50']}/{s['tokens_in']['p95']}   "
              f"judge ms p50/p95 {s['judge_ms']['p50']}/{s['judge_ms']['p95']}")
    rm = s["recall_ms"]
    if rm["p50"] is not None:
        print(f"  recall ms p50/p95  {rm['p50']:.1f}/{rm['p95']:.1f}")
    cov = s["coverage"]
    if cov.get("n"):
        print(f"  job coverage       micro {_frac(cov['micro'])}   macro {cov['macro']:.0%}   "
              f"full {cov['full']}/{cov['n']}")
    if baseline:
        _diff(run, baseline)


def _diff(run: dict, baseline: dict) -> None:
    b, bs = baseline["cases"], baseline["summary"]
    tier = run["meta"]["tier"]
    print(f"\npaired diff vs {baseline['meta']['engine']} ({baseline['meta'].get('when', '')}):")
    if tier == "judge" and "correct" in bs:
        print(f"  verdict accuracy   {_frac(bs['correct'])} -> {_frac(run['summary']['correct'])}")
    elif "recall" in bs:
        print(f"  recall@candidates  {_frac(bs['recall'])} -> {_frac(run['summary']['recall'])}")
    key = "ok" if tier == "judge" else "hit"
    flips = []
    for cid, row in run["cases"].items():
        old = b.get(cid)
        if old is None or old.get(key) is None or row.get(key) is None or old[key] == row[key]:
            continue
        flips.append(("FIXED" if row[key] else "BROKE", row["stratum"], row["q"],
                      old.get("verdict", ""), row.get("verdict", ""), row.get("why", "")))
    for kind, stratum, q, was, now, why in sorted(flips, key=lambda f: (f[0], f[1], f[2])):
        print(f"  {kind:5} [{stratum}] {q[:58]!r:62} {was:8} -> {now:8} {why}")
    if not flips:
        print("  no case flipped")


# ---- main ----------------------------------------------------------------------------------------
class DirStore:
    """Find's card vectors on local disk (`<cache>/find-vectors/...`), standing in for the object
    store so a bench rerun embeds no card twice."""

    def __init__(self, root: Path):
        self.root = root

    async def get_named(self, name: str) -> bytes | None:
        path = self.root / name
        return path.read_bytes() if path.exists() else None

    async def put_named(self, name: str, body: bytes) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)


async def warm_semantic(cat: store.Catalog, cache: Path | None) -> dict:
    """Build the card vectors before scoring (cached under `<cache>/find-vectors/` when there is a
    cache) and say how the channel stands: off (no key), on (with what was reused or computed),
    or failed."""
    if not find_index.enabled():
        return {"channel": "off"}
    find_index.configure(DirStore(cache) if cache else None)
    vectors = await find_index.prepare(cat, _index(cat))
    return ({"channel": "on", "model": vectors.model, "dim": vectors.dim, "reused": vectors.reused,
             "computed": vectors.computed} if vectors else {"channel": "failed"})


class DiskEmbed:
    """Wraps `infra.embed.embed_query`: a query's vector already on disk (by model, size and folded
    text) is served from there, so a rerun embeds nothing and reads the same recall as the run
    before it, which is what keeps the judge's disk cache hitting."""

    def __init__(self, directory: Path | None, live):
        self.dir = directory
        self.live = live
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    async def __call__(self, text: str, **kw) -> embed_infra.Embedding:
        key = hashlib.sha256(json.dumps([kw.get("model"), kw.get("dim"), embed_infra._fold(text)]).encode()).hexdigest()
        path = self.dir / f"{key}.json" if self.dir else None
        if path and path.exists():
            return embed_infra.Embedding(vectors=[json.loads(path.read_text())], ms=0, cached=True)
        e = await self.live(text, **kw)
        if path and e.vector is not None:
            path.write_text(json.dumps(e.vector))
        return e


@contextmanager
def cached_apis(cache: Path | None):
    """The judge key from the environment, and the judge and the query embedding served from
    disk for the block (`infra.judge.judge` and `infra.embed.embed_query` wrapped, restored after),
    so a rerun costs nothing it already paid for; yields the judge wrapper for its hit and miss
    counts."""
    s = get_settings()
    if not s.typesafe_api_key:
        s.typesafe_api_key = os.environ.get("TYPESAFE_API_KEY", "")
    if not s.typesafe_api_key:
        raise SystemExit("the judge needs TYPESAFE_API_KEY (or TREG_TYPESAFE_API_KEY)")
    disk = DiskJudge(cache / "jev" if cache else None, judge_infra.judge)
    embed = DiskEmbed(cache / "embed" if cache else None, embed_infra.embed_query)
    judge_infra.judge, embed_infra.embed_query = disk, embed
    try:
        yield disk
    finally:
        judge_infra.judge, embed_infra.embed_query = disk.live, embed.live


def judge_cost(answers) -> dict:
    """Tokens and latency over the answers the judge gave live or from disk, and the abstentions."""
    toks = [a.tokens_in for a in answers if a.tokens_in]
    ms = [a.judge_ms for a in answers if a.judge_ms]
    return {"tokens_in": {"p50": pct(toks, 0.5), "p95": pct(toks, 0.95)},
            "judge_ms": {"p50": pct(ms, 0.5), "p95": pct(ms, 0.95)},
            "abstained": sum(1 for a in answers if a.error)}


async def run_all(engine, cases: list[Case], tier: str, concurrency: int,
                  cache: Path | None = None) -> tuple[dict[str, Answer], dict]:
    embed = await warm_semantic(engine.cat, cache) if isinstance(engine, V2) else {"channel": "off"}
    if tier == "recall":
        return {c.id: await engine.recall(c) for c in cases}, embed
    sem = asyncio.Semaphore(concurrency)

    async def one(c: Case) -> tuple[str, Answer]:
        async with sem:
            return c.id, await engine.answer(c)
    return dict(await asyncio.gather(*(one(c) for c in cases))), embed


def _index(cat: store.Catalog):
    from treg.domain.catalog import find_recall
    return find_recall.index(cat)


def bench(cases: list[Case], *, engine: str, tier: str, cache: Path | None = None,
          baseline: dict | None = None, concurrency: int = 6) -> dict:
    """Run one engine over `cases` and return the run (meta, summary, per-case rows)."""
    cat = store.load()
    view = View(cat)
    bad = view.check_golds(cases)
    if bad:
        raise SystemExit("labels no longer match the catalog:\n  " + "\n  ".join(bad))
    s = get_settings()
    disk = None
    if tier == "judge" and engine != "logged":
        with cached_apis(cache) as disk:
            answers, embed = asyncio.run(run_all(ENGINES[engine](cat), cases, tier, concurrency, cache))
    else:
        answers, embed = asyncio.run(run_all(ENGINES[engine](cat), cases, tier, concurrency, cache))
    rows = {}
    for c in cases:
        a = answers[c.id]
        row = {"q": c.q, "stratum": c.stratum, "candidates": a.candidates}
        if a.reach is not None:
            row["reach"] = a.reach
        if a.embed_error:
            row["embed_error"] = a.embed_error
        if tier == "recall":
            row["hit"] = any(view.is_gold(i, c.gold) for i in a.candidates) if c.gold else None
        else:
            ok, why = score(c, a, view)
            row.update(ok=ok, why=why, verdict=a.verdict, kept=a.kept, shown=a.shown, named=a.named,
                       tokens_in=a.tokens_in, judge_ms=a.judge_ms, error=a.error)
        rows[c.id] = row
    meta = {"engine": engine, "tier": tier, "when": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "catalog": f"{len(cat.capabilities)} jobs, {len(view.endpoints)} endpoints",
            "keep": float(s.search_judge_keep), "high": float(s.search_judge_high),
            "find_candidates": int(s.find_candidates), "model": s.typesafe_model}
    if disk:
        meta["judge_cache"] = {"hits": disk.hits, "misses": disk.misses}
    if engine == "v2":
        meta["semantic"] = embed
    return {"meta": meta, "summary": summarize(cases, answers, tier, view, baseline), "cases": rows}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cases", type=Path, required=True, help="YAML (labels inline) or JSONL (labels.yaml beside it)")
    ap.add_argument("--labels", type=Path, help="labels for a JSONL case file (default: labels.yaml beside it)")
    ap.add_argument("--tier", choices=("recall", "judge"), default="recall")
    ap.add_argument("--engine", choices=sorted(ENGINES), default="v1")
    ap.add_argument("--baseline", type=Path, help="an earlier run file to diff against")
    ap.add_argument("--cache", type=Path, help="directory for cached judge answers (and the default run file)")
    ap.add_argument("--out", type=Path, help="where to write this run (default: <cache>/run-<engine>-<tier>.json)")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args(argv)
    baseline = json.loads(args.baseline.read_text()) if args.baseline else None
    if baseline and baseline["meta"]["tier"] != args.tier:
        sys.exit(f"baseline is a {baseline['meta']['tier']} run; this is {args.tier}")
    cases = load_cases(args.cases, args.labels)
    run = bench(cases, engine=args.engine, tier=args.tier, cache=args.cache, baseline=baseline,
                concurrency=args.concurrency)
    report(run, baseline)
    out = args.out or (args.cache / f"run-{args.engine}-{args.tier}.json" if args.cache else None)
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(run, ensure_ascii=False, indent=1))
        print(f"\nrun written to {out}")


if __name__ == "__main__":
    main()
