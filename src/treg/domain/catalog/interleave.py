"""Interleaving for the discovery experiment — pure list logic, no I/O.

Two rankers answer the same query with two pages. Serving one page to half the callers and the other
to the other half (an A/B split) compares the rankers only across callers, and the noise between
callers swamps the difference between pages. Team-draft interleaving merges both pages into ONE and
remembers which ranker put each row there; when the caller goes on to `call` a row, the ranker that
supplied it scores a point. Every query becomes a paired comparison, which is why interleaving
needs an order of magnitude fewer queries than a split for the same confidence.

Credit is given only where the rankers DISAGREE. A row both pages carry at the same rank says
nothing about either; a row only one page carries, or one they rank differently, does. Queries on
which the two pages are identical therefore contribute nothing — correctly, since the judged
ranker changed nothing there.
"""
from __future__ import annotations

import hashlib
import random

BASELINE, JUDGED, BOTH = "baseline", "judged", "both"


def draft_seed(*parts: str) -> int:
    """A stable coin for one query: same query, same merge — the merge is reproducible from the log
    and a caller that repeats a query sees the same page, not a reshuffle."""
    return int(hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16], 16)


def team_draft(baseline: list[str], judged: list[str], k: int, seed: int) -> list[tuple[str, str]]:
    """Merge two ranked id lists into at most `k` rows of `(id, owner)`.

    Each round a coin decides who picks first; the picker takes its highest-ranked id not yet on the
    page, then the other side takes its own. A row both lists wanted is owned by whichever side took
    it — the coin makes that fair over many queries — but `owner` also says `both` when the other
    list held it too, so the reader can apply the disagreement rule (`credit`) rather than the pick.
    """
    rng = random.Random(seed)
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    pos = {BASELINE: 0, JUDGED: 0}
    lists = {BASELINE: baseline, JUDGED: judged}
    other = {BASELINE: set(judged), JUDGED: set(baseline)}

    def take(side: str) -> bool:
        src, i = lists[side], pos[side]
        while i < len(src) and src[i] in seen:
            i += 1
        pos[side] = i
        if i >= len(src) or len(out) >= k:
            return False
        eid = src[i]
        seen.add(eid)
        out.append((eid, BOTH if eid in other[side] else side))
        pos[side] = i + 1
        return True

    while len(out) < k and (pos[BASELINE] < len(baseline) or pos[JUDGED] < len(judged)):
        first, second = (BASELINE, JUDGED) if rng.random() < 0.5 else (JUDGED, BASELINE)
        took = take(first)
        took = take(second) or took
        if not took:
            break
    return out


def credit(called: str, baseline: list[str], judged: list[str]) -> str | None:
    """Which ranker earns the point when `called` was chosen off an interleaved page — or None.

    Only one page carried it → that ranker. Both carried it → the one that ranked it higher; the same
    rank is a tie and no evidence. Not on either page (a caller found it some other way) → None.
    """
    in_a, in_b = called in baseline, called in judged
    if in_a and not in_b:
        return BASELINE
    if in_b and not in_a:
        return JUDGED
    if in_a and in_b:
        ra, rb = baseline.index(called), judged.index(called)
        return BASELINE if ra < rb else JUDGED if rb < ra else None
    return None


def bucketed(rows: list[tuple[dict, float]], probs: list[float], *, keep: float, high: float
             ) -> list[tuple[dict, float, float]]:
    """The judged page from `(endpoint, lexical_score)` candidates and the judge's probabilities:
    drop below `keep`, put `>= high` first, and keep the LEXICAL order inside each bucket.

    Buckets rather than a sort by probability, on purpose. Rows that score alike lexically still
    tie exactly, so the evidence rerank (`store.rerank`) keeps its say inside a bucket; the judge
    decides whether a row belongs on the page and roughly where, not the fine order the measured
    record already settles."""
    tagged = [(ep, score, p) for (ep, score), p in zip(rows, probs) if p >= keep]
    return sorted(tagged, key=lambda t: (0 if t[2] >= high else 1))  # stable: lexical order within
