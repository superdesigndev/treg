"""The discovery experiment — does a relevance judge find what the lexical gate misses?

`catalog_search` is token matching: reproducible, explainable, and blind to meaning. The SearchMiss
log shows its two failure shapes — a task-phrased query whose words are parameter VALUES ("apple
stock closing prices for last year") admits nothing, and a query whose words happen to occur in an
unrelated row admits the wrong thing. Both are semantic. This module puts a judge (TypeSafe's Jev,
`infra.judge`) behind a widened lexical recall (`store.candidates`) and measures the result against
the shipped ranker on BEHAVIOUR: did the caller go on to `call` an endpoint from the page it was
shown. There are no labels; the caller's next action is the label.

Three modes, one setting (`search_experiment`):

- `off`      — nothing here runs; search is exactly the shipped ranker.
- `shadow`   — both pages are computed and logged, the baseline is served. Answers "how often do the
               pages differ, what does the judge cost and how long does it take" before any caller
               sees a changed page, and lets the log be read counterfactually against later calls.
- `interleave` — most callers see a team-draft merge of the two pages (`domain.catalog.interleave`);
               two small holdouts see a pure page each so the absolute effect and the latency cost
               can be read as well as the paired preference.
- `v2`       — most callers see the job-first answer (`application.catalog_search`, the engine behind
               /catalog/find); the same two holdouts see the pure baseline and the pure v1 judged
               page, so v2 is read against what it replaces and against the shipped ranker.

Arms are dealt per CALLER - the team and sign-in email where the search resolved them, else a hash
of the bearer token, salted either way - so one agent's session is consistent and its re-queries are
measurable, and a caller whose token rotates (an OAuth grant) keeps its arm. Whatever happens here,
the caller gets a page: a judge that times out, errors or is unconfigured abstains and the baseline
is served — the experiment can degrade search latency by at most `typesafe_timeout_s`, never its
availability. The HTTP search route is not in the experiment: it is anonymous, so there is no later
call to credit.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..config import get_settings
from ..domain.catalog import interleave
from ..domain.catalog import store as catalog_store
from ..infra import judge as judge_infra

MODES = ("off", "shadow", "interleave", "v2")
ARM_SHADOW, ARM_BASELINE, ARM_JUDGED, ARM_INTERLEAVE, ARM_V2 = "shadow", "baseline", "judged", "interleave", "v2"

# Rows the judge put in its top bucket carry their lexical score plus this, so a plain score-desc
# sort (which is what `store.rerank` does first) keeps the buckets apart and the lexical order —
# and therefore the evidence tie-break — intact inside each. Stripped again before anything is shown.
_BUCKET_LIFT = 1_000_000.0

Rows = list[tuple[dict, float]]
# Turns a scored list into the page the caller sees, the same way the baseline was built (evidence
# rerank, routed grouping, cut to the page); returns the page and the observed stats it fetched.
Finish = Callable[[Rows], Awaitable[tuple[Rows, dict[str, dict]]]]


def mode() -> str:
    """The effective mode: the setting, unless the judge cannot run, in which case `off`."""
    s = get_settings()
    m = str(s.search_experiment).strip().lower()
    if m not in MODES or not s.typesafe_api_key:
        return "off"
    return m


def caller_key(token: str) -> str | None:
    """A stable, non-reversible handle on one caller — the unit the arms are dealt over."""
    return hashlib.sha256(token.encode()).hexdigest()[:16] if token else None


def identity_key(org_id: int | None, email: str | None) -> str | None:
    """The caller's handle by WHO they are, where the search resolved it (else the arm is dealt by
    `caller_key`): an OAuth grant's token rotates hourly, and a caller dealt by token would change
    arms with it."""
    if org_id is None or not email:
        return None
    return hashlib.sha256(f"{org_id}\x1f{email.strip().lower()}".encode()).hexdigest()[:16]


def arm_for(key: str | None) -> str:
    """`baseline` / `judged` for the two holdouts, the mode's majority arm (`interleave`, or `v2`
    in v2 mode) for everyone else; a caller without a key (no token reached us) is in the majority
    arm — there is no later call to credit either way."""
    s = get_settings()
    majority = ARM_V2 if str(s.search_experiment).strip().lower() == "v2" else ARM_INTERLEAVE
    if key is None:
        return majority
    bucket = int(hashlib.sha256(f"{s.search_experiment_salt}\x1f{key}".encode()).hexdigest()[:8], 16) % 100
    holdout = max(0, min(int(s.search_experiment_holdout_percent), 50))
    if bucket < holdout:
        return ARM_BASELINE
    if bucket < 2 * holdout:
        return ARM_JUDGED
    return majority


@dataclass
class Outcome:
    arm: str
    shown: Rows                                  # the page to serve, in order
    stats: dict[str, dict] = field(default_factory=dict)   # observed stats for rows the judge added
    owners: dict[str, str] = field(default_factory=dict)   # id -> baseline | judged | both (interleave arm)
    log: dict = field(default_factory=dict)      # SearchLog fields the caller does not know (no identity/source)


async def run(query: str, cat: catalog_store.Catalog, *, baseline: Rows, baseline_total: int,
              limit: int, caller: str | None, finish: Finish) -> Outcome:
    """Judge the widened recall, build the judged page, and decide what this caller sees.

    Never raises past the judge: an abstaining judge yields the baseline in every arm, and the row
    still records why (`judge_error`), because the latency and failure rate of the judge are part
    of the result, not noise around it.
    """
    s = get_settings()
    current = mode()
    cands = catalog_store.candidates(query, cat, max(1, int(s.search_experiment_candidates)))
    views = [judge_infra.candidate_view(ep, cat.capabilities.get(ep.get("capability") or "", ""))
             for ep, _ in cands]
    verdict = await judge_infra.judge(
        query, views, api_key=s.typesafe_api_key, model=s.typesafe_model, url=s.typesafe_url,
        timeout_s=float(s.typesafe_timeout_s))

    judged_page: Rows | None = None
    stats: dict[str, dict] = {}
    probs: dict[str, float] = {}
    if verdict.probs is not None:
        kept = interleave.bucketed(cands, verdict.probs, keep=float(s.search_judge_keep),
                                   high=float(s.search_judge_high))
        probs = {ep["id"]: p for ep, _, p in kept}
        lifted = [(ep, score + (_BUCKET_LIFT if p >= float(s.search_judge_high) else 0.0))
                  for ep, score, p in kept]
        lifted = catalog_store.with_routed_parents(lifted, cat)
        page, stats = await finish(lifted)
        judged_page = [(ep, score - _BUCKET_LIFT if score >= _BUCKET_LIFT else score) for ep, score in page]

    base_ids = [ep["id"] for ep, _ in baseline]
    judged_ids = [ep["id"] for ep, _ in judged_page] if judged_page is not None else None
    differs = judged_ids is not None and judged_ids != base_ids

    arm = ARM_SHADOW if current == "shadow" else arm_for(caller)
    shown, owners = baseline, {}
    if judged_page is not None and arm == ARM_JUDGED:
        shown = judged_page
    elif judged_page is not None and arm == ARM_INTERLEAVE:
        by_id = {ep["id"]: (ep, score) for ep, score in [*baseline, *judged_page]}
        draft = interleave.team_draft(base_ids, judged_ids, limit,
                                      interleave.draft_seed(s.search_experiment_salt, query.strip().lower()))
        shown = [by_id[eid] for eid, _ in draft]
        owners = dict(draft)

    log = dict(
        mode=current, arm=arm,
        baseline_ids=base_ids,
        judged=[[eid, round(probs[eid], 3)] for eid in judged_ids if eid in probs] if judged_ids is not None else None,
        shown=[[ep["id"], owners.get(ep["id"], arm), ep.get("capability")] for ep, _ in shown],
        baseline_total=int(baseline_total), differs=bool(differs),
        judge_ms=verdict.ms, judge_tokens_in=verdict.tokens_in, judge_tokens_out=verdict.tokens_out,
        judge_error=verdict.error,
    )
    return Outcome(arm=arm, shown=shown, stats=stats, owners=owners, log=log)
