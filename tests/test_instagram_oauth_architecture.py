"""Instagram's two explicit grants and catalog-call authorization contract."""

from __future__ import annotations

import json
from dataclasses import replace
from contextlib import asynccontextmanager
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import crypto
from treg import oauth_providers
from treg.application import connect as connect_use_cases
from treg.application.call import resolve as call_resolution
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import Secret, Tool
from treg.timeutil import utcnow_naive


@pytest.fixture
def instagram_apps(monkeypatch):
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_ID", "instagram-cid")
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_SECRET", "instagram-csec")
    monkeypatch.setenv("TREG_META_CLIENT_ID", "meta-cid")
    monkeypatch.setenv("TREG_META_CLIENT_SECRET", "meta-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_legacy_instagram_grants_keep_the_facebook_page_profile():
    assert oauth_providers.INSTAGRAM.authorization_method_name("") == "facebook-page"
    profile = oauth_providers.INSTAGRAM.profile_for_authorization("")
    parsed = urlsplit(profile.base_url)
    assert parsed.scheme == "https"
    assert parsed.hostname == "graph.facebook.com"
    assert profile.call_token_field == "page_access_token"


def test_legacy_grant_inference_uses_provider_metadata(monkeypatch):
    methods = tuple(
        replace(method, name="direct" if index == 0 else "delegated")
        for index, method in enumerate(oauth_providers.INSTAGRAM.authorization_methods)
    )
    provider = replace(
        oauth_providers.INSTAGRAM,
        service="future-social",
        legacy_authorization_method="delegated",
        authorization_methods=methods,
    )
    monkeypatch.setitem(oauth_providers.REGISTRY, provider.service, provider)
    secret = Secret(provider=provider.service, authorization_method="")
    assert call_resolution._authorization_method(secret) == "delegated"


async def _complete(clients: AsyncClient, body: dict) -> dict:
    started = await clients.post("/oauth/start", json=body)
    assert started.status_code == 200, started.text
    state = started.json()["state"]
    callback = await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    assert callback.status_code == 200, callback.text
    status = (await clients.get(f"/oauth/status/{state}")).json()
    assert status["status"] == "done", status
    return {**started.json(), **status}


async def test_instagram_manage_connect_uses_direct_login_and_records_identity(
    clients: AsyncClient, instagram_apps,
):
    started = await clients.post(
        "/oauth/start", json={"provider": "instagram", "capability": "manage"},
    )
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["consent_url"]).query)
    assert urlsplit(started.json()["consent_url"]).netloc == "www.instagram.com"
    assert query["client_id"] == ["instagram-cid"]
    assert query["enable_fb_login"] == ["false"]
    assert "instagram_business_manage_messages" in query["scope"][0].split(",")

    state = started.json()["state"]
    assert (await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")).status_code == 200
    status = (await clients.get(f"/oauth/status/{state}")).json()
    rows = (await clients.get("/connections")).json()
    connection = next(row for row in rows if row["id"] == status["secret_id"])
    assert connection["authorization_method"] == "instagram-login"
    assert connection["resource_ref"] == "17841400000000000"
    assert connection["resource_name"] == "direct_ig"
    assert connection["health"] == "ok"
    assert connection["supports_discovery"] is False

    async with session_maker() as db:
        secret = await db.get(Secret, status["secret_id"])
        blob = json.loads(crypto.decrypt(secret.value))
        assert blob["access_token"] == "IG-LONG-TOKEN"
        assert blob["refresh_grant_type"] == "ig_refresh_token"
        tool = (await db.execute(select(Tool).where(Tool.name == "instagram"))).scalars().one()
        assert tool.host == "graph.instagram.com"
        assert tool.bindings[0]["secret_field"] == "access_token"


async def test_plain_instagram_connect_defaults_to_approved_page_scopes(
    clients: AsyncClient, instagram_apps,
):
    started = await clients.post("/oauth/start", json={"provider": "instagram"})
    assert started.status_code == 200, started.text
    query = parse_qs(urlsplit(started.json()["consent_url"]).query)
    scopes = set(query["scope"][0].split())
    assert urlsplit(started.json()["consent_url"]).netloc == "www.facebook.com"
    assert "pages_show_list" in scopes
    assert "instagram_manage_messages" not in scopes
    assert "pages_messaging" not in scopes


@pytest.mark.parametrize(
    ("pending", "host", "scope_separator", "required_scope"),
    [
        ("instagram-login", "www.facebook.com", " ", "instagram_manage_messages"),
        ("", "www.instagram.com", ",", "instagram_business_manage_messages"),
    ],
)
async def test_review_setting_moves_plain_connect_to_each_approved_experience(
    clients: AsyncClient, instagram_apps, monkeypatch,
    pending, host, scope_separator, required_scope,
):
    monkeypatch.setenv("TREG_OAUTH_REVIEW_PENDING", pending)
    get_settings.cache_clear()
    try:
        started = await clients.post("/oauth/start", json={"provider": "instagram"})
        assert started.status_code == 200, started.text
        query = parse_qs(urlsplit(started.json()["consent_url"]).query)
        assert urlsplit(started.json()["consent_url"]).netloc == host
        assert required_scope in query["scope"][0].split(scope_separator)
        assert "review" not in started.json()["connect_guidance"].lower()
    finally:
        get_settings.cache_clear()


async def test_page_tools_are_a_separate_facebook_grant(
    clients: AsyncClient, instagram_apps,
):
    direct = await _complete(
        clients, {"provider": "instagram", "capability": "manage"},
    )
    page = await clients.post(
        "/oauth/start", json={"provider": "instagram", "capability": "page-tools"},
    )
    assert page.status_code == 200, page.text
    assert "linked to a Facebook Page" in page.json()["connect_guidance"]
    assert "separate from Instagram Login" in page.json()["connect_guidance"]
    query = parse_qs(urlsplit(page.json()["consent_url"]).query)
    assert urlsplit(page.json()["consent_url"]).netloc == "www.facebook.com"
    assert query["client_id"] == ["meta-cid"]
    assert "pages_show_list" in query["scope"][0].split()
    assert "instagram_manage_messages" not in query["scope"][0].split()
    assert "pages_messaging" not in query["scope"][0].split()
    state = page.json()["state"]
    assert (await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")).status_code == 200
    page_status = (await clients.get(f"/oauth/status/{state}")).json()

    rows = {row["id"]: row for row in (await clients.get("/connections")).json()}
    assert rows[direct["secret_id"]]["authorization_method"] == "instagram-login"
    assert rows[page_status["secret_id"]]["authorization_method"] == "facebook-page"
    assert rows[page_status["secret_id"]]["name"] == "instagram-page-tools"
    assert len(rows) == 2


async def test_catalog_call_selects_instagram_grant_when_facebook_shares_the_host(
    clients: AsyncClient, instagram_apps,
):
    await _complete(clients, {"provider": "facebook"})
    page = await _complete(
        clients, {"provider": "instagram", "capability": "page-messages"},
    )
    selected = await clients.post(
        f"/connections/{page['secret_id']}/resource",
        json={"resource_ref": "IG-DIRECT", "resource_name": "direct_ig"},
    )
    assert selected.status_code == 200, selected.text
    response = await clients.get(
        "/call/instagram.x.user-messages",
        params={"page_id": "PAGE-DIRECT", "platform": "instagram"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"][0]["id"] == "IG-CONVERSATION-1"


async def test_marketplace_secret_uses_the_newest_grant_for_a_method(
    clients: AsyncClient,
):
    async with session_maker() as db:
        old = Secret(
            org_id=1, name="instagram-old", owner="owner@example.com", kind="oauth",
            value=crypto.encrypt("{}"), provider="instagram",
            authorization_method="instagram-login",
        )
        new = Secret(
            org_id=1, name="instagram-new", owner="owner@example.com", kind="oauth",
            value=crypto.encrypt("{}"), provider="instagram",
            authorization_method="instagram-login",
        )
        db.add(old)
        await db.flush()
        db.add(new)
        await db.commit()
        selected = await call_resolution._marketplace_secret(
            "instagram", 1, db, ("instagram-login",),
        )
        assert selected is not None
        assert selected.id == new.id


async def test_catalog_call_explicitly_selects_each_instagram_authorization(
    clients: AsyncClient, instagram_apps,
):
    await _complete(clients, {"provider": "instagram", "capability": "manage"})
    page = await _complete(
        clients, {"provider": "instagram", "capability": "page-messages"},
    )
    selected = await clients.post(
        f"/connections/{page['secret_id']}/resource",
        json={"resource_ref": "IG-DIRECT", "resource_name": "direct_ig"},
    )
    assert selected.status_code == 200, selected.text

    direct = await clients.get(
        "/call/instagram.x.user-messages",
        params={"ig_user_id": "17841400000000000"},
        headers={"X-Treg-Authorization-Method": "instagram-login"},
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["raw_path"] == "/v25.0/17841400000000000/conversations"
    assert direct.json()["auth"] == "Bearer IG-LONG-TOKEN"

    page_call = await clients.get(
        "/call/instagram.x.user-messages",
        params={"page_id": "PAGE-DIRECT", "platform": "instagram"},
        headers={"X-Treg-Authorization-Method": "facebook-page"},
    )
    assert page_call.status_code == 200, page_call.text
    assert page_call.json()["data"][0]["page_id"] == "PAGE-DIRECT"

    missing_platform = await clients.get(
        "/call/instagram.x.user-messages",
        params={"page_id": "PAGE-DIRECT"},
        headers={"X-Treg-Authorization-Method": "facebook-page"},
    )
    assert missing_platform.status_code == 400, missing_platform.text
    assert "platform=<value>" in missing_platform.json()["detail"]


async def test_access_dry_run_reports_each_instagram_grant_independently(
    clients: AsyncClient, instagram_apps,
):
    await _complete(clients, {"provider": "instagram", "capability": "page-messages"})

    direct = await clients.get(
        "/catalog/endpoints/instagram.x.user-messages/access",
        params={"authorization_method": "instagram-login"},
    )
    assert direct.status_code == 200, direct.text
    assert direct.json()["tier"] == "none"

    page = await clients.get(
        "/catalog/endpoints/instagram.x.user-messages/access",
        params={"authorization_method": "facebook-page"},
    )
    assert page.status_code == 200, page.text
    assert page.json()["tier"] in {"tool", "credential"}
    assert page.json()["authorization_method"] == "facebook-page"


async def test_page_core_grant_guides_message_calls_to_the_message_upgrade(
    clients: AsyncClient, instagram_apps,
):
    await _complete(clients, {"provider": "instagram", "capability": "page-tools"})
    response = await clients.get(
        "/catalog/endpoints/instagram.x.user-messages/access",
        params={"authorization_method": "facebook-page"},
    )
    assert response.status_code == 200, response.text
    detail = response.json()
    assert detail["tier"] == "none"
    assert detail["connect_capability"] == "page-messages"
    assert detail["connect_command"].endswith("--capability page-messages")
    assert "needs more access" in detail["detail"]


async def test_page_only_access_dry_run_preserves_connect_guidance(
    clients: AsyncClient, instagram_apps,
):
    response = await clients.get(
        "/catalog/endpoints/instagram.x.user-recently-searched-hashtags/access",
        params={"authorization_method": "facebook-page"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "tier": "none",
        "authorization_method": "facebook-page",
        "connect_capability": "page-tools",
        "connect_command": "treg connections connect --provider instagram --capability page-tools",
        "action_label": "Enable Facebook Page tools",
        "missing_message": (
            "This tool requires Facebook Page authorization and an Instagram Professional "
            "account linked to that Page."
        ),
        "detail": (
            "no instagram credential in this org yet — connect with: "
            "treg connections connect --provider instagram --capability page-tools"
        ),
    }


async def test_instagram_method_rejects_the_other_methods_identifier(
    clients: AsyncClient, instagram_apps,
):
    await _complete(clients, {"provider": "instagram", "capability": "manage"})
    response = await clients.get(
        "/call/instagram.x.user-messages",
        params={"ig_user_id": "17841400000000000", "page_id": "PAGE-DIRECT"},
        headers={"X-Treg-Authorization-Method": "instagram-login"},
    )
    assert response.status_code == 400, response.text
    assert "does not accept page_id with instagram-login" in response.json()["detail"]


async def test_required_identity_lookup_runs_after_database_session_closes(
    clients: AsyncClient, instagram_apps, monkeypatch,
):
    real_session_maker = connect_use_cases.session_maker
    real_identity = connect_use_cases._record_connected_identity
    open_sessions = 0

    @asynccontextmanager
    async def tracked_session_maker():
        nonlocal open_sessions
        open_sessions += 1
        try:
            async with real_session_maker() as db:
                yield db
        finally:
            open_sessions -= 1

    async def checked_identity(*args, **kwargs):
        assert open_sessions == 0
        return await real_identity(*args, **kwargs)

    monkeypatch.setattr(connect_use_cases, "session_maker", tracked_session_maker)
    monkeypatch.setattr(connect_use_cases, "_record_connected_identity", checked_identity)
    await _complete(clients, {"provider": "instagram", "capability": "manage"})


async def test_page_discovery_runs_after_database_session_closes(
    clients: AsyncClient, instagram_apps, monkeypatch,
):
    from treg.api import app

    page = await _complete(
        clients, {"provider": "instagram", "capability": "page-tools"},
    )
    real_session_maker = connect_use_cases.session_maker
    real_client = app.state.http
    open_sessions = 0

    @asynccontextmanager
    async def tracked_session_maker():
        nonlocal open_sessions
        open_sessions += 1
        try:
            async with real_session_maker() as db:
                yield db
        finally:
            open_sessions -= 1

    class CheckedClient:
        async def get(self, *args, **kwargs):
            assert open_sessions == 0
            return await real_client.get(*args, **kwargs)

    monkeypatch.setattr(connect_use_cases, "session_maker", tracked_session_maker)
    result = await connect_use_cases.list_connection_resources(
        secret_id=page["secret_id"], org_id=1, client_factory=CheckedClient,
    )
    assert {item["id"] for item in result["resources"]} == {"IG-DIRECT", "IG-CLIENT"}


async def test_no_direct_professional_account_is_setup_required(
    clients: AsyncClient, instagram_apps, monkeypatch,
):
    monkeypatch.setitem(
        oauth_providers.REGISTRY,
        "instagram",
        replace(oauth_providers.INSTAGRAM, identity_path="/missing-professional-account"),
    )
    connected = await _complete(
        clients, {"provider": "instagram", "capability": "manage"},
    )
    rows = {row["id"]: row for row in (await clients.get("/connections")).json()}
    connection = rows[connected["secret_id"]]
    assert connection["health"] == "setup_required"
    assert "Business or Creator account" in connection["health_detail"]
    assert connection["health_detail"] == oauth_providers.INSTAGRAM.identity_missing_detail
    async with session_maker() as db:
        tool = (await db.execute(select(Tool).where(Tool.name == "instagram"))).scalars().first()
        assert tool is None


async def test_empty_page_discovery_is_not_reported_as_working(
    clients: AsyncClient, instagram_apps, monkeypatch,
):
    provider = oauth_providers.INSTAGRAM
    methods = tuple(
        replace(
            method,
            overrides=tuple({
                **dict(method.overrides),
                "discover_path": "/empty-resources",
                "discover_extra_path": "",
            }.items()),
        ) if method.name == "facebook-page" else method
        for method in provider.authorization_methods
    )
    monkeypatch.setitem(
        oauth_providers.REGISTRY, "instagram", replace(provider, authorization_methods=methods),
    )
    page = await _complete(
        clients, {"provider": "instagram", "capability": "page-tools"},
    )
    response = await clients.get(f"/connections/{page['secret_id']}/resources")
    assert response.status_code == 200, response.text
    assert response.json()["setup_required"] is True
    assert "linked to a Facebook Page" in response.json()["setup_detail"]
    rows = {row["id"]: row for row in (await clients.get("/connections")).json()}
    assert rows[page["secret_id"]]["health"] == "setup_required"


async def test_direct_connection_does_not_satisfy_a_page_only_tool(
    clients: AsyncClient, instagram_apps,
):
    await _complete(clients, {"provider": "instagram", "capability": "manage"})
    response = await clients.get("/call/instagram.x.hashtag-search")
    assert response.status_code == 428, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "authorization_missing"
    assert detail["required_authorization_method"] == "facebook-page"
    assert detail["required_capability"] == "page-tools"
    assert detail["cli_command"] == (
        "treg connections connect --provider instagram --capability page-tools"
    )
    assert detail["message"] == (
        "This tool requires Facebook Page authorization and an Instagram Professional account "
        "linked to that Page."
    )


async def test_expired_grant_returns_structured_reconnect_guidance(
    clients: AsyncClient, instagram_apps,
):
    connected = await _complete(
        clients, {"provider": "instagram", "capability": "manage"},
    )
    async with session_maker() as db:
        secret = await db.get(Secret, connected["secret_id"])
        blob = json.loads(crypto.decrypt(secret.value))
        blob.pop("refresh_token", None)
        secret.value = crypto.encrypt(json.dumps(blob))
        secret.expires_at = utcnow_naive() - timedelta(minutes=1)
        await db.commit()
    response = await clients.get(
        "/call/instagram.instagram.user.profile",
        params={"ig_user_id": "17841400000000000", "fields": "id,username"},
    )
    assert response.status_code == 428, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "authorization_expired"
    assert detail["required_authorization_method"] == "instagram-login"
