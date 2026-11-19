"""The affiliate program on Dub: the click cookie, the lead outbox, and the Stripe tag.

Covered: the landing parks only a well-formed `dub_id` and only when Dub is configured; a team
created with the click queues one lead and is never also paid the friend referral; without the
click the friend referral is untouched; the drain uploads, retries and dead-letters by Dub's
answer; and the Stripe customer and Checkout carry `dubCustomerExternalId` for that team alone.
"""

from __future__ import annotations

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream, verified_signup

from treg.api import app
from treg.application import affiliates, billing
from treg.config import get_settings
from treg.infra import dub
from treg.infra.db import reset_db, session_maker
from treg.models import AffiliateLead, Org, Referral
from treg.routers.signup_cookies import AFFILIATE_COOKIE, REFERRAL_COOKIE


@pytest.fixture
async def c(monkeypatch):
    await reset_db()
    monkeypatch.setattr(get_settings(), "dub_api_key", "dub_test_suite", raising=False)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_suite", raising=False)
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


def _cookies(**values: str) -> dict:
    return {"Cookie": "; ".join(f"{k}={v}" for k, v in values.items())}


async def _referrer_code(c: AsyncClient) -> str:
    r = await verified_signup(c, json={"email": "ann@superdesign.dev"})
    token = r.json()["token"]
    r = await c.post("/referrals/code", headers={"X-Treg-Token": token})
    return r.json()["code"]


async def _leads() -> list[AffiliateLead]:
    async with session_maker() as db:
        return list((await db.execute(select(AffiliateLead))).scalars().all())


def test_click_ids_are_allowlisted():
    assert dub.valid_click_id("cm3w8x2abcDEF_-1") == "cm3w8x2abcDEF_-1"
    assert dub.valid_click_id('"quoted"') == "quoted"
    for bad in ("", "a b", "x;y", "<script>", "a" * 65, "é"):
        assert dub.valid_click_id(bad) == ""


async def test_dub_link_serves_the_landing_and_parks_the_click(c):
    r = await c.get("/?dub_id=cm3w8x2abc&utm_source=partner")
    assert r.status_code == 200
    assert "<html" in r.text[:300].lower()
    assert r.cookies.get(AFFILIATE_COOKIE) == "cm3w8x2abc"


async def test_dubs_real_redirect_shape_serves_the_landing_not_the_dashboard(c):
    r = await c.get("/?via=testpartner&dub_id=cm3w8x2abc&utm_source=youtube&gclid=abc")
    assert r.status_code == 200
    assert "OpenRouter for agent tools" in r.text
    assert r.cookies.get(AFFILIATE_COOKIE) == "cm3w8x2abc"


async def test_an_unknown_parameter_still_goes_to_the_dashboard(c):
    r = await c.get("/?dub_id=cm3w8x2abc&topup=success")
    assert "OpenRouter for agent tools" not in r.text


async def test_a_malformed_click_is_not_parked(c):
    r = await c.get("/?dub_id=bad%20value")
    assert AFFILIATE_COOKIE not in r.cookies


async def test_nothing_is_parked_while_dub_is_off(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "dub_api_key", "")
    r = await c.get("/?dub_id=cm3w8x2abc")
    assert AFFILIATE_COOKIE not in r.cookies


async def test_the_dashboard_parks_the_click_too(c):
    r = await c.get("/app?dub_id=cm3w8x2abc&ref=grokbot")
    assert r.cookies.get(AFFILIATE_COOKIE) == "cm3w8x2abc"


async def test_affiliate_click_queues_a_lead_and_wins_over_the_friend_code(c):
    code = await _referrer_code(c)
    r = await verified_signup(c, json={"email": "bob@superdesign.dev"},
                              headers=_cookies(**{AFFILIATE_COOKIE: "cm3w8x2abc", REFERRAL_COOKIE: code}))
    assert r.status_code == 200, r.text
    org_id = r.json()["org_id"]
    leads = await _leads()
    assert [(lead.org_id, lead.click_id, lead.uploaded_at) for lead in leads] == [(org_id, "cm3w8x2abc", None)]
    async with session_maker() as db:
        assert (await db.execute(select(Referral))).scalars().all() == []


async def test_without_a_click_the_friend_referral_is_unchanged(c):
    code = await _referrer_code(c)
    r = await verified_signup(c, json={"email": "bob@superdesign.dev"}, headers=_cookies(**{REFERRAL_COOKIE: code}))
    assert r.status_code == 200, r.text
    assert await _leads() == []
    async with session_maker() as db:
        rows = (await db.execute(select(Referral))).scalars().all()
    assert [row.referred_org_id for row in rows] == [r.json()["org_id"]]


async def test_a_click_cookie_does_nothing_while_dub_is_off(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "dub_api_key", "")
    code = await _referrer_code(c)
    r = await verified_signup(c, json={"email": "bob@superdesign.dev"},
                              headers=_cookies(**{AFFILIATE_COOKIE: "cm3w8x2abc", REFERRAL_COOKIE: code}))
    assert r.status_code == 200, r.text
    assert await _leads() == []
    async with session_maker() as db:
        assert len((await db.execute(select(Referral))).scalars().all()) == 1


def _dub_transport(status: int, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"click": {"id": "x"}} if status == 200 else {"error": "no"})
    return httpx.MockTransport(handler)


async def _attributed_team(c) -> int:
    r = await verified_signup(c, json={"email": "bob@superdesign.dev"},
                              headers=_cookies(**{AFFILIATE_COOKIE: "cm3w8x2abc"}))
    return r.json()["org_id"]


async def test_drain_uploads_the_lead(c):
    org_id = await _attributed_team(c)
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_dub_transport(200, seen)) as client:
        assert await affiliates.drain_once(client, session_factory=session_maker) == {"sent": 1, "retried": 0, "failed": 0}
        assert await affiliates.drain_once(client, session_factory=session_maker) == {"sent": 0}
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.dub.co/track/lead"
    assert seen[0].headers["authorization"] == "Bearer dub_test_suite"
    import json
    assert json.loads(seen[0].content) == {
        "clickId": "cm3w8x2abc", "eventName": "Sign up",
        "customerExternalId": str(org_id), "customerEmail": "bob@superdesign.dev"}
    assert (await _leads())[0].uploaded_at is not None


async def test_a_server_error_retries_later(c):
    await _attributed_team(c)
    seen: list = []
    async with httpx.AsyncClient(transport=_dub_transport(503, seen)) as client:
        assert await affiliates.drain_once(client, session_factory=session_maker) == {"sent": 0, "retried": 1, "failed": 0}
        assert await affiliates.drain_once(client, session_factory=session_maker) == {"sent": 0}, "not due until the backoff passes"
    lead = (await _leads())[0]
    assert lead.next_attempt_at is not None and lead.failed_at is None and lead.attempts == 1


async def test_an_unknown_click_is_dead_lettered(c):
    await _attributed_team(c)
    async with httpx.AsyncClient(transport=_dub_transport(404, [])) as client:
        assert await affiliates.drain_once(client, session_factory=session_maker) == {"sent": 0, "retried": 0, "failed": 1}
    lead = (await _leads())[0]
    assert lead.failed_at is not None and lead.error.startswith("404")


async def test_drain_is_inert_while_dub_is_off(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "dub_api_key", "")
    assert await affiliates.drain_once(None, session_factory=session_maker) == {"sent": 0, "reason": "disabled"}


async def _stripe_calls(monkeypatch) -> list:
    calls: list = []

    async def sdk(fn, **kw):
        calls.append((fn, kw))
        return {"id": "cus_1" if "customer" in str(fn).lower() and "session" not in str(fn).lower() else "cs_1",
                "url": "https://checkout.stripe.test/cs_1"}
    monkeypatch.setattr(billing, "_sdk", sdk)
    return calls


async def test_an_attributed_team_tags_its_stripe_customer_and_checkout(c, monkeypatch):
    org_id = await _attributed_team(c)
    calls = await _stripe_calls(monkeypatch)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        await billing.create_topup_checkout(db, org, 10)
    metadata = [kw["metadata"] for _, kw in calls if "metadata" in kw]
    assert len(metadata) == 2, calls
    assert all(m.get("dubCustomerExternalId") == str(org_id) for m in metadata)


async def test_a_team_no_affiliate_brought_is_not_tagged(c, monkeypatch):
    r = await verified_signup(c, json={"email": "bob@superdesign.dev"})
    calls = await _stripe_calls(monkeypatch)
    async with session_maker() as db:
        org = await db.get(Org, r.json()["org_id"])
        await billing.create_topup_checkout(db, org, 10)
    assert all("dubCustomerExternalId" not in kw.get("metadata", {}) for _, kw in calls)
