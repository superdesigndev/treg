"""`treg-worker billing autotopup-blocked`: an operator hears which paying team its own auto top-up
settings are refusing, and which setting it is, without the check moving any money."""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import update

from conftest import verified_signup

from treg import worker
from treg.api import app
from treg.application import autotopup_watch as watch
from treg.application import billing
from treg.config import get_settings
from treg.domain import money
from treg.infra import db as infra_db
from treg.infra import slack
from treg.infra.db import reset_db, session_maker
from treg.models import CallRecord, LedgerEntry, Org


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
async def c(monkeypatch):
    await reset_db()
    monkeypatch.setattr(get_settings(), "ops_slack_webhook_url", "", raising=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client


async def _team(c, email: str, *, refused: int, refused_before: int = 0, auto_topups: int = 1,
                bought: bool = True, **org) -> int:
    r = await verified_signup(c, json={"email": email})
    assert r.status_code == 200, r.text
    org_id = r.json()["org_id"]
    now = _now()
    async with session_maker() as db:
        if bought:
            for i in range(auto_topups):
                await money.topup(db, org_id, 50_000_000, f"pi_{org_id}_{i}", meta={"auto": True})
        for i in range(4):
            db.add(LedgerEntry(id=uuid.uuid4().hex, org_id=org_id, kind="settle", amount_micro=-2_500_000,
                               created_at=now - timedelta(minutes=10 + i)))
        for n, ago in ((refused, timedelta(minutes=5)), (refused_before, timedelta(minutes=70))):
            for _ in range(n):
                db.add(CallRecord(org_id=org_id, user_email=email, tool_name="x", method="POST", path="/call/x",
                                  status_code=402, refused_by="balance", created_at=now - ago))
        db.add(CallRecord(org_id=org_id, user_email=email, tool_name="x", method="POST", path="/call/x",
                          status_code=200, created_at=now - timedelta(minutes=5)))
        fields = {"balance_micro": 100_000, "autotopup_enabled": True, "autotopup_consented_at": now,
                  "stripe_customer_id": f"cus_{org_id}", "stripe_default_pm": "pm_card", **org}
        await db.execute(update(Org).where(Org.id == org_id).values(**fields))
        await db.commit()
    return org_id


async def _schema_ok() -> None:
    """The worker's startup schema check, which a test database does not need to pass."""


async def test_each_blocking_limit_is_named_with_the_teams_own_numbers(c):
    now = _now()
    capped = await _team(c, "capped@x.dev", refused=30, auto_topups=2, autotopup_amount_micro=50_000_000,
                         autotopup_monthly_cap_micro=100_000_000)
    hourly = await _team(c, "hourly@x.dev", refused=40, refused_before=25, autotopup_amount_micro=50_000_000,
                         autotopup_max_per_hour=1, autotopup_last_attempt_at=now - timedelta(minutes=20))
    failing = await _team(c, "failing@x.dev", refused=25, autotopup_failures=3)
    off = await _team(c, "off@x.dev", refused=21, autotopup_enabled=False, autotopup_disabled_reason="no_card",
                      stripe_default_pm=None)
    await _team(c, "quiet@x.dev", refused=19)
    await _team(c, "rich@x.dev", refused=50, balance_micro=900_000_000)
    await _team(c, "never-paid@x.dev", refused=50, bought=False)

    found = {b.org_id: b for b in await watch.find_blocked(now=now, min_refusals=20)}

    assert set(found) == {capped, hourly, failing, off}
    assert found[capped].limit == watch.LIMIT_MONTHLY_CAP
    assert "$100.00 of $100.00" in found[capped].detail
    assert found[hourly].limit == watch.LIMIT_PER_HOUR
    assert "at most 1 charge(s) per hour of $50.00" in found[hourly].detail
    assert (found[hourly].refused_last_hour, found[hourly].refused_hour_before) == (40, 25)
    assert not found[hourly].new and found[capped].new
    assert found[failing].limit == watch.LIMIT_FAILURES
    assert found[off].limit == watch.LIMIT_OFF and "no_card" in found[off].detail
    b = found[hourly]
    assert (b.spent_last_hour_micro, b.settled_last_hour) == (10_000_000, 4)
    assert b.turned_away_micro() == 40 * 2_500_000
    assert b.owner_email == "hourly@x.dev"


async def test_the_check_moves_no_money(c):
    org_id = await _team(c, "capped@x.dev", refused=30, autotopup_monthly_cap_micro=20_000_000)
    async with session_maker() as db:
        before = (await db.get(Org, org_id)).balance_micro
    await watch.find_blocked(min_refusals=1)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        assert org.balance_micro == before and org.autotopup_last_attempt_at is None


async def test_the_worker_posts_new_teams_every_run_and_standing_ones_once_a_day(c, monkeypatch, capsys):
    now = _now()
    new = await _team(c, "new@x.dev", refused=30, autotopup_failures=3)
    standing = await _team(c, "standing@x.dev", refused=30, refused_before=30, autotopup_failures=3)
    monkeypatch.setattr(get_settings(), "ops_slack_webhook_url", "https://hooks.slack.test/ops", raising=False)
    monkeypatch.setattr(infra_db, "verify_db", _schema_ok)
    posted: list[str] = []

    async def post(url, text, **kw):
        posted.append(text)

    monkeypatch.setattr(slack, "post", post)
    other_hour = (now.hour + 12) % 24
    assert await worker._billing_autotopup_blocked(argparse.Namespace(min_refusals=20, digest_hour=other_hour)) == 0
    report = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {r["org_id"] for r in report} == {new, standing}
    [text] = posted
    assert f"team {new}, owner new@x.dev" in text and f"team {standing}" not in text
    assert "card_failures" in text and "refusals include retries" in text

    posted.clear()
    assert await worker._billing_autotopup_blocked(argparse.Namespace(min_refusals=20, digest_hour=now.hour)) == 0
    [text] = posted
    assert f"team {new}" in text and f"team {standing}" in text


async def test_nothing_is_posted_when_no_team_is_refused(c, monkeypatch, capsys):
    await _team(c, "quiet@x.dev", refused=0)
    monkeypatch.setattr(get_settings(), "ops_slack_webhook_url", "https://hooks.slack.test/ops", raising=False)
    monkeypatch.setattr(infra_db, "verify_db", _schema_ok)
    posted: list[str] = []

    async def post(url, text, **kw):
        posted.append(text)

    monkeypatch.setattr(slack, "post", post)
    hour = _now().hour
    assert await worker._billing_autotopup_blocked(argparse.Namespace(min_refusals=20, digest_hour=hour)) == 0
    assert posted == [] and json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == []


def test_the_limits_follow_the_order_attempt_auto_topup_checks_them():
    now = _now()
    org = Org(id=1, name="t", slug="t", autotopup_enabled=True, stripe_customer_id="cus", stripe_default_pm="pm",
              autotopup_amount_micro=50_000_000, autotopup_monthly_cap_micro=100_000_000, autotopup_max_per_hour=1,
              autotopup_last_attempt_at=now - timedelta(minutes=5))
    assert watch.blocking_limit(org, now=now, month_spend_micro=100_000_000)[0] == watch.LIMIT_PER_HOUR
    org.autotopup_last_attempt_at = now - timedelta(hours=2)
    assert watch.blocking_limit(org, now=now, month_spend_micro=100_000_000)[0] == watch.LIMIT_MONTHLY_CAP
    assert watch.blocking_limit(org, now=now, month_spend_micro=0)[0] == watch.LIMIT_REFILL_DUE
    org.autotopup_failures = 1
    org.autotopup_last_attempt_at = now - timedelta(minutes=30)
    assert watch.blocking_limit(org, now=now, month_spend_micro=0)[0] == watch.LIMIT_RETRY_WAIT
    assert billing.autotopup_wait_s(org) == get_settings().autotopup_cooldown_s
