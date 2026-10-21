"""Credit admission through real identity proofs, teams and money transactions."""
import asyncio
from datetime import timedelta

import httpx
import pytest
from sqlmodel import select

from conftest import verified_identity
from treg.config import get_settings
from treg.domain import money
from treg.domain.identity import promotions
from treg.infra.db import session_maker
from treg.models import CreditBlock, LedgerEntry, Org, SignupPromoBucket, SignupPromoClaim, User
from treg.timeutil import utcnow_naive


@pytest.fixture
def limits(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "promo_grant_ip_limit", 2)
    monkeypatch.setattr(settings, "promo_grant_domain_limit", 2)
    monkeypatch.setattr(settings, "promo_grant_window_s", 3600)
    return settings


async def _team(client, token, name, ip):
    response = await client.post("/orgs", json={"name": name},
        headers={"X-Treg-Token": token, "X-Forwarded-For": ip})
    assert response.status_code == 200, response.text
    return response.json()["org_id"]


async def _balance(ids):
    async with session_maker() as db:
        orgs = (await db.execute(select(Org).where(Org.id.in_(ids)))).scalars().all()
        grants = (await db.execute(select(LedgerEntry).where(
            LedgerEntry.org_id.in_(ids), LedgerEntry.kind == "grant"))).scalars().all()
        blocks = (await db.execute(select(CreditBlock).where(CreditBlock.org_id.in_(ids)))).scalars().all()
    total = sum(org.balance_micro for org in orgs)
    assert total == sum(block.remaining_micro for block in blocks)
    assert total == sum(entry.amount_micro for entry in grants)
    return total


@pytest.mark.parametrize("concurrent", [False, True])
@pytest.mark.parametrize("dimension", ["network", "domain", "global"])
async def test_related_verified_accounts_share_a_grant_limit(clients, limits, monkeypatch, concurrent, dimension):
    if dimension == "global":
        monkeypatch.setattr(limits, "promo_grant_global_limit", 2)
    tokens = [await verified_identity(clients,
        f"person{i}@{'branch'+str(i)+'.business.co.uk' if dimension == 'domain' else 'business'+str(i)+'.com'}")
        for i in range(5)]
    async def create(i):
        ip = "203.0.113.20" if dimension == "network" else f"203.0.113.{20+i}"
        return await _team(clients, tokens[i], f"team-{i}", ip)
    ids = await asyncio.gather(*(create(i) for i in range(5))) if concurrent else [await create(i) for i in range(5)]
    assert await _balance(ids) == 2 * limits.promo_grant_micro
    async with session_maker() as db:
        users = (await db.execute(select(User).where(User.email.like("person%@%")))).scalars().all()
        assert sum(user.signup_promo_available for user in users) == 3
        claims = (await db.execute(select(SignupPromoClaim).where(
            SignupPromoClaim.user_id.in_([u.id for u in users])))).scalars().all()
        assert len(claims) == 2


async def test_ipv6_rotation_shares_one_network_and_other_network_still_grants(clients, limits):
    tokens = [await verified_identity(clients, f"v6user{i}@gmail.com") for i in range(4)]
    ids = [await _team(clients, token, f"ipv6-{i}", f"2001:db8:1:2::{i+1}")
           for i, token in enumerate(tokens[:3])]
    assert await _balance(ids) == 2 * limits.promo_grant_micro
    other = await _team(clients, tokens[3], "other-network", "2001:db8:1:3::1")
    assert await _balance([other]) == limits.promo_grant_micro


async def test_failed_money_write_rolls_back_budgets_and_claim(clients, limits, monkeypatch):
    monkeypatch.setattr(limits, "promo_grant_ip_limit", 1)
    token = await verified_identity(clients, "rollback@business.com")
    original = money.grant
    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError("synthetic failure")
    with monkeypatch.context() as patch:
        patch.setattr(money, "grant", fail)
        failed = await _team(clients, token, "failed", "203.0.113.40")
    retry = await _team(clients, token, "retry", "203.0.113.40")
    assert await _balance([failed, retry]) == limits.promo_grant_micro


async def test_budget_rejection_does_not_consume_other_bucket_or_eligibility(clients, limits, monkeypatch):
    monkeypatch.setattr(limits, "promo_grant_domain_limit", 1)
    first = await verified_identity(clients, "first@business.com")
    deferred = await verified_identity(clients, "second@branch.business.com")
    other = await verified_identity(clients, "other@different.com")
    await _team(clients, first, "first", "203.0.113.50")
    rejected = await _team(clients, deferred, "deferred", "203.0.113.51")
    allowed = await _team(clients, other, "different", "203.0.113.51")
    assert await _balance([rejected, allowed]) == limits.promo_grant_micro
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "second@branch.business.com"))).scalar_one()
        assert user.signup_promo_available


async def test_sliding_window_releases_deferred_credit(clients, limits, monkeypatch):
    monkeypatch.setattr(limits, "promo_grant_ip_limit", 1)
    first = await verified_identity(clients, "first@window.com")
    second = await verified_identity(clients, "second@window.com")
    await _team(clients, first, "first", "203.0.113.60")
    deferred = await _team(clients, second, "deferred", "203.0.113.60")
    assert await _balance([deferred]) == 0
    future = utcnow_naive() + timedelta(seconds=limits.promo_grant_window_s + 1)
    monkeypatch.setattr(promotions, "utcnow_naive", lambda: future)
    retry = await _team(clients, second, "later", "203.0.113.60")
    assert await _balance([retry]) == limits.promo_grant_micro


async def test_missing_network_defers_without_breaking_team_creation(clients, limits):
    from treg.application import signup
    token = await verified_identity(clients, "missing@business.com")
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "missing@business.com"))).scalar_one()
    team = await signup.create_org(user=user, name="missing-ip", ad_cookie="", utm_cookie="", referral_cookie="")
    assert await _balance([team["org_id"]]) == 0
    retry = await _team(clients, token, "with-ip", "203.0.113.70")
    assert await _balance([retry]) == limits.promo_grant_micro


@pytest.mark.parametrize("provider", ["github", "google"])
@pytest.mark.parametrize("team_before_proof", [False, True])
async def test_social_accounts_share_the_same_credit_limit(clients, limits, monkeypatch, provider, team_before_proof):
    from treg.api import app

    monkeypatch.setattr(limits, "promo_grant_ip_limit", 1)
    monkeypatch.setattr(limits, f"{provider}_client_id", "test-client")
    current = {"email": "", "id": 0}
    def upstream(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "test-access"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": current["id"], "login": "test", "email": None})
        if request.url.path == "/user/emails":
            return httpx.Response(200, json=[{"email": current["email"], "verified": True, "primary": True}])
        return httpx.Response(200, json={"sub": current["email"], "email": current["email"], "email_verified": True})
    ids = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        with monkeypatch.context() as patch:
            patch.setattr(app.state, "http", outbound)
            for i in range(2):
                current["id"] = i + 100
                current["email"] = f"social{i}@provider{i}.com"
                if team_before_proof:
                    response = await clients.post("/users", json={"email": current["email"]})
                    assert response.status_code == 200, response.text
                    ids.append(response.json()["org_id"])
                assert (await clients.get(f"/auth/{provider}")).status_code == 302
                state = clients.cookies.get("treg_oauth_state")
                callback = await clients.get(f"/auth/{provider}/callback", params={"code": "test", "state": state},
                    headers={"X-Forwarded-For": "203.0.113.80"})
                assert callback.status_code == 302, callback.text
                me = await clients.get("/auth/me", headers={"X-Treg-Token": ""})
                assert me.status_code == 200 and me.json()["email"] == current["email"]
                ids.append(await _team(clients, "", f"social-{i}", "203.0.113.80"))
    assert await _balance(ids) == limits.promo_grant_micro
    token = await verified_identity(clients, "after-social@third.com")
    after = await _team(clients, token, "after-social", "203.0.113.80")
    assert await _balance([after]) == 0


@pytest.mark.parametrize("ip", ["::ffff:203.0.113.90", "203.0.113.90"])
async def test_aliases_do_not_charge_extra_quota(clients, limits, ip):
    first = await verified_identity(clients, "alias+one@gmail.com")
    alias = await verified_identity(clients, "a.l.i.a.s+two@googlemail.com")
    other = await verified_identity(clients, "other@gmail.com")
    ids = [await _team(clients, first, "first", ip),
           await _team(clients, alias, "alias", "203.0.113.90"),
           await _team(clients, other, "other", "203.0.113.90")]
    assert await _balance(ids) == 2 * limits.promo_grant_micro


@pytest.mark.parametrize(("first", "second"), [
    ("first@a.business.co.uk", "second@b.business.co.uk"),
    ("first@a.business.com", "second@business.com."),
    ("first@bücher.de", "second@xn--bcher-kva.de"),
    ("first@a.blogspot.com", "second@b.blogspot.com"),
])
async def test_equivalent_domains_share_credit_limits(clients, limits, monkeypatch, first, second):
    monkeypatch.setattr(limits, "promo_grant_domain_limit", 1)
    ids = [await _team(clients, await verified_identity(clients, email), f"domain-{i}", f"203.0.113.{100+i}")
           for i, email in enumerate((first, second))]
    assert await _balance(ids) == limits.promo_grant_micro


async def test_shared_mail_providers_only_skip_the_domain_limit(clients, limits, monkeypatch):
    monkeypatch.setattr(limits, "promo_grant_domain_limit", 1)
    ids = [await _team(clients, await verified_identity(clients, f"mail{i}@gmail.com"), f"mail-{i}",
                       f"203.0.113.{110+i}") for i in range(3)]
    assert await _balance(ids) == 3 * limits.promo_grant_micro


async def test_storage_failure_defers_and_does_not_consume_claim(clients, limits, monkeypatch):
    token = await verified_identity(clients, "store@business.com")
    async def unavailable(*args, **kwargs):
        raise RuntimeError("synthetic store failure")
    with monkeypatch.context() as patch:
        patch.setattr(promotions, "_admit_grant", unavailable)
        failed = await _team(clients, token, "store-failure", "203.0.113.120")
    retry = await _team(clients, token, "store-retry", "203.0.113.120")
    assert await _balance([failed]) == 0
    assert await _balance([retry]) == limits.promo_grant_micro


@pytest.mark.parametrize("concurrent", [False, True])
async def test_idna_spellings_of_one_inbox_share_one_grant(clients, limits, concurrent):
    tokens = [await verified_identity(clients, email)
              for email in ("same@bücher.de", "same@xn--bcher-kva.de")]
    async def create(i):
        return await _team(clients, tokens[i], f"idna-{i}", f"203.0.113.{130+i}")
    ids = await asyncio.gather(create(0), create(1)) if concurrent else [await create(0), await create(1)]
    assert await _balance(ids) == limits.promo_grant_micro
