"""The Team page's "Spent last 30 days" column: `GET /orgs/{id}/api-keys/spend`, read from the rollup.

What these pin: an admin sees every key and the team's not-attributed line; a member sees only
their own keys; another team's spend never shows; nothing shows before the rollup has caught up;
and a page open is a small bounded read of the rollup, never the ledger or the audit table, however
large the team.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

from treg import crypto
from treg.application import key_spend_rollup as rollup
from treg.infra import db as infra_db
from treg.infra.db import session_maker
from treg.models import ApiKeySpendCursor, ApiKeySpendDay, Membership, Org, User
from treg.timeutil import utcnow_naive


def _h(token: str) -> dict[str, str]:
    return {"X-Treg-Token": token}


def _day(days_ago: int) -> str:
    return (utcnow_naive() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _row(org_id: int, days_ago: int, key_id: int, micro: int, endpoint: str = "exa.search", charges: int = 1):
    return ApiKeySpendDay(org_id=org_id, day=_day(days_ago), api_key_id=key_id, endpoint_id=endpoint,
                          spent_micro=micro, charges=charges, updated_at=utcnow_naive())


async def _add(*rows) -> None:
    async with session_maker() as db:
        db.add_all(rows)
        await db.commit()


async def _caught_up() -> None:
    now = utcnow_naive()
    await _add(ApiKeySpendCursor(id=rollup.CURSOR_ID, watermark_at=now, history_from=_day(30), caught_up_at=now))


async def _member(org_id: int, email: str, role: str = "member") -> str:
    token = crypto.new_token()
    async with session_maker() as db:
        user = User(email=email)
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, org_id=org_id, role=role, token_hash=crypto.hash_token(token)))
        await db.commit()
    return token


@contextmanager
def _statements():
    seen: list[str] = []

    def record(conn, cursor, statement, *args):
        seen.append(statement.lower())
    engines = list({id(e.sync_engine): e.sync_engine for e in infra_db._engines}.values())
    for engine in engines:
        event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        for engine in engines:
            event.remove(engine, "before_cursor_execute", record)


async def test_admin_sees_every_key_and_member_only_their_own(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    admin_key = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "Admin CI"})).json()["id"]
    member_token = await _member(org_id, "spender@example.dev")
    member_key = (await clients.post(f"/orgs/{org_id}/api-keys", headers=_h(member_token),
                                     json={"name": "Member CI"})).json()["id"]
    async with session_maker() as db:
        other = Org(name="Other", slug="other-spend")
        db.add(other)
        await db.commit()
    await _add(
        _row(org_id, 0, admin_key, 2_000), _row(org_id, 3, admin_key, 500, "tavily.search"),
        _row(org_id, 1, member_key, 3_000, charges=2),
        _row(org_id, 2, rollup.NOT_ATTRIBUTED, 40),
        _row(org_id, 31, admin_key, 7_000),                # before the 30-day window
        _row(other.id, 0, admin_key, 100_000),             # another team's row under a colliding id
        _row(other.id, 0, rollup.NOT_ATTRIBUTED, 100_000),
    )

    before = (await clients.get(f"/orgs/{org_id}/api-keys/spend")).json()
    assert before["ready"] is False and before["keys"] == [] and before["unattributed"] is None

    await _caught_up()
    seen = (await clients.get(f"/orgs/{org_id}/api-keys/spend")).json()
    assert seen["ready"] and seen["days"] == 30 and seen["since"] == _day(29)
    assert seen["keys"] == [{"id": member_key, "spent_micro": 3_000, "charges": 2},
                            {"id": admin_key, "spent_micro": 2_500, "charges": 2}]
    assert seen["unattributed"] == {"spent_micro": 40, "charges": 1}

    own = (await clients.get(f"/orgs/{org_id}/api-keys/spend", headers=_h(member_token))).json()
    assert own["keys"] == [{"id": member_key, "spent_micro": 3_000, "charges": 2}]
    assert own["unattributed"] is None

    assert (await clients.get(f"/orgs/{other.id}/api-keys/spend")).status_code == 403


async def test_a_large_team_is_a_small_bounded_read_of_the_rollup(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    await _caught_up()
    keys, tools, days = 120, 6, 31
    await _add(*[_row(org_id, d, k, 10 + k, f"tool{t}.x")
                 for k in range(1, keys + 1) for t in range(tools) for d in range(days)])

    with _statements() as seen:
        got = (await clients.get(f"/orgs/{org_id}/api-keys/spend")).json()

    assert len(got["keys"]) == keys
    assert got["keys"][0] == {"id": keys, "spent_micro": (10 + keys) * tools * 30, "charges": tools * 30}
    touched = [s for s in seen if "apikeyspend" in s]
    assert len(touched) == 2      # the cursor, then one grouped read of the rollup
    assert not [s for s in seen if "ledgerentry" in s or "callrecord" in s]


# The Usage page's spend chart: `GET /orgs/{id}/usage/spend`, read from the same rollup.

async def test_the_chart_buckets_stacks_and_folds_from_the_rollup(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    keys = [(await clients.post(f"/orgs/{org_id}/api-keys", json={"name": f"K{i}"})).json()["id"] for i in range(10)]
    async with session_maker() as db:
        other = Org(name="Other", slug="other-chart")
        db.add(other)
        await db.commit()
    await _caught_up()
    await _add(
        *[_row(org_id, 1, key, 1_000 * (i + 1)) for i, key in enumerate(keys)],
        _row(org_id, 2, keys[9], 500, "tavily.search", charges=3),
        _row(org_id, 2, rollup.NOT_ATTRIBUTED, 50_000),
        _row(org_id, 40, keys[0], 9_999_999),                   # before the rollup's history
        _row(other.id, 1, keys[0], 777_777),                     # another team's money
    )

    got = (await clients.get(f"/orgs/{org_id}/usage/spend")).json()
    assert got["ready"] and got["history_from"] == _day(30) and got["from"] == _day(29) and got["to"] == _day(0)
    assert got["spent_micro"] == 55_000 + 50_000 + 500 and got["charges"] == 14
    # The biggest seven keep their own series; the other four fold into Other.
    assert [s["id"] for s in got["series"]] == ["none", str(keys[9]), *[str(k) for k in keys[8:3:-1]], "__other"]
    assert got["series"][-1] == {"id": "__other", "name": "Other", "spent_micro": 10_000, "members": 4}
    assert len({s["slot"] for s in got["series"][:-1]}) == 7
    assert len(got["ranking"]) == 11 and got["ranking"][0]["name"] == "No API key"
    yesterday = next(b for b in got["buckets"] if b["start"] == _day(1))
    assert yesterday["parts"]["__other"] == 10_000 and yesterday["charges"] == 10
    assert 777_777 not in [s["spent_micro"] for s in got["series"]]

    # Asked from further back than the rollup holds: the range starts at its history.
    old = (await clients.get(f"/orgs/{org_id}/usage/spend?from={_day(90)}&to={_day(0)}&group=month")).json()
    assert old["from"] == _day(30) and old["spent_micro"] == got["spent_micro"]

    # One provider, stacked by tool, one key: filters narrow the money, never the options.
    tools = (await clients.get(f"/orgs/{org_id}/usage/spend?provider=tavily&stack=tool&key={keys[9]}")).json()
    assert tools["spent_micro"] == 500 and tools["charges"] == 3
    assert [s["id"] for s in tools["series"]] == ["tavily.search"]
    assert {p["id"] for p in tools["options"]["providers"]} == {"exa", "tavily"}
    assert len(tools["options"]["keys"]) == 10

    assert (await clients.get(f"/orgs/{org_id}/usage/spend?stack=tool")).status_code == 422
    member = await _member(org_id, "chart-member@example.dev")
    assert (await clients.get(f"/orgs/{org_id}/usage/spend", headers=_h(member))).status_code == 403
    assert (await clients.get(f"/orgs/{other.id}/usage/spend")).status_code == 403


async def test_the_chart_waits_for_the_rollup(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    assert (await clients.get(f"/orgs/{org_id}/usage/spend")).json() == {"ready": False, "history_from": None}


async def test_the_chart_for_a_large_team_is_a_small_bounded_read_of_the_rollup(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    await _caught_up()
    keys, tools, days = 120, 6, 31
    await _add(*[_row(org_id, d, k, 10 + k, f"tool{t}.x")
                 for k in range(1, keys + 1) for t in range(tools) for d in range(days)])

    with _statements() as seen:
        got = (await clients.get(f"/orgs/{org_id}/usage/spend?from={_day(30)}")).json()

    assert len(got["ranking"]) == keys and len(got["series"]) == 8
    assert all(len(b["parts"]) == 8 for b in got["buckets"])
    assert got["spent_micro"] == sum(10 + k for k in range(1, keys + 1)) * tools * days
    touched = [s for s in seen if "apikeyspend" in s]
    assert len(touched) == 5      # the cursor, key totals, tool totals, series totals, folded buckets
    assert not [s for s in seen if "ledgerentry" in s or "callrecord" in s]


def test_color_slots_never_spins_with_more_ids_than_slots():
    from treg.application import key_spend
    for ids in (["none", *map(str, range(1, 20))], [f"tool{i}.x" for i in range(30)], ["7"] * 3 + ["14", "21"]):
        slots = key_spend.color_slots(ids)
        assert len(slots) <= key_spend.MAX_SERIES
        assert len(set(slots.values())) == len(slots)
        assert set(slots.values()) <= set(range(key_spend.MAX_SERIES))


@pytest.mark.parametrize("n, kept, folded", [(7, 7, 0), (8, 7, 1), (9, 7, 2)])
async def test_the_chart_keeps_seven_series_and_folds_the_rest_even_one(clients, n, kept, folded):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    await _caught_up()
    # Key ids 7, 14, 21, ... all prefer the same color slot: the case that once spun forever.
    await _add(*[_row(org_id, 1, 7 * (i + 1), 1_000 * (i + 1)) for i in range(n)])

    got = (await asyncio.wait_for(clients.get(f"/orgs/{org_id}/usage/spend"), timeout=10)).json()

    own = [s for s in got["series"] if s["id"] != "__other"]
    other = [s for s in got["series"] if s["id"] == "__other"]
    assert len(own) == kept and len({s["slot"] for s in own}) == kept
    assert [o["members"] for o in other] == ([folded] if folded else [])
    assert len(got["ranking"]) == n
    assert all(len(b["parts"]) <= 8 for b in got["buckets"])
