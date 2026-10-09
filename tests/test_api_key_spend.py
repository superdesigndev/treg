"""Billed spend per API key: money from the ledger, the key from the audit row's call_ref."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from treg import crypto
from treg.application import key_spend
from treg.infra.db import session_maker
from treg.models import ApiKey, CallRecord, LedgerEntry, Membership, Org, User
from treg.timeutil import utcnow_naive


def _h(token: str) -> dict[str, str]:
    return {"X-Treg-Token": token}


def _entry(org_id: int, call_id: str, kind: str, amount: int, at: datetime, endpoint: str = "") -> LedgerEntry:
    return LedgerEntry(id=crypto.new_token()[:32], org_id=org_id, kind=kind, amount_micro=amount,
                       call_id=call_id, endpoint_id=endpoint or None, created_at=at)


def _record(org_id: int, call_ref: str, key_id: int | None, at: datetime) -> CallRecord:
    # A cost the ledger contradicts, so a total read from the audit table would be visibly wrong.
    return CallRecord(org_id=org_id, user_email="tim@superdesign.dev", tool_name="t", method="GET",
                      path="/", status_code=200, call_ref=call_ref, api_key_id=key_id,
                      cost_charged_micro=999_999, created_at=at)


async def test_spend_is_settled_ledger_money_matched_by_call_ref(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    now = datetime(2026, 10, 9, 12)
    recent, older, long_ago = datetime(2026, 10, 5), datetime(2026, 9, 20), datetime(2026, 8, 1)
    async with session_maker() as db:
        other = Org(name="Other", slug="other-spend")
        db.add(other)
        await db.flush()
        db.add_all([
            # key 1: one call inside 7 days, one inside only 30 days, one outside both windows
            _record(org_id, "c1", 1, recent), _entry(org_id, "c1", "reserve", -1_500, recent),
            _entry(org_id, "c1", "settle", -1_000, recent),
            _record(org_id, "c2", 1, older), _entry(org_id, "c2", "settle", -500, older),
            _record(org_id, "old", 1, long_ago), _entry(org_id, "old", "settle", -7_000, long_ago),
            # an overflow hold settles under a suffixed id; its parent's row names the key
            _entry(org_id, "c1:overflow", "settle", -20, recent),
            # key 2: a released call costs nothing; two audit rows for one call count it once
            _record(org_id, "c3", 2, recent), _record(org_id, "c3", 2, recent),
            _entry(org_id, "c3", "settle", -300, recent),
            _record(org_id, "c4", 2, recent), _entry(org_id, "c4", "reserve", -800, recent),
            _entry(org_id, "c4", "release", 800, recent),
            # not attributed: no audit row at all, and an audit row with no key
            _entry(org_id, "lost", "settle", -50, recent),
            _record(org_id, "keyless", None, older), _entry(org_id, "keyless", "settle", -5, older),
            # another team's money under a colliding ref is not this team's
            _entry(other.id, "c1", "settle", -100_000, recent),
            _entry(org_id, None, "grant", 1_000_000, recent),
        ])
        await db.commit()

        month = await key_spend.spend_by_api_key(db, org_id, key_spend.window_start(now, 30))
        week = await key_spend.spend_by_api_key(db, org_id, key_spend.window_start(now, 7))

    # c1 and its overflow hold are one call; c3's two audit rows are one call
    assert month == {
        "keys": {1: {"spend_micro": 1_520, "calls": 2}, 2: {"spend_micro": 300, "calls": 1}},
        "unattributed": {"spend_micro": 55, "calls": 2},
    }
    assert week == {
        "keys": {1: {"spend_micro": 1_020, "calls": 1}, 2: {"spend_micro": 300, "calls": 1}},
        "unattributed": {"spend_micro": 50, "calls": 1},
    }


async def test_route_shows_admin_every_key_and_member_only_their_own(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    admin_key = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "Admin CI"})).json()["id"]
    member_token = crypto.new_token()
    async with session_maker() as db:
        member = User(email="spender@example.dev")
        db.add(member)
        await db.flush()
        db.add(Membership(user_id=member.id, org_id=org_id, role="member",
                          token_hash=crypto.hash_token(member_token)))
        await db.commit()
    member_key = (await clients.post(
        f"/orgs/{org_id}/api-keys", headers=_h(member_token), json={"name": "Member CI"},
    )).json()["id"]
    now = utcnow_naive()
    async with session_maker() as db:
        db.add_all([
            _record(org_id, "a", admin_key, now), _entry(org_id, "a", "settle", -2_000, now),
            _record(org_id, "m", member_key, now), _entry(org_id, "m", "settle", -3_000, now),
            _entry(org_id, "x", "settle", -40, now),
        ])
        await db.commit()

    seen = (await clients.get(f"/orgs/{org_id}/api-keys/spend")).json()
    assert seen["days"] == 30
    assert [(row["id"], row["name"], row["spend_micro"], row["calls"]) for row in seen["keys"]] == [
        (member_key, "Member CI", 3_000, 1), (admin_key, "Admin CI", 2_000, 1)]
    assert seen["unattributed"] == {"spend_micro": 40, "calls": 1}

    own = (await clients.get(f"/orgs/{org_id}/api-keys/spend?days=7", headers=_h(member_token))).json()
    assert own["days"] == 7
    assert [(row["id"], row["spend_micro"]) for row in own["keys"]] == [(member_key, 3_000)]
    assert own["unattributed"] is None

    # a revoked key keeps its line: the money it spent is still the team's
    assert (await clients.post(f"/orgs/{org_id}/api-keys/{admin_key}/revoke")).status_code == 200
    revoked = (await clients.get(f"/orgs/{org_id}/api-keys/spend")).json()["keys"]
    assert [(row["id"], row["state"]) for row in revoked] == [(member_key, "active"), (admin_key, "revoked")]

    assert (await clients.get(f"/orgs/{org_id + 1}/api-keys/spend")).status_code == 403


async def test_daily_spend_is_one_keys_charges_per_day_for_its_holder_or_an_admin(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    mine = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "Mine"})).json()["id"]
    other = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "Other"})).json()["id"]
    member_token = crypto.new_token()
    async with session_maker() as db:
        member = User(email="peeker@example.dev")
        db.add(member)
        await db.flush()
        db.add(Membership(user_id=member.id, org_id=org_id, role="member",
                          token_hash=crypto.hash_token(member_token)))
        await db.commit()
    today = utcnow_naive().replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    async with session_maker() as db:
        db.add_all([
            _record(org_id, "a", mine, yesterday), _entry(org_id, "a", "settle", -100, yesterday),
            _record(org_id, "b", mine, today), _entry(org_id, "b", "settle", -200, today),
            _entry(org_id, "b:overflow", "settle", -5, today),
            _record(org_id, "c", mine, today), _entry(org_id, "c", "settle", -300, today),
            _record(org_id, "d", other, today), _entry(org_id, "d", "settle", -9_000, today),
            _entry(org_id, "lost", "settle", -1, today),
        ])
        await db.commit()

    got = (await clients.get(f"/orgs/{org_id}/api-keys/{mine}/spend?days=30")).json()
    assert got["by_day"] == [
        {"day": yesterday.date().isoformat(), "spend_micro": 100, "calls": 1},
        {"day": today.date().isoformat(), "spend_micro": 505, "calls": 2},
    ]
    assert (await clients.get(
        f"/orgs/{org_id}/api-keys/{mine}/spend", headers=_h(member_token),
    )).status_code == 403
    assert (await clients.get(f"/orgs/{org_id}/api-keys/999999/spend")).status_code == 404


async def test_spend_report_buckets_stacks_and_filters_ledger_money(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    ci = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "CI"})).json()["id"]
    laptop = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "Laptop"})).json()["id"]
    mon, tue, next_mon = datetime(2026, 9, 7, 9), datetime(2026, 9, 8, 9), datetime(2026, 9, 14, 9)
    async with session_maker() as db:
        db.add_all([
            _record(org_id, "a", ci, mon), _entry(org_id, "a", "settle", -100, mon, "acme.search"),
            _record(org_id, "b", ci, tue), _entry(org_id, "b", "settle", -200, tue, "acme.extract"),
            _record(org_id, "c", laptop, next_mon), _entry(org_id, "c", "settle", -400, next_mon, "other.lookup"),
            _entry(org_id, "d", "settle", -8, tue, "acme.search"),  # no audit row: not attributed
        ])
        await db.commit()

        by_day = await key_spend.spend_report(db, org_id, date(2026, 9, 7), date(2026, 9, 14))
        by_week = await key_spend.spend_report(db, org_id, date(2026, 9, 7), date(2026, 9, 14), group="week")
        acme = await key_spend.spend_report(db, org_id, date(2026, 9, 7), date(2026, 9, 14),
                                            provider="acme", stack="tool")
        ci_only = await key_spend.spend_report(db, org_id, date(2026, 9, 7), date(2026, 9, 14), key_id=ci)

    assert by_day["spend_micro"] == 708 and by_day["calls"] == 4
    assert [b["start"] for b in by_day["buckets"]][:2] == ["2026-09-07", "2026-09-08"]
    assert len(by_day["buckets"]) == 8  # every day of the range, the empty ones too
    assert [(s["id"], s["spend_micro"]) for s in by_day["series"]] == [
        (str(laptop), 400), (str(ci), 300), ("none", 8)]
    assert by_day["series"][2]["name"] == "No API key"
    assert [b["parts"] for b in by_week["buckets"]] == [
        {str(ci): 300, "none": 8}, {str(laptop): 400}]
    # Within one provider, by tool; the option lists still name everything, unfiltered.
    assert [(s["id"], s["name"], s["spend_micro"]) for s in acme["series"]] == [
        ("acme.extract", "extract", 200), ("acme.search", "search", 108)]
    assert [p["id"] for p in acme["options"]["providers"]] == ["other", "acme"]
    assert ci_only["spend_micro"] == 300 and {k["id"] for k in ci_only["options"]["keys"]} == {ci, laptop}


async def test_spend_report_folds_past_seven_series_into_other(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    at = datetime(2026, 9, 7, 9)
    async with session_maker() as db:
        db.add_all([_entry(org_id, f"x{i}", "settle", -(100 + i), at, f"acme.tool{i}") for i in range(9)])
        await db.commit()
        report = await key_spend.spend_report(db, org_id, date(2026, 9, 7), date(2026, 9, 7),
                                              provider="acme", stack="tool")
    assert len(report["series"]) == 8 and report["series"][-1]["id"] == "__other"
    assert report["series"][-1]["spend_micro"] == 100 + 101 and report["series"][-1]["members"] == 2
    assert report["buckets"][0]["others"] == {"acme.tool0": 100, "acme.tool1": 101}
    assert len({s["slot"] for s in report["series"][:-1]}) == 7  # no two shown series share a color
    assert [r["id"] for r in report["ranking"]] == [f"acme.tool{i}" for i in range(8, -1, -1)]
    assert sum(report["buckets"][0]["parts"].values()) == report["spend_micro"]


async def test_spend_route_validates_its_window_and_is_admin_only(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    ok = await clients.get(f"/orgs/{org_id}/usage/spend?from=2026-09-01&to=2026-09-03&group=week")
    assert ok.status_code == 200, ok.text
    assert (ok.json()["from"], ok.json()["to"]) == ("2026-09-01", "2026-09-03")
    async with session_maker() as db:
        db.add(_entry(org_id, "t1", "settle", -50, utcnow_naive(), "tavily.search"))
        await db.commit()
    named = (await clients.get(f"/orgs/{org_id}/usage/spend?days=7")).json()["options"]["providers"]
    assert named == [{"id": "tavily", "name": "Tavily", "spend_micro": 50}]
    for bad in ("from=2026-09-05&to=2026-09-01", "from=nope", "from=2025-01-01&to=2026-09-01",
                "group=year", "stack=tool"):
        assert (await clients.get(f"/orgs/{org_id}/usage/spend?{bad}")).status_code == 422, bad
    usage = (await clients.get(f"/orgs/{org_id}/usage?from=2026-09-01&to=2026-09-03")).json()
    assert (usage["from"], usage["to"], usage["days"]) == ("2026-09-01", "2026-09-03", 3)
    member_token = crypto.new_token()
    async with session_maker() as db:
        member = User(email="viewer-of-spend@example.dev")
        db.add(member)
        await db.flush()
        db.add(Membership(user_id=member.id, org_id=org_id, role="member",
                          token_hash=crypto.hash_token(member_token)))
        await db.commit()
    assert (await clients.get(f"/orgs/{org_id}/usage/spend", headers=_h(member_token))).status_code == 403


def test_a_series_keeps_its_color_slot_whatever_else_is_shown():
    alone = key_spend.color_slots(["12"])
    crowded = key_spend.color_slots(["5", "19", "12", "none"])
    assert alone["12"] == crowded["5"] == 5  # 12 % 7 == 5 == 19 % 7: the bigger spender keeps it
    assert crowded["19"] == 6 and crowded["12"] == 0 and crowded["none"] == 3
    assert len(set(crowded.values())) == 4


async def test_keys_are_named_by_what_they_are_then_whose(clients):
    org_id = (await clients.get("/auth/me")).json()["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        human = ApiKey(org_id=org_id, identity_label="tim@superdesign.dev", kind="additional_human", name="Laptop")
        agent = ApiKey(org_id=org_id, identity_label=f"agent-{org.slug}-scout@agents.example",
                       kind="agent", name="Agent key", created_by="tim@superdesign.dev")
        db.add_all([human, agent])
        await db.flush()
        names = await key_spend._key_names(db, org_id, [human.id, agent.id])
    assert names == {human.id: "Laptop · tim", agent.id: "scout · tim"}
