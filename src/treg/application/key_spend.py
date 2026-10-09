"""Billed spend per API key and per tool: the ledger's settle entries, attributed through the audit row.

The money comes only from `LedgerEntry` (kind `settle`), never from `CallRecord.cost_*`, because the
ledger is the authority and audit rows may be shed. `CallRecord` supplies only the key: its
`call_ref` is the ledger `call_id` of a metered call. A routed child (`<ref>:r1`) or an overflow
hold (`<ref>:overflow`) settles under a suffixed id whose own audit row may not exist; those fall
back to the parent ref before the part after the first colon. A charge whose call has no keyed
audit row (shed, written before key tracking, or never keyed) is reported as not attributed, so
the per-key lines and that one line always add up to the ledger total.

The tool and its provider come from the settle entry's own `endpoint_id`, so a report by tool needs
no audit row at all.
"""

from __future__ import annotations

import zlib
from datetime import date, datetime, timedelta

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from ..domain.catalog import store as catalog_store
from ..models import ApiKey, CallRecord, LedgerEntry, Org

# Audit rows are looked up by `call_ref` (indexed), in batches: the cost follows the team's METERED
# calls in the window, not every call it made, which on a busy team is most of `callrecord`.
_REF_BATCH = 500
# A chart folds everything past this many series into one "Other" (no eighth hue).
MAX_SERIES = 7
GROUPS = ("day", "week", "month")
UNATTRIBUTED = "none"
OTHER = "__other"


def window_start(now: datetime, days: int) -> datetime:
    """Today (UTC) plus the `days - 1` before it: the same window as `/orgs/{id}/usage`."""
    return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)


async def _settled(db: AsyncSession, org_id: int, since: datetime, until: datetime | None):
    """`(call_id, endpoint_id, day, micro)` per call and UTC day, over `(org_id, created_at)`."""
    day = func.date(LedgerEntry.created_at)  # 'YYYY-MM-DD' on sqlite, a date on Postgres; str() both
    query = select(LedgerEntry.call_id, LedgerEntry.endpoint_id, day,
                   func.coalesce(func.sum(-LedgerEntry.amount_micro), 0)).where(
        LedgerEntry.org_id == org_id, LedgerEntry.kind == "settle", LedgerEntry.created_at >= since)
    if until is not None:
        query = query.where(LedgerEntry.created_at < until)
    rows = (await db.execute(query.group_by(LedgerEntry.call_id, LedgerEntry.endpoint_id, day))).all()
    return [(call_id or "", endpoint_id or "", str(d), int(micro)) for call_id, endpoint_id, d, micro in rows]


async def _key_of(db: AsyncSession, org_id: int, refs: set[str], key_id: int | None = None) -> dict[str, int]:
    """The key on each call's audit row, for exactly these refs (and their parents)."""
    wanted = sorted({ref for ref in refs if ref} | {ref.split(":", 1)[0] for ref in refs if ref})
    found: dict[str, int] = {}
    for i in range(0, len(wanted), _REF_BATCH):
        query = select(CallRecord.call_ref, CallRecord.api_key_id).where(
            CallRecord.org_id == org_id, CallRecord.call_ref.in_(wanted[i:i + _REF_BATCH]),
            CallRecord.api_key_id.is_not(None))
        if key_id is not None:
            query = query.where(CallRecord.api_key_id == key_id)
        for call_ref, owner in (await db.execute(query.order_by(CallRecord.id))).all():
            found.setdefault(call_ref, int(owner))  # several rows can share a ref; the first one keys it
    return found


def _owner(key_of: dict[str, int], ref: str) -> int | None:
    owner = key_of.get(ref)
    return owner if owner is not None else key_of.get(ref.split(":", 1)[0])


async def _attributed(db: AsyncSession, org_id: int, since: datetime, until: datetime | None = None,
                      key_id: int | None = None):
    """`(key_id, endpoint_id, day, parent_ref, micro)` per settled call; `key_id` None = no keyed row.

    Given `key_id`, only that key's audit rows are read, so every other charge comes back as None.
    """
    settled = await _settled(db, org_id, since, until)
    key_of = await _key_of(db, org_id, {row[0] for row in settled}, key_id) if settled else {}
    return [(_owner(key_of, ref), endpoint_id, day, ref.split(":", 1)[0], micro)
            for ref, endpoint_id, day, micro in settled]


async def spend_by_api_key(db: AsyncSession, org_id: int, since: datetime) -> dict:
    """`{"keys": {key_id: {"spend_micro", "calls"}}, "unattributed": {"spend_micro", "calls"}}`.

    `calls` counts billed calls: distinct parent refs with a settle in the window.
    """
    spend: dict[int | None, int] = {}
    calls: dict[int | None, set[str]] = {}
    for key_id, _endpoint, _day, parent, micro in await _attributed(db, org_id, since):
        spend[key_id] = spend.get(key_id, 0) + micro
        calls.setdefault(key_id, set()).add(parent)  # an overflow hold is part of its parent's call
    return {
        "keys": {key_id: _line(micro, len(calls[key_id]))
                 for key_id, micro in spend.items() if key_id is not None},
        "unattributed": _line(spend.get(None, 0), len(calls.get(None, ()))),
    }


async def daily_spend_of_key(db: AsyncSession, org_id: int, key_id: int, since: datetime) -> list[dict]:
    """One key's billed spend per UTC day since `since`, oldest first; days without a charge are
    absent (the caller draws the empty days)."""
    spend: dict[str, int] = {}
    calls: dict[str, set[str]] = {}
    for owner, _endpoint, day, parent, micro in await _attributed(db, org_id, since, key_id=key_id):
        if owner != key_id:
            continue
        spend[day] = spend.get(day, 0) + micro
        calls.setdefault(day, set()).add(parent)
    return [{"day": day, **_line(spend[day], len(calls[day]))} for day in sorted(spend)]


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
    ever share a color.
    """
    taken: dict[int, str] = {}
    out: dict[str, int] = {}
    for sid in ids:
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


def _endpoint(endpoint_id: str, endpoints: dict) -> tuple[str, str]:
    """`(provider, tool name)` for a settled endpoint id; a retired id keeps its own prefix."""
    ep = endpoints.get(endpoint_id) or {}
    provider = ep.get("provider") or (endpoint_id.split(".", 1)[0] if endpoint_id else "")
    # Stacked within one provider, its own prefix on every tool is noise.
    bare = endpoint_id[len(provider) + 1:] if provider and endpoint_id.startswith(provider + ".") else endpoint_id
    return provider or "unknown", ep.get("name") or bare or "unknown"


async def spend_report(
    db: AsyncSession, org_id: int, first: date, last: date, *,
    group: str = "day", key_id: int | None = None, provider: str | None = None, stack: str = "key",
) -> dict:
    """The team's billed spend between two UTC days inclusive, bucketed and stacked for a chart.

    Filters (`key_id`, `provider`) narrow the money; the `options` lists are computed before them, so
    a filter never hides its own alternatives. `stack` is `key` (one series per API key, plus the
    unattributed money) or `tool` (one per endpoint). The biggest `MAX_SERIES` series keep their own
    line and the rest fold into Other, so every series keeps the color of its slot.
    """
    since = datetime.combine(first, datetime.min.time())
    until = datetime.combine(last + timedelta(days=1), datetime.min.time())
    endpoints = catalog_store.load().by_id
    rows = []
    key_totals: dict[int | None, int] = {}
    provider_totals: dict[str, int] = {}
    for owner, endpoint_id, day, parent, micro in await _attributed(db, org_id, since, until):
        prov, tool = _endpoint(endpoint_id, endpoints)
        key_totals[owner] = key_totals.get(owner, 0) + micro
        provider_totals[prov] = provider_totals.get(prov, 0) + micro
        if (key_id is None or owner == key_id) and (provider is None or prov == provider):
            rows.append((owner, endpoint_id, tool, date.fromisoformat(day[:10]), parent, micro))

    def series_of(row) -> str:
        if stack == "tool":
            return row[1] or "unknown"
        return UNATTRIBUTED if row[0] is None else str(row[0])

    totals: dict[str, int] = {}
    series_calls: dict[str, set[str]] = {}
    for row in rows:
        sid = series_of(row)
        totals[sid] = totals.get(sid, 0) + row[5]
        series_calls.setdefault(sid, set()).add(row[4])
    ranked = sorted(totals, key=lambda sid: (-totals[sid], sid))
    kept = set(ranked[:MAX_SERIES]) if len(ranked) > MAX_SERIES + 1 else set(ranked)

    # `parts` draws the bar; `others` breaks the Other slice down for its tooltip.
    buckets = {b: {"parts": {}, "others": {}, "calls": set()} for b in _buckets(first, last, group)}
    for row in rows:
        sid = series_of(row)
        bucket = buckets.get(bucket_of(row[3], group))
        if bucket is None:
            continue
        if sid not in kept:
            bucket["others"][sid] = bucket["others"].get(sid, 0) + row[5]
            sid = OTHER
        bucket["parts"][sid] = bucket["parts"].get(sid, 0) + row[5]
        bucket["calls"].add(row[4])

    names = await _key_names(db, org_id, [k for k in key_totals if k is not None])
    tool_names = {row[1]: row[2] for row in rows}

    def name(sid: str) -> str:
        if stack == "tool":
            return tool_names.get(sid, sid)
        return "No API key" if sid == UNATTRIBUTED else names.get(int(sid), f"Key #{sid}")

    shown = [sid for sid in ranked if sid in kept]
    slots = color_slots(shown)
    series = [{"id": sid, "name": name(sid), "spend_micro": totals[sid], "slot": slots[sid]} for sid in shown]
    folded = [sid for sid in ranked if sid not in kept]
    if folded:
        series.append({"id": OTHER, "name": "Other", "spend_micro": sum(totals[s] for s in folded), "members": len(folded)})
    return {
        "from": first.isoformat(), "to": last.isoformat(), "group": group, "stack": stack,
        "spend_micro": sum(totals.values()),
        "calls": len({row[4] for row in rows}),
        "series": series,
        "buckets": [{"start": b.isoformat(), "parts": v["parts"], "calls": len(v["calls"]),
                     **({"others": v["others"]} if v["others"] else {})}
                    for b, v in buckets.items()],
        # Every series, none folded, biggest first: the list under the chart and the CSV.
        "ranking": [{"id": sid, "name": name(sid), "spend_micro": totals[sid], "calls": len(series_calls[sid])}
                    for sid in ranked],
        "options": {
            "keys": [{"id": k, "name": names.get(k, f"Key #{k}"), "spend_micro": micro}
                     for k, micro in sorted(key_totals.items(), key=lambda kv: -kv[1]) if k is not None],
            "providers": [{"id": p, "spend_micro": micro}
                          for p, micro in sorted(provider_totals.items(), key=lambda kv: -kv[1])],
        },
    }


async def _key_names(db: AsyncSession, org_id: int, ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    rows = (await db.execute(select(ApiKey.id, ApiKey.name, ApiKey.identity_label, ApiKey.kind, ApiKey.created_by).where(
        ApiKey.org_id == org_id, ApiKey.id.in_(ids)))).all()
    org = await db.get(Org, org_id)
    # An agent's identity is `agent-<team slug>-<name>@…`; the name is what the Team page shows.
    prefixes = [f"agent-{slug}-" for slug in (org.slug, org.previous_slug) if org and slug]
    out = {}
    # One name for a key everywhere: what it is, then whose. A person's key is its name and its
    # holder; an agent is its own name and the person who created it. The dashboard's keyLabel
    # names keys the same way.
    for key, key_name, identity, kind, created_by in rows:
        who = (identity or "").split("@", 1)[0]
        if kind == "agent":
            agent = next((who[len(p):] for p in prefixes if who.startswith(p)), who) or key_name
            creator = (created_by or "").split("@", 1)[0]
            out[key] = f"{agent} · {creator}" if creator else agent
        else:
            out[key] = f"{key_name} · {who}" if who else key_name
    return out


def _line(spend_micro: int, calls: int) -> dict:
    return {"spend_micro": spend_micro, "calls": calls}
