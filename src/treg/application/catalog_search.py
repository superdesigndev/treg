"""Catalog search for agents - the use case behind MCP `catalog_search` on both surfaces.

The lexical page is the shipped ranker: `store.rank_band` over a band wider than the page (so a
routed group can collapse without starving it), the evidence rerank, the listed hub tools merged by
score with no boost (docs/hub-listing-decisions.md), routed parents grouped with their children, cut
to the page. Nothing here touches `store.search` or its scoring; the MCP layer only shapes the rows
this returns.

What the caller sees is the discovery experiment's say (`search_experiment`, one setting):

- `off`: the lexical page, nothing recorded.
- `shadow` / `interleave`: the v1 judged page beside it, served by arm (`search_experiment.run`).
- `v2`: the job-first answer, the engine behind /catalog/find (`catalog_find`: recall by job and
  by meaning, one judge request, nine rules, every vendor of a fitting job), laid out for an agent
  by `agent_page`. Two holdouts keep the pure lexical page and the pure v1 judged page, so v2 is
  read against what it replaces. An agent's input always means something, so the judge's "not a
  task" serves the lexical page under verdict `keyword` (`decide`'s `not_task`); a `none` for a
  catalog gap is served as an empty page that says so, which is the answer that stops an agent
  re-querying. The holdout that sees the lexical page still has v2 computed and recorded, so v2's
  verdicts can be read against what that caller did with the lexical page (the counterfactual on
  a false `none`).

Session discipline: the hub read and the per-caller judge cap each open, commit and close their own
session before any judge request, so no connection is held while the judge thinks.
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .. import audit, ratestore
from ..config import get_settings
from ..domain.catalog import store as catalog_store
from ..infra.db import session_maker
from . import catalog_find as find
from . import hub as hub_app
from . import search_experiment

log = logging.getLogger("treg.search")

Rows = list[tuple[dict, float]]
Observed = Callable[[list[str]], Awaitable[dict[str, dict]]]
# (caller key, team id, email) for the experiment's log - resolved only while the experiment is on
Identify = Callable[[], Awaitable[tuple[str | None, int | None, str | None]]]

RATE_NS = "catalog_search"
RATE_WINDOW_S = 3600
RATE_LIMITED = "rate_limited"
UNRESOLVED = "unresolved"       # the cap's one bucket for every caller whose identity did not resolve


@dataclass(frozen=True)
class Caller:
    """Who is searching, as far as the hub's lists need to know: the team slug and sign-in email
    that decide which listed hub tools this caller may see (None, None: an unknown reader, no hub)."""
    hub_slug: str | None = None
    hub_email: str | None = None


@dataclass
class Job:
    """A job on the page: its id, how many vendors do it, how many rows of it the page shows."""
    capability: str
    providers: int
    shown: int


@dataclass
class Page:
    rows: Rows                                 # the page to serve, in order
    total: int                                 # matches before the page cut (hub rows included)
    tie_truncated: bool                        # the tie group outran what the evidence sort weighs
    stats: dict[str, dict]                     # observed stats for every row on the page
    # a group's first row -> what the page left out of the group: child rows of a lexical routed
    # group (`group_routed`), vendors of a judged job (`agent_page`)
    hidden: dict[str, int]
    steering: bool                             # routed discovery on: parents lead their groups
    lexical_empty: bool = False                # the shipped ranker admitted nothing (the miss log's signal)
    arm: str | None = None                     # the experiment's arm, when it ran
    caller_key: str | None = None              # the experiment's handle on the caller, when it ran
    log: dict = field(default_factory=dict)    # the experiment's SearchLog fields, when it ran
    verdict: str | None = None                 # v2's verdict when v2 answered (keyword: the lexical page under it)
    reason: str = ""                           # why a none is empty, or a keyword page served
    platform: str | None = None                # the platform the judge read the task as needing, when it did
    jobs: list[Job] = field(default_factory=list)

    @property
    def judged(self) -> bool:
        """The rows are v2's answer, not the shipped ranker's."""
        return self.verdict in find.JUDGED


def _group(rows: Rows, limit: int) -> tuple[Rows, dict[str, int]]:
    """A capability with a ROUTED row shows the parent first and its children right under it, so
    an agent sees "let treg choose" before the specific providers; the parent counts the children
    the page did not show."""
    grouped = catalog_store.group_routed(
        [{"ep": ep, "score": score, "capability": ep.get("capability"), "kind": ep.get("kind")} for ep, score in rows],
        max_children=catalog_store.MAX_ROUTED_CHILDREN)
    hidden = {r["ep"]["id"]: r["children_hidden"] for r in grouped if r.get("children_hidden")}
    return [(r["ep"], r["score"]) for r in grouped][:limit], hidden


async def _hub_rows(query: str, cat: catalog_store.Catalog, caller: Caller) -> tuple[Rows, dict[str, dict]]:
    async with session_maker() as db:
        return await hub_app.search_listed(db, query, cat, org_slug=caller.hub_slug, email=caller.hub_email)


async def lexical(query: str, cat: catalog_store.Catalog, limit: int, *, observed: Observed,
                  hub: tuple[Rows, dict[str, dict]]) -> Page:
    """The shipped ranker's page. Score, then let the evidence break the ties: token scoring
    produces ties by the dozen, and with a page of 8 the rows an agent sees would otherwise be
    decided by file order. The band is widened only so routed groups can collapse without
    starving the page; with steering off there is no collapsing, so the page is the band."""
    steer = catalog_store.routed_discovery_on()
    ranked, total, tie_truncated = catalog_store.rank_band(query, cat, min(100, limit * 4) if steer else limit)
    stats = await observed([ep["id"] for ep, _ in ranked])
    ranked = catalog_store.rerank(ranked, stats, cat)
    hub_ranked, hub_stats = hub
    if hub_ranked:
        stats = {**stats, **hub_stats}
        ranked = catalog_store.merge_by_score(ranked, hub_ranked)
        total += len(hub_ranked)
    rows, hidden = _group(ranked, limit)
    return Page(rows, total, tie_truncated, stats, hidden, steer)


async def added_page(query: str, cat: catalog_store.Catalog, limit: int, opts: catalog_store.AddedOptions, *,
                     caller: Caller, observed: Observed) -> Page:
    """A recently-added page (catalog.md "`added`"), for every surface: a flat list of tools inside
    the window, best match first when there are words, newest first with `sort=newest` or with no
    words. No routed parent (a choice among tools, not a tool added on a day), no grouping, no
    judge and no experiment record: the options ask for a date list, which is deterministic.
    Listed hub tools take part on the day treg approved them."""
    keep = catalog_store.added_keep(opts)
    async with session_maker() as db:
        hub_rows, hub_stats = await hub_app.search_listed(
            db, query, cat, org_slug=caller.hub_slug, email=caller.hub_email, everything=not query.strip())
    hub_rows = [(ep, score) for ep, score in hub_rows if keep(ep)]
    if opts.newest or not query.strip():
        rows, total = catalog_store.added_rows(query, cat, opts)
        rows = catalog_store.newest_first(rows + hub_rows)[:limit]
        stats = await observed([ep["id"] for ep, _ in rows if ep.get("kind") != "hub"])
    else:
        rows, total, _ = catalog_store.rank_band(query, cat, limit, keep=keep)
        stats = await observed([ep["id"] for ep, _ in rows])
        rows = catalog_store.merge_by_score(catalog_store.rerank(rows, stats, cat), hub_rows)[:limit]
    return Page(rows, total + len(hub_rows), False, {**stats, **hub_stats}, {}, False)


# --------------------------------------------------------------------------------------------
# v2: the job-first answer, laid out for an agent
# --------------------------------------------------------------------------------------------

def _leads(row: dict, high: float) -> bool:
    """A member the judge rated on its own at or over high: the vendor whose own words matched
    the query's qualifier, which the evidence order would bury past the page cut."""
    return row.get("fit_from") == find.FIT_FROM_ENDPOINT and (row.get("p") or 0.0) >= high


def _groups(found: find.Found, cat: catalog_store.Catalog, hub: Rows, steer: bool) -> list[find.Group]:
    """The answer's groups as the page lays them out: inside a job the members the judge rated on
    their own at or over high lead, then the evidence order; a listed hub tool of a kept job joins
    at the end, no lexical gate; a routed parent, where the job has one and routed discovery is
    on, leads a strong or closest job's group (a name page is what the name offers, and "let treg
    choose" is not one of its rows)."""
    high = float(get_settings().search_judge_high)
    task = found.verdict in (find.STRONG, find.CLOSEST)
    out = [find.Group(g.capability, g.p, list(g.rows), g.providers, g.hidden) for g in found.groups]
    by_job = {g.capability: g for g in out if g.capability}
    if task:
        for g in out:
            g.rows.sort(key=lambda r: not _leads(r, high))     # stable: both classes keep their order
        for ep, _ in hub:
            g = by_job.get(ep.get("capability") or "")
            if g is not None:
                g.rows.append({"ep": ep, "p": None, "fit_from": None})
    if task and steer:
        for g in out:
            parent = catalog_store.routed_parent(cat, g.capability)
            if parent is not None:
                g.rows.insert(0, {"ep": parent, "p": g.p, "fit_from": find.FIT_FROM_JOB})
    return out


def _vendors(rows: list[dict]) -> set[str]:
    return {r["ep"]["provider"] for r in rows if r["ep"].get("kind") != "routed"}


def page_groups(limit: int) -> int:
    """How many jobs a page of `limit` rows holds: one per two rows (four on a page of eight), so
    the best job still shows a vendor or two when the judge kept many jobs, and the rest wait for
    `catalog_get` or a longer page. Measured on agents' searches at a page of eight: fewer jobs
    per page lifts the chance the row an agent calls is on it, more jobs per page lifts the
    chance its job is; one per two rows holds both near their best."""
    return max(1, limit // 2)


def agent_page(found: find.Found, cat: catalog_store.Catalog, limit: int, *, hub: Rows,
               steer: bool) -> tuple[list[dict], int, list[Job], dict[str, int]]:
    """A v2 answer cut to an agent's page. A judged answer keeps its best `page_groups` jobs (a
    name page every job of the name); their rows are picked round-robin (the first row of every
    job, then the second, ...), so a page of eight holds four jobs as 2+2+2+2 rather than eight
    vendors of the first, and laid out job by job, each job's rows contiguous, the way the CLI
    and the routed copy read a page. Returns the rows, the count before the cut, the jobs on the
    page, and per job (on its first row) the vendors left out."""
    groups = _groups(found, cat, hub, steer)
    kept = groups[:page_groups(limit)] if found.verdict in (find.STRONG, find.CLOSEST) else groups
    slots = sorted((depth, gi) for gi, g in enumerate(kept) for depth in range(len(g.rows)))[:limit]
    taken = Counter(gi for _, gi in slots)
    rows: list[dict] = []
    jobs: list[Job] = []
    hidden: dict[str, int] = {}
    for gi, g in enumerate(kept):
        take = g.rows[:taken[gi]]
        if not take:
            continue
        rows.extend(take)
        if g.capability:
            jobs.append(Job(g.capability, g.providers, sum(r["ep"].get("kind") != "routed" for r in take)))
            left = len(_vendors(g.rows) - _vendors(take)) + g.hidden
            if left:
                hidden[take[0]["ep"]["id"]] = left
    return rows, sum(len(g.rows) for g in groups), jobs, hidden


def serve(found: find.Found, cat: catalog_store.Catalog, limit: int, *, hub: Rows,
          steer: bool) -> tuple[list[dict], int, list[Job], dict[str, int]] | None:
    """What an agent is served for a v2 answer: the page of a judged answer (strong, closest,
    name), an empty page for a none, or None for a keyword verdict, meaning the lexical page the
    caller already has. The one dispatch the use case and the bench share."""
    if found.verdict in find.JUDGED:
        return agent_page(found, cat, limit, hub=hub, steer=steer)
    if found.verdict == find.NONE:
        return [], 0, [], {}
    return None


async def _judge_cap(key: str) -> bool:
    """One caller's judged searches this hour, within `search_judge_max_per_caller_hour`. Its own
    session, closed here."""
    async with session_maker() as db:
        ok = await ratestore.rate_check(db, RATE_NS, [(key, int(get_settings().search_judge_max_per_caller_hour))],
                                        RATE_WINDOW_S)
        await db.commit()
    return ok


async def _v2(query: str, cat: catalog_store.Catalog, limit: int, *, page: Page, observed: Observed,
              provider_display, hub: Rows, serve_it: bool) -> dict:
    """The job-first answer over the lexical `page`, which it turns into the page to serve (v2's
    rows, or the lexical ones under verdict `keyword`); returns the answer's SearchLog fields.
    `serve_it=False` judges and records only (the lexical holdout): no rows are laid out."""
    r = await find.recall_with_meaning(query, cat, provider_display)
    found = await find.judge_and_decide(query, r.cands, cat, provider_display,
                                        timeout_s=float(get_settings().typesafe_timeout_s), not_task=find.KEYWORD)
    if serve_it:
        seen: dict[str, dict] = {}

        async def evidence(ids: list[str]) -> dict[str, dict]:
            # the routed parents of the jobs read, too: a parent leads its group on the page
            rows = catalog_store.with_routed_parents([(cat.by_id[i], 0.0) for i in ids], cat)
            st = await observed([ep["id"] for ep, _ in rows])
            seen.update(st)
            return st
        await find.lay_out(found, query, cat, evidence=evidence, keyword_rows=False)
        page.verdict, page.reason = found.verdict, found.reason
        choice = (found.platform or {}).get("choice")
        page.platform = choice if choice and choice != "none" else None
        served = serve(found, cat, limit, hub=hub, steer=page.steering)
        if served is not None:
            rows, page.total, page.jobs, page.hidden = served
            page.rows = [(row["ep"], 0.0) for row in rows]
            page.stats = {**page.stats, **seen}
    return find.log_fields_v2(found, r)


async def search(query: str, limit: int, *, cat: catalog_store.Catalog, source: str, caller: Caller,
                 observed: Observed, identify: Identify, provider_display=lambda s: s) -> Page:
    """The page this caller sees, recorded. The lexical page first; while the experiment is on, the
    judge reads a wider recall (v1) or the jobs (v2) and the arm decides what is shown - whatever
    it does, the result is a page. The miss log: in v1 modes it is judged by the LEXICAL page (it
    measures the shipped ranker's coverage, and a judged page that found something is the
    experiment's result, not a reason to stop recording the gap); a served v2 answer files a miss
    when it is a gap, or when it fell back to an empty lexical page."""
    hub = await _hub_rows(query, cat, caller)
    page = await lexical(query, cat, limit, observed=observed, hub=hub)
    page.lexical_empty = not page.rows and bool(query.strip())
    mode = search_experiment.mode()
    if not query.strip() or mode == "off":
        if page.lexical_empty:
            audit.record_search_miss(query=query.strip(), source=source)
        return page

    key, org_id, email = await identify()
    page.caller_key = key
    identity = search_experiment.identity_key(org_id, email)
    arm_key = identity or key
    arm = search_experiment.arm_for(arm_key)
    lexical_ids, lexical_total = [ep["id"] for ep, _ in page.rows], page.total

    # The cap is keyed by WHO the caller is; a token the search could not resolve (the tool reads
    # the catalog without validating a per-team token, so any string reaches here) shares one
    # bucket, or a caller rotating made-up tokens would have a fresh cap per search.
    if mode == "v2" and not await _judge_cap(identity or UNRESOLVED):
        # past the cap no judge runs for this caller this hour, whichever arm: the lexical page,
        # recorded as such (a search is not metered; this is the bound on what one can spend)
        page.arm = arm
        if arm == search_experiment.ARM_V2:
            page.verdict, page.reason = find.KEYWORD, RATE_LIMITED       # a holdout's page carries no verdict
        page.log = dict(mode="v2", arm=arm, engine="v2", judge_error=RATE_LIMITED,
                        verdict=find.verdict_label(find.KEYWORD, RATE_LIMITED), baseline_ids=lexical_ids,
                        baseline_total=lexical_total, differs=False,
                        shown=find.shown_rows([ep for ep, _ in page.rows], "baseline"))
        audit.record_search(query=query.strip(), source=source, org_id=org_id, user_email=email, **page.log)
        if page.lexical_empty:
            audit.record_search_miss(query=query.strip(), source=source, engine="v2",
                                     reason=find.miss_reason(find.KEYWORD, RATE_LIMITED))
        return page

    if mode != "v2" or arm == search_experiment.ARM_JUDGED:
        # v1: the experiment deals the arm and builds what it serves (in v2 mode: the holdout on
        # the v1 judged page)
        async def finish(rows: Rows) -> tuple[Rows, dict[str, dict]]:
            st = await observed([ep["id"] for ep, _ in rows])
            grouped, _ = _group(catalog_store.rerank(rows, st, cat), limit)
            return grouped, st
        exp = await search_experiment.run(query, cat, baseline=page.rows, baseline_total=page.total,
                                          limit=limit, caller=arm_key, finish=finish)
        page.stats = {**exp.stats, **page.stats}
        page.rows, page.arm, page.log = exp.shown, exp.arm, exp.log
        audit.record_search(query=query.strip(), source=source, org_id=org_id, user_email=email, **page.log)
        if page.lexical_empty:
            audit.record_search_miss(query=query.strip(), source=source)
        return page

    # v2 mode: `v2` is served the answer; `baseline` keeps the lexical page and records the answer
    serve_it = arm == search_experiment.ARM_V2
    try:
        fields = await _v2(query, cat, limit, page=page, observed=observed, provider_display=provider_display,
                           hub=hub[0], serve_it=serve_it)
    except Exception:  # noqa: BLE001 - a search degrades to the shipped ranker, never fails
        log.warning("v2 search failed; serving the lexical page", exc_info=True)
        if serve_it:
            page.verdict, page.reason = find.KEYWORD, "error"       # a holdout's page carries no verdict
        fields = dict(engine="v2", judge_error="error", verdict=find.verdict_label(find.KEYWORD, "error"))
    page.arm = arm
    page.log = dict(mode="v2", arm=arm, baseline_ids=lexical_ids, baseline_total=lexical_total, differs=False,
                    shown=find.shown_rows([ep for ep, _ in page.rows], find.row_owner(page.verdict or "")), **fields)
    audit.record_search(query=query.strip(), source=source, org_id=org_id, user_email=email, **page.log)
    if serve_it and (page.verdict == find.NONE or (page.verdict == find.KEYWORD and page.lexical_empty)):
        audit.record_search_miss(query=query.strip(), source=source, engine="v2",
                                 reason=find.miss_reason(page.verdict, page.reason))
    elif not serve_it and page.lexical_empty:
        audit.record_search_miss(query=query.strip(), source=source)
    return page
