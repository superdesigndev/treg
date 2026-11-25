"""Where review invitations go: more often on endpoints that still lack reviews.

A review is worth most where we have few. Each worker keeps, per endpoint (the one a review is
credited to: a direct call's own endpoint, or the child that answered a routed call), how many
reviews it holds in the last `review_target_window_days`, counting at most `review_target_team_cap`
per team so one busy team cannot fill an endpoint alone. Below `review_target_reviews` an
endpoint's invitation rate climbs linearly toward `review_target_rate` (an endpoint with none);
at or above it, the base `review_sample_rate` applies. The per-team hourly budget still caps how
many invitations a team receives, so this moves invitations, it does not add work per team.

The ranking counts at most `review_target_team_cap` reviews per team and endpoint, so a team that
already holds that many on an endpoint is not invited there again until older ones leave the
window: its budget goes to endpoints where its review still counts. A team that has not reviewed
an endpoint at all is invited at least at `review_target_new_team_rate`, because a ranking needs
many teams per endpoint, not many reviews from a few.

The counts are a cached snapshot, refreshed in the background every `REFRESH_S`; until the first
read lands every endpoint gets the base rate. `invited` on a review recomputes the rate with the
snapshot of the moment, so near an endpoint's target it can disagree with the invitation sent;
PostHog's `hint_attached` is the exact record of invitations.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from sqlalchemy import case, func, select

from .. import hints
from ..config import get_settings
from ..infra.db import session_maker
from ..models import CallReview
from ..timeutil import utcnow_naive

log = logging.getLogger(__name__)
REFRESH_S = 600
READ_TIMEOUT_S = 2.0


class _Counts:
    def __init__(self) -> None:
        self.by_endpoint: dict[str, int] | None = None
        self.by_pair: dict[tuple[int, str], int] = {}
        self.next_refresh = 0.0
        self.task: asyncio.Task | None = None

    async def refresh(self) -> None:
        settings = get_settings()
        since = utcnow_naive() - timedelta(days=settings.review_target_window_days)
        cap = settings.review_target_team_cap
        per_team = (select(CallReview.endpoint_id, CallReview.org_id,
                           case((func.count() > cap, cap), else_=func.count()).label("n"))
                    .where(CallReview.created_at >= since)
                    .group_by(CallReview.endpoint_id, CallReview.org_id).subquery())
        try:
            # Optional data: a slow or stuck read must never hold a worker's startup or pile up,
            # so it is bounded, and on timeout the last snapshot (or the base rate) stands.
            async with asyncio.timeout(READ_TIMEOUT_S), session_maker() as db:
                rows = (await db.execute(select(per_team.c.endpoint_id, per_team.c.org_id,
                                                per_team.c.n))).all()
        except Exception:  # a failed or timed-out read keeps the last snapshot
            log.warning("review target counts refresh failed", exc_info=True)
            return
        by_endpoint: dict[str, int] = {}
        by_pair: dict[tuple[int, str], int] = {}
        for endpoint, org_id, n in rows:
            if endpoint:
                by_endpoint[endpoint] = by_endpoint.get(endpoint, 0) + int(n)
                by_pair[(int(org_id), endpoint)] = int(n)
        self.by_pair = by_pair
        self.by_endpoint = by_endpoint

    def current(self) -> dict[str, int] | None:
        """The snapshot, at once; a stale one starts a single background refresh."""
        now = time.monotonic()
        if now >= self.next_refresh and (self.task is None or self.task.done()):
            self.next_refresh = now + REFRESH_S
            try:
                self.task = asyncio.get_running_loop().create_task(self.refresh())
            except RuntimeError:  # no running loop: keep the snapshot we have
                pass
        return self.by_endpoint


_counts = _Counts()


def enabled() -> bool:
    return get_settings().review_target_reviews > 0


def targeted_rate(endpoint_id: str | None, org_id: int | None = None) -> float | None:
    """The targeted rate for a call by team `org_id` credited to `endpoint_id`, or None when the
    base rate applies (targeting off, no snapshot yet, no endpoint, or nothing to boost). 0 when
    the team already holds `review_target_team_cap` reviews of this endpoint in the window."""
    settings = get_settings()
    if not enabled() or not endpoint_id:
        return None
    counts = _counts.current()
    if counts is None:
        return None
    base = settings.review_sample_rate
    top = max(base, settings.review_target_rate)
    mine = _counts.by_pair.get((org_id, endpoint_id), 0) if org_id is not None else None
    if mine is not None and mine >= settings.review_target_team_cap:
        return 0.0
    rate_ = None
    target = settings.review_target_reviews
    have = counts.get(endpoint_id, 0)
    if have < target:
        rate_ = base + (top - base) * (1 - have / target)
    if mine == 0 and settings.review_target_new_team_rate > max(base, rate_ or 0):
        rate_ = settings.review_target_new_team_rate
    return rate_


def rate(endpoint_id: str | None, org_id: int | None = None) -> float:
    """The review invitation rate for a call by team `org_id` credited to `endpoint_id`."""
    boosted = targeted_rate(endpoint_id, org_id)
    return get_settings().review_sample_rate if boosted is None else boosted


def sampled(call_id: str, endpoint_id: str | None, org_id: int | None = None) -> bool:
    """Whether a review of this call is invited: the base draw, or the same draw at the targeted
    rate (0 for a team already at its cap on this endpoint)."""
    boosted = targeted_rate(endpoint_id, org_id)
    if boosted is None:
        return hints.sampled("review", call_id)
    return hints.sampled_at("review", call_id, boosted)


async def warm() -> None:
    """Load the counts now (tests, and a worker before it serves when targeting is on)."""
    if enabled():
        _counts.next_refresh = time.monotonic() + REFRESH_S
        await _counts.refresh()


def reset() -> None:
    global _counts
    if _counts.task is not None and not _counts.task.done():
        try:
            _counts.task.cancel()
        except RuntimeError:  # its loop already closed (between isolated tests)
            pass
    _counts = _Counts()
