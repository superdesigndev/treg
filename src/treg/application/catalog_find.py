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
judge abstains rather than fails (see `infra.judge`); an abstaining judge falls back to the keyword
page, labelled as such, never to an error.

Session discipline: `admit` opens, commits and closes its own session BEFORE the judge's upstream
call, so no request holds a database connection while Jev is thinking.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from .. import audit, ratestore
from ..config import get_settings
from ..domain.catalog import store as catalog_store
from ..infra import db as database
from ..infra import judge as judge_infra

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


def name_rows(query: str, cat: catalog_store.Catalog, provider_display) -> tuple[str, list[dict]]:
    """What a bare name offers: the endpoints on the platforms whose name or slug contains it (the
    Catalog box's platform filter); else, when the name is a provider's, that provider's endpoints.
    Platform first, because "tiktok" means the platform, not the one provider that happens to be
    called TikTok. Browse endpoints only, like the platform shelves.

    Ordered so the first lines read as jobs: inside a platform, catalogued jobs before uncatalogued
    endpoints (Tag Manager's raw API surface), the jobs most providers sell first."""
    q = query.strip().lower()
    if not q:
        return "", []
    shown = [e for e in cat.endpoints if catalog_store.browsable(e)]
    sellers: dict[str, int] = {}
    for e in shown:
        if e["capability"]:
            sellers[e["capability"]] = sellers.get(e["capability"], 0) + 1

    def jobs_first(eps: list[dict]) -> list[dict]:   # stable: ties keep the catalog's order
        return sorted(eps, key=lambda e: (not e["capability"], -sellers.get(e["capability"], 0)))

    on: dict[str, list[dict]] = {}
    for e in shown:
        on.setdefault(e["platform"], []).append(e)
    slugs = [slug for slug, plat in cat.platforms.items()
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


def _short(label: str) -> str:
    """A platform label without its gloss, lowercased: "Google Analytics (GA4)" -> "google analytics".
    The same cut as the pages' `platShort` (frontend/src/state/catalog.js), so a name matches what
    the shelves show."""
    return label.split(" — ")[0].split(" (")[0].strip().lower()


def _is_named(q: str, slug: str, plat: dict) -> bool:
    """`q` (lowercased) is exactly this platform's name or slug ("google ads", "tiktok-shop")."""
    q = " ".join(q.split())
    return q in (_short(plat["label"]), slug, slug.replace("-", " "))


def names_a_platform(query: str, cat: catalog_store.Catalog) -> bool:
    return any(_is_named(query.lower(), slug, p) for slug, p in cat.platforms.items())


async def judge(query: str, cands: list[tuple[dict, float]], cat: catalog_store.Catalog,
                provider_display) -> Judged:
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
                                criteria=FIT_CRITERIA, extra={"name": NAME_QUESTION})
    if j.probs is None:
        page, _, _ = catalog_store.rank_band(query, cat, 25)
        return Judged(KEYWORD, [(ep, None) for ep, _ in page], j)
    keep, high = float(s.search_judge_keep), float(s.search_judge_high)
    scored = sorted(zip((ep for ep, _ in cands), j.probs), key=lambda t: -t[1])
    strong = bool(scored) and scored[0][1] >= high
    kept = [(ep, p) for ep, p in scored if p >= keep]
    if not strong and ((j.extra or {}).get("name", 0.0) >= float(s.find_name_min) or names_a_platform(query, cat)):
        named, rows = name_rows(query, cat, provider_display)
        if rows:
            return Judged(NAME, [(ep, None) for ep in rows], j, kept, named)
    return Judged(STRONG if strong else CLOSEST if kept else NONE, kept, j, kept)


async def stream(query: str, provider_display) -> AsyncIterator[dict]:
    """The two events of one find, in order: `candidates` (the lexical recall, at once) and `judged`
    (the kept rows and the verdict, when the judge answers). Logged once the answer is out.
    `high` rides along so the pages draw the strong cut from this server's setting, not a copy."""
    cat = catalog_store.load()
    cands = catalog_store.candidates(query, cat, max(1, int(get_settings().find_candidates)))
    yield {"event": "candidates",
           "candidates": [{"id": ep["id"], "platform": ep.get("platform") or "", "provider": ep["provider"]}
                          for ep, _ in cands]}
    judged = await judge(query, cands, cat, provider_display)
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
        mode="find", arm="judged",
        baseline_ids=[ep["id"] for ep, _ in cands],
        judged=None if judged.kept is None else [[ep["id"], round(p, 3)] for ep, p in judged.kept],
        shown=[[ep["id"], "judged" if p is not None else "name" if judged.verdict == NAME else "baseline"]
               for ep, p in judged.rows],
        baseline_total=int(baseline_total), differs=False,
        judge_ms=j.ms, judge_tokens_in=j.tokens_in, judge_tokens_out=j.tokens_out, judge_error=j.error)
    if judged.verdict == NONE or (judged.verdict == KEYWORD and not judged.rows):
        audit.record_search_miss(query=query, source=source)
