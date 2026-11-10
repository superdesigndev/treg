"""The /showcase leaderboard: teams apply, staff review, the public board shows only what was
approved and consented, and people upvote.

What these pin: an owner or admin applies for their team, at most five entries per team; nothing is
public before approval, and approval publishes the pictures before the row points at them; an edit
to a live entry leaves the live version until it is approved; a rejected application can be fixed
and resubmitted; the public JSON never carries a team, an exact call count or money; the weekly fold
counts exactly a team's upstream calls; one upvote per person per app; none of it on a self-hosted
registry.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from fake_media_store import MemoryMediaStore
from treg.api import app
from treg.application import images, showcase
from treg.config import get_settings
from treg.domain import showcase as rules
from treg.domain.identity import session
from treg.infra.db import session_maker
from treg.models import (ApiKey, CallRecord, Membership, Org, ShowcaseApp, ShowcaseAppEvent, ShowcaseAppKey, ShowcaseCursor,
                         ShowcaseVote, ShowcaseWeek, User)
from treg.timeutil import utcnow_naive

TODAY = utcnow_naive().date()
NOW = datetime(TODAY.year, TODAY.month, TODAY.day, 12)
NEWEST = rules.last_complete_week(TODAY)
ADMIN = "showcase-staff-token-for-tests"


@pytest.fixture
def store():
    st = MemoryMediaStore()
    images.use_store(st)
    yield st
    images.use_store(None)


@pytest.fixture
def staff(monkeypatch):
    monkeypatch.setenv("TREG_ADMIN_TOKEN", ADMIN)
    get_settings.cache_clear()
    yield {"X-Treg-Token": ADMIN}
    get_settings.cache_clear()


def picture(w=300, h=300) -> bytes:
    out = BytesIO()
    Image.new("RGB", (w, h), (20, 120, 200)).save(out, "PNG")
    return out.getvalue()


async def _tim() -> tuple[User, int]:
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "tim@superdesign.dev"))).scalar_one()
        org_id = (await db.execute(select(Membership.org_id).where(Membership.user_id == user.id))).scalars().first()
        return user, org_id


def _cookie(client, user: User) -> None:
    client.headers.pop("X-Treg-Token", None)
    client.cookies.set(session.COOKIE, session.make_session(user.id, token_version=user.token_version))


def fields(**over) -> dict:
    return {"name": "Example App", "url": "https://example.com", "tagline": "Does one thing well",
            "description": "A longer description.", "category": "ads", "maker_name": "Ada",
            "x_handle": "@ada", "show_calls": True, "show_streak": True, **over}


# ---- the rules, alone ---------------------------------------------------------------------------

def _week(start, calls, days=(), providers=(), categories=None):
    return rules.Week(week_start=start, calls=calls, active_days=sum(1 << d for d in days),
                      providers=frozenset(providers), categories=categories or {})


def test_tiers_streaks_growth_and_rising():
    prev = NEWEST - timedelta(days=7)
    act = rules.activity([_week(prev, 400, (4, 5, 6), {"a", "b"}, {"search": 400}),
                          _week(NEWEST, 900, (0, 1, 2, 3), {"b", "c"}, {"search": 100, "contacts": 800})], NEWEST)
    assert (act.calls, act.active_days, act.providers, act.streak_days) == (1300, 7, 3, 7)
    assert act.categories == ["contacts", "search"] and act.growth_pct == 125 and act.rising
    small = rules.activity([_week(prev, 10, (6,)), _week(NEWEST, 400, (0,))], NEWEST)
    assert small.growth_pct == 3900 and not small.rising


def test_an_application_is_checked_field_by_field():
    clean = rules.clean_revision(rules.split_submission(fields(name="  Spaced   Name  ", x_handle="@ada"))[0])
    assert clean["name"] == "Spaced Name" and clean["x_handle"] == "ada" and clean["shot_keys"] == []
    for bad in ({"url": "http://example.com"}, {"url": "javascript:alert(1)"}, {"category": "nope"},
                {"name": ""}, {"tagline": "x" * 81}, {"x_handle": "not a handle"},
                {"launch_post_url": "ftp://x"}, {"shot_keys": ["a"] * 5}, {"surprise": 1}):
        with pytest.raises(rules.ApplicationInvalid):
            rules.clean_revision(rules.split_submission(fields(**bad))[0])


def test_public_entry_shows_only_consented_buckets():
    act = rules.Activity(calls=123_456, active_days=20, providers=12, categories=["search"],
                         streak_days=9, growth_pct=73, rising=True)
    assert rules.public_entry(act, rules.Consent(), 3) == {"upvotes": 3, "badges": []}
    full = rules.public_entry(act, rules.Consent(True, True, True, True), 0)
    assert full["calls_tier"] == "100k+" and full["growth_pct"] == 70 and "123456" not in json.dumps(full)


# ---- applying and review ------------------------------------------------------------------------

async def _upload(client, kind, data=None) -> str:
    r = await client.post(f"/images/{kind}", content=data or picture(1600 if kind == "showcase_shot" else 300,
                                                                     1000 if kind == "showcase_shot" else 300))
    assert r.status_code == 200, r.text
    return r.json()["key"]


async def test_apply_review_approve_and_nothing_public_before(clients, store, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    teams = (await clients.get("/showcase/my-teams")).json()
    assert any(t["id"] == org_id and t["limit"] == 5 for t in teams)
    icon, shot = await _upload(clients, "showcase_icon"), await _upload(clients, "showcase_shot")
    applied = await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(icon_key=icon, shot_keys=[shot]))
    assert applied.status_code == 200, applied.text
    entry = applied.json()
    assert entry["status"] == "pending" and entry["live"] is None
    assert entry["pending"]["icon_url"] == f"/images/staging/{icon}"
    assert store.keys("public") == []
    assert (await clients.get("/showcase/leaderboard")).json()["apps"] == []

    queue = (await clients.get("/admin/showcase/apps?status=review", headers=staff)).json()
    assert [a["id"] for a in queue] == [entry["id"]] and queue[0]["team"]["id"] == org_id
    stale = await clients.post(f"/admin/showcase/apps/{entry['id']}/approve", headers=staff,
                               json={"expected_version": 0, "actor": "ops@superdesign.dev"})
    assert stale.status_code == 409
    approved = await clients.post(f"/admin/showcase/apps/{entry['id']}/approve", headers=staff,
                                  json={"expected_version": entry["version"], "actor": "ops@superdesign.dev"})
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved" and approved.json()["pending"] is None
    assert icon in store.keys("public") and shot in store.keys("public")
    assert shot.replace(".webp", "-640.webp") in store.keys("public")
    board = (await clients.get("/showcase/leaderboard")).json()
    (pub,) = board["apps"]
    assert pub["name"] == "Example App" and pub["icon_url"] == f"/media-files/{icon}"
    assert pub["shots"][0]["small_url"].endswith("-640.webp")
    async with session_maker() as db:   # approval counts nothing; the worker's next run does
        assert (await db.execute(select(ShowcaseWeek).where(ShowcaseWeek.org_id == org_id))).scalars().all() == []
    # Once approved, nothing waits for review any more.
    assert (await clients.get("/admin/showcase/apps?status=review", headers=staff)).json() == []
    detail = (await clients.get(f"/admin/showcase/apps/{entry['id']}", headers=staff)).json()
    assert [h["action"] for h in detail["history"]] == ["approved", "applied"]
    assert detail["history"][0]["actor"] == "admin:ops@superdesign.dev"


async def test_an_edit_to_a_live_app_waits_for_review(clients, store, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    entry = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields())).json()
    entry = (await clients.post(f"/admin/showcase/apps/{entry['id']}/approve", headers=staff,
                                json={"expected_version": entry["version"]})).json()
    edited = await clients.patch(f"/showcase/apps/{entry['id']}",
                                 json={"expected_version": entry["version"], "fields": fields(name="Renamed")})
    assert edited.status_code == 200
    assert edited.json()["live"]["name"] == "Example App" and edited.json()["pending"]["name"] == "Renamed"
    assert (await clients.get("/showcase/leaderboard")).json()["apps"][0]["name"] == "Example App"
    rejected = await clients.post(f"/admin/showcase/apps/{entry['id']}/reject", headers=staff,
                                  json={"expected_version": edited.json()["version"], "reason": "Name taken"})
    assert rejected.json()["status"] == "approved" and rejected.json()["pending"] is None
    assert rejected.json()["reject_reason"] == "Name taken"
    assert (await clients.get("/showcase/leaderboard")).json()["apps"][0]["name"] == "Example App"


async def test_a_rejected_application_can_be_fixed_and_resubmitted(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    entry = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields())).json()
    assert (await clients.post(f"/admin/showcase/apps/{entry['id']}/reject", headers=staff,
                               json={"expected_version": entry["version"], "reason": ""})).status_code == 422
    rejected = (await clients.post(f"/admin/showcase/apps/{entry['id']}/reject", headers=staff,
                                   json={"expected_version": entry["version"], "reason": "Add a real link"})).json()
    assert rejected["status"] == "rejected"
    again = await clients.patch(f"/showcase/apps/{entry['id']}", json={"expected_version": rejected["version"],
                                                                         "fields": fields(url="https://example.org")})
    assert again.json()["status"] == "pending" and again.json()["reject_reason"] == ""


async def test_a_rejected_app_cannot_return_to_review_past_the_limit(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    first = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(name="App 0"))).json()
    rejected = (await clients.post(f"/admin/showcase/apps/{first['id']}/reject", headers=staff,
                                   json={"expected_version": first["version"], "reason": "No"})).json()
    for i in range(1, 6):
        assert (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(name=f"App {i}"))).status_code == 200
    back = await clients.patch(f"/showcase/apps/{first['id']}", json={"expected_version": rejected["version"],
                                                                        "fields": fields(name="App 0 again")})
    assert back.status_code == 409
    assert (await clients.post(f"/admin/showcase/apps/{first['id']}/approve", headers=staff,
                               json={"expected_version": rejected["version"]})).status_code == 409


async def test_an_edit_previews_kept_pictures_from_the_public_side(clients, store, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    icon = await _upload(clients, "showcase_icon")
    entry = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(icon_key=icon))).json()
    entry = (await clients.post(f"/admin/showcase/apps/{entry['id']}/approve", headers=staff,
                                json={"expected_version": entry["version"]})).json()
    for key in store.keys("staging"):
        await store.delete("staging", key)          # the staging copy has expired
    edited = (await clients.patch(f"/showcase/apps/{entry['id']}", json={
        "expected_version": entry["version"], "fields": fields(icon_key=icon, name="Renamed")})).json()
    assert edited["pending"]["icon_url"] == f"/media-files/{icon}"
    approved = await clients.post(f"/admin/showcase/apps/{entry['id']}/approve", headers=staff,
                                  json={"expected_version": edited["version"]})
    assert approved.status_code == 200 and approved.json()["live"]["name"] == "Renamed"


async def test_five_apps_per_team_and_only_owners_or_admins_apply(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    for i in range(5):
        assert (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(name=f"App {i}"))).status_code == 200
    sixth = await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(name="App 6"))
    assert sixth.status_code == 409 and "5 apps" in sixth.json()["detail"]
    assert (await clients.post(f"/admin/showcase/teams/{org_id}/apps", headers=staff,
                               json={"fields": fields(name="Staff 6")})).status_code == 409
    async with session_maker() as db:
        db.add(Org(name="Not mine", slug="not-mine"))
        await db.commit()
        other = (await db.execute(select(Org.id).where(Org.slug == "not-mine"))).scalar_one()
    assert (await clients.post(f"/showcase/teams/{other}/apps", json=fields())).status_code == 403
    assert (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(),
                               headers={"Origin": "https://evil.example"})).status_code == 403


async def test_staff_create_hide_and_the_board_follows(clients, store, staff):
    _, org_id = await _tim()
    icon = (await clients.post("/admin/images/showcase_icon", headers=staff, content=picture())).json()["key"]
    made = await clients.post(f"/admin/showcase/teams/{org_id}/apps", headers=staff,
                              json={"fields": fields(icon_key=icon), "approve": True, "actor": "ops@superdesign.dev"})
    assert made.status_code == 200 and made.json()["status"] == "approved"
    assert icon in store.keys("public")
    hidden = await clients.post(f"/admin/showcase/apps/{made.json()['id']}/hide", headers=staff,
                                json={"expected_version": made.json()["version"], "hidden": True})
    assert hidden.json()["status"] == "hidden"
    assert (await clients.get("/showcase/leaderboard")).json()["apps"] == []
    assert (await clients.get("/admin/showcase/apps", headers={"X-Treg-Token": "wrong"})).status_code == 403


# ---- the public board and the weekly fold ---------------------------------------------------------

async def _approved(org_id: int, **over) -> int:
    async with session_maker() as db:
        rev, flags = rules.split_submission(fields())
        app_ = ShowcaseApp(org_id=org_id, status="approved", approved_at=datetime(2026, 9, 1),
                           **{**rules.clean_revision(rev), **flags, **over})
        db.add(app_)
        await db.commit()
        return app_.id


async def _calls(org_id, at, n, *, provider="tikhub", endpoint="tikhub.tiktok.user.profile", refused_by=None,
                 key=None):
    async with session_maker() as db:
        for _ in range(n):
            db.add(CallRecord(org_id=org_id, user_email="a@b.c", tool_name=endpoint, method="GET", path="/x",
                              status_code=200, endpoint_id=endpoint, provider=provider, refused_by=refused_by,
                              created_at=at, api_key_id=key))
        await db.commit()


async def test_the_fold_counts_only_new_upstream_calls_in_small_batches(clients):
    _, org_id = await _tim()
    await _approved(org_id)
    monday = datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9)
    await _calls(org_id, rules.window_start(TODAY) - timedelta(days=3), 7)          # before the window
    await _calls(org_id, monday, 3)
    await _calls(org_id, monday + timedelta(days=2), 2, provider="anyapi", endpoint="anyapi.x.search.posts")
    await _calls(org_id, monday + timedelta(days=2), 4, refused_by="policy")
    first = await showcase.refresh_activity(now=NOW, batch_rows=2)
    assert first["caught_up"] and first["calls"] == 9      # refused calls move the cursor but are not activity
    async with session_maker() as db:
        row = await db.get(ShowcaseWeek, (org_id, 0, NEWEST.isoformat()))
        assert (await db.execute(select(func.count()).select_from(ShowcaseWeek))).scalar_one() == 1
    assert (row.calls, row.active_days, row.providers) == (5, 0b101, ["anyapi", "tikhub"])
    assert sum(row.categories.values()) == 5

    # A second run reads only what arrived since; a call younger than the lag waits for the next.
    await _calls(org_id, monday + timedelta(days=3), 1)
    await _calls(org_id, NOW - timedelta(minutes=1), 1)
    again = await showcase.refresh_activity(now=NOW)
    assert again["calls"] == 1
    async with session_maker() as db:
        assert (await db.get(ShowcaseWeek, (org_id, 0, NEWEST.isoformat()))).calls == 6
    assert (await showcase.refresh_activity(now=NOW))["calls"] == 0
    assert (await showcase.refresh_activity(now=NOW + timedelta(minutes=10)))["calls"] == 1


async def test_a_capped_run_resumes_where_it_stopped(clients):
    _, org_id = await _tim()
    await _approved(org_id)
    await _calls(org_id, datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9), 5)
    first = await showcase.refresh_activity(now=NOW, max_rows=3, batch_rows=2)
    assert (first["calls"], first["caught_up"]) == (3, False)
    second = await showcase.refresh_activity(now=NOW, max_rows=3, batch_rows=2)
    assert (second["calls"], second["caught_up"]) == (2, True)
    async with session_maker() as db:
        assert (await db.get(ShowcaseWeek, (org_id, 0, NEWEST.isoformat()))).calls == 5


async def test_teams_take_turns_and_a_team_is_read_once_for_all_its_apps(clients):
    _, busy = await _tim()
    first_app = await _approved(busy, name="Busy one")
    await _approved(busy, name="Busy two")
    async with session_maker() as db:
        quiet_org = Org(name="Quiet", slug="quiet-team")
        db.add(quiet_org)
        await db.commit()
        quiet = quiet_org.id
    await _approved(quiet, name="Quiet app")
    monday = datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9)
    await _calls(busy, monday, 6)
    await _calls(quiet, monday, 1)
    run = await showcase.refresh_activity(now=NOW, max_rows=4, batch_rows=2)
    assert (run["sources"], run["calls"], run["caught_up"]) == (2, 4, False)   # busy 2, quiet 1, busy 1: quiet never waited
    async with session_maker() as db:
        assert (await db.get(ShowcaseWeek, (quiet, 0, NEWEST.isoformat()))).calls == 1
    await showcase.refresh_activity(now=NOW)
    board = {a["name"]: a for a in (await clients.get("/showcase/leaderboard")).json()["apps"]}
    assert board["Busy one"]["calls_tier"] is None and board["Busy one"]["streak_days"] == 1
    assert board["Busy two"]["streak_days"] == 1 and first_app


async def test_a_team_no_longer_listed_loses_its_rows_and_starts_over(clients, staff):
    _, org_id = await _tim()
    app_id = await _approved(org_id)
    await _calls(org_id, datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9), 2)
    await showcase.refresh_activity(now=NOW)
    async with session_maker() as db:
        app_ = await db.get(ShowcaseApp, app_id)
        app_.status = "hidden"
        await db.commit()
    await showcase.refresh_activity(now=NOW)
    async with session_maker() as db:
        assert await db.get(ShowcaseCursor, (org_id, 0)) is None and await db.get(ShowcaseWeek, (org_id, 0, NEWEST.isoformat())) is None
        app_ = await db.get(ShowcaseApp, app_id)
        app_.status = "approved"
        await db.commit()
    assert (await showcase.refresh_activity(now=NOW))["calls"] == 2


async def test_a_cursor_is_created_once_even_when_two_runs_find_none(clients):
    _, org_id = await _tim()
    async with session_maker() as db:
        await showcase._locked_cursor(db, (org_id, 0))
        await db.commit()
    async with session_maker() as db:
        await showcase._locked_cursor(db, (org_id, 0))
        await db.commit()
        assert (await db.execute(select(func.count()).select_from(ShowcaseCursor))).scalar_one() == 1


async def test_weeks_that_leave_the_window_are_dropped(clients):
    _, org_id = await _tim()
    await _approved(org_id)
    async with session_maker() as db:
        old = (rules.keep_from(TODAY) - timedelta(days=7)).isoformat()
        db.add(ShowcaseWeek(org_id=org_id, api_key_id=0, week_start=old, calls=9, active_days=1))
        await db.commit()
    await showcase.refresh_activity(now=NOW)
    async with session_maker() as db:
        assert await db.get(ShowcaseWeek, (org_id, 0, old)) is None


async def test_the_public_board_never_leaks_the_team_or_exact_numbers(clients):
    _, org_id = await _tim()
    open_id = await _approved(org_id, name="Open")
    await _approved(org_id, name="Quiet", show_calls=False, show_streak=False)
    await _calls(org_id, datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9), 1234)
    await showcase.refresh_activity(now=NOW)
    r = await clients.get("/showcase/leaderboard")
    assert r.headers["cache-control"] == "no-cache" and "1234" not in r.text
    again = await clients.get("/showcase/leaderboard", headers={"If-None-Match": r.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    by_name = {a["name"]: a for a in r.json()["apps"]}
    assert by_name["Open"]["calls_tier"] == "1k+" and "calls_tier" not in by_name["Quiet"]
    assert r.json()["boards"]["most_active"] == [open_id]
    for a in r.json()["apps"]:
        assert not {"org_id", "team", "calls", "pending", "submitted_by"} & set(a)


async def test_an_upvote_is_one_per_person_per_app_and_can_be_taken_back(clients):
    user, org_id = await _tim()
    app_id = await _approved(org_id)
    clients.headers.pop("X-Treg-Token", None)
    assert (await clients.post("/showcase/vote", json={"id": app_id, "up": True})).status_code == 401
    _cookie(clients, user)
    for _ in range(2):  # a second upvote changes nothing
        assert (await clients.post("/showcase/vote", json={"id": app_id, "up": True})).json() == \
            {"id": app_id, "up": True, "upvotes": 1}
    assert (await clients.get("/showcase/votes/me")).json()["upvoted"] == [app_id]
    assert (await clients.post("/showcase/vote", json={"id": app_id, "up": False})).json()["upvotes"] == 0
    assert (await clients.get("/showcase/votes/me")).json()["upvoted"] == []
    assert (await clients.post("/showcase/vote", json={"id": app_id, "value": -1})).status_code == 422
    assert (await clients.post("/showcase/vote", json={"id": 999999, "up": True})).status_code == 404
    assert (await clients.post("/showcase/vote", json={"id": app_id, "up": True},
                               headers={"Origin": "https://evil.example"})).status_code == 403
    async with session_maker() as db:
        db.add(ShowcaseVote(app_id=app_id, user_id=user.id))
        await db.commit()
    with pytest.raises(IntegrityError):
        async with session_maker() as db:
            db.add(ShowcaseVote(app_id=app_id, user_id=user.id))
            await db.commit()


async def test_deleting_a_team_removes_its_entries_and_everything_under_them(clients):
    from treg.domain.governance.teams import cascade_delete_org
    user, _ = await _tim()
    async with session_maker() as db:
        org = Org(name="Leaving", slug="leaving-team")
        db.add(org)
        await db.flush()
        org_id = org.id
        await db.commit()
    app_id = await _approved(org_id)
    async with session_maker() as db:
        db.add(ShowcaseWeek(org_id=org_id, api_key_id=0, week_start=NEWEST.isoformat(), calls=1, active_days=1))
        db.add(ShowcaseCursor(org_id=org_id, api_key_id=0, through_id=1))
        db.add(ShowcaseVote(app_id=app_id, user_id=user.id))
        db.add(ShowcaseAppEvent(app_id=app_id, version=1, action="applied", actor="t", source="api"))
        await db.commit()
        await cascade_delete_org(await db.get(Org, org_id), db)
        await db.commit()
    async with session_maker() as db:
        for model in (ShowcaseApp, ShowcaseWeek, ShowcaseCursor, ShowcaseVote, ShowcaseAppEvent):
            assert (await db.execute(select(model))).scalars().all() == [], model.__name__


async def test_the_leaderboard_does_not_exist_on_a_self_hosted_registry(monkeypatch):
    monkeypatch.setenv("TREG_PUBLIC_URL", "https://registry.example.internal")
    get_settings.cache_clear()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as c:
            for method, path in (("GET", "/showcase/leaderboard"), ("GET", "/showcase/votes/me"),
                                 ("POST", "/showcase/vote")):
                r = await c.request(method, path, json={"id": 1, "up": True} if method == "POST" else None)
                assert r.status_code == 404, path
    finally:
        get_settings.cache_clear()


# ---- activity from API keys ----------------------------------------------------------------------

async def _key(org_id, name, **over) -> int:
    async with session_maker() as db:
        key = ApiKey(org_id=org_id, kind="agent", name=name, safe_prefix=f"treg_{name[:4]}", **over)
        db.add(key)
        await db.commit()
        return key.id


async def test_an_app_counts_its_own_keys_and_the_team_choice_is_applied_at_once(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    team_app = await _approved(org_id, name="Whole team", show_providers=True)
    keyed = await _approved(org_id, name="Keyed", show_providers=True)
    k1, k2 = await _key(org_id, "prod"), await _key(org_id, "other")
    monday = datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9)
    await _calls(org_id, monday, 3, key=k1)
    await _calls(org_id, monday, 2, key=k2, provider="anyapi", endpoint="anyapi.x.search.posts")

    listed = (await clients.get(f"/showcase/teams/{org_id}/keys")).json()
    assert {k["name"] for k in listed} >= {"prod", "other"} and all(k["app_id"] is None for k in listed)
    r = await clients.put(f"/showcase/apps/{keyed}/usage", json={"source": "keys", "key_ids": [k1]})
    assert r.status_code == 200, r.text
    assert r.json()["usage"] == {"source": "keys", "keys": [
        {"id": k1, "name": "prod", "prefix": "treg_prod", "last_used_at": None}]}
    assert r.json()["status"] == "approved" and r.json()["pending"] is None      # not reviewed
    assert (await clients.put(f"/showcase/apps/{keyed}/usage", json={"source": "keys", "key_ids": [k1]})).status_code == 422
    assert (await clients.put(f"/showcase/apps/{team_app}/usage",
                              json={"source": "keys", "key_ids": [k1]})).status_code == 409   # one app per key
    assert (await clients.put(f"/showcase/apps/{keyed}/usage", json={"source": "keys", "key_ids": []})).status_code == 422
    assert {k["id"]: k["app_id"] for k in (await clients.get(f"/showcase/teams/{org_id}/keys")).json()}[k1] == keyed

    run = await showcase.refresh_activity(now=NOW)
    assert run["sources"] == 2
    async with session_maker() as db:
        assert (await db.get(ShowcaseWeek, (org_id, k1, NEWEST.isoformat()))).calls == 3
        assert (await db.get(ShowcaseWeek, (org_id, 0, NEWEST.isoformat()))).calls == 5
    board = {a["name"]: a for a in (await clients.get("/showcase/leaderboard")).json()["apps"]}
    assert board["Keyed"]["providers"] == 1 and board["Whole team"]["providers"] == 2
    assert "usage" not in board["Keyed"] and "keys" not in json.dumps(board)

    # Back to the team: the key's rows are no longer needed and go.
    assert (await clients.put(f"/showcase/apps/{keyed}/usage", json={"source": "team"})).json()["usage"] == {"source": "team"}
    await showcase.refresh_activity(now=NOW)
    async with session_maker() as db:
        assert await db.get(ShowcaseWeek, (org_id, k1, NEWEST.isoformat())) is None
    detail = (await clients.get(f"/admin/showcase/apps/{keyed}", headers=staff)).json()
    assert [h["action"] for h in detail["history"]][:2] == ["usage", "usage"]
    assert detail["history"][0]["changes"] == {"usage": [f"keys:{k1}", "team"]}


async def test_only_active_keys_of_the_team_and_only_its_owner_or_admin(clients):
    user, org_id = await _tim()
    _cookie(clients, user)
    app_id = await _approved(org_id)
    async with session_maker() as db:
        other = Org(name="Elsewhere", slug="elsewhere-team")
        db.add(other)
        await db.commit()
        other_id = other.id
    foreign = await _key(other_id, "theirs")
    revoked = await _key(org_id, "old", state="revoked", revoked_at=datetime(2026, 9, 1))
    for bad in (foreign, revoked, 999999):
        assert (await clients.put(f"/showcase/apps/{app_id}/usage",
                                  json={"source": "keys", "key_ids": [bad]})).status_code == 422
    assert (await clients.put(f"/showcase/apps/{app_id}/usage", json={"source": "nope"})).status_code == 422
    assert revoked not in {k["id"] for k in (await clients.get(f"/showcase/teams/{org_id}/keys")).json()}
    assert (await clients.get(f"/showcase/teams/{other_id}/keys")).status_code == 403
    async with session_maker() as db:
        (await db.execute(select(Membership).where(Membership.user_id == user.id, Membership.org_id == org_id))
         ).scalar_one().role = "member"
        await db.commit()
    assert (await clients.put(f"/showcase/apps/{app_id}/usage", json={"source": "team"})).status_code == 403
    assert (await clients.get(f"/showcase/teams/{org_id}/apps")).status_code == 403


async def test_a_rotated_key_keeps_counting_for_its_app(clients):
    user, org_id = await _tim()
    _cookie(clients, user)
    app_id = await _approved(org_id, show_calls=True)
    old = await _key(org_id, "first")
    assert (await clients.put(f"/showcase/apps/{app_id}/usage", json={"source": "keys", "key_ids": [old]})).status_code == 200
    new = await _key(org_id, "second")
    async with session_maker() as db:
        row = await db.get(ApiKey, old)
        row.state, row.revoked_at, row.deleted_at, row.replacement_key_id = "revoked", NOW, NOW, new
        await db.commit()
    monday = datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9)
    await _calls(org_id, monday, 2, key=old)
    await _calls(org_id, monday + timedelta(days=1), 3, key=new)
    await showcase.refresh_activity(now=NOW)
    async with session_maker() as db:
        weeks = (await db.execute(select(ShowcaseWeek).where(ShowcaseWeek.org_id == org_id))).scalars().all()
    assert sorted((w.api_key_id, w.calls) for w in weeks) == sorted([(old, 2), (new, 3)])
    view = (await clients.get(f"/showcase/teams/{org_id}/apps")).json()[0]
    assert [k["name"] for k in view["usage"]["keys"]] == ["second"]
    board = (await clients.get("/showcase/leaderboard")).json()["apps"][0]
    assert board["streak_days"] == 2
    other = await _approved(org_id, name="Other")
    assert (await clients.put(f"/showcase/apps/{other}/usage",
                              json={"source": "keys", "key_ids": [new]})).status_code == 409   # the replacement is taken


async def test_the_team_removes_its_live_app_and_only_staff_show_it_again(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    app_id = await _approved(org_id)
    pending = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(name="Waiting"))).json()
    assert (await clients.post(f"/showcase/apps/{pending['id']}/remove")).status_code == 422
    removed = await clients.post(f"/showcase/apps/{app_id}/remove")
    assert removed.status_code == 200 and removed.json()["status"] == "hidden"
    assert all(a["id"] != app_id for a in (await clients.get("/showcase/leaderboard")).json()["apps"])
    detail = (await clients.get(f"/admin/showcase/apps/{app_id}", headers=staff)).json()
    assert detail["history"][0]["action"] == "removed" and detail["history"][0]["source"] == "web"
    assert (await clients.post(f"/showcase/apps/{app_id}/remove",
                               headers={"Origin": "https://evil.example"})).status_code == 403


async def test_the_queue_says_when_a_proposal_was_sent(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    entry = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields())).json()
    queue = (await clients.get("/admin/showcase/apps?status=review", headers=staff)).json()
    assert queue[0]["id"] == entry["id"] and queue[0]["pending_since"]
    assert (await clients.get(f"/admin/showcase/apps/{entry['id']}", headers=staff)).json()["pending_since"]


# ---- the picture sweep ---------------------------------------------------------------------------

async def test_the_sweep_deletes_only_pictures_no_row_names(clients, store):
    user, org_id = await _tim()
    _cookie(clients, user)
    keys = [await _upload(clients, "showcase_shot", picture(1600, 1000 + i)) for i in range(3)]
    used, gone, shared = keys
    for k in keys:
        await images.publish(k)
    await store.put("public", "site/fable/ad1.mp4", b"video", "video/mp4")       # not ours: never touched
    await _approved(org_id, shot_keys=[used, shared])
    async with session_maker() as db:
        other = Org(name="Sharing", slug="sharing-team")
        db.add(other)
        await db.commit()
        other_id = other.id
    await _approved(other_id, shot_keys=[shared])
    later = datetime.now(timezone.utc) + timedelta(days=2)
    assert (await images.sweep())["deleted"] == 0                                 # all younger than the grace
    result = await images.sweep(now=later)
    assert result["deleted"] == 2                                                 # `gone`, with its small copy
    left = store.keys("public")
    assert gone not in left and gone.replace(".webp", "-640.webp") not in left
    assert {used, shared, used.replace(".webp", "-640.webp"), "site/fable/ad1.mp4"} <= set(left)
    # A deleted team leaves its pictures behind; the sweep takes those no one else uses.
    from treg.domain.governance.teams import cascade_delete_org
    async with session_maker() as db:
        await cascade_delete_org(await db.get(Org, other_id), db)
        await db.commit()
    assert (await images.sweep(now=later))["deleted"] == 0                         # `shared` is still Tim's


async def test_withdrawing_an_application_removes_its_history_and_key_links(clients):
    user, org_id = await _tim()
    _cookie(clients, user)
    entry = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields())).json()
    key = await _key(org_id, "prod")
    assert (await clients.put(f"/showcase/apps/{entry['id']}/usage",
                              json={"source": "keys", "key_ids": [key]})).status_code == 200
    assert (await clients.delete(f"/showcase/apps/{entry['id']}")).status_code == 200
    async with session_maker() as db:
        for model in (ShowcaseApp, ShowcaseAppEvent, ShowcaseAppKey):
            assert (await db.execute(select(model))).scalars().all() == [], model.__name__
    again = (await clients.post(f"/showcase/teams/{org_id}/apps", json=fields(name="Again"))).json()
    assert (await clients.put(f"/showcase/apps/{again['id']}/usage",
                              json={"source": "keys", "key_ids": [key]})).status_code == 200   # the key is free


async def test_a_new_cursor_never_reads_before_the_window(clients):
    _, org_id = await _tim()
    await _approved(org_id)
    await _calls(org_id, rules.window_start(TODAY) - timedelta(days=20), 4)            # long before the window
    await _calls(org_id, datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9), 1)
    async with session_maker() as db:            # a cursor created mid-run, as if another run dropped it
        n, _ = await showcase._fold_batch(db, (org_id, 0), since=rules.window_start(TODAY), young=NOW,
                                          limit=100, domains={}, first=False)
        await db.commit()
    assert n == 1


async def test_what_the_board_shows_is_the_teams_choice_and_applies_at_once(clients, staff):
    user, org_id = await _tim()
    _cookie(clients, user)
    created = (await clients.post(f"/admin/showcase/teams/{org_id}/apps", headers=staff, json={
        "fields": fields(show_calls=True, show_streak=True, show_providers=True, show_categories=True),
        "approve": True})).json()
    assert created["live"]["show_providers"] is True              # staff listed it with sharing on
    await _calls(org_id, datetime(NEWEST.year, NEWEST.month, NEWEST.day, 9), 150)
    await showcase.refresh_activity(now=NOW)
    assert (await clients.get("/showcase/leaderboard")).json()["apps"][0]["calls_tier"] == "100+"

    # The team turns the call tier off: live at once, nothing waits for review.
    off = await clients.patch(f"/showcase/apps/{created['id']}", json={
        "expected_version": created["version"], "fields": fields(show_calls=False, show_streak=True,
                                                                  show_providers=True, show_categories=True)})
    assert off.status_code == 200 and off.json()["pending"] is None and off.json()["live"]["show_calls"] is False
    assert "calls_tier" not in (await clients.get("/showcase/leaderboard")).json()["apps"][0]

    # A text change and a flag together: the flag now, the text after review.
    both = (await clients.patch(f"/showcase/apps/{created['id']}", json={
        "expected_version": off.json()["version"], "fields": fields(name="Renamed", show_calls=False, show_streak=False,
                                                                     show_providers=True, show_categories=True)})).json()
    assert both["live"]["show_streak"] is False and both["live"]["name"] == "Example App"
    assert both["pending"]["name"] == "Renamed" and both["pending"]["show_streak"] is False
    approved = (await clients.post(f"/admin/showcase/apps/{created['id']}/approve", headers=staff,
                                   json={"expected_version": both["version"]})).json()
    assert approved["live"]["name"] == "Renamed" and approved["live"]["show_streak"] is False   # not reverted
    history = (await clients.get(f"/admin/showcase/apps/{created['id']}", headers=staff)).json()["history"]
    assert [h["action"] for h in history][:4] == ["approved", "edited", "sharing", "sharing"]
    assert history[3]["changes"] == {"show_calls": [True, False]}
