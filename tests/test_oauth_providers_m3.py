"""Milestone 3 — the rest of the Google family, Slack, and X.

X is the interesting one: it rejects an authorization code exchanged without a PKCE verifier, and
rejects the client secret in the request body. Both quirks are captured on the pending connect at
start time so the callback exchanges the code exactly the way the consent URL was built.
"""

from __future__ import annotations

from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from httpx import AsyncClient
from sqlmodel import select

from treg import oauth
from treg import oauth_providers as P
from treg.config import get_settings
from treg.infra.db import session_maker
from treg.models import PendingOAuth


@pytest.fixture
def all_apps(monkeypatch):
    for k in ("GOOGLE", "SLACK", "X", "TIKTOK"):
        monkeypatch.setenv(f"TREG_{k}_CLIENT_ID", f"{k.lower()}-cid")
        monkeypatch.setenv(f"TREG_{k}_CLIENT_SECRET", f"{k.lower()}-csec")
    monkeypatch.setenv("TREG_META_CLIENT_ID", "meta-cid")
    monkeypatch.setenv("TREG_META_CLIENT_SECRET", "meta-csec")
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_ID", "instagram-cid")
    monkeypatch.setenv("TREG_INSTAGRAM_CLIENT_SECRET", "instagram-csec")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _q(payload: dict) -> dict:
    return parse_qs(urlsplit(payload["consent_url"]).query)


def test_default_capability_is_the_broadest():
    """Connect asks for the fullest capability; a narrower one is chosen up front, not bolted on
    afterwards. satisfied_capabilities() is set containment, so every provider's tiers must be
    strict supersets in order, or a connection that can post would report it "cannot read"."""
    order = ("read", "draft", "post", "write", "manage")
    for provider in P.REGISTRY.values():
        tiers = [cap for cap in order if cap in provider.capabilities]
        if not tiers:
            continue  # bring-your-own credentials have no consent tiers
        for narrower, wider in zip(tiers, tiers[1:]):
            assert set(provider.scopes_for(narrower)) < set(provider.scopes_for(wider)), (
                provider.service, narrower, wider)
        assert provider.default_capability == tiers[-1], provider.service
    assert set(P.INSTAGRAM.scopes["page-tools"]) < set(P.INSTAGRAM.scopes["page-messages"])
    assert P.INSTAGRAM.connect_default_capability == "page-tools"
    # video.publish is the whole difference between "we drafted it for you" and "we posted it".
    assert "video.publish" not in P.TIKTOK.scopes_for("draft")
    assert "video.publish" in P.TIKTOK.scopes_for("post")
    # GTM can audit, prepare and publish without authority to delete a container or administer
    # the account's users.
    requested = {scope for scopes in P.GOOGLE_TAG_MANAGER.scopes.values() for scope in scopes}
    assert not {
        "https://www.googleapis.com/auth/tagmanager.delete.containers",
        "https://www.googleapis.com/auth/tagmanager.manage.users",
        "https://www.googleapis.com/auth/tagmanager.manage.accounts",
    } & requested


# ---- X's two quirks ----------------------------------------------------------------------
async def test_x_consent_url_carries_a_pkce_challenge(clients: AsyncClient, all_apps):
    d = (await clients.post("/oauth/start", json={"provider": "x"})).json()
    q = _q(d)
    assert q["code_challenge_method"] == ["S256"]
    assert q["code_challenge"], "X rejects a code exchanged without a verifier"
    # the verifier itself must stay server-side
    assert "code_verifier" not in q


async def test_pkce_challenge_matches_the_stored_verifier(clients: AsyncClient, all_apps):
    d = (await clients.post("/oauth/start", json={"provider": "x"})).json()
    challenge = _q(d)["code_challenge"][0]
    async with session_maker() as db:
        p = (await db.execute(select(PendingOAuth).where(PendingOAuth.state == d["state"]))).scalars().one()
    assert p.code_verifier
    assert oauth.pkce_challenge(p.code_verifier) == challenge
    assert p.token_endpoint_auth_method == "client_secret_basic"


async def test_google_does_not_use_pkce(clients: AsyncClient, all_apps):
    d = (await clients.post("/oauth/start", json={"provider": "google-search-console"})).json()
    assert "code_challenge" not in _q(d)


# ---- TikTok's two quirks -------------------------------------------------------------------
async def test_tiktok_consent_url_uses_client_key_not_client_id(clients: AsyncClient, all_apps):
    """TikTok ignores the OAuth2 spelling. Sending `client_id` gets a consent page that errors out
    rather than an obvious 400, so this is worth pinning."""
    q = _q((await clients.post("/oauth/start", json={"provider": "tiktok"})).json())
    assert q["client_key"] == ["tiktok-cid"]
    assert "client_id" not in q


async def test_tiktok_comma_joins_its_scopes(clients: AsyncClient, all_apps):
    """Space-joined scopes come back from TikTok as scope_not_authorized — it splits on commas."""
    q = _q((await clients.post("/oauth/start", json={"provider": "tiktok"})).json())
    scope = q["scope"][0]
    assert "," in scope and " " not in scope
    assert set(scope.split(",")) == set(P.TIKTOK.scopes_for(P.TIKTOK.default_capability))


async def test_tiktok_granted_scopes_are_stored_space_joined(clients: AsyncClient, all_apps, monkeypatch):
    """The wire dialect must not leak into storage: every reader of granted_scopes uses .split(),
    so a comma-joined grant would read as one bogus scope and report every capability unsatisfied."""
    # Registry mode takes token_uri from the provider, not the body, so point the provider itself at
    # the in-process upstream (frozen dataclass → replace rather than setattr).
    monkeypatch.setitem(P.REGISTRY, "tiktok", replace(P.TIKTOK, token_uri="http://upstream/token"))
    body = {"provider": "tiktok", "capability": "post"}
    state = (await clients.post("/oauth/start", json=body)).json()["state"]
    await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    sid = (await clients.get(f"/oauth/status/{state}")).json()["secret_id"]

    conn = {c["id"]: c for c in (await clients.get("/connections")).json()}[sid]
    assert set(conn["capabilities"]) == {"read", "draft", "post"}


# ---- per-provider consent params ---------------------------------------------------------
async def test_google_keeps_offline_consent_params(clients: AsyncClient, all_apps):
    """access_type=offline + prompt=consent is what guarantees Google returns a refresh_token."""
    q = _q((await clients.post("/oauth/start", json={"provider": "google-search-console"})).json())
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]


async def test_each_provider_uses_its_own_client_credentials(clients: AsyncClient, all_apps):
    for service, expected in (("google-search-console", "google-cid"),
                              ("google-tag-manager", "google-cid"), ("x", "x-cid")):
        q = _q((await clients.post("/oauth/start", json={"provider": service})).json())
        assert q["client_id"] == [expected], service


# ---- scope gap detection (the re-consent trigger) -----------------------------------------
def test_satisfied_capabilities_detects_a_scope_gap():
    """Providers never backfill scopes onto an issued grant — a later capability needs re-consent,
    and this is how we know to prompt instead of letting the call 403."""
    gsc = P.GOOGLE_SEARCH_CONSOLE
    read_only = gsc.scopes_for("read")
    assert gsc.satisfied_capabilities(read_only) == ["read"]
    assert "write" not in gsc.satisfied_capabilities(read_only)
    both = read_only + gsc.scopes_for("write")
    assert set(gsc.satisfied_capabilities(both)) == {"read", "write"}


async def test_unconfigured_providers_are_listed_but_flagged(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_X_CLIENT_ID", "")
    monkeypatch.setenv("TREG_X_CLIENT_SECRET", "")
    get_settings.cache_clear()
    try:
        rows = {p["service"]: p for p in (await clients.get("/oauth/providers")).json()}
        assert rows["x"]["configured"] is False
        r = await clients.post("/oauth/start", json={"provider": "x"})
        assert r.status_code == 422 and "not configured" in r.text
        # a bring-your-own-token provider needs nothing from the deployment, so it stays offerable
        assert rows["slack"]["configured"] is True
    finally:
        get_settings.cache_clear()


# ---- the gap, surfaced on the connection --------------------------------------------------
async def test_connection_reports_the_capability_it_lacks(clients: AsyncClient, all_apps):
    """Connect read-only, then see that `write` is named as missing — the reconnect trigger."""
    body = {
        "provider": "google-search-console", "capability": "read",
        "token_uri": "http://upstream/token",  # the in-process upstream stands in for Google
    }
    state = (await clients.post("/oauth/start", json=body)).json()["state"]
    await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    sid = (await clients.get(f"/oauth/status/{state}")).json()["secret_id"]

    conn = {c["id"]: c for c in (await clients.get("/connections")).json()}[sid]
    assert conn["capabilities"] == ["read"]
    assert conn["missing_capabilities"] == ["write"]


async def test_byo_connection_has_no_capability_fields(clients: AsyncClient):
    body = {"name": "byo", "client_id": "c", "client_secret": "s",
            "auth_uri": "http://p/auth", "token_uri": "http://upstream/token", "scopes": ["x"]}
    state = (await clients.post("/oauth/start", json=body)).json()["state"]
    await clients.get(f"/oauth/callback?code=AUTHCODE&state={state}")
    sid = (await clients.get(f"/oauth/status/{state}")).json()["secret_id"]
    conn = {c["id"]: c for c in (await clients.get("/connections")).json()}[sid]
    assert "missing_capabilities" not in conn  # nothing to compare against without a provider


async def test_linkedin_does_not_get_googles_consent_params(clients: AsyncClient, monkeypatch):
    monkeypatch.setenv("TREG_LINKEDIN_CLIENT_ID", "li-cid")
    monkeypatch.setenv("TREG_LINKEDIN_CLIENT_SECRET", "li-csec")
    get_settings.cache_clear()
    try:
        d = (await clients.post("/oauth/start", json={"provider": "linkedin"})).json()
        q = _q(d)
        assert q["client_id"] == ["li-cid"]
        assert "access_type" not in q and "prompt" not in q, "LinkedIn rejects Google's params"
        assert "w_member_social" in q["scope"][0]
    finally:
        get_settings.cache_clear()


def test_meta_messaging_stays_out_of_the_publish_tier():
    """A publish-only connect must never put "manage your messages" (or lead retrieval, or the
    Page's Messenger inbox) on the consent screen — the two-way surfaces live only in manage."""
    two_way = {
        "instagram_manage_messages", "instagram_manage_comments", "pages_messaging",
        "pages_manage_engagement", "leads_retrieval", "catalog_management",
    }
    for provider in (P.FACEBOOK, P.INSTAGRAM):
        for cap in ("read", "post"):
            assert not two_way & set(provider.scopes[cap]), (provider.service, cap)
    assert "instagram_business_manage_messages" in P.INSTAGRAM.scopes["manage"]
    assert not {"instagram_manage_messages", "pages_messaging"} & set(
        P.INSTAGRAM.scopes["page-tools"]
    )
    assert {"instagram_manage_messages", "pages_messaging"} <= set(
        P.INSTAGRAM.scopes["page-messages"]
    )


def test_instagram_consent_never_mentions_page_publishing():
    """Scopes are per capability. An Instagram connect asking for pages_manage_posts would put
    'manage your Pages' posts' on the consent screen for authority it never uses."""
    for cap in P.INSTAGRAM.scopes.values():
        assert "pages_manage_posts" not in cap


def test_meta_ads_read_can_still_list_accounts():
    """/me/adaccounts is a Business asset listing. Drop business_management from read and the
    connect consents cleanly, then offers an empty account picker."""
    for cap in P.META_ADS.scopes.values():
        assert "business_management" in cap
    assert set(P.META_ADS.scopes["read"]) < set(P.META_ADS.scopes["manage"])
    assert "ads_management" not in P.META_ADS.scopes["read"], "read must not be able to spend money"
