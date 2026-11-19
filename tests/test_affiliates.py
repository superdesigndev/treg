"""The affiliate program on Dub: the click cookie, the lead outbox, and the Stripe tag.

Covered: the landing parks only a well-formed `dub_id` and only when Dub is configured; a team
created with the click queues one lead and is never also paid the friend referral; without the
click the friend referral is untouched; the drain uploads, retries and dead-letters by Dub's
answer; and the Stripe customer and Checkout carry `dubCustomerExternalId` for that team alone.
A code typed at a CLI or agent signup (`ref`) is only ever a Dub partner's link key: queued as a
code-only lead that the drain resolves to the partner. A typed friend code is ignored; the friend
referral comes only from the `?ref=` link cookie.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from conftest import make_upstream, verified_signup

from treg.api import app
from treg.application import affiliates, billing
from treg.config import get_settings
from treg.domain.identity.promotions import promo_inbox
from treg.infra import dub
from treg.infra import stripe as stripe_adapter
from treg.infra.db import reset_db, session_maker
from treg.models import AffiliateClaim, AffiliateLead, Org, Referral
from treg.timeutil import utcnow_naive
from treg.routers.signup_cookies import AFFILIATE_COOKIE, PARTNER_CODE_COOKIE, REFERRAL_COOKIE


@pytest.fixture
async def c(monkeypatch):
    await reset_db()
    monkeypatch.setattr(get_settings(), "dub_api_key", "dub_test_suite", raising=False)
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_suite", raising=False)
    monkeypatch.setattr(affiliates, "_http_client", lambda: httpx.AsyncClient(transport=_unreachable))
    app.state.http = AsyncClient(transport=ASGITransport(app=make_upstream()), base_url="http://upstream")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://registry") as client:
        yield client
    await app.state.http.aclose()


def _refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no Dub in the test suite", request=request)


_unreachable = httpx.MockTransport(_refuse)


def _payment_time_dub(monkeypatch, transport: httpx.MockTransport) -> None:
    monkeypatch.setattr(affiliates, "_http_client", lambda: httpx.AsyncClient(transport=transport))


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


_IDLE = {"sent": 0, "retried": 0, "failed": 0}


async def _drain(client, stripe_calls: list | None = None) -> dict:
    """One drain with Stripe faked; the Stripe sync count is checked where it matters."""
    async def stripe_call(op, **kw):
        (stripe_calls if stripe_calls is not None else []).append((op, kw))
        return {}
    result = await affiliates.drain_once(client, session_factory=session_maker, stripe_call=stripe_call)
    for key in ("synced", "sync_refused", "sync_later"):
        result.pop(key, None)
    return result


def _dub_transport(status: int, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json={"click": {"id": "x"}, "link": {"partnerId": "pn_1"}, "id": "pn_1",
                                            "email": "jane@partner.example"} if status == 200 else {"error": "no"})
    return httpx.MockTransport(handler)


async def _attributed_team(c) -> int:
    r = await verified_signup(c, json={"email": "bob@superdesign.dev"},
                              headers=_cookies(**{AFFILIATE_COOKIE: "cm3w8x2abc"}))
    return r.json()["org_id"]


async def test_drain_uploads_the_lead(c):
    org_id = await _attributed_team(c)
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_dub_transport(200, seen)) as client:
        assert await _drain(client) == {"sent": 1, "retried": 0, "failed": 0}
        assert await _drain(client) == _IDLE
    assert [r.url.path for r in seen] == ["/track/lead", "/partners/pn_1"]
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
        assert await _drain(client) == {"sent": 0, "retried": 1, "failed": 0}
        assert await _drain(client) == _IDLE, "not due until the backoff passes"
    lead = (await _leads())[0]
    assert lead.next_attempt_at is not None and lead.failed_at is None and lead.attempts == 1


async def test_an_unknown_click_is_dead_lettered(c):
    await _attributed_team(c)
    async with httpx.AsyncClient(transport=_dub_transport(404, [])) as client:
        assert await _drain(client) == {"sent": 0, "retried": 0, "failed": 1}
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


async def test_a_first_top_up_calls_stripe_checkout_holding_no_transaction(c, monkeypatch):
    org_id = await _attributed_team(c)
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "jane@partner.example", [])) as client:
        await _drain(client)
    held: dict = {}
    async with session_maker() as db:
        async def sdk(fn, **kw):
            held[fn] = db.in_transaction()
            return {"id": "cs_1" if fn == stripe_adapter.CHECKOUT_SESSION_CREATE else "cus_1",
                    "url": "https://checkout.stripe.test/cs_1"}
        monkeypatch.setattr(billing, "_sdk", sdk)
        org = await db.get(Org, org_id)
        await billing.create_topup_checkout(db, org, 10)
    assert held == {stripe_adapter.CUSTOMER_CREATE: False, stripe_adapter.CHECKOUT_SESSION_CREATE: False}, \
        "billing's session holds no transaction while Stripe is called"


async def test_a_pending_lead_tags_nothing_until_it_is_verified(c, monkeypatch):
    org_id = await _attributed_team(c)
    calls = await _stripe_calls(monkeypatch)
    async with session_maker() as db:
        assert await affiliates.stripe_metadata(db, org_id) == {}
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        await billing.create_topup_checkout(db, org, 10)
    assert all("dubCustomerExternalId" not in kw.get("metadata", {}) for _, kw in calls)


async def test_an_attributed_team_tags_its_stripe_customer_and_checkout(c, monkeypatch):
    org_id = await _attributed_team(c)
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "jane@partner.example", [])) as client:
        assert await _drain(client) == {"sent": 1, "retried": 0, "failed": 0}
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


def test_partner_codes_and_countries_are_allowlisted():
    assert dub.valid_partner_code(" Jane-Doe ") == "jane-doe"
    for bad in ("", "ab", "-jane", "jane_doe", "a" * 33, "jane doe"):
        assert dub.valid_partner_code(bad) == ""
    assert dub.valid_country("au") == "AU"
    for bad in ("", "XX", "T1", "AUS", "1"):
        assert dub.valid_country(bad) == ""
    assert affiliates.country_of("XX") == "US"


async def _orgs_with_ref(c, ref: str, *, headers: dict | None = None):
    from conftest import verified_identity
    token = await verified_identity(c, "bob@superdesign.dev")
    return await c.post("/orgs", json={"name": "Bob", "ref": ref},
                        headers={**(headers or {}), "X-Treg-Token": token})


async def test_a_partner_code_queues_a_code_only_lead_with_the_callers_country(c):
    r = await _orgs_with_ref(c, "Jane-Doe", headers={"CF-IPCountry": "au"})
    assert r.status_code == 200, r.text
    leads = await _leads()
    assert [(lead.org_id, lead.click_id, lead.partner_code, lead.country) for lead in leads] == [
        (r.json()["org_id"], None, "jane-doe", "AU")]
    async with session_maker() as db:
        assert (await db.execute(select(Referral))).scalars().all() == []


async def test_a_friends_code_typed_at_signup_is_ignored(c):
    code = await _referrer_code(c)
    r = await _orgs_with_ref(c, code)
    assert r.status_code == 200, r.text
    assert await _leads() == []
    async with session_maker() as db:
        assert (await db.execute(select(Referral))).scalars().all() == []


async def test_a_link_click_wins_over_a_typed_code(c):
    r = await _orgs_with_ref(c, "jane-doe", headers=_cookies(**{AFFILIATE_COOKIE: "cm3w8x2abc"}))
    assert [(lead.click_id, lead.partner_code) for lead in await _leads()] == [("cm3w8x2abc", None)]
    assert r.status_code == 200


async def test_a_typed_partner_code_skips_the_parked_friend_referral(c):
    code = await _referrer_code(c)
    await _orgs_with_ref(c, "jane-doe", headers=_cookies(**{REFERRAL_COOKIE: code}))
    assert [lead.partner_code for lead in await _leads()] == ["jane-doe"]
    async with session_maker() as db:
        assert (await db.execute(select(Referral))).scalars().all() == []


async def test_a_web_ref_link_that_is_no_friends_code_does_nothing(c):
    r = await verified_signup(c, json={"email": "bob@superdesign.dev"},
                              headers=_cookies(**{REFERRAL_COOKIE: "jane-doe"}))
    assert r.status_code == 200, r.text
    assert await _leads() == []
    async with session_maker() as db:
        assert (await db.execute(select(Referral))).scalars().all() == []


async def test_the_parked_partner_code_is_read_when_the_body_has_none(c):
    r = await _orgs_with_ref(c, "", headers=_cookies(**{PARTNER_CODE_COOKIE: "jane-doe"}))
    assert r.status_code == 200, r.text
    assert [lead.partner_code for lead in await _leads()] == ["jane-doe"]


async def test_a_malformed_code_is_ignored(c):
    r = await _orgs_with_ref(c, "x;drop")
    assert r.status_code == 200, r.text
    assert await _leads() == []


async def test_a_partner_code_does_nothing_while_dub_is_off(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "dub_api_key", "")
    r = await _orgs_with_ref(c, "jane-doe")
    assert r.status_code == 200, r.text
    assert await _leads() == []


async def test_the_first_run_door_takes_the_code_too(c):
    from conftest import verified_identity
    token = await verified_identity(c, "bob@superdesign.dev")
    r = await c.post("/onboard/team", json={"ref": "jane-doe"}, headers={"X-Treg-Token": token})
    assert r.status_code == 200, r.text
    assert [lead.partner_code for lead in await _leads()] == ["jane-doe"]


async def test_the_first_run_door_still_takes_no_body(c):
    from conftest import verified_identity
    token = await verified_identity(c, "bob@superdesign.dev")
    r = await c.post("/onboard/team", headers={"X-Treg-Token": token})
    assert r.status_code == 200, r.text
    assert await _leads() == []


async def test_cli_login_page_parks_the_partner_code_not_the_friend_cookie(c):
    r = await c.get("/login?cli=abcdefgh12&ref=Jane-Doe")
    assert r.cookies.get(PARTNER_CODE_COOKIE) == "jane-doe"
    assert REFERRAL_COOKIE not in r.cookies
    r = await c.get("/login?cli=abcdefgh12&ref=bad%20code")
    assert PARTNER_CODE_COOKIE not in r.cookies


def _code_transport(info_status: int, commission_status: int, seen: list, partner_id: str = "pn_1",
                    partner_email: str = "jane@partner.example", partner_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/links/info":
            return httpx.Response(info_status, json={"partnerId": partner_id} if info_status == 200 else {})
        if request.url.path.startswith("/partners/"):
            return httpx.Response(partner_status, json={"id": partner_id, "email": partner_email}
                                  if partner_status == 200 else {"error": "no"})
        return httpx.Response(commission_status, json={"success": True} if commission_status == 200 else {})
    return httpx.MockTransport(handler)


async def test_drain_resolves_a_code_to_its_partner_and_creates_the_lead(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "dub_link_domain", "treg.dub.link")
    org_id = (await _orgs_with_ref(c, "jane-doe", headers={"CF-IPCountry": "AU"})).json()["org_id"]
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_code_transport(200, 200, seen)) as client:
        assert await _drain(client) == {"sent": 1, "retried": 0, "failed": 0}
    import json
    assert dict(seen[0].url.params) == {"domain": "treg.dub.link", "key": "jane-doe"}
    assert str(seen[1].url) == "https://api.dub.co/partners/pn_1"
    assert str(seen[2].url) == "https://api.dub.co/commissions"
    assert json.loads(seen[2].content) == {"type": "lead", "partnerId": "pn_1", "customer": {
        "externalId": str(org_id), "country": "AU", "email": "bob@superdesign.dev"}}


async def test_an_unknown_code_is_dead_lettered_and_tags_no_stripe_customer(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "dub_link_domain", "treg.dub.link")
    org_id = (await _orgs_with_ref(c, "nobody")).json()["org_id"]
    async with httpx.AsyncClient(transport=_code_transport(404, 200, [])) as client:
        assert await _drain(client) == {"sent": 0, "retried": 0, "failed": 1}
    lead = (await _leads())[0]
    assert lead.failed_at is not None and "no Dub link" in lead.error
    async with session_maker() as db:
        assert await affiliates.stripe_metadata(db, org_id) == {}


async def test_a_code_lead_waits_for_the_link_domain(c):
    await _orgs_with_ref(c, "jane-doe")
    seen: list = []
    async with httpx.AsyncClient(transport=_code_transport(200, 200, seen)) as client:
        assert await _drain(client) == {"sent": 0, "retried": 1, "failed": 0}
    assert seen == []
    lead = (await _leads())[0]
    assert lead.failed_at is None and "dub_link_domain" in lead.error


async def test_a_code_lead_retries_on_a_server_error(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "dub_link_domain", "treg.dub.link")
    await _orgs_with_ref(c, "jane-doe")
    async with httpx.AsyncClient(transport=_code_transport(200, 503, [])) as client:
        assert await _drain(client) == {"sent": 0, "retried": 1, "failed": 0}
    lead = (await _leads())[0]
    assert lead.failed_at is None and lead.error.startswith("503")


async def _self_referral_lead(c, monkeypatch, partner_email: str):
    monkeypatch.setattr(get_settings(), "dub_link_domain", "treg.dub.link")
    org_id = (await _orgs_with_ref(c, "jane-doe")).json()["org_id"]
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_code_transport(200, 200, seen, partner_email=partner_email)) as client:
        result = await _drain(client)
    return org_id, seen, result


async def test_a_partner_code_on_the_partners_own_team_is_dead_lettered(c, monkeypatch):
    org_id, seen, result = await _self_referral_lead(c, monkeypatch, "bob@superdesign.dev")
    assert result == {"sent": 0, "retried": 0, "failed": 1}
    assert [r.url.path for r in seen] == ["/links/info", "/partners/pn_1"], "no lead is created"
    lead = (await _leads())[0]
    assert lead.failed_at is not None and lead.error == "self_referral" and lead.uploaded_at is None
    async with session_maker() as db:
        assert await affiliates.stripe_metadata(db, org_id) == {}


async def test_self_referral_sees_through_inbox_aliases(c, monkeypatch):
    _, _, result = await _self_referral_lead(c, monkeypatch, "Bob+affiliate@SuperDesign.dev")
    assert result["failed"] == 1 and (await _leads())[0].error == "self_referral"


def test_gmail_dot_aliases_are_one_inbox():
    owner = {promo_inbox("jane.doe@gmail.com")}
    assert affiliates._is_self(dub.Outcome(ok=True, retry=False, status=200, email="janedoe+x@googlemail.com"), owner)
    assert not affiliates._is_self(dub.Outcome(ok=True, retry=False, status=200, email="jane@other.example"), owner)
    assert not affiliates._is_self(dub.Outcome(ok=False, retry=True, status=503), owner)


async def test_a_partner_lookup_failure_retries_rather_than_blocking(c, monkeypatch):
    monkeypatch.setattr(get_settings(), "dub_link_domain", "treg.dub.link")
    await _orgs_with_ref(c, "jane-doe")
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_code_transport(200, 200, seen, partner_status=503)) as client:
        assert await _drain(client) == {"sent": 0, "retried": 1, "failed": 0}
    assert all(r.url.path != "/commissions" for r in seen)
    lead = (await _leads())[0]
    assert lead.failed_at is None and lead.error.startswith("503: partner lookup")


def _click_transport(partner_id: str | None, partner_email: str, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/track/lead":
            return httpx.Response(200, json={"click": {"id": "x"}, "customer": {},
                                             "link": {"partnerId": partner_id} if partner_id else None})
        return httpx.Response(200, json={"id": partner_id, "email": partner_email})
    return httpx.MockTransport(handler)


async def test_a_click_from_the_partners_own_link_leaves_the_team_untagged(c):
    org_id = await _attributed_team(c)
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "bob@superdesign.dev", seen)) as client:
        assert await _drain(client) == {"sent": 0, "retried": 0, "failed": 1}
    lead = (await _leads())[0]
    assert lead.error == "self_referral" and lead.uploaded_at is not None, "Dub has the lead; treg tags nothing"
    async with session_maker() as db:
        assert await affiliates.stripe_metadata(db, org_id) == {}


async def test_a_click_from_another_partner_is_sent(c):
    org_id = await _attributed_team(c)
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "jane@partner.example", seen)) as client:
        assert await _drain(client) == {"sent": 1, "retried": 0, "failed": 0}
    async with session_maker() as db:
        assert await affiliates.stripe_metadata(db, org_id) == {"dubCustomerExternalId": str(org_id)}


async def _set_customer(org_id: int, customer_id: str) -> None:
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.stripe_customer_id = customer_id
        db.add(org)
        await db.commit()


async def test_a_customer_made_before_verification_is_tagged_by_the_drain(c):
    org_id = await _attributed_team(c)
    await _set_customer(org_id, "cus_early")
    stripe_calls: list = []
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "jane@partner.example", [])) as client:
        await _drain(client, stripe_calls)
        await _drain(client, stripe_calls)
    assert stripe_calls == [("Customer.modify", {
        "api_key": "sk_test_suite", "id": "cus_early",
        "metadata": {"dubCustomerExternalId": str(org_id)}})], "tagged once, never again"
    assert (await _leads())[0].stripe_synced_at is not None


async def test_a_customer_committed_after_verification_is_still_tagged(c):
    org_id = await _attributed_team(c)
    stripe_calls: list = []
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "jane@partner.example", [])) as client:
        await _drain(client, stripe_calls)
        assert stripe_calls == [] and (await _leads())[0].stripe_synced_at is None, "no customer yet: stays pending"
        await _set_customer(org_id, "cus_late")
        await _drain(client, stripe_calls)
    assert stripe_calls == [("Customer.modify", {
        "api_key": "sk_test_suite", "id": "cus_late",
        "metadata": {"dubCustomerExternalId": str(org_id)}})]
    assert (await _leads())[0].stripe_synced_at is not None


async def test_a_self_referral_untags_an_existing_customer(c):
    org_id = await _attributed_team(c)
    await _set_customer(org_id, "cus_self")
    stripe_calls: list = []
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "bob@superdesign.dev", [])) as client:
        await _drain(client, stripe_calls)
    assert stripe_calls == [("Customer.modify", {
        "api_key": "sk_test_suite", "id": "cus_self", "metadata": {"dubCustomerExternalId": ""}})]


async def test_a_failed_stripe_sync_is_retried(c):
    org_id = await _attributed_team(c)
    await _set_customer(org_id, "cus_retry")
    attempts: list = []

    async def flaky(op, **kw):
        attempts.append(op)
        if len(attempts) == 1:
            raise RuntimeError("stripe down")
        return {}
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "jane@partner.example", [])) as client:
        await affiliates.drain_once(client, session_factory=session_maker, stripe_call=flaky)
        lead = (await _leads())[0]
        assert lead.stripe_synced_at is None and lead.next_attempt_at is not None
        await affiliates.drain_once(client, session_factory=session_maker, stripe_call=flaky)
        assert len(attempts) == 1, "not retried before its delay passes"
        await _due_now(lead.id)
        await affiliates.drain_once(client, session_factory=session_maker, stripe_call=flaky)
    assert len(attempts) == 2 and (await _leads())[0].stripe_synced_at is not None


async def test_a_click_whose_partner_check_fails_is_rechecked_not_acknowledged(c):
    org_id = await _attributed_team(c)
    state = {"partner_status": 503}
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/track/lead":
            return httpx.Response(200, json={"click": {"id": "x"}, "link": {"partnerId": "pn_1"}})
        if state["partner_status"] != 200:
            return httpx.Response(state["partner_status"], json={"error": "down"})
        return httpx.Response(200, json={"id": "pn_1", "email": "bob@superdesign.dev"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _drain(client) == {"sent": 0, "retried": 1, "failed": 0}
        lead = (await _leads())[0]
        assert lead.uploaded_at is not None and lead.verified_at is None and lead.partner_id == "pn_1"
        async with session_maker() as db:
            assert await affiliates.stripe_metadata(db, org_id) == {}
            row = await db.get(AffiliateLead, lead.id)
            row.next_attempt_at = None
            db.add(row)
            await db.commit()
        state["partner_status"] = 200
        assert await _drain(client) == {"sent": 0, "retried": 0, "failed": 1}
    assert [r.url.path for r in seen].count("/track/lead") == 1, "the lead is never sent twice"
    assert (await _leads())[0].error == "self_referral"


def test_the_lead_table_has_no_foreign_key():
    assert not AffiliateLead.__table__.foreign_keys


async def test_two_drains_never_work_the_same_lead(c):
    await _attributed_team(c)
    now = utcnow_naive()
    async with session_maker() as first:
        claimed_first, _ = await affiliates._due(first, now)
    async with session_maker() as second:
        claimed_second, _ = await affiliates._due(second, now)
    assert len(claimed_first) == 1 and claimed_second == [], "the claim keeps a second worker off the lead"


async def test_a_self_referral_found_after_another_drain_verified_it_untags_stripe(c, monkeypatch):
    org_id = await _attributed_team(c)
    await _set_customer(org_id, "cus_raced")

    async def raced_send(client, settings, lead, *, email, inboxes):
        # Another worker resent the lead, Dub deduplicated it, and that worker verified and tagged.
        async with session_maker() as db:
            row = await db.get(AffiliateLead, lead.id)
            row.verified_at = row.stripe_synced_at = row.uploaded_at = datetime(2026, 1, 1)
            db.add(row)
            await db.commit()
        return affiliates._Result(dub.Outcome(ok=True, retry=False, status=200), recorded=True, partner_id="pn_1",
                                  self_referral=True)
    monkeypatch.setattr(affiliates, "_send", raced_send)
    stripe_calls: list = []
    await _drain(None, stripe_calls)
    lead = (await _leads())[0]
    assert lead.error == "self_referral" and lead.verified_at is None
    assert stripe_calls == [("Customer.modify", {
        "api_key": "sk_test_suite", "id": "cus_raced", "metadata": {"dubCustomerExternalId": ""}})]
    async with session_maker() as db:
        assert await affiliates.stripe_metadata(db, org_id) == {}


async def _due_now(lead_id: int) -> None:
    async with session_maker() as db:
        row = await db.get(AffiliateLead, lead_id)
        row.next_attempt_at = None
        db.add(row)
        await db.commit()


async def test_a_team_that_pays_right_away_is_checked_before_its_checkout(c, monkeypatch):
    """Dub's Stripe app credits only a payment that carries the tag when it is made."""
    org_id = await _attributed_team(c)
    seen: list[httpx.Request] = []
    _payment_time_dub(monkeypatch, _click_transport("pn_1", "jane@partner.example", seen))
    calls = await _stripe_calls(monkeypatch)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        await billing.create_topup_checkout(db, org, 10)
    metadata = [kw["metadata"] for _, kw in calls if "metadata" in kw]
    assert len(metadata) == 2 and all(m.get("dubCustomerExternalId") == str(org_id) for m in metadata)
    assert [r.url.path for r in seen] == ["/track/lead", "/partners/pn_1"]
    lead = (await _leads())[0]
    assert lead.verified_at is not None
    async with httpx.AsyncClient(transport=_unreachable) as client:
        assert await _drain(client) == _IDLE, "the drain has nothing left to send"


async def test_a_self_referral_found_at_checkout_tags_nothing(c, monkeypatch):
    org_id = await _attributed_team(c)
    _payment_time_dub(monkeypatch, _click_transport("pn_1", "bob@superdesign.dev", []))
    calls = await _stripe_calls(monkeypatch)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        await billing.create_topup_checkout(db, org, 10)
    assert all("dubCustomerExternalId" not in kw.get("metadata", {}) for _, kw in calls)
    assert (await _leads())[0].error == "self_referral"


async def test_a_dub_outage_at_checkout_never_blocks_the_payment(c, monkeypatch):
    org_id = await _attributed_team(c)
    calls = await _stripe_calls(monkeypatch)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        result = await billing.create_topup_checkout(db, org, 10)
    assert result["url"] and all("dubCustomerExternalId" not in kw.get("metadata", {}) for _, kw in calls)
    lead = (await _leads())[0]
    assert lead.verified_at is None and lead.failed_at is None and lead.next_attempt_at is not None, \
        "left to the drain, with a retry delay"


async def test_a_slow_dub_at_checkout_is_cut_off(c, monkeypatch):
    import asyncio
    org_id = await _attributed_team(c)
    monkeypatch.setattr(affiliates, "CHECK_NOW_S", 0.05)

    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})
    _payment_time_dub(monkeypatch, httpx.MockTransport(slow))
    await _stripe_calls(monkeypatch)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        assert (await billing.create_topup_checkout(db, org, 10))["url"]
    lead = (await _leads())[0]
    assert lead.verified_at is None and lead.next_attempt_at is not None, "still claimed; the drain retries it"


async def test_a_lead_a_worker_holds_is_not_checked_again_at_checkout(c, monkeypatch):
    org_id = await _attributed_team(c)
    async with session_maker() as db:
        await affiliates._due(db, utcnow_naive())
    seen: list[httpx.Request] = []
    _payment_time_dub(monkeypatch, _click_transport("pn_1", "jane@partner.example", seen))
    async with session_maker() as db:
        assert await affiliates.stripe_metadata(db, org_id) == {}
    assert seen == []


def _deduplicated_transport(customers: list, partner_email: str, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/track/lead":
            return httpx.Response(200, json={"click": {"id": "x"}, "link": None})
        if request.url.path == "/customers":
            return httpx.Response(200, json=customers)
        return httpx.Response(200, json={"id": "pn_1", "email": partner_email})
    return httpx.MockTransport(handler)


async def test_a_deduplicated_lead_still_gets_the_self_referral_check(c):
    """The retry after a reply that timed out: Dub names no partner, its customer record does."""
    org_id = await _attributed_team(c)
    seen: list[httpx.Request] = []
    transport = _deduplicated_transport([{"externalId": str(org_id), "partner": {"id": "pn_1"}}],
                                        "bob@superdesign.dev", seen)
    async with httpx.AsyncClient(transport=transport) as client:
        assert await _drain(client) == {"sent": 0, "retried": 0, "failed": 1}
    assert [r.url.path for r in seen] == ["/track/lead", "/customers", "/partners/pn_1"]
    assert seen[1].url.params["externalId"] == str(org_id)
    assert (await _leads())[0].error == "self_referral"


async def test_a_deduplicated_lead_with_no_partner_is_verified(c):
    await _attributed_team(c)
    transport = _deduplicated_transport([{"externalId": "1", "partner": None}], "", [])
    async with httpx.AsyncClient(transport=transport) as client:
        assert await _drain(client) == {"sent": 1, "retried": 0, "failed": 0}


async def test_a_deduplicated_lead_dub_cannot_find_yet_is_rechecked(c):
    await _attributed_team(c)
    seen: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=_deduplicated_transport([], "", seen)) as client:
        assert await _drain(client) == {"sent": 0, "retried": 1, "failed": 0}
        lead = (await _leads())[0]
        assert lead.uploaded_at is not None and lead.verified_at is None
        await _due_now(lead.id)
        await _drain(client)
    assert [r.url.path for r in seen].count("/track/lead") == 1, "a recorded lead is only re-checked"


async def test_a_customer_stripe_refuses_is_settled_not_retried_forever(c):
    org_id = await _attributed_team(c)
    await _set_customer(org_id, "cus_gone")
    attempts: list = []

    async def refuse(op, **kw):
        attempts.append(op)
        raise stripe_adapter.InvalidRequestError("No such customer: 'cus_gone'", "id")
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "jane@partner.example", [])) as client:
        await affiliates.drain_once(client, session_factory=session_maker, stripe_call=refuse)
        await affiliates.drain_once(client, session_factory=session_maker, stripe_call=refuse)
    lead = (await _leads())[0]
    assert len(attempts) == 1 and lead.stripe_synced_at is not None and lead.error.startswith("stripe:")
    async with session_maker() as db:
        assert await affiliates.stripe_metadata(db, org_id) == {"dubCustomerExternalId": str(org_id)}, \
            "still the partner's team: a later customer is tagged at creation"


async def test_failing_customers_never_hold_up_the_rest(c, monkeypatch):
    monkeypatch.setattr(affiliates, "_BATCH", 1)
    first = await _attributed_team(c)
    r = await verified_signup(c, json={"email": "amy@superdesign.dev"},
                              headers=_cookies(**{AFFILIATE_COOKIE: "cm3w8x2xyz"}))
    second = r.json()["org_id"]
    await _set_customer(first, "cus_broken")
    await _set_customer(second, "cus_fine")
    tagged: list = []

    async def stripe_call(op, **kw):
        if kw["id"] == "cus_broken":
            raise RuntimeError("stripe down")
        tagged.append(kw["id"])
        return {}
    async with httpx.AsyncClient(transport=_click_transport("pn_1", "jane@partner.example", [])) as client:
        for _ in range(4):
            await affiliates.drain_once(client, session_factory=session_maker, stripe_call=stripe_call)
    assert tagged == ["cus_fine"]


async def test_a_partner_earns_on_the_person_not_every_team_they_make(c):
    from conftest import verified_identity
    token = await verified_identity(c, "bob@superdesign.dev")
    click = _cookies(**{AFFILIATE_COOKIE: "cm3w8x2abc"})
    first = await c.post("/orgs", json={"name": "Bob"}, headers={**click, "X-Treg-Token": token})
    second = await c.post("/orgs", json={"name": "Bob two", "ref": "jane-doe"},
                          headers={**click, "X-Treg-Token": token})
    assert first.status_code == second.status_code == 200
    assert [lead.org_id for lead in await _leads()] == [first.json()["org_id"]]


async def test_making_a_team_clears_the_parked_click_and_code(c):
    from conftest import verified_identity
    token = await verified_identity(c, "bob@superdesign.dev")
    r = await c.post("/orgs", json={"name": "Bob"}, headers={
        **_cookies(**{AFFILIATE_COOKIE: "cm3w8x2abc", PARTNER_CODE_COOKIE: "jane-doe"}),
        "X-Treg-Token": token})
    cleared = [h for h in r.headers.get_list("set-cookie") if "Max-Age=0" in h or "max-age=0" in h.lower()]
    assert any(h.startswith(f"{AFFILIATE_COOKIE}=") for h in cleared)
    assert any(h.startswith(f"{PARTNER_CODE_COOKIE}=") for h in cleared)


async def test_deleting_the_team_never_makes_its_creator_attributable_again(c):
    from conftest import verified_identity
    token = await verified_identity(c, "bob@superdesign.dev")
    click = _cookies(**{AFFILIATE_COOKIE: "cm3w8x2abc"})
    first = await c.post("/orgs", json={"name": "Bob"}, headers={**click, "X-Treg-Token": token})
    assert first.status_code == 200
    from treg.domain.governance import teams
    async with session_maker() as db:
        await teams.cascade_delete_org(await db.get(Org, first.json()["org_id"]), db)
        await db.commit()
    assert await _leads() == []
    again = await c.post("/orgs", json={"name": "Bob again", "ref": "jane-doe"},
                         headers={**click, "X-Treg-Token": token})
    assert again.status_code == 200, again.text
    assert await _leads() == []
    async with session_maker() as db:
        claims = (await db.execute(select(AffiliateClaim))).scalars().all()
    assert [claim.inbox for claim in claims] == [promo_inbox("bob@superdesign.dev")]
