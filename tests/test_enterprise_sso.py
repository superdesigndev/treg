"""Documented WorkOS response shapes exercised without a WorkOS account or network."""

import hashlib
import hmac
import json
import time
import base64
import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlmodel import delete, select

from treg import mcp
from treg import worker
from treg.application import auth as auth_use_cases, billing, enterprise_billing, enterprise_sso
from treg.domain.identity import session as identity_session
from treg.domain.identity import mcp_oauth
from treg.infra import workos
from treg.infra.db import session_maker
from treg.models import EnterpriseSSO, EnterpriseSSOAudit, EnterpriseSSOBlock, EnterpriseSSOIdentity, Membership, Org, User
from treg.timeutil import utcnow_naive


def profile(org="org_workos_1", email="employee@example.com"):
    return {"id": "prof_01DMC79VCBZ0NY2099737PSVF1", "organization_id": org,
            "connection_id": "conn_01E4ZCR3C56J083X43JQXF3JK5", "idp_id": "103456789012345678901",
            "email": email, "connection_type": "OktaSAML"}


def organization(org_id: int, *, domains: list[dict] | None = None,
                 allow_outside: bool = False) -> dict:
    return {"id": "org_workos_1", "external_id": str(org_id),
            "allow_profiles_outside_organization": allow_outside,
            "domains": domains if domains is not None else
                [{"domain": "example.com", "state": "verified"}]}


async def signin(org_id: int, sso_profile: dict, *, proven_user_id: int | None = None,
                 workos_org: dict | None = None):
    return await enterprise_sso.finish_signin(org_id, sso_profile,
        organization=workos_org or organization(org_id), proven_user_id=proven_user_id)


async def team(clients, *, plan="sso"):
    orgs = (await clients.get("/orgs")).json()
    org_id = orgs[0]["org_id"]
    async with session_maker() as db:
        db.add(EnterpriseSSO(org_id=org_id, plan=plan, subscription_status="active",
                             workos_org_id="org_workos_1"))
        await db.commit()
    return org_id


async def test_sso_profile_must_match_team_and_jit_is_member(clients):
    org_id = await team(clients)
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="another team"):
        await signin(org_id, profile(org="org_other"))
    result = await signin(org_id, profile())
    assert result.org_id == org_id
    assert (await signin(org_id, profile())).user_id == result.user_id
    async with session_maker() as db:
        member = (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one()
        assert member.role == "member"
        audit = (await db.execute(select(EnterpriseSSOAudit).where(
            EnterpriseSSOAudit.org_id == org_id))).scalars().all()
        assert [row.event for row in audit] == ["sso.signin", "sso.signin"]
        assert all(row.subject_user_id == result.user_id for row in audit)


async def test_owner_can_restore_manually_removed_sso_member(clients):
    org_id = await team(clients)
    result = await signin(org_id, profile())
    removed = await clients.delete(f"/orgs/{org_id}/members/{result.user_id}")
    assert removed.status_code == 200
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="deactivated"):
        await signin(org_id, profile())
    blocked = await clients.get(f"/orgs/{org_id}/sso/blocked")
    assert blocked.json() == {"members": [{"user_id": result.user_id, "email": "employee@example.com"}]}
    restored = await clients.post(f"/orgs/{org_id}/sso/blocked/{result.user_id}/restore")
    assert restored.status_code == 200
    assert (await signin(org_id, profile())).user_id == result.user_id
    assert (await clients.get(f"/orgs/{org_id}/sso/blocked")).json() == {"members": []}
    async with session_maker() as db:
        events = (await db.execute(select(EnterpriseSSOAudit.event).where(
            EnterpriseSSOAudit.org_id == org_id))).scalars().all()
        assert "sso.manual_access_removed" in events
        assert "sso.manual_access_restored" in events


async def test_directory_cannot_restore_a_manually_removed_member(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com", "state": "active"}
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "create", "event": "dsync.user.created", "data": base})
    result = await signin(org_id, profile())
    assert (await clients.delete(f"/orgs/{org_id}/members/{result.user_id}")).status_code == 200
    await enterprise_sso.apply_directory_event({"id": "update", "event": "dsync.user.updated", "data": base})
    await enterprise_sso.reconcile_directory(org_id, "directory_1", [base], 1000)
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="deactivated"):
        await signin(org_id, profile())
    assert (await clients.post(f"/orgs/{org_id}/sso/blocked/{result.user_id}/restore")).status_code == 200
    assert (await signin(org_id, profile())).user_id == result.user_id


async def test_sso_does_not_link_an_unverified_existing_account(clients):
    org_id = await team(clients)
    async with session_maker() as db:
        db.add(User(email="employee@example.com"))
        await db.commit()
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="sign in to your existing"):
        await signin(org_id, profile())
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "employee@example.com"))).scalar_one()
        assert user.email_verified_at is None
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == user.id))).scalar_one_or_none() is None


async def test_sso_does_not_take_over_a_verified_existing_account(clients):
    org_id = await team(clients)
    async with session_maker() as db:
        victim = User(email="employee@example.com", email_verified_at=utcnow_naive())
        db.add(victim)
        await db.commit()
        victim_id = victim.id
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="sign in to your existing"):
        await signin(org_id, profile())
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="sign in to your existing"):
        await signin(org_id, profile(), proven_user_id=victim_id + 1)
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == victim_id))).scalar_one_or_none() is None
    linked = await signin(org_id, profile(), proven_user_id=victim_id)
    assert linked.user_id == victim_id
    assert (await signin(org_id, profile())).user_id == victim_id


async def test_sso_requires_verified_company_domain_even_for_new_users(clients):
    org_id = await team(clients)
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="verify the company's email domain"):
        await signin(org_id, profile(), workos_org=organization(org_id, domains=[]))
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="verify the company's email domain"):
        await signin(org_id, profile(email="employee@other.example"))
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="not trusted"):
        await signin(org_id, profile(), workos_org=organization(org_id, allow_outside=True))
    async with session_maker() as db:
        assert (await db.execute(select(User).where(User.email == "employee@example.com"))).scalar_one_or_none() is None


async def test_enforced_sso_blocks_browser_approved_cli_and_oauth_consent(clients):
    org_id = await team(clients)
    employee = await signin(org_id, profile())
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        other = Org(name="Personal", slug="personal")
        db.add(other)
        await db.flush()
        db.add(Membership(org_id=other.id, user_id=employee.user_id, role="member", token_hash=""))
        await db.commit()
        slug, other_id = org.slug, other.id
    regular = identity_session.make_session(employee.user_id, token_version=employee.token_version)
    scoped = identity_session.make_session(employee.user_id, token_version=employee.token_version,
                                          sso_org_id=org_id)
    clients.cookies.set(identity_session.COOKIE, regular)
    old_additional = (await clients.post(f"/orgs/{org_id}/api-keys",
        json={"name": "Before SSO"}, headers={"X-Treg-Token": "", "X-Treg-Org": slug})).json()["secret"]
    old_agent = (await clients.post(f"/orgs/{org_id}/agents",
        json={"name": "before-sso"})).json()["token"]
    await enterprise_sso.set_enforced(org_id, True)
    old_key = identity_session.make_identity(employee.user_id, employee.token_version,
                                             org=slug, scope=identity_session.TEAM_SCOPE)
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Token": old_key, "X-Treg-Org": slug})).status_code == 403
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Token": old_additional, "X-Treg-Org": slug})).status_code == 403
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Token": old_agent, "X-Treg-Org": slug})).status_code == 403
    renewed_key = identity_session.make_identity(employee.user_id, employee.token_version,
        org=slug, scope=identity_session.TEAM_SCOPE, sso_org_id=org_id)
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Token": renewed_key, "X-Treg-Org": slug})).status_code == 200
    clients.cookies.set(identity_session.COOKIE, scoped)
    new_additional = (await clients.post(f"/orgs/{org_id}/api-keys",
        json={"name": "After SSO"}, headers={"X-Treg-Token": "", "X-Treg-Org": slug})).json()["secret"]
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Token": new_additional, "X-Treg-Org": slug})).status_code == 200
    new_agent = (await clients.post(f"/orgs/{org_id}/agents",
        json={"name": "after-sso"})).json()["token"]
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Token": new_agent, "X-Treg-Org": slug})).status_code == 200
    started = await auth_use_cases.start_cli_login()
    assert [o["slug"] for o in (await auth_use_cases.cli_orgs(regular))["orgs"]] == ["personal"]
    with pytest.raises(auth_use_cases.CliPairingError, match="sign_in_through_sso"):
        await auth_use_cases.approve_cli_login(regular, started["login_id"], started["code"], slug)
    assert (await auth_use_cases.approve_cli_login(
        scoped, started["login_id"], started["code"], slug))["ok"]

    registered = await clients.post("/oauth/register", json={
        "client_name": "Test Client", "redirect_uris": ["https://client.test/cb"]})
    client_id = registered.json()["client_id"]
    verifier = "sso-security-regression-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    args = dict(client_id=client_id, redirect_uri="https://client.test/cb", response_type="code",
                code_challenge=challenge, code_challenge_method="S256",
                resource=mcp_oauth.mcp_resource_url(), scope="treg:call")
    view = await auth_use_cases.prepare_oauth_authorization(session_cookie=regular, **args)
    assert [team["org_id"] for team in view.teams] == [other_id]
    with pytest.raises(auth_use_cases.OAuthServerError, match="access_denied"):
        await auth_use_cases.approve_oauth_authorization(
            session_cookie=regular, decision="allow", org_id=org_id, **args)
    view = await auth_use_cases.prepare_oauth_authorization(session_cookie=scoped, **args)
    assert [team["org_id"] for team in view.teams] == [org_id]
    with pytest.raises(auth_use_cases.OAuthServerError, match="access_denied"):
        await auth_use_cases.approve_oauth_authorization(
            session_cookie=scoped, decision="allow", org_id=other_id, **args)
    code = await auth_use_cases.approve_oauth_authorization(
        session_cookie=scoped, decision="allow", org_id=org_id, **args)
    tokens = await auth_use_cases.exchange_oauth_token(
        grant_type="authorization_code", code=code, redirect_uri="https://client.test/cb",
        client_id=client_id, code_verifier=verifier, resource=args["resource"], refresh_token="")
    assert mcp_oauth.read_access_token(tokens["access_token"],
        expected_audience=args["resource"])["sso_org_id"] == org_id
    bridge = await mcp._internal_auth(tokens["access_token"])
    assert (await clients.get(f"/orgs/{org_id}/balance", headers=bridge)).status_code == 200
    with pytest.raises(auth_use_cases.OAuthGrantError, match="SSO"):
        await auth_use_cases.move_oauth_grant(user_id=employee.user_id,
            family_id=(await auth_use_cases.list_oauth_grants(employee.user_id))[0]["grant"],
            team="personal")


async def test_oauth_grant_from_regular_login_stops_when_sso_becomes_required(clients):
    org_id = await team(clients)
    employee = await signin(org_id, profile())
    regular = identity_session.make_session(employee.user_id, token_version=employee.token_version)
    registered = await clients.post("/oauth/register", json={
        "client_name": "Test Client", "redirect_uris": ["https://client.test/cb"]})
    client_id = registered.json()["client_id"]
    verifier = "before-enforcement-verifier"
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    resource = mcp_oauth.mcp_resource_url()
    code = await auth_use_cases.approve_oauth_authorization(
        session_cookie=regular, decision="allow", org_id=org_id,
        client_id=client_id, redirect_uri="https://client.test/cb", response_type="code",
        code_challenge=challenge, code_challenge_method="S256", resource=resource, scope="treg:call")
    tokens = await auth_use_cases.exchange_oauth_token(
        grant_type="authorization_code", code=code, redirect_uri="https://client.test/cb",
        client_id=client_id, code_verifier=verifier, resource=resource, refresh_token="")
    await enterprise_sso.set_enforced(org_id, True)
    bridge = await mcp._internal_auth(tokens["access_token"])
    assert (await clients.get(f"/orgs/{org_id}/balance", headers=bridge)).status_code == 403
    with pytest.raises(auth_use_cases.OAuthServerError, match="invalid_grant"):
        await auth_use_cases.exchange_oauth_token(
            grant_type="refresh_token", code="", redirect_uri="", client_id=client_id,
            code_verifier="", resource=resource, refresh_token=tokens["refresh_token"])


async def test_company_sso_cookie_cannot_open_another_team_or_mint_its_key(clients):
    org_id = await team(clients)
    result = await signin(org_id, profile())
    async with session_maker() as db:
        other = Org(name="Unrelated", slug="unrelated")
        db.add(other)
        await db.flush()
        db.add(Membership(org_id=other.id, user_id=result.user_id, role="member", token_hash=""))
        await db.commit()
        other_id = other.id
    clients.headers.pop("X-Treg-Token")
    clients.cookies.set(identity_session.COOKIE,
        identity_session.make_session(result.user_id, token_version=result.token_version,
                                      sso_org_id=org_id))
    assert {row["org_id"] for row in (await clients.get("/orgs")).json()} == {org_id}
    assert (await clients.get(f"/orgs/{org_id}/balance",
        headers={"X-Treg-Org": str(org_id)})).status_code == 200
    assert (await clients.get(f"/orgs/{other_id}/balance",
        headers={"X-Treg-Org": str(other_id)})).status_code == 403
    key = (await clients.get("/auth/cli-token", headers={"X-Treg-Org": str(org_id)})).json()["token"]
    assert identity_session.read_identity_claims(key)["sso_org_id"] == org_id
    assert (await clients.get(f"/orgs/{other_id}/balance", headers={
        "X-Treg-Token": key, "X-Treg-Org": str(other_id)})).status_code == 403
    assert (await clients.post("/orgs", json={"name": "Another"},
        headers={"X-Treg-Token": key})).status_code == 403
    assert (await clients.get("/auth/cli-token",
        headers={"X-Treg-Org": str(other_id)})).status_code == 403
    assert (await clients.post("/orgs", json={"name": "Another"})).status_code == 403


async def test_company_sso_session_cannot_become_platform_admin(clients):
    org_id = await team(clients)
    result = await signin(org_id, profile())
    async with session_maker() as db:
        user = await db.get(User, result.user_id)
        user.is_superadmin = True
        db.add(user)
        await db.commit()
    clients.headers.pop("X-Treg-Token")
    clients.cookies.set(identity_session.COOKIE,
        identity_session.make_session(result.user_id, token_version=result.token_version,
                                      sso_org_id=org_id))
    assert (await clients.get("/admin/stats")).status_code == 403
    key = (await clients.get("/auth/cli-token", headers={"X-Treg-Org": str(org_id)})).json()["token"]
    assert (await clients.get("/admin/stats", headers={"X-Treg-Token": key})).status_code == 403


async def test_role_change_on_sso_team_is_durable_with_old_and_new_role(clients):
    org_id = await team(clients)
    result = await signin(org_id, profile())
    response = await clients.patch(f"/orgs/{org_id}/members/{result.user_id}",
        json={"role": "admin"})
    assert response.status_code == 200, response.text
    async with session_maker() as db:
        change = (await db.execute(select(EnterpriseSSOAudit).where(
            EnterpriseSSOAudit.org_id == org_id,
            EnterpriseSSOAudit.event == "sso.role_changed"))).scalar_one()
        assert (change.subject_user_id, change.from_role, change.to_role) == (
            result.user_id, "member", "admin")


async def test_sso_email_discovery_routes_but_does_not_join(clients, monkeypatch):
    from treg.config import get_settings
    org_id = await team(clients)
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        config.subscription_status = "past_due"  # SSO remains available during payment retries.
        await db.commit()
    monkeypatch.setattr(get_settings(), "workos_api_key", "sk_test")
    async def find(_client, *, key, domain):
        assert key == "sk_test" and domain == "example.com"
        return [{"id": "org_workos_1"}]
    monkeypatch.setattr(workos, "verified_organizations_for_domain", find)
    result = await clients.post("/auth/sso/discover", json={"email": "employee@example.com"})
    assert result.status_code == 200
    assert len(result.json()["teams"]) == 1
    async with session_maker() as db:
        assert (await db.execute(select(User).where(
            User.email == "employee@example.com"))).scalar_one_or_none() is None


async def test_workos_discovery_uses_verified_org_domain_without_listing_test_idp_connection():
    """WorkOS staging excludes its Test IdP from connection lists; org domains still route."""
    def response(request):
        assert request.url.path == "/organizations"
        assert request.url.params.get("domains[]") == "example.com"
        return httpx.Response(200, json={"data": [
            {"id": "org_verified", "domains": [{"domain": "example.com", "state": "verified"}]},
            {"id": "org_pending", "domains": [{"domain": "example.com", "state": "pending"}]},
        ], "list_metadata": {"after": None}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        found = await workos.verified_organizations_for_domain(client,
            key="sk_test", domain="example.com")
    assert [item["id"] for item in found] == ["org_verified"]


async def test_directory_deactivation_removes_team_access(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com"}
    assert await enterprise_sso.apply_directory_event({"id": "event_activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    assert await enterprise_sso.apply_directory_event({"id": "event_user", "event": "dsync.user.created",
        "data": {**base, "state": "active"}})
    result = await signin(org_id, profile())
    async with session_maker() as db:
        org_slug = (await db.get(Org, org_id)).slug
        employee = await db.get(User, result.user_id)
        employee_token_version = employee.token_version
    clients.cookies.set(identity_session.COOKIE, identity_session.make_session(
        result.user_id, token_version=employee_token_version, sso_org_id=org_id))
    employee_key = (await clients.get("/auth/cli-token", headers={
        "X-Treg-Token": "", "X-Treg-Org": org_slug})).json()["token"]
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Token": employee_key, "X-Treg-Org": org_slug})).status_code == 200
    event = {"id": "event_delete", "event": "dsync.user.deleted", "data": {**base, "state": "inactive"}}
    assert await enterprise_sso.apply_directory_event(event)
    assert not await enterprise_sso.apply_directory_event(event)
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None
        assert (await db.execute(select(EnterpriseSSOAudit).where(
            EnterpriseSSOAudit.org_id == org_id,
            EnterpriseSSOAudit.event == "sso.directory_access_removed"))).scalar_one().subject_user_id == result.user_id
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Token": employee_key, "X-Treg-Org": org_slug})).status_code in (401, 403)
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="not active"):
        await signin(org_id, profile())


async def test_reconciliation_repairs_a_missed_directory_removal(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com", "state": "active"}
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "add", "event": "dsync.user.created", "data": base})
    result = await signin(org_id, profile())
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="wrong directory roster"):
        await enterprise_sso.reconcile_directory(org_id, "directory_1", [{**base, "directory_id": "other"}], 1000)
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none()
    repaired = await enterprise_sso.reconcile_directory(org_id, "directory_1", [], 1000)
    assert repaired["memberships_removed"] == 1
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None


async def test_reconciliation_removes_pre_sync_identity_missing_from_roster(clients):
    org_id = await team(clients, plan="sso_directory")
    result = await signin(org_id, profile())
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    repaired = await enterprise_sso.reconcile_directory(org_id, "directory_1", [], 1000)
    assert repaired["memberships_removed"] == 1
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None


async def test_user_event_before_activation_and_lost_activation_repair(clients):
    org_id = await team(clients, plan="sso_directory")
    user = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com", "state": "active"}
    assert await enterprise_sso.apply_directory_event({"id": "first-user", "event": "dsync.user.created",
        "data": user})
    assert (await signin(org_id, profile())).org_id == org_id
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        config.directory_active = False
        db.add(config)
        await db.commit()
    repaired = await enterprise_sso.reconcile_directory(org_id, "directory_1", [user], 1000,
        verified_workos_org_id="org_workos_1")
    assert repaired["roster_size"] == 1
    assert (await signin(org_id, profile())).org_id == org_id


async def test_worker_recognizes_linked_workos_directory(clients, monkeypatch):
    from treg.config import get_settings
    org_id = await team(clients, plan="sso_directory")
    monkeypatch.setattr(get_settings(), "workos_api_key", "sk_test")

    async def list_dirs(client, *, key, organization):
        return [{"id": "directory_1", "organization_id": organization, "state": "linked"}]

    async def list_users(client, *, key, directory):
        return [{"id": "directory_user_1", "directory_id": directory,
                 "organization_id": "org_workos_1", "idp_id": profile()["idp_id"],
                 "email": "employee@example.com", "state": "active"}]

    monkeypatch.setattr(workos, "directories_for_organization", list_dirs)
    monkeypatch.setattr(workos, "directory_users", list_users)
    assert await worker._enterprise_reconcile(SimpleNamespace(org=org_id)) == 0
    async with session_maker() as db:
        config = await db.get(EnterpriseSSO, org_id)
        assert config.directory_id == "directory_1" and config.directory_active
    assert (await signin(org_id, profile())).org_id == org_id


async def test_suspended_directory_user_loses_access(clients):
    org_id = await team(clients, plan="sso_directory")
    user = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com", "state": "active"}
    await enterprise_sso.apply_directory_event({"id": "created", "event": "dsync.user.created",
        "data": user})
    result = await signin(org_id, profile())
    await enterprise_sso.apply_directory_event({"id": "suspended", "event": "dsync.user.updated",
        "data": {**user, "state": "suspended"}})
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None


async def test_whole_directory_removal_revokes_members(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com", "state": "active"}
    await enterprise_sso.apply_directory_event({"id": "event_activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "event_user", "event": "dsync.user.created", "data": base})
    result = await signin(org_id, profile())
    await enterprise_sso.apply_directory_event({"id": "event_directory_delete", "event": "dsync.deleted",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="directory access is inactive"):
        await signin(org_id, profile())


async def test_whole_directory_removal_revokes_pre_sync_members(clients):
    org_id = await team(clients, plan="sso_directory")
    result = await signin(org_id, profile())
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "delete", "event": "dsync.deleted",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None


async def test_older_directory_events_cannot_restore_access(clients):
    org_id = await team(clients, plan="sso_directory")
    base = {"organization_id": "org_workos_1", "directory_id": "directory_1",
            "id": "directory_user_1", "idp_id": profile()["idp_id"],
            "email": "employee@example.com"}
    await enterprise_sso.apply_directory_event({"id": "activate", "event": "dsync.activated",
        "created_at": "2026-01-01T00:00:00Z",
        "data": {"id": "directory_1", "organization_id": "org_workos_1"}})
    await enterprise_sso.apply_directory_event({"id": "deleted", "event": "dsync.user.deleted",
        "created_at": "2026-01-03T00:00:00Z", "data": {**base, "state": "inactive"}})
    assert not await enterprise_sso.apply_directory_event({"id": "old-create", "event": "dsync.user.created",
        "created_at": "2026-01-02T00:00:00Z", "data": {**base, "state": "active"}})
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="not active"):
        await signin(org_id, profile())


def test_workos_webhook_signature_and_portal_response():
    body = json.dumps({"id": "event_1", "event": "dsync.user.deleted", "data": {}}).encode()
    timestamp = str(int(time.time() * 1000))
    signature = hmac.new(b"whsec_test", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    assert workos.verify_event(body, f"t={timestamp},v1={signature}", "whsec_test")["id"] == "event_1"
    with pytest.raises(workos.WorkOSError):
        workos.verify_event(body + b" ", f"t={timestamp},v1={signature}", "whsec_test")


async def test_workos_portal_accepts_documented_url():
    def handle(request):
        assert request.url.path == "/portal/generate_link"
        return httpx.Response(200, json={"link": "https://setup.workos.com?token=example"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        url = await workos.portal_link(client, key="sk_test", organization="org_1", intent="sso")
    assert url == "https://setup.workos.com?token=example"


async def test_workos_portal_accepts_live_staging_url():
    def handle(request):
        return httpx.Response(201, json={"link": "https://setup.workos.com/init?apiToken=example"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        url = await workos.portal_link(client, key="sk_test", organization="org_1", intent="sso")
    assert url == "https://setup.workos.com/init?apiToken=example"


async def test_workos_invites_it_contact_with_both_setup_intents():
    calls = []
    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json={"data": []})
        if request.url.path.endswith("/invite"):
            assert json.loads(request.content) == {"intents": ["sso", "directory_sync"]}
            return httpx.Response(204)
        assert json.loads(request.content) == {"email": "it@example.com"}
        return httpx.Response(201, json={"id": "it_contact_123"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await workos.invite_it_contact(client, key="sk_test", organization="org_123",
            email="it@example.com", directory_sync=True)
    assert calls == [("GET", "/organizations/org_123/it_contacts"),
                     ("POST", "/organizations/org_123/it_contacts"),
                     ("POST", "/organizations/org_123/it_contacts/it_contact_123/invite")]


async def test_workos_code_exchange_uses_documented_profile_shape():
    def handle(request):
        assert request.url.path == "/sso/token"
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {"client_id": "client_test", "client_secret": "sk_test",
                        "code": "code_test", "grant_type": "authorization_code"}
        return httpx.Response(200, json={"access_token": "token_test", "profile": profile()})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        result = await workos.exchange_code(client, key="sk_test", client_id="client_test", code="code_test")
    assert result["organization_id"] == "org_workos_1"
    assert result["connection_type"] == "OktaSAML"


def stripe_price(price_id="price_bundle", amount_cents=29900):
    return {"id": price_id, "active": True, "currency": "usd", "unit_amount": amount_cents,
            "billing_scheme": "per_unit", "recurring": {"interval": "month", "interval_count": 1,
                                                  "usage_type": "licensed"}}


async def test_catalog_can_offer_bundle_only(monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "enterprise_sso_only_price_id", "price_sso")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "price_bundle")
    async def fake_call(operation, *, api_key, **kwargs):
        assert operation == enterprise_billing.stripe_adapter.PRICE_RETRIEVE
        return stripe_price(kwargs["id"], 17400 if kwargs["id"] == "price_sso" else 29900)
    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", fake_call)
    monkeypatch.setattr(settings, "enterprise_sso_bundle_only", False)
    assert {item["id"] for item in await enterprise_billing.catalog()} == {"sso", "sso_directory"}
    monkeypatch.setattr(settings, "enterprise_sso_bundle_only", True)
    assert [item["id"] for item in await enterprise_billing.catalog()] == ["sso_directory"]
    async def inactive_price(operation, *, api_key, **kwargs):
        return {**stripe_price(kwargs["id"]), "active": False}
    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", inactive_price)
    assert await enterprise_billing.catalog() == []
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "")
    assert await enterprise_billing.catalog() == []


async def test_stripe_price_sets_displayed_bundle_amount(monkeypatch):
    from treg.config import Settings
    monkeypatch.delenv("TREG_ENTERPRISE_SSO_BUNDLE_ONLY", raising=False)
    settings = Settings(_env_file=None)
    assert settings.enterprise_sso_bundle_only
    from treg.config import get_settings
    current = get_settings()
    monkeypatch.setattr(current, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(current, "enterprise_sso_bundle_only", True)
    monkeypatch.setattr(current, "enterprise_sso_bundle_price_id", "price_bundle")
    async def fake_call(operation, *, api_key, **kwargs):
        return stripe_price(kwargs["id"])
    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", fake_call)
    assert await enterprise_billing.catalog() == [
        {"id": "sso_directory", "price_id": "price_bundle",
         "usd_monthly_cents": 29900, "directory_sync": True}]


async def test_existing_subscription_status_does_not_call_stripe(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "workos_api_key", "sk_workos_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "price_bundle")
    org_id = await team(clients, plan="sso_directory")
    async def unavailable(*_args, **_kwargs):
        raise AssertionError("an existing subscriber must not need Stripe's Price API")
    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", unavailable)
    for subscription_status in ("active", "trialing", "past_due"):
        async with session_maker() as db:
            row = await db.get(EnterpriseSSO, org_id)
            row.subscription_status = subscription_status
            await db.commit()
        status = (await clients.get(f"/orgs/{org_id}/sso")).json()
        assert status["subscription_status"] == subscription_status
        assert status["billing_configured"] is True
        assert status["plans"] == []


async def test_member_status_skips_stripe_catalog_without_subscription(clients, monkeypatch):
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        membership = (await db.execute(select(Membership).where(Membership.org_id == org_id))).scalar_one()
        membership.role = "member"
        await db.commit()

    async def unexpected_catalog():
        raise AssertionError("a non-owner must not request Stripe prices")

    monkeypatch.setattr(enterprise_billing, "catalog", unexpected_catalog)
    response = await clients.get(f"/orgs/{org_id}/sso")
    assert response.status_code == 200
    assert response.json()["plans"] == []
    assert response.json()["sso_ready"] is False


async def test_stripe_price_floor_hides_offer_and_blocks_checkout(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "workos_api_key", "sk_workos_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_only", True)
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "price_bundle")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_min_price_usd", 250)
    async def cheap_price(operation, *, api_key, **kwargs):
        assert operation == enterprise_billing.stripe_adapter.PRICE_RETRIEVE
        return stripe_price("price_bundle", 2900)
    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", cheap_price)
    assert await enterprise_billing.catalog() == []
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    with pytest.raises(enterprise_billing.EnterpriseBillingError, match="configured minimum"):
        await enterprise_billing.start_checkout(org_id, "sso_directory",
            price_id="price_bundle", base="https://treg.example", email="owner@example.com")


async def test_browser_callback_requires_company_proof_for_enforced_team(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    org_id = await team(clients)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        slug = org.slug
    async def exchange(_client, **_kwargs):
        return profile()
    async def get_org(_client, **_kwargs):
        return organization(org_id)
    monkeypatch.setattr(workos, "exchange_code", exchange)
    monkeypatch.setattr(workos, "get_organization", get_org)
    start = await clients.get(f"/auth/sso/{slug}/start", params={"return_to": "/enrich-arena?run=saved-run"})
    assert start.status_code == 303
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = await clients.get("/auth/sso/callback", params={"state": state, "code": "code_test"})
    assert callback.status_code == 303
    assert callback.headers["location"] == "/enrich-arena?run=saved-run"
    await enterprise_sso.set_enforced(org_id, True)
    clients.headers.pop("X-Treg-Token")
    sso_access = await clients.get(f"/orgs/{org_id}/balance", headers={"X-Treg-Org": slug})
    assert sso_access.status_code == 200
    sso_key = await clients.get("/auth/cli-token", headers={"X-Treg-Org": slug})
    assert sso_key.status_code == 200
    async with session_maker() as db:
        user = (await db.execute(select(User).where(User.email == "employee@example.com"))).scalar_one()
    clients.cookies.set(identity_session.COOKIE,
        identity_session.make_session(user.id, token_version=user.token_version))
    regular_access = await clients.get(f"/orgs/{org_id}/balance", headers={"X-Treg-Org": slug})
    assert regular_access.status_code == 403
    assert "SSO" in regular_access.json()["detail"]
    regular_key = await clients.get("/auth/cli-token", headers={"X-Treg-Org": slug})
    assert regular_key.status_code == 403
    bearer_key = await clients.get("/auth/cli-token", headers={
        "X-Treg-Token": sso_key.json()["token"], "X-Treg-Org": slug})
    assert bearer_key.status_code == 200
    assert identity_session.read_identity_claims(bearer_key.json()["token"])["sso_org_id"] == org_id


async def test_existing_account_links_only_after_normal_browser_signin(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    org_id = await team(clients)
    async with session_maker() as db:
        slug = (await db.get(Org, org_id)).slug
        existing = User(email="employee@example.com", email_verified_at=utcnow_naive())
        db.add(existing)
        await db.commit()
        existing_id = existing.id
    async def exchange(_client, **_kwargs):
        return profile()
    async def get_org(_client, **_kwargs):
        return organization(org_id)
    monkeypatch.setattr(workos, "exchange_code", exchange)
    monkeypatch.setattr(workos, "get_organization", get_org)

    async def callback():
        start = await clients.get(f"/auth/sso/{slug}/start")
        state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
        return await clients.get("/auth/sso/callback", params={"state": state, "code": "code_test"})

    clients.cookies.delete(identity_session.COOKIE)
    refused = await callback()
    assert refused.status_code == 303 and "sso_error=link" in refused.headers["location"]
    landing = await clients.get(refused.headers["location"])
    assert landing.status_code == 200 and "You already have a treg account" in landing.text
    async with session_maker() as db:
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == existing_id))).scalar_one_or_none() is None
    clients.cookies.set(identity_session.COOKIE,
        identity_session.make_session(existing_id, token_version=0))
    linked = await callback()
    assert linked.status_code == 303 and linked.headers["location"].startswith("/app?sso=success")
    claims = identity_session.read_session_claims(linked.cookies.get(identity_session.COOKIE))
    assert claims["uid"] == existing_id and claims["sso_org_id"] == org_id


async def test_workos_domain_rejection_returns_to_signin_without_raw_error(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    org_id = await team(clients)
    async with session_maker() as db:
        slug = (await db.get(Org, org_id)).slug
    start = await clients.get(f"/auth/sso/{slug}/start")
    state = parse_qs(urlsplit(start.headers["location"]).query)["state"][0]
    callback = await clients.get("/auth/sso/callback", params={
        "state": state,
        "error": "profile_not_allowed_outside_organization",
        "error_description": "Do not show this raw message",
    })
    assert callback.status_code == 303
    assert callback.headers["location"] == f"/?sso_error=domain&team={slug}"
    assert "Do not show this raw message" not in callback.headers["location"]
    assert "treg_sso_state=" in callback.headers["set-cookie"]
    landing = await clients.get(callback.headers["location"])
    assert landing.status_code == 200
    assert 'id="sso-team"' not in landing.text
    assert 'treg-pending-sso' in landing.text
    assert '/media/signin-sso.js' in landing.text
    assert "Company SSO sign-in did not finish" in landing.text


async def test_subscription_webhooks_activate_then_cancel_without_balance_credit(clients):
    orgs = (await clients.get("/orgs")).json()
    org_id = orgs[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.stripe_customer_id = "cus_test"
        db.add(org)
        await db.commit()
    metadata = {"treg_kind": "enterprise_sso", "treg_org_id": str(org_id),
                "treg_plan": "sso_directory"}
    async with session_maker() as db:
        activated = await billing.handle_webhook_event(db, {"type": "customer.subscription.created",
            "created": 100, "data": {"object": {"id": "sub_test", "customer": "cus_test",
            "status": "active", "metadata": metadata}}})
        assert activated["handled"]
        row = await db.get(EnterpriseSSO, org_id)
        assert row.plan == "sso_directory" and row.subscription_status == "active"
        row.workos_org_id = "org_workos_1"
        db.add(row)
        await db.commit()
    result = await signin(org_id, profile())
    async with session_maker() as db:
        canceled = await billing.handle_webhook_event(db, {"type": "customer.subscription.deleted",
            "created": 200, "data": {"object": {"id": "sub_test", "customer": "cus_test",
            "metadata": metadata}}})
        assert canceled["handled"]
        stale = await billing.handle_webhook_event(db, {"type": "checkout.session.completed",
            "created": 150, "data": {"object": {"customer": "cus_test", "mode": "subscription",
            "subscription": "sub_test", "payment_status": "paid", "metadata": metadata}}})
        assert stale["stale"]
        row = await db.get(EnterpriseSSO, org_id)
        assert row.subscription_status == "canceled"
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == result.user_id))).scalar_one_or_none() is None


async def test_canceled_workos_setup_is_removed_and_retried(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "price_bundle")
    org_id = await team(clients, plan="sso_directory")
    signed_in = await signin(org_id, profile())
    assert (await clients.delete(f"/orgs/{org_id}/members/{signed_in.user_id}")).status_code == 200
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        row.subscription_status = "canceled"
        row.stripe_subscription_id = "sub_old"
        row.directory_id = "directory_known"
        row.directory_active = True
        await enterprise_sso.remove_sso_created_memberships(db, org_id)
        db.add(row)
        await db.commit()
    with pytest.raises(billing.EnterpriseBillingError, match="previous SSO setup"):
        await billing.start_checkout(org_id, "sso_directory",
            price_id="price_bundle", base="https://treg.example", email="owner@example.com")
    assert (await billing.status(org_id))["cleanup_pending"]

    async def get_org(client, *, key, organization):
        return {"id": organization, "external_id": str(org_id)}
    async def list_dirs(client, *, key, organization):
        return [{"id": "directory_extra", "organization_id": organization}] if "directory_extra" not in deleted else []
    async def list_conns(client, *, key, organization):
        return [{"id": "conn_extra", "organization_id": organization}] if "conn_extra" not in deleted else []
    async def delete_dir(client, *, key, directory):
        deleted.append(directory)
    async def delete_conn(client, *, key, connection):
        if fail_once.pop(0):
            raise workos.WorkOSError("temporary failure")
        deleted.append(connection)
    deleted = []
    fail_once = [True, False]
    monkeypatch.setattr(workos, "get_organization", get_org)
    monkeypatch.setattr(workos, "directories_for_organization", list_dirs)
    monkeypatch.setattr(workos, "connections_for_organization", list_conns)
    monkeypatch.setattr(workos, "delete_directory", delete_dir)
    monkeypatch.setattr(workos, "delete_connection", delete_conn)
    with pytest.raises(workos.WorkOSError):
        await billing.cleanup_canceled_workos(org_id)
    async with session_maker() as db:
        assert (await db.get(EnterpriseSSO, org_id)).workos_cleanup_completed_at is None
    result = await billing.cleanup_canceled_workos(org_id)
    assert result["connections_deleted"] == 1
    assert deleted == ["directory_extra", "conn_extra"]
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        assert row.workos_cleanup_completed_at is not None
        assert row.connection_id is None and row.directory_id is None
        assert (await db.execute(select(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.org_id == org_id))).scalar_one_or_none() is None
        assert (await db.execute(select(EnterpriseSSOBlock).where(
            EnterpriseSSOBlock.org_id == org_id,
            EnterpriseSSOBlock.user_id == signed_in.user_id))).scalar_one_or_none() is not None
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == signed_in.user_id))).scalar_one_or_none() is None
    assert (await billing.cleanup_canceled_workos(org_id))["skipped"] == "already cleaned"
    assert not (await billing.status(org_id))["cleanup_pending"]
    async def fake_stripe(operation, *, api_key, **kwargs):
        if operation == billing.stripe_adapter.PRICE_RETRIEVE:
            return stripe_price()
        return {"id": "cus_new"} if operation == billing.stripe_adapter.CUSTOMER_CREATE else {
            "url": "https://checkout.stripe.com/c/pay/renewal"}
    monkeypatch.setattr(billing.stripe_adapter, "call", fake_stripe)
    assert (await billing.start_checkout(org_id, "sso_directory",
        price_id="price_bundle", base="https://treg.example", email="owner@example.com"))["url"].endswith("renewal")


async def test_manual_block_survives_a_new_workos_connection_and_can_be_erased(clients):
    org_id = await team(clients)
    signed_in = await signin(org_id, profile())
    assert (await clients.delete(f"/orgs/{org_id}/members/{signed_in.user_id}")).status_code == 200
    async with session_maker() as db:
        await db.execute(delete(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.org_id == org_id))
        config = await db.get(EnterpriseSSO, org_id)
        config.connection_id = None
        db.add(config)
        await db.commit()
    replacement = {**profile(), "id": "prof_replacement", "connection_id": "conn_replacement"}
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="deactivated"):
        await signin(org_id, replacement, proven_user_id=signed_in.user_id)
    assert (await clients.get(f"/orgs/{org_id}/sso/blocked")).json() == {
        "members": [{"user_id": signed_in.user_id, "email": "employee@example.com"}]}
    async with session_maker() as db:
        await enterprise_sso.erase_user_links(db, signed_in.user_id)
        await db.commit()
        assert (await db.execute(select(EnterpriseSSOBlock).where(
            EnterpriseSSOBlock.org_id == org_id))).first() is None


async def test_global_user_deletion_erases_sso_identity_and_block(clients, monkeypatch):
    from treg.config import get_settings

    org_id = await team(clients)
    signed_in = await signin(org_id, profile())
    assert (await clients.delete(f"/orgs/{org_id}/members/{signed_in.user_id}")).status_code == 200
    monkeypatch.setattr(get_settings(), "admin_token", "test-admin-token")
    erased = await clients.delete(f"/admin/users/{signed_in.user_id}",
        headers={"X-Treg-Token": "test-admin-token"})
    assert erased.status_code == 200, erased.text
    async with session_maker() as db:
        assert await db.get(User, signed_in.user_id) is None
        assert (await db.execute(select(EnterpriseSSOBlock).where(
            EnterpriseSSOBlock.user_id == signed_in.user_id))).first() is None
        assert (await db.execute(select(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.user_id == signed_in.user_id))).first() is None


async def test_canceled_team_cannot_be_deleted_before_workos_cleanup(clients):
    org_id = await team(clients)
    slug = next(org["slug"] for org in (await clients.get("/orgs")).json()
                if org["org_id"] == org_id)
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        row.subscription_status = "canceled"
        db.add(row)
        await db.commit()
    response = await clients.delete(f"/orgs/{org_id}?confirm={slug}")
    assert response.status_code == 409
    assert "SSO setup cleanup" in response.json()["detail"]


async def test_workos_delete_resources_is_retry_safe():
    seen = []
    def handle(request):
        seen.append((request.method, request.url.path))
        assert request.headers["Authorization"] == "Bearer sk_test"
        return httpx.Response(404 if request.url.path.endswith("missing") else 204)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await workos.delete_directory(client, key="sk_test", directory="directory_missing")
        await workos.delete_connection(client, key="sk_test", connection="conn_present")
    assert seen == [("DELETE", "/directories/directory_missing"),
                    ("DELETE", "/connections/conn_present")]


async def test_billing_loss_preserves_preexisting_member_and_lifts_enforcement(clients):
    org_id = await team(clients)
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.stripe_customer_id = "cus_test"
        user = User(email="employee@example.com", email_verified_at=utcnow_naive())
        db.add(user)
        await db.flush()
        db.add(Membership(org_id=org_id, user_id=user.id, role="admin", token_hash=""))
        config = await db.get(EnterpriseSSO, org_id)
        config.stripe_subscription_id = "sub_test"
        db.add(org)
        db.add(config)
        await db.commit()
        user_id = user.id
    await signin(org_id, profile(), proven_user_id=user_id)
    async with session_maker() as db:
        identity = (await db.execute(select(EnterpriseSSOIdentity).where(
            EnterpriseSSOIdentity.org_id == org_id))).scalar_one()
        assert not identity.membership_created_by_sso
        config = await db.get(EnterpriseSSO, org_id)
        config.connection_id = profile()["connection_id"]
        db.add(config)
        await db.commit()
    await enterprise_sso.set_enforced(org_id, True)
    meta = {"treg_kind": "enterprise_sso", "treg_org_id": str(org_id), "treg_plan": "sso"}
    async with session_maker() as db:
        await billing.handle_webhook_event(db, {"type": "customer.subscription.updated",
            "created": 100, "data": {"object": {"id": "sub_test", "customer": "cus_test",
            "status": "past_due", "metadata": meta}}})
        config = await db.get(EnterpriseSSO, org_id)
        assert config.subscription_status == "past_due" and config.enforced
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == user_id))).scalar_one().role == "admin"
    assert (await signin(org_id, profile())).user_id == user_id
    async with session_maker() as db:
        await billing.handle_webhook_event(db, {"type": "customer.subscription.deleted",
            "created": 101, "data": {"object": {"id": "sub_test", "customer": "cus_test",
            "metadata": meta}}})
        assert (await db.execute(select(Membership).where(
            Membership.org_id == org_id, Membership.user_id == user_id))).scalar_one().role == "admin"
        assert not (await db.get(EnterpriseSSO, org_id)).enforced
    regular = identity_session.make_session(user_id, token_version=0)
    clients.cookies.set(identity_session.COOKIE, regular)
    clients.headers.pop("X-Treg-Token")
    assert (await clients.get(f"/orgs/{org_id}/balance", headers={
        "X-Treg-Org": str(org_id)})).status_code == 200


async def test_idp_id_cannot_link_two_profiles(clients):
    org_id = await team(clients)
    await signin(org_id, profile())
    second = {**profile(email="other@example.com"), "id": "prof_different"}
    with pytest.raises(enterprise_sso.EnterpriseSSOError, match="already linked"):
        await signin(org_id, second)


async def test_sso_session_can_revoke_tokens_without_losing_scope(clients):
    org_id = await team(clients)
    result = await signin(org_id, profile())
    await enterprise_sso.set_enforced(org_id, True)
    clients.headers.pop("X-Treg-Token")
    clients.cookies.set(identity_session.COOKIE, identity_session.make_session(
        result.user_id, token_version=result.token_version, sso_org_id=org_id))
    response = await clients.post("/auth/revoke-tokens")
    assert response.status_code == 200
    claims = identity_session.read_session_claims(response.cookies.get(identity_session.COOKIE))
    assert claims["sso_org_id"] == org_id
    assert identity_session.read_identity_claims(response.json()["token"])["sso_org_id"] == org_id
    clients.cookies.set(identity_session.COOKIE, response.cookies.get(identity_session.COOKIE))
    key = (await clients.post(f"/orgs/{org_id}/api-keys", json={"name": "Scoped key"},
        headers={"X-Treg-Token": "", "X-Treg-Org": str(org_id)})).json()["secret"]
    via_key = await clients.post("/auth/revoke-tokens", headers={"X-Treg-Token": key})
    assert via_key.status_code == 200
    assert identity_session.read_session_claims(
        via_key.cookies.get(identity_session.COOKIE))["sso_org_id"] == org_id


async def test_owner_can_lift_enforcement_after_subscription_ends(clients):
    org_id = await team(clients)
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        row.connection_id = profile()["connection_id"]
        db.add(row)
        await db.commit()
    await enterprise_sso.set_enforced(org_id, True)
    async with session_maker() as db:
        row = await db.get(EnterpriseSSO, org_id)
        row.subscription_status = "canceled"
        db.add(row)
        await db.commit()
    assert await enterprise_sso.set_enforced(org_id, False) is False


async def test_checkout_uses_monthly_team_subscription_without_touching_balance(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_workos_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_only", True)
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "price_bundle")
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    calls = []
    async def fake_call(operation, *, api_key, **kwargs):
        assert api_key == "sk_stripe_test"
        calls.append((operation, kwargs))
        if operation == enterprise_billing.stripe_adapter.PRICE_RETRIEVE:
            assert kwargs == {"id": "price_bundle"}
            return stripe_price()
        if operation == enterprise_billing.stripe_adapter.CUSTOMER_CREATE:
            return {"id": "cus_test"}
        return {"url": "https://checkout.stripe.com/c/pay/test"}
    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", fake_call)
    result = await enterprise_billing.start_checkout(org_id, "sso_directory",
        price_id="price_bundle", base="https://treg.example", email="owner@example.com")
    assert result["url"].startswith("https://checkout.stripe.com/")
    checkout = calls[-1][1]
    assert checkout["mode"] == "subscription"
    assert checkout["line_items"] == [{"quantity": 1, "price": "price_bundle"}]
    assert [operation for operation, _ in calls] == [
        enterprise_billing.stripe_adapter.PRICE_RETRIEVE,
        enterprise_billing.stripe_adapter.CUSTOMER_CREATE,
        enterprise_billing.stripe_adapter.CHECKOUT_SESSION_CREATE]
    assert checkout["subscription_data"]["metadata"]["treg_org_id"] == str(org_id)


async def test_checkout_refuses_nonmonthly_or_inactive_price(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_workos_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_only", True)
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "price_wrong")
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    calls = []

    async def fake_call(operation, *, api_key, **kwargs):
        calls.append(operation)
        price = stripe_price("price_wrong")
        price["recurring"]["interval"] = "year"
        return price

    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", fake_call)
    with pytest.raises(enterprise_billing.EnterpriseBillingError, match="flat monthly USD"):
        await enterprise_billing.start_checkout(org_id, "sso_directory",
            price_id="price_wrong", base="https://treg.example", email="owner@example.com")
    assert calls == [enterprise_billing.stripe_adapter.PRICE_RETRIEVE]
    async with session_maker() as db:
        assert (await db.get(EnterpriseSSO, org_id)).checkout_pending_until == 0


async def test_checkout_rejects_price_changed_since_plan_was_displayed(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_workos_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "price_new")
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    with pytest.raises(enterprise_billing.EnterpriseBillingError, match="price changed"):
        await enterprise_billing.start_checkout(org_id, "sso_directory", price_id="price_old",
            base="https://treg.example", email="owner@example.com")
    async with session_maker() as db:
        assert await db.get(EnterpriseSSO, org_id) is None


async def test_two_checkout_clicks_create_only_one_stripe_session(clients, monkeypatch):
    from treg.config import get_settings
    settings = get_settings()
    monkeypatch.setattr(settings, "workos_api_key", "sk_workos_test")
    monkeypatch.setattr(settings, "workos_client_id", "client_test")
    monkeypatch.setattr(settings, "stripe_secret_key", "sk_stripe_test")
    monkeypatch.setattr(settings, "stripe_webhook_secret", "whsec_test")
    monkeypatch.setattr(settings, "enterprise_sso_bundle_price_id", "price_bundle")
    org_id = (await clients.get("/orgs")).json()[0]["org_id"]
    async with session_maker() as db:
        org = await db.get(Org, org_id)
        org.stripe_customer_id = "cus_test"
        db.add(org)
        await db.commit()
    calls = []
    async def fake_call(operation, *, api_key, **kwargs):
        calls.append(operation)
        await asyncio.sleep(0.02)
        if operation == enterprise_billing.stripe_adapter.PRICE_RETRIEVE:
            assert kwargs == {"id": "price_bundle"}
            return stripe_price()
        return {"url": "https://checkout.stripe.com/c/pay/test"}
    monkeypatch.setattr(enterprise_billing.stripe_adapter, "call", fake_call)
    results = await asyncio.gather(*(
        enterprise_billing.start_checkout(org_id, "sso_directory",
            price_id="price_bundle", base="https://treg.example", email="owner@example.com")
        for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert calls == [enterprise_billing.stripe_adapter.PRICE_RETRIEVE,
                     enterprise_billing.stripe_adapter.CHECKOUT_SESSION_CREATE]
    successful = next(item for item in results if isinstance(item, dict))
    assert (await enterprise_billing.start_checkout(org_id, "sso_directory",
        price_id="price_bundle", base="https://treg.example", email="owner@example.com"))["url"] == successful["url"]
