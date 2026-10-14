"""Find tools for a job - a person describes what they want done, the catalog answers with the
endpoints that can do it.

`/catalog/search` is token matching, and a pasted job ("find the emails of CTOs at Series A fintech
startups in Berlin") carries rare words that are parameter VALUES, so its gate admits nothing. This
use case is the discovery experiment's mechanism (`application.search_experiment`) served to people:
the same loose lexical recall (`store.candidates`, one required hit is enough) read by the same
relevance judge (`infra.judge`, TypeSafe's Jev, one Noul per candidate in one request), bucketed at
the same `search_judge_keep` / `search_judge_high` cuts. What differs is the audience: the dashboard's
Catalog page and the public /search page, both anonymous-capable, so the route is rate limited here.

A person also types bare names ("google", "semrush") into the same box. That is not a job, and no
endpoint "accomplishes" it, so the same judge request asks one more question - is `task` only a
name? - and a name is answered with the platform or provider it names, under its own verdict.

Two phases, because the recall is instant and the judge is not: `stream` yields the candidates
first and the judged rows when they arrive, and the pages animate the wait on the first event. The
judge abstains rather than fails (see `infra.judge`); an unambiguous provider name still opens that
provider's tools, and other abstentions fall back to the keyword page, never to an error.

Two engines behind `find_engine` (docs/context/architecture/find.md). v1, above: endpoint recall.
v2: recall by JOB (`domain.catalog.find_recall`), so one judge seat carries every vendor of a job,
the same single request also asks which platform the task needs, nine rules decide the verdict
(`decide`), and a fitting job lists all its vendors (`expand`). `shadow` serves v1 and logs v2.

Session discipline: `admit` opens, commits and closes its own session BEFORE the judge's upstream
call, and the evidence read that orders v2's rows happens after the judge has answered, so no
request holds a database connection while Jev is thinking.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field

from .. import audit, ratestore
from . import find_index
from ..config import get_settings
from ..domain.catalog import find_recall
from ..domain.catalog import store as catalog_store
from ..infra import db as database
from ..infra import judge as judge_infra

log = logging.getLogger("treg.find")

RATE_NS = "catalog_find"
RATE_WINDOW_S = 3600
MAX_QUERY_CHARS = 500

# verdicts: what the page says above the rows
STRONG = "strong"      # at least one row at or over `search_judge_high`
CLOSEST = "closest"    # rows kept, none strong - shown as "closest matches", not as an answer
NONE = "none"          # the judge read every candidate and kept nothing
KEYWORD = "keyword"    # the judge abstained; the rows are the keyword page, unjudged
NAME = "name"          # the query only names a platform or provider; the rows are what it offers, unjudged

MAX_NAME_PLATFORMS = 12
MAX_NAME_ROWS_PER_PLATFORM = 40

# What a fit means, attached to every candidate question. Without it the judge scored a bare name
# ("google") at 0.6+ against every Google endpoint; the `false` side makes a name fit nothing, and
# the NAME question below answers it instead.
FIT_CRITERIA = {
    "true": "The endpoint returns the data or performs the action the task asks for, or performs "
            "one essential step of it.",
    "false": "The endpoint only shares words or a platform with the task, or returns different data "
             "than the task needs. Also false when the task only names a product, company or "
             "platform without saying what to get or do.",
}
NAME_QUESTION = {
    "type": "noul",
    "instructions": "`task` is only the name of a product, company, platform or data source, "
                    "without saying what data to get or what to do.",
    "criteria": {
        "true": "A bare name such as 'google', 'semrush' or 'Google Search Console': the person "
                "wants to see what is available there.",
        "false": "The text names data, a result or an action, even in two words and even alongside "
                 "a platform, such as 'backlinks', 'tiktok ads' or 'verify email'.",
    },
}


def configured() -> bool:
    return bool(get_settings().typesafe_api_key)


def clean_query(q: str) -> str:
    return " ".join((q or "").split())[:MAX_QUERY_CHARS]


async def admit(client_ip: str) -> bool:
    """Per-IP and deployment-wide sliding windows. Each find is one judge call (a fraction of a
    cent), so this bounds abuse, not a bill."""
    s = get_settings()
    async with database.session_maker() as db:
        ok = await ratestore.rate_check(
            db, RATE_NS,
            [(f"ip:{client_ip}", int(s.find_max_per_ip_hour)), ("all", int(s.find_max_per_hour))],
            RATE_WINDOW_S)
        await db.commit()
    return ok


def _row(ep: dict, cat: catalog_store.Catalog, provider_display, p: float | None) -> dict:
    """One kept endpoint, standing alone: its identity, the job and platform it files under, its
    price in the catalog's own shape (the pages format it like every other price), and its fit."""
    return {
        "id": ep["id"],
        "name": ep.get("name") or (ep.get("summary") or "")[:80],
        "provider": ep["provider"],
        "provider_display": provider_display(ep["provider"]),
        **catalog_store.endpoint_context(ep, cat),
        "cost": cat.cost_view(ep.get("cost"), ep.get("provider")),
        "p": None if p is None else round(float(p), 3),
    }


@dataclass
class Judged:
    verdict: str
    rows: list[tuple[dict, float | None]]   # what is shown; probability None when unjudged
    judgement: judge_infra.Judgement
    kept: list[tuple[dict, float]] | None = None   # the judge's rows at or over keep; None = abstained
    named: str = ""   # on NAME: what the name named, "platform" or "provider" (the pages group by it)


def name_rows(query: str, cat: catalog_store.Catalog, provider_display,
              platform: str | None = None) -> tuple[str, list[dict]]:
    """What a bare name offers: the endpoints on the platforms whose name or slug contains it (the
    Catalog box's platform filter); else, when the name is a provider's, that provider's endpoints.
    Platform first, because "tiktok" means the platform, not the one provider that happens to be
    called TikTok. Browse endpoints only, like the platform shelves.

    Ordered so the first lines read as jobs: inside a platform, catalogued jobs before uncatalogued
    endpoints (Tag Manager's raw API surface), the jobs most providers sell first.

    Scoped to one `platform`, a name can only be a provider's: what it offers on that shelf."""
    q = query.strip().lower()
    if not q:
        return "", []
    shown = [e for e in (cat.for_platform(platform) if platform else cat.endpoints)
             if catalog_store.browsable(e) and not catalog_store.paused(e)]
    jobs_first = _jobs_first(shown)
    on: dict[str, list[dict]] = {}
    for e in shown:
        on.setdefault(e["platform"], []).append(e)
    slugs = [] if platform else [slug for slug, plat in cat.platforms.items()
                                 if on.get(slug) and q in f"{plat['label']} {slug}".lower()]
    if slugs:
        # The platform of exactly that name, then those the name starts ("tiktok" -> TikTok Shop)
        # before one that merely mentions it ("Douyin (TikTok China)"); then the Catalog shelves'
        # own featured rank, then the most jobs.
        def rank(slug: str) -> tuple:
            plat = cat.platforms[slug]
            featured = plat.get("featured")
            jobs = len({e["capability"] for e in on[slug] if e["capability"]})
            return (not _is_named(q, slug, plat), not (_short(plat["label"]).startswith(q) or slug.startswith(q)),
                    featured is None, featured or 0, -jobs, slug)
        slugs = sorted(slugs, key=rank)[:MAX_NAME_PLATFORMS]
        return "platform", [e for slug in slugs for e in jobs_first(on[slug])[:MAX_NAME_ROWS_PER_PLATFORM]]
    return "provider", jobs_first([e for e in shown if q in (e["provider"].lower(), provider_display(e["provider"]).lower())])


def _jobs_first(shown: list[dict]) -> Callable[[list[dict]], list[dict]]:
    """A sort for rows of `shown` that reads as jobs: catalogued jobs before uncatalogued endpoints,
    the jobs most providers sell first. Stable: ties keep the catalog's order."""
    sellers = Counter(e["capability"] for e in shown if e["capability"])
    return lambda eps: sorted(eps, key=lambda e: (not e["capability"], -sellers[e["capability"]]))


def _short(label: str) -> str:
    """A platform label without its gloss, lowercased: "Google Analytics (GA4)" -> "google analytics".
    The same cut as the pages' `platShort` (frontend/src/state/catalog.js), so a name matches what
    the shelves show."""
    return find_recall.short_label(label).lower()


def _is_named(q: str, slug: str, plat: dict) -> bool:
    """`q` (lowercased) is exactly this platform's name or slug ("google ads", "tiktok-shop")."""
    q = " ".join(q.split())
    return q in (_short(plat["label"]), slug, slug.replace("-", " "))


def names_a_platform(query: str, cat: catalog_store.Catalog) -> bool:
    return any(_is_named(query.lower(), slug, p) for slug, p in cat.platforms.items())


async def judge(query: str, cands: list[tuple[dict, float]], cat: catalog_store.Catalog,
                provider_display, platform: str | None = None) -> Judged:
    """Judge the recall and decide the verdict. Never raises: an abstaining judge yields the
    keyword page (possibly empty) under the KEYWORD verdict. Rows are best fit first: this page is
    an answer to one job, so unlike the experiment's `interleave.bucketed` it does not keep the
    lexical order inside a bucket. A bare name with no strong fit (the judge reads it as one, or
    it is exactly a platform's name) is answered with what that name offers when the catalog has it."""
    s = get_settings()
    views = [judge_infra.candidate_view(ep, cat.capabilities.get(ep.get("capability") or "", ""))
             for ep, _ in cands]
    j = await judge_infra.judge(query, views, api_key=s.typesafe_api_key, model=s.typesafe_model,
                                url=s.typesafe_url, timeout_s=float(s.find_timeout_s),
                                criteria=FIT_CRITERIA, extra={"name": NAME_QUESTION} if views else None)
    if j.probs is None:
        hit = find_recall.name_of(query, find_recall.index(cat), platform, provider_display)
        if hit and hit.kind == "provider":
            return Judged(NAME, [(ep, None) for ep in name_page(hit, cat, platform)], j,
                          named="provider")
        page, _, _ = catalog_store.rank_band(query, cat, 25, platform)
        return Judged(KEYWORD, [(ep, None) for ep, _ in page[:25]], j)
    keep, high = float(s.search_judge_keep), float(s.search_judge_high)
    scored = sorted(zip((ep for ep, _ in cands), j.probs), key=lambda t: -t[1])
    strong = bool(scored) and scored[0][1] >= high
    kept = [(ep, p) for ep, p in scored if p >= keep]
    if not strong:
        hit = find_recall.name_of(query, find_recall.index(cat), platform, provider_display)
        if hit and hit.kind == "provider":
            return Judged(NAME, [(ep, None) for ep in name_page(hit, cat, platform)], j,
                          kept, "provider")
    if not strong and ((j.extra or {}).get("name", 0.0) >= float(s.find_name_min)
                       or (not platform and names_a_platform(query, cat))):
        named, rows = name_rows(query, cat, provider_display, platform)
        if rows:
            return Judged(NAME, [(ep, None) for ep in rows], j, kept, named)
    return Judged(STRONG if strong else CLOSEST if kept else NONE, kept, j, kept)


async def stream(query: str, provider_display, platform: str | None = None,
                 evidence: Evidence | None = None) -> AsyncIterator[dict]:
    """The two events of one find, in order: `candidates` (the recall, at once) and `judged` (the
    rows and the verdict, when the judge answers). Logged once the answer is out. `high` rides
    along so the pages draw the strong cut from this server's setting, not a copy.

    `find_engine` picks the answer: `v1` (endpoint recall), `v2` (job recall and the nine rules),
    or `shadow` - v1 is served and v2 runs beside it, its judge request in parallel, for the log
    only. `platform` scopes the whole find to one shelf. `evidence` reads the measured success of
    endpoint ids (the evidence rerank's input) once the judge has answered; None = unmeasured."""
    engine = str(get_settings().find_engine).strip().lower()
    if engine == "v2":
        async for event in _stream_v2(query, provider_display, platform, evidence):
            yield event
        return
    shadow = None
    if engine == "shadow":
        shadow = asyncio.create_task(_shadow_v2(query, provider_display, platform, evidence))
    try:
        async for event in _stream_v1(query, provider_display, platform):
            yield event
        if shadow is not None:
            await shadow
    finally:
        if shadow is not None and not shadow.done():
            shadow.cancel()


async def _stream_v1(query: str, provider_display, platform: str | None) -> AsyncIterator[dict]:
    cat = catalog_store.load()
    n = max(1, int(get_settings().find_candidates))
    cands = catalog_store.candidates(query, cat, n, platform)
    yield {"event": "candidates",
           "candidates": [{"id": ep["id"], "platform": ep.get("platform") or "", "provider": ep["provider"]}
                          for ep, _ in cands]}
    judged = await judge(query, cands, cat, provider_display, platform)
    yield {"event": "judged", "verdict": judged.verdict, "named": judged.named, "read": len(cands),
           "high": float(get_settings().search_judge_high),
           "rows": [_row(ep, cat, provider_display, p) for ep, p in judged.rows]}
    _, baseline_total = catalog_store.search(query, cat, 0)
    _log(query, source="web-find", baseline_total=baseline_total, cands=cands, judged=judged)


def _log(query: str, *, source: str, baseline_total: int, cands: list[tuple[dict, float]],
         judged: Judged) -> None:
    """One SearchLog row per find (mode `find`), and a SearchMiss when nothing fit - the same two
    tables the MCP experiment and the keyword route already write, so the misses land in one
    report. Fire-and-forget, like every audit write."""
    j = judged.judgement
    audit.record_search(
        query=query, source=source, org_id=None, user_email=None,
        mode="find", arm="judged", engine="v1",
        baseline_ids=[ep["id"] for ep, _ in cands],
        judged=None if judged.kept is None else [[ep["id"], round(p, 3)] for ep, p in judged.kept],
        shown=[[ep["id"], "judged" if p is not None else "name" if judged.verdict == NAME else "baseline",
                ep.get("capability")] for ep, p in judged.rows],
        baseline_total=int(baseline_total), differs=False,
        judge_ms=j.ms, judge_tokens_in=j.tokens_in, judge_tokens_out=j.tokens_out, judge_error=j.error)
    if judged.verdict == NONE or (judged.verdict == KEYWORD and not judged.rows):
        audit.record_search_miss(query=query, source=source, engine="v1",
                                 reason=JUDGE_OFF if judged.verdict == KEYWORD else None)


# ==== v2: recall by job, one judge request, nine rules ============================================
# Why a `none` has nothing to show: the catalog lacks it (a gap, worth recording), or the text is
# not a task the page can read; `judge_off` is the keyword fallback that found nothing. A shelf's
# find reads that shelf only, so its `none` is `scope`: it cannot say the catalog lacks anything.
GAP = "gap"
NOT_TASK = "not_task"
JUDGE_OFF = "judge_off"
SCOPE = "scope"

JUDGED = (STRONG, CLOSEST, NAME)  # verdicts whose rows are the judge's answer, not the keyword page's

FIT_FROM_JOB = "job"            # the row carries its job's fit
FIT_FROM_ENDPOINT = "endpoint"  # the row was judged on its own words
FOLDED = 5                      # vendors a job under high shows (as `store.MAX_ROUTED_CHILDREN`)
CAPPED_TOP = 0.6                # under a confident "no platform", below this is a gap

# What a fitting JOB means. The `false` side is the endpoint question's: sharing words is not doing
# the job, and a bare name does no job at all (the NAME question answers that).
JOB_CRITERIA = {
    "true": "Tools that do this job return the data or perform the action the task asks for, or one "
            "essential step of it.",
    "false": FIT_CRITERIA["false"],
}

# Measured success per endpoint id, for the rows' order. Must not raise: the route passes
# `_observed_or_empty`, which answers {} when the observations are unavailable.
Evidence = Callable[[list[str]], Awaitable[dict]]


def platform_question(ix: find_recall.Index) -> dict:
    """Which platform the task most needs, or none: a `none` with confidence marks a catalog gap."""
    options = {slug: f"{p.get('label', slug)}: {p.get('summary', '')}" for slug, p in ix.platforms.items()}
    return {
        "type": "choice",
        "instructions": "Which platform or data source does `task` most need? Pick `none` when no "
                        "listed platform provides the data or action, or when `task` is not a task.",
        "criteria": {**options, "none": "nothing listed covers this, or the text is not a task"},
    }


def _unit_view(unit: find_recall.Unit, cat: catalog_store.Catalog) -> dict:
    if unit.kind == find_recall.JOB:
        return judge_infra.job_view(unit.id, cat.capabilities.get(unit.id, ""), unit.platform,
                                    len(unit.providers), list(unit.examples))
    ep = cat.by_id[unit.id]
    return judge_infra.candidate_view(ep, cat.capabilities.get(ep.get("capability") or "", ""))


@dataclass
class Group:
    """One job's rows on a v2 answer (or an endpoint's, on its own): a strong job's every vendor, a
    closest job's first `FOLDED`, a name page's rows of that job; `providers` is the vendors doing
    the job as the judge was told (on a name page, those on it), `hidden` the vendors a fold left
    out of `rows`."""
    capability: str
    p: float | None
    rows: list[dict]                 # {"ep", "p", "fit_from"} in evidence order
    providers: int = 0
    hidden: int = 0


@dataclass
class Found:
    """One v2 answer: the verdict, why a `none` is empty, what a name named, and the rows."""
    verdict: str
    judgement: judge_infra.Judgement
    cands: list[find_recall.Candidate]
    kept: list[tuple[find_recall.Candidate, float]] = field(default_factory=list)
    reason: str = ""
    name: find_recall.NameHit | None = None
    rows: list[dict] = field(default_factory=list)   # {"ep", "p", "fit_from", "children_hidden"}
    groups: list[Group] = field(default_factory=list)   # the same rows by job (strong, closest, name)

    @property
    def platform(self) -> dict | None:
        answer = (self.judgement.extra or {}).get("plat")
        return {"choice": answer["choice"], "confidence": round(answer["confidence"], 3)} \
            if isinstance(answer, dict) else None

    @property
    def name_p(self) -> float | None:
        v = (self.judgement.extra or {}).get("name")
        return float(v) if isinstance(v, (int, float)) else None


def recall_v2(query: str, cat: catalog_store.Catalog, provider_display,
              platform: str | None = None, semantic: list[float] | None = None) -> list[find_recall.Candidate]:
    s = get_settings()
    return find_recall.recall(
        query, find_recall.index(cat), cat.aliases, semantic=semantic, platform=platform,
        n_jobs=int(s.find_jobs), n_delta=int(s.find_delta), n_raw=int(s.find_raw),
        n_plat=int(s.find_platform_seats))


@dataclass
class Recalled:
    cands: list[find_recall.Candidate]
    embed: find_index.Semantic
    recall_ms: float


async def recall_with_meaning(query: str, cat: catalog_store.Catalog, provider_display,
                              platform: str | None = None) -> Recalled:
    """The query's vector (when the card vectors are ready), then both channels. The embedding
    request is the only I/O; without it the recall is the lexical channel alone."""
    sem = await find_index.semantic(query, cat, find_recall.index(cat))
    t0 = time.perf_counter()
    cands = recall_v2(query, cat, provider_display, platform, sem.scores)
    return Recalled(cands, sem, (time.perf_counter() - t0) * 1000)


async def judge_v2(query: str, cands: list[find_recall.Candidate], cat: catalog_store.Catalog,
                   provider_display, platform: str | None = None,
                   timeout_s: float | None = None) -> judge_infra.Judgement:
    """One request: a fit per unit, "is it only a name?", and (off a shelf) which platform. With no
    units the two extra questions are still asked, so an empty recall can still be told apart as a
    catalog gap or not a task. `timeout_s` defaults to find's (`find_timeout_s`); an agent's search
    passes its own budget."""
    s = get_settings()
    extra = {"name": NAME_QUESTION}
    if platform is None:
        extra["plat"] = platform_question(find_recall.index(cat))
    return await judge_infra.judge(
        query, [_unit_view(c.unit, cat) for c in cands], api_key=s.typesafe_api_key,
        model=s.typesafe_model, url=s.typesafe_url,
        timeout_s=float(s.find_timeout_s if timeout_s is None else timeout_s),
        criteria=FIT_CRITERIA, job_criteria=JOB_CRITERIA, extra=extra)


def decide(query: str, cands: list[find_recall.Candidate], j: judge_infra.Judgement,
           ix: find_recall.Index, platform: str | None = None, provider_display=lambda s: s,
           not_task: str = NONE) -> Found:
    """The verdict, the first rule that holds (docs/context/architecture/find.md):

    1. the judge abstained: keyword, with the judge's reason
    2. the query is exactly a name, or a name's prefix the judge reads as a name: name
    3. no strong fit, a short query, a name matches: name (a typed prefix)
    4. no strong fit, the judge reads a name, the name table has none and nothing is kept: none/gap
    5. the judge picks no platform with confidence: none/gap under `CAPPED_TOP`, else closest (never strong)
    6. a fit at or over high: strong
    7. a fit at or over keep: closest
    8. nothing kept and the judge picked a platform with confidence: none/gap (the catalog has the
       platform, not this job on it: a gap worth recording)
    9. otherwise: `not_task` (none for a person; an agent's search passes keyword, since its input
       always means something and the keyword page serves it), reason not_task

    On a shelf (`platform`) any `none` is `scope`: that find read one shelf.
    """
    s = get_settings()
    if j.probs is None:
        hit = find_recall.name_of(query, ix, platform, provider_display)
        if hit and hit.kind == "provider":
            return Found(NAME, j, cands, name=hit)
        return Found(KEYWORD, j, cands, reason=j.error or "")
    keep, high = float(s.search_judge_keep), float(s.search_judge_high)
    scored = sorted(zip(cands, j.probs), key=lambda t: -t[1])
    top = scored[0][1] if scored else 0.0
    kept = [(c, p) for c, p in scored if p >= keep]
    hit = find_recall.name_of(query, ix, platform, provider_display)
    found = Found(NONE, j, cands, kept)
    is_name = (found.name_p or 0.0) >= float(s.find_name_min)
    strong = top >= high
    plat = found.platform
    if hit and (hit.exact or is_name or (not strong and len(find_recall.query_tokens(query)) <= 3)):
        found.verdict, found.name = NAME, hit
    elif is_name and not strong and not kept:
        found.reason = GAP
    elif plat and plat["choice"] == "none" and plat["confidence"] >= float(s.find_gap_min):
        if top < CAPPED_TOP:
            found.reason = GAP
        else:
            found.verdict = CLOSEST
    elif strong:
        found.verdict = STRONG
    elif kept:
        found.verdict = CLOSEST
    elif plat and plat["choice"] != "none" and plat["confidence"] >= float(s.find_gap_min):
        found.reason = GAP        # the platform is in the catalog; this job on it is not
    else:
        found.verdict, found.reason = not_task, NOT_TASK
    if found.verdict in (NONE, KEYWORD):
        found.kept = []
        if platform and found.verdict == NONE:
            found.reason = SCOPE
    return found


def verdict_label(verdict: str, reason: str = "") -> str:
    """The verdict as recorded: the reason after a colon where there is one (`none:gap`)."""
    return f"{verdict}:{reason}" if reason else verdict


def expand_groups(found: Found, cat: catalog_store.Catalog, stats: dict, platform: str | None = None) -> list[Group]:
    """The rows of a strong or closest answer by kept unit, best first. A job at or over high lists
    every vendor in the evidence rerank's order (measured success, then core, then price), all of
    them carrying the job's fit: the pages show a job as one line with its vendor count, so cutting
    vendors here would only hide them. A job under high shows one row for each of its first
    `FOLDED` providers and counts the job's other providers in `hidden`. An endpoint judged on its
    own carries its own fit, also where its job was expanded, and is left out when its own fit is
    under keep."""
    s = get_settings()
    keep, high = float(s.search_judge_keep), float(s.search_judge_high)
    own = {c.unit.id: p for c, p in zip(found.cands, found.judgement.probs or [])
           if c.unit.kind == find_recall.ENDPOINT}
    groups: list[Group] = []
    placed: set[str] = set()
    for c, p in found.kept:
        if c.unit.kind == find_recall.ENDPOINT:
            if c.unit.id not in placed:
                placed.add(c.unit.id)
                groups.append(Group("", p, [{"ep": cat.by_id[c.unit.id], "p": p, "fit_from": FIT_FROM_ENDPOINT}]))
            continue
        members = [cat.by_id[m] for m in c.unit.members if not platform or cat.by_id[m]["platform"] == platform]
        rows: list[dict] = []
        for ep, _ in catalog_store.rerank([(ep, 0.0) for ep in members], stats, cat):
            if ep["id"] in placed:
                continue
            if ep["id"] in own:
                if own[ep["id"]] >= keep:
                    rows.append({"ep": ep, "p": own[ep["id"]], "fit_from": FIT_FROM_ENDPOINT})
            else:
                rows.append({"ep": ep, "p": p, "fit_from": FIT_FROM_JOB})
        hidden = 0
        if p < high:
            # Folded by provider: the first row of each of the first `FOLDED` providers. The count
            # is of providers, the job's own (what the judge was told it has), less those on the page.
            firsts: dict[str, dict] = {}
            for r in rows:
                firsts.setdefault(r["ep"]["provider"], r)
            rows = list(firsts.values())[:FOLDED]
            on_page = {r["ep"]["provider"] for g in groups for r in g.rows if r["ep"]["capability"] == c.unit.id}
            on_page |= {r["ep"]["provider"] for r in rows}
            hidden = len({ep["provider"] for ep in members} - on_page)
        placed.update(r["ep"]["id"] for r in rows)
        groups.append(Group(c.unit.id, p, rows, len(c.unit.providers), hidden))
    return groups


def flatten(groups: list[Group]) -> list[dict]:
    """The groups as one list of rows, a folded job's first row carrying `children_hidden`."""
    rows: list[dict] = []
    for g in groups:
        if g.rows and g.hidden:
            g.rows[0]["children_hidden"] = g.hidden
        rows.extend(g.rows)
    return rows


def expand(found: Found, cat: catalog_store.Catalog, stats: dict, platform: str | None = None) -> list[dict]:
    return flatten(expand_groups(found, cat, stats, platform))


def name_groups(rows: list[dict]) -> list[Group]:
    """A name page's rows by job, in the page's order (a job across platforms is one group); an
    uncatalogued endpoint is its own. `providers` counts the vendors of the job on the page."""
    by: dict[str, Group] = {}
    for r in rows:
        cap = r["ep"].get("capability") or ""
        key = cap or r["ep"]["id"]
        g = by.get(key)
        if g is None:
            g = by[key] = Group(cap, None, [])
        g.rows.append(r)
    for g in by.values():
        g.providers = len({r["ep"]["provider"] for r in g.rows})
    return list(by.values())


def name_page(hit: find_recall.NameHit, cat: catalog_store.Catalog, platform: str | None = None) -> list[dict]:
    """What a name offers, jobs first: a platform's endpoints (the platforms the name matches, in
    `name_of`'s order, `MAX_NAME_PLATFORMS` at most, each cut at `MAX_NAME_ROWS_PER_PLATFORM`); a
    provider's (on the shelf, when scoped); or every endpoint whose name carries the product, by
    platform."""
    shown = [e for e in find_recall.shown_endpoints(cat) if not platform or e["platform"] == platform]
    jobs_first = _jobs_first(shown)
    if hit.kind == "platform":
        on: dict[str, list[dict]] = {}
        for e in shown:
            on.setdefault(e["platform"], []).append(e)
        return [e for slug in hit.keys[:MAX_NAME_PLATFORMS]
                for e in jobs_first(on.get(slug, []))[:MAX_NAME_ROWS_PER_PLATFORM]]
    if hit.kind == "provider":
        return jobs_first([e for e in shown if e["provider"] == hit.keys[0]])
    ids = set(hit.keys)
    return sorted(jobs_first([e for e in shown if e["id"] in ids]), key=lambda e: e["platform"])


async def judge_and_decide(query: str, cands: list[find_recall.Candidate], cat: catalog_store.Catalog,
                           provider_display, platform: str | None = None, timeout_s: float | None = None,
                           not_task: str = NONE) -> Found:
    """The verdict, with no rows yet: one judge request, then `decide`. Never raises."""
    j = await judge_v2(query, cands, cat, provider_display, platform, timeout_s)
    return decide(query, cands, j, find_recall.index(cat), platform, provider_display, not_task)


async def lay_out(found: Found, query: str, cat: catalog_store.Catalog, platform: str | None = None,
                  evidence: Evidence | None = None, keyword_rows: bool = True) -> Found:
    """The rows of a decided answer (and, for a judged one, the same rows by job in `groups`). A
    keyword verdict's rows are the keyword page, read here only for a consumer that shows it
    (`keyword_rows`; an agent's search already holds its own lexical page)."""
    if found.verdict == KEYWORD:
        if keyword_rows:
            page, _, _ = catalog_store.rank_band(query, cat, 25, platform)
            found.rows = [{"ep": ep, "p": None} for ep, _ in page[:25]]
    elif found.verdict == NAME and found.name:
        found.rows = [{"ep": ep, "p": None} for ep in name_page(found.name, cat, platform)]
        found.groups = name_groups(found.rows)
    elif found.verdict in (STRONG, CLOSEST):
        ids = sorted({m for c, _ in found.kept for m in c.unit.members})
        stats = await evidence(ids) if evidence is not None and ids else {}
        found.groups = expand_groups(found, cat, stats, platform)
        found.rows = flatten(found.groups)
    return found


async def answer_v2(query: str, cands: list[find_recall.Candidate], cat: catalog_store.Catalog,
                    provider_display, platform: str | None = None,
                    evidence: Evidence | None = None, timeout_s: float | None = None,
                    not_task: str = NONE, keyword_rows: bool = True) -> Found:
    """Judge the units, decide, and lay out the rows. Never raises."""
    found = await judge_and_decide(query, cands, cat, provider_display, platform, timeout_s, not_task)
    return await lay_out(found, query, cat, platform, evidence, keyword_rows)


def _candidate_endpoints(cands: list[find_recall.Candidate], cat: catalog_store.Catalog) -> list[dict]:
    """Every endpoint the recall reaches, units expanded to their members: the pages light the
    platforms and vendors being read."""
    out: dict[str, dict] = {}
    for c in cands:
        for eid in (c.unit.members or (c.unit.id,)):
            ep = cat.by_id[eid]
            out.setdefault(eid, {"id": eid, "platform": ep.get("platform") or "", "provider": ep["provider"]})
    return list(out.values())


def _v2_row(r: dict, cat: catalog_store.Catalog, provider_display) -> dict:
    row = _row(r["ep"], cat, provider_display, r["p"])
    if r.get("fit_from"):
        row["fit_from"] = r["fit_from"]
    if r.get("children_hidden"):
        row["children_hidden"] = r["children_hidden"]
    return row


async def _stream_v2(query: str, provider_display, platform: str | None,
                     evidence: Evidence | None, served: bool = True) -> AsyncIterator[dict]:
    cat = catalog_store.load()

    def event(cands: list[find_recall.Candidate], reached: list[dict]) -> dict:
        return {"event": "candidates", "candidates": reached,
                "units": [{"kind": c.unit.kind, "id": c.unit.id} for c in cands]}
    # The lexical recall is instant, so it is the first event; the query's vector and the fused
    # recall follow, as a second `candidates` event when the meaning changed what is read.
    t0 = time.perf_counter()
    lexical = recall_v2(query, cat, provider_display, platform)
    lexical_ms = (time.perf_counter() - t0) * 1000
    reached = _candidate_endpoints(lexical, cat)
    yield event(lexical, reached)
    sem = await find_index.semantic(query, cat, find_recall.index(cat))
    r = Recalled(lexical, sem, lexical_ms)
    if sem.scores is not None:
        t0 = time.perf_counter()
        r = Recalled(recall_v2(query, cat, provider_display, platform, sem.scores), sem,
                     (time.perf_counter() - t0) * 1000)
        if [c.unit.id for c in r.cands] != [c.unit.id for c in lexical]:
            reached = _candidate_endpoints(r.cands, cat)
            yield event(r.cands, reached)
    found = await answer_v2(query, r.cands, cat, provider_display, platform, evidence)
    yield {"event": "judged", "verdict": found.verdict, "named": found.name.kind if found.name else "",
           "read": len(r.cands), "high": float(get_settings().search_judge_high),
           "rows": [_v2_row(row, cat, provider_display) for row in found.rows],
           "reason": found.reason, "platform": found.platform, "engine": "v2",
           "embed": {"ms": r.embed.ms, "error": r.embed.error}}
    _log_v2(query, cat, found, r, reached, served)


async def _shadow_v2(query: str, provider_display, platform: str | None, evidence: Evidence | None) -> None:
    """v2 beside a served v1 answer, for the log only: the v2 stream, its events unread. Never raises."""
    try:
        async for _ in _stream_v2(query, provider_display, platform, evidence, served=False):
            pass
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the shadow is a measurement; it never touches the answer
        log.warning("find shadow failed", exc_info=True)


def row_owner(verdict: str) -> str:
    """Who put a v2 answer's rows on the page, for `SearchLog.shown`."""
    return "name" if verdict == NAME else "judged" if verdict in JUDGED else "baseline"


def shown_rows(rows: list[dict], owner: str) -> list[list]:
    """`SearchLog.shown`: each served row as [endpoint id, owner, job], the job so the report can
    credit a call to any vendor of a job the page showed."""
    return [[ep["id"], "hub" if ep.get("kind") == "hub" else owner, ep.get("capability")] for ep in rows]


def log_fields_v2(found: Found, r: Recalled) -> dict:
    """What a v2 answer records about itself (`SearchLog`, docs/context/architecture/find.md), the
    same for a find and for an agent's search: the judge's readings, the recall's, the verdict with
    its reason after a colon, and every unit read as [kind, id, p]."""
    j = found.judgement
    probs = j.probs or [None] * len(found.cands)
    plat = found.platform or {}
    return dict(
        engine="v2",
        judged=None if j.probs is None else [[c.unit.id, round(p, 3)] for c, p in found.kept],
        judge_ms=j.ms, judge_tokens_in=j.tokens_in, judge_tokens_out=j.tokens_out, judge_error=j.error,
        platform_choice=plat.get("choice"), platform_conf=plat.get("confidence"),
        name_p=None if found.name_p is None else round(found.name_p, 3), recall_ms=round(r.recall_ms),
        embed_ms=r.embed.ms, embed_error=r.embed.error,
        verdict=verdict_label(found.verdict, found.reason),
        units=[[c.unit.kind, c.unit.id, None if p is None else round(p, 3)] for c, p in zip(found.cands, probs)])


def _log_v2(query: str, cat: catalog_store.Catalog, found: Found, r: Recalled, reached: list[dict],
            served: bool = True) -> None:
    """The v2 SearchLog row, and - when v2's answer is the one served - its SearchMiss: a shadow
    answer never files a miss beside the served engine's, so each find files at most one."""
    _, baseline_total = catalog_store.search(query, cat, 0)
    audit.record_search(
        query=query, source="web-find", org_id=None, user_email=None,
        mode="find", arm="judged",
        baseline_ids=[c["id"] for c in reached],
        shown=shown_rows([row["ep"] for row in found.rows], row_owner(found.verdict)),
        baseline_total=int(baseline_total), differs=False,
        **log_fields_v2(found, r))
    if served and (found.verdict == NONE or (found.verdict == KEYWORD and not found.rows)):
        audit.record_search_miss(query=query, source="web-find", engine="v2",
                                 reason=miss_reason(found.verdict, found.reason))


def miss_reason(verdict: str, reason: str) -> str | None:
    """Why a v2 answer's empty page was empty, for `SearchMiss`: a none's reason; `not_task` where
    the keyword page served in its place and was empty too; `judge_off` where the judge did not
    answer (a timeout, an error, a cap) and the keyword page was empty."""
    if verdict == KEYWORD:
        return NOT_TASK if reason == NOT_TASK else JUDGE_OFF
    return reason or None
