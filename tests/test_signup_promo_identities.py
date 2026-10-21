"""Provider evidence must survive email, network, login-door and team changes."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256

import httpx
import pytest

from conftest import verified_identity
from treg.api import app
from treg.config import get_settings
from treg.domain import money
from treg import policy
from treg.infra.db import session_maker
from treg.models import User
from sqlalchemy import delete
from sqlmodel import select
from test_signup_promo_limits import _balance, _team


@asynccontextmanager
async def provider(monkeypatch, name, *, subject="12345", emails=None, profile_email=None):
    state = {"subject": int(subject) if name == "github" else subject, "emails": emails or ["first@one.example"],
             "profile_email": profile_email, "verified": True}
    monkeypatch.setattr(get_settings(), f"{name}_client_id", "test-client")

    def upstream(request):
        if request.method == "POST":
            return httpx.Response(200, json={"access_token": "test-access"})
        if request.url.path == "/user":
            return httpx.Response(200, json={"id": state["subject"], "login": "test",
                "email": state["profile_email"], "created_at": "2020-01-01T00:00:00Z"})
        if request.url.path == "/user/emails":
            if "pages" in state:
                page = state["pages"].get(int(request.url.params.get("page", 1)), [])
                return httpx.Response(200, json=page)
            return httpx.Response(200, json=[{"email": email, "verified": state["verified"],
                "primary": i == 0} for i, email in enumerate(state["emails"])])
        return httpx.Response(200, json={"sub": state["subject"], "email": state["emails"][0],
                                       "email_verified": state["verified"]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as outbound:
        monkeypatch.setattr(app.state, "http", outbound)
        yield state


async def social(client, name, ip="203.0.113.1"):
    assert (await client.get(f"/auth/{name}")).status_code == 302
    state = client.cookies.get("treg_oauth_state")
    response = await client.get(f"/auth/{name}/callback", params={"code": "test", "state": state},
                                headers={"X-Forwarded-For": ip})
    assert response.status_code == 302, response.text
    assert "error=" not in response.headers["location"], response.headers["location"]
    me = await client.get("/auth/me", headers={"X-Treg-Token": ""})
    assert me.status_code == 200, me.text


@pytest.mark.parametrize("name", ["github", "google"])
@pytest.mark.parametrize("concurrent", [False, True])
async def test_provider_email_rotation_shares_one_gift_across_networks(clients, monkeypatch, name, concurrent):
    async with provider(monkeypatch, name) as state:
        tokens = []
        for i in range(3):
            state["emails"] = [f"rotate{i}@business{i}.com"]
            await social(clients, name, f"203.0.113.{10+i}")
            tokens.append(await verified_identity(clients, state["emails"][0]))
        async def create(i):
            return await _team(clients, tokens[i], f"rotated-{i}", f"203.0.113.{20+i}")
        ids = await asyncio.gather(*(create(i) for i in range(3))) if concurrent else [await create(i) for i in range(3)]
    assert await _balance(ids) == get_settings().promo_grant_micro


@pytest.mark.parametrize("email_first", [False, True])
async def test_github_verified_secondary_email_cannot_collect_via_otp(clients, monkeypatch, email_first):
    primary, secondary = "primary@one.com", "secondary@two.com"
    ids = []
    if email_first:
        token = await verified_identity(clients, secondary)
        ids.append(await _team(clients, token, "email-first", "203.0.113.30"))
    async with provider(monkeypatch, "github", emails=[primary, secondary]):
        await social(clients, "github")
        ids.append(await _team(clients, "", "social-team", "203.0.113.31"))
    token = await verified_identity(clients, secondary)
    ids.append(await _team(clients, token, "secondary-team", "203.0.113.32"))
    assert await _balance(ids) == get_settings().promo_grant_micro


@pytest.mark.parametrize("name", ["github", "google"])
@pytest.mark.parametrize("legacy_claim", [False, True])
async def test_link_after_email_grant_carries_spent_status_to_new_provider_email(clients, monkeypatch, name, legacy_claim):
    first = "funded@one.com"
    token = await verified_identity(clients, first)
    original = await _team(clients, token, "funded", "203.0.113.40")
    if legacy_claim:
        from treg.models import SignupPromoIdentity
        async with session_maker() as db:
            await db.execute(delete(SignupPromoIdentity))
            await db.commit()
    async with provider(monkeypatch, name, emails=[first]) as state:
        await social(clients, name)
        state["emails"] = ["changed@two.com"]
        await social(clients, name)
        changed = await _team(clients, "", "changed", "203.0.113.41")
    assert await _balance([original, changed]) == get_settings().promo_grant_micro


async def test_independent_provider_accounts_receive_credit_automatically(clients, monkeypatch):
    ids = []
    async with provider(monkeypatch, "github") as state:
        for i in range(4):
            state.update(subject=i+100, emails=[f"independent{i}@company{i}.com"])
            await social(clients, "github")
            ids.append(await _team(clients, "", f"independent-{i}", f"203.0.113.{50+i}"))
    assert await _balance(ids) == 4 * get_settings().promo_grant_micro


@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_identity_claim_rolls_back_with_failed_money_write(clients, monkeypatch, error):
    async with provider(monkeypatch, "github"):
        await social(clients, "github")
        original = money.grant
        async def fail(*args, **kwargs):
            await original(*args, **kwargs)
            raise error("synthetic failure")
        with monkeypatch.context() as patch:
            patch.setattr(money, "grant", fail)
            if error is asyncio.CancelledError:
                with pytest.raises(asyncio.CancelledError):
                    await _team(clients, "", "failed-identity", "203.0.113.60")
                from treg.models import Org
                async with session_maker() as db:
                    failed = (await db.execute(select(Org.id).where(Org.name == "failed-identity"))).scalar_one()
            else:
                failed = await _team(clients, "", "failed-identity", "203.0.113.60")
        retry = await _team(clients, "", "retry-identity", "203.0.113.61")
    assert await _balance([failed]) == 0
    assert await _balance([retry]) == get_settings().promo_grant_micro


async def test_later_observed_alias_is_consumed_after_the_original_gift(clients, monkeypatch):
    async with provider(monkeypatch, "github", emails=["original@one.com"]) as state:
        await social(clients, "github")
        original = await _team(clients, "", "original", "203.0.113.70")
        state["emails"].append("later@two.com")
        await social(clients, "github")
    token = await verified_identity(clients, "later@two.com")
    later = await _team(clients, token, "later", "203.0.113.71")
    assert await _balance([original, later]) == get_settings().promo_grant_micro


async def test_provider_evidence_outlives_deleted_user_and_teams(clients, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_token", "test-admin")
    async with provider(monkeypatch, "github", emails=["deleted@one.com"]) as state:
        await social(clients, "github")
        original = await _team(clients, "", "deleted-team", "203.0.113.80")
        assert await _balance([original]) == get_settings().promo_grant_micro
        async with session_maker() as db:
            user = (await db.execute(select(User).where(User.email == "deleted@one.com"))).scalar_one()
        removed = await clients.delete(f"/admin/users/{user.id}", headers={"X-Treg-Token": "test-admin"})
        assert removed.status_code == 200, removed.text
        state["emails"] = ["new-address@two.com"]
        await social(clients, "github")
        recreated = await _team(clients, "", "recreated-team", "203.0.113.81")
    assert await _balance([recreated]) == 0


async def test_github_pagination_includes_verified_aliases_beyond_first_page(clients, monkeypatch):
    async with provider(monkeypatch, "github") as state:
        state["pages"] = {
            1: [{"email": f"page{i}@one.com", "verified": True, "primary": i == 0} for i in range(100)],
            2: [{"email": "last@two.com", "verified": True, "primary": False}],
        }
        await social(clients, "github")
        original = await _team(clients, "", "paginated", "203.0.113.90")
    token = await verified_identity(clients, "last@two.com")
    last = await _team(clients, token, "last-page", "203.0.113.91")
    assert await _balance([original, last]) == get_settings().promo_grant_micro


@pytest.mark.parametrize(("name", "subject"), [
    ("github", None), ("github", True), ("github", -1), ("github", "123"),
    ("google", None), ("google", 123), ("google", ""), ("google", " spaced "),
])
async def test_malformed_subject_cannot_downgrade_into_email_only_credit(clients, monkeypatch, name, subject):
    async with provider(monkeypatch, name) as state:
        state["subject"] = subject
        await clients.get(f"/auth/{name}")
        nonce = clients.cookies.get("treg_oauth_state")
        response = await clients.get(f"/auth/{name}/callback", params={"code": "test", "state": nonce})
    assert response.status_code == 502
    assert "treg_session" not in response.cookies
    async with session_maker() as db:
        assert (await db.execute(select(User).where(User.email == state["emails"][0]))).first() is None


@pytest.mark.parametrize("verified", [False, "true", 1])
async def test_github_profile_email_is_never_a_substitute_for_verified_email_evidence(clients, monkeypatch, verified):
    async with provider(monkeypatch, "github", profile_email="first@one.example") as state:
        state["verified"] = verified
        await clients.get("/auth/github")
        nonce = clients.cookies.get("treg_oauth_state")
        response = await clients.get("/auth/github/callback", params={"code": "test", "state": nonce})
    assert response.status_code == 400
    async with session_maker() as db:
        assert (await db.execute(select(User).where(User.email == "first@one.example"))).first() is None


async def test_policy_gets_trusted_persisted_evidence_even_after_switching_to_email(clients, monkeypatch):
    seen = []
    async def handler(ctx):
        seen.append(ctx)
        return policy.CreditDecision.GRANT
    registry = policy.PolicyRegistry()
    registry.on_signup_credit(handler)
    monkeypatch.setattr(policy, "registry", registry)
    async with provider(monkeypatch, "github", emails=["evidence@one.com"]):
        await social(clients, "github")
    token = await verified_identity(clients, "evidence@one.com")
    org = await _team(clients, token, "evidence-team", "203.0.113.100")
    assert await _balance([org]) == get_settings().promo_grant_micro
    assert len(seen) == 1
    assert seen[0].client_ip == "203.0.113.100"
    assert seen[0].identities == (policy.SignupCreditIdentity(
        provider="github", subject_hash=sha256(b"12345").hexdigest(), account_created_at=datetime(2020, 1, 1)),)


async def test_simultaneous_otp_grant_and_oauth_alias_observation_do_not_reopen_credit(clients, monkeypatch):
    token = await verified_identity(clients, "racing@two.com")
    async with provider(monkeypatch, "github", emails=["primary@one.com", "racing@two.com"]):
        _, first = await asyncio.gather(social(clients, "github"),
            _team(clients, token, "racing-email", "203.0.113.110"))
        second = await _team(clients, "", "racing-social", "203.0.113.111")
    assert await _balance([first, second]) == get_settings().promo_grant_micro


async def test_incomplete_github_email_pagination_refuses_the_proof(clients, monkeypatch):
    async with provider(monkeypatch, "github") as state:
        state["pages"] = {i: [{"email": f"page{i}-{j}@one.com", "verified": True} for j in range(100)]
                          for i in range(1, 11)}
        await clients.get("/auth/github")
        nonce = clients.cookies.get("treg_oauth_state")
        response = await clients.get("/auth/github/callback", params={"code": "test", "state": nonce})
    assert response.status_code == 502
    async with session_maker() as db:
        assert (await db.execute(select(User).where(User.email.like("page%@one.com")))).first() is None


async def test_google_subject_is_opaque_and_case_sensitive(clients, monkeypatch):
    ids = []
    async with provider(monkeypatch, "google") as state:
        for i, subject in enumerate(("CaseSensitive", "casesensitive")):
            state.update(subject=subject, emails=[f"case{i}@company{i}.com"])
            await social(clients, "google")
            ids.append(await _team(clients, "", f"case-{i}", f"203.0.113.{120+i}"))
    assert await _balance(ids) == 2 * get_settings().promo_grant_micro
