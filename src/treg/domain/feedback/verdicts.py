"""What agents said about a catalog endpoint after using its result: the aggregate the catalog shows.

One team is one vote per endpoint. A team's several reviews of one endpoint split its vote across
the verdicts it gave, so an agent that rates every call of a batch weighs as much as a team that
rated once; that is what lets volunteered reviews count alongside invited ones. `not_sure` is not
a verdict and is left out. An endpoint below `MIN_TEAMS` teams gets no share and no label: a
share of three teams is an anecdote. What it does get is its reasons, quoted as early reviews
under the band `teams: 0`, because one agent's account of what the result was good or bad for
says more about an endpoint than a percentage over three votes would. With no quotable reason
there is nothing to say, and the row stays silent.

The score is only comparable between sibling endpoints of one capability (models lean toward
`useful`, and a harder job draws harsher verdicts), which is why it is shown per endpoint and
never rolled up per provider.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ...models import CallReview

VERDICTS = ("useful", "partly", "not_useful")
WINDOW_DAYS = 90
MIN_TEAMS = 5
SAMPLES = 3
MIN_REASON_CHARS = 40
# How many teams rated an endpoint is published as the band it falls in (its floor), never exactly:
# an exact count next to the shares would give each small group's votes away, and a count that
# moves from 5 to 6 would say how the new team voted.
TEAM_BANDS = (5, 10, 25, 50)


@dataclass(frozen=True)
class Review:
    endpoint_id: str
    org_id: int
    usefulness: str
    reason: str | None
    client: str
    created_at: datetime


async def since(db: AsyncSession, start: datetime) -> list[Review]:
    rows = await db.execute(select(
        CallReview.endpoint_id, CallReview.org_id, CallReview.usefulness, CallReview.reason,
        CallReview.client, CallReview.created_at,
    ).where(CallReview.created_at >= start, CallReview.usefulness.in_(VERDICTS)))
    return [Review(*row) for row in rows]


def team_band(n: int) -> int:
    return max((b for b in TEAM_BANDS if b <= n), default=0)


# Contact data an agent copied from its result into a reason. The review tool asks agents to leave
# private data out; a reason that still carries an email address, a phone number or a personal
# profile link is never published.
_EMAIL = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
# Separators include every Unicode dash (non-breaking hyphen, en dash, minus sign, ...).
_PHONE = re.compile(r"\+?\d[\d\s().\-\u2010-\u2015\u2212\uFE58\uFE63\uFF0D]{6,}\d")
_PHONE_DIGITS = 9
# Dates and clock times are taken out before looking for a phone number: `2026-11-22 14:00` and
# `2026-11-01 - 2026-11-22` run nine digits together without being one.
_DATETIME = re.compile(r"\b\d{4}[-/.]\d{1,2}[-/.]\d{1,2}(?:T\d{1,2}:\d{2}(?::\d{2})?)?\b"
                       r"|\b\d{1,2}:\d{2}(?::\d{2})?\b")
# A person's LinkedIn page: public profile, old public profile, Sales Navigator lead or Recruiter.
_PROFILE = re.compile(r"linkedin\.com/(?:in|pub|sales/lead|sales/people|talent/profile)/", re.IGNORECASE)
# Capability segments whose results are about a person: their reasons name the person, so they
# are never quoted, whatever a pattern can catch.
PERSON_SEGMENTS = frozenset({"people", "person", "persons", "contact", "contacts", "employee",
                             "employees", "user", "users", "member", "members", "followers", "teams",
                             "artist", "artists"})
# Any segment naming these (`phone`, `by_email`, `channels_creator`, `founders`, `ownership`,
# `talents`) is about a person.
PERSON_FRAGMENTS = ("email", "phone", "creator", "founder", "owner", "talent")
# A profile is a person's (`linkedin.profile.search`, `threads.search.profiles`,
# `youtube.channel.profile`) unless the capability names what else it is the profile of.
PROFILE_SEGMENTS = frozenset({"profile", "profiles"})
NOT_A_PERSON = frozenset({"company", "companies", "business", "businesses", "repo", "school", "coin",
                          "crypto", "stocks", "seller", "app", "job", "jobs"})


def carries_contact_data(reason: str) -> bool:
    return bool(_EMAIL.search(reason) or _PROFILE.search(reason) or any(
        sum(c.isdigit() for c in m.group()) >= _PHONE_DIGITS
        for m in _PHONE.finditer(_DATETIME.sub(" ", reason))))


def about_people(capability: str | None) -> bool:
    """Whether a capability's results are about people (`people.search`, `phone.validate`,
    `instagram.user.profile`, `companies.employees`, ...)."""
    segments = set((capability or "").lower().split("."))
    profile = not PROFILE_SEGMENTS.isdisjoint(segments) and NOT_A_PERSON.isdisjoint(segments)
    return bool(capability) and (profile or not PERSON_SEGMENTS.isdisjoint(segments) or any(
        f in s for s in segments for f in PERSON_FRAGMENTS))


def may_quote(capability: str | None) -> bool:
    """Whether an endpoint's reasons may be quoted at all: only when its capability is known and
    not about people. An endpoint with no capability could be about anyone, so it shows none."""
    return bool(capability) and not about_people(capability)


def quotable(reason: str | None) -> bool:
    """A reason long enough to say something about the endpoint, and carrying no contact data.
    Quoted as written: the review tool tells agents reasons may be quoted without naming the team,
    and to leave private data out."""
    text = (reason or "").strip()
    return len(text) >= MIN_REASON_CHARS and not carries_contact_data(text)


def _quotas(share: dict[str, float], slots: int) -> dict[str, int]:
    """Largest-remainder split of `slots` by verdict share, so the quotes shown are drawn in the
    proportions the teams voted rather than picked for tone."""
    exact = {v: share[v] * slots for v in VERDICTS}
    quota = {v: int(exact[v]) for v in VERDICTS}
    for v in sorted(VERDICTS, key=lambda v: exact[v] - quota[v], reverse=True)[:slots - sum(quota.values())]:
        quota[v] += 1
    return quota


def _samples(candidates: list[Review], share: dict[str, float], slots: int) -> list[Review]:
    slots = min(slots, len(candidates))
    newest = sorted(candidates, key=lambda r: r.created_at, reverse=True)
    picked: list[Review] = []
    for verdict, n in _quotas(share, slots).items():
        picked += [r for r in newest if r.usefulness == verdict][:n]
    # A verdict with too few quotable reasons leaves its slots to the newest of the rest.
    picked += [r for r in newest if r not in picked][:slots - len(picked)]
    return sorted(picked, key=lambda r: r.created_at, reverse=True)


def _quote(r: Review) -> dict:
    # A month, never a day: enough to read, not enough to date a quote to the day another team
    # ran its task.
    return {"usefulness": r.usefulness, "reason": r.reason.strip(), "client": r.client,
            "month": r.created_at.strftime("%Y-%m")}


def summarize(reviews: list[Review], *, min_teams: int = MIN_TEAMS, samples: int = SAMPLES,
              quotes_allowed: Callable[[str], bool] = lambda _endpoint_id: True) -> dict[str, dict]:
    """Per endpoint id: `{teams, share, samples}` from `min_teams` teams, `teams` banded. Below
    that the `share` is withheld (`teams` is then the band below the first, 0) and the row exists
    only when there is a quotable reason to show. An endpoint `quotes_allowed` refuses (one whose
    results are about people) shows no reason at all."""
    by_endpoint: dict[str, dict[int, list[Review]]] = {}
    for r in reviews:
        by_endpoint.setdefault(r.endpoint_id, {}).setdefault(r.org_id, []).append(r)
    out: dict[str, dict] = {}
    for endpoint_id, teams in by_endpoint.items():
        votes = dict.fromkeys(VERDICTS, 0.0)
        candidates: list[Review] = []
        for rows in teams.values():
            for r in rows:
                votes[r.usefulness] += 1 / len(rows)
            quoted = [r for r in rows if quotable(r.reason)]
            if quoted:  # one quote per team at most: its latest
                candidates.append(max(quoted, key=lambda r: r.created_at))
        share = {v: votes[v] / len(teams) for v in VERDICTS}
        scored = len(teams) >= min_teams
        quotes = ([_quote(r) for r in _samples(candidates, share, samples)]
                  if quotes_allowed(endpoint_id) else [])
        if scored or quotes:
            out[endpoint_id] = {"teams": team_band(len(teams)), "samples": quotes}
        if scored:
            # Two places: enough to read, not enough to reconstruct one team's votes.
            out[endpoint_id]["share"] = {v: round(s, 2) for v, s in share.items()}
    return out
