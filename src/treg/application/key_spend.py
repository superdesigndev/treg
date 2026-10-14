"""Billed spend per API key, read from the daily rollup (`ApiKeySpendDay`).

The rollup is the ledger's settle entries, attributed to a key through the audit row and folded by
`treg-worker spend rollup` (`application/key_spend_rollup.py`, which explains the attribution). The
pages read only the rollup: never the ledger, never `callrecord`, so a page open costs a few
grouped rows per key and day however much the team spent.
"""

from __future__ import annotations

import zlib
from datetime import date, timedelta

from sqlalchemy import case, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..domain.catalog import store as catalog_store
from ..models import ApiKey, ApiKeySpendCursor, ApiKeySpendDay, Org
from .key_spend_rollup import CURSOR_ID, NOT_ATTRIBUTED

WINDOW_DAYS = 30   # the Team page's "Spent" column: today and the 29 days before it


async def history(db: AsyncSession) -> dict:
    """`{"ready", "history_from"}`. Not ready until the first backfill has caught up once: before
    that the rollup holds part of the window, and a partial number would read as a real one."""
    state = await db.get(ApiKeySpendCursor, CURSOR_ID)
    return {"ready": bool(state and state.caught_up_at), "history_from": state.history_from if state else None}


def first_day(today: date, days: int) -> date:
    """Today (UTC) plus the `days - 1` before it."""
    return today - timedelta(days=days - 1)


async def spend_by_key(db: AsyncSession, org_id: int, since: date, *, membership_id: int | None = None) -> dict:
    """`{"keys": {key_id: {"spent_micro", "charges"}}, "unattributed": {...}}` from `since` (a UTC
    day) to today. Given `membership_id`, only that member's keys, and no unattributed line."""
    query = (select(ApiKeySpendDay.api_key_id, func.sum(ApiKeySpendDay.spent_micro), func.sum(ApiKeySpendDay.charges))
             .where(ApiKeySpendDay.org_id == org_id, ApiKeySpendDay.day >= since.isoformat())
             .group_by(ApiKeySpendDay.api_key_id))
    if membership_id is not None:
        query = query.where(ApiKeySpendDay.api_key_id.in_(
            select(ApiKey.id).where(ApiKey.org_id == org_id, ApiKey.membership_id == membership_id)))
    lines = {int(key_id): {"spent_micro": int(spent or 0), "charges": int(charges or 0)}
             for key_id, spent, charges in (await db.execute(query)).all()}
    none = lines.pop(NOT_ATTRIBUTED, {"spent_micro": 0, "charges": 0})
    return {"keys": lines, "unattributed": none if membership_id is None else None}


# The Usage page's spend chart.
MAX_SERIES = 7     # past this many series, the rest fold into one "Other" (no eighth hue)
GROUPS = ("day", "week", "month")
UNATTRIBUTED = "none"
OTHER = "__other"
OTHER_KEY = -1     # the folded keys' id inside the grouped read


def bucket_of(day: date, group: str) -> date:
    """The first day of the bucket `day` falls in: itself, its ISO week's Monday, or its month's 1st."""
    if group == "week":
        return day - timedelta(days=day.weekday())
    if group == "month":
        return day.replace(day=1)
    return day


def _buckets(first: date, last: date, group: str) -> list[date]:
    out, day = [], bucket_of(first, group)
    while day <= last:
        out.append(day)
        if group == "month":
            day = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
        else:
            day += timedelta(days=7 if group == "week" else 1)
    return out


def color_slots(ids: list[str]) -> dict[str, int]:
    """A color slot (0..MAX_SERIES-1) per shown series that stays put across filters and ranges.

    Each id prefers the slot its own value picks (a key's id, a tool's checksum; the unattributed
    line always prefers the same one), so a key keeps its color when the range or a filter changes.
    In rank order, an id whose slot is taken moves to the next free one, so no two shown series
    ever share a color. There are only `MAX_SERIES` slots: ids past that many get none (the caller
    folds them into Other), so the search for a free slot always ends.
    """
    taken: dict[int, str] = {}
    out: dict[str, int] = {}
    for sid in ids:
        if len(taken) >= MAX_SERIES:
            break
        if sid == UNATTRIBUTED:
            want = 3
        elif sid.isdigit():
            want = int(sid) % MAX_SERIES
        else:
            want = zlib.crc32(sid.encode()) % MAX_SERIES
        while want in taken:
            want = (want + 1) % MAX_SERIES
        taken[want] = sid
        out[sid] = want
    return out


def provider_of(endpoint_id: str) -> str:
    """A catalog endpoint id is `<provider>.<rest>`; the rollup keeps retired ids, so the prefix,
    not the live catalog, names the provider."""
    return endpoint_id.split(".", 1)[0] if endpoint_id else "unknown"


def _tool_name(endpoint_id: str, endpoints: dict) -> str:
    ep = endpoints.get(endpoint_id) or {}
    provider = provider_of(endpoint_id)
    # Stacked within one provider, its own prefix on every tool is noise.
    return ep.get("name") or (endpoint_id[len(provider) + 1:] if endpoint_id.startswith(provider + ".") else endpoint_id) or "unknown"


async def spend_report(
    db: AsyncSession, org_id: int, first: date, last: date, *,
    group: str = "day", key_id: int | None = None, provider: str | None = None, stack: str = "key",
) -> dict:
    """The team's billed spend between two UTC days inclusive, bucketed and stacked for a chart,
    read only from the rollup in four grouped reads, each bounded by the team's keys or tools,
    times the days only once the series are folded to at most `MAX_SERIES` plus Other.

    Filters (`key_id`, `provider`) narrow the money; the `options` lists are computed before them, so
    a filter never hides its own alternatives. `stack` is `key` (one series per API key, plus the
    unattributed money) or `tool` (one per endpoint).
    """
    T = ApiKeySpendDay
    window = [T.org_id == org_id, T.day >= first.isoformat(), T.day <= last.isoformat()]
    narrowed = window + ([T.api_key_id == key_id] if key_id is not None else []) \
        + ([T.endpoint_id.startswith(provider + ".", autoescape=True)] if provider else [])
    spent, charges = func.sum(T.spent_micro), func.sum(T.charges)

    key_totals = {int(k): int(v or 0) for k, v in (await db.execute(
        select(T.api_key_id, spent).where(*window).group_by(T.api_key_id))).all()}
    provider_totals: dict[str, int] = {}
    for endpoint_id, micro in (await db.execute(select(T.endpoint_id, spent).where(*window).group_by(T.endpoint_id))).all():
        prov = provider_of(endpoint_id)
        provider_totals[prov] = provider_totals.get(prov, 0) + int(micro or 0)

    column = T.endpoint_id if stack == "tool" else T.api_key_id

    def sid(raw) -> str:
        if stack == "tool":
            return raw or "unknown"
        return UNATTRIBUTED if int(raw) == NOT_ATTRIBUTED else str(int(raw))

    totals: dict = {}
    for raw, micro, n in (await db.execute(select(column, spent, charges).where(*narrowed).group_by(column))).all():
        totals[raw] = (int(micro or 0), int(n or 0))
    ranked = sorted(totals, key=lambda raw: (-totals[raw][0], sid(raw)))
    kept = ranked[:MAX_SERIES]   # one more series would need an eighth color: it folds, even alone
    folded = ranked[len(kept):]

    # Per bucket, only the kept series and one Other: days x (MAX_SERIES + 1) rows at most.
    other = OTHER if stack == "tool" else OTHER_KEY
    # Grouped by its label: Postgres would not match a repeated CASE whose IN list is bound twice.
    series_of = (case((column.in_(kept), column), else_=other) if folded else column).label("series")
    buckets = {b: {"parts": {}, "charges": 0} for b in _buckets(first, last, group)}
    for day, raw, micro, n in (await db.execute(
            select(T.day, series_of, spent, charges).where(*narrowed).group_by(T.day, literal_column("series")))).all():
        bucket = buckets.get(bucket_of(date.fromisoformat(day), group))
        if bucket is None:
            continue
        name = OTHER if raw == other else sid(raw)
        bucket["parts"][name] = bucket["parts"].get(name, 0) + int(micro or 0)
        bucket["charges"] += int(n or 0)

    names = await _key_names(db, org_id, [k for k in key_totals if k != NOT_ATTRIBUTED])
    endpoints = catalog_store.load().by_id if stack == "tool" else {}

    def name(raw) -> str:
        if stack == "tool":
            return _tool_name(raw, endpoints)
        return "No API key" if int(raw) == NOT_ATTRIBUTED else names.get(int(raw), f"Key #{raw}")

    slots = color_slots([sid(raw) for raw in kept])
    series = [{"id": sid(raw), "name": name(raw), "spent_micro": totals[raw][0], "slot": slots[sid(raw)]} for raw in kept]
    if folded:
        series.append({"id": OTHER, "name": "Other", "spent_micro": sum(totals[raw][0] for raw in folded),
                       "members": len(folded)})
    return {
        "from": first.isoformat(), "to": last.isoformat(), "group": group, "stack": stack,
        "spent_micro": sum(v[0] for v in totals.values()),
        "charges": sum(v[1] for v in totals.values()),
        "series": series,
        "buckets": [{"start": b.isoformat(), **v} for b, v in buckets.items()],
        # Every series, none folded, biggest first: the list under the chart.
        "ranking": [{"id": sid(raw), "name": name(raw), "spent_micro": totals[raw][0], "charges": totals[raw][1]}
                    for raw in ranked],
        "options": {
            "keys": [{"id": k, "name": names.get(k, f"Key #{k}"), "spent_micro": micro}
                     for k, micro in sorted(key_totals.items(), key=lambda kv: (-kv[1], kv[0])) if k != NOT_ATTRIBUTED],
            "providers": [{"id": p, "spent_micro": micro}
                          for p, micro in sorted(provider_totals.items(), key=lambda kv: (-kv[1], kv[0]))],
        },
    }


async def _key_names(db: AsyncSession, org_id: int, ids: list[int]) -> dict[int, str]:
    """One name for a key everywhere: what it is, then whose. A person's key is its name and its
    holder; an agent is its own name and the person who created it. The dashboard's keyLabel
    names keys the same way."""
    if not ids:
        return {}
    rows = (await db.execute(select(ApiKey.id, ApiKey.name, ApiKey.identity_label, ApiKey.kind, ApiKey.created_by).where(
        ApiKey.org_id == org_id, ApiKey.id.in_(ids)))).all()
    org = await db.get(Org, org_id)
    # An agent's identity is `agent-<team slug>-<name>@…`; the name is what the Team page shows.
    prefixes = [f"agent-{slug}-" for slug in (org.slug, org.previous_slug) if org and slug]
    out = {}
    for key, key_name, identity, kind, created_by in rows:
        who = (identity or "").split("@", 1)[0]
        if kind == "agent":
            agent = next((who[len(p):] for p in prefixes if who.startswith(p)), who) or key_name
            creator = (created_by or "").split("@", 1)[0]
            out[key] = f"{agent} · {creator}" if creator else agent
        else:
            out[key] = f"{key_name} · {who}" if who else key_name
    return out
