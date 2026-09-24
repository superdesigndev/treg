"""Find tools for a job - a person describes what they want done, the catalog answers with the
endpoints that can do it.

`/catalog/search` is token matching, and a pasted job ("find the emails of CTOs at Series A fintech
startups in Berlin") carries rare words that are parameter VALUES, so its gate admits nothing. This
use case is the discovery experiment's mechanism (`application.search_experiment`) served to people:
the same loose lexical recall (`store.candidates`, one required hit is enough) read by the same
relevance judge (`infra.judge`, TypeSafe's Jev, one Noul per candidate in one request), bucketed at
the same `search_judge_keep` / `search_judge_high` cuts. What differs is the audience: the dashboard's
Catalog page and the public /search page, both anonymous-capable, so the route is rate limited here.

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
    rows: list[tuple[dict, float | None]]   # (endpoint, probability); probability None on KEYWORD
    judgement: judge_infra.Judgement


async def judge(query: str, cands: list[tuple[dict, float]], cat: catalog_store.Catalog) -> Judged:
    """Judge the recall and decide the verdict. Never raises: an abstaining judge yields the
    keyword page (possibly empty) under the KEYWORD verdict. Rows are best fit first: this page is
    an answer to one job, so unlike the experiment's `interleave.bucketed` it does not keep the
    lexical order inside a bucket."""
    s = get_settings()
    views = [judge_infra.candidate_view(ep, cat.capabilities.get(ep.get("capability") or "", ""))
             for ep, _ in cands]
    j = await judge_infra.judge(query, views, api_key=s.typesafe_api_key, model=s.typesafe_model,
                                url=s.typesafe_url, timeout_s=float(s.find_timeout_s))
    if j.probs is None:
        page, _, _ = catalog_store.rank_band(query, cat, 25)
        return Judged(KEYWORD, [(ep, None) for ep, _ in page], j)
    keep, high = float(s.search_judge_keep), float(s.search_judge_high)
    scored = sorted(zip((ep for ep, _ in cands), j.probs), key=lambda t: -t[1])
    kept = [(ep, p) for ep, p in scored if p >= keep]
    verdict = STRONG if scored and scored[0][1] >= high else CLOSEST if kept else NONE
    return Judged(verdict, kept, j)


async def stream(query: str, provider_display) -> AsyncIterator[dict]:
    """The two events of one find, in order: `candidates` (the lexical recall, at once) and `judged`
    (the kept rows and the verdict, when the judge answers). Logged once the answer is out.
    `high` rides along so the pages draw the strong cut from this server's setting, not a copy."""
    cat = catalog_store.load()
    cands = catalog_store.candidates(query, cat, max(1, int(get_settings().find_candidates)))
    yield {"event": "candidates",
           "candidates": [{"id": ep["id"], "platform": ep.get("platform") or ""} for ep, _ in cands]}
    judged = await judge(query, cands, cat)
    yield {"event": "judged", "verdict": judged.verdict, "read": len(cands),
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
        judged=[[ep["id"], round(p, 3)] for ep, p in judged.rows] if j.probs is not None else None,
        shown=[[ep["id"], "judged" if p is not None else "baseline"] for ep, p in judged.rows],
        baseline_total=int(baseline_total), differs=False,
        judge_ms=j.ms, judge_tokens_in=j.tokens_in, judge_tokens_out=j.tokens_out, judge_error=j.error)
    if judged.verdict == NONE or (judged.verdict == KEYWORD and not judged.rows):
        audit.record_search_miss(query=query, source=source)
